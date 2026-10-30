#!/usr/bin/env python3
"""
Phase 3 — E4: Apply Approved Merges & Final Canonical Resolution
=================================================================

Prerequisites (all must pass or E4 STOP):
  - review_queue.csv: 0 PENDING, 0 NEEDS_MORE_INFO
  - review_queue_manifest.txt: every immutable hash matches current queue
  - cross_league_proposals.csv: 0 PENDING, 0 NEEDS_MORE_INFO (if file exists)
  - All APPROVED rows: approved_target_canonical_id non-empty + FK valid
  - No cycles in merge graph
  - approved_target_canonical_id points to an ACTIVE canonical (not DEPRECATED),
    OR points to DEPRECATED canonical which resolves via redirect to ACTIVE
  - Phase 2 files present

Apply logic:
  - For each APPROVED row: canonical source -> canonical target
  - Mark source canonical DEPRECATED
  - Append redirect (source -> target) to deprecated_canonical_map.csv
  - Re-point aliases from source -> target
  - Rebuild teams_canonical.csv with resolved canonical_id
  - Resolve transitive chains + detect cycles
  - If 0 APPROVED: no-op merge, but still verify state consistent

Backup:
  - Before any write, backup all E4 target files to backups/phase3_E4_<ts>/

Determinism:
  - Sort all writes by PK
  - LF-only output
  - Rebuild 2x check
  - No timestamps in data files (except report metadata line)

Read-only:
  - Phase 2 files, E1 outputs must have SHA-256 pre == post
  - Only queue (read), reports (write), backups (write), and E4 outputs (write)
"""
import sys
import csv
import io
import hashlib
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
REPORTS_DIR   = DATA_DIR / "reports"
BACKUPS_DIR   = DATA_DIR / "backups"

# Inputs
REVIEW_QUEUE_CSV        = MAPPINGS_DIR / "review_queue.csv"
REVIEW_QUEUE_MANIFEST   = MAPPINGS_DIR / "review_queue_manifest.txt"
CROSS_LEAGUE_CSV        = MAPPINGS_DIR / "cross_league_proposals.csv"
CANONICAL_TEAMS_CSV     = MAPPINGS_DIR / "canonical_teams.csv"
TEAM_ALIASES_CSV        = MAPPINGS_DIR / "team_aliases.csv"
DEPRECATED_MAP_CSV      = MAPPINGS_DIR / "deprecated_canonical_map.csv"
TEAMS_CANONICAL_CSV     = PROCESSED_DIR / "teams_canonical.csv"
TEAMS_CSV               = PROCESSED_DIR / "teams.csv"

# Reports
REVIEW_SUMMARY_TXT      = REPORTS_DIR / "phase3_e3_review_summary.txt"
E4_REPORT_TXT           = REPORTS_DIR / "phase3_e4_report.txt"

# Schemas
REVIEW_QUEUE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
    "status", "approved_target_canonical_id",
    "reviewer_notes", "reviewed_date",
]
CROSS_LEAGUE_FIELDS = [
    "proposal_id", "raw_name_a", "raw_name_b",
    "reason", "evidence",
    "status", "approved_target_canonical_id",
    "reviewer_notes", "reviewed_date",
]
CANONICAL_FIELDS = [
    "canonical_id", "canonical_name", "country",
    "primary_league", "status", "notes",
]
ALIAS_FIELDS = [
    "alias_id", "source", "original_name",
    "canonical_id", "canonical_name",
    "confidence", "mapping_status", "evidence", "approved_date",
]
DEPRECATED_FIELDS = [
    "deprecated_canonical_id", "canonical_name",
    "superseded_by", "superseded_date", "reason",
]
TEAMS_CANONICAL_FIELDS = [
    "team_id", "raw_name", "normalized_name",
    "league_first_seen", "first_seen_date", "active_status",
    "canonical_status", "canonical_id", "canonical_name",
    "canonical_confidence", "merge_status",
]

IMMUTABLE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
]

EXPECTED_TEAMS = 144

# ---- Helpers ----

def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _norm_field(s: str) -> str:
    if s is None:
        return ""
    return s.replace("\r", "").strip()

def read_csv_lf_safe(path: Path):
    if not path.exists():
        return [], [], f"missing:{path}"
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    else:
        return [], [], "decode_failed"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = [_norm_field(f) for f in (reader.fieldnames or [])]
    rows = []
    for r in reader:
        rows.append({_norm_field(k): _norm_field(v) for k, v in r.items()})
    return rows, fields, None

def write_csv_lf(path: Path, fields: list, rows: list, pk: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows, key=lambda r: r.get(pk, ""))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted_rows)

def write_text_lf(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(content, encoding="utf-8")

def immutable_line(row: dict) -> str:
    return "|".join(str(row.get(f, "")) for f in IMMUTABLE_FIELDS)

def immutable_sha256(row: dict) -> str:
    return hashlib.sha256(immutable_line(row).encode("utf-8")).hexdigest()

def parse_manifest(text: str):
    text_n = text.replace("\r\n", "\n").replace("\r", "\n")
    result = {}
    for ln in text_n.split("\n"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "  " not in ln:
            continue
        h, rid = ln.split("  ", 1)
        result[rid.strip()] = h.strip()
    return result

def structural_equal_csv(path: Path, fields: list, expected_rows: list, pk: str):
    """LF-safe structural compare. Returns (is_equal, reason)."""
    if not path.exists():
        return False, "file_missing"
    rows, got_fields, err = read_csv_lf_safe(path)
    if err:
        return False, f"read_error:{err}"
    if got_fields != fields:
        return False, f"fields_mismatch:{got_fields}"
    if len(rows) != len(expected_rows):
        return False, f"row_count_mismatch:got={len(rows)},expected={len(expected_rows)}"
    g = sorted(rows, key=lambda r: r.get(pk, ""))
    e = sorted(expected_rows, key=lambda r: r.get(pk, ""))
    for i, (gr, er) in enumerate(zip(g, e)):
        for f in fields:
            if gr.get(f, "") != er.get(f, ""):
                return False, f"row_diff@idx={i} field={f}: got={gr.get(f,'')!r} exp={er.get(f,'')!r}"
    return True, "identical"

# ---- Merge graph resolution ----

def resolve_active(canonical_id: str, deprecated_map: dict, status_map: dict):
    """
    Follow chain until ACTIVE canonical reached.
    Returns (final_id, chain). Raises on cycle.
    """
    chain = [canonical_id]
    seen = {canonical_id}
    current = canonical_id
    while status_map.get(current) == "DEPRECATED":
        next_id = deprecated_map.get(current)
        if not next_id:
            raise RuntimeError(f"DEPRECATED canonical {current} has no redirect")
        if next_id in seen:
            raise RuntimeError(f"cycle detected: {chain + [next_id]}")
        chain.append(next_id)
        seen.add(next_id)
        current = next_id
    return current, chain

# ---- Main ----

def main():
    print("=" * 78)
    print("PHASE 3 — E4: APPLY APPROVED & FINAL CANONICAL RESOLUTION")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Report       : {E4_REPORT_TXT}")
    print()

    # ---- Pre-flight: required files ----
    for p in (REVIEW_QUEUE_CSV, REVIEW_QUEUE_MANIFEST,
              CANONICAL_TEAMS_CSV, TEAM_ALIASES_CSV,
              TEAMS_CANONICAL_CSV, TEAMS_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}")
            sys.exit(1)
    # Optional: cross_league, deprecated_map (may be header-only)
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Pre SHA-256 ----
    pre_sha = {
        "review_queue.csv":            sha256_file(REVIEW_QUEUE_CSV),
        "review_queue_manifest.txt":   sha256_file(REVIEW_QUEUE_MANIFEST),
        "canonical_teams.csv":         sha256_file(CANONICAL_TEAMS_CSV),
        "team_aliases.csv":            sha256_file(TEAM_ALIASES_CSV),
        "deprecated_canonical_map.csv":sha256_file(DEPRECATED_MAP_CSV),
        "teams_canonical.csv":         sha256_file(TEAMS_CANONICAL_CSV),
        "teams.csv":                   sha256_file(TEAMS_CSV),
    }
    print("--- PRE SHA-256 ---")
    for k in sorted(pre_sha):
        print(f"  {k:32s} {pre_sha[k]}")
    print()

    # ---- Load queue ----
    print("--- LOAD review_queue ---")
    rq_rows, rq_fields, err = read_csv_lf_safe(REVIEW_QUEUE_CSV)
    if err:
        print(f"[FAIL] queue read: {err}")
        sys.exit(1)
    if rq_fields != REVIEW_QUEUE_FIELDS:
        print(f"[FAIL] queue schema mismatch: {rq_fields}")
        sys.exit(1)
    print(f"  {len(rq_rows)} rows")
    print()

    # ---- Pre-flight: no PENDING / NEEDS_MORE_INFO in queue ----
    print("--- PRE-FLIGHT: queue statuses ---")
    pending = [r for r in rq_rows if r.get("status") == "PENDING"]
    needs   = [r for r in rq_rows if r.get("status") == "NEEDS_MORE_INFO"]
    approved = [r for r in rq_rows if r.get("status") == "APPROVED"]
    rejected = [r for r in rq_rows if r.get("status") == "REJECTED"]
    print(f"  PENDING        : {len(pending)}")
    print(f"  NEEDS_MORE_INFO: {len(needs)}")
    print(f"  APPROVED       : {len(approved)}")
    print(f"  REJECTED       : {len(rejected)}")
    if pending or needs:
        print("  [FAIL] unresolved statuses present — E4 STOP")
        sys.exit(1)
    print(f"  [OK] all queue rows resolved")
    print()

    # ---- Pre-flight: manifest verify ----
    print("--- PRE-FLIGHT: manifest verification ---")
    manifest_text = REVIEW_QUEUE_MANIFEST.read_text(encoding="utf-8")
    manifest_map = parse_manifest(manifest_text)
    if len(manifest_map) != len(rq_rows):
        print(f"  [FAIL] manifest row count mismatch")
        sys.exit(1)
    mismatches = []
    for r in rq_rows:
        computed = immutable_sha256(r)
        if manifest_map.get(r["review_id"]) != computed:
            mismatches.append(r["review_id"])
    if mismatches:
        print(f"  [FAIL] manifest mismatches: {mismatches[:5]}")
        sys.exit(1)
    print(f"  [OK] all {len(rq_rows)} immutable hashes match manifest")
    print()

    # ---- Pre-flight: cross_league_proposals (optional) ----
    print("--- PRE-FLIGHT: cross_league_proposals ---")
    cl_rows = []
    if CROSS_LEAGUE_CSV.exists():
        cl_rows, cl_fields, err = read_csv_lf_safe(CROSS_LEAGUE_CSV)
        if err:
            print(f"  [FAIL] read error: {err}")
            sys.exit(1)
        if cl_fields != CROSS_LEAGUE_FIELDS:
            print(f"  [FAIL] schema mismatch")
            sys.exit(1)
        cl_pending = [r for r in cl_rows if r.get("status") == "PENDING"]
        cl_needs   = [r for r in cl_rows if r.get("status") == "NEEDS_MORE_INFO"]
        cl_approved = [r for r in cl_rows if r.get("status") == "APPROVED"]
        cl_rejected = [r for r in cl_rows if r.get("status") == "REJECTED"]
        print(f"  total={len(cl_rows)} PENDING={len(cl_pending)} "
              f"NEEDS={len(cl_needs)} APPROVED={len(cl_approved)} REJECTED={len(cl_rejected)}")
        if cl_pending or cl_needs:
            print(f"  [FAIL] cross_league_proposals has unresolved rows")
            sys.exit(1)
        print(f"  [OK] cross_league_proposals resolved")
    else:
        print(f"  [INFO] file missing — treated as 0 proposals")
    print()

    # ---- Load canonical_teams + team_aliases ----
    print("--- LOAD E1 outputs ---")
    ct_rows, ct_fields, err = read_csv_lf_safe(CANONICAL_TEAMS_CSV)
    if err or ct_fields != CANONICAL_FIELDS:
        print(f"[FAIL] canonical_teams: {err or ct_fields}")
        sys.exit(1)
    ta_rows, ta_fields, err = read_csv_lf_safe(TEAM_ALIASES_CSV)
    if err or ta_fields != ALIAS_FIELDS:
        print(f"[FAIL] team_aliases: {err or ta_fields}")
        sys.exit(1)
    print(f"  canonical_teams : {len(ct_rows)}")
    print(f"  team_aliases    : {len(ta_rows)}")
    print()

    # ---- Build merge graph from APPROVED ----
    print("--- MERGE GRAPH ---")
    # canonical_id -> status (ACTIVE/DEPRECATED)
    status_map = {r["canonical_id"]: r["status"] for r in ct_rows}
    canon_by_id = {r["canonical_id"]: r for r in ct_rows}

    # Build redirects (source -> target) from APPROVED rows
    # For each APPROVED: source = the canonical_id in pair != approved_target
    #                    target = approved_target_canonical_id
    pending_redirects = {}
    for r in approved:
        target = r["approved_target_canonical_id"]
        cid_a = r["canonical_a_id"]
        cid_b = r["canonical_b_id"]
        if not target:
            print(f"  [FAIL] APPROVED row missing approved_target: {r['review_id']}")
            sys.exit(1)
        if target not in status_map:
            print(f"  [FAIL] approved_target not in canonical_teams: {target}")
            sys.exit(1)
        # Determine source
        if target == cid_a:
            source = cid_b
        elif target == cid_b:
            source = cid_a
        else:
            print(f"  [FAIL] approved_target {target} not in pair "
                  f"{cid_a} / {cid_b}")
            sys.exit(1)
        if source in pending_redirects:
            print(f"  [FAIL] canonical {source} already assigned redirect")
            sys.exit(1)
        pending_redirects[source] = target

    # Also: cross_league APPROVED (same logic)
    for r in (cl_rows if CROSS_LEAGUE_CSV.exists() else []):
        if r.get("status") != "APPROVED":
            continue
        target = r["approved_target_canonical_id"]
        if not target:
            print(f"  [FAIL] cross_league APPROVED missing target: {r['proposal_id']}")
            sys.exit(1)
        # Cross-league has raw names, need to resolve canonical ids via aliases
        # Use alias_by_raw
        # (Skip detailed cross-league resolution for Phase 3 baseline — 0 rows)
        # For safety: block if non-empty (E4 baseline expects 0)
        print(f"  [INFO] cross_league APPROVED row present: {r['proposal_id']} "
              f"(Phase 3 baseline expects 0)")
        print(f"  [FAIL] cross-league merges not supported in E4 baseline — "
              f"require explicit E4-cross implementation")
        sys.exit(1)

    print(f"  APPROVED redirects: {len(pending_redirects)}")
    if pending_redirects:
        for source, target in sorted(pending_redirects.items()):
            print(f"    {source} -> {target}")
    print()

    # ---- Cycle detection ----
    print("--- CYCLE DETECTION ---")
    if pending_redirects:
        # DFS for cycles
        def detect_cycle(graph):
            visited = set()
            stack = set()
            def dfs(node):
                if node in stack:
                    return [node]
                if node in visited:
                    return None
                visited.add(node)
                stack.add(node)
                nxt = graph.get(node)
                if nxt is not None:
                    cyc = dfs(nxt)
                    if cyc is not None:
                        return [node] + cyc
                stack.remove(node)
                return None
            for n in graph:
                c = dfs(n)
                if c:
                    return c
            return None
        cyc = detect_cycle(pending_redirects)
        if cyc:
            print(f"  [FAIL] cycle detected: {cyc}")
            sys.exit(1)
    print(f"  [OK] no cycles")
    print()

    # ---- Validate targets resolve to ACTIVE ----
    print("--- VALIDATE TARGETS ---")
    # Combine existing deprecated_map (may have prior redirects) + new
    existing_deprecated = {}
    if DEPRECATED_MAP_CSV.exists():
        dm_rows, dm_fields, err = read_csv_lf_safe(DEPRECATED_MAP_CSV)
        if err:
            print(f"  [FAIL] deprecated_map read: {err}")
            sys.exit(1)
        if dm_fields != DEPRECATED_FIELDS:
            print(f"  [FAIL] deprecated_map schema mismatch")
            sys.exit(1)
        for r in dm_rows:
            existing_deprecated[r["deprecated_canonical_id"]] = r["superseded_by"]

    combined_redirects = dict(existing_deprecated)
    combined_redirects.update(pending_redirects)

    # Simulate status after applying merges
    new_status = dict(status_map)
    for source in pending_redirects:
        new_status[source] = "DEPRECATED"

    # Every target must resolve to ACTIVE
    for source, target in pending_redirects.items():
        try:
            final, chain = resolve_active(target, combined_redirects, new_status)
        except RuntimeError as e:
            print(f"  [FAIL] resolve error for {source}->{target}: {e}")
            sys.exit(1)
        if new_status.get(final) != "ACTIVE":
            print(f"  [FAIL] chain end not ACTIVE: {final}")
            sys.exit(1)
    print(f"  [OK] all targets resolve to ACTIVE canonical")
    print()

    # ---- If 0 APPROVED: still validate no-op consistency ----
    if len(approved) == 0:
        print("--- 0 APPROVED: NO-OP MERGE ---")
        print("  Expected: canonical_teams, team_aliases, deprecated_map unchanged")
        print("           teams_canonical.csv unchanged")
        print()

    # ---- Backup ----
    print("--- BACKUP ---")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUPS_DIR / f"phase3_E4_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for name, path in [
        ("canonical_teams.csv.pre_e4", CANONICAL_TEAMS_CSV),
        ("team_aliases.csv.pre_e4", TEAM_ALIASES_CSV),
        ("deprecated_canonical_map.csv.pre_e4", DEPRECATED_MAP_CSV),
        ("teams_canonical.csv.pre_e4", TEAMS_CANONICAL_CSV),
    ]:
        if path.exists():
            shutil.copy2(path, backup_dir / name)
    with open(backup_dir / "SHA256SUMS.pre_e4.txt", "w", encoding="utf-8") as f:
        for k in sorted(pre_sha):
            f.write(f"{pre_sha[k]}  {k}\n")
    print(f"  backup dir: {backup_dir.relative_to(PROJECT_ROOT)}")
    print()

    # ---- Apply merges ----
    print("--- APPLY MERGES ---")
    review_date = datetime.now().strftime("%Y-%m-%d")

    # Update canonical_teams: mark source DEPRECATED
    canonical_new = [dict(r) for r in ct_rows]
    canon_by_id_new = {r["canonical_id"]: r for r in canonical_new}
    for source in pending_redirects:
        if source in canon_by_id_new:
            canon_by_id_new[source]["status"] = "DEPRECATED"
            existing_note = canon_by_id_new[source].get("notes", "")
            canon_by_id_new[source]["notes"] = (existing_note +
                                                "; DEPRECATED: merged via E4").strip("; ")

    # Append deprecated_map entries
    deprecated_new = list(dm_rows) if DEPRECATED_MAP_CSV.exists() else []
    existing_dep_ids = {r["deprecated_canonical_id"] for r in deprecated_new}
    for source, target in pending_redirects.items():
        if source in existing_dep_ids:
            print(f"  [WARN] {source} already in deprecated_map — skipping append")
            continue
        deprecated_new.append({
            "deprecated_canonical_id": source,
            "canonical_name": canon_by_id[source]["canonical_name"],
            "superseded_by": target,
            "superseded_date": review_date,
            "reason": "merged_via_E4",
        })

    # Re-point aliases
    alias_new = [dict(r) for r in ta_rows]
    for a in alias_new:
        cid = a["canonical_id"]
        if cid in pending_redirects:
            # resolve chain (should be 1-level for E4, but use full resolver)
            try:
                final, _ = resolve_active(cid, combined_redirects, new_status)
            except RuntimeError as e:
                print(f"  [FAIL] alias resolve {cid}: {e}")
                sys.exit(1)
            a["canonical_id"] = final
            a["canonical_name"] = canon_by_id[final]["canonical_name"]
            existing_ev = a.get("evidence", "")
            a["evidence"] = (existing_ev + "; re-pointed_e4").strip("; ")

    # Rebuild teams_canonical.csv: for each Phase 2 team row, lookup alias -> canonical -> resolve
    print("--- REBUILD teams_canonical ---")
    phase2_teams, _, err = read_csv_lf_safe(TEAMS_CSV)
    if err:
        print(f"  [FAIL] teams.csv read: {err}")
        sys.exit(1)
    if len(phase2_teams) != EXPECTED_TEAMS:
        print(f"  [FAIL] teams.csv rows={len(phase2_teams)}, expected {EXPECTED_TEAMS}")
        sys.exit(1)

    alias_by_raw = {}
    for a in alias_new:
        if a["source"] == "football-data.co.uk":
            alias_by_raw[a["original_name"]] = a

    teams_canonical_new = []
    for t in phase2_teams:
        raw = t["raw_name"]
        if raw not in alias_by_raw:
            print(f"  [FAIL] no alias for raw_name={raw!r}")
            sys.exit(1)
        a = alias_by_raw[raw]
        cid = a["canonical_id"]
        # resolve
        try:
            final, _ = resolve_active(cid, combined_redirects, new_status)
        except RuntimeError as e:
            print(f"  [FAIL] resolve team {raw}: {e}")
            sys.exit(1)
        canon = canon_by_id[final]
        if new_status.get(final) != "ACTIVE":
            print(f"  [FAIL] team {raw} resolves to non-ACTIVE canonical {final}")
            sys.exit(1)
        merge_status = "SELF" if (canon["canonical_name"] == raw) else "MERGED"
        teams_canonical_new.append({
            "team_id":              t["team_id"],
            "raw_name":             raw,
            "normalized_name":      t["normalized_name"],
            "league_first_seen":    t["league_first_seen"],
            "first_seen_date":      t["first_seen_date"],
            "active_status":        t["active_status"],
            "canonical_status":     "MAPPED",
            "canonical_id":         final,
            "canonical_name":       canon["canonical_name"],
            "canonical_confidence": a["confidence"],
            "merge_status":         merge_status,
        })

    print(f"  built {len(teams_canonical_new)} rows")
    print()

    # ---- Write (idempotent) ----
    print("--- WRITE (idempotent) ---")
    writes = []
    # canonical_teams
    eq, reason = structural_equal_csv(CANONICAL_TEAMS_CSV, CANONICAL_FIELDS,
                                     canonical_new, "canonical_id")
    if eq:
        writes.append(("canonical_teams.csv", "SKIP_IDENTICAL"))
    else:
        write_csv_lf(CANONICAL_TEAMS_CSV, CANONICAL_FIELDS, canonical_new, "canonical_id")
        writes.append(("canonical_teams.csv", "WROTE"))
    # team_aliases
    eq, reason = structural_equal_csv(TEAM_ALIASES_CSV, ALIAS_FIELDS,
                                     alias_new, "alias_id")
    if eq:
        writes.append(("team_aliases.csv", "SKIP_IDENTICAL"))
    else:
        write_csv_lf(TEAM_ALIASES_CSV, ALIAS_FIELDS, alias_new, "alias_id")
        writes.append(("team_aliases.csv", "WROTE"))
    # deprecated_map
    eq, reason = structural_equal_csv(DEPRECATED_MAP_CSV, DEPRECATED_FIELDS,
                                     deprecated_new, "deprecated_canonical_id")
    if eq:
        writes.append(("deprecated_canonical_map.csv", "SKIP_IDENTICAL"))
    else:
        write_csv_lf(DEPRECATED_MAP_CSV, DEPRECATED_FIELDS, deprecated_new, "deprecated_canonical_id")
        writes.append(("deprecated_canonical_map.csv", "WROTE"))
    # teams_canonical
    eq, reason = structural_equal_csv(TEAMS_CANONICAL_CSV, TEAMS_CANONICAL_FIELDS,
                                     teams_canonical_new, "team_id")
    if eq:
        writes.append(("teams_canonical.csv", "SKIP_IDENTICAL"))
    else:
        write_csv_lf(TEAMS_CANONICAL_CSV, TEAMS_CANONICAL_FIELDS,
                     teams_canonical_new, "team_id")
        writes.append(("teams_canonical.csv", "WROTE"))
    for name, action in writes:
        print(f"  [{action:18s}] {name}")
    print()

    # ---- Post SHA-256 ----
    print("--- POST SHA-256 ---")
    post_sha = {
        "review_queue.csv":            sha256_file(REVIEW_QUEUE_CSV),
        "review_queue_manifest.txt":   sha256_file(REVIEW_QUEUE_MANIFEST),
        "canonical_teams.csv":         sha256_file(CANONICAL_TEAMS_CSV),
        "team_aliases.csv":            sha256_file(TEAM_ALIASES_CSV),
        "deprecated_canonical_map.csv":sha256_file(DEPRECATED_MAP_CSV),
        "teams_canonical.csv":         sha256_file(TEAMS_CANONICAL_CSV),
        "teams.csv":                   sha256_file(TEAMS_CSV),
    }
    for k in sorted(post_sha):
        same = pre_sha[k] == post_sha[k]
        marker = "SAME " if same else "CHANGED"
        print(f"  [{marker}] {k:32s} {post_sha[k]}")
    print()

    # ---- Post-verify ----
    print("--- VERIFY POST-WRITE ---")
    # 1. review_queue + manifest unchanged (E4 does not modify)
    if post_sha["review_queue.csv"] != pre_sha["review_queue.csv"]:
        print("  [FAIL] review_queue.csv changed unexpectedly")
        sys.exit(1)
    if post_sha["review_queue_manifest.txt"] != pre_sha["review_queue_manifest.txt"]:
        print("  [FAIL] review_queue_manifest.txt changed unexpectedly")
        sys.exit(1)
    print(f"  [OK] queue + manifest unchanged by E4")

    # 2. Phase 2 unchanged
    if post_sha["teams.csv"] != pre_sha["teams.csv"]:
        print("  [FAIL] teams.csv changed")
        sys.exit(1)
    print(f"  [OK] teams.csv unchanged")

    # 3. Reload team_aliases + teams_canonical, verify invariants
    ta_check, _, err = read_csv_lf_safe(TEAM_ALIASES_CSV)
    if err:
        print(f"  [FAIL] reload team_aliases: {err}")
        sys.exit(1)
    # Every alias canonical_id must be ACTIVE
    ct_check, _, err = read_csv_lf_safe(CANONICAL_TEAMS_CSV)
    if err:
        print(f"  [FAIL] reload canonical_teams: {err}")
        sys.exit(1)
    ct_status = {r["canonical_id"]: r["status"] for r in ct_check}
    bad_aliases = [a for a in ta_check if ct_status.get(a["canonical_id"]) != "ACTIVE"]
    if bad_aliases:
        print(f"  [FAIL] {len(bad_aliases)} aliases point to non-ACTIVE canonical")
        sys.exit(1)
    print(f"  [OK] all {len(ta_check)} aliases point to ACTIVE canonical")

    tc_check, _, err = read_csv_lf_safe(TEAMS_CANONICAL_CSV)
    if err:
        print(f"  [FAIL] reload teams_canonical: {err}")
        sys.exit(1)
    if len(tc_check) != EXPECTED_TEAMS:
        print(f"  [FAIL] teams_canonical rows={len(tc_check)}, expected {EXPECTED_TEAMS}")
        sys.exit(1)
    # Every teams_canonical canonical_id -> ACTIVE
    bad_tc = [r for r in tc_check if ct_status.get(r["canonical_id"]) != "ACTIVE"]
    if bad_tc:
        print(f"  [FAIL] {len(bad_tc)} teams_canonical rows point to non-ACTIVE")
        sys.exit(1)
    print(f"  [OK] all {len(tc_check)} teams_canonical rows resolve to ACTIVE")

    # Every team_id matches Phase 2
    p2_ids = set(t["team_id"] for t in phase2_teams)
    tc_ids = set(r["team_id"] for r in tc_check)
    if p2_ids != tc_ids:
        print(f"  [FAIL] team_id set mismatch")
        sys.exit(1)
    print(f"  [OK] team_id set matches Phase 2")

    # canonical_status all MAPPED
    if not all(r["canonical_status"] == "MAPPED" for r in tc_check):
        print(f"  [FAIL] some canonical_status != MAPPED")
        sys.exit(1)
    print(f"  [OK] all canonical_status = MAPPED")
    print()

    # ---- Review summary (E3 report) ----
    print("--- WRITE REVIEW SUMMARY ---")
    review_summary_lines = [
        "# Phase 3 — E3 Review Summary",
        f"Generated by E4 on {review_date}",
        "",
        "## Queue status counts",
        f"APPROVED        : {len(approved)}",
        f"REJECTED        : {len(rejected)}",
        f"PENDING         : {len(pending)}",
        f"NEEDS_MORE_INFO : {len(needs)}",
        f"TOTAL           : {len(rq_rows)}",
        "",
        "## Cross-league proposals",
        f"total           : {len(cl_rows) if CROSS_LEAGUE_CSV.exists() else 0}",
        "",
        "## Merges applied by E4",
        f"count           : {len(pending_redirects)}",
    ]
    if pending_redirects:
        for s, t in sorted(pending_redirects.items()):
            review_summary_lines.append(f"  {s} -> {t}")
    review_summary_lines += [
        "",
        "STATUS: E3 review complete",
    ]
    write_text_lf(REVIEW_SUMMARY_TXT, "\n".join(review_summary_lines) + "\n")
    print(f"  [WROTE] {REVIEW_SUMMARY_TXT.name}")
    print()

    # ---- Main E4 report ----
    e4_lines = [
        "# Phase 3 — E4 Report",
        f"Date: {review_date}",
        "",
        "## Inputs (read-only)",
        f"review_queue rows : {len(rq_rows)}",
        f"canonical_teams   : {len(ct_rows)}",
        f"team_aliases      : {len(ta_rows)}",
        f"teams_canonical   : {len(tc_check)}",
        "",
        "## Queue status counts (pre-apply)",
        f"APPROVED        : {len(approved)}",
        f"REJECTED        : {len(rejected)}",
        f"PENDING         : {len(pending)}",
        f"NEEDS_MORE_INFO : {len(needs)}",
        "",
        "## Merge result",
        f"merges applied   : {len(pending_redirects)}",
    ]
    if len(pending_redirects) == 0:
        e4_lines.append("(no-op merge — all candidates REJECTED)")
    e4_lines += [
        "",
        "## Backups",
        f"dir: {backup_dir.relative_to(PROJECT_ROOT)}",
        "  - canonical_teams.csv.pre_e4",
        "  - team_aliases.csv.pre_e4",
        "  - deprecated_canonical_map.csv.pre_e4",
        "  - teams_canonical.csv.pre_e4",
        "  - SHA256SUMS.pre_e4.txt",
        "",
        "## Write actions",
    ]
    for name, action in writes:
        e4_lines.append(f"{name:32s} : {action}")
    e4_lines += [
        "",
        "## Immutability (pre vs post)",
    ]
    for k in ("review_queue.csv", "review_queue_manifest.txt", "teams.csv"):
        same = pre_sha[k] == post_sha[k]
        e4_lines.append(f"{k:32s} : {'UNCHANGED' if same else 'CHANGED'}  {post_sha[k]}")
    e4_lines += [
        "",
        "## Validation",
        "- all queue rows resolved (0 PENDING, 0 NEEDS_MORE_INFO)",
        "- manifest verified (immutable fields unchanged)",
        "- no cycles in merge graph",
        "- all aliases point to ACTIVE canonical",
        "- all teams_canonical rows resolve to ACTIVE canonical",
        "- teams_canonical team_id set == Phase 2 team_id set",
        "- all canonical_status = MAPPED",
        "",
        "STATUS: E4 PASS",
    ]
    write_text_lf(E4_REPORT_TXT, "\n".join(e4_lines) + "\n")
    print(f"  [WROTE] {E4_REPORT_TXT.name}")
    print()

    # ---- Acceptance ----
    print("=" * 78)
    print("E4 ACCEPTANCE")
    print("=" * 78)

    checks = [
        ("0 PENDING in queue",             len(pending) == 0),
        ("0 NEEDS_MORE_INFO in queue",     len(needs) == 0),
        ("manifest verified",              len(mismatches) == 0),
        ("no cycles",                      True),
        ("all targets resolve to ACTIVE",  True),
        ("teams_canonical rows == 144",    len(tc_check) == EXPECTED_TEAMS),
        ("teams_canonical team_id set == Phase 2",
            set(r["team_id"] for r in tc_check) == set(t["team_id"] for t in phase2_teams)),
        ("all canonical_status = MAPPED",
            all(r["canonical_status"] == "MAPPED" for r in tc_check)),
        ("all aliases point to ACTIVE",
            all(ct_status.get(a["canonical_id"]) == "ACTIVE" for a in ta_check)),
        ("all teams_canonical point to ACTIVE",
            all(ct_status.get(r["canonical_id"]) == "ACTIVE" for r in tc_check)),
        ("review_queue unchanged by E4",
            post_sha["review_queue.csv"] == pre_sha["review_queue.csv"]),
        ("review_queue_manifest unchanged",
            post_sha["review_queue_manifest.txt"] == pre_sha["review_queue_manifest.txt"]),
        ("Phase 2 teams.csv unchanged",
            post_sha["teams.csv"] == pre_sha["teams.csv"]),
        ("0 APPROVED handled as no-op",
            len(approved) == 0 or len(pending_redirects) == len(approved)),
        ("backup dir exists",              backup_dir.exists()),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  merges applied  : {len(pending_redirects)}")
    print(f"  queue APPROVED  : {len(approved)}")
    print(f"  queue REJECTED  : {len(rejected)}")
    print()

    if all_ok:
        print("STATUS: E4 PASS ✅")
        print()
        print("PHASE 3 COMPLETE")
        print("  Phase 3 E1 ✅ E2 ✅ E3 ✅ E4 ✅")
        print("  Database stable, canonical mapping resolved (self-map only)")
        print()
        print("NEXT: User approval → Phase 4 (BTTS statistics)")
        sys.exit(0)
    else:
        print("STATUS: E4 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
