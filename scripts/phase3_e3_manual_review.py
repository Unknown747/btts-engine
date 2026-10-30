#!/usr/bin/env python3
"""
Phase 3 — E3: Manual Review Application
========================================

Applies user-specified decisions to review_queue.csv.
STRICT: only touches user-editable fields (status, reviewer_notes,
reviewed_date, approved_target_canonical_id). Immutable fields NEVER changed.

Inputs (READ-ONLY, verified pre/post):
  data/mappings/review_queue.csv          (E2 output)
  data/mappings/review_queue_manifest.txt (E2 output)
  data/mappings/canonical_teams.csv       (E1 output)
  data/mappings/team_aliases.csv          (E1 output)
  data/processed/teams_canonical.csv      (E1 output)
  data/processed/teams.csv                (Phase 2)
  data/processed/leagues.csv              (Phase 2)
  data/processed/season_meta.csv          (Phase 2)
  data/processed/matches.csv              (Phase 2)

Writes (only):
  data/backups/phase3_E3_<ts>/review_queue.csv.pre_e3   (backup)
  data/backups/phase3_E3_<ts>/SHA256SUMS.pre_e3.txt     (backup manifest)
  data/mappings/review_queue.csv                        (edited)
  data/reports/phase3_e3_manual_review.txt              (report)

Rules enforced:
  - Immutable fields verified pre/post.
  - approved_target_canonical_id must remain empty for REJECTED.
  - reviewed_date = ISO date (from CLI arg or today).
  - Manifest hash per row MUST remain identical post-edit.
  - Backup created before edit.
"""
import sys
import csv
import io
import hashlib
import shutil
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
REPORTS_DIR   = DATA_DIR / "reports"
BACKUPS_DIR   = DATA_DIR / "backups"

REVIEW_QUEUE_CSV      = MAPPINGS_DIR / "review_queue.csv"
REVIEW_QUEUE_MANIFEST = MAPPINGS_DIR / "review_queue_manifest.txt"
CANONICAL_TEAMS_CSV   = MAPPINGS_DIR / "canonical_teams.csv"
TEAM_ALIASES_CSV      = MAPPINGS_DIR / "team_aliases.csv"
TEAMS_CANONICAL_CSV   = PROCESSED_DIR / "teams_canonical.csv"
TEAMS_CSV             = PROCESSED_DIR / "teams.csv"
LEAGUES_CSV           = PROCESSED_DIR / "leagues.csv"
SEASON_META_CSV       = PROCESSED_DIR / "season_meta.csv"
MATCHES_CSV           = PROCESSED_DIR / "matches.csv"

REPORT_TXT            = REPORTS_DIR / "phase3_e3_manual_review.txt"

REVIEW_QUEUE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
    "status", "approved_target_canonical_id",
    "reviewer_notes", "reviewed_date",
]

IMMUTABLE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
]

USER_FIELDS = [
    "status", "approved_target_canonical_id",
    "reviewer_notes", "reviewed_date",
]

# ---- User-specified decisions (from prompt, hardcoded here for traceability) ----
DECISION_STATUS = "REJECTED"
DECISION_NOTE   = ("Distinct football clubs. Name similarity does not "
                   "establish identical club identity.")
DECISION_TARGET = ""  # must be empty for REJECTED

# All 5 review_ids to set to REJECTED (from E2 output)
TARGET_REVIEW_IDS = {
    "md5:07140726fcb459f21b42a0a22c0b8f91",   # Ath Madrid <-> Real Madrid
    "md5:08fa5704f7a21a433a345574466434b4",   # Le Mans <-> Lens
    "md5:a2127e8035cff8145e507fe43d8719bf",   # Leganes <-> Levante
    "md5:e01abe46421bb2aec0193d9491ceb977",   # Heidenheim <-> Hoffenheim
    "md5:ef7bf0c77ba24e238ac8b41a37ede682",   # Paris FC <-> Paris SG
}

# ---- Helpers ----

def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _normalize_field(s: str) -> str:
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
    fields = [_normalize_field(f) for f in (reader.fieldnames or [])]
    rows = []
    for r in reader:
        rows.append({_normalize_field(k): _normalize_field(v) for k, v in r.items()})
    return rows, fields, None

def write_csv_lf(path: Path, fields: list, rows: list, pk: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows, key=lambda r: r[pk])
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted_rows)

def immutable_line(row: dict) -> str:
    return "|".join(str(row.get(f, "")) for f in IMMUTABLE_FIELDS)

def immutable_sha256(row: dict) -> str:
    return hashlib.sha256(immutable_line(row).encode("utf-8")).hexdigest()

def parse_manifest(text: str):
    """Return dict review_id -> sha256_hex from manifest data lines."""
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

# ---- Main ----

def main():
    print("=" * 78)
    print("PHASE 3 — E3: MANUAL REVIEW APPLICATION")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Queue file   : {REVIEW_QUEUE_CSV}")
    print(f"Report       : {REPORT_TXT}")
    print(f"Mode         : apply REJECTED to {len(TARGET_REVIEW_IDS)} review_ids")
    print()

    # ---- Pre-flight ----
    for p in (REVIEW_QUEUE_CSV, REVIEW_QUEUE_MANIFEST,
              CANONICAL_TEAMS_CSV, TEAM_ALIASES_CSV, TEAMS_CANONICAL_CSV,
              TEAMS_CSV, LEAGUES_CSV, SEASON_META_CSV, MATCHES_CSV):
        if not p.exists():
            print(f"[FAIL] missing: {p}")
            sys.exit(1)

    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Pre SHA-256 ----
    pre_sha = {
        "review_queue.csv":          sha256_file(REVIEW_QUEUE_CSV),
        "review_queue_manifest.txt": sha256_file(REVIEW_QUEUE_MANIFEST),
        "canonical_teams.csv":       sha256_file(CANONICAL_TEAMS_CSV),
        "team_aliases.csv":          sha256_file(TEAM_ALIASES_CSV),
        "teams_canonical.csv":       sha256_file(TEAMS_CANONICAL_CSV),
        "teams.csv":                 sha256_file(TEAMS_CSV),
        "leagues.csv":               sha256_file(LEAGUES_CSV),
        "season_meta.csv":           sha256_file(SEASON_META_CSV),
        "matches.csv":               sha256_file(MATCHES_CSV),
    }
    print("--- PRE SHA-256 ---")
    for k in sorted(pre_sha):
        print(f"  {k:30s} {pre_sha[k]}")
    print()

    # ---- Load queue ----
    print("--- LOAD QUEUE ---")
    rows, fields, err = read_csv_lf_safe(REVIEW_QUEUE_CSV)
    if err:
        print(f"[FAIL] queue read error: {err}")
        sys.exit(1)
    if fields != REVIEW_QUEUE_FIELDS:
        print(f"[FAIL] queue schema mismatch: {fields}")
        sys.exit(1)
    print(f"  loaded {len(rows)} rows")
    print()

    # ---- Sanity: all target ids present ----
    print("--- SANITY: TARGET REVIEW_IDS ---")
    present_ids = {r["review_id"] for r in rows}
    missing = TARGET_REVIEW_IDS - present_ids
    if missing:
        print(f"  [FAIL] review_ids missing in queue: {sorted(missing)}")
        sys.exit(1)
    print(f"  [OK] all {len(TARGET_REVIEW_IDS)} target review_ids present")
    print()

    # ---- Manifest pre-verification ----
    print("--- MANIFEST PRE-VERIFICATION ---")
    manifest_text = REVIEW_QUEUE_MANIFEST.read_text(encoding="utf-8")
    manifest_map = parse_manifest(manifest_text)
    print(f"  manifest rows: {len(manifest_map)}")
    if len(manifest_map) != len(rows):
        print(f"  [FAIL] manifest row count != queue row count")
        sys.exit(1)

    # Recompute immutable hashes and compare to manifest
    pre_hash_mismatches = []
    for r in rows:
        rid = r["review_id"]
        computed = immutable_sha256(r)
        from_manifest = manifest_map.get(rid, "")
        if computed != from_manifest:
            pre_hash_mismatches.append((rid, from_manifest, computed))
    if pre_hash_mismatches:
        print(f"  [FAIL] pre-edit manifest mismatches: {len(pre_hash_mismatches)}")
        for rid, fm, cm in pre_hash_mismatches[:5]:
            print(f"    {rid}: manifest={fm[:16]}... computed={cm[:16]}...")
        sys.exit(1)
    print(f"  [OK] all {len(rows)} immutable hashes match manifest")
    print()

    # Snapshot immutable values for post-check
    immutable_pre = {r["review_id"]: immutable_line(r) for r in rows}

    # ---- Validate target statuses (all pending) ----
    print("--- VALIDATE TARGETS PENDING ---")
    for rid in TARGET_REVIEW_IDS:
        row = next(r for r in rows if r["review_id"] == rid)
        if row["status"] != "PENDING":
            print(f"  [FAIL] {rid} already has status={row['status']}")
            sys.exit(1)
    print(f"  [OK] all targets still PENDING")
    print()

    # ---- Backup ----
    print("--- BACKUP ---")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUPS_DIR / f"phase3_E3_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(REVIEW_QUEUE_CSV, backup_dir / "review_queue.csv.pre_e3")
    shutil.copy2(REVIEW_QUEUE_MANIFEST, backup_dir / "review_queue_manifest.txt.pre_e3")
    with open(backup_dir / "SHA256SUMS.pre_e3.txt", "w", encoding="utf-8") as f:
        for k, v in sorted(pre_sha.items()):
            f.write(f"{v}  {k}\n")
    print(f"  backup dir: {backup_dir.relative_to(PROJECT_ROOT)}")
    print(f"    - review_queue.csv.pre_e3")
    print(f"    - review_queue_manifest.txt.pre_e3")
    print(f"    - SHA256SUMS.pre_e3.txt")
    print()

    # ---- Apply decisions ----
    print("--- APPLY DECISIONS ---")
    review_date = datetime.now().strftime("%Y-%m-%d")
    print(f"  reviewed_date = {review_date}")
    print(f"  decision      = {DECISION_STATUS}")
    print(f"  note          = {DECISION_NOTE!r}")
    print()

    changed_count = 0
    for r in rows:
        if r["review_id"] not in TARGET_REVIEW_IDS:
            continue
        # Verify immutable unchanged in-memory
        before = immutable_line(r)
        # Apply user fields ONLY
        r["status"] = DECISION_STATUS
        r["approved_target_canonical_id"] = DECISION_TARGET  # empty for REJECTED
        r["reviewer_notes"] = DECISION_NOTE
        r["reviewed_date"] = review_date
        after = immutable_line(r)
        if before != after:
            print(f"  [FAIL] immutable changed in-memory for {r['review_id']}")
            sys.exit(1)
        changed_count += 1
        print(f"  [EDIT] {r['review_id']}  {r['raw_name_a']} <-> {r['raw_name_b']}")
    print()

    if changed_count != len(TARGET_REVIEW_IDS):
        print(f"  [FAIL] changed {changed_count}, expected {len(TARGET_REVIEW_IDS)}")
        sys.exit(1)
    print(f"  [OK] {changed_count}/{len(TARGET_REVIEW_IDS)} rows updated")
    print()

    # ---- Post-edit sanity (before write) ----
    # Verify immutable fields identical to snapshot
    for r in rows:
        rid = r["review_id"]
        if immutable_line(r) != immutable_pre[rid]:
            print(f"  [FAIL] immutable drift for {rid}")
            sys.exit(1)
    print(f"  [OK] in-memory immutable integrity confirmed")
    print()

    # ---- Write queue (LF) ----
    print("--- WRITE QUEUE ---")
    write_csv_lf(REVIEW_QUEUE_CSV, REVIEW_QUEUE_FIELDS, rows, "review_id")
    print(f"  [WROTE] review_queue.csv")
    print()

    # ---- Manifest NOT rewritten (must be identical) ----
    print("--- MANIFEST POST-CHECK ---")
    post_manifest_sha = sha256_file(REVIEW_QUEUE_MANIFEST)
    if post_manifest_sha != pre_sha["review_queue_manifest.txt"]:
        print(f"  [FAIL] manifest file changed!")
        sys.exit(1)
    print(f"  [OK] manifest file unchanged (SHA-256 identical)")

    # Re-read queue from disk and verify per-row immutable hash matches manifest
    rows2, fields2, err = read_csv_lf_safe(REVIEW_QUEUE_CSV)
    if err:
        print(f"  [FAIL] post-write read error: {err}")
        sys.exit(1)
    if fields2 != REVIEW_QUEUE_FIELDS:
        print(f"  [FAIL] post-write schema mismatch")
        sys.exit(1)

    post_hash_mismatches = []
    for r in rows2:
        rid = r["review_id"]
        computed = immutable_sha256(r)
        from_manifest = manifest_map.get(rid, "")
        if computed != from_manifest:
            post_hash_mismatches.append((rid, from_manifest, computed))
    if post_hash_mismatches:
        print(f"  [FAIL] post-write manifest mismatches: {len(post_hash_mismatches)}")
        for rid, fm, cm in post_hash_mismatches[:5]:
            print(f"    {rid}: manifest={fm[:16]}... computed={cm[:16]}...")
        sys.exit(1)
    print(f"  [OK] all {len(rows2)} immutable hashes still match manifest after edit")
    print()

    # ---- Post SHA-256 ----
    print("--- POST SHA-256 ---")
    post_sha = {
        "review_queue.csv":          sha256_file(REVIEW_QUEUE_CSV),
        "review_queue_manifest.txt": sha256_file(REVIEW_QUEUE_MANIFEST),
        "canonical_teams.csv":       sha256_file(CANONICAL_TEAMS_CSV),
        "team_aliases.csv":          sha256_file(TEAM_ALIASES_CSV),
        "teams_canonical.csv":       sha256_file(TEAMS_CANONICAL_CSV),
        "teams.csv":                 sha256_file(TEAMS_CSV),
        "leagues.csv":               sha256_file(LEAGUES_CSV),
        "season_meta.csv":           sha256_file(SEASON_META_CSV),
        "matches.csv":               sha256_file(MATCHES_CSV),
    }
    for k in sorted(post_sha):
        same = pre_sha[k] == post_sha[k]
        marker = "SAME " if same else "CHANGED"
        print(f"  [{marker}] {k:30s} {post_sha[k]}")
    print()

    # ---- Invariants ----
    print("--- INVARIANTS ---")
    # 1. review_queue.csv MUST change (edited).
    if post_sha["review_queue.csv"] == pre_sha["review_queue.csv"]:
        print(f"  [FAIL] queue did not change despite edits")
        sys.exit(1)
    print(f"  [OK] queue changed (edited)")
    # 2. manifest MUST NOT change.
    if post_sha["review_queue_manifest.txt"] != pre_sha["review_queue_manifest.txt"]:
        print(f"  [FAIL] manifest changed unexpectedly")
        sys.exit(1)
    print(f"  [OK] manifest unchanged")
    # 3. All other files MUST NOT change.
    for k in ("canonical_teams.csv", "team_aliases.csv", "teams_canonical.csv",
              "teams.csv", "leagues.csv", "season_meta.csv", "matches.csv"):
        if post_sha[k] != pre_sha[k]:
            print(f"  [FAIL] {k} unexpectedly changed")
            sys.exit(1)
    print(f"  [OK] canonical_teams, team_aliases, teams_canonical unchanged")
    print(f"  [OK] Phase 2 files unchanged")
    print()

    # ---- Status counts ----
    print("--- STATUS COUNTS ---")
    counts = {"APPROVED": 0, "REJECTED": 0, "PENDING": 0, "NEEDS_MORE_INFO": 0}
    for r in rows2:
        if r["status"] in counts:
            counts[r["status"]] += 1
    for k, v in counts.items():
        print(f"  {k:16s} : {v}")
    print()

    # ---- Full queue content (for display) ----
    print("--- REVIEW QUEUE (FULL, post-edit) ---")
    for r in rows2:
        print(f"  review_id : {r['review_id']}")
        print(f"  pair      : {r['raw_name_a']!r} <-> {r['raw_name_b']!r}")
        print(f"  score     : {r['score']}  algo={r['algorithm']}")
        print(f"  status    : {r['status']}")
        print(f"  target    : {r['approved_target_canonical_id']!r}")
        print(f"  notes     : {r['reviewer_notes']!r}")
        print(f"  reviewed  : {r['reviewed_date']}")
        print()
    print()

    # ---- Report ----
    report_lines = [
        "# Phase 3 — E3 Manual Review Report",
        f"Date: {review_date}",
        f"Applied decisions: {len(TARGET_REVIEW_IDS)} rows -> REJECTED",
        "",
        "## Reviewer decision (all 5 rows)",
        f"status : REJECTED",
        f"note   : {DECISION_NOTE!r}",
        f"approved_target_canonical_id : empty (REJECTED)",
        f"reviewed_date : {review_date}",
        "",
        "## Queue status counts (post-edit)",
        f"APPROVED        : {counts['APPROVED']}",
        f"REJECTED        : {counts['REJECTED']}",
        f"PENDING         : {counts['PENDING']}",
        f"NEEDS_MORE_INFO : {counts['NEEDS_MORE_INFO']}",
        "",
        "## Backups",
        f"dir: {backup_dir.relative_to(PROJECT_ROOT)}",
        f"  - review_queue.csv.pre_e3",
        f"  - review_queue_manifest.txt.pre_e3",
        f"  - SHA256SUMS.pre_e3.txt",
        "",
        "## Manifest verification",
        f"Pre-edit: all {len(rows)} immutable hashes match manifest",
        f"Post-edit: all {len(rows2)} immutable hashes still match manifest",
        f"Manifest file SHA-256 unchanged: {post_sha['review_queue_manifest.txt']}",
        "",
        "## Immutable file SHA-256 (must be unchanged)",
    ]
    for k in ("canonical_teams.csv", "team_aliases.csv", "teams_canonical.csv",
              "teams.csv", "leagues.csv", "season_meta.csv", "matches.csv"):
        same = pre_sha[k] == post_sha[k]
        report_lines.append(f"{k:24s} : {'UNCHANGED' if same else 'CHANGED'}  {pre_sha[k]}")
    report_lines += [
        "",
        "## Mutable file SHA-256 (expected to change / not change)",
        f"review_queue.csv          : pre={pre_sha['review_queue.csv']}",
        f"                            post={post_sha['review_queue.csv']}  (CHANGED — expected)",
        f"review_queue_manifest.txt : {post_sha['review_queue_manifest.txt']}  (UNCHANGED — expected)",
        "",
        "STATUS: E3 PASS",
    ]
    REPORT_TXT.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"  [WROTE] {REPORT_TXT.name}")
    print()

    # ---- Acceptance ----
    print("=" * 78)
    print("E3 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("5 target review_ids found",             len(TARGET_REVIEW_IDS) == 5),
        ("5 rows edited",                         changed_count == 5),
        ("all targets now REJECTED",
            all(r["status"] == "REJECTED" for r in rows2 if r["review_id"] in TARGET_REVIEW_IDS)),
        ("no targets have approved_target",
            all(r["approved_target_canonical_id"] == ""
                for r in rows2 if r["review_id"] in TARGET_REVIEW_IDS)),
        ("all targets have reviewer_notes",
            all(r["reviewer_notes"] for r in rows2 if r["review_id"] in TARGET_REVIEW_IDS)),
        ("all targets have reviewed_date",
            all(r["reviewed_date"] for r in rows2 if r["review_id"] in TARGET_REVIEW_IDS)),
        ("immutable fields unchanged (pre vs post manifest verify)",
            len(post_hash_mismatches) == 0),
        ("manifest file SHA-256 unchanged",
            pre_sha["review_queue_manifest.txt"] == post_sha["review_queue_manifest.txt"]),
        ("queue file SHA-256 changed (edit applied)",
            pre_sha["review_queue.csv"] != post_sha["review_queue.csv"]),
        ("canonical_teams.csv unchanged",
            pre_sha["canonical_teams.csv"] == post_sha["canonical_teams.csv"]),
        ("team_aliases.csv unchanged",
            pre_sha["team_aliases.csv"] == post_sha["team_aliases.csv"]),
        ("teams_canonical.csv unchanged",
            pre_sha["teams_canonical.csv"] == post_sha["teams_canonical.csv"]),
        ("Phase 2 files unchanged",
            all(pre_sha[k] == post_sha[k] for k in
                ("teams.csv", "leagues.csv", "season_meta.csv", "matches.csv"))),
        ("backup dir exists",                     backup_dir.exists()),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  status counts: APPROVED={counts['APPROVED']} "
          f"REJECTED={counts['REJECTED']} "
          f"PENDING={counts['PENDING']} "
          f"NEEDS_MORE_INFO={counts['NEEDS_MORE_INFO']}")
    print()
    if all_ok:
        print("STATUS: E3 PASS ✅")
        print()
        print("NEXT: User approval → E4 (apply approved merges)")
        print("      NOTE: 0 APPROVED rows. E4 will be a no-op merge step.")
        sys.exit(0)
    else:
        print("STATUS: E3 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
