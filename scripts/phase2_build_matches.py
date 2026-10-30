#!/usr/bin/env python3
"""
Phase 2 — D6: BUILD matches.csv
================================

Input:
  - 18 eligible files:
      data/raw/{LEAGUE}_2425.csv                    (6 files)
      data/raw/{LEAGUE}_2526.csv                    (6 files)
      data/raw/snapshots/2627/2026-10-30/{LEAGUE}_2627.csv  (6 files)
  - data/processed/leagues.csv
  - data/processed/teams.csv

  EXCLUDED: 2324 (HISTORICAL_BACKUP)

Output:
  - data/processed/matches.csv (17 columns, target 4,608 rows)
  - data/reports/phase2_d6_match_audit.txt

match_id (LOCKED formula):
  md5: + MD5( league_code | season | date_iso | norm_home | norm_away )
  norm = lowercase + strip + collapse spaces (for hash only)

Schema (LOCKED, 17 columns):
  match_id, league_code, league_name, season, season_label, date,
  home_team, away_team, home_goals, away_goals,
  halftime_home_goals, halftime_away_goals,
  btts_result, data_status, source_file, snapshot_date, notes

data_status rules:
  COMPLETE         : FTHG, FTAG, HTHG, HTAG all valid (int>=0)
  INCOMPLETE_STATS : FTHG, FTAG valid; HTHG or HTAG missing/invalid
  INVALID          : FTHG/FTAG missing/invalid

BTTS result:
  1 = YES   (FTHG > 0 AND FTAG > 0)
  0 = NO
  empty = INVALID (no usable FT goals)

TIDAK mengerjakan:
  - BTTS per-team stats (Phase 4)
  - ML features (Phase 5)
  - D7 validate_db (separate approval)
  - Phase 3 / Phase 4
"""
import sys
import csv
import hashlib
import shutil
import re
from pathlib import Path
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
RAW_DIR       = DATA_DIR / "raw"
SNAPSHOT_DIR  = RAW_DIR / "snapshots" / "2627" / "2026-10-30"
PROCESSED_DIR = DATA_DIR / "processed"
BACKUP_DIR    = DATA_DIR / "backups"
REPORTS_DIR   = DATA_DIR / "reports"

OUT_CSV   = PROCESSED_DIR / "matches.csv"
AUDIT_TXT = REPORTS_DIR / "phase2_d6_match_audit.txt"

LEAGUES_CSV     = PROCESSED_DIR / "leagues.csv"
TEAMS_CSV       = PROCESSED_DIR / "teams.csv"
SEASON_META_CSV = PROCESSED_DIR / "season_meta.csv"

LEAGUES = ["E0", "D1", "SP1", "I1", "F1", "N1"]

# Sources (LOCKED). snapshot_date is empty for full-season sources.
SOURCES = [
    ("2425", 2024, "2024/25", RAW_DIR,      ""),           # 2024/25 full
    ("2526", 2025, "2025/26", RAW_DIR,      ""),           # 2025/26 full
    ("2627", 2026, "2026/27", SNAPSHOT_DIR, "2026-10-30"), # snapshot
]

TARGET_ROWS = 4608
EXPECTED_COMPLETE   = 4607
EXPECTED_INCOMPLETE = 1
EXPECTED_INVALID    = 0

FIELDNAMES = [
    "match_id", "league_code", "league_name", "season", "season_label", "date",
    "home_team", "away_team", "home_goals", "away_goals",
    "halftime_home_goals", "halftime_away_goals",
    "btts_result", "data_status", "source_file", "snapshot_date", "notes",
]

MATCH_ID_RE = re.compile(r"^md5:[0-9a-f]{32}$")
VALID_STATUS = {"COMPLETE", "INCOMPLETE_STATS", "INVALID"}

# The 1 known INCOMPLETE_STATS row (Union Berlin vs Bochum, D1_2425)
KNOWN_INCOMPLETE = {
    "league_code": "D1",
    "season": 2024,
    "date": "2024-12-14",
    "home_team": "Union Berlin",
    "away_team": "Bochum",
}

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def normalize_name(raw: str) -> str:
    if raw is None:
        return ""
    return " ".join(str(raw).strip().lower().split())

def make_match_id(league_code: str, season: int, date_iso: str,
                  home_norm: str, away_norm: str) -> str:
    raw = f"{league_code}|{season}|{date_iso}|{home_norm}|{away_norm}"
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()
    return f"md5:{h}"

def parse_date_iso(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

def parse_int(s):
    if s is None:
        return None
    s = str(s).strip()
    if s == "" or s.lower() in ("nan", "none", "null", "-"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None

def read_csv_robust(path: Path):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                rows = [dict(r) for r in reader]
                fields = reader.fieldnames or []
            return rows, fields, enc, None
        except UnicodeDecodeError as e:
            last_err = str(e)
            continue
        except Exception as e:
            return [], [], enc, str(e)[:200]
    return [], [], "?", last_err

def _coerce_row(r: dict) -> dict:
    """Normalize a match row for comparison.
    Empty strings for numeric fields stay empty."""
    def _s_int(v):
        return "" if v is None or str(v) == "" else str(int(v))
    return {
        "match_id":             str(r["match_id"]).strip(),
        "league_code":          str(r["league_code"]).strip(),
        "league_name":          str(r["league_name"]).strip(),
        "season":               str(int(r["season"])),
        "season_label":         str(r["season_label"]).strip(),
        "date":                 str(r["date"]).strip(),
        "home_team":            str(r["home_team"]),   # as-is
        "away_team":            str(r["away_team"]),   # as-is
        "home_goals":           _s_int(r.get("home_goals")),
        "away_goals":           _s_int(r.get("away_goals")),
        "halftime_home_goals":  _s_int(r.get("halftime_home_goals")),
        "halftime_away_goals":  _s_int(r.get("halftime_away_goals")),
        "btts_result":          _s_int(r.get("btts_result")),
        "data_status":          str(r["data_status"]).strip(),
        "source_file":          str(r["source_file"]).strip(),
        "snapshot_date":        str(r.get("snapshot_date", "")).strip(),
        "notes":                str(r.get("notes", "")).strip(),
    }

def file_equals_csv(path: Path, expected_rows: list) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            rows = [dict(r) for r in reader]
    except Exception:
        return False
    if fields != FIELDNAMES:
        return False
    if len(rows) != len(expected_rows):
        return False
    norm_existing = [_coerce_row(r) for r in rows]
    norm_expected = [_coerce_row(r) for r in expected_rows]
    # Row-by-row comparison must be order-independent → sort by match_id
    norm_existing.sort(key=lambda x: x["match_id"])
    norm_expected.sort(key=lambda x: x["match_id"])
    return norm_existing == norm_expected

# ---------- Pre-flight ----------

def preflight() -> dict:
    """Verify inputs exist & load leagues + teams."""
    problems = []

    # 1. 18 source files
    for code, _, _, src_dir, _ in SOURCES:
        for league in LEAGUES:
            f = src_dir / f"{league}_{code}.csv"
            if not f.exists():
                problems.append(f"source missing: {f}")

    # 2. leagues.csv
    if not LEAGUES_CSV.exists():
        problems.append(f"missing: {LEAGUES_CSV}")
    # 3. teams.csv
    if not TEAMS_CSV.exists():
        problems.append(f"missing: {TEAMS_CSV}")
    # 4. season_meta.csv (informational, needed to validate priority keys)
    if not SEASON_META_CSV.exists():
        problems.append(f"missing: {SEASON_META_CSV}")

    if problems:
        print("[FAIL] Pre-flight problems:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    # Load leagues
    league_names = {}
    with open(LEAGUES_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            league_names[r["league_code"]] = r["league_name"]

    # Load teams (raw_name set)
    team_raw_names = set()
    with open(TEAMS_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            team_raw_names.add(r["raw_name"])

    # Load season_meta (season set)
    season_meta = {}
    with open(SEASON_META_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            season_meta[int(r["season"])] = r

    return {
        "league_names":   league_names,
        "team_raw_names": team_raw_names,
        "season_meta":    season_meta,
    }

# ---------- Build ----------

def classify_status(fthg, ftag, hthg, htag):
    """Return (data_status, btts_result, notes)."""
    ft_ok = (fthg is not None and ftag is not None
             and fthg >= 0 and ftag >= 0)
    ht_ok = (hthg is not None and htag is not None
             and hthg >= 0 and htag >= 0)

    if not ft_ok:
        return "INVALID", "", "FT goals missing or invalid"

    btts = 1 if (fthg > 0 and ftag > 0) else 0

    if not ht_ok:
        return "INCOMPLETE_STATS", btts, "HT missing in source"
    return "COMPLETE", btts, ""

def build_matches(ctx: dict):
    """Iterate sources, build match rows. Return rows list + stats."""
    rows = []
    stats = {
        "files_read":    0,
        "rows_read":     0,
        "files_errors":  [],
    }

    league_names   = ctx["league_names"]
    team_raw_names = ctx["team_raw_names"]

    for code, season, label, src_dir, snap_date in SOURCES:
        for league in LEAGUES:
            f = src_dir / f"{league}_{code}.csv"
            file_rows, fields, enc, err = read_csv_robust(f)
            if err:
                stats["files_errors"].append(f"{f.name}: {err}")
                continue
            required = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG",
                        "HTHG", "HTAG"]
            missing = [c for c in required if c not in fields]
            if missing:
                stats["files_errors"].append(
                    f"{f.name}: missing cols {missing}"
                )
                continue

            stats["files_read"] += 1
            stats["rows_read"] += len(file_rows)

            for idx, raw_row in enumerate(file_rows):
                date_iso = parse_date_iso(raw_row.get("Date"))
                if date_iso is None:
                    # Defensive: D1b already validated dates are all parseable
                    # If we hit this, it's a data drift → fail loud
                    stats["files_errors"].append(
                        f"{f.name} row {idx+1}: unparseable date "
                        f"{raw_row.get('Date')!r}"
                    )
                    continue

                home_raw = raw_row.get("HomeTeam", "")
                away_raw = raw_row.get("AwayTeam", "")
                if not home_raw or not away_raw:
                    stats["files_errors"].append(
                        f"{f.name} row {idx+1}: empty team name"
                    )
                    continue

                fthg = parse_int(raw_row.get("FTHG"))
                ftag = parse_int(raw_row.get("FTAG"))
                hthg = parse_int(raw_row.get("HTHG"))
                htag = parse_int(raw_row.get("HTAG"))

                status, btts, notes = classify_status(fthg, ftag, hthg, htag)

                home_norm = normalize_name(home_raw)
                away_norm = normalize_name(away_raw)
                match_id = make_match_id(league, season, date_iso,
                                         home_norm, away_norm)

                rows.append({
                    "match_id":             match_id,
                    "league_code":          league,
                    "league_name":          league_names.get(league, ""),
                    "season":               season,
                    "season_label":         label,
                    "date":                 date_iso,
                    "home_team":            str(home_raw),
                    "away_team":            str(away_raw),
                    "home_goals":           fthg if fthg is not None else "",
                    "away_goals":           ftag if ftag is not None else "",
                    "halftime_home_goals":  hthg if hthg is not None else "",
                    "halftime_away_goals":  htag if htag is not None else "",
                    "btts_result":          btts if btts != "" else "",
                    "data_status":          status,
                    "source_file":          f.name,
                    "snapshot_date":        snap_date,
                    "notes":                notes,
                })

    # Deterministic sort by match_id
    rows.sort(key=lambda r: r["match_id"])
    return rows, stats

# ---------- Determinism ----------

def determinism_check(ctx: dict) -> tuple:
    rows_a, _ = build_matches(ctx)
    rows_b, _ = build_matches(ctx)

    def ser(rows):
        buf = []
        for r in rows:
            buf.append("|".join(str(r[f]) for f in FIELDNAMES))
        return "\n".join(buf)

    if ser(rows_a) == ser(rows_b):
        return True, f"deterministic ({len(rows_a)} rows, rebuild identical)"
    return False, "NON-DETERMINISTIC"

# ---------- Validate ----------

def validate_matches(rows: list, ctx: dict) -> list:
    problems = []
    league_names   = ctx["league_names"]
    team_raw_names = ctx["team_raw_names"]

    # Row count
    if len(rows) != TARGET_ROWS:
        problems.append(
            f"expected {TARGET_ROWS} rows, got {len(rows)}"
        )

    # Season 2023 forbidden
    s2023 = [r for r in rows if int(r["season"]) == 2023]
    if s2023:
        problems.append(f"{len(s2023)} rows with season 2023 (forbidden)")

    # Match ID format + uniqueness
    ids = [r["match_id"] for r in rows]
    for r in rows:
        if not MATCH_ID_RE.match(r["match_id"]):
            problems.append(f"invalid match_id: {r['match_id']!r}")
    if len(set(ids)) != len(ids):
        dup = [i for i in set(ids) if ids.count(i) > 1]
        problems.append(f"duplicate match_id ({len(dup)}): {dup[:3]}")

    # Status distribution
    status_counts = defaultdict(int)
    for r in rows:
        status_counts[r["data_status"]] += 1
        if r["data_status"] not in VALID_STATUS:
            problems.append(f"invalid data_status: {r['data_status']!r} "
                            f"for {r['match_id']}")
    if status_counts["COMPLETE"] != EXPECTED_COMPLETE:
        problems.append(f"COMPLETE count = {status_counts['COMPLETE']}, "
                        f"expected {EXPECTED_COMPLETE}")
    if status_counts["INCOMPLETE_STATS"] != EXPECTED_INCOMPLETE:
        problems.append(f"INCOMPLETE_STATS count = {status_counts['INCOMPLETE_STATS']}, "
                        f"expected {EXPECTED_INCOMPLETE}")
    if status_counts["INVALID"] != EXPECTED_INVALID:
        problems.append(f"INVALID count = {status_counts['INVALID']}, "
                        f"expected {EXPECTED_INVALID}")

    # Foreign key: league_code
    for r in rows:
        if r["league_code"] not in league_names:
            problems.append(f"unknown league_code {r['league_code']!r} "
                            f"in {r['match_id']}")

    # Foreign key: home_team / away_team
    missing_teams = set()
    for r in rows:
        if r["home_team"] not in team_raw_names:
            missing_teams.add(r["home_team"])
        if r["away_team"] not in team_raw_names:
            missing_teams.add(r["away_team"])
    if missing_teams:
        problems.append(f"{len(missing_teams)} team(s) not in teams.csv: "
                        f"{sorted(missing_teams)[:5]}")

    # Date ISO + btts consistency + data type spot checks
    for r in rows:
        try:
            datetime.strptime(r["date"], "%Y-%m-%d")
        except (ValueError, TypeError):
            problems.append(f"non-ISO date {r['date']!r} in {r['match_id']}")

        # BTTS consistency for COMPLETE & INCOMPLETE_STATS
        if r["data_status"] in ("COMPLETE", "INCOMPLETE_STATS"):
            hg = r["home_goals"]
            ag = r["away_goals"]
            if hg == "" or ag == "":
                problems.append(f"FT goals empty for non-INVALID: "
                                f"{r['match_id']}")
                continue
            expected_btts = 1 if (int(hg) > 0 and int(ag) > 0) else 0
            if int(r["btts_result"]) != expected_btts:
                problems.append(
                    f"btts_result mismatch for {r['match_id']}: "
                    f"got {r['btts_result']}, expected {expected_btts} "
                    f"(FT {hg}-{ag})"
                )

    # The 1 known INCOMPLETE_STATS must match exactly
    known = [r for r in rows if
             r["league_code"] == KNOWN_INCOMPLETE["league_code"]
             and int(r["season"]) == KNOWN_INCOMPLETE["season"]
             and r["date"] == KNOWN_INCOMPLETE["date"]
             and r["home_team"] == KNOWN_INCOMPLETE["home_team"]
             and r["away_team"] == KNOWN_INCOMPLETE["away_team"]]
    if len(known) != 1:
        problems.append(
            f"known INCOMPLETE_STATS row not found exactly once: {known}"
        )
    else:
        k = known[0]
        if k["data_status"] != "INCOMPLETE_STATS":
            problems.append(f"known row data_status wrong: {k['data_status']}")
        if int(k["home_goals"]) != 0 or int(k["away_goals"]) != 2:
            problems.append(f"known row FT wrong: {k['home_goals']}-{k['away_goals']}")
        if int(k["btts_result"]) != 0:
            problems.append(f"known row btts_result wrong: {k['btts_result']}")
        if k["halftime_home_goals"] != "" or k["halftime_away_goals"] != "":
            problems.append(f"known row HT not empty: "
                            f"{k['halftime_home_goals']}-{k['halftime_away_goals']}")
        if "HT missing" not in k["notes"]:
            problems.append(f"known row notes wrong: {k['notes']!r}")

    return problems

# ---------- Audit ----------

def write_audit(path, rows, stats, determinism_msg,
                verify_fields, verify_rows, write_action, out_sha,
                mode="PASS"):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# Phase 2 — D6 Match Audit Report")
    lines.append(f"# Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"# Mode: {mode}")
    lines.append("")

    # Input stats
    lines.append("## Input")
    lines.append(f"files_read   : {stats['files_read']}")
    lines.append(f"rows_read    : {stats['rows_read']}")
    lines.append(f"files_errors : {stats['files_errors']}")
    lines.append("")

    # Output summary
    lines.append("## Output")
    lines.append(f"output_sha256 : {out_sha}")
    lines.append(f"write_action  : {write_action}")
    lines.append(f"total_rows    : {len(verify_rows)}")
    lines.append("")

    # Status distribution
    st = defaultdict(int)
    for r in verify_rows:
        st[r["data_status"]] += 1
    lines.append("## Data Status Distribution")
    for k in ["COMPLETE", "INCOMPLETE_STATS", "INVALID"]:
        lines.append(f"  {k:18s}: {st.get(k, 0)}")
    lines.append("")

    # Per league
    lines.append("## Rows per League")
    per_league = defaultdict(int)
    for r in verify_rows:
        per_league[r["league_code"]] += 1
    for lc in sorted(per_league):
        lines.append(f"  {lc:4s}: {per_league[lc]}")
    lines.append("")

    # Per season
    lines.append("## Rows per Season")
    per_season = defaultdict(int)
    for r in verify_rows:
        per_season[int(r["season"])] += 1
    for s in sorted(per_season):
        lines.append(f"  {s}: {per_season[s]}")
    lines.append("")

    # Per league × season matrix
    lines.append("## Rows per (League, Season)")
    per_ls = defaultdict(int)
    for r in verify_rows:
        per_ls[(r["league_code"], int(r["season"]))] += 1
    for lc in sorted(set(k[0] for k in per_ls)):
        for s in sorted(set(k[1] for k in per_ls if k[0] == lc)):
            lines.append(f"  {lc} {s}: {per_ls[(lc, s)]}")
    lines.append("")

    # Aggregate BTTS (sanity only — NOT Phase 4 per-team stats)
    lines.append("## Aggregate BTTS (sanity, NOT Phase 4 per-team)")
    n_yes = sum(1 for r in verify_rows
                if r["data_status"] != "INVALID"
                and str(r["btts_result"]) == "1")
    n_no  = sum(1 for r in verify_rows
                if r["data_status"] != "INVALID"
                and str(r["btts_result"]) == "0")
    lines.append(f"  BTTS YES : {n_yes}")
    lines.append(f"  BTTS NO  : {n_no}")
    lines.append(f"  total    : {n_yes + n_no}")
    if n_yes + n_no > 0:
        lines.append(f"  YES rate : {n_yes/(n_yes+n_no):.4f}")
    lines.append("")

    # Determinism
    lines.append("## Determinism")
    lines.append(f"result: {determinism_msg}")
    lines.append("")

    # Foreign keys
    lines.append("## Foreign Key Validation")
    ctx_league_names = set(r["league_code"] for r in verify_rows)
    lines.append("league_code set: " + ",".join(sorted(ctx_league_names)))
    lines.append("all league_code in leagues.csv: checked OK")

    # Sample
    lines.append("## Sample Rows (10)")
    lines.append("match_id | league | season | date | home | away | FT | HT | btts | status")
    for r in verify_rows[:10]:
        lines.append(
            f"{r['match_id']} | {r['league_code']} | {r['season']} | "
            f"{r['date']} | {r['home_team']} | {r['away_team']} | "
            f"{r['home_goals']}-{r['away_goals']} | "
            f"{r['halftime_home_goals']}-{r['halftime_away_goals']} | "
            f"{r['btts_result']} | {r['data_status']}"
        )
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 2 — D6: BUILD matches.csv")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Output       : {OUT_CSV}")
    print(f"Audit        : {AUDIT_TXT}")
    print()

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Pre-flight ----
    print("--- PRE-FLIGHT ---")
    ctx = preflight()
    print(f"  [OK] 18 source files present.")
    print(f"  [OK] leagues.csv loaded ({len(ctx['league_names'])} leagues).")
    print(f"  [OK] teams.csv loaded ({len(ctx['team_raw_names'])} raw names).")
    print(f"  [OK] season_meta.csv loaded ({len(ctx['season_meta'])} seasons).")
    print()

    # ---- Build ----
    print("--- BUILD ---")
    rows, stats = build_matches(ctx)
    print(f"  files read    : {stats['files_read']}")
    print(f"  rows read     : {stats['rows_read']}")
    print(f"  rows built    : {len(rows)}")
    if stats["files_errors"]:
        print(f"  [FAIL] files_errors: {stats['files_errors']}")
        sys.exit(1)
    print()

    # ---- Determinism check ----
    print("--- DETERMINISM CHECK ---")
    ok, det_msg = determinism_check(ctx)
    if not ok:
        print(f"  [FAIL] {det_msg}")
        sys.exit(1)
    print(f"  [OK] {det_msg}")
    print()

    # ---- Validation pre-write ----
    print("--- VALIDATE (pre-write) ---")
    problems = validate_matches(rows, ctx)
    if problems:
        print(f"  [FAIL] {len(problems)} problem(s):")
        for p in problems[:20]:
            print(f"    - {p}")
        if len(problems) > 20:
            print(f"    ... and {len(problems) - 20} more")
        print()
        print("STATUS: D6 FAIL — VALIDATION")
        sys.exit(1)
    print(f"  [OK] All {len(rows)} rows validated.")
    print()

    # ---- Idempotent write ----
    print("--- WRITE (idempotent) ---")
    if file_equals_csv(OUT_CSV, rows):
        print(f"  [SKIP_IDENTICAL] {OUT_CSV.relative_to(PROJECT_ROOT)} "
              f"already identical.")
        write_action = "SKIP_IDENTICAL"
    elif OUT_CSV.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"phase2_D6_matches_{ts}.csv"
        shutil.copy2(OUT_CSV, backup_path)
        print(f"  [BLOCKED_DIFFERENT] Existing differs from build.")
        print(f"  [BACKUP] Saved -> {backup_path.relative_to(PROJECT_ROOT)}")
        print()
        print("STATUS: D6 BLOCKED_DIFFERENT ❌")
        print("NOT overwriting. User must review.")
        sys.exit(1)
    else:
        with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        print(f"  [OK] Wrote {OUT_CSV.relative_to(PROJECT_ROOT)}")
        write_action = "WROTE"
    print()

    # ---- Post-write verify ----
    print("--- VERIFY (post-write) ---")
    with open(OUT_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        verify_fields = reader.fieldnames or []
        verify_rows = [dict(r) for r in reader]
    print(f"  fields : {verify_fields}")
    print(f"  rows   : {len(verify_rows)}")
    print(f"  sha256 : {sha256_file(OUT_CSV)}")
    print(f"  size   : {OUT_CSV.stat().st_size} bytes")

    problems = validate_matches(verify_rows, ctx)
    if problems:
        print(f"  [FAIL] post-write validation:")
        for p in problems[:20]:
            print(f"    - {p}")
        sys.exit(1)
    print(f"  [OK] Post-write validation PASS.")
    print()

    # ---- Audit ----
    write_audit(AUDIT_TXT, rows, stats, det_msg,
                verify_fields, verify_rows, write_action,
                sha256_file(OUT_CSV))
    print(f"  [OK] Audit: {AUDIT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    # ---- Acceptance ----
    print("=" * 78)
    print("D6 ACCEPTANCE")
    print("=" * 78)
    st = defaultdict(int)
    for r in verify_rows:
        st[r["data_status"]] += 1

    checks = [
        ("Output file exists",                       OUT_CSV.exists()),
        ("Schema fields match",                      verify_fields == FIELDNAMES),
        ("Exactly 4,608 rows",                       len(verify_rows) == TARGET_ROWS),
        ("No season 2023",                           not any(int(r["season"]) == 2023 for r in verify_rows)),
        ("All match_id match md5:32hex",             all(MATCH_ID_RE.match(r["match_id"]) for r in verify_rows)),
        ("No duplicate match_id",                    len(set(r["match_id"] for r in verify_rows)) == len(verify_rows)),
        ("COMPLETE == 4,607",                        st["COMPLETE"] == EXPECTED_COMPLETE),
        ("INCOMPLETE_STATS == 1",                    st["INCOMPLETE_STATS"] == EXPECTED_INCOMPLETE),
        ("INVALID == 0",                             st["INVALID"] == EXPECTED_INVALID),
        ("All dates ISO valid",
            all(_iso_ok(r["date"]) for r in verify_rows)),
        ("All league_code in leagues.csv",
            all(r["league_code"] in ctx["league_names"] for r in verify_rows)),
        ("All home/away teams in teams.csv",
            all(r["home_team"] in ctx["team_raw_names"]
                and r["away_team"] in ctx["team_raw_names"]
                for r in verify_rows)),
        ("Determinism verified",                     ok),
    ]
    all_ok = all(ok_ for _, ok_ in checks)
    for desc, ok_ in checks:
        print(f"  [{'PASS' if ok_ else 'FAIL'}] {desc}")
    print()

    # Aggregate sanity (NOT Phase 4)
    n_yes = sum(1 for r in verify_rows
                if r["data_status"] != "INVALID" and str(r["btts_result"]) == "1")
    n_no  = sum(1 for r in verify_rows
                if r["data_status"] != "INVALID" and str(r["btts_result"]) == "0")
    print(f"  Total rows   : {len(verify_rows)}")
    print(f"  COMPLETE     : {st['COMPLETE']}")
    print(f"  INCOMPLETE   : {st['INCOMPLETE_STATS']}")
    print(f"  INVALID      : {st['INVALID']}")
    print(f"  BTTS YES (agg): {n_yes}")
    print(f"  BTTS NO  (agg): {n_no}")
    if n_yes + n_no > 0:
        print(f"  YES rate (agg): {n_yes/(n_yes+n_no):.4f}")
    print()

    if all_ok:
        print("STATUS: D6 PASS ✅")
        print()
        print("NEXT: User approval → D7 (validate database)")
        sys.exit(0)
    else:
        print("STATUS: D6 FAIL ❌")
        sys.exit(1)

def _iso_ok(s):
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False

if __name__ == "__main__":
    main()
