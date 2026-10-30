#!/usr/bin/env python3
"""
Phase 5 — G2 v2: H2H Rolling + League Context
===============================================

Fix v1 -> v2:
  v1 detected schema mismatch (G1 31 vs G2 40) as BLOCKED_DIFFERENT,
  which is wrong: extending schema is the intended flow.
  v2 detects 4 states:
    - file missing          -> WROTE
    - schema == G1_FIELDS   -> EXTEND (backup + WROTE 40 cols)
    - schema == G2_FIELDS   -> SKIP_IDENTICAL or BLOCKED_DIFFERENT
    - other schema          -> BLOCKED_UNKNOWN_SCHEMA
  All business logic (pre-flight, indexes, compute, anti-leakage,
  determinism) identical to v1.

Input (READ-ONLY):
  data/features/match_features.csv    (G1 output, 31 cols OR already G2, 40 cols)
  data/processed/matches.csv
  data/processed/teams.csv
  data/processed/teams_canonical.csv
  data/processed/season_meta.csv
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE/UPDATE):
  data/features/match_features.csv    (40 cols = G1 31 + 9 new)
  data/reports/phase5_g2_report.txt
"""
import sys
import csv
import io
import hashlib
import shutil
from pathlib import Path
from datetime import datetime, date as dt_date
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
FEATURES_DIR  = DATA_DIR / "features"
REPORTS_DIR   = DATA_DIR / "reports"
BACKUPS_DIR   = DATA_DIR / "backups"

MATCH_FEATURES_CSV   = FEATURES_DIR / "match_features.csv"
MATCHES_CSV          = PROCESSED_DIR / "matches.csv"
TEAMS_CSV            = PROCESSED_DIR / "teams.csv"
TEAMS_CANONICAL_CSV  = PROCESSED_DIR / "teams_canonical.csv"
SEASON_META_CSV      = PROCESSED_DIR / "season_meta.csv"
CANONICAL_TEAMS_CSV  = MAPPINGS_DIR  / "canonical_teams.csv"
DEPRECATED_MAP_CSV   = MAPPINGS_DIR  / "deprecated_canonical_map.csv"

REPORT_TXT = REPORTS_DIR / "phase5_g2_report.txt"

G1_FIELDS = [
    "match_id", "league_code", "season", "date",
    "home_team", "away_team", "home_canonical_id", "away_canonical_id",
    "btts_target",
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

G2_NEW_FIELDS = [
    "h2h_matches_before",
    "h2h_btts_rate_before",
    "h2h_avg_home_goals_before",
    "h2h_avg_away_goals_before",
    "h2h_last_meeting_days_before",
    "league_btts_rate_before",
    "league_avg_home_goals_before",
    "league_avg_away_goals_before",
    "league_matches_played_before",
]

G2_FIELDS = G1_FIELDS + G2_NEW_FIELDS

FLOAT_PRECISION = 6
FLOAT_TOL_CSV = 1e-6

VALID_STATUS_COMPLETE   = "COMPLETE"
VALID_STATUS_INCOMPLETE = "INCOMPLETE_STATS"
VALID_STATUS_INVALID    = "INVALID"

FLOAT_FIELDS = {
    "home_goals_scored_avg_before", "home_goals_conceded_avg_before",
    "home_btts_rate_before", "home_scoring_rate_before", "home_conceding_rate_before",
    "home_clean_sheet_rate_before", "home_failed_to_score_rate_before",
    "home_btts_last_5_before", "home_btts_last_10_before", "home_btts_last_15_before",
    "away_goals_scored_avg_before", "away_goals_conceded_avg_before",
    "away_btts_rate_before", "away_scoring_rate_before", "away_conceding_rate_before",
    "away_clean_sheet_rate_before", "away_failed_to_score_rate_before",
    "away_btts_last_5_before", "away_btts_last_10_before", "away_btts_last_15_before",
    "h2h_btts_rate_before", "h2h_avg_home_goals_before", "h2h_avg_away_goals_before",
    "league_btts_rate_before", "league_avg_home_goals_before", "league_avg_away_goals_before",
}

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

def days_between(iso_a, iso_b):
    try:
        da = dt_date.fromisoformat(iso_a)
        db = dt_date.fromisoformat(iso_b)
        return (db - da).days
    except (ValueError, TypeError):
        return None

def structural_equal_csv_for_schema(path, fields, expected_rows, pk_fn):
    """
    Compare CSV at `path` against expected_rows with `fields` schema.
    Assumes caller has already verified schema matches.
    """
    got_rows, got_fields, err = read_csv_lf_safe(path)
    if err: return False, f"read_error:{err}"
    if got_fields != fields: return False, "fields_mismatch"
    if len(got_rows) != len(expected_rows):
        return False, f"row_count: got={len(got_rows)} exp={len(expected_rows)}"
    got_sorted = sorted(got_rows, key=pk_fn)
    exp_sorted = sorted(expected_rows, key=pk_fn)
    for i, (gr, er) in enumerate(zip(got_sorted, exp_sorted)):
        for f in fields:
            gv = gr.get(f, ""); ev = er.get(f, "")
            if f in FLOAT_FIELDS:
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
    # Load existing match_features (may be G1 31 cols OR G2 40 cols)
    existing_rows, existing_fields, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
    if err:
        return {"fail": f"match_features.csv read: {err}"}
    if existing_fields not in (G1_FIELDS, G2_FIELDS):
        return {"fail": f"match_features.csv schema unknown: {len(existing_fields)} cols, "
                        f"expected 31 (G1) or 40 (G2)"}

    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: return {"fail": f"matches.csv read: {err}"}

    eligible_seasons = inputs["eligible_seasons"]
    team_by_raw_name = inputs["team_by_raw_name"]

    for m in matches_rows:
        if str(m.get("data_status", "")).strip() == VALID_STATUS_COMPLETE:
            fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
            if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
                return {"fail": f"COMPLETE FT invalid: match_id={m.get('match_id')}"}

    seen_teams = set()
    for m in matches_rows:
        h = m.get("home_team", "").strip(); a = m.get("away_team", "").strip()
        if h: seen_teams.add(h)
        if a: seen_teams.add(a)
    unknown = sorted(t for t in seen_teams if t not in team_by_raw_name)
    if unknown:
        return {"fail": f"V-F1-23 unknown team(s): {unknown[:10]}"}

    eligible_matches = []
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in eligible_seasons: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue
        eligible_matches.append(m)

    if len(existing_rows) != len(eligible_matches):
        return {"fail": f"existing rows ({len(existing_rows)}) != N_ELIGIBLE ({len(eligible_matches)})"}

    # Determine input state
    if existing_fields == G1_FIELDS:
        input_state = "G1_INPUT"
    else:  # G2_FIELDS
        input_state = "G2_INPUT"

    return {
        "fail": None,
        "existing_rows":    existing_rows,
        "existing_fields":  existing_fields,
        "input_state":      input_state,
        "matches_rows":     matches_rows,
        "eligible_matches": eligible_matches,
        "N_ELIGIBLE":       len(eligible_matches),
    }

# ---------- Build H2H index ----------

def pair_key_for(cid_a, cid_b):
    a, b = sorted([cid_a, cid_b])
    return "md5:" + hashlib.md5(f"{a}|{b}".encode("utf-8")).hexdigest()

def build_h2h_index(eligible_matches, inputs):
    team_by_raw_name = inputs["team_by_raw_name"]
    tc_by_team = inputs["tc_by_team"]
    canonical_status = inputs["canonical_status"]
    dep_redirect = inputs["dep_redirect"]

    index = defaultdict(list)
    for m in eligible_matches:
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name.get(h_raw)
        a_id = team_by_raw_name.get(a_raw)
        if h_id is None or a_id is None:
            raise RuntimeError(f"unknown team in H2H index: {h_raw}/{a_raw}")
        h_tc = tc_by_team.get(h_id); a_tc = tc_by_team.get(a_id)
        if h_tc is None or a_tc is None:
            raise RuntimeError(f"teams_canonical missing for {h_id}/{a_id}")
        h_cid = resolve_active(h_tc["canonical_id"], canonical_status, dep_redirect)
        a_cid = resolve_active(a_tc["canonical_id"], canonical_status, dep_redirect)
        if h_cid is None or a_cid is None:
            continue
        if h_cid == a_cid:
            continue

        pk = pair_key_for(h_cid, a_cid)
        date = m.get("date", "").strip()
        mid = m.get("match_id", "").strip()
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0

        index[pk].append({
            "date": date, "match_id": mid,
            "home_cid": h_cid, "away_cid": a_cid,
            "fthg": fthg, "ftag": ftag, "btts": btts,
        })

    for pk in index:
        index[pk].sort(key=lambda r: (r["date"], r["match_id"]))
    return index

def build_league_index(eligible_matches):
    index = defaultdict(list)
    for m in eligible_matches:
        lc = m.get("league_code", "").strip()
        s = try_int(m.get("season"))
        date = m.get("date", "").strip()
        mid = m.get("match_id", "").strip()
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0
        index[(lc, s)].append({
            "date": date, "match_id": mid,
            "fthg": fthg, "ftag": ftag, "btts": btts,
        })
    for k in index:
        index[k].sort(key=lambda r: (r["date"], r["match_id"]))
    return index

# ---------- Rolling computation ----------

def compute_h2h_before(h2h_list, cutoff_date):
    if not h2h_list:
        return {"matches": 0, "btts_rate": None, "avg_home_goals": None,
                "avg_away_goals": None, "last_meeting_days": None}
    lo, hi = 0, len(h2h_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if h2h_list[mid]["date"] < cutoff_date:
            lo = mid + 1
        else:
            hi = mid
    selected = h2h_list[:lo]
    n = len(selected)
    if n == 0:
        return {"matches": 0, "btts_rate": None, "avg_home_goals": None,
                "avg_away_goals": None, "last_meeting_days": None}
    sum_btts = sum(r["btts"] for r in selected)
    sum_fthg = sum(r["fthg"] for r in selected)
    sum_ftag = sum(r["ftag"] for r in selected)
    last_date = selected[-1]["date"]
    days = days_between(last_date, cutoff_date)
    return {
        "matches": n,
        "btts_rate": sum_btts / n,
        "avg_home_goals": sum_fthg / n,
        "avg_away_goals": sum_ftag / n,
        "last_meeting_days": days,
    }

def compute_league_before(league_list, cutoff_date):
    if not league_list:
        return {"matches": 0, "btts_rate": None, "avg_home_goals": None, "avg_away_goals": None}
    lo, hi = 0, len(league_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if league_list[mid]["date"] < cutoff_date:
            lo = mid + 1
        else:
            hi = mid
    selected = league_list[:lo]
    n = len(selected)
    if n == 0:
        return {"matches": 0, "btts_rate": None, "avg_home_goals": None, "avg_away_goals": None}
    sum_btts = sum(r["btts"] for r in selected)
    sum_fthg = sum(r["fthg"] for r in selected)
    sum_ftag = sum(r["ftag"] for r in selected)
    return {
        "matches": n,
        "btts_rate": sum_btts / n,
        "avg_home_goals": sum_fthg / n,
        "avg_away_goals": sum_ftag / n,
    }

# ---------- Extend features ----------

def extend_features(g1_rows, h2h_index, league_index):
    out_rows = []
    for r in g1_rows:
        home_cid = r["home_canonical_id"]
        away_cid = r["away_canonical_id"]
        date = r["date"]
        lc = r["league_code"]
        season = int(r["season"]) if r["season"] else None

        pk = pair_key_for(home_cid, away_cid)
        h2h_list = h2h_index.get(pk, [])
        league_list = league_index.get((lc, season), [])

        h2h = compute_h2h_before(h2h_list, date)
        lg = compute_league_before(league_list, date)

        # Start from G1 fields only (drop any pre-existing G2 fields to ensure clean extend)
        new = {f: r.get(f, "") for f in G1_FIELDS}

        new["h2h_matches_before"] = h2h["matches"]
        new["h2h_btts_rate_before"] = serialize_float(h2h["btts_rate"])
        new["h2h_avg_home_goals_before"] = serialize_float(h2h["avg_home_goals"])
        new["h2h_avg_away_goals_before"] = serialize_float(h2h["avg_away_goals"])
        new["h2h_last_meeting_days_before"] = "" if h2h["last_meeting_days"] is None else h2h["last_meeting_days"]

        new["league_btts_rate_before"] = serialize_float(lg["btts_rate"])
        new["league_avg_home_goals_before"] = serialize_float(lg["avg_home_goals"])
        new["league_avg_away_goals_before"] = serialize_float(lg["avg_away_goals"])
        new["league_matches_played_before"] = lg["matches"]

        out_rows.append(new)
    out_rows.sort(key=lambda r: (r["date"], r["match_id"]))
    return out_rows

# ---------- Anti-leakage spot check ----------

def anti_leakage_spot_check(feature_rows, eligible_matches, inputs, sample_n=20):
    team_by_raw_name = inputs["team_by_raw_name"]
    tc_by_team = inputs["tc_by_team"]
    canonical_status = inputs["canonical_status"]
    dep_redirect = inputs["dep_redirect"]

    sample_ids = set()
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
        feat = feats_by_id.get(mid)
        if not feat: continue

        date = m.get("date", "").strip()
        lc = m.get("league_code", "").strip()
        s = try_int(m.get("season"))
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name[h_raw]; a_id = team_by_raw_name[a_raw]
        h_cid = resolve_active(tc_by_team[h_id]["canonical_id"], canonical_status, dep_redirect)
        a_cid = resolve_active(tc_by_team[a_id]["canonical_id"], canonical_status, dep_redirect)

        h2h_n = 0; h2h_btts = 0
        h2h_last_date = None
        lg_n = 0; lg_btts = 0
        for m2 in eligible_matches:
            d2 = m2.get("date", "").strip()
            if d2 >= date: continue
            fthg2 = try_int(m2.get("home_goals")); ftag2 = try_int(m2.get("away_goals"))
            btts2 = 1 if (fthg2 > 0 and ftag2 > 0) else 0
            h2_raw = m2.get("home_team", "").strip()
            a2_raw = m2.get("away_team", "").strip()
            h2_id = team_by_raw_name.get(h2_raw); a2_id = team_by_raw_name.get(a2_raw)
            if h2_id and a2_id:
                h2_cid = resolve_active(tc_by_team[h2_id]["canonical_id"], canonical_status, dep_redirect)
                a2_cid = resolve_active(tc_by_team[a2_id]["canonical_id"], canonical_status, dep_redirect)
                pair_match = (
                    (h2_cid == h_cid and a2_cid == a_cid) or
                    (h2_cid == a_cid and a2_cid == h_cid)
                )
                if pair_match:
                    h2h_n += 1
                    h2h_btts += btts2
                    if h2h_last_date is None or d2 > h2h_last_date:
                        h2h_last_date = d2
            s2 = try_int(m2.get("season"))
            lc2 = m2.get("league_code", "").strip()
            if lc2 == lc and s2 == s:
                lg_n += 1
                lg_btts += btts2

        if h2h_n != int(feat["h2h_matches_before"]):
            problems.append(f"{mid}: h2h_n indep={h2h_n} feat={feat['h2h_matches_before']}")
        elif h2h_n > 0:
            exp_rate = h2h_btts / h2h_n
            got = try_float(feat["h2h_btts_rate_before"])
            if got is None or abs(exp_rate - got) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: h2h_btts_rate indep={exp_rate:.6f} feat={got}")
            if h2h_last_date:
                exp_days = days_between(h2h_last_date, date)
                got_days = try_int(feat["h2h_last_meeting_days_before"])
                if got_days is None or exp_days != got_days:
                    problems.append(f"{mid}: h2h_last_days indep={exp_days} feat={got_days}")

        if lg_n != int(feat["league_matches_played_before"]):
            problems.append(f"{mid}: lg_n indep={lg_n} feat={feat['league_matches_played_before']}")
        elif lg_n > 0:
            exp_rate = lg_btts / lg_n
            got = try_float(feat["league_btts_rate_before"])
            if got is None or abs(exp_rate - got) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: league_btts_rate indep={exp_rate:.6f} feat={got}")

    return problems, len(sample_ids)

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 5 — G2 v2: H2H ROLLING + LEAGUE CONTEXT")
    print("=" * 78)
    print()

    for p in (MATCH_FEATURES_CSV, MATCHES_CSV, TEAMS_CSV, TEAMS_CANONICAL_CSV,
              SEASON_META_CSV, CANONICAL_TEAMS_CSV):
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
    pre_match_features_sha = sha256_file(MATCH_FEATURES_CSV)

    print("--- LOAD INPUTS ---")
    inputs = load_inputs()
    print(f"  eligible_seasons : {sorted(inputs['eligible_seasons'])}")
    print()

    print("--- PRE-FLIGHT ---")
    pf = preflight(inputs)
    if pf.get("fail"):
        print(f"  [FAIL] {pf['fail']}"); sys.exit(1)
    print(f"  input_state   : {pf['input_state']} (existing cols={len(pf['existing_fields'])})")
    print(f"  N_ELIGIBLE    : {pf['N_ELIGIBLE']}")
    print(f"  [OK] pre-flight PASS")
    print()

    # Prepare g1_rows (drop G2 fields if already G2 input)
    g1_rows = [{f: r.get(f, "") for f in G1_FIELDS} for r in pf["existing_rows"]]

    print("--- BUILD INDEXES ---")
    h2h_index = build_h2h_index(pf["eligible_matches"], inputs)
    league_index = build_league_index(pf["eligible_matches"])
    print(f"  h2h pairs      : {len(h2h_index)}")
    print(f"  league-season  : {len(league_index)}")
    print()

    print("--- EXTEND FEATURES ---")
    feature_rows = extend_features(g1_rows, h2h_index, league_index)
    print(f"  rows extended : {len(feature_rows)}")
    print()

    print("--- DETERMINISM ---")
    h2h_index2 = build_h2h_index(pf["eligible_matches"], inputs)
    league_index2 = build_league_index(pf["eligible_matches"])
    feature_rows2 = extend_features(g1_rows, h2h_index2, league_index2)
    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f,"")) for f in fields)
                         for r in sorted(rows, key=lambda x: x["match_id"]))
    det_ok = (ser(feature_rows, G2_FIELDS) == ser(feature_rows2, G2_FIELDS))
    if not det_ok:
        print("  [FAIL] non-deterministic"); sys.exit(1)
    print(f"  [OK] rebuild 2x identical")
    print()

    print("--- ANTI-LEAKAGE SPOT CHECK (sample 20) ---")
    problems, sample_size = anti_leakage_spot_check(feature_rows, pf["eligible_matches"], inputs, sample_n=20)
    if problems:
        print(f"  [FAIL] {len(problems)} mismatch(es):")
        for p in problems[:10]: print(f"    {p}")
        sys.exit(1)
    print(f"  [OK] {sample_size} samples verified")
    print()

    # ---------- WRITE (v2: state-aware) ----------
    print("--- WRITE (idempotent, state-aware) ---")
    def pk_fn(r): return r["match_id"]
    actions = {}

    if not MATCH_FEATURES_CSV.exists():
        # State: ABSENT -> write fresh
        write_csv_lf(MATCH_FEATURES_CSV, G2_FIELDS, feature_rows, pk=pk_fn)
        actions["match_features.csv"] = "WROTE"
    else:
        # File exists — inspect schema
        existing_rows_check, existing_fields_check, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
        if err:
            print(f"  [FAIL] cannot re-read match_features: {err}"); sys.exit(1)

        if existing_fields_check == G1_FIELDS:
            # State: G1_INPUT -> EXTEND (backup + write 40 cols)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = BACKUPS_DIR / f"phase5_G2_match_features_{ts}.csv"
            shutil.copy2(MATCH_FEATURES_CSV, backup)
            write_csv_lf(MATCH_FEATURES_CSV, G2_FIELDS, feature_rows, pk=pk_fn)
            actions["match_features.csv"] = "WROTE_EXTEND_FROM_G1"
            actions["backup"] = backup.name
        elif existing_fields_check == G2_FIELDS:
            # State: G2_INPUT -> SKIP if identical, else BLOCK
            eq, reason = structural_equal_csv_for_schema(
                MATCH_FEATURES_CSV, G2_FIELDS, feature_rows, pk_fn
            )
            if eq:
                actions["match_features.csv"] = "SKIP_IDENTICAL"
            else:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = BACKUPS_DIR / f"phase5_G2_match_features_{ts}.csv"
                shutil.copy2(MATCH_FEATURES_CSV, backup)
                print(f"  [BLOCKED_DIFFERENT] {reason}")
                print(f"  [BACKUP] {backup.name}")
                print()
                print("STATUS: G2 BLOCKED_DIFFERENT ❌")
                print("Existing G2 file differs from current build.")
                print("User must inspect backup vs new build, then decide.")
                sys.exit(1)
        else:
            print(f"  [FAIL] unknown schema: {len(existing_fields_check)} cols")
            print(f"  expected 31 (G1) or 40 (G2)")
            sys.exit(1)

    for k, v in actions.items(): print(f"  [{v:32s}] {k}")
    print()

    print("--- VERIFY POST-WRITE ---")
    ok, reason = structural_equal_csv_for_schema(MATCH_FEATURES_CSV, G2_FIELDS, feature_rows, pk_fn)
    if not ok:
        print(f"  [FAIL] {reason}"); sys.exit(1)
    print(f"  [OK] CSV matches in-memory structurally (40 cols)")
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
    for k in post_sha:
        same = pre_sha[k] == post_sha[k]
        print(f"  [{'SAME ' if same else 'DIFF '}] {k}")
        if not same:
            print(f"    [FAIL] input changed"); sys.exit(1)
    print()

    print("--- WRITE REPORT ---")
    report_lines = [
        "# Phase 5 — G2 v2 Report (H2H Rolling + League Context)",
        "",
        "## Input SHA-256",
        f"match_features.csv (pre): {pre_match_features_sha}",
    ]
    for k, v in pre_sha.items():
        report_lines.append(f"{k}: {v}")
    report_lines += [
        "",
        "## Pre-flight",
        f"input_state  : {pf['input_state']}",
        f"N_ELIGIBLE   : {pf['N_ELIGIBLE']}",
        "",
        "## Output",
        f"match_features rows : {len(feature_rows)}",
        f"columns (G2 total)  : {len(G2_FIELDS)}",
        f"  G1 fields         : {len(G1_FIELDS)}",
        f"  G2 new fields     : {len(G2_NEW_FIELDS)}",
        f"write action        : {actions.get('match_features.csv')}",
        f"output SHA-256      : {sha256_file(MATCH_FEATURES_CSV)}",
        "",
        "## Anti-leakage",
        "- H2H: pair canonical dengan date < X.date (strict)",
        "- League: (league_code, season) dengan date < X.date (strict) — same season",
        "- Spot check: 20 samples verified independent recompute",
        "",
        "## Determinism",
        "rebuild 2x: identical",
        "",
        "## v2 fix note",
        "v1 blocked schema-mismatch (G1 31 vs G2 40) as BLOCKED_DIFFERENT.",
        "v2 recognizes G1_INPUT state and extends to G2 with backup.",
        "",
        "STATUS: G2 v2 PASS",
    ]
    write_text_lf(REPORT_TXT, "\n".join(report_lines) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("G2 v2 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("Pre-flight PASS",                    True),
        ("N_ELIGIBLE > 0",                     pf["N_ELIGIBLE"] > 0),
        ("Row count == N_ELIGIBLE",            len(feature_rows) == pf["N_ELIGIBLE"]),
        ("Columns == 40",                      len(G2_FIELDS) == 40),
        ("G1 columns preserved",               all(G1_FIELDS[i] in feature_rows[0] for i in range(len(G1_FIELDS)))),
        ("Anti-leakage spot check PASS",       len(problems) == 0),
        ("Determinism 2x rebuild",             det_ok),
        ("All Phase 2/3 inputs SHA-256 unchanged",
            all(pre_sha[k] == post_sha[k] for k in post_sha)),
        ("match_features.csv valid state",
            actions.get("match_features.csv") in ("WROTE","WROTE_EXTEND_FROM_G1","SKIP_IDENTICAL")),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  rows     : {len(feature_rows)}")
    print(f"  columns  : {len(G2_FIELDS)}")
    print(f"  write    : {actions.get('match_features.csv')}")
    print()
    if all_ok:
        print("STATUS: G2 v2 PASS ✅")
        print()
        print("NEXT: User approval → G3 (attack/defense strength)")
        sys.exit(0)
    else:
        print("STATUS: G2 v2 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
