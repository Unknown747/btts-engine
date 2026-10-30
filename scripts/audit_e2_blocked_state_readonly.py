#!/usr/bin/env python3
"""
Read-only audit of Phase 3 state, intended to inspect E2 BLOCKED_DIFFERENT
context WITHOUT modifying any file.

STRICT READ-ONLY GUARANTEE:
  - Does NOT delete any file
  - Does NOT rename any file
  - Does NOT overwrite any existing file
  - Does NOT execute E2 logic
  - Does NOT write to any file in data/processed, data/mappings, data/raw,
    data/raw/snapshots, data/backups
  - Writes ONLY ONE output: data/reports/audit_e2_state_<ts>.txt
    (timestamped; will not overwrite a previous audit)

Scope:
  1) Inspect existing review_queue.csv (structure, rows, status, notes)
  2) Inspect existing review_queue_manifest.txt (hash format, count,
     per-row hash verification against queue)
  3) Compare current queue against freshly-computed E2 candidates IN-MEMORY
     (does NOT write; only computes and reports diff)
  4) SHA-256 pre/post for E1 outputs and Phase 2 files
  5) Write audit report (single file)
"""
import sys
import csv
import hashlib
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
REPORTS_DIR   = DATA_DIR / "reports"

# Files to audit
REVIEW_QUEUE_CSV      = MAPPINGS_DIR / "review_queue.csv"
REVIEW_QUEUE_MANIFEST = MAPPINGS_DIR / "review_queue_manifest.txt"

# Files to hash pre/post (must not change during audit)
HASH_FILES = {
    # E1 outputs
    "canonical_teams.csv":         MAPPINGS_DIR / "canonical_teams.csv",
    "team_aliases.csv":            MAPPINGS_DIR / "team_aliases.csv",
    "cross_league_proposals.csv":  MAPPINGS_DIR / "cross_league_proposals.csv",
    "deprecated_canonical_map.csv":MAPPINGS_DIR / "deprecated_canonical_map.csv",
    "teams_canonical.csv":         PROCESSED_DIR / "teams_canonical.csv",
    # Phase 2 outputs
    "leagues.csv":                 PROCESSED_DIR / "leagues.csv",
    "season_meta.csv":             PROCESSED_DIR / "season_meta.csv",
    "teams.csv":                   PROCESSED_DIR / "teams.csv",
    "matches.csv":                 PROCESSED_DIR / "matches.csv",
    # E2 audit targets (also pre/post)
    "review_queue.csv":            REVIEW_QUEUE_CSV,
    "review_queue_manifest.txt":   REVIEW_QUEUE_MANIFEST,
}

IMMUTABLE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
]

REVIEW_QUEUE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
    "status", "approved_target_canonical_id",
    "reviewer_notes", "reviewed_date",
]

ALGO_SEQUENCE   = "difflib.SequenceMatcher"
ALGO_TOKEN_SORT = "difflib.TokenSortRatio"
ALGO_TOKEN_SET  = "difflib.TokenSetRatio"
ALGO_ORDER = [ALGO_SEQUENCE, ALGO_TOKEN_SORT, ALGO_TOKEN_SET]

THRESHOLD = 0.70

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def read_csv_robust(path: Path):
    if not path.exists():
        return None, None, "missing"
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                rows = [dict(r) for r in reader]
                fields = reader.fieldnames or []
            return rows, fields, None
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return None, None, str(e)[:200]
    return None, None, "decode_failed"

def norm(s: str) -> str:
    if s is None:
        return ""
    return " ".join(str(s).strip().lower().split())

def seq_ratio(a, b):
    return SequenceMatcher(None, a, b).ratio()

def token_sort_ratio(a, b):
    return SequenceMatcher(None, " ".join(sorted(a.split())),
                          " ".join(sorted(b.split()))).ratio()

def token_set_ratio(a, b):
    seta, setb = set(a.split()), set(b.split())
    if not seta and not setb:
        return 1.0
    if not seta or not setb:
        return 0.0
    inter = seta & setb
    base = " ".join(sorted(inter))
    diff_a = " ".join(sorted(seta - setb))
    diff_b = " ".join(sorted(setb - seta))
    sa = (base + " " + diff_a).strip()
    sb = (base + " " + diff_b).strip()
    return SequenceMatcher(None, sa, sb).ratio()

def pick_winner_algo(a_norm, b_norm):
    scores = {
        ALGO_SEQUENCE:   seq_ratio(a_norm, b_norm),
        ALGO_TOKEN_SORT: token_sort_ratio(a_norm, b_norm),
        ALGO_TOKEN_SET:  token_set_ratio(a_norm, b_norm),
    }
    best_algo, best_score = None, -1.0
    for algo in ALGO_ORDER:
        s = scores[algo]
        if s > best_score + 1e-12:
            best_score, best_algo = s, algo
    return best_score, best_algo

def immutable_line(row: dict) -> str:
    return "|".join(str(row.get(f, "")) for f in IMMUTABLE_FIELDS)

def immutable_sha256(row: dict) -> str:
    return hashlib.sha256(immutable_line(row).encode("utf-8")).hexdigest()

# ---------- E2 in-memory candidate computation (NO WRITE) ----------

def compute_e2_candidates_readonly():
    """
    Recompute what E2 WOULD produce. Purely in memory.
    Returns (candidates, diagnostics).
    """
    teams_path = PROCESSED_DIR / "teams.csv"
    leagues_path = PROCESSED_DIR / "leagues.csv"
    canon_path = MAPPINGS_DIR / "canonical_teams.csv"
    alias_path = MAPPINGS_DIR / "team_aliases.csv"

    diag = {"notes": []}
    for p in (teams_path, leagues_path, canon_path, alias_path):
        if not p.exists():
            diag["notes"].append(f"missing: {p.name}")
            return [], diag

    teams_rows, _, err = read_csv_robust(teams_path)
    leagues_rows, _, err = read_csv_robust(leagues_path)
    canon_rows, _, err = read_csv_robust(canon_path)
    alias_rows, _, err = read_csv_robust(alias_path)
    if err:
        diag["notes"].append(f"read error: {err}")
        return [], diag

    league_to_country = {r["league_code"]: r["country"] for r in leagues_rows}
    alias_by_raw = {}
    for a in alias_rows:
        if a.get("source") == "football-data.co.uk":
            alias_by_raw[a["original_name"]] = a["canonical_id"]

    by_raw = {t["raw_name"]: t for t in teams_rows}
    raws = sorted(by_raw.keys())
    canon_ids = set(c["canonical_id"] for c in canon_rows)

    candidates = []
    for i in range(len(raws)):
        for j in range(i + 1, len(raws)):
            a_raw, b_raw = raws[i], raws[j]
            a, b = by_raw[a_raw], by_raw[b_raw]
            if a["league_first_seen"] != b["league_first_seen"]:
                continue
            a_c = league_to_country.get(a["league_first_seen"], "")
            b_c = league_to_country.get(b["league_first_seen"], "")
            if not a_c or not b_c or a_c != b_c:
                continue
            a_norm = a["normalized_name"]
            b_norm = b["normalized_name"]
            if not a_norm or not b_norm:
                continue
            same_first = (a_norm[0] == b_norm[0])
            len_diff = abs(len(a_norm) - len(b_norm))
            if (not same_first) and len_diff > 3:
                continue
            score, algo = pick_winner_algo(a_norm, b_norm)
            if score < THRESHOLD:
                continue
            cid_a = alias_by_raw.get(a_raw)
            cid_b = alias_by_raw.get(b_raw)
            if cid_a not in canon_ids or cid_b not in canon_ids:
                diag["notes"].append(f"fk issue for {a_raw}/{b_raw}")
                continue
            x, y = sorted([a_raw, b_raw])
            rid = "md5:" + hashlib.md5(f"{x}|{y}".encode("utf-8")).hexdigest()
            candidates.append({
                "review_id": rid,
                "raw_name_a": x,
                "raw_name_b": y,
                "canonical_a_id": alias_by_raw[x],
                "canonical_b_id": alias_by_raw[y],
                "algorithm": algo,
                "score": f"{score:.4f}",
                "suggested_merge": "YES",
                "status": "PENDING",
                "approved_target_canonical_id": "",
                "reviewer_notes": "",
                "reviewed_date": "",
            })

    candidates.sort(key=lambda r: r["review_id"])
    diag["count"] = len(candidates)
    return candidates, diag

# ---------- Audit ----------

def audit_queue(rows, fields, err):
    out = []
    if err:
        out.append(f"[ERROR] read error: {err}")
        return out
    if rows is None:
        out.append("[MISSING] review_queue.csv not found")
        return out
    out.append(f"file: data/mappings/review_queue.csv")
    out.append(f"row count (excl header): {len(rows)}")
    out.append(f"fields ({len(fields)}): {fields}")
    out.append("")
    if not rows:
        out.append("(queue is empty — header only)")
        return out
    out.append("Per-row detail:")
    for idx, r in enumerate(rows, 1):
        out.append(f"--- row {idx} ---")
        for k, v in r.items():
            out.append(f"  {k}: {v!r}")
        # Immutable hash
        out.append(f"  immutable_sha256: {immutable_sha256(r)}")
    return out

def audit_manifest(queue_rows):
    out = []
    if not REVIEW_QUEUE_MANIFEST.exists():
        out.append("[MISSING] review_queue_manifest.txt not found")
        return out
    text = REVIEW_QUEUE_MANIFEST.read_text(encoding="utf-8")
    lines = text.splitlines()
    out.append(f"file: data/mappings/review_queue_manifest.txt")
    out.append(f"total lines: {len(lines)}")
    comment_lines = [l for l in lines if l.startswith("#")]
    data_lines = [l for l in lines if l.strip() and not l.startswith("#")]
    out.append(f"comment lines: {len(comment_lines)}")
    out.append(f"data lines (hashes): {len(data_lines)}")
    out.append("")
    out.append("Header comments:")
    for c in comment_lines:
        out.append(f"  {c}")
    out.append("")
    if not data_lines:
        out.append("(manifest data section is empty — no hash rows)")
        return out
    out.append("Hash lines analysis:")
    parsed = []
    fmt_ok = True
    for ln in data_lines:
        parts = ln.split("  ", 1)
        if len(parts) != 2:
            fmt_ok = False
            out.append(f"  [MALFORMED] {ln!r}")
            continue
        h, rid = parts[0].strip(), parts[1].strip()
        ok_hex = (len(h) == 64 and all(ch in "0123456789abcdef" for ch in h))
        ok_rid = (rid.startswith("md5:") and len(rid) == 4 + 32)
        out.append(f"  hash={h}  review_id={rid}  sha256_format={'OK' if ok_hex else 'BAD'}  review_id_format={'OK' if ok_rid else 'BAD'}")
        parsed.append((h, rid))
        if not ok_hex or not ok_rid:
            fmt_ok = False
    out.append(f"format valid: {fmt_ok}")
    out.append("")
    out.append("Cross-check vs current queue:")
    queue_by_id = {r["review_id"]: r for r in (queue_rows or [])}
    for h, rid in parsed:
        if rid not in queue_by_id:
            out.append(f"  [MISSING_IN_QUEUE] {rid}")
            continue
        expected = immutable_sha256(queue_by_id[rid])
        if expected == h:
            out.append(f"  [MATCH] {rid}")
        else:
            out.append(f"  [MISMATCH] {rid}")
            out.append(f"      manifest_hash : {h}")
            out.append(f"      expected_hash : {expected}")
    # Also: any queue row NOT in manifest?
    manifest_ids = set(rid for _, rid in parsed)
    for rid in queue_by_id:
        if rid not in manifest_ids:
            out.append(f"  [QUEUE_ROW_NOT_IN_MANIFEST] {rid}")
    return out

def audit_diff(queue_rows, computed_candidates):
    out = []
    out.append(f"queue rows (existing)          : {len(queue_rows or [])}")
    out.append(f"computed E2 candidates (in-mem): {len(computed_candidates)}")
    out.append("")
    if not queue_rows and not computed_candidates:
        out.append("Both empty. No diff.")
        return out
    q_by_id = {r["review_id"]: r for r in (queue_rows or [])}
    c_by_id = {r["review_id"]: r for r in (computed_candidates or [])}
    all_ids = sorted(set(q_by_id) | set(c_by_id))
    out.append(f"union review_ids: {len(all_ids)}")
    for rid in all_ids:
        in_q = rid in q_by_id
        in_c = rid in c_by_id
        if in_q and not in_c:
            out.append(f"[ONLY_IN_QUEUE] {rid}")
            out.append(f"  row: {q_by_id[rid]}")
        elif in_c and not in_q:
            out.append(f"[ONLY_IN_COMPUTED] {rid}")
            out.append(f"  row: {c_by_id[rid]}")
        else:
            q = q_by_id[rid]
            c = c_by_id[rid]
            diffs = []
            for f in REVIEW_QUEUE_FIELDS:
                qv = str(q.get(f, ""))
                cv = str(c.get(f, ""))
                if qv != cv:
                    diffs.append((f, qv, cv))
            if diffs:
                out.append(f"[DIFF] {rid}")
                for f, qv, cv in diffs:
                    out.append(f"  field {f}: queue={qv!r} computed={cv!r}")
            else:
                out.append(f"[IDENTICAL] {rid}")
    return out

# ---------- Main ----------

def main():
    print("=" * 78)
    print("READ-ONLY AUDIT — E2 BLOCKED_DIFFERENT STATE")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print("Mode         : READ-ONLY (no file writes except audit report)")
    print()

    # ---- Pre SHA-256 ----
    print("--- PRE SHA-256 (targets for read-only verification) ---")
    pre_sha = {name: sha256_file(p) for name, p in HASH_FILES.items()}
    for name in sorted(pre_sha):
        print(f"  {name:30s} {pre_sha[name]}")
    print()

    # ---- Section 1: audit existing queue ----
    q_rows, q_fields, q_err = read_csv_robust(REVIEW_QUEUE_CSV)
    section1 = audit_queue(q_rows, q_fields, q_err)

    # ---- Section 2: audit manifest ----
    section2 = audit_manifest(q_rows or [])

    # ---- Section 3: E2 in-memory candidates + diff ----
    computed, diag = compute_e2_candidates_readonly()
    section3 = []
    section3.append("E2 candidate recomputation (IN MEMORY, not written)")
    section3.append(f"  raw_names considered : 144")
    section3.append(f"  threshold            : {THRESHOLD}")
    section3.append(f"  algorithms computed  : {ALGO_ORDER}")
    section3.append(f"  candidates found     : {len(computed)}")
    if diag["notes"]:
        for n in diag["notes"]:
            section3.append(f"  note: {n}")
    section3.append("")
    if computed:
        section3.append("Candidate details (in-memory, NOT written to disk):")
        for c in computed:
            section3.append(f"  {c['review_id']}  "
                            f"{c['raw_name_a']!r} <-> {c['raw_name_b']!r}  "
                            f"score={c['score']}  algo={c['algorithm']}")
    else:
        section3.append("(0 candidates above threshold)")
    section3.append("")
    section3.extend(audit_diff(q_rows or [], computed))

    # ---- Post SHA-256 ----
    print("--- POST SHA-256 ---")
    post_sha = {name: sha256_file(p) for name, p in HASH_FILES.items()}
    for name in sorted(post_sha):
        same = pre_sha[name] == post_sha[name]
        print(f"  [{'OK' if same else 'CHANGED'}] {name:30s} {post_sha[name]}")

    # ---- Write audit report (single file, timestamped) ----
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_path = REPORTS_DIR / f"audit_e2_state_{ts}.txt"

    lines = []
    lines.append("# Phase 3 — E2 BLOCKED_DIFFERENT Audit (READ-ONLY)")
    lines.append(f"Timestamp: {ts}")
    lines.append("Mode: read-only; no modifications to any audited file")
    lines.append("")
    lines.append("## Context")
    lines.append("- Script E2 (phase3_e2_fuzzy_candidates.py) has NOT been executed.")
    lines.append("- review_queue.csv currently on disk is the E1 baseline (empty).")
    lines.append("- review_queue_manifest.txt currently on disk is the E1 baseline (header only).")
    lines.append("- This audit recomputes E2 candidates IN-MEMORY ONLY.")
    lines.append("")
    lines.append("## Section 1 — review_queue.csv inspection")
    lines.extend(section1)
    lines.append("")
    lines.append("## Section 2 — review_queue_manifest.txt inspection")
    lines.extend(section2)
    lines.append("")
    lines.append("## Section 3 — Existing queue vs in-memory E2 candidates")
    lines.extend(section3)
    lines.append("")
    lines.append("## Section 4 — SHA-256 pre/post (all targets)")
    lines.append("| file | pre | post | unchanged |")
    lines.append("|---|---|---|---|")
    for name in sorted(pre_sha):
        same = pre_sha[name] == post_sha[name]
        lines.append(f"| {name} | {pre_sha[name]} | {post_sha[name]} | {'YES' if same else 'NO'} |")
    lines.append("")
    lines.append("## Summary")
    changed = [n for n in pre_sha if pre_sha[n] != post_sha[n]]
    lines.append(f"Files changed during audit: {len(changed)}")
    if changed:
        for n in changed:
            lines.append(f"  - {n}")
    lines.append(f"Audit report written to: data/reports/{audit_path.name}")
    lines.append("")
    lines.append("STATUS: READ-ONLY AUDIT COMPLETE")

    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(f"[OK] audit report: {audit_path.relative_to(PROJECT_ROOT)}")
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print(f"  queue rows (existing)         : {len(q_rows or [])}")
    print(f"  manifest hash rows            : {sum(1 for l in (REVIEW_QUEUE_MANIFEST.read_text(encoding='utf-8').splitlines() if REVIEW_QUEUE_MANIFEST.exists() else []) if l.strip() and not l.startswith('#'))}")
    print(f"  computed E2 candidates        : {len(computed)}")
    print(f"  files changed during audit    : {len(changed)}")
    print()
    print("STATUS: READ-ONLY AUDIT COMPLETE ✅")
    print()
    print("STOP. Menunggu approval.")
    sys.exit(0)

if __name__ == "__main__":
    main()
