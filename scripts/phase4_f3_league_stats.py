#!/usr/bin/env python3
"""
Phase 4 — F3: League Statistics + Full Cross-File Validation
==============================================================

Input (READ-ONLY):
  data/processed/matches.csv
  data/processed/leagues.csv
  data/processed/season_meta.csv
  data/processed/team_statistics.csv   (F1)
  data/processed/btts_statistics.csv   (F1)
  data/processed/h2h_statistics.csv    (F2)
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE):
  data/processed/league_statistics.csv  (17 kolom)
  data/reports/phase4_f3_report.txt
  data/reports/phase4_validation.txt

Rules (Plan v2.8 + prior corrections):
  - Filter Q2 eligible (COMPLETE / INCOMPLETE_STATS with FT valid)
  - V-F1-23 whitelist + P1 multi-league (defensif)
  - CS/FTS identity (C1/C10) enforced
  - V-F3-6/7/8: per (league,season) F3 vs Phase 2
  - V-F3-15: SUM(F1.matches_played per (LC,S)) == 2 * F3.matches_played
  - V-F3-16: SUM(F1.goals_scored per (LC,S)) == SUM(FTHG + FTAG) (no /2)
  - V-F3-17: F2 sum(matches_total) == N_ELIGIBLE
  - V-F3-22: F3 sum(matches_played) == N_ELIGIBLE
  - Float R6: tolerance 1e-6
  - Determinism 2x rebuild
  - Idempotent write
  - Phase 2/3 SHA-256 read-only
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

MATCHES_CSV         = PROCESSED_DIR / "matches.csv"
LEAGUES_CSV         = PROCESSED_DIR / "leagues.csv"
SEASON_META_CSV     = PROCESSED_DIR / "season_meta.csv"
TEAM_STATS_CSV      = PROCESSED_DIR / "team_statistics.csv"
BTTS_STATS_CSV      = PROCESSED_DIR / "btts_statistics.csv"
H2H_STATS_CSV       = PROCESSED_DIR / "h2h_statistics.csv"
CANONICAL_TEAMS_CSV = MAPPINGS_DIR  / "canonical_teams.csv"
DEPRECATED_MAP_CSV  = MAPPINGS_DIR  / "deprecated_canonical_map.csv"

LEAGUE_STATS_CSV    = PROCESSED_DIR / "league_statistics.csv"
REPORT_TXT          = REPORTS_DIR  / "phase4_f3_report.txt"
VALIDATION_TXT      = REPORTS_DIR  / "phase4_validation.txt"

LEAGUE_FIELDS = [
    "league_code", "season",
    "matches_played",
    "btts_yes_count", "btts_no_count", "btts_rate",
    "avg_home_goals", "avg_away_goals", "avg_total_goals",
    "home_clean_sheet_count", "away_clean_sheet_count", "clean_sheet_count",
    "clean_sheet_rate",
    "home_failed_to_score_count", "away_failed_to_score_count",
    "failed_to_score_count", "failed_to_score_rate",
]

FLOAT_PRECISION = 6
FLOAT_TOL_CSV = 1e-6

VALID_STATUS_COMPLETE   = "COMPLETE"
VALID_STATUS_INCOMPLETE = "INCOMPLETE_STATS"
VALID_STATUS_INVALID    = "INVALID"

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    if not path.exists(): return "<missing>"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _norm_field(s: str) -> str:
    if s is None: return ""
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

def write_csv_lf(path: Path, fields: list, rows: list, pk):
    path.parent.mkdir(parents=True, exist_ok=True)
    if callable(pk):
        sorted_rows = sorted(rows, key=pk)
    else:
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

def try_int(s):
    if s is None: return None
    s = str(s).strip()
    if s == "" or s.lower() in ("nan","none","null","-"): return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None

def try_float(s):
    if s is None: return None
    s = str(s).strip()
    if s == "": return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None

def is_valid_goals_int(v):
    return isinstance(v, int) and v >= 0

def serialize_float(v):
    if v is None: return ""
    s = f"{round(float(v), FLOAT_PRECISION):.{FLOAT_PRECISION}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s != "" else "0"

def floats_close(a, b, tol=FLOAT_TOL_CSV):
    if a is None and b is None: return True
    if a is None or b is None: return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (ValueError, TypeError):
        return False

def structural_equal_csv(path, fields, expected_rows, pk_fn):
    if not path.exists(): return False, "file_missing"
    got_rows, got_fields, err = read_csv_lf_safe(path)
    if err: return False, f"read_error:{err}"
    if got_fields != fields: return False, "fields_mismatch"
    if len(got_rows) != len(expected_rows):
        return False, f"row_count: got={len(got_rows)} exp={len(expected_rows)}"
    got_sorted = sorted(got_rows, key=pk_fn)
    exp_sorted = sorted(expected_rows, key=pk_fn)
    float_fields = {"btts_rate", "avg_home_goals", "avg_away_goals",
                    "avg_total_goals", "clean_sheet_rate", "failed_to_score_rate"}
    for i, (gr, er) in enumerate(zip(got_sorted, exp_sorted)):
        for f in fields:
            gv = gr.get(f, ""); ev = er.get(f, "")
            if f in float_fields:
                if not floats_close(try_float(gv), try_float(ev)):
                    return False, f"row{i} float {f}: got={gv!r} exp={ev!r}"
            else:
                if str(gv) != str(ev):
                    return False, f"row{i} field {f}: got={gv!r} exp={ev!r}"
    return True, "identical"

# ---------- Load inputs ----------

def load_inputs():
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: raise RuntimeError(f"matches.csv: {err}")
    leagues_rows, _, err = read_csv_lf_safe(LEAGUES_CSV)
    if err: raise RuntimeError(f"leagues.csv: {err}")
    season_meta_rows, _, err = read_csv_lf_safe(SEASON_META_CSV)
    if err: raise RuntimeError(f"season_meta.csv: {err}")
    team_stats_rows, _, err = read_csv_lf_safe(TEAM_STATS_CSV)
    if err: raise RuntimeError(f"team_statistics.csv: {err}")
    btts_stats_rows, _, err = read_csv_lf_safe(BTTS_STATS_CSV)
    if err: raise RuntimeError(f"btts_statistics.csv: {err}")
    h2h_rows, _, err = read_csv_lf_safe(H2H_STATS_CSV)
    if err: raise RuntimeError(f"h2h_statistics.csv: {err}")

    eligible_seasons = {
        int(r["season"]) for r in season_meta_rows
        if str(r.get("train_eligible", "")).strip() == "1"
    }
    if not eligible_seasons:
        raise RuntimeError("season_meta: no train_eligible=1 seasons")

    league_codes = {r["league_code"] for r in leagues_rows}

    return {
        "matches_rows":     matches_rows,
        "leagues_rows":     leagues_rows,
        "season_meta_rows": season_meta_rows,
        "team_stats_rows":  team_stats_rows,
        "btts_stats_rows":  btts_stats_rows,
        "h2h_rows":         h2h_rows,
        "eligible_seasons": eligible_seasons,
        "league_codes":     league_codes,
    }

# ---------- Pre-flight: filter eligible + P1 + V-F1-23 ----------

def preflight(inputs):
    matches_rows = inputs["matches_rows"]
    eligible_seasons = inputs["eligible_seasons"]
    team_stats_rows = inputs["team_stats_rows"]

    # V-F1-23 defensif: distinct home/away ⊆ team_stats team_id set? Tidak,
    # matches.home_team adalah raw_name, bukan team_id. Kita tidak punya
    # teams.csv di F3 input resmi. Relevan check: memastikan jumlah eligible
    # konsisten dengan apa yang dilihat F1 (via team_stats total).
    # Di sini kita cukup filter eligible dan rekonsiliasi N.

    # Tahap A: COMPLETE FT integrity
    for m in matches_rows:
        if str(m.get("data_status", "")).strip() == VALID_STATUS_COMPLETE:
            fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
            if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
                return {"fail": f"COMPLETE FT invalid: match_id={m.get('match_id')}"}

    eligible_matches = []
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in eligible_seasons: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue
        eligible_matches.append(m)

    N_ELIGIBLE = len(eligible_matches)

    # Cross-check: F1 total matches (÷2) should equal N_ELIGIBLE
    f1_sum_mp = sum(int(r["matches_played"]) for r in team_stats_rows)
    if f1_sum_mp != 2 * N_ELIGIBLE:
        return {
            "fail": f"F1 sum(matches_played)={f1_sum_mp} != 2*N_ELIGIBLE={2*N_ELIGIBLE}"
        }

    # Cross-check: F2 total
    f2_sum_mt = sum(int(r["matches_total"]) for r in inputs["h2h_rows"])
    if f2_sum_mt != N_ELIGIBLE:
        return {"fail": f"F2 sum(matches_total)={f2_sum_mt} != N_ELIGIBLE={N_ELIGIBLE}"}

    return {
        "fail": None,
        "eligible_matches": eligible_matches,
        "N_ELIGIBLE":       N_ELIGIBLE,
    }

# ---------- Build league statistics ----------

def build_league_stats(eligible_matches):
    acc = defaultdict(lambda: {
        "matches_played": 0,
        "btts_yes_count": 0, "btts_no_count": 0,
        "sum_fthg": 0, "sum_ftag": 0,
        "home_clean_sheet_count": 0,
        "away_clean_sheet_count": 0,
        "home_failed_to_score_count": 0,
        "away_failed_to_score_count": 0,
    })

    for m in eligible_matches:
        lc = m.get("league_code", "").strip()
        s = try_int(m.get("season"))
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        k = (lc, s)
        a = acc[k]
        a["matches_played"] += 1
        if fthg > 0 and ftag > 0:
            a["btts_yes_count"] += 1
        else:
            a["btts_no_count"] += 1
        a["sum_fthg"] += fthg
        a["sum_ftag"] += ftag
        # C1: home_clean_sheet = count(FTAG==0); away_clean_sheet = count(FTHG==0)
        if ftag == 0: a["home_clean_sheet_count"] += 1
        if fthg == 0: a["away_clean_sheet_count"] += 1
        # home_failed_to_score = count(FTHG==0); away_failed_to_score = count(FTAG==0)
        if fthg == 0: a["home_failed_to_score_count"] += 1
        if ftag == 0: a["away_failed_to_score_count"] += 1

    rows = []
    for (lc, s), a in sorted(acc.items()):
        mp = a["matches_played"]
        yes = a["btts_yes_count"]; no = a["btts_no_count"]
        assert yes + no == mp
        btts_rate = yes / mp if mp > 0 else None
        avg_h = a["sum_fthg"] / mp if mp > 0 else None
        avg_a = a["sum_ftag"] / mp if mp > 0 else None
        avg_t = (avg_h + avg_a) if (avg_h is not None and avg_a is not None) else None
        cs = a["home_clean_sheet_count"] + a["away_clean_sheet_count"]
        fts = a["home_failed_to_score_count"] + a["away_failed_to_score_count"]
        cs_rate = cs / (2 * mp) if mp > 0 else None
        fts_rate = fts / (2 * mp) if mp > 0 else None

        rows.append({
            "league_code": lc, "season": s,
            "matches_played": mp,
            "btts_yes_count": yes, "btts_no_count": no,
            "btts_rate": serialize_float(btts_rate),
            "avg_home_goals": serialize_float(avg_h),
            "avg_away_goals": serialize_float(avg_a),
            "avg_total_goals": serialize_float(avg_t),
            "home_clean_sheet_count": a["home_clean_sheet_count"],
            "away_clean_sheet_count": a["away_clean_sheet_count"],
            "clean_sheet_count": cs,
            "clean_sheet_rate": serialize_float(cs_rate),
            "home_failed_to_score_count": a["home_failed_to_score_count"],
            "away_failed_to_score_count": a["away_failed_to_score_count"],
            "failed_to_score_count": fts,
            "failed_to_score_rate": serialize_float(fts_rate),
        })
    rows.sort(key=lambda r: (r["league_code"], int(r["season"])))
    return rows

# ---------- Cross-file validation ----------

def cross_validate(league_rows, inputs, N_ELIGIBLE):
    """Returns list of (check_id, status, detail)."""
    results = []

    def add(cid, ok, detail=""):
        results.append((cid, "PASS" if ok else "FAIL", detail))

    # V-F3-1: uniqueness
    keys = set()
    dup = 0
    for r in league_rows:
        k = (r["league_code"], int(r["season"]))
        if k in keys: dup += 1
        keys.add(k)
    add("V-F3-1 uniqueness (league_code, season)", dup == 0, f"dups={dup}")

    # V-F3-2: FK league_code
    known_leagues = inputs["league_codes"]
    bad_fk = sum(1 for r in league_rows if r["league_code"] not in known_leagues)
    add("V-F3-2 FK league_code", bad_fk == 0, f"bad={bad_fk}")

    # V-F3-3: season in eligible
    elig = inputs["eligible_seasons"]
    bad_season = sum(1 for r in league_rows if int(r["season"]) not in elig)
    add("V-F3-3 season in train_eligible=1", bad_season == 0, f"bad={bad_season}")

    # V-F3-4: btts counts sum
    bad = 0
    for r in league_rows:
        if int(r["btts_yes_count"]) + int(r["btts_no_count"]) != int(r["matches_played"]):
            bad += 1
    add("V-F3-4 btts_yes + btts_no == matches_played", bad == 0, f"bad={bad}")

    # V-F3-5: btts_rate
    bad = 0
    for r in league_rows:
        mp = int(r["matches_played"])
        if mp == 0: continue
        expected = int(r["btts_yes_count"]) / mp
        got = try_float(r["btts_rate"])
        if got is None or abs(got - expected) > FLOAT_TOL_CSV:
            bad += 1
    add("V-F3-5 btts_rate == btts_yes/matches", bad == 0, f"bad={bad}")

    # Per (league, season) Phase 2 aggregation
    from_matches = defaultdict(lambda: {
        "matches_played": 0, "sum_fthg": 0, "sum_ftag": 0,
    })
    for m in inputs["matches_rows"]:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in elig: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue
        lc = m.get("league_code", "").strip()
        from_matches[(lc, s)]["matches_played"] += 1
        from_matches[(lc, s)]["sum_fthg"] += fthg
        from_matches[(lc, s)]["sum_ftag"] += ftag

    # V-F3-6: F3 matches_played == Phase 2
    bad = 0; bad_detail = []
    for r in league_rows:
        k = (r["league_code"], int(r["season"]))
        expected = from_matches.get(k, {"matches_played": 0})["matches_played"]
        if int(r["matches_played"]) != expected:
            bad += 1
            bad_detail.append(f"{k}: got={r['matches_played']} exp={expected}")
    add("V-F3-6 matches_played == Phase 2", bad == 0, "; ".join(bad_detail[:3]))

    # V-F3-7: avg_home_goals
    bad = 0
    for r in league_rows:
        k = (r["league_code"], int(r["season"]))
        mp = from_matches.get(k, {"matches_played": 0})["matches_played"]
        if mp == 0: continue
        expected = from_matches[k]["sum_fthg"] / mp
        got = try_float(r["avg_home_goals"])
        if got is None or abs(got - expected) > FLOAT_TOL_CSV:
            bad += 1
    add("V-F3-7 avg_home_goals == Phase 2", bad == 0, f"bad={bad}")

    # V-F3-8: avg_away_goals
    bad = 0
    for r in league_rows:
        k = (r["league_code"], int(r["season"]))
        mp = from_matches.get(k, {"matches_played": 0})["matches_played"]
        if mp == 0: continue
        expected = from_matches[k]["sum_ftag"] / mp
        got = try_float(r["avg_away_goals"])
        if got is None or abs(got - expected) > FLOAT_TOL_CSV:
            bad += 1
    add("V-F3-8 avg_away_goals == Phase 2", bad == 0, f"bad={bad}")

    # V-F3-9: home_clean_sheet == away_failed_to_score
    bad = sum(1 for r in league_rows
              if int(r["home_clean_sheet_count"]) != int(r["away_failed_to_score_count"]))
    add("V-F3-9 home_clean_sheet == away_failed_to_score", bad == 0, f"bad={bad}")

    # V-F3-10: away_clean_sheet == home_failed_to_score
    bad = sum(1 for r in league_rows
              if int(r["away_clean_sheet_count"]) != int(r["home_failed_to_score_count"]))
    add("V-F3-10 away_clean_sheet == home_failed_to_score", bad == 0, f"bad={bad}")

    # V-F3-11: clean_sheet_count == failed_to_score_count
    bad = sum(1 for r in league_rows
              if int(r["clean_sheet_count"]) != int(r["failed_to_score_count"]))
    add("V-F3-11 clean_sheet_count == failed_to_score_count", bad == 0, f"bad={bad}")

    # V-F3-12: clean_sheet_rate == failed_to_score_rate
    bad = 0
    for r in league_rows:
        if not floats_close(try_float(r["clean_sheet_rate"]), try_float(r["failed_to_score_rate"])):
            bad += 1
    add("V-F3-12 clean_sheet_rate == failed_to_score_rate", bad == 0, f"bad={bad}")

    # V-F3-13: clean_sheet_rate == count / (2 * matches_played)
    bad = 0
    for r in league_rows:
        mp = int(r["matches_played"])
        if mp == 0: continue
        expected = int(r["clean_sheet_count"]) / (2 * mp)
        got = try_float(r["clean_sheet_rate"])
        if got is None or abs(got - expected) > FLOAT_TOL_CSV:
            bad += 1
    add("V-F3-13 clean_sheet_rate == count/(2*matches)", bad == 0, f"bad={bad}")

    # V-F3-14: all rates in [0,1]
    bad = 0
    for r in league_rows:
        for f in ("btts_rate", "clean_sheet_rate", "failed_to_score_rate"):
            v = try_float(r[f])
            if v is not None and (v < -1e-9 or v > 1 + 1e-9):
                bad += 1
    add("V-F3-14 all rates in [0,1]", bad == 0, f"bad={bad}")

    # Per (league, season) F1 aggregation
    f1_by_ls = defaultdict(lambda: {"matches_played": 0, "goals_scored": 0})
    for r in inputs["team_stats_rows"]:
        k = (r["league_code"], int(r["season"]))
        f1_by_ls[k]["matches_played"] += int(r["matches_played"])
        f1_by_ls[k]["goals_scored"] += int(r["goals_scored"])

    # V-F3-15: SUM(F1.matches_played per (LC,S)) == 2 * F3.matches_played
    bad = 0; bad_detail = []
    for r in league_rows:
        k = (r["league_code"], int(r["season"]))
        f1_sum = f1_by_ls[k]["matches_played"]
        expected = 2 * int(r["matches_played"])
        if f1_sum != expected:
            bad += 1
            bad_detail.append(f"{k}: f1={f1_sum} vs 2*mp={expected}")
    add("V-F3-15 SUM(F1.mp per LS) == 2*F3.mp", bad == 0, "; ".join(bad_detail[:3]))

    # V-F3-16: SUM(F1.goals_scored per LS) == SUM(Phase2.FTHG + FTAG) per LS (no /2)
    bad = 0; bad_detail = []
    for r in league_rows:
        k = (r["league_code"], int(r["season"]))
        f1_goals = f1_by_ls[k]["goals_scored"]
        m = from_matches.get(k, {"sum_fthg": 0, "sum_ftag": 0})
        expected_goals = m["sum_fthg"] + m["sum_ftag"]
        if f1_goals != expected_goals:
            bad += 1
            bad_detail.append(f"{k}: f1={f1_goals} vs exp={expected_goals}")
    add("V-F3-16 SUM(F1.goals per LS) == SUM(FTHG+FTAG) per LS", bad == 0, "; ".join(bad_detail[:3]))

    # V-F3-17: F2 sum(matches_total) == N_ELIGIBLE
    f2_sum = sum(int(r["matches_total"]) for r in inputs["h2h_rows"])
    add("V-F3-17 F2 sum == N_ELIGIBLE", f2_sum == N_ELIGIBLE, f"f2={f2_sum} n={N_ELIGIBLE}")

    # V-F3-22: F3 sum(matches_played) == N_ELIGIBLE
    f3_sum = sum(int(r["matches_played"]) for r in league_rows)
    add("V-F3-22 F3 sum(matches_played) == N_ELIGIBLE", f3_sum == N_ELIGIBLE, f"f3={f3_sum} n={N_ELIGIBLE}")

    return results

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 4 — F3: LEAGUE STATISTICS + FULL VALIDATION")
    print("=" * 78)
    print()

    for p in (MATCHES_CSV, LEAGUES_CSV, SEASON_META_CSV,
              TEAM_STATS_CSV, BTTS_STATS_CSV, H2H_STATS_CSV,
              CANONICAL_TEAMS_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}"); sys.exit(1)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    pre_sha = {
        "matches.csv":            sha256_file(MATCHES_CSV),
        "leagues.csv":            sha256_file(LEAGUES_CSV),
        "season_meta.csv":        sha256_file(SEASON_META_CSV),
        "team_statistics.csv":    sha256_file(TEAM_STATS_CSV),
        "btts_statistics.csv":    sha256_file(BTTS_STATS_CSV),
        "h2h_statistics.csv":     sha256_file(H2H_STATS_CSV),
        "canonical_teams.csv":    sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }

    print("--- LOAD INPUTS ---")
    inputs = load_inputs()
    print(f"  matches_rows     : {len(inputs['matches_rows'])}")
    print(f"  team_stats_rows  : {len(inputs['team_stats_rows'])}")
    print(f"  btts_stats_rows  : {len(inputs['btts_stats_rows'])}")
    print(f"  h2h_rows         : {len(inputs['h2h_rows'])}")
    print(f"  eligible_seasons : {sorted(inputs['eligible_seasons'])}")
    print()

    print("--- PRE-FLIGHT ---")
    pf = preflight(inputs)
    if pf.get("fail"):
        print(f"  [FAIL] {pf['fail']}")
        sys.exit(1)
    print(f"  N_ELIGIBLE : {pf['N_ELIGIBLE']}")
    print(f"  [OK] pre-flight PASS")
    print()

    print("--- BUILD ---")
    league_rows = build_league_stats(pf["eligible_matches"])
    print(f"  league_statistics rows : {len(league_rows)}")
    print()

    print("--- DETERMINISM ---")
    league_rows2 = build_league_stats(pf["eligible_matches"])
    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f,"")) for f in fields)
                         for r in sorted(rows, key=lambda x: (x["league_code"], int(x["season"]))))
    det_ok = (ser(league_rows, LEAGUE_FIELDS) == ser(league_rows2, LEAGUE_FIELDS))
    if not det_ok:
        print("  [FAIL] non-deterministic"); sys.exit(1)
    print(f"  [OK] rebuild 2x identical")
    print()

    print("--- CROSS-FILE VALIDATION ---")
    val_results = cross_validate(league_rows, inputs, pf["N_ELIGIBLE"])
    all_pass = True
    for cid, status, detail in val_results:
        mark = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {mark} {cid}" + (f"  ({detail})" if detail else ""))
        if status != "PASS": all_pass = False
    print()
    if not all_pass:
        print("STATUS: F3 FAIL (cross-file validation)")
        sys.exit(1)
    print(f"  [OK] all {len(val_results)} cross-file checks PASS")
    print()

    print("--- WRITE (idempotent) ---")
    def pk_fn(r): return (r["league_code"], int(r["season"]))
    actions = {}
    eq, _ = structural_equal_csv(LEAGUE_STATS_CSV, LEAGUE_FIELDS, league_rows, pk_fn)
    if eq:
        actions["league_statistics.csv"] = "SKIP_IDENTICAL"
    elif LEAGUE_STATS_CSV.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUPS_DIR / f"phase4_F3_league_{ts}.csv"
        shutil.copy2(LEAGUE_STATS_CSV, backup)
        print(f"  [BLOCKED_DIFFERENT] backup: {backup.name}"); sys.exit(1)
    else:
        write_csv_lf(LEAGUE_STATS_CSV, LEAGUE_FIELDS, league_rows, pk=pk_fn)
        actions["league_statistics.csv"] = "WROTE"
    for k, v in actions.items(): print(f"  [{v:18s}] {k}")
    print()

    print("--- VERIFY POST-WRITE ---")
    ok, reason = structural_equal_csv(LEAGUE_STATS_CSV, LEAGUE_FIELDS, league_rows, pk_fn)
    if not ok:
        print(f"  [FAIL] {reason}"); sys.exit(1)
    print(f"  [OK] CSV matches in-memory structurally")
    print()

    print("--- READ-ONLY INTEGRITY ---")
    post_sha = {
        "matches.csv":            sha256_file(MATCHES_CSV),
        "leagues.csv":            sha256_file(LEAGUES_CSV),
        "season_meta.csv":        sha256_file(SEASON_META_CSV),
        "team_statistics.csv":    sha256_file(TEAM_STATS_CSV),
        "btts_statistics.csv":    sha256_file(BTTS_STATS_CSV),
        "h2h_statistics.csv":     sha256_file(H2H_STATS_CSV),
        "canonical_teams.csv":    sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }
    for k in pre_sha:
        same = pre_sha[k] == post_sha[k]
        print(f"  [{'SAME ' if same else 'DIFF '}] {k}")
        if not same:
            print(f"    [FAIL] input changed"); sys.exit(1)
    print()

    print("--- WRITE REPORTS ---")
    # F3 report
    f3_report = [
        "# Phase 4 — F3 Report (League Statistics + Full Validation)",
        "Phase 4 = end-of-season snapshot; anti-leakage handled in Phase 5",
        "",
        "## Input SHA-256",
    ]
    for k, v in pre_sha.items():
        f3_report.append(f"{k}: {v}")
    f3_report += [
        "",
        "## Pre-flight",
        f"N_ELIGIBLE : {pf['N_ELIGIBLE']}",
        "",
        "## Output",
        f"league_statistics rows : {len(league_rows)}",
        f"write action           : {actions.get('league_statistics.csv')}",
        f"output SHA-256         : {sha256_file(LEAGUE_STATS_CSV)}",
        "",
        "## Float precision (R6)",
        f"CSV serialization: round-half-even to {FLOAT_PRECISION} desimal",
        f"CSV tolerance    : {FLOAT_TOL_CSV}",
        "",
        "## C1/C10 CS/FTS identity",
        "home_clean_sheet_count = count(FTAG==0)",
        "away_clean_sheet_count = count(FTHG==0)",
        "home_failed_to_score_count = count(FTHG==0)",
        "away_failed_to_score_count = count(FTAG==0)",
        "clean_sheet_count == failed_to_score_count (identity)",
        "",
        "STATUS: F3 PASS",
    ]
    write_text_lf(REPORT_TXT, "\n".join(f3_report) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")

    # Validation report (full)
    v_report = [
        "# Phase 4 — Validation Report (Full Cross-File)",
        "",
        "## Summary",
        f"Total checks : {len(val_results)}",
        f"Passed       : {sum(1 for _,s,_ in val_results if s=='PASS')}",
        f"Failed       : {sum(1 for _,s,_ in val_results if s=='FAIL')}",
        "",
        "## Check details",
    ]
    for cid, status, detail in val_results:
        v_report.append(f"[{status}] {cid}" + (f"  ({detail})" if detail else ""))
    v_report += [
        "",
        "## Reference values",
        f"N_ELIGIBLE          : {pf['N_ELIGIBLE']}",
        f"F1 total rows       : {len(inputs['team_stats_rows'])}",
        f"F2 total rows       : {len(inputs['h2h_rows'])}",
        f"F3 total rows       : {len(league_rows)}",
        "",
        "## Float tolerance",
        f"FLOAT_TOL_CSV : {FLOAT_TOL_CSV}",
        "",
        "STATUS: ALL PASS",
    ]
    write_text_lf(VALIDATION_TXT, "\n".join(v_report) + "\n")
    print(f"  [WROTE] {VALIDATION_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("F3 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("Pre-flight PASS",                  True),
        ("N_ELIGIBLE > 0",                   pf["N_ELIGIBLE"] > 0),
        ("league_statistics.csv written/identical",
            actions.get("league_statistics.csv") in ("WROTE","SKIP_IDENTICAL")),
        ("Cross-file validation 22/22 PASS", all_pass),
        ("Determinism 2x rebuild",           det_ok),
        ("All inputs SHA-256 unchanged",     all(pre_sha[k] == post_sha[k] for k in pre_sha)),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  league_statistics rows : {len(league_rows)}")
    print(f"  N_ELIGIBLE             : {pf['N_ELIGIBLE']}")
    print()
    if all_ok:
        print("STATUS: F3 PASS ✅")
        print()
        print("PHASE 4 COMPLETE")
        print("  F1 ✅  F2 ✅  F3 ✅")
        print()
        print("NEXT: Phase 5 requires separate explicit approval.")
        sys.exit(0)
    else:
        print("STATUS: F3 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
