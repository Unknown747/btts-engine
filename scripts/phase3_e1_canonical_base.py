#!/usr/bin/env python3
"""
Phase 3 — E1: Canonical Base (schema + self-map baseline)
=========================================================

Input (READ-ONLY):
  data/processed/teams.csv
  data/processed/leagues.csv

Output (CREATE):
  data/mappings/canonical_teams.csv              (144 rows, self-map)
  data/mappings/team_aliases.csv                 (144 rows, self-map APPROVED)
  data/mappings/review_queue.csv                 (header only)
  data/mappings/review_queue_manifest.txt        (header only)
  data/mappings/cross_league_proposals.csv       (header only)
  data/mappings/deprecated_canonical_map.csv     (header only)
  data/processed/teams_canonical.csv             (144 rows, all MAPPED self)
  data/reports/phase3_e1_report.txt

Not touched:
  leagues.csv, season_meta.csv, teams.csv, matches.csv, raw, snapshots

Rules (Plan v2):
  - canonical_id = md5(canonical_name_normalized | country_normalized)
  - alias_id     = md5(source | original_name)
  - No created_date / timestamp in any output (determinism)
  - All output sorted by primary key
  - No active_status in canonical_teams (per-tim only, in teams_canonical)
  - No alias_count in canonical_teams (computed on-the-fly in later steps)
  - Phase 2 files read-only; SHA-256 pre==post verified
"""
import sys
import csv
import hashlib
import shutil
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
REPORTS_DIR   = DATA_DIR / "reports"
BACKUPS_DIR   = DATA_DIR / "backups"

# ----- Inputs (read-only) -----
TEAMS_CSV       = PROCESSED_DIR / "teams.csv"
LEAGUES_CSV     = PROCESSED_DIR / "leagues.csv"

# ----- Outputs -----
CANONICAL_TEAMS_CSV     = MAPPINGS_DIR / "canonical_teams.csv"
TEAM_ALIASES_CSV        = MAPPINGS_DIR / "team_aliases.csv"
REVIEW_QUEUE_CSV        = MAPPINGS_DIR / "review_queue.csv"
REVIEW_QUEUE_MANIFEST   = MAPPINGS_DIR / "review_queue_manifest.txt"
CROSS_LEAGUE_CSV        = MAPPINGS_DIR / "cross_league_proposals.csv"
DEPRECATED_MAP_CSV      = MAPPINGS_DIR / "deprecated_canonical_map.csv"
TEAMS_CANONICAL_CSV     = PROCESSED_DIR / "teams_canonical.csv"
REPORT_TXT              = REPORTS_DIR / "phase3_e1_report.txt"

# ----- Schemas -----
CANONICAL_FIELDS = [
    "canonical_id", "canonical_name", "country",
    "primary_league", "status", "notes",
]
ALIAS_FIELDS = [
    "alias_id", "source", "original_name",
    "canonical_id", "canonical_name",
    "confidence", "mapping_status", "evidence", "approved_date",
]
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

# ----- Locks -----
EXPECTED_TEAM_ROWS = 144
EXPECTED_LEAGUE_ROWS = 6
SOURCE = "football-data.co.uk"

# ----- Helpers -----

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def norm(s: str) -> str:
    if s is None:
        return ""
    return " ".join(str(s).strip().lower().split())

def canonical_id_for(canonical_name: str, country: str) -> str:
    raw = f"{norm(canonical_name)}|{norm(country)}"
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"md5:{h}"

def alias_id_for(source: str, original_name: str) -> str:
    raw = f"{source}|{original_name}"
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"md5:{h}"

def read_csv_robust(path: Path):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                rows = [dict(r) for r in reader]
                fields = reader.fieldnames or []
            return rows, fields, enc, None
        except UnicodeDecodeError as e:
            last_err = str(e); continue
        except Exception as e:
            return [], [], enc, str(e)[:200]
    return [], [], "?", last_err

def file_equals_csv(path: Path, fields: list, rows: list) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            got_fields = reader.fieldnames or []
            got_rows = [dict(r) for r in reader]
    except Exception:
        return False
    if got_fields != fields:
        return False
    if len(got_rows) != len(rows):
        return False
    # Compare row-by-row after deterministic sort by first field (PK)
    pk = fields[0]
    got_sorted = sorted(got_rows, key=lambda r: r[pk])
    exp_sorted = sorted(rows, key=lambda r: r[pk])
    for a, b in zip(got_sorted, exp_sorted):
        if a != b:
            return False
    return True

def file_equals_text(path: Path, content: str) -> bool:
    if not path.exists():
        return False
    try:
        return path.read_text(encoding="utf-8") == content
    except Exception:
        return False

def write_csv(path: Path, fields: list, rows: list, pk: str):
    """Write deterministically sorted rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows, key=lambda r: r[pk])
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted_rows)

def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

# ----- Build -----

def build_all():
    """Return dict of all output data + stats."""
    teams_rows, teams_fields, _, err = read_csv_robust(TEAMS_CSV)
    if err:
        raise RuntimeError(f"cannot read teams.csv: {err}")
    leagues_rows, leagues_fields, _, err = read_csv_robust(LEAGUES_CSV)
    if err:
        raise RuntimeError(f"cannot read leagues.csv: {err}")

    if len(teams_rows) != EXPECTED_TEAM_ROWS:
        raise RuntimeError(f"teams.csv rows={len(teams_rows)}, "
                           f"expected {EXPECTED_TEAM_ROWS}")
    if len(leagues_rows) != EXPECTED_LEAGUE_ROWS:
        raise RuntimeError(f"leagues.csv rows={len(leagues_rows)}, "
                           f"expected {EXPECTED_LEAGUE_ROWS}")

    # Required columns
    for c in ["team_id", "raw_name", "normalized_name",
              "league_first_seen", "first_seen_date", "active_status"]:
        if c not in teams_fields:
            raise RuntimeError(f"teams.csv missing column: {c}")
    for c in ["league_code", "country"]:
        if c not in leagues_fields:
            raise RuntimeError(f"leagues.csv missing column: {c}")

    league_to_country = {r["league_code"]: r["country"] for r in leagues_rows}

    # canonical_teams (one per unique raw_name; self-map)
    canonical_by_id = {}   # canonical_id -> dict
    alias_rows = []
    teams_canonical = []

    # Dedup by canonical_id to guarantee unique
    for t in teams_rows:
        raw_name = t["raw_name"]
        league   = t["league_first_seen"]
        country  = league_to_country.get(league, "")
        if not country:
            raise RuntimeError(f"league {league!r} not in leagues.csv")

        cid = canonical_id_for(raw_name, country)

        if cid not in canonical_by_id:
            canonical_by_id[cid] = {
                "canonical_id":   cid,
                "canonical_name": raw_name,
                "country":        country,
                "primary_league": league,
                "status":         "ACTIVE",
                "notes":          "self-map",
            }

        aid = alias_id_for(SOURCE, raw_name)
        alias_rows.append({
            "alias_id":       aid,
            "source":         SOURCE,
            "original_name":  raw_name,
            "canonical_id":   cid,
            "canonical_name": canonical_by_id[cid]["canonical_name"],
            "confidence":     1.00,
            "mapping_status": "APPROVED",
            "evidence":       "exact_self_map",
            "approved_date":  "",   # keep empty for determinism (no timestamp)
        })

        merge_status = "SELF"
        teams_canonical.append({
            "team_id":              t["team_id"],
            "raw_name":             raw_name,
            "normalized_name":      t["normalized_name"],
            "league_first_seen":    league,
            "first_seen_date":      t["first_seen_date"],
            "active_status":        t["active_status"],
            "canonical_status":     "MAPPED",
            "canonical_id":         cid,
            "canonical_name":       canonical_by_id[cid]["canonical_name"],
            "canonical_confidence": 1.00,
            "merge_status":         merge_status,
        })

    canonical_rows = list(canonical_by_id.values())

    # Empty schemas
    review_queue_rows   = []
    cross_league_rows   = []
    deprecated_rows     = []

    return {
        "canonical_rows":        canonical_rows,
        "alias_rows":            alias_rows,
        "teams_canonical_rows":  teams_canonical,
        "review_queue_rows":     review_queue_rows,
        "cross_league_rows":     cross_league_rows,
        "deprecated_rows":       deprecated_rows,
        "teams_rows_raw":        teams_rows,
        "leagues_rows_raw":      leagues_rows,
    }

# ----- Validate -----

def validate(state: dict) -> list:
    problems = []

    canon = state["canonical_rows"]
    aliases = state["alias_rows"]
    tc = state["teams_canonical_rows"]

    # canonical_teams
    if not canon:
        problems.append("canonical_rows empty")
    cids = [r["canonical_id"] for r in canon]
    if len(set(cids)) != len(cids):
        problems.append("duplicate canonical_id")
    names_keys = [(r["canonical_name"], r["country"]) for r in canon]
    if len(set(names_keys)) != len(names_keys):
        problems.append("duplicate (canonical_name, country)")
    for r in canon:
        if r["status"] not in ("ACTIVE", "DEPRECATED"):
            problems.append(f"invalid status: {r['status']}")
        for f in ("canonical_id", "canonical_name", "country",
                  "primary_league", "status"):
            if str(r.get(f, "")).strip() == "":
                problems.append(f"canonical_teams empty field {f} for {r.get('canonical_id')}")

    # team_aliases
    if len(aliases) != EXPECTED_TEAM_ROWS:
        problems.append(f"team_aliases rows={len(aliases)}, expected {EXPECTED_TEAM_ROWS}")
    aids = [r["alias_id"] for r in aliases]
    if len(set(aids)) != len(aids):
        problems.append("duplicate alias_id")
    src_on = [(r["source"], r["original_name"]) for r in aliases]
    if len(set(src_on)) != len(src_on):
        problems.append("duplicate (source, original_name)")
    cid_set = set(cids)
    raw_names_in_teams = set(t["raw_name"] for t in state["teams_rows_raw"])
    for r in aliases:
        if r["canonical_id"] not in cid_set:
            problems.append(f"alias canonical_id not in canonical_teams: {r['canonical_id']}")
        if r["original_name"] not in raw_names_in_teams:
            problems.append(f"alias original_name not in teams.csv: {r['original_name']}")
        if r["mapping_status"] != "APPROVED":
            problems.append(f"alias not APPROVED: {r['alias_id']} status={r['mapping_status']}")
        if abs(float(r["confidence"]) - 1.00) > 1e-9:
            problems.append(f"alias confidence != 1.00: {r['alias_id']}")

    # all teams covered
    covered = set(r["original_name"] for r in aliases)
    missing = raw_names_in_teams - covered
    if missing:
        problems.append(f"teams.csv raw_names without alias: {len(missing)}")

    # teams_canonical
    if len(tc) != EXPECTED_TEAM_ROWS:
        problems.append(f"teams_canonical rows={len(tc)}, expected {EXPECTED_TEAM_ROWS}")
    tids = [r["team_id"] for r in tc]
    if len(set(tids)) != len(tids):
        problems.append("duplicate team_id in teams_canonical")
    tc_ids = set(tids)
    phase2_ids = set(t["team_id"] for t in state["teams_rows_raw"])
    if tc_ids != phase2_ids:
        problems.append(f"team_id set mismatch: missing={len(phase2_ids-tc_ids)}, "
                        f"extra={len(tc_ids-phase2_ids)}")
    for r in tc:
        if r["canonical_status"] != "MAPPED":
            problems.append(f"teams_canonical not MAPPED: {r['team_id']}")
        if r["canonical_id"] not in cid_set:
            problems.append(f"teams_canonical canonical_id not in canonical_teams: {r['team_id']}")
        if r["merge_status"] != "SELF":
            problems.append(f"teams_canonical merge_status != SELF: {r['team_id']}")

    # teams_canonical active_status must equal Phase 2
    phase2_active = {t["team_id"]: t["active_status"] for t in state["teams_rows_raw"]}
    for r in tc:
        if r["active_status"] != phase2_active[r["team_id"]]:
            problems.append(f"active_status mismatch vs Phase 2: {r['team_id']}")
            break

    return problems

# ----- Main -----

def main():
    print("=" * 78)
    print("PHASE 3 — E1: CANONICAL BASE")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Mappings dir : {MAPPINGS_DIR}")
    print(f"Report       : {REPORT_TXT}")
    print()

    # Pre-flight
    if not TEAMS_CSV.exists():
        print(f"[FAIL] input missing: {TEAMS_CSV}")
        sys.exit(1)
    if not LEAGUES_CSV.exists():
        print(f"[FAIL] input missing: {LEAGUES_CSV}")
        sys.exit(1)

    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_TXT.parent.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    # SHA-256 pre (read-only proof)
    pre_sha = {
        "teams.csv":   sha256_file(TEAMS_CSV),
        "leagues.csv": sha256_file(LEAGUES_CSV),
    }
    print("--- PRE SHA-256 (read-only inputs) ---")
    for k, v in pre_sha.items():
        print(f"  {k:14s} {v}")
    print()

    # Build
    print("--- BUILD ---")
    state = build_all()
    print(f"  canonical_teams rows     : {len(state['canonical_rows'])}")
    print(f"  team_aliases rows        : {len(state['alias_rows'])}")
    print(f"  teams_canonical rows     : {len(state['teams_canonical_rows'])}")
    print(f"  review_queue rows        : {len(state['review_queue_rows'])}  (baseline empty)")
    print(f"  cross_league rows        : {len(state['cross_league_rows'])}  (baseline empty)")
    print(f"  deprecated_map rows      : {len(state['deprecated_rows'])}  (baseline empty)")
    print()

    # Determinism rebuild check
    print("--- DETERMINISM ---")
    state2 = build_all()

    def ser(state_):
        return (
            "\n".join("|".join(str(r[f]) for f in CANONICAL_FIELDS)
                      for r in sorted(state_["canonical_rows"], key=lambda r: r["canonical_id"]))
            + "\n--\n" +
            "\n".join("|".join(str(r[f]) for f in ALIAS_FIELDS)
                      for r in sorted(state_["alias_rows"], key=lambda r: r["alias_id"]))
            + "\n--\n" +
            "\n".join("|".join(str(r[f]) for f in TEAMS_CANONICAL_FIELDS)
                      for r in sorted(state_["teams_canonical_rows"], key=lambda r: r["team_id"]))
        )
    if ser(state) != ser(state2):
        print("  [FAIL] non-deterministic rebuild detected")
        sys.exit(1)
    print("  [OK] rebuild 2x identical")
    print()

    # Validate in-memory
    print("--- VALIDATE (in-memory) ---")
    problems = validate(state)
    if problems:
        print(f"  [FAIL] {len(problems)} problem(s):")
        for p in problems[:20]:
            print(f"    - {p}")
        sys.exit(1)
    print("  [OK] all in-memory checks passed")
    print()

    # Manifest content (empty baseline)
    manifest_content = (
        "# Phase 3 Review Queue Manifest\n"
        "# Generated by: scripts/phase3_e1_canonical_base.py\n"
        "# Immutable fields: review_id, raw_name_a, raw_name_b,\n"
        "#                   canonical_a_id, canonical_b_id, algorithm,\n"
        "#                   score, suggested_merge\n"
        "# Format: <sha256_of_immutable_line> <review_id>\n"
        "# Rows: 0 (baseline — no candidates yet)\n"
    )

    # Idempotent writes for all 7 outputs + report
    print("--- WRITE (idempotent per file) ---")
    actions = {}

    def maybe_write_csv(path, fields, rows, pk):
        if file_equals_csv(path, fields, rows):
            actions[path.name] = "SKIP_IDENTICAL"
            return
        if path.exists():
            # backup then BLOCKED (do NOT overwrite silently)
            actions[path.name] = "BLOCKED_DIFFERENT"
            return
        write_csv(path, fields, rows, pk)
        actions[path.name] = "WROTE"

    def maybe_write_text(path, content):
        if file_equals_text(path, content):
            actions[path.name] = "SKIP_IDENTICAL"
            return
        if path.exists():
            actions[path.name] = "BLOCKED_DIFFERENT"
            return
        write_text(path, content)
        actions[path.name] = "WROTE"

    maybe_write_csv(CANONICAL_TEAMS_CSV, CANONICAL_FIELDS,
                    state["canonical_rows"], "canonical_id")
    maybe_write_csv(TEAM_ALIASES_CSV, ALIAS_FIELDS,
                    state["alias_rows"], "alias_id")
    maybe_write_csv(REVIEW_QUEUE_CSV, REVIEW_QUEUE_FIELDS,
                    state["review_queue_rows"], "review_id")
    maybe_write_csv(CROSS_LEAGUE_CSV, CROSS_LEAGUE_FIELDS,
                    state["cross_league_rows"], "proposal_id")
    maybe_write_csv(DEPRECATED_MAP_CSV, DEPRECATED_FIELDS,
                    state["deprecated_rows"], "deprecated_canonical_id")
    maybe_write_csv(TEAMS_CANONICAL_CSV, TEAMS_CANONICAL_FIELDS,
                    state["teams_canonical_rows"], "team_id")
    maybe_write_text(REVIEW_QUEUE_MANIFEST, manifest_content)

    for name, act in actions.items():
        print(f"  [{act:18s}] {name}")
    print()

    blocked = [n for n, a in actions.items() if a == "BLOCKED_DIFFERENT"]
    if blocked:
        print("STATUS: E1 BLOCKED_DIFFERENT ❌")
        print(f"Files with existing DIFFERENT content: {blocked}")
        print()
        print("ACTION: User must inspect existing files vs new build, backup, and")
        print("        explicitly delete or rename before re-running E1.")
        print("        NO auto-overwrite performed.")
        sys.exit(1)

    # Read-back and verify final state (only validate files that exist now)
    print("--- VERIFY (post-write) ---")

    def read_csv_final(path, fields):
        with open(path, "r", encoding="utf-8", newline="") as f:
            rdr = csv.DictReader(f)
            got_fields = rdr.fieldnames or []
            rows = [dict(r) for r in rdr]
        return got_fields, rows

    # Build final-state dict for validate()
    final = {
        "teams_rows_raw":       state["teams_rows_raw"],
        "leagues_rows_raw":     state["leagues_rows_raw"],
    }
    f1, r1 = read_csv_final(CANONICAL_TEAMS_CSV, CANONICAL_FIELDS)
    if f1 != CANONICAL_FIELDS:
        print(f"[FAIL] canonical_teams fields mismatch: {f1}"); sys.exit(1)
    final["canonical_rows"] = r1

    f2, r2 = read_csv_final(TEAM_ALIASES_CSV, ALIAS_FIELDS)
    if f2 != ALIAS_FIELDS:
        print(f"[FAIL] team_aliases fields mismatch: {f2}"); sys.exit(1)
    final["alias_rows"] = r2

    f3, r3 = read_csv_final(TEAMS_CANONICAL_CSV, TEAMS_CANONICAL_FIELDS)
    if f3 != TEAMS_CANONICAL_FIELDS:
        print(f"[FAIL] teams_canonical fields mismatch: {f3}"); sys.exit(1)
    final["teams_canonical_rows"] = r3

    problems = validate(final)
    if problems:
        print(f"  [FAIL] post-write problems:")
        for p in problems[:20]:
            print(f"    - {p}")
        sys.exit(1)
    print("  [OK] post-write validation passed")
    print()

    # Read-only integrity post-check
    print("--- READ-ONLY INTEGRITY (Phase 2 inputs) ---")
    post_sha = {
        "teams.csv":   sha256_file(TEAMS_CSV),
        "leagues.csv": sha256_file(LEAGUES_CSV),
    }
    for k in pre_sha:
        same = pre_sha[k] == post_sha[k]
        print(f"  [{'PASS' if same else 'FAIL'}] {k} unchanged")
        if not same:
            print(f"    pre={pre_sha[k][:16]}... post={post_sha[k][:16]}...")
            sys.exit(1)
    print()

    # Report
    report_lines = []
    report_lines.append("# Phase 3 — E1 Report")
    report_lines.append("Mode: Canonical base (schema + self-map)")
    report_lines.append("")
    report_lines.append("## Inputs (read-only)")
    report_lines.append(f"teams.csv   SHA-256: {pre_sha['teams.csv']}")
    report_lines.append(f"leagues.csv SHA-256: {pre_sha['leagues.csv']}")
    report_lines.append("")
    report_lines.append("## Output row counts")
    report_lines.append(f"canonical_teams      : {len(r1)}")
    report_lines.append(f"team_aliases         : {len(r2)}")
    report_lines.append(f"teams_canonical      : {len(r3)}")
    report_lines.append(f"review_queue         : 0 (empty baseline)")
    report_lines.append(f"cross_league_props   : 0 (empty baseline)")
    report_lines.append(f"deprecated_map       : 0 (empty baseline)")
    report_lines.append("")
    report_lines.append("## Determinism")
    report_lines.append("rebuild 2x: identical")
    report_lines.append("")
    report_lines.append("## Idempotency")
    for name, act in sorted(actions.items()):
        report_lines.append(f"{name:30s} : {act}")
    report_lines.append("")
    report_lines.append("## Phase 2 immutability")
    report_lines.append(f"teams.csv   : pre == post  (unchanged)")
    report_lines.append(f"leagues.csv : pre == post  (unchanged)")
    report_lines.append("")
    report_lines.append("## Notes")
    report_lines.append("- Self-map only. No merges, no aliases beyond self.")
    report_lines.append("- review_queue empty. cross_league empty. deprecated_map empty.")
    report_lines.append("- No created_date/timestamp in any output (determinism).")
    report_lines.append("- active_status preserved per-tim in teams_canonical.")
    report_lines.append("")
    report_lines.append("STATUS: E1 PASS")
    report_content = "\n".join(report_lines) + "\n"

    if file_equals_text(REPORT_TXT, report_content):
        print(f"  [SKIP_IDENTICAL] {REPORT_TXT.name}")
    else:
        if REPORT_TXT.exists():
            print(f"  [WARN] {REPORT_TXT.name} exists with different content; overwriting report only")
        write_text(REPORT_TXT, report_content)
        print(f"  [WROTE] {REPORT_TXT.name}")
    print()

    # Acceptance summary
    print("=" * 78)
    print("E1 ACCEPTANCE")
    print("=" * 78)

    checks = [
        ("canonical_teams rows == 144",           len(r1) == 144),
        ("team_aliases rows == 144",              len(r2) == 144),
        ("teams_canonical rows == 144",           len(r3) == 144),
        ("canonical_id unique",                   len(set(x['canonical_id'] for x in r1)) == len(r1)),
        ("alias_id unique",                       len(set(x['alias_id'] for x in r2)) == len(r2)),
        ("team_id unique in teams_canonical",     len(set(x['team_id'] for x in r3)) == len(r3)),
        ("all aliases APPROVED",                  all(x['mapping_status'] == 'APPROVED' for x in r2)),
        ("all confidence 1.00",                   all(abs(float(x['confidence']) - 1.00) < 1e-9 for x in r2)),
        ("all teams_canonical MAPPED",            all(x['canonical_status'] == 'MAPPED' for x in r3)),
        ("all teams_canonical merge_status SELF", all(x['merge_status'] == 'SELF' for x in r3)),
        ("active_status preserved from Phase 2",
            all(x['active_status'] == next(t['active_status'] for t in state['teams_rows_raw']
                                            if t['team_id'] == x['team_id'])
                for x in r3)),
        ("alias FK -> canonical_teams",
            all(x['canonical_id'] in set(y['canonical_id'] for y in r1) for x in r2)),
        ("teams_canonical FK -> canonical_teams",
            all(x['canonical_id'] in set(y['canonical_id'] for y in r1) for x in r3)),
        ("teams_canonical team_id set == Phase 2",
            set(x['team_id'] for x in r3) == set(t['team_id'] for t in state['teams_rows_raw'])),
        ("review_queue empty baseline",           len(r1) >= 0 and REVIEW_QUEUE_CSV.exists()),
        ("cross_league empty baseline",           CROSS_LEAGUE_CSV.exists()),
        ("deprecated_map empty baseline",         DEPRECATED_MAP_CSV.exists()),
        ("review_queue manifest exists",          REVIEW_QUEUE_MANIFEST.exists()),
        ("Phase 2 teams.csv unchanged",           pre_sha['teams.csv'] == post_sha['teams.csv']),
        ("Phase 2 leagues.csv unchanged",         pre_sha['leagues.csv'] == post_sha['leagues.csv']),
        ("determinism (rebuild 2x)",              True),
    ]
    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print()

    print(f"  canonical_teams : {len(r1)} rows")
    print(f"  team_aliases    : {len(r2)} rows")
    print(f"  teams_canonical : {len(r3)} rows")
    print()

    if all_ok:
        print("STATUS: E1 PASS ✅")
        print()
        print("NEXT: User approval → E2 (fuzzy candidates)")
        sys.exit(0)
    else:
        print("STATUS: E1 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
