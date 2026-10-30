#!/usr/bin/env python3
"""
Phase 5 — G3: Attack/Defense Strength + Home/Away Split
========================================================

Input (READ-ONLY):
  data/features/match_features.csv    (G2 output, 40 cols OR already G3, 48 cols)
  data/processed/matches.csv
  data/processed/teams.csv
  data/processed/teams_canonical.csv
  data/processed/season_meta.csv
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE/UPDATE):
  data/features/match_features.csv    (48 cols = G2 40 + 8 new)
  data/reports/phase5_g3_report.txt

New fields (8):
  E. Home/away split (4):
    home_home_scored_avg_before    (home team, hanya saat home)
    home_home_conceded_avg_before
    away_away_scored_avg_before    (away team, hanya saat away)
    away_away_conceded_avg_before
  H. Attack/defense strength (4):
    home_attack_strength
    home_defense_strength
    away_attack_strength
    away_defense_strength

Anti-leakage:
  - Home/away split: match dengan venue sesuai (home_team home / away_team away),
    date < X.date (strict)
  - Strength: derived dari G1/G2 fields (already time-filtered)
  - Spot check 20 samples independent recompute

State-aware write (G2_INPUT -> extend to G3):
  - ABSENT                     -> WROTE
  - schema == G2_FIELDS (40)   -> WROTE_EXTEND_FROM_G2 (backup + write 48)
  - schema == G3_FIELDS (48)   -> SKIP_IDENTICAL or BLOCKED_DIFFERENT
  - other                      -> BLOCKED_UNKNOWN_SCHEMA
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

REPORT_TXT = REPORTS_DIR / "phase5_g3_report.txt"

# Schemas
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

G3_NEW_FIELDS = [
    "home_home_scored_avg_before",
    "home_home_conceded_avg_before",
    "away_away_scored_avg_before",
    "away_away_conceded_avg_before",
    "home_attack_strength",
    "home_defense_strength",
    "away_attack_strength",
    "away_defense_strength",
]

G3_FIELDS = G2_FIELDS + G3_NEW_FIELDS

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
    "home_home_scored_avg_before", "home_home_conceded_avg_before",
    "away_away_scored_avg_before", "away_away_conceded_avg_before",
    "home_attack_strength", "home_defense_strength",
    "away_attack_strength", "away_defense_strength",
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

def structural_equal_csv_for_schema(path, fields, expected_rows, pk_fn):
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
    existing_rows, existing_fields, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
    if err:
        return {"fail": f"match_features.csv read: {err}"}
    if existing_fields not in (G2_FIELDS, G3_FIELDS):
        return {"fail": f"match_features.csv schema unknown: {len(existing_fields)} cols, "
                        f"expected 40 (G2) or 48 (G3)"}

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

    if existing_fields == G2_FIELDS:
        input_state = "G2_INPUT"
    else:
        input_state = "G3_INPUT"

    return {
        "fail": None,
        "existing_rows":    existing_rows,
        "existing_fields":  existing_fields,
        "input_state":      input_state,
        "matches_rows":     matches_rows,
        "eligible_matches": eligible_matches,
        "N_ELIGIBLE":       len(eligible_matches),
    }

# ---------- Build home/away split index ----------

def build_venue_split_index(eligible_matches, inputs):
    """
    For each team_id:
      - home_appearances: sorted list of {date, match_id, gs (as home), gc (as home)}
      - away_appearances: sorted list of {date, match_id, gs (as away), gc (as away)}
    """
    team_by_raw_name = inputs["team_by_raw_name"]
    home_index = defaultdict(list)
    away_index = defaultdict(list)

    for m in eligible_matches:
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name.get(h_raw)
        a_id = team_by_raw_name.get(a_raw)
        if h_id is None or a_id is None:
            raise RuntimeError(f"unknown team in split index: {h_raw}/{a_raw}")
        date = m.get("date", "").strip()
        mid = m.get("match_id", "").strip()
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))

        home_index[h_id].append({
            "date": date, "match_id": mid,
            "gs": fthg, "gc": ftag,
        })
        away_index[a_id].append({
            "date": date, "match_id": mid,
            "gs": ftag, "gc": fthg,
        })

    for tid in home_index:
        home_index[tid].sort(key=lambda r: (r["date"], r["match_id"]))
    for tid in away_index:
        away_index[tid].sort(key=lambda r: (r["date"], r["match_id"]))
    return home_index, away_index

def compute_venue_split_before(index_list, cutoff_date):
    if not index_list:
        return {"matches": 0, "scored_avg": None, "conceded_avg": None}
    lo, hi = 0, len(index_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if index_list[mid]["date"] < cutoff_date:
            lo = mid + 1
        else:
            hi = mid
    selected = index_list[:lo]
    n = len(selected)
    if n == 0:
        return {"matches": 0, "scored_avg": None, "conceded_avg": None}
    sum_gs = sum(r["gs"] for r in selected)
    sum_gc = sum(r["gc"] for r in selected)
    return {
        "matches": n,
        "scored_avg": sum_gs / n,
        "conceded_avg": sum_gc / n,
    }

# ---------- Compute strength ----------

def compute_strength(numerator, denominator):
    """Returns float or None if either is None or denominator is 0."""
    if numerator is None or denominator is None:
        return None
    try:
        num = float(numerator)
        den = float(denominator)
    except (ValueError, TypeError):
        return None
    if den == 0:
        return None
    return num / den

# ---------- Extend features ----------

def extend_features(g2_rows, home_index, away_index):
    out_rows = []
    for r in g2_rows:
        h_id_raw = r.get("home_team", "").strip()
        a_id_raw = r.get("away_team", "").strip()
        date = r.get("date", "").strip()
        # We need team_id, not raw_name
        # g2_rows have home_team/away_team as raw_name. We need team_id via mapping.
        # Actually we can derive from canonical_id? Better to pass team_by_raw_name.
        # For simplicity, use raw_name directly against index keys? No, index keys are team_id.
        # We'll map inside via passed dict.
        pass
    return out_rows

# (Replacing above stub with real implementation)
def extend_features_impl(g2_rows, home_index, away_index, team_by_raw_name):
    out_rows = []
    for r in g2_rows:
        h_raw = r.get("home_team", "").strip()
        a_raw = r.get("away_team", "").strip()
        h_id = team_by_raw_name.get(h_raw)
        a_id = team_by_raw_name.get(a_raw)
        if h_id is None or a_id is None:
            raise RuntimeError(f"unknown team in extend: {h_raw}/{a_raw}")
        date = r.get("date", "").strip()

        h_home = compute_venue_split_before(home_index.get(h_id, []), date)
        a_away = compute_venue_split_before(away_index.get(a_id, []), date)

        new = {f: r.get(f, "") for f in G2_FIELDS}

        new["home_home_scored_avg_before"] = serialize_float(h_home["scored_avg"])
        new["home_home_conceded_avg_before"] = serialize_float(h_home["conceded_avg"])
        new["away_away_scored_avg_before"] = serialize_float(a_away["scored_avg"])
        new["away_away_conceded_avg_before"] = serialize_float(a_away["conceded_avg"])

        # Strength = num / league_avg (Q5=a)
        h_attack = compute_strength(
            try_float(new["home_goals_scored_avg_before"]),
            try_float(new["league_avg_home_goals_before"])
        )
        h_defense = compute_strength(
            try_float(new["home_goals_conceded_avg_before"]),
            try_float(new["league_avg_away_goals_before"])
        )
        a_attack = compute_strength(
            try_float(new["away_goals_scored_avg_before"]),
            try_float(new["league_avg_away_goals_before"])
        )
        a_defense = compute_strength(
            try_float(new["away_goals_conceded_avg_before"]),
            try_float(new["league_avg_home_goals_before"])
        )

        new["home_attack_strength"] = serialize_float(h_attack)
        new["home_defense_strength"] = serialize_float(h_defense)
        new["away_attack_strength"] = serialize_float(a_attack)
        new["away_defense_strength"] = serialize_float(a_defense)

        out_rows.append(new)
    out_rows.sort(key=lambda r: (r["date"], r["match_id"]))
    return out_rows

# ---------- Anti-leakage spot check ----------

def anti_leakage_spot_check(feature_rows, eligible_matches, inputs, sample_n=20):
    team_by_raw_name = inputs["team_by_raw_name"]

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
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()

        # Independent recompute: home team as home, away team as away
        h_home_n = 0; h_home_gs = 0; h_home_gc = 0
        a_away_n = 0; a_away_gs = 0; a_away_gc = 0
        for m2 in eligible_matches:
            d2 = m2.get("date", "").strip()
            if d2 >= date: continue
            h2_raw = m2.get("home_team", "").strip()
            a2_raw = m2.get("away_team", "").strip()
            fthg2 = try_int(m2.get("home_goals")); ftag2 = try_int(m2.get("away_goals"))
            if h2_raw == h_raw:
                h_home_n += 1
                h_home_gs += fthg2; h_home_gc += ftag2
            if a2_raw == a_raw:
                a_away_n += 1
                a_away_gs += ftag2; a_away_gc += fthg2

        # Compare home_home
        exp_scored = h_home_gs / h_home_n if h_home_n > 0 else None
        exp_conceded = h_home_gc / h_home_n if h_home_n > 0 else None
        got_s = try_float(feat["home_home_scored_avg_before"])
        got_c = try_float(feat["home_home_conceded_avg_before"])
        if exp_scored is not None:
            if got_s is None or abs(exp_scored - got_s) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: h_home_scored indep={exp_scored:.6f} feat={got_s}")
            if got_c is None or abs(exp_conceded - got_c) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: h_home_conceded indep={exp_conceded:.6f} feat={got_c}")
        else:
            if got_s is not None or got_c is not None:
                problems.append(f"{mid}: h_home should be empty")

        # Compare away_away
        exp_scored = a_away_gs / a_away_n if a_away_n > 0 else None
        exp_conceded = a_away_gc / a_away_n if a_away_n > 0 else None
        got_s = try_float(feat["away_away_scored_avg_before"])
        got_c = try_float(feat["away_away_conceded_avg_before"])
        if exp_scored is not None:
            if got_s is None or abs(exp_scored - got_s) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: a_away_scored indep={exp_scored:.6f} feat={got_s}")
            if got_c is None or abs(exp_conceded - got_c) > FLOAT_TOL_CSV:
                problems.append(f"{mid}: a_away_conceded indep={exp_conceded:.6f} feat={got_c}")
        else:
            if got_s is not None or got_c is not None:
                problems.append(f"{mid}: a_away should be empty")

        # Strength check: recompute from CSV fields
        for s_field, num_field, den_field in [
            ("home_attack_strength",  "home_goals_scored_avg_before", "league_avg_home_goals_before"),
            ("home_defense_strength", "home_goals_conceded_avg_before", "league_avg_away_goals_before"),
            ("away_attack_strength",  "away_goals_scored_avg_before", "league_avg_away_goals_before"),
            ("away_defense_strength", "away_goals_conceded_avg_before", "league_avg_home_goals_before"),
        ]:
            exp = compute_strength(try_float(feat[num_field]), try_float(feat[den_field]))
            got = try_float(feat[s_field])
            if exp is None:
                if got is not None:
                    problems.append(f"{mid}: {s_field} should be empty but got {got}")
            else:
                if got is None or abs(exp - got) > FLOAT_TOL_CSV:
                    problems.append(f"{mid}: {s_field} indep={exp:.6f} feat={got}")

    return problems, len(sample_ids)

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 5 — G3: ATTACK/DEFENSE STRENGTH + HOME/AWAY SPLIT")
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

    # Prepare g2_rows
    g2_rows = [{f: r.get(f, "") for f in G2_FIELDS} for r in pf["existing_rows"]]

    print("--- BUILD VENUE INDEXES ---")
    home_index, away_index = build_venue_split_index(pf["eligible_matches"], inputs)
    print(f"  home-index teams : {len(home_index)}")
    print(f"  away-index teams : {len(away_index)}")
    print()

    print("--- EXTEND FEATURES ---")
    feature_rows = extend_features_impl(g2_rows, home_index, away_index, inputs["team_by_raw_name"])
    print(f"  rows extended : {len(feature_rows)}")
    print()

    print("--- DETERMINISM ---")
    home_index2, away_index2 = build_venue_split_index(pf["eligible_matches"], inputs)
    feature_rows2 = extend_features_impl(g2_rows, home_index2, away_index2, inputs["team_by_raw_name"])
    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f,"")) for f in fields)
                         for r in sorted(rows, key=lambda x: x["match_id"]))
    det_ok = (ser(feature_rows, G3_FIELDS) == ser(feature_rows2, G3_FIELDS))
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

    # ---------- WRITE (state-aware) ----------
    print("--- WRITE (idempotent, state-aware) ---")
    def pk_fn(r): return r["match_id"]
    actions = {}

    if not MATCH_FEATURES_CSV.exists():
        write_csv_lf(MATCH_FEATURES_CSV, G3_FIELDS, feature_rows, pk=pk_fn)
        actions["match_features.csv"] = "WROTE"
    else:
        existing_rows_check, existing_fields_check, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
        if err:
            print(f"  [FAIL] cannot re-read: {err}"); sys.exit(1)

        if existing_fields_check == G2_FIELDS:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = BACKUPS_DIR / f"phase5_G3_match_features_{ts}.csv"
            shutil.copy2(MATCH_FEATURES_CSV, backup)
            write_csv_lf(MATCH_FEATURES_CSV, G3_FIELDS, feature_rows, pk=pk_fn)
            actions["match_features.csv"] = "WROTE_EXTEND_FROM_G2"
            actions["backup"] = backup.name
        elif existing_fields_check == G3_FIELDS:
            eq, reason = structural_equal_csv_for_schema(
                MATCH_FEATURES_CSV, G3_FIELDS, feature_rows, pk_fn
            )
            if eq:
                actions["match_features.csv"] = "SKIP_IDENTICAL"
            else:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = BACKUPS_DIR / f"phase5_G3_match_features_{ts}.csv"
                shutil.copy2(MATCH_FEATURES_CSV, backup)
                print(f"  [BLOCKED_DIFFERENT] {reason}")
                print(f"  [BACKUP] {backup.name}")
                sys.exit(1)
        else:
            print(f"  [FAIL] unknown schema: {len(existing_fields_check)} cols")
            sys.exit(1)

    for k, v in actions.items(): print(f"  [{v:32s}] {k}")
    print()

    print("--- VERIFY POST-WRITE ---")
    ok, reason = structural_equal_csv_for_schema(MATCH_FEATURES_CSV, G3_FIELDS, feature_rows, pk_fn)
    if not ok:
        print(f"  [FAIL] {reason}"); sys.exit(1)
    print(f"  [OK] CSV matches in-memory structurally (48 cols)")
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
        "# Phase 5 — G3 Report (Attack/Defense Strength + Home/Away Split)",
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
        f"columns (G3 total)  : {len(G3_FIELDS)}",
        f"  G1 fields         : {len(G1_FIELDS)}",
        f"  G2 new fields     : {len(G2_NEW_FIELDS)}",
        f"  G3 new fields     : {len(G3_NEW_FIELDS)}",
        f"write action        : {actions.get('match_features.csv')}",
        f"output SHA-256      : {sha256_file(MATCH_FEATURES_CSV)}",
        "",
        "## Anti-leakage",
        "- Home/away split: match venue-specific, date < X.date (strict)",
        "- Strength: derived from G1/G2 fields (already time-filtered)",
        "- Spot check: 20 samples verified",
        "",
        "## Strength normalization (Q5=a)",
        "home_attack_strength   = home_goals_scored_avg_before / league_avg_home_goals_before",
        "home_defense_strength  = home_goals_conceded_avg_before / league_avg_away_goals_before",
        "away_attack_strength   = away_goals_scored_avg_before / league_avg_away_goals_before",
        "away_defense_strength  = away_goals_conceded_avg_before / league_avg_home_goals_before",
        "",
        "## Determinism",
        "rebuild 2x: identical",
        "",
        "STATUS: G3 PASS",
    ]
    write_text_lf(REPORT_TXT, "\n".join(report_lines) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("G3 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("Pre-flight PASS",                    True),
        ("N_ELIGIBLE > 0",                     pf["N_ELIGIBLE"] > 0),
        ("Row count == N_ELIGIBLE",            len(feature_rows) == pf["N_ELIGIBLE"]),
        ("Columns == 48",                      len(G3_FIELDS) == 48),
        ("G1 + G2 columns preserved",
            all(f in feature_rows[0] for f in G2_FIELDS)),
        ("Anti-leakage spot check PASS",       len(problems) == 0),
        ("Determinism 2x rebuild",             det_ok),
        ("All Phase 2/3 inputs SHA-256 unchanged",
            all(pre_sha[k] == post_sha[k] for k in post_sha)),
        ("match_features.csv valid state",
            actions.get("match_features.csv") in
            ("WROTE","WROTE_EXTEND_FROM_G2","SKIP_IDENTICAL")),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  rows     : {len(feature_rows)}")
    print(f"  columns  : {len(G3_FIELDS)}")
    print(f"  write    : {actions.get('match_features.csv')}")
    print()
    if all_ok:
        print("STATUS: G3 PASS ✅")
        print()
        print("NEXT: User approval → G4 (anti-leakage full audit)")
        sys.exit(0)
    else:
        print("STATUS: G3 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
