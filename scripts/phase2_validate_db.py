#!/usr/bin/env python3
"""
Phase 2 — D7: VALIDATE DATABASE (READ-ONLY)
=============================================

Input  (READ-ONLY, tidak diubah):
  data/processed/leagues.csv
  data/processed/season_meta.csv
  data/processed/teams.csv
  data/processed/matches.csv

Output:
  data/reports/phase2_d7_db_validation.txt

Scope (final gate Phase 2):
  A. Referential integrity (FK cross-file)
  B. Uniqueness (PK per file)
  C. Consistency (locked values cross-file)
  D. Orphan detection (STRICT FAIL)
  E. Schema & data type
  F. Data quality (fixed targets)
  G. Read-only integrity (SHA-256 pre/post identical)

STRICT POLICY:
  - Any failure = exit code 1
  - No "warning" allows masking a failed check
  - No auto-fix, no write to processed/

TIDAK mengerjakan:
  - BTTS per-team stats (Phase 4)
  - ML features (Phase 5)
  - Phase 3 / Phase 4 / D6 rebuild
"""
import sys
import csv
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
REPORTS_DIR   = DATA_DIR / "reports"

LEAGUES_CSV     = PROCESSED_DIR / "leagues.csv"
SEASON_META_CSV = PROCESSED_DIR / "season_meta.csv"
TEAMS_CSV       = PROCESSED_DIR / "teams.csv"
MATCHES_CSV     = PROCESSED_DIR / "matches.csv"

OUT_TXT = REPORTS_DIR / "phase2_d7_db_validation.txt"

# LOCKED targets
TARGET_MATCHES   = 4608
TARGET_TEAMS     = 144
TARGET_LEAGUES   = 6
TARGET_SEASONS   = 4
TARGET_COMPLETE  = 4607
TARGET_INCOMPLETE = 1
TARGET_INVALID   = 0

# LOCKED league codes
EXPECTED_LEAGUES = {"E0", "D1", "SP1", "I1", "F1", "N1"}

# LOCKED season metadata
LOCKED_SEASON_META = {
    2023: {"label": "2023/24", "status": "COMPLETED", "priority": "HISTORICAL_BACKUP",
           "weight": 0.0, "train_eligible": 0, "snapshot_date": ""},
    2024: {"label": "2024/25", "status": "COMPLETED", "priority": "SECONDARY",
           "weight": 0.4, "train_eligible": 1, "snapshot_date": ""},
    2025: {"label": "2025/26", "status": "COMPLETED", "priority": "PRIMARY",
           "weight": 1.0, "train_eligible": 1, "snapshot_date": ""},
    2026: {"label": "2026/27", "status": "IN_PROGRESS", "priority": "RECENT_BOOST",
           "weight": 0.8, "train_eligible": 1, "snapshot_date": "2026-10-30"},
}
ELIGIBLE_SEASONS = {2024, 2025, 2026}   # seasons WITH matches
NON_ELIGIBLE_SEASONS = {2023}           # season WITHOUT matches (backup only)

# LOCKED schemas
LEAGUES_FIELDS     = ["league_code", "league_name", "country", "tier",
                      "source", "active_status"]
SEASON_META_FIELDS = ["season", "season_label", "season_status", "priority",
                      "weight", "train_eligible", "snapshot_date", "notes"]
TEAMS_FIELDS       = ["team_id", "raw_name", "normalized_name",
                      "league_first_seen", "first_seen_date",
                      "active_status", "canonical_status"]
MATCHES_FIELDS     = ["match_id", "league_code", "league_name", "season",
                      "season_label", "date", "home_team", "away_team",
                      "home_goals", "away_goals",
                      "halftime_home_goals", "halftime_away_goals",
                      "btts_result", "data_status", "source_file",
                      "snapshot_date", "notes"]

VALID_DATA_STATUS = {"COMPLETE", "INCOMPLETE_STATS", "INVALID"}
VALID_ACTIVE_STATUS = {"ACTIVE", "HISTORICAL"}
VALID_SEASON_STATUS = {"COMPLETED", "IN_PROGRESS"}
VALID_PRIORITY = {"HISTORICAL_BACKUP", "SECONDARY", "PRIMARY", "RECENT_BOOST"}

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

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

def try_int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None

def try_float(v):
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None

def is_iso_date(s):
    try:
        datetime.strptime(str(s).strip(), "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False

# ---------- Validation result collector ----------

class Report:
    def __init__(self):
        self.sections = []   # list of (title, list_of_lines)
        self.failures = []   # flat list of failure descriptions

    def section(self, title):
        self.sections.append((title, []))

    def line(self, s):
        if not self.sections:
            self.sections.append(("(no section)", []))
        self.sections[-1][1].append(s)

    def check(self, ok: bool, desc: str, detail: str = ""):
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {desc}"
        if detail:
            line += f"  ({detail})"
        self.line(line)
        if not ok:
            self.failures.append(desc + (f": {detail}" if detail else ""))

    def ok(self) -> bool:
        return len(self.failures) == 0

    def render(self) -> str:
        buf = []
        for title, lines in self.sections:
            buf.append(title)
            buf.append("-" * len(title))
            buf.extend(lines)
            buf.append("")
        return "\n".join(buf)

# ---------- File loaders with pre-hash ----------

def load_all():
    files = {
        "leagues":     LEAGUES_CSV,
        "season_meta": SEASON_META_CSV,
        "teams":       TEAMS_CSV,
        "matches":     MATCHES_CSV,
    }
    pre_sha = {}
    loaded = {}
    for name, path in files.items():
        if not path.exists():
            print(f"[FAIL] Input missing: {path}")
            sys.exit(1)
        pre_sha[name] = sha256_file(path)
        rows, fields, enc, err = read_csv_robust(path)
        if err:
            print(f"[FAIL] Cannot read {path}: {err}")
            sys.exit(1)
        loaded[name] = {"rows": rows, "fields": fields, "path": path}
    return loaded, pre_sha

# ---------- A. Referential integrity ----------

def check_referential_integrity(rep: Report, data: dict):
    rep.section("A. REFERENTIAL INTEGRITY")

    leagues_set = set(r["league_code"] for r in data["leagues"]["rows"])
    teams_raw   = set(r["raw_name"] for r in data["teams"]["rows"])
    seasons_set = set(int(r["season"]) for r in data["season_meta"]["rows"])

    # A1: matches.league_code -> leagues.league_code
    bad_lc = set()
    for r in data["matches"]["rows"]:
        if r["league_code"] not in leagues_set:
            bad_lc.add(r["league_code"])
    rep.check(len(bad_lc) == 0,
              "matches.league_code ⊆ leagues.league_code",
              f"{len(bad_lc)} bad codes: {sorted(bad_lc)[:5]}" if bad_lc else "")

    # A2: matches.home_team -> teams.raw_name
    bad_home = set()
    for r in data["matches"]["rows"]:
        if r["home_team"] not in teams_raw:
            bad_home.add(r["home_team"])
    rep.check(len(bad_home) == 0,
              "matches.home_team ⊆ teams.raw_name",
              f"{len(bad_home)} bad: {sorted(bad_home)[:5]}" if bad_home else "")

    # A3: matches.away_team -> teams.raw_name
    bad_away = set()
    for r in data["matches"]["rows"]:
        if r["away_team"] not in teams_raw:
            bad_away.add(r["away_team"])
    rep.check(len(bad_away) == 0,
              "matches.away_team ⊆ teams.raw_name",
              f"{len(bad_away)} bad: {sorted(bad_away)[:5]}" if bad_away else "")

    # A4: matches.season -> season_meta.season
    bad_season = set()
    for r in data["matches"]["rows"]:
        s = try_int(r["season"])
        if s is None or s not in seasons_set:
            bad_season.add(r["season"])
    rep.check(len(bad_season) == 0,
              "matches.season ⊆ season_meta.season",
              f"{len(bad_season)} bad: {sorted(bad_season)[:5]}" if bad_season else "")

# ---------- B. Uniqueness ----------

def check_uniqueness(rep: Report, data: dict):
    rep.section("B. UNIQUENESS")

    def _dup_count(rows, key):
        vals = [r[key] for r in rows]
        return len(vals) - len(set(vals))

    # leagues.league_code
    dup = _dup_count(data["leagues"]["rows"], "league_code")
    rep.check(dup == 0, "leagues.league_code unique",
              f"{dup} duplicates" if dup else "")

    # season_meta.season (int-coerced)
    vals = [try_int(r["season"]) for r in data["season_meta"]["rows"]]
    dup = len(vals) - len(set(vals))
    rep.check(dup == 0, "season_meta.season unique",
              f"{dup} duplicates" if dup else "")

    # teams.team_id
    dup = _dup_count(data["teams"]["rows"], "team_id")
    rep.check(dup == 0, "teams.team_id unique",
              f"{dup} duplicates" if dup else "")

    # teams.normalized_name
    dup = _dup_count(data["teams"]["rows"], "normalized_name")
    rep.check(dup == 0, "teams.normalized_name unique",
              f"{dup} duplicates" if dup else "")

    # matches.match_id
    dup = _dup_count(data["matches"]["rows"], "match_id")
    rep.check(dup == 0, "matches.match_id unique",
              f"{dup} duplicates" if dup else "")

# ---------- C. Consistency ----------

def check_consistency(rep: Report, data: dict):
    rep.section("C. CONSISTENCY")

    # Build season_meta lookup
    meta_by_season = {}
    for r in data["season_meta"]["rows"]:
        s = try_int(r["season"])
        if s is not None:
            meta_by_season[s] = r

    # C1: matches.season_label == season_meta.season_label per season
    bad_label = 0
    for r in data["matches"]["rows"]:
        s = try_int(r["season"])
        if s is None or s not in meta_by_season:
            bad_label += 1
            continue
        if r["season_label"].strip() != meta_by_season[s]["season_label"].strip():
            bad_label += 1
    rep.check(bad_label == 0,
              "matches.season_label == season_meta.season_label",
              f"{bad_label} mismatches" if bad_label else "")

    # C2: matches.snapshot_date == season_meta.snapshot_date per season
    bad_snap = 0
    for r in data["matches"]["rows"]:
        s = try_int(r["season"])
        if s is None or s not in meta_by_season:
            bad_snap += 1
            continue
        expected = str(meta_by_season[s]["snapshot_date"]).strip()
        actual = str(r["snapshot_date"]).strip()
        if actual != expected:
            bad_snap += 1
    rep.check(bad_snap == 0,
              "matches.snapshot_date == season_meta.snapshot_date",
              f"{bad_snap} mismatches" if bad_snap else "")

    # C3: season_meta locked values
    locked_ok = True
    details = []
    for s, expected in LOCKED_SEASON_META.items():
        if s not in meta_by_season:
            locked_ok = False
            details.append(f"season {s} missing")
            continue
        r = meta_by_season[s]
        # season_label
        if r["season_label"].strip() != expected["label"]:
            locked_ok = False
            details.append(f"{s} label {r['season_label']!r}!={expected['label']!r}")
        # season_status
        if r["season_status"].strip() != expected["status"]:
            locked_ok = False
            details.append(f"{s} status {r['season_status']!r}!={expected['status']!r}")
        # priority
        if r["priority"].strip() != expected["priority"]:
            locked_ok = False
            details.append(f"{s} priority {r['priority']!r}!={expected['priority']!r}")
        # weight
        w = try_float(r["weight"])
        if w is None or abs(w - expected["weight"]) > 1e-9:
            locked_ok = False
            details.append(f"{s} weight {r['weight']!r}!={expected['weight']}")
        # train_eligible
        te = try_int(r["train_eligible"])
        if te != expected["train_eligible"]:
            locked_ok = False
            details.append(f"{s} train_eligible {r['train_eligible']!r}!={expected['train_eligible']}")
        # snapshot_date
        if str(r["snapshot_date"]).strip() != expected["snapshot_date"]:
            locked_ok = False
            details.append(f"{s} snapshot_date {r['snapshot_date']!r}!={expected['snapshot_date']!r}")
    rep.check(locked_ok,
              "season_meta values match LOCKED decisions",
              "; ".join(details[:5]) if details else "")

    # C4: data_status vs FT goals consistency
    bad_status = 0
    for r in data["matches"]["rows"]:
        st = r["data_status"].strip()
        fthg = try_int(r["home_goals"])
        ftag = try_int(r["away_goals"])
        hthg = try_int(r["halftime_home_goals"])
        htag = try_int(r["halftime_away_goals"])

        if st == "COMPLETE":
            if fthg is None or ftag is None or hthg is None or htag is None:
                bad_status += 1
        elif st == "INCOMPLETE_STATS":
            if fthg is None or ftag is None:
                bad_status += 1
            if hthg is not None and htag is not None:
                bad_status += 1   # HT should be missing
        elif st == "INVALID":
            bad_status += 1   # D6 target: 0 INVALID
    rep.check(bad_status == 0,
              "data_status matches FT/HT presence rules",
              f"{bad_status} mismatches" if bad_status else "")

    # C5: btts_result consistency
    bad_btts = 0
    for r in data["matches"]["rows"]:
        st = r["data_status"].strip()
        if st not in ("COMPLETE", "INCOMPLETE_STATS"):
            continue
        fthg = try_int(r["home_goals"])
        ftag = try_int(r["away_goals"])
        btts = try_int(r["btts_result"])
        if fthg is None or ftag is None or btts is None:
            bad_btts += 1
            continue
        expected = 1 if (fthg > 0 and ftag > 0) else 0
        if btts != expected:
            bad_btts += 1
    rep.check(bad_btts == 0,
              "btts_result matches (FT_home>0 & FT_away>0)",
              f"{bad_btts} mismatches" if bad_btts else "")

# ---------- D. Orphan detection ----------

def check_orphans(rep: Report, data: dict):
    rep.section("D. ORPHAN DETECTION (STRICT FAIL)")

    teams_raw = set(r["raw_name"] for r in data["teams"]["rows"])
    leagues_set = set(r["league_code"] for r in data["leagues"]["rows"])
    seasons_meta = set(try_int(r["season"]) for r in data["season_meta"]["rows"])

    # Collect observed sets from matches
    observed_teams = set()
    observed_leagues = set()
    observed_seasons = set()
    for r in data["matches"]["rows"]:
        observed_teams.add(r["home_team"])
        observed_teams.add(r["away_team"])
        observed_leagues.add(r["league_code"])
        s = try_int(r["season"])
        if s is not None:
            observed_seasons.add(s)

    # D1: teams.csv -> must appear in matches.csv
    orphan_teams = teams_raw - observed_teams
    rep.check(len(orphan_teams) == 0,
              "all teams.csv raw_name appear in matches.csv",
              f"{len(orphan_teams)} orphans: {sorted(orphan_teams)[:5]}" if orphan_teams else "")

    # D2: leagues.csv -> must appear in matches.csv
    orphan_leagues = leagues_set - observed_leagues
    rep.check(len(orphan_leagues) == 0,
              "all leagues.csv league_code appear in matches.csv",
              f"{len(orphan_leagues)} orphans: {sorted(orphan_leagues)}" if orphan_leagues else "")

    # D3: eligible seasons (2024, 2025, 2026) must appear in matches
    missing_seasons = ELIGIBLE_SEASONS - observed_seasons
    rep.check(len(missing_seasons) == 0,
              "all ELIGIBLE seasons appear in matches.csv",
              f"missing: {sorted(missing_seasons)}" if missing_seasons else "")

    # D4: 2023 must NOT appear in matches
    s2023 = observed_seasons & NON_ELIGIBLE_SEASONS
    rep.check(len(s2023) == 0,
              "season 2023 has 0 matches (backup only)",
              f"found: {sorted(s2023)}" if s2023 else "")

# ---------- E. Schema & data types ----------

def check_schema(rep: Report, data: dict):
    rep.section("E. SCHEMA & DATA TYPES")

    # E1: field names exact
    rep.check(data["leagues"]["fields"] == LEAGUES_FIELDS,
              "leagues.csv fields exact", 
              f"got {data['leagues']['fields']}" if data["leagues"]["fields"] != LEAGUES_FIELDS else "")
    rep.check(data["season_meta"]["fields"] == SEASON_META_FIELDS,
              "season_meta.csv fields exact",
              f"got {data['season_meta']['fields']}" if data["season_meta"]["fields"] != SEASON_META_FIELDS else "")
    rep.check(data["teams"]["fields"] == TEAMS_FIELDS,
              "teams.csv fields exact",
              f"got {data['teams']['fields']}" if data["teams"]["fields"] != TEAMS_FIELDS else "")
    rep.check(data["matches"]["fields"] == MATCHES_FIELDS,
              "matches.csv fields exact",
              f"got {data['matches']['fields']}" if data["matches"]["fields"] != MATCHES_FIELDS else "")

    # E2: primary keys non-empty
    def _pk_nonempty(rows, key):
        return all(str(r[key]).strip() != "" for r in rows)

    rep.check(_pk_nonempty(data["leagues"]["rows"], "league_code"),
              "leagues.league_code non-empty")
    rep.check(_pk_nonempty(data["season_meta"]["rows"], "season"),
              "season_meta.season non-empty")
    rep.check(_pk_nonempty(data["teams"]["rows"], "team_id"),
              "teams.team_id non-empty")
    rep.check(_pk_nonempty(data["matches"]["rows"], "match_id"),
              "matches.match_id non-empty")

    # E3: data_type parse

    # season int
    bad = sum(1 for r in data["season_meta"]["rows"] if try_int(r["season"]) is None)
    rep.check(bad == 0, "season_meta.season int-parseable", f"{bad} bad" if bad else "")

    # weight float
    bad = sum(1 for r in data["season_meta"]["rows"] if try_float(r["weight"]) is None)
    rep.check(bad == 0, "season_meta.weight float-parseable", f"{bad} bad" if bad else "")

    # train_eligible int
    bad = sum(1 for r in data["season_meta"]["rows"] if try_int(r["train_eligible"]) is None)
    rep.check(bad == 0, "season_meta.train_eligible int-parseable", f"{bad} bad" if bad else "")

    # leagues.tier int
    bad = sum(1 for r in data["leagues"]["rows"] if try_int(r["tier"]) is None)
    rep.check(bad == 0, "leagues.tier int-parseable", f"{bad} bad" if bad else "")

    # matches.season int, goals int (or empty for INVALID/INCOMPLETE)
    bad = 0
    for r in data["matches"]["rows"]:
        if try_int(r["season"]) is None:
            bad += 1
    rep.check(bad == 0, "matches.season int-parseable", f"{bad} bad" if bad else "")

    # goals: if non-empty, must be int-parseable
    bad = 0
    for r in data["matches"]["rows"]:
        for f in ("home_goals", "away_goals",
                  "halftime_home_goals", "halftime_away_goals",
                  "btts_result"):
            v = str(r[f]).strip()
            if v != "" and try_int(v) is None:
                bad += 1
    rep.check(bad == 0, "matches goals/btts int-parseable (or empty)", f"{bad} bad" if bad else "")

    # E4: enum validations

    # data_status
    bad = sum(1 for r in data["matches"]["rows"]
              if r["data_status"].strip() not in VALID_DATA_STATUS)
    rep.check(bad == 0, "matches.data_status in valid enum", f"{bad} bad" if bad else "")

    # teams.active_status
    bad = sum(1 for r in data["teams"]["rows"]
              if r["active_status"].strip() not in VALID_ACTIVE_STATUS)
    rep.check(bad == 0, "teams.active_status in valid enum", f"{bad} bad" if bad else "")

    # season_meta.season_status
    bad = sum(1 for r in data["season_meta"]["rows"]
              if r["season_status"].strip() not in VALID_SEASON_STATUS)
    rep.check(bad == 0, "season_meta.season_status in valid enum", f"{bad} bad" if bad else "")

    # season_meta.priority
    bad = sum(1 for r in data["season_meta"]["rows"]
              if r["priority"].strip() not in VALID_PRIORITY)
    rep.check(bad == 0, "season_meta.priority in valid enum", f"{bad} bad" if bad else "")

    # E5: ISO date fields

    # teams.first_seen_date
    bad = sum(1 for r in data["teams"]["rows"] if not is_iso_date(r["first_seen_date"]))
    rep.check(bad == 0, "teams.first_seen_date ISO valid", f"{bad} bad" if bad else "")

    # matches.date
    bad = sum(1 for r in data["matches"]["rows"] if not is_iso_date(r["date"]))
    rep.check(bad == 0, "matches.date ISO valid", f"{bad} bad" if bad else "")

    # matches.snapshot_date: empty OR ISO
    bad = 0
    for r in data["matches"]["rows"]:
        v = str(r["snapshot_date"]).strip()
        if v != "" and not is_iso_date(v):
            bad += 1
    rep.check(bad == 0, "matches.snapshot_date ISO or empty", f"{bad} bad" if bad else "")

    # season_meta.snapshot_date: empty OR ISO
    bad = 0
    for r in data["season_meta"]["rows"]:
        v = str(r["snapshot_date"]).strip()
        if v != "" and not is_iso_date(v):
            bad += 1
    rep.check(bad == 0, "season_meta.snapshot_date ISO or empty", f"{bad} bad" if bad else "")

# ---------- F. Data quality ----------

def check_data_quality(rep: Report, data: dict):
    rep.section("F. DATA QUALITY (LOCKED TARGETS)")

    n_matches = len(data["matches"]["rows"])
    n_teams   = len(data["teams"]["rows"])
    n_leagues = len(data["leagues"]["rows"])
    n_seasons = len(data["season_meta"]["rows"])

    rep.check(n_matches == TARGET_MATCHES,
              f"matches rows == {TARGET_MATCHES}", f"got {n_matches}")
    rep.check(n_teams == TARGET_TEAMS,
              f"teams rows == {TARGET_TEAMS}", f"got {n_teams}")
    rep.check(n_leagues == TARGET_LEAGUES,
              f"leagues rows == {TARGET_LEAGUES}", f"got {n_leagues}")
    rep.check(n_seasons == TARGET_SEASONS,
              f"season_meta rows == {TARGET_SEASONS}", f"got {n_seasons}")

    # Status distribution
    st = defaultdict(int)
    for r in data["matches"]["rows"]:
        st[r["data_status"].strip()] += 1

    rep.check(st["COMPLETE"] == TARGET_COMPLETE,
              f"COMPLETE count == {TARGET_COMPLETE}", f"got {st['COMPLETE']}")
    rep.check(st["INCOMPLETE_STATS"] == TARGET_INCOMPLETE,
              f"INCOMPLETE_STATS count == {TARGET_INCOMPLETE}", f"got {st['INCOMPLETE_STATS']}")
    rep.check(st["INVALID"] == TARGET_INVALID,
              f"INVALID count == {TARGET_INVALID}", f"got {st['INVALID']}")

    # League code set
    got_leagues = set(r["league_code"] for r in data["leagues"]["rows"])
    rep.check(got_leagues == EXPECTED_LEAGUES,
              f"league_code set == {sorted(EXPECTED_LEAGUES)}",
              f"got {sorted(got_leagues)}" if got_leagues != EXPECTED_LEAGUES else "")

# ---------- G. Read-only integrity ----------

def check_readonly(rep: Report, pre_sha: dict):
    rep.section("G. READ-ONLY INTEGRITY")
    files = {
        "leagues":     LEAGUES_CSV,
        "season_meta": SEASON_META_CSV,
        "teams":       TEAMS_CSV,
        "matches":     MATCHES_CSV,
    }
    for name, path in files.items():
        now = sha256_file(path)
        same = (now == pre_sha[name])
        rep.check(same, f"{name}.csv unchanged (SHA-256 pre==post)",
                  "" if same else f"pre={pre_sha[name][:16]}... post={now[:16]}...")

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 2 — D7: VALIDATE DATABASE (READ-ONLY)")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Report       : {OUT_TXT}")
    print()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Load + capture pre-hash
    print("--- LOAD (read-only) ---")
    data, pre_sha = load_all()
    for name, d in data.items():
        print(f"  {name:12s}: {len(d['rows'])} rows, "
              f"{len(d['fields'])} fields, SHA {pre_sha[name][:16]}...")
    print()

    # Run validations
    rep = Report()
    rep.section("HEADER")
    rep.line(f"Timestamp        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    rep.line(f"Phase            : Phase 2 — D7")
    rep.line(f"Mode             : READ-ONLY database validation")
    rep.line(f"Inputs           : leagues.csv, season_meta.csv, teams.csv, matches.csv")
    rep.line("")

    check_referential_integrity(rep, data)
    check_uniqueness(rep, data)
    check_consistency(rep, data)
    check_orphans(rep, data)
    check_schema(rep, data)
    check_data_quality(rep, data)
    check_readonly(rep, pre_sha)

    # Summary section
    total_checks = sum(len(lines) for _, lines in rep.sections
                       if any("[PASS]" in l or "[FAIL]" in l for l in lines))
    passes = sum(1 for _, lines in rep.sections for l in lines if "[PASS]" in l)
    fails  = sum(1 for _, lines in rep.sections for l in lines if "[FAIL]" in l)

    rep.section("SUMMARY")
    rep.line(f"Total checks     : {passes + fails}")
    rep.line(f"Passed           : {passes}")
    rep.line(f"Failed           : {fails}")
    rep.line("")
    if rep.ok():
        rep.line("STATUS: D7 PASS ✅")
        rep.line("Phase 2 database validated successfully.")
    else:
        rep.line("STATUS: D7 FAIL ❌")
        rep.line("Failures:")
        for f in rep.failures:
            rep.line(f"  - {f}")

    # Write report
    OUT_TXT.write_text(rep.render() + "\n", encoding="utf-8")
    print(f"--- REPORT ---")
    print(f"  [OK] {OUT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    # Console summary
    print("=" * 78)
    print("D7 SUMMARY")
    print("=" * 78)
    print(f"  Total checks : {passes + fails}")
    print(f"  Passed       : {passes}")
    print(f"  Failed       : {fails}")
    print()

    if rep.ok():
        print("STATUS: D7 PASS ✅")
        print("Phase 2 database validated successfully.")
        print()
        print("NEXT: Phase 2 FINAL CLOSED. Phase 3 requires explicit approval.")
        sys.exit(0)
    else:
        print("STATUS: D7 FAIL ❌")
        print("Failures:")
        for f in rep.failures:
            print(f"  - {f}")
        sys.exit(1)

if __name__ == "__main__":
    main()
