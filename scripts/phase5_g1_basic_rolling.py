#!/usr/bin/env python3
"""
Phase 5 — G1: Basic Rolling Stats (Home & Away)
================================================

Input (READ-ONLY):
  data/processed/matches.csv
  data/processed/teams.csv
  data/processed/teams_canonical.csv
  data/processed/season_meta.csv
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE):
  data/features/match_features.csv    (31 kolom: identity + target + rolling home/away)
  data/reports/phase5_g1_report.txt

Anti-leakage (KRITIKAL):
  - Rolling stats per match X HANYA dari match dengan date < X.date (strict)
  - Same-day excluded
  - Rolling lintas musim (tidak reset per season)
  - Filter Q2: COMPLETE + INCOMPLETE_STATS dengan FT valid

Algoritma (Q8=c):
  - Build index team_id -> sorted (date, match_id, stats) dari semua team-appearances
  - Untuk setiap match X, binary search index untuk cutoff date < X.date
  - Aggregate rolling, deterministic sort by (date, match_id)

Idempotency: SKIP_IDENTICAL / BLOCKED_DIFFERENT / WROTE
Determinism: rebuild 2x byte-identical
"""
import sys
import csv
import io
import hashlib
import shutil
from bisect import bisect_left
from pathlib import Path
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
FEATURES_DIR  = DATA_DIR / "features"
REPORTS_DIR   = DATA_DIR / "reports"
BACKUPS_DIR   = DATA_DIR / "backups"

# Inputs
MATCHES_CSV          = PROCESSED_DIR / "matches.csv"
TEAMS_CSV            = PROCESSED_DIR / "teams.csv"
TEAMS_CANONICAL_CSV  = PROCESSED_DIR / "teams_canonical.csv"
SEASON_META_CSV      = PROCESSED_DIR / "season_meta.csv"
CANONICAL_TEAMS_CSV  = MAPPINGS_DIR  / "canonical_teams.csv"
DEPRECATED_MAP_CSV   = MAPPINGS_DIR  / "deprecated_canonical_map.csv"

# Outputs
MATCH_FEATURES_CSV = FEATURES_DIR / "match_features.csv"
REPORT_TXT         = REPORTS_DIR  / "phase5_g1_report.txt"

# Schema (31 kolom G1)
G1_FIELDS = [
    # A. Identitas (8)
    "match_id", "league_code", "season", "date",
    "home_team", "away_team", "home_canonical_id", "away_canonical_id",
    # B. Target (1)
    "btts_target",
    # C. Home rolling (11)
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
    # D. Away rolling (11)
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
]

RECENT_WINDOWS = [5, 10, 15]
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
    float_fields = {f for f in G1_FIELDS if "avg" in f or "rate" in f or "before" in f and "matches_played" not in f}
    float_fields = {
        "home_goals_scored_avg_before", "home_goals_conceded_avg_before",
        "home_btts_rate_before", "home_scoring_rate_before", "home_conceding_rate_before",
        "home_clean_sheet_rate_before", "home_failed_to_score_rate_before",
        "home_btts_last_5_before", "home_btts_last_10_before", "home_btts_last_15_before",
        "away_goals_scored_avg_before", "away_goals_conceded_avg_before",
        "away_btts_rate_before", "away_scoring_rate_before", "away_conceding_rate_before",
        "away_clean_sheet_rate_before", "away_failed_to_score_rate_before",
        "away_btts_last_5_before", "away_btts_last_10_before", "away_btts_last_15_before",
    }
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
    if not eligible_seasons:
        raise RuntimeError("season_meta: no train_eligible=1 seasons")

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

# ---------- Pre-flight ----------

def preflight(inputs):
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: return {"fail": f"matches.csv read: {err}"}

    eligible_seasons = inputs["eligible_seasons"]
    team_by_raw_name = inputs["team_by_raw_name"]

    # Tahap A: COMPLETE FT integrity (all rows)
    for m in matches_rows:
        if str(m.get("data_status", "")).strip() == VALID_STATUS_COMPLETE:
            fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
            if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
                return {"fail": f"COMPLETE FT invalid: match_id={m.get('match_id')}"}

    # V-F1-23: team whitelist
    seen_teams = set()
    for m in matches_rows:
        h = m.get("home_team", "").strip(); a = m.get("away_team", "").strip()
        if h: seen_teams.add(h)
        if a: seen_teams.add(a)
    unknown = sorted(t for t in seen_teams if t not in team_by_raw_name)
    if unknown:
        return {"fail": f"V-F1-23 unknown team(s): {unknown[:10]}"}

    # Filter Q2 eligible
    eligible_matches = []
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in eligible_seasons: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue
        eligible_matches.append(m)

    return {
        "fail": None,
        "matches_rows":     matches_rows,
        "eligible_matches": eligible_matches,
        "N_ELIGIBLE":       len(eligible_matches),
    }

# ---------- Build team-appearance index ----------

def build_team_appearance_index(eligible_matches, inputs):
    """
    Build for each team_id a sorted list of (date, match_id, stats) dari
    setiap team-appearance (home OR away).
    stats = {goals_scored, goals_conceded, btts, clean_sheet, failed_to_score}
    """
    team_by_raw_name = inputs["team_by_raw_name"]
    index = defaultdict(list)

    for m in eligible_matches:
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name.get(h_raw)
        a_id = team_by_raw_name.get(a_raw)
        if h_id is None or a_id is None:
            raise RuntimeError(f"unknown team at build index: {h_raw}/{a_raw}")
        date = m.get("date", "").strip()
        mid = m.get("match_id", "").strip()
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0

        # Home appearance
        index[h_id].append({
            "date": date, "match_id": mid,
            "goals_scored": fthg, "goals_conceded": ftag,
            "btts": btts,
            "clean_sheet": 1 if ftag == 0 else 0,
            "failed_to_score": 1 if fthg == 0 else 0,
        })
        # Away appearance
        index[a_id].append({
            "date": date, "match_id": mid,
            "goals_scored": ftag, "goals_conceded": fthg,
            "btts": btts,
            "clean_sheet": 1 if fthg == 0 else 0,
            "failed_to_score": 1 if ftag == 0 else 0,
        })

    # Sort each list by (date, match_id) ASC — deterministic
    for tid in index:
        index[tid].sort(key=lambda r: (r["date"], r["match_id"]))
    return index

# ---------- Compute rolling for a match ----------

def compute_rolling_before(index_list, cutoff_date):
    """
    Given sorted index list for a team and a cutoff date,
    return aggregate dict from all appearances with date < cutoff_date.
    Uses binary search (bisect) for performance (Q8=c).
    """
    # Find rightmost index where date < cutoff_date
    # index_list is sorted by date ASC (then match_id ASC).
    # We need all entries with date < cutoff_date.
    # Since date is ISO string "YYYY-MM-DD", lexicographic comparison works.
    lo, hi = 0, len(index_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if index_list[mid]["date"] < cutoff_date:
            lo = mid + 1
        else:
            hi = mid
    cutoff_idx = lo
    selected = index_list[:cutoff_idx]
    n = len(selected)
    if n == 0:
        return {
            "matches_played": 0,
            "goals_scored_avg": None,
            "goals_conceded_avg": None,
            "btts_rate": None,
            "scoring_rate": None,
            "conceding_rate": None,
            "clean_sheet_rate": None,
            "failed_to_score_rate": None,
            "recent_btts": {w: None for w in RECENT_WINDOWS},
        }

    sum_gs = sum(r["goals_scored"] for r in selected)
    sum_gc = sum(r["goals_conceded"] for r in selected)
    sum_btts = sum(r["btts"] for r in selected)
    sum_cs = sum(r["clean_sheet"] for r in selected)
    sum_fts = sum(r["failed_to_score"] for r in selected)

    recent_btts = {}
    for w in RECENT_WINDOWS:
        if n >= w:
            last_w = selected[-w:]
            recent_btts[w] = sum(r["btts"] for r in last_w) / w
        else:
            recent_btts[w] = None

    return {
        "matches_played": n,
        "goals_scored_avg": sum_gs / n,
        "goals_conceded_avg": sum_gc / n,
        "btts_rate": sum_btts / n,
        "scoring_rate": (n - sum_fts) / n,
        "conceding_rate": (n - sum_cs) / n,
        "clean_sheet_rate": sum_cs / n,
        "failed_to_score_rate": sum_fts / n,
        "recent_btts": recent_btts,
    }

# ---------- Build features ----------

def build_features(eligible_matches, inputs):
    team_by_raw_name = inputs["team_by_raw_name"]
    tc_by_team = inputs["tc_by_team"]
    canonical_status = inputs["canonical_status"]
    dep_redirect = inputs["dep_redirect"]

    # Build index (team_id -> sorted appearances)
    index = build_team_appearance_index(eligible_matches, inputs)

    rows = []
    for m in eligible_matches:
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name[h_raw]
        a_id = team_by_raw_name[a_raw]
        date = m.get("date", "").strip()
        league = m.get("league_code", "").strip()
        season = try_int(m.get("season"))
        mid = m.get("match_id", "").strip()
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0

        # Resolve canonical
        h_tc = tc_by_team.get(h_id); a_tc = tc_by_team.get(a_id)
        if h_tc is None or a_tc is None:
            raise RuntimeError(f"teams_canonical missing for {h_id}/{a_id}")
        h_cid = resolve_active(h_tc["canonical_id"], canonical_status, dep_redirect)
        a_cid = resolve_active(a_tc["canonical_id"], canonical_status, dep_redirect)
        if h_cid is None or a_cid is None:
            raise RuntimeError(f"cannot resolve canonical for {h_raw}/{a_raw}")

        # Rolling stats: match dengan date < X.date (strict)
        h_roll = compute_rolling_before(index[h_id], date)
        a_roll = compute_rolling_before(index[a_id], date)

        rows.append({
            "match_id": mid,
            "league_code": league,
            "season": season,
            "date": date,
            "home_team": h_raw,
            "away_team": a_raw,
            "home_canonical_id": h_cid,
            "away_canonical_id": a_cid,
            "btts_target": btts,
            # Home rolling
            "home_matches_played_before": h_roll["matches_played"],
            "home_goals_scored_avg_before": serialize_float(h_roll["goals_scored_avg"]),
            "home_goals_conceded_avg_before": serialize_float(h_roll["goals_conceded_avg"]),
            "home_btts_rate_before": serialize_float(h_roll["btts_rate"]),
            "home_scoring_rate_before": serialize_float(h_roll["scoring_rate"]),
            "home_conceding_rate_before": serialize_float(h_roll["conceding_rate"]),
            "home_clean_sheet_rate_before": serialize_float(h_roll["clean_sheet_rate"]),
            "home_failed_to_score_rate_before": serialize_float(h_roll["failed_to_score_rate"]),
            "home_btts_last_5_before": serialize_float(h_roll["recent_btts"][5]),
            "home_btts_last_10_before": serialize_float(h_roll["recent_btts"][10]),
            "home_btts_last_15_before": serialize_float(h_roll["recent_btts"][15]),
            # Away rolling
            "away_matches_played_before": a_roll["matches_played"],
            "away_goals_scored_avg_before": serialize_float(a_roll["goals_scored_avg"]),
            "away_goals_conceded_avg_before": serialize_float(a_roll["goals_conceded_avg"]),
            "away_btts_rate_before": serialize_float(a_roll["btts_rate"]),
            "away_scoring_rate_before": serialize_float(a_roll["scoring_rate"]),
            "away_conceding_rate_before": serialize_float(a_roll["conceding_rate"]),
            "away_clean_sheet_rate_before": serialize_float(a_roll["clean_sheet_rate"]),
            "away_failed_to_score_rate_before": serialize_float(a_roll["failed_to_score_rate"]),
            "away_btts_last_5_before": serialize_float(a_roll["recent_btts"][5]),
            "away_btts_last_10_before": serialize_float(a_roll["recent_btts"][10]),
            "away_btts_last_15_before": serialize_float(a_roll["recent_btts"][15]),
        })

    # Deterministic sort by match_id (or date, match_id)
    rows.sort(key=lambda r: (r["date"], r["match_id"]))
    return rows

# ---------- Anti-leakage spot check ----------

def anti_leakage_spot_check(feature_rows, eligible_matches, inputs, sample_n=20):
    """
    For a sample of matches, recompute rolling stats using strict date < X.date filter
    from raw matches (independent path). Compare with feature CSV values.
    Returns list of problems.
    """
    import random
    random.seed(42)  # deterministic sample
    team_by_raw_name = inputs["team_by_raw_name"]

    sample_ids = set()
    # Deterministic sample by sorted match_id
    sorted_ids = sorted(m["match_id"] for m in eligible_matches)
    step = max(1, len(sorted_ids) // sample_n)
    for i in range(0, len(sorted_ids), step):
        sample_ids.add(sorted_ids[i])
        if len(sample_ids) >= sample_n: break

    feats_by_id = {r["match_id"]: r for r in feature_rows}
    problems = []

    for m in eligible_matches:
        mid = m.get("match_id", "").strip()
        if mid not in sample_ids: continue
        date = m.get("date", "").strip()
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name[h_raw]; a_id = team_by_raw_name[a_raw]

        # Independent recompute: loop raw eligible matches with date < X.date
        h_apps = []; a_apps = []
        for m2 in eligible_matches:
            d2 = m2.get("date", "").strip()
            if d2 >= date: continue  # strict <
            h2_raw = m2.get("home_team", "").strip()
            a2_raw = m2.get("away_team", "").strip()
            fthg2 = try_int(m2.get("home_goals")); ftag2 = try_int(m2.get("away_goals"))
            btts2 = 1 if (fthg2 > 0 and ftag2 > 0) else 0
            # Home team X appearance
            if h2_raw == h_raw:
                h_apps.append({"date": d2, "match_id": m2.get("match_id",""),
                               "gs": fthg2, "gc": ftag2, "btts": btts2,
                               "cs": 1 if ftag2 == 0 else 0,
                               "fts": 1 if fthg2 == 0 else 0})
            if a2_raw == h_raw:
                h_apps.append({"date": d2, "match_id": m2.get("match_id",""),
                               "gs": ftag2, "gc": fthg2, "btts": btts2,
                               "cs": 1 if fthg2 == 0 else 0,
                               "fts": 1 if ftag2 == 0 else 0})
            # Away team X appearance
            if h2_raw == a_raw:
                a_apps.append({"date": d2, "match_id": m2.get("match_id",""),
                               "gs": fthg2, "gc": ftag2, "btts": btts2,
                               "cs": 1 if ftag2 == 0 else 0,
                               "fts": 1 if fthg2 == 0 else 0})
            if a2_raw == a_raw:
                a_apps.append({"date": d2, "match_id": m2.get("match_id",""),
                               "gs": ftag2, "gc": fthg2, "btts": btts2,
                               "cs": 1 if fthg2 == 0 else 0,
                               "fts": 1 if ftag2 == 0 else 0})

        h_apps.sort(key=lambda r: (r["date"], r["match_id"]))
        a_apps.sort(key=lambda r: (r["date"], r["match_id"]))

        feat = feats_by_id[mid]

        # Home check
        n_h = len(h_apps)
        if n_h != int(feat["home_matches_played_before"]):
            problems.append(f"{mid}: home matches_played mismatch: indep={n_h} feat={feat['home_matches_played_before']}")
        elif n_h > 0:
            exp_gs = sum(r["gs"] for r in h_apps) / n_h
            got_gs = try_float(feat["home_goals_scored_avg_before"])
            if got_gs is None or abs(exp_gs - got_gs) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: home gs_avg mismatch: indep={exp_gs:.6f} feat={got_gs}")
            exp_btts = sum(r["btts"] for r in h_apps) / n_h
            got_btts = try_float(feat["home_btts_rate_before"])
            if got_btts is None or abs(exp_btts - got_btts) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: home btts_rate mismatch: indep={exp_btts:.6f} feat={got_btts}")

        # Away check
        n_a = len(a_apps)
        if n_a != int(feat["away_matches_played_before"]):
            problems.append(f"{mid}: away matches_played mismatch: indep={n_a} feat={feat['away_matches_played_before']}")
        elif n_a > 0:
            exp_gs = sum(r["gs"] for r in a_apps) / n_a
            got_gs = try_float(feat["away_goals_scored_avg_before"])
            if got_gs is None or abs(exp_gs - got_gs) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: away gs_avg mismatch: indep={exp_gs:.6f} feat={got_gs}")

    return problems, len(sample_ids)

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 5 — G1: BASIC ROLLING STATS")
    print("=" * 78)
    print()

    for p in (MATCHES_CSV, TEAMS_CSV, TEAMS_CANONICAL_CSV, SEASON_META_CSV, CANONICAL_TEAMS_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}"); sys.exit(1)
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    pre_sha = {
        "matches.csv": sha256_file(MATCHES_CSV),
        "teams.csv": sha256_file(TEAMS_CSV),
        "teams_canonical.csv": sha256_file(TEAMS_CANONICAL_CSV),
        "season_meta.csv": sha256_file(SEASON_META_CSV),
        "canonical_teams.csv": sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }

    print("--- LOAD INPUTS ---")
    inputs = load_inputs()
    print(f"  eligible_seasons   : {sorted(inputs['eligible_seasons'])}")
    print(f"  teams raw_name map : {len(inputs['team_by_raw_name'])}")
    print()

    print("--- PRE-FLIGHT ---")
    pf = preflight(inputs)
    if pf.get("fail"):
        print(f"  [FAIL] {pf['fail']}"); sys.exit(1)
    print(f"  N_ELIGIBLE : {pf['N_ELIGIBLE']}")
    print(f"  [OK] pre-flight PASS")
    print()

    print("--- BUILD ---")
    feature_rows = build_features(pf["eligible_matches"], inputs)
    print(f"  rows built : {len(feature_rows)}")
    print()

    print("--- DETERMINISM ---")
    feature_rows2 = build_features(pf["eligible_matches"], inputs)
    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f,"")) for f in fields)
                         for r in sorted(rows, key=lambda x: x["match_id"]))
    det_ok = (ser(feature_rows, G1_FIELDS) == ser(feature_rows2, G1_FIELDS))
    if not det_ok:
        print("  [FAIL] non-deterministic"); sys.exit(1)
    print(f"  [OK] rebuild 2x identical")
    print()

    print("--- ANTI-LEAKAGE SPOT CHECK (sample 20) ---")
    problems, sample_size = anti_leakage_spot_check(feature_rows, pf["eligible_matches"], inputs, sample_n=20)
    if problems:
        print(f"  [FAIL] {len(problems)} leakage/mismatch issues (sample size {sample_size}):")
        for p in problems[:10]: print(f"    {p}")
        sys.exit(1)
    print(f"  [OK] {sample_size} samples verified (independent recompute == CSV)")
    print()

    print("--- WRITE (idempotent) ---")
    def pk_fn(r): return r["match_id"]
    actions = {}
    eq, _ = structural_equal_csv(MATCH_FEATURES_CSV, G1_FIELDS, feature_rows, pk_fn)
    if eq:
        actions["match_features.csv"] = "SKIP_IDENTICAL"
    elif MATCH_FEATURES_CSV.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUPS_DIR / f"phase5_G1_match_features_{ts}.csv"
        shutil.copy2(MATCH_FEATURES_CSV, backup)
        print(f"  [BLOCKED_DIFFERENT] backup: {backup.name}"); sys.exit(1)
    else:
        write_csv_lf(MATCH_FEATURES_CSV, G1_FIELDS, feature_rows, pk=pk_fn)
        actions["match_features.csv"] = "WROTE"
    for k, v in actions.items(): print(f"  [{v:18s}] {k}")
    print()

    print("--- VERIFY POST-WRITE ---")
    ok, reason = structural_equal_csv(MATCH_FEATURES_CSV, G1_FIELDS, feature_rows, pk_fn)
    if not ok:
        print(f"  [FAIL] {reason}"); sys.exit(1)
    print(f"  [OK] CSV matches in-memory structurally")
    print()

    print("--- READ-ONLY INTEGRITY ---")
    post_sha = {
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
        "# Phase 5 — G1 Report (Basic Rolling Stats)",
        "",
        "## Input SHA-256",
    ]
    for k, v in pre_sha.items():
        report_lines.append(f"{k}: {v}")
    report_lines += [
        "",
        "## Pre-flight",
        f"N_ELIGIBLE : {pf['N_ELIGIBLE']}",
        "",
        "## Output",
        f"match_features rows : {len(feature_rows)}",
        f"columns (G1)        : {len(G1_FIELDS)}",
        f"write action        : {actions.get('match_features.csv')}",
        f"output SHA-256      : {sha256_file(MATCH_FEATURES_CSV)}",
        "",
        "## Anti-leakage",
        "- Rolling stats per match X HANYA dari match date < X.date (strict)",
        "- Same-day excluded",
        "- Spot check: 20 samples verified (independent recompute)",
        "",
        "## Determinism",
        "rebuild 2x: identical",
        "",
        "## Float precision",
        f"CSV serialization: round-half-even to {FLOAT_PRECISION} desimal",
        f"CSV tolerance    : {FLOAT_TOL_CSV}",
        "",
        "STATUS: G1 PASS",
    ]
    write_text_lf(REPORT_TXT, "\n".join(report_lines) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("G1 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("Pre-flight PASS",                    True),
        ("N_ELIGIBLE > 0",                     pf["N_ELIGIBLE"] > 0),
        ("Row count == N_ELIGIBLE",            len(feature_rows) == pf["N_ELIGIBLE"]),
        ("Columns == 31",                      len(G1_FIELDS) == 31),
        ("Anti-leakage spot check PASS",       len(problems) == 0),
        ("Determinism 2x rebuild",             det_ok),
        ("All inputs SHA-256 unchanged",
            all(pre_sha[k] == post_sha[k] for k in pre_sha)),
        ("match_features.csv written/identical",
            actions.get("match_features.csv") in ("WROTE","SKIP_IDENTICAL")),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  match_features rows : {len(feature_rows)}")
    print(f"  N_ELIGIBLE          : {pf['N_ELIGIBLE']}")
    print(f"  columns             : {len(G1_FIELDS)}")
    print()
    if all_ok:
        print("STATUS: G1 PASS ✅")
        print()
        print("NEXT: User approval → G2 (H2H rolling + league context)")
        sys.exit(0)
    else:
        print("STATUS: G1 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
