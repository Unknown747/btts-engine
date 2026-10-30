#!/usr/bin/env python3
"""
Phase 5 — G4: Anti-Leakage Full Audit (STRICT)
===============================================

Input (READ-ONLY):
  data/features/match_features.csv    (G3, 48 cols)
  data/processed/matches.csv
  data/processed/teams.csv
  data/processed/teams_canonical.csv
  data/processed/season_meta.csv
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE):
  data/reports/phase5_g4_report.txt
  data/reports/phase5_g4_audit.csv    (4,608 rows, per-match audit trail)

Proses:
  Untuk SETIAP match X di match_features.csv:
    - Recompute SEMUA rolling features dari raw matches.csv
      dengan strict filter date < X.date (INDEPENDENT path)
    - Bandingkan dengan CSV row X
    - FAIL exit 1 jika ada mismatch di kolom manapun

Strict audit:
  - 100% match (bukan sample)
  - Independent recompute (tidak share state dengan G1/G2/G3)
  - Fail-fast: 1 mismatch -> exit 1
"""
import sys
import csv
import io
import hashlib
from pathlib import Path
from datetime import datetime, date as dt_date
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
FEATURES_DIR  = DATA_DIR / "features"
REPORTS_DIR   = DATA_DIR / "reports"

MATCH_FEATURES_CSV   = FEATURES_DIR / "match_features.csv"
MATCHES_CSV          = PROCESSED_DIR / "matches.csv"
TEAMS_CSV            = PROCESSED_DIR / "teams.csv"
TEAMS_CANONICAL_CSV  = PROCESSED_DIR / "teams_canonical.csv"
SEASON_META_CSV      = PROCESSED_DIR / "season_meta.csv"
CANONICAL_TEAMS_CSV  = MAPPINGS_DIR  / "canonical_teams.csv"
DEPRECATED_MAP_CSV   = MAPPINGS_DIR  / "deprecated_canonical_map.csv"

REPORT_TXT = REPORTS_DIR / "phase5_g4_report.txt"
AUDIT_CSV  = REPORTS_DIR / "phase5_g4_audit.csv"

AUDIT_FIELDS = [
    "match_id", "date", "season", "league_code",
    "status", "mismatch_fields", "n_fields_checked",
]

# Fields audited (non-identity, non-target)
AUDIT_FIELDS_CHECK = [
    # Home rolling (11)
    "home_matches_played_before",
    "home_goals_scored_avg_before",
    "home_goals_conceded_avg_before",
    "home_btts_rate_before",
    "home_scoring_rate_before",
    "home_conceding_rate_before",
    "home_clean_sheet_rate_before",
    "home_failed_to_score_rate_before",
    "home_btts_last_5_before",
    "home_btts_last_10_before",
    "home_btts_last_15_before",
    # Away rolling (11)
    "away_matches_played_before",
    "away_goals_scored_avg_before",
    "away_goals_conceded_avg_before",
    "away_btts_rate_before",
    "away_scoring_rate_before",
    "away_conceding_rate_before",
    "away_clean_sheet_rate_before",
    "away_failed_to_score_rate_before",
    "away_btts_last_5_before",
    "away_btts_last_10_before",
    "away_btts_last_15_before",
    # H2H (5)
    "h2h_matches_before",
    "h2h_btts_rate_before",
    "h2h_avg_home_goals_before",
    "h2h_avg_away_goals_before",
    "h2h_last_meeting_days_before",
    # League (4)
    "league_btts_rate_before",
    "league_avg_home_goals_before",
    "league_avg_away_goals_before",
    "league_matches_played_before",
    # Split (4)
    "home_home_scored_avg_before",
    "home_home_conceded_avg_before",
    "away_away_scored_avg_before",
    "away_away_conceded_avg_before",
    # Strength (4)
    "home_attack_strength",
    "home_defense_strength",
    "away_attack_strength",
    "away_defense_strength",
]

FLOAT_PRECISION = 6
FLOAT_TOL = 1e-6

RECENT_WINDOWS = [5, 10, 15]

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

def floats_close(a, b, tol=FLOAT_TOL):
    if a is None and b is None: return True
    if a is None or b is None: return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (ValueError, TypeError):
        return False

def ints_equal(a, b):
    """Compare as int, treating empty as None."""
    ai = try_int(a); bi = try_int(b)
    if ai is None and bi is None: return True
    if ai is None or bi is None: return False
    return ai == bi

def days_between(iso_a, iso_b):
    try:
        da = dt_date.fromisoformat(iso_a)
        db = dt_date.fromisoformat(iso_b)
        return (db - da).days
    except (ValueError, TypeError):
        return None

def pair_key_for(cid_a, cid_b):
    a, b = sorted([cid_a, cid_b])
    return "md5:" + hashlib.md5(f"{a}|{b}".encode("utf-8")).hexdigest()

# ---------- Load inputs ----------

def load_inputs():
    teams_rows, _, err = read_csv_lf_safe(TEAMS_CSV)
    if err: raise RuntimeError(f"teams.csv: {err}")
    season_meta_rows, _, err = read_csv_lf_safe(SEASON_META_CSV)
    if err: raise RuntimeError(f"season_meta.csv: {err}")
    teams_canon_rows, _, err = read_csv_lf_safe(TEAMS_CANONICAL_CSV)
    if err: raise RuntimeError(f"teams_canonical.csv: {err}")
    canonical_rows, _, err = read_csv_lf_safe(CANONICAL_TEAMS_CSV)
    if err: raise RuntimeError(f"canonical_teams.csv: {err}")
    deprecated_rows, _, err = read_csv_lf_safe(DEPRECATED_MAP_CSV)
    if err and "missing" in str(err):
        deprecated_rows = []
    elif err:
        raise RuntimeError(f"deprecated_canonical_map.csv: {err}")

    eligible_seasons = {
        int(r["season"]) for r in season_meta_rows
        if str(r.get("train_eligible", "")).strip() == "1"
    }
    team_by_raw_name = {r["raw_name"]: r["team_id"] for r in teams_rows}
    tc_by_team = {r["team_id"]: r for r in teams_canon_rows}
    canonical_status = {r["canonical_id"]: r.get("status", "ACTIVE") for r in canonical_rows}
    dep_redirect = {r["deprecated_canonical_id"]: r["superseded_by"] for r in deprecated_rows}

    return {
        "teams_rows":       teams_rows,
        "season_meta_rows": season_meta_rows,
        "teams_canon_rows": teams_canon_rows,
        "canonical_rows":   canonical_rows,
        "deprecated_rows":  deprecated_rows,
        "eligible_seasons": eligible_seasons,
        "team_by_raw_name": team_by_raw_name,
        "tc_by_team":       tc_by_team,
        "canonical_status": canonical_status,
        "dep_redirect":     dep_redirect,
    }

def resolve_active(cid, canonical_status, dep_redirect, max_depth=32):
    seen = set(); cur = cid
    for _ in range(max_depth):
        if cur in seen: return None
        seen.add(cur)
        s = canonical_status.get(cur)
        if s == "ACTIVE": return cur
        if s != "DEPRECATED": return None
        nxt = dep_redirect.get(cur)
        if nxt is None: return None
        cur = nxt
    return None

# ---------- Build ALL indexes fresh from raw matches ----------

def build_all_indexes(eligible_matches, inputs):
    """
    Independent build from raw matches (not from G1/G2/G3 state).
    Builds 4 indexes:
      - team_home_index[team_id] = sorted list (home appearances)
      - team_away_index[team_id] = sorted list (away appearances)
      - h2h_index[pair_key] = sorted list
      - league_index[(league_code, season)] = sorted list
    """
    team_by_raw_name = inputs["team_by_raw_name"]
    tc_by_team = inputs["tc_by_team"]
    canonical_status = inputs["canonical_status"]
    dep_redirect = inputs["dep_redirect"]

    team_home = defaultdict(list)   # all appearances (home or away) for home rolling
    team_all = defaultdict(list)    # all appearances (home or away) for basic rolling
    team_home_only = defaultdict(list)  # only when home (for home/away split)
    team_away_only = defaultdict(list)  # only when away (for home/away split)
    h2h_index = defaultdict(list)
    league_index = defaultdict(list)

    for m in eligible_matches:
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name.get(h_raw); a_id = team_by_raw_name.get(a_raw)
        if h_id is None or a_id is None:
            raise RuntimeError(f"unknown team: {h_raw}/{a_raw}")
        h_tc = tc_by_team.get(h_id); a_tc = tc_by_team.get(a_id)
        if not h_tc or not a_tc:
            raise RuntimeError(f"teams_canonical missing for {h_id}/{a_id}")
        h_cid = resolve_active(h_tc["canonical_id"], canonical_status, dep_redirect)
        a_cid = resolve_active(a_tc["canonical_id"], canonical_status, dep_redirect)
        if not h_cid or not a_cid:
            raise RuntimeError(f"cannot resolve canonical for {h_raw}/{a_raw}")

        date = m.get("date", "").strip()
        mid = m.get("match_id", "").strip()
        lc = m.get("league_code", "").strip()
        s = try_int(m.get("season"))
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0

        # Team appearance — home perspective (for home rolling: total appearances)
        team_all[h_id].append({
            "date": date, "match_id": mid,
            "gs": fthg, "gc": ftag, "btts": btts,
            "cs": 1 if ftag == 0 else 0, "fts": 1 if fthg == 0 else 0,
        })
        team_home_only[h_id].append({
            "date": date, "match_id": mid, "gs": fthg, "gc": ftag,
        })
        # Team appearance — away perspective
        team_all[a_id].append({
            "date": date, "match_id": mid,
            "gs": ftag, "gc": fthg, "btts": btts,
            "cs": 1 if fthg == 0 else 0, "fts": 1 if ftag == 0 else 0,
        })
        team_away_only[a_id].append({
            "date": date, "match_id": mid, "gs": ftag, "gc": fthg,
        })

        # H2H
        if h_cid != a_cid:
            pk = pair_key_for(h_cid, a_cid)
            h2h_index[pk].append({
                "date": date, "match_id": mid,
                "fthg": fthg, "ftag": ftag, "btts": btts,
            })

        # League
        if s is not None:
            league_index[(lc, s)].append({
                "date": date, "match_id": mid,
                "fthg": fthg, "ftag": ftag, "btts": btts,
            })

    for k in team_all: team_all[k].sort(key=lambda r: (r["date"], r["match_id"]))
    for k in team_home_only: team_home_only[k].sort(key=lambda r: (r["date"], r["match_id"]))
    for k in team_away_only: team_away_only[k].sort(key=lambda r: (r["date"], r["match_id"]))
    for k in h2h_index: h2h_index[k].sort(key=lambda r: (r["date"], r["match_id"]))
    for k in league_index: league_index[k].sort(key=lambda r: (r["date"], r["match_id"]))

    return {
        "team_all":      team_all,
        "team_home_only": team_home_only,
        "team_away_only": team_away_only,
        "h2h":           h2h_index,
        "league":        league_index,
    }

# ---------- Recompute rolling for one match ----------

def slice_before(sorted_list, cutoff_date):
    lo, hi = 0, len(sorted_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_list[mid]["date"] < cutoff_date:
            lo = mid + 1
        else:
            hi = mid
    return sorted_list[:lo]

def recompute_rolling(apps, cutoff_date):
    """Team rolling from all appearances with date < cutoff."""
    selected = slice_before(apps, cutoff_date)
    n = len(selected)
    if n == 0:
        return {
            "matches": 0,
            "gs_avg": None, "gc_avg": None,
            "btts_rate": None,
            "scoring_rate": None, "conceding_rate": None,
            "cs_rate": None, "fts_rate": None,
            "last_5": None, "last_10": None, "last_15": None,
        }
    sum_gs = sum(r["gs"] for r in selected)
    sum_gc = sum(r["gc"] for r in selected)
    sum_btts = sum(r["btts"] for r in selected)
    sum_cs = sum(r["cs"] for r in selected)
    sum_fts = sum(r["fts"] for r in selected)
    recent = {}
    for w in RECENT_WINDOWS:
        if n >= w:
            recent[w] = sum(r["btts"] for r in selected[-w:]) / w
        else:
            recent[w] = None
    return {
        "matches": n,
        "gs_avg": sum_gs / n, "gc_avg": sum_gc / n,
        "btts_rate": sum_btts / n,
        "scoring_rate": (n - sum_fts) / n,
        "conceding_rate": (n - sum_cs) / n,
        "cs_rate": sum_cs / n, "fts_rate": sum_fts / n,
        "last_5": recent[5], "last_10": recent[10], "last_15": recent[15],
    }

def recompute_venue_split(apps, cutoff_date):
    selected = slice_before(apps, cutoff_date)
    n = len(selected)
    if n == 0:
        return {"matches": 0, "scored_avg": None, "conceded_avg": None}
    sum_gs = sum(r["gs"] for r in selected)
    sum_gc = sum(r["gc"] for r in selected)
    return {"matches": n, "scored_avg": sum_gs / n, "conceded_avg": sum_gc / n}

def recompute_h2h(apps, cutoff_date):
    selected = slice_before(apps, cutoff_date)
    n = len(selected)
    if n == 0:
        return {"matches": 0, "btts_rate": None, "avg_h": None, "avg_a": None, "last_days": None}
    sum_btts = sum(r["btts"] for r in selected)
    sum_fthg = sum(r["fthg"] for r in selected)
    sum_ftag = sum(r["ftag"] for r in selected)
    last_date = selected[-1]["date"]
    return {
        "matches": n,
        "btts_rate": sum_btts / n,
        "avg_h": sum_fthg / n,
        "avg_a": sum_ftag / n,
        "last_days": days_between(last_date, cutoff_date),
    }

def recompute_league(apps, cutoff_date):
    selected = slice_before(apps, cutoff_date)
    n = len(selected)
    if n == 0:
        return {"matches": 0, "btts_rate": None, "avg_h": None, "avg_a": None}
    sum_btts = sum(r["btts"] for r in selected)
    sum_fthg = sum(r["fthg"] for r in selected)
    sum_ftag = sum(r["ftag"] for r in selected)
    return {
        "matches": n,
        "btts_rate": sum_btts / n,
        "avg_h": sum_fthg / n,
        "avg_a": sum_ftag / n,
    }

def compute_strength(num, den):
    if num is None or den is None: return None
    try:
        n = float(num); d = float(den)
    except (ValueError, TypeError):
        return None
    if d == 0: return None
    return n / d

# ---------- Audit one match ----------

def audit_match(feat_row, indexes, inputs):
    """
    Recompute all 40 fields for match X from raw indexes.
    Return (n_mismatched, list_of_mismatched_fields).
    """
    team_by_raw_name = inputs["team_by_raw_name"]
    h_raw = feat_row["home_team"].strip()
    a_raw = feat_row["away_team"].strip()
    h_id = team_by_raw_name.get(h_raw)
    a_id = team_by_raw_name.get(a_raw)
    date = feat_row["date"].strip()
    lc = feat_row["league_code"].strip()
    s = try_int(feat_row["season"])
    h_cid = feat_row["home_canonical_id"].strip()
    a_cid = feat_row["away_canonical_id"].strip()

    # Recompute
    h_roll = recompute_rolling(indexes["team_all"].get(h_id, []), date)
    a_roll = recompute_rolling(indexes["team_all"].get(a_id, []), date)
    h_split = recompute_venue_split(indexes["team_home_only"].get(h_id, []), date)
    a_split = recompute_venue_split(indexes["team_away_only"].get(a_id, []), date)
    pk = pair_key_for(h_cid, a_cid)
    h2h = recompute_h2h(indexes["h2h"].get(pk, []), date)
    lg = recompute_league(indexes["league"].get((lc, s), []), date)

    # Expected values (serialized)
    expected = {
        "home_matches_played_before": h_roll["matches"],
        "home_goals_scored_avg_before": serialize_float(h_roll["gs_avg"]),
        "home_goals_conceded_avg_before": serialize_float(h_roll["gc_avg"]),
        "home_btts_rate_before": serialize_float(h_roll["btts_rate"]),
        "home_scoring_rate_before": serialize_float(h_roll["scoring_rate"]),
        "home_conceding_rate_before": serialize_float(h_roll["conceding_rate"]),
        "home_clean_sheet_rate_before": serialize_float(h_roll["cs_rate"]),
        "home_failed_to_score_rate_before": serialize_float(h_roll["fts_rate"]),
        "home_btts_last_5_before": serialize_float(h_roll["last_5"]),
        "home_btts_last_10_before": serialize_float(h_roll["last_10"]),
        "home_btts_last_15_before": serialize_float(h_roll["last_15"]),
        "away_matches_played_before": a_roll["matches"],
        "away_goals_scored_avg_before": serialize_float(a_roll["gs_avg"]),
        "away_goals_conceded_avg_before": serialize_float(a_roll["gc_avg"]),
        "away_btts_rate_before": serialize_float(a_roll["btts_rate"]),
        "away_scoring_rate_before": serialize_float(a_roll["scoring_rate"]),
        "away_conceding_rate_before": serialize_float(a_roll["conceding_rate"]),
        "away_clean_sheet_rate_before": serialize_float(a_roll["cs_rate"]),
        "away_failed_to_score_rate_before": serialize_float(a_roll["fts_rate"]),
        "away_btts_last_5_before": serialize_float(a_roll["last_5"]),
        "away_btts_last_10_before": serialize_float(a_roll["last_10"]),
        "away_btts_last_15_before": serialize_float(a_roll["last_15"]),
        "h2h_matches_before": h2h["matches"],
        "h2h_btts_rate_before": serialize_float(h2h["btts_rate"]),
        "h2h_avg_home_goals_before": serialize_float(h2h["avg_h"]),
        "h2h_avg_away_goals_before": serialize_float(h2h["avg_a"]),
        "h2h_last_meeting_days_before": "" if h2h["last_days"] is None else h2h["last_days"],
        "league_btts_rate_before": serialize_float(lg["btts_rate"]),
        "league_avg_home_goals_before": serialize_float(lg["avg_h"]),
        "league_avg_away_goals_before": serialize_float(lg["avg_a"]),
        "league_matches_played_before": lg["matches"],
        "home_home_scored_avg_before": serialize_float(h_split["scored_avg"]),
        "home_home_conceded_avg_before": serialize_float(h_split["conceded_avg"]),
        "away_away_scored_avg_before": serialize_float(a_split["scored_avg"]),
        "away_away_conceded_avg_before": serialize_float(a_split["conceded_avg"]),
    }
    # Strength derived from expected (using recomputed rolling/league)
    expected["home_attack_strength"] = serialize_float(
        compute_strength(try_float(expected["home_goals_scored_avg_before"]),
                         try_float(expected["league_avg_home_goals_before"])))
    expected["home_defense_strength"] = serialize_float(
        compute_strength(try_float(expected["home_goals_conceded_avg_before"]),
                         try_float(expected["league_avg_away_goals_before"])))
    expected["away_attack_strength"] = serialize_float(
        compute_strength(try_float(expected["away_goals_scored_avg_before"]),
                         try_float(expected["league_avg_away_goals_before"])))
    expected["away_defense_strength"] = serialize_float(
        compute_strength(try_float(expected["away_goals_conceded_avg_before"]),
                         try_float(expected["league_avg_home_goals_before"])))

    # Compare with CSV
    mismatched = []
    int_fields = {
        "home_matches_played_before", "away_matches_played_before",
        "h2h_matches_before", "league_matches_played_before",
        "h2h_last_meeting_days_before",
    }
    for field in AUDIT_FIELDS_CHECK:
        csv_val = feat_row.get(field, "")
        exp_val = expected[field]
        if field in int_fields:
            if not ints_equal(csv_val, exp_val):
                mismatched.append(field)
        else:
            if not floats_close(try_float(csv_val), try_float(exp_val)):
                mismatched.append(field)

    return len(mismatched), mismatched

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 5 — G4: ANTI-LEAKAGE FULL AUDIT (STRICT)")
    print("=" * 78)
    print()

    for p in (MATCH_FEATURES_CSV, MATCHES_CSV, TEAMS_CSV, TEAMS_CANONICAL_CSV,
              SEASON_META_CSV, CANONICAL_TEAMS_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}"); sys.exit(1)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    pre_sha = {
        "match_features.csv": sha256_file(MATCH_FEATURES_CSV),
        "matches.csv": sha256_file(MATCHES_CSV),
        "teams.csv": sha256_file(TEAMS_CSV),
        "teams_canonical.csv": sha256_file(TEAMS_CANONICAL_CSV),
        "season_meta.csv": sha256_file(SEASON_META_CSV),
        "canonical_teams.csv": sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }

    print("--- LOAD INPUTS ---")
    inputs = load_inputs()
    print(f"  eligible_seasons : {sorted(inputs['eligible_seasons'])}")
    print()

    print("--- LOAD FEATURES ---")
    feat_rows, feat_fields, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
    if err: print(f"  [FAIL] {err}"); sys.exit(1)
    print(f"  features rows    : {len(feat_rows)}")
    print(f"  features cols    : {len(feat_fields)}")
    print()

    print("--- LOAD MATCHES (raw) ---")
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: print(f"  [FAIL] {err}"); sys.exit(1)
    eligible_seasons = inputs["eligible_seasons"]
    team_by_raw_name = inputs["team_by_raw_name"]

    eligible_matches = []
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in eligible_seasons: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue
        eligible_matches.append(m)
    print(f"  eligible matches : {len(eligible_matches)}")
    print()

    if len(eligible_matches) != len(feat_rows):
        print(f"  [FAIL] row count mismatch: matches={len(eligible_matches)} features={len(feat_rows)}")
        sys.exit(1)

    print("--- BUILD ALL INDEXES (fresh from raw) ---")
    start = datetime.now()
    indexes = build_all_indexes(eligible_matches, inputs)
    print(f"  team_all keys   : {len(indexes['team_all'])}")
    print(f"  h2h keys        : {len(indexes['h2h'])}")
    print(f"  league keys     : {len(indexes['league'])}")
    print(f"  build time      : {(datetime.now() - start).total_seconds():.1f}s")
    print()

    print("--- AUDIT ALL 4,608 MATCHES (100% strict) ---")
    start = datetime.now()
    audit_rows = []
    n_pass = 0
    n_fail = 0
    fail_samples = []

    for i, feat in enumerate(feat_rows):
        n_mis, mismatched = audit_match(feat, indexes, inputs)
        status = "PASS" if n_mis == 0 else "FAIL"
        audit_rows.append({
            "match_id": feat["match_id"],
            "date": feat["date"],
            "season": feat["season"],
            "league_code": feat["league_code"],
            "status": status,
            "mismatch_fields": ",".join(mismatched) if mismatched else "",
            "n_fields_checked": len(AUDIT_FIELDS_CHECK),
        })
        if n_mis == 0:
            n_pass += 1
        else:
            n_fail += 1
            if len(fail_samples) < 10:
                fail_samples.append((feat["match_id"], mismatched[:5]))
        # progress report every 500
        if (i + 1) % 500 == 0:
            elapsed = (datetime.now() - start).total_seconds()
            print(f"  ... {i+1}/{len(feat_rows)} audited ({elapsed:.1f}s)")

    elapsed = (datetime.now() - start).total_seconds()
    print(f"  audit complete  : {len(audit_rows)} matches in {elapsed:.1f}s")
    print(f"  PASS            : {n_pass}")
    print(f"  FAIL            : {n_fail}")
    print()

    if n_fail > 0:
        print(f"  [FAIL] {n_fail} match(es) failed audit:")
        for mid, fields in fail_samples:
            print(f"    {mid}: {fields}")
        if n_fail > 10:
            print(f"    ... and {n_fail - 10} more")
        print()
        print("STATUS: G4 FAIL ❌ — DATA LEAKAGE DETECTED")
        print("Phase 5 cannot proceed to G5 until leakage is fixed in G1/G2/G3.")
        # Write audit CSV + report even on fail (for investigation)
        write_csv_lf(AUDIT_CSV, AUDIT_FIELDS, audit_rows, pk="match_id")
        report_lines = [
            "# Phase 5 — G4 Report (Anti-Leakage Audit) — FAILED",
            f"Total matches audited : {len(audit_rows)}",
            f"PASS                  : {n_pass}",
            f"FAIL                  : {n_fail}",
            "",
            "## Failures (first 10)",
        ]
        for mid, fields in fail_samples:
            report_lines.append(f"- {mid}: {fields}")
        write_text_lf(REPORT_TXT, "\n".join(report_lines) + "\n")
        sys.exit(1)

    print("--- WRITE AUDIT CSV ---")
    write_csv_lf(AUDIT_CSV, AUDIT_FIELDS, audit_rows, pk="match_id")
    print(f"  [WROTE] {AUDIT_CSV.relative_to(PROJECT_ROOT)}")
    print()

    print("--- READ-ONLY INTEGRITY ---")
    post_sha = {
        "match_features.csv": sha256_file(MATCH_FEATURES_CSV),
        "matches.csv": sha256_file(MATCHES_CSV),
        "teams.csv": sha256_file(TEAMS_CSV),
        "teams_canonical.csv": sha256_file(TEAMS_CANONICAL_CSV),
        "season_meta.csv": sha256_file(SEASON_META_CSV),
        "canonical_teams.csv": sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }
    for k in pre_sha:
        same = pre_sha[k] == post_sha[k]
        print(f"  [{'SAME ' if same else 'DIFF '}] {k}")
        if not same:
            print(f"    [FAIL] input changed"); sys.exit(1)
    print()

    print("--- WRITE REPORT ---")
    report_lines = [
        "# Phase 5 — G4 Report (Anti-Leakage Audit)",
        "",
        "## Input SHA-256",
    ]
    for k, v in pre_sha.items():
        report_lines.append(f"{k}: {v}")
    report_lines += [
        "",
        "## Audit summary",
        f"Total matches audited : {len(audit_rows)}",
        f"PASS                  : {n_pass}",
        f"FAIL                  : {n_fail}",
        f"Fields per match      : {len(AUDIT_FIELDS_CHECK)}",
        "",
        "## Audit method",
        "- Full strict recompute (100% match, not sample)",
        "- Independent from G1/G2/G3 state (fresh indexes from raw matches)",
        "- Strict filter date < X.date (no same-day, no future)",
        "",
        "## Fields audited (40)",
    ]
    for f in AUDIT_FIELDS_CHECK:
        report_lines.append(f"  - {f}")
    report_lines += [
        "",
        "## Result",
        "ZERO data leakage. All 4,608 matches PASS strict audit.",
        "",
        "STATUS: G4 PASS",
    ]
    write_text_lf(REPORT_TXT, "\n".join(report_lines) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("G4 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("N_ELIGIBLE == features rows",        len(eligible_matches) == len(feat_rows)),
        ("All 4,608 matches audited",          len(audit_rows) == len(feat_rows)),
        ("100% PASS (zero leakage)",           n_fail == 0),
        ("Audit CSV written",                  AUDIT_CSV.exists()),
        ("Report written",                     REPORT_TXT.exists()),
        ("All input files SHA-256 unchanged",
            all(pre_sha[k] == post_sha[k] for k in pre_sha)),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  matches audited : {len(audit_rows)}")
    print(f"  PASS            : {n_pass}")
    print(f"  FAIL            : {n_fail}")
    print()
    if all_ok:
        print("STATUS: G4 PASS ✅")
        print()
        print("ZERO DATA LEAKAGE VERIFIED")
        print()
        print("NEXT: User approval → G5 (final assembly + metadata)")
        sys.exit(0)
    else:
        print("STATUS: G4 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
