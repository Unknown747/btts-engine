#!/usr/bin/env python3
"""
Phase 4 — F1: Team Statistics + BTTS Statistics (v2)
=====================================================

Fix v1 → v2:
  - v1 assumed matches.home_team/away_team contain team_id (md5:xxx).
    Actually they contain raw_name. Fix: map raw_name -> team_id via teams.csv.
  - New pre-flight V-F1-23: distinct(home_team ∪ away_team) ⊆ teams.raw_name,
    fail-fast exit 1 if any unknown team detected.
  - Independent validation function reads teams.csv fresh from disk for mapping
    (no shared state with build path).

Input (READ-ONLY):
  data/processed/matches.csv
  data/processed/teams.csv
  data/processed/teams_canonical.csv
  data/processed/leagues.csv
  data/processed/season_meta.csv
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE):
  data/processed/team_statistics.csv          (25 kolom)
  data/processed/btts_statistics.csv          (19 kolom)
  data/reports/phase4_f1_report.txt

Rules (Plan v2.8 + v2 fix):
  - Tahap A: COMPLETE FT integrity scan SELURUH matches.csv (R4)
  - Tahap B: Klasifikasi partisi utama + rekonsiliasi
  - V-F1-23: distinct(home_team ∪ away_team) ⊆ teams.raw_name, fail-fast
  - eligible_match = COMPLETE or INCOMPLETE_STATS with FT valid (Q2)
  - Multi-league (P1): FAIL exit 1
  - Validasi independen: baca ulang matches.csv AND teams.csv dari disk
  - Float precision: 6 desimal CSV, toleransi 1e-6 CSV / 1e-12 in-memory (R6)
  - Idempotency: SKIP_IDENTICAL / BLOCKED_DIFFERENT
  - Determinism: rebuild 2x byte-identical
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

# Inputs
MATCHES_CSV          = PROCESSED_DIR / "matches.csv"
TEAMS_CSV            = PROCESSED_DIR / "teams.csv"
TEAMS_CANONICAL_CSV  = PROCESSED_DIR / "teams_canonical.csv"
LEAGUES_CSV          = PROCESSED_DIR / "leagues.csv"
SEASON_META_CSV      = PROCESSED_DIR / "season_meta.csv"
CANONICAL_TEAMS_CSV  = MAPPINGS_DIR  / "canonical_teams.csv"
DEPRECATED_MAP_CSV   = MAPPINGS_DIR  / "deprecated_canonical_map.csv"

# Outputs
TEAM_STATS_CSV       = PROCESSED_DIR / "team_statistics.csv"
BTTS_STATS_CSV       = PROCESSED_DIR / "btts_statistics.csv"
REPORT_TXT           = REPORTS_DIR  / "phase4_f1_report.txt"

# Schemas
TEAM_STATS_FIELDS = [
    "team_id", "canonical_id", "season", "league_code",
    "matches_played", "home_matches", "away_matches",
    "goals_scored", "goals_conceded",
    "home_goals_scored", "home_goals_conceded",
    "away_goals_scored", "away_goals_conceded",
    "avg_goals_scored", "avg_goals_conceded",
    "home_avg_scored", "home_avg_conceded",
    "away_avg_scored", "away_avg_conceded",
    "scoring_rate", "conceding_rate",
    "clean_sheet_count", "failed_to_score_count",
    "clean_sheet_rate", "failed_to_score_rate",
]

BTTS_STATS_FIELDS = [
    "team_id", "canonical_id", "season", "league_code",
    "total_matches",
    "btts_yes_count", "btts_no_count",
    "btts_yes_percentage", "btts_no_percentage",
    "home_btts_count", "home_btts_percentage",
    "away_btts_count", "away_btts_percentage",
    "last_5_btts_count", "last_5_btts_percentage",
    "last_10_btts_count", "last_10_btts_percentage",
    "last_15_btts_count", "last_15_btts_percentage",
]

# Locks
RECENT_WINDOWS = [5, 10, 15]
FLOAT_PRECISION = 6
FLOAT_TOL_CSV = 1e-6
FLOAT_TOL_MEM = 1e-12

VALID_STATUS_COMPLETE   = "COMPLETE"
VALID_STATUS_INCOMPLETE = "INCOMPLETE_STATS"
VALID_STATUS_INVALID    = "INVALID"

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _norm_field(s: str) -> str:
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
    if s is None:
        return None
    s = str(s).strip()
    if s == "" or s.lower() in ("nan", "none", "null", "-"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None

def is_valid_goals_int(v):
    return isinstance(v, int) and v >= 0

def serialize_float(v):
    if v is None:
        return ""
    s = f"{round(float(v), FLOAT_PRECISION):.{FLOAT_PRECISION}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s != "" else "0"

def floats_equal(a, b):
    a_empty = (a is None or str(a).strip() == "")
    b_empty = (b is None or str(b).strip() == "")
    if a_empty and b_empty:
        return True
    if a_empty or b_empty:
        return False
    try:
        return abs(float(a) - float(b)) <= FLOAT_TOL_CSV
    except (ValueError, TypeError):
        return False

def structural_equal_csv(path: Path, fields: list, expected_rows: list, pk_fn):
    if not path.exists():
        return False, "file_missing"
    got_rows, got_fields, err = read_csv_lf_safe(path)
    if err:
        return False, f"read_error:{err}"
    if got_fields != fields:
        return False, "fields_mismatch"
    if len(got_rows) != len(expected_rows):
        return False, f"row_count: got={len(got_rows)} exp={len(expected_rows)}"

    got_sorted = sorted(got_rows, key=pk_fn)
    exp_sorted = sorted(expected_rows, key=pk_fn)

    float_fields = {
        "avg_goals_scored", "avg_goals_conceded",
        "home_avg_scored", "home_avg_conceded",
        "away_avg_scored", "away_avg_conceded",
        "scoring_rate", "conceding_rate",
        "clean_sheet_rate", "failed_to_score_rate",
        "btts_yes_percentage", "btts_no_percentage",
        "home_btts_percentage", "away_btts_percentage",
        "last_5_btts_percentage", "last_10_btts_percentage",
        "last_15_btts_percentage",
    }

    for i, (gr, er) in enumerate(zip(got_sorted, exp_sorted)):
        for f in fields:
            gv = gr.get(f, "")
            ev = er.get(f, "")
            if f in float_fields:
                if not floats_equal(gv, ev):
                    return False, f"row{i} float {f}: got={gv!r} exp={ev!r}"
            else:
                if str(gv) != str(ev):
                    return False, f"row{i} field {f}: got={gv!r} exp={ev!r}"
    return True, "identical"

# ---------- Load inputs ----------

def load_inputs():
    teams_rows, teams_fields, err = read_csv_lf_safe(TEAMS_CSV)
    if err:
        raise RuntimeError(f"teams.csv: {err}")
    leagues_rows, leagues_fields, err = read_csv_lf_safe(LEAGUES_CSV)
    if err:
        raise RuntimeError(f"leagues.csv: {err}")
    season_meta_rows, season_meta_fields, err = read_csv_lf_safe(SEASON_META_CSV)
    if err:
        raise RuntimeError(f"season_meta.csv: {err}")
    teams_canon_rows, teams_canon_fields, err = read_csv_lf_safe(TEAMS_CANONICAL_CSV)
    if err:
        raise RuntimeError(f"teams_canonical.csv: {err}")
    canonical_rows, canonical_fields, err = read_csv_lf_safe(CANONICAL_TEAMS_CSV)
    if err:
        raise RuntimeError(f"canonical_teams.csv: {err}")

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

    # raw_name -> team_id mapping (FIX v2)
    team_by_raw_name = {}
    for r in teams_rows:
        team_by_raw_name[r["raw_name"]] = r["team_id"]
    if not team_by_raw_name:
        raise RuntimeError("teams.csv: empty raw_name mapping")

    canonical_status = {}
    for r in canonical_rows:
        canonical_status[r["canonical_id"]] = r.get("status", "ACTIVE")

    dep_redirect = {}
    for r in deprecated_rows:
        dep_redirect[r["deprecated_canonical_id"]] = r["superseded_by"]

    tc_by_team = {}
    for r in teams_canon_rows:
        tc_by_team[r["team_id"]] = r

    canonical_by_id = {r["canonical_id"]: r for r in canonical_rows}

    return {
        "teams_rows":         teams_rows,
        "leagues_rows":       leagues_rows,
        "season_meta_rows":   season_meta_rows,
        "teams_canon_rows":   teams_canon_rows,
        "canonical_rows":     canonical_rows,
        "deprecated_rows":    deprecated_rows,
        "eligible_seasons":   eligible_seasons,
        "team_by_raw_name":   team_by_raw_name,
        "canonical_status":   canonical_status,
        "dep_redirect":       dep_redirect,
        "tc_by_team":         tc_by_team,
        "canonical_by_id":    canonical_by_id,
    }

def resolve_active(cid, canonical_status, dep_redirect, max_depth=32):
    seen = set()
    current = cid
    for _ in range(max_depth):
        if current in seen:
            return None
        seen.add(current)
        status = canonical_status.get(current)
        if status == "ACTIVE":
            return current
        if status != "DEPRECATED":
            return None
        nxt = dep_redirect.get(current)
        if nxt is None:
            return None
        current = nxt
    return None

# ---------- Pre-flight V-F1-23: team whitelist ----------

def preflight_team_whitelist(matches_rows, team_by_raw_name):
    """
    V-F1-23: distinct(home_team ∪ away_team) ⊆ teams.raw_name.
    Returns (ok: bool, unknown: list[str]).
    """
    seen = set()
    for m in matches_rows:
        h = m.get("home_team", "").strip()
        a = m.get("away_team", "").strip()
        if h: seen.add(h)
        if a: seen.add(a)
    unknown = sorted(t for t in seen if t not in team_by_raw_name)
    return (len(unknown) == 0, unknown)

# ---------- Pre-flight Tahap A + Tahap B ----------

def preflight_classify(inputs):
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err:
        return {"fail": f"matches.csv read error: {err}"}

    eligible_seasons = inputs["eligible_seasons"]
    N_total = len(matches_rows)

    # Tahap A: COMPLETE FT integrity — scan ALL rows
    complete_ft_violations = []
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        if ds != VALID_STATUS_COMPLETE:
            continue
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
            complete_ft_violations.append({
                "match_id": m.get("match_id", ""),
                "season": m.get("season", ""),
                "data_status": ds,
                "FTHG": m.get("home_goals", ""),
                "FTAG": m.get("away_goals", ""),
            })

    if complete_ft_violations:
        return {
            "fail": "Tahap A FAIL — COMPLETE with invalid FT",
            "violations": complete_ft_violations,
        }

    # Tahap B: mutually exclusive partition
    N_SEASON_EXCLUDED = 0
    N_STATUS_UNKNOWN = 0
    N_INVALID_STATUS = 0
    N_INCOMPLETE_FT_INVALID = 0
    N_ELIGIBLE = 0
    N_eligible_complete = 0
    N_eligible_incomplete = 0
    N_per_season = defaultdict(int)
    N_eligible_per_season = defaultdict(int)
    N_eligible_complete_per_season = defaultdict(int)
    N_eligible_incomplete_per_season = defaultdict(int)
    eligible_matches = []

    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        season_int = try_int(m.get("season"))
        season_in_eligible = (season_int is not None and season_int in eligible_seasons)

        if season_int is None:
            N_per_season["unknown"] += 1
        else:
            N_per_season[season_int] += 1

        if not season_in_eligible:
            N_SEASON_EXCLUDED += 1
            continue

        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE, VALID_STATUS_INVALID):
            N_STATUS_UNKNOWN += 1
            continue

        if ds == VALID_STATUS_INVALID:
            N_INVALID_STATUS += 1
            continue

        if ds == VALID_STATUS_COMPLETE:
            N_ELIGIBLE += 1
            N_eligible_complete += 1
            N_eligible_per_season[season_int] += 1
            N_eligible_complete_per_season[season_int] += 1
            eligible_matches.append(m)
            continue

        if ds == VALID_STATUS_INCOMPLETE:
            fthg = try_int(m.get("home_goals"))
            ftag = try_int(m.get("away_goals"))
            if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
                N_INCOMPLETE_FT_INVALID += 1
                continue
            N_ELIGIBLE += 1
            N_eligible_incomplete += 1
            N_eligible_per_season[season_int] += 1
            N_eligible_incomplete_per_season[season_int] += 1
            eligible_matches.append(m)
            continue

    partition_sum = (
        N_SEASON_EXCLUDED
        + N_STATUS_UNKNOWN
        + N_INVALID_STATUS
        + N_INCOMPLETE_FT_INVALID
        + N_ELIGIBLE
    )
    reconciliation_ok = (partition_sum == N_total)

    return {
        "fail": None,
        "matches_rows":            matches_rows,
        "eligible_matches":        eligible_matches,
        "N_total":                 N_total,
        "N_SEASON_EXCLUDED":       N_SEASON_EXCLUDED,
        "N_STATUS_UNKNOWN":        N_STATUS_UNKNOWN,
        "N_INVALID_STATUS":        N_INVALID_STATUS,
        "N_INCOMPLETE_FT_INVALID": N_INCOMPLETE_FT_INVALID,
        "N_ELIGIBLE":              N_ELIGIBLE,
        "N_eligible_complete":     N_eligible_complete,
        "N_eligible_incomplete":   N_eligible_incomplete,
        "N_per_season":            dict(N_per_season),
        "N_eligible_per_season":   dict(N_eligible_per_season),
        "N_eligible_complete_per_season": dict(N_eligible_complete_per_season),
        "N_eligible_incomplete_per_season": dict(N_eligible_incomplete_per_season),
        "reconciliation_ok":       reconciliation_ok,
    }

# ---------- Build (in-memory) ----------

def build_team_stats(eligible_matches, inputs):
    tc_by_team = inputs["tc_by_team"]
    canonical_status = inputs["canonical_status"]
    dep_redirect = inputs["dep_redirect"]
    team_by_raw_name = inputs["team_by_raw_name"]
    canonical_by_id = inputs["canonical_by_id"]

    acc = defaultdict(lambda: {
        "league_codes": set(),
        "matches_played": 0,
        "home_matches": 0,
        "away_matches": 0,
        "goals_scored": 0,
        "goals_conceded": 0,
        "home_goals_scored": 0,
        "home_goals_conceded": 0,
        "away_goals_scored": 0,
        "away_goals_conceded": 0,
        "clean_sheet_count": 0,
        "failed_to_score_count": 0,
        "btts_yes_count": 0,
        "btts_no_count": 0,
        "home_btts_count": 0,
        "away_btts_count": 0,
        "recent": [],
    })

    for m in eligible_matches:
        home_raw = m.get("home_team", "").strip()
        away_raw = m.get("away_team", "").strip()
        # FIX v2: map raw_name -> team_id
        home_id = team_by_raw_name.get(home_raw)
        away_id = team_by_raw_name.get(away_raw)
        if home_id is None:
            raise RuntimeError(f"unknown home_team {home_raw!r} in matches.csv (V-F1-23)")
        if away_id is None:
            raise RuntimeError(f"unknown away_team {away_raw!r} in matches.csv (V-F1-23)")

        season = try_int(m.get("season"))
        league = m.get("league_code", "").strip()
        date = m.get("date", "").strip()
        match_id = m.get("match_id", "").strip()
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0

        if season is None:
            continue

        hk = (home_id, season)
        acc[hk]["league_codes"].add(league)
        acc[hk]["matches_played"] += 1
        acc[hk]["home_matches"] += 1
        acc[hk]["goals_scored"] += fthg
        acc[hk]["goals_conceded"] += ftag
        acc[hk]["home_goals_scored"] += fthg
        acc[hk]["home_goals_conceded"] += ftag
        if ftag == 0: acc[hk]["clean_sheet_count"] += 1
        if fthg == 0: acc[hk]["failed_to_score_count"] += 1
        if btts:
            acc[hk]["btts_yes_count"] += 1
            acc[hk]["home_btts_count"] += 1
        else:
            acc[hk]["btts_no_count"] += 1
        acc[hk]["recent"].append((date, match_id, btts))

        ak = (away_id, season)
        acc[ak]["league_codes"].add(league)
        acc[ak]["matches_played"] += 1
        acc[ak]["away_matches"] += 1
        acc[ak]["goals_scored"] += ftag
        acc[ak]["goals_conceded"] += fthg
        acc[ak]["away_goals_scored"] += ftag
        acc[ak]["away_goals_conceded"] += fthg
        if fthg == 0: acc[ak]["clean_sheet_count"] += 1
        if ftag == 0: acc[ak]["failed_to_score_count"] += 1
        if btts:
            acc[ak]["btts_yes_count"] += 1
            acc[ak]["away_btts_count"] += 1
        else:
            acc[ak]["btts_no_count"] += 1
        acc[ak]["recent"].append((date, match_id, btts))

    multi_league = [
        (k[0], k[1], sorted(v["league_codes"]))
        for k, v in acc.items() if len(v["league_codes"]) > 1
    ]
    if multi_league:
        raise RuntimeError(
            "P1 multi-league violation: "
            + str(multi_league[:10])
            + (f" (+{len(multi_league)-10} more)" if len(multi_league) > 10 else "")
        )

    team_stats_rows = []
    btts_stats_rows = []
    for (team_id, season), v in sorted(acc.items(), key=lambda x: (x[0][0], x[0][1])):
        tc = tc_by_team.get(team_id)
        if tc is None:
            raise RuntimeError(f"team {team_id} not in teams_canonical")
        cid_raw = tc["canonical_id"]
        cid = resolve_active(cid_raw, canonical_status, dep_redirect)
        if cid is None:
            raise RuntimeError(f"cannot resolve canonical for team {team_id} cid={cid_raw}")
        canon_name = canonical_by_id[cid]["canonical_name"]

        league = sorted(v["league_codes"])[0]
        mp = v["matches_played"]
        gs = v["goals_scored"]; gc = v["goals_conceded"]
        cs = v["clean_sheet_count"]; fts = v["failed_to_score_count"]

        avg_gs = gs / mp if mp > 0 else None
        avg_gc = gc / mp if mp > 0 else None
        h_avg_s = v["home_goals_scored"] / v["home_matches"] if v["home_matches"] > 0 else None
        h_avg_c = v["home_goals_conceded"] / v["home_matches"] if v["home_matches"] > 0 else None
        a_avg_s = v["away_goals_scored"] / v["away_matches"] if v["away_matches"] > 0 else None
        a_avg_c = v["away_goals_conceded"] / v["away_matches"] if v["away_matches"] > 0 else None
        scoring_rate = (mp - fts) / mp if mp > 0 else None
        conceding_rate = (mp - cs) / mp if mp > 0 else None
        cs_rate = cs / mp if mp > 0 else None
        fts_rate = fts / mp if mp > 0 else None

        team_stats_rows.append({
            "team_id": team_id, "canonical_id": cid, "season": season,
            "league_code": league,
            "matches_played": mp,
            "home_matches": v["home_matches"], "away_matches": v["away_matches"],
            "goals_scored": gs, "goals_conceded": gc,
            "home_goals_scored": v["home_goals_scored"],
            "home_goals_conceded": v["home_goals_conceded"],
            "away_goals_scored": v["away_goals_scored"],
            "away_goals_conceded": v["away_goals_conceded"],
            "avg_goals_scored": serialize_float(avg_gs),
            "avg_goals_conceded": serialize_float(avg_gc),
            "home_avg_scored": serialize_float(h_avg_s),
            "home_avg_conceded": serialize_float(h_avg_c),
            "away_avg_scored": serialize_float(a_avg_s),
            "away_avg_conceded": serialize_float(a_avg_c),
            "scoring_rate": serialize_float(scoring_rate),
            "conceding_rate": serialize_float(conceding_rate),
            "clean_sheet_count": cs, "failed_to_score_count": fts,
            "clean_sheet_rate": serialize_float(cs_rate),
            "failed_to_score_rate": serialize_float(fts_rate),
        })

        total = mp
        yes = v["btts_yes_count"]; no = v["btts_no_count"]
        assert yes + no == total, f"btts count mismatch {team_id} {season}"
        btts_yes_pct = yes / total if total > 0 else None
        btts_no_pct = no / total if total > 0 else None
        h_btts_pct = v["home_btts_count"] / v["home_matches"] if v["home_matches"] > 0 else None
        a_btts_pct = v["away_btts_count"] / v["away_matches"] if v["away_matches"] > 0 else None

        recent_sorted = sorted(v["recent"], key=lambda t: (t[0], t[1]))
        recent_vals = {}
        for N in RECENT_WINDOWS:
            if total >= N:
                last_n = recent_sorted[-N:]
                c = sum(b for _, _, b in last_n)
                recent_vals[N] = (c, c / N)
            else:
                recent_vals[N] = (None, None)

        btts_stats_rows.append({
            "team_id": team_id, "canonical_id": cid, "season": season,
            "league_code": league,
            "total_matches": total,
            "btts_yes_count": yes, "btts_no_count": no,
            "btts_yes_percentage": serialize_float(btts_yes_pct),
            "btts_no_percentage": serialize_float(btts_no_pct),
            "home_btts_count": v["home_btts_count"],
            "home_btts_percentage": serialize_float(h_btts_pct),
            "away_btts_count": v["away_btts_count"],
            "away_btts_percentage": serialize_float(a_btts_pct),
            "last_5_btts_count": "" if recent_vals[5][0] is None else recent_vals[5][0],
            "last_5_btts_percentage": "" if recent_vals[5][1] is None else serialize_float(recent_vals[5][1]),
            "last_10_btts_count": "" if recent_vals[10][0] is None else recent_vals[10][0],
            "last_10_btts_percentage": "" if recent_vals[10][1] is None else serialize_float(recent_vals[10][1]),
            "last_15_btts_count": "" if recent_vals[15][0] is None else recent_vals[15][0],
            "last_15_btts_percentage": "" if recent_vals[15][1] is None else serialize_float(recent_vals[15][1]),
        })

    team_stats_rows.sort(key=lambda r: (r["team_id"], int(r["season"])))
    btts_stats_rows.sort(key=lambda r: (r["team_id"], int(r["season"])))
    return team_stats_rows, btts_stats_rows

# ---------- Independent validation (reads disk fresh) ----------

def validate_team_independent(team_id, season, eligible_seasons):
    """
    Reads matches.csv AND teams.csv from disk fresh.
    Applies same eligibility filter + team mapping.
    Does NOT use any build state.
    """
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err:
        raise RuntimeError(f"validate_team_independent matches read: {err}")
    teams_rows_fresh, _, err = read_csv_lf_safe(TEAMS_CSV)
    if err:
        raise RuntimeError(f"validate_team_independent teams read: {err}")
    team_by_raw = {r["raw_name"]: r["team_id"] for r in teams_rows_fresh}

    gs = gc = n_eligible = home_n = away_n = cs = fts = 0
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s != season: continue
        if s not in eligible_seasons: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals"))
        ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue

        hid = team_by_raw.get(m.get("home_team", "").strip())
        aid = team_by_raw.get(m.get("away_team", "").strip())
        if hid == team_id:
            n_eligible += 1; home_n += 1
            gs += fthg; gc += ftag
            if ftag == 0: cs += 1
            if fthg == 0: fts += 1
        if aid == team_id:
            n_eligible += 1; away_n += 1
            gs += ftag; gc += fthg
            if fthg == 0: cs += 1
            if ftag == 0: fts += 1
    return gs, gc, n_eligible, home_n, away_n, cs, fts

# ---------- Report ----------

def build_report(preflight, team_stats_rows, btts_stats_rows, actions, det_msg,
                 validation_notes, vf123_ok, vf123_unknown):
    lines = []
    lines.append("# Phase 4 — F1 Report v2 (Team Statistics + BTTS Statistics)")
    lines.append("Phase 4 = end-of-season snapshot; anti-leakage handled in Phase 5")
    lines.append("")
    lines.append("## Input (read-only)")
    for k, path in [
        ("matches.csv", MATCHES_CSV), ("teams.csv", TEAMS_CSV),
        ("teams_canonical.csv", TEAMS_CANONICAL_CSV),
        ("leagues.csv", LEAGUES_CSV), ("season_meta.csv", SEASON_META_CSV),
        ("canonical_teams.csv", CANONICAL_TEAMS_CSV),
        ("deprecated_canonical_map.csv", DEPRECATED_MAP_CSV),
    ]:
        lines.append(f"{k}: SHA-256={sha256_file(path)}")
    lines.append("")

    lines.append("## V-F1-23: team whitelist")
    if vf123_ok:
        lines.append("PASS — distinct(home_team ∪ away_team) ⊆ teams.raw_name")
    else:
        lines.append(f"FAIL — unknown teams: {vf123_unknown}")
    lines.append("")

    lines.append("## Tahap A: COMPLETE FT integrity")
    if preflight.get("fail"):
        lines.append(f"FAIL: {preflight['fail']}")
        lines.append(f"Violations: {preflight.get('violations', [])}")
    else:
        lines.append("PASS — all COMPLETE rows have valid FT")
    lines.append("")

    lines.append("## Tahap B: Partisi utama (mutually exclusive)")
    lines.append(f"N_total                  : {preflight.get('N_total')}")
    lines.append(f"N_SEASON_EXCLUDED        : {preflight.get('N_SEASON_EXCLUDED')}")
    lines.append(f"N_STATUS_UNKNOWN         : {preflight.get('N_STATUS_UNKNOWN')}")
    lines.append(f"N_INVALID_STATUS         : {preflight.get('N_INVALID_STATUS')}")
    lines.append(f"N_INCOMPLETE_FT_INVALID  : {preflight.get('N_INCOMPLETE_FT_INVALID')}")
    lines.append(f"N_ELIGIBLE               : {preflight.get('N_ELIGIBLE')}")
    lines.append(f"reconciliation_ok        : {preflight.get('reconciliation_ok')}")
    lines.append("")

    lines.append("## Dimensi tambahan (NOT partisi utama)")
    lines.append(f"N_eligible_complete      : {preflight.get('N_eligible_complete')}")
    lines.append(f"N_eligible_incomplete    : {preflight.get('N_eligible_incomplete')}")
    lines.append(f"N_per_season (all rows)  : {preflight.get('N_per_season')}")
    lines.append(f"N_eligible_per_season    : {preflight.get('N_eligible_per_season')}")
    lines.append("")

    lines.append("## Output row counts")
    lines.append(f"team_statistics rows     : {len(team_stats_rows)}")
    lines.append(f"btts_statistics rows     : {len(btts_stats_rows)}")
    lines.append("")

    lines.append("## Determinism")
    lines.append(f"result: {det_msg}")
    lines.append("")

    lines.append("## Idempotency")
    for k, v in sorted(actions.items()):
        lines.append(f"{k:32s} : {v}")
    lines.append("")

    lines.append("## Validation notes")
    for n in validation_notes:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("## Float precision")
    lines.append(f"CSV serialization: round-half-even to {FLOAT_PRECISION} decimals, strip zeros")
    lines.append(f"CSV tolerance    : {FLOAT_TOL_CSV}")
    lines.append(f"In-memory tolerance: {FLOAT_TOL_MEM}")
    lines.append("")
    lines.append("STATUS: F1 v2 PASS")
    return "\n".join(lines) + "\n"

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 4 — F1 v2: TEAM + BTTS STATISTICS")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print()

    for p in (MATCHES_CSV, TEAMS_CSV, TEAMS_CANONICAL_CSV, LEAGUES_CSV,
              SEASON_META_CSV, CANONICAL_TEAMS_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}")
            sys.exit(1)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    pre_sha = {
        "matches.csv": sha256_file(MATCHES_CSV),
        "teams.csv": sha256_file(TEAMS_CSV),
        "teams_canonical.csv": sha256_file(TEAMS_CANONICAL_CSV),
        "leagues.csv": sha256_file(LEAGUES_CSV),
        "season_meta.csv": sha256_file(SEASON_META_CSV),
        "canonical_teams.csv": sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }

    # ---- Load ----
    print("--- LOAD INPUTS ---")
    inputs = load_inputs()
    print(f"  eligible_seasons   : {sorted(inputs['eligible_seasons'])}")
    print(f"  teams raw_name map : {len(inputs['team_by_raw_name'])} entries")
    print()

    # ---- V-F1-23 pre-flight ----
    print("--- V-F1-23: TEAM WHITELIST ---")
    matches_rows_raw, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err:
        print(f"  [FAIL] matches.csv read: {err}")
        sys.exit(1)
    vf123_ok, vf123_unknown = preflight_team_whitelist(matches_rows_raw, inputs["team_by_raw_name"])
    if not vf123_ok:
        print(f"  [FAIL] {len(vf123_unknown)} unknown team(s) in matches.csv:")
        for t in vf123_unknown[:20]:
            print(f"    - {t!r}")
        print(f"  STOP — data anomaly, tidak boleh lanjut.")
        sys.exit(1)
    print(f"  [OK] all teams in matches.csv found in teams.csv")
    print()

    # ---- Pre-flight Tahap A + B ----
    print("--- PRE-FLIGHT (Tahap A + B) ---")
    pf = preflight_classify(inputs)
    if pf.get("fail"):
        print(f"  [FAIL] {pf['fail']}")
        if pf.get("violations"):
            for v in pf["violations"][:10]:
                print(f"    {v}")
        sys.exit(1)
    print(f"  N_total         : {pf['N_total']}")
    print(f"  N_ELIGIBLE      : {pf['N_ELIGIBLE']}")
    print(f"  reconciliation  : {pf['reconciliation_ok']}")
    if not pf["reconciliation_ok"]:
        print("  [FAIL] partition reconciliation failed")
        sys.exit(1)
    print(f"  [OK] Tahap A PASS, Tahap B balanced")
    print()

    # ---- Build ----
    print("--- BUILD ---")
    try:
        team_stats_rows, btts_stats_rows = build_team_stats(pf["eligible_matches"], inputs)
    except RuntimeError as e:
        print(f"  [FAIL] {e}")
        sys.exit(1)
    print(f"  team_statistics : {len(team_stats_rows)} rows")
    print(f"  btts_statistics : {len(btts_stats_rows)} rows")
    print()

    # ---- Determinism ----
    print("--- DETERMINISM ---")
    try:
        ts2, bs2 = build_team_stats(pf["eligible_matches"], inputs)
    except RuntimeError as e:
        print(f"  [FAIL] {e}")
        sys.exit(1)

    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f, "")) for f in fields)
                         for r in sorted(rows, key=lambda x: (x["team_id"], int(x["season"]))))
    det_ok = (ser(team_stats_rows, TEAM_STATS_FIELDS) == ser(ts2, TEAM_STATS_FIELDS)
              and ser(btts_stats_rows, BTTS_STATS_FIELDS) == ser(bs2, BTTS_STATS_FIELDS))
    if not det_ok:
        print("  [FAIL] non-deterministic rebuild")
        sys.exit(1)
    det_msg = "PASS — 2x rebuild identical"
    print(f"  [OK] {det_msg}")
    print()

    # ---- Validation ----
    print("--- VALIDATION ---")
    validation_notes = []

    # V-F1-5c
    eligible_pairs = set()
    for m in pf["eligible_matches"]:
        s = try_int(m["season"])
        h = m["home_team"].strip(); a = m["away_team"].strip()
        if h in inputs["team_by_raw_name"]:
            eligible_pairs.add((inputs["team_by_raw_name"][h], s))
        if a in inputs["team_by_raw_name"]:
            eligible_pairs.add((inputs["team_by_raw_name"][a], s))
    distinct_pairs = set((r["team_id"], int(r["season"])) for r in team_stats_rows)
    v_f1_5c_ok = (distinct_pairs == eligible_pairs)
    if not v_f1_5c_ok:
        print(f"  [FAIL] V-F1-5c distinct (team_id, season) mismatch")
        sys.exit(1)
    validation_notes.append("V-F1-5c distinct (team_id, season) match: PASS")

    # V-F1-5a
    sum_mp = sum(int(r["matches_played"]) for r in team_stats_rows)
    v_f1_5a_ok = (sum_mp == 2 * pf["N_ELIGIBLE"])
    if not v_f1_5a_ok:
        print(f"  [FAIL] V-F1-5a: sum={sum_mp}, expected {2*pf['N_ELIGIBLE']}")
        sys.exit(1)
    validation_notes.append(f"V-F1-5a SUM(matches_played)==2*N_ELIGIBLE: PASS ({sum_mp})")

    # V-F1-5b
    per_season_sum = defaultdict(int)
    for r in team_stats_rows:
        per_season_sum[int(r["season"])] += int(r["matches_played"])
    v_f1_5b_ok = all(
        per_season_sum[s] == 2 * pf["N_eligible_per_season"].get(s, 0)
        for s in inputs["eligible_seasons"]
    )
    if not v_f1_5b_ok:
        print(f"  [FAIL] V-F1-5b per-season mismatch")
        sys.exit(1)
    validation_notes.append("V-F1-5b per-season sum: PASS")

    # V-F1-4
    for r in team_stats_rows:
        if int(r["matches_played"]) != int(r["home_matches"]) + int(r["away_matches"]):
            print(f"  [FAIL] V-F1-4 for {r['team_id']} {r['season']}")
            sys.exit(1)
    validation_notes.append("V-F1-4 matches_played==home+away: PASS")

    # V-F1-14/15 independent
    mismatches = 0
    for r in team_stats_rows:
        tid = r["team_id"]; s = int(r["season"])
        gs, gc, n, hn, an, cs, fts = validate_team_independent(tid, s, inputs["eligible_seasons"])
        if (gs != int(r["goals_scored"]) or gc != int(r["goals_conceded"])
            or n != int(r["matches_played"]) or hn != int(r["home_matches"])
            or an != int(r["away_matches"])
            or cs != int(r["clean_sheet_count"]) or fts != int(r["failed_to_score_count"])):
            mismatches += 1
    if mismatches > 0:
        print(f"  [FAIL] V-F1-14/15: {mismatches} mismatches")
        sys.exit(1)
    validation_notes.append("V-F1-14/15 independent from disk: PASS")

    # V-F1-9/10/20
    ts_by_key = {(r["team_id"], int(r["season"])): r for r in team_stats_rows}
    for b in btts_stats_rows:
        k = (b["team_id"], int(b["season"]))
        if k not in ts_by_key:
            print(f"  [FAIL] btts key {k} missing")
            sys.exit(1)
        if b["league_code"] != ts_by_key[k]["league_code"]:
            print(f"  [FAIL] V-F1-20 league mismatch {k}")
            sys.exit(1)
        if int(b["total_matches"]) != int(ts_by_key[k]["matches_played"]):
            print(f"  [FAIL] V-F1-10 total_matches mismatch {k}")
            sys.exit(1)
        if int(b["btts_yes_count"]) + int(b["btts_no_count"]) != int(b["total_matches"]):
            print(f"  [FAIL] V-F1-9 btts counts {k}")
            sys.exit(1)
    validation_notes.append("V-F1-9/10/20: PASS")

    # V-F1-12
    for b in btts_stats_rows:
        total = int(b["total_matches"])
        for N in RECENT_WINDOWS:
            if total >= N:
                if str(b[f"last_{N}_btts_count"]).strip() == "" or str(b[f"last_{N}_btts_percentage"]).strip() == "":
                    print(f"  [FAIL] V-F1-12 filled expected for {b['team_id']} N={N}")
                    sys.exit(1)
            else:
                if str(b[f"last_{N}_btts_count"]).strip() != "" or str(b[f"last_{N}_btts_percentage"]).strip() != "":
                    print(f"  [FAIL] V-F1-12 empty expected for {b['team_id']} N={N}")
                    sys.exit(1)
    validation_notes.append("V-F1-12 recent windows: PASS")

    # V-F1-13
    agg_yes = defaultdict(int)
    for b in btts_stats_rows:
        agg_yes[int(b["season"])] += int(b["btts_yes_count"])
    from_matches_yes = defaultdict(int)
    for m in pf["eligible_matches"]:
        fthg = try_int(m["home_goals"]); ftag = try_int(m["away_goals"])
        if fthg > 0 and ftag > 0:
            from_matches_yes[try_int(m["season"])] += 1
    v_f1_13_ok = all(agg_yes[s] == 2 * from_matches_yes[s] for s in inputs["eligible_seasons"])
    if not v_f1_13_ok:
        print(f"  [FAIL] V-F1-13 per-season btts aggregate")
        sys.exit(1)
    validation_notes.append("V-F1-13 per-season btts aggregate: PASS")

    print(f"  [OK] all validations PASS")
    print()

    # ---- Write (idempotent) ----
    print("--- WRITE ---")
    actions = {}
    def pk_team(r): return (r["team_id"], int(r["season"]))

    if structural_equal_csv(TEAM_STATS_CSV, TEAM_STATS_FIELDS, team_stats_rows, pk_team)[0]:
        actions["team_statistics.csv"] = "SKIP_IDENTICAL"
    elif TEAM_STATS_CSV.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUPS_DIR / f"phase4_F1_team_stats_{ts}.csv"
        shutil.copy2(TEAM_STATS_CSV, backup)
        print(f"  [BLOCKED_DIFFERENT] team_statistics.csv differs; backup: {backup.name}")
        sys.exit(1)
    else:
        write_csv_lf(TEAM_STATS_CSV, TEAM_STATS_FIELDS, team_stats_rows, pk=pk_team)
        actions["team_statistics.csv"] = "WROTE"

    if structural_equal_csv(BTTS_STATS_CSV, BTTS_STATS_FIELDS, btts_stats_rows, pk_team)[0]:
        actions["btts_statistics.csv"] = "SKIP_IDENTICAL"
    elif BTTS_STATS_CSV.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUPS_DIR / f"phase4_F1_btts_stats_{ts}.csv"
        shutil.copy2(BTTS_STATS_CSV, backup)
        print(f"  [BLOCKED_DIFFERENT] btts_statistics.csv differs; backup: {backup.name}")
        sys.exit(1)
    else:
        write_csv_lf(BTTS_STATS_CSV, BTTS_STATS_FIELDS, btts_stats_rows, pk=pk_team)
        actions["btts_statistics.csv"] = "WROTE"

    for k, v in sorted(actions.items()):
        print(f"  [{v:18s}] {k}")
    print()

    # ---- Post-write verify ----
    print("--- VERIFY POST-WRITE ---")
    ok1, r1 = structural_equal_csv(TEAM_STATS_CSV, TEAM_STATS_FIELDS, team_stats_rows, pk_team)
    ok2, r2 = structural_equal_csv(BTTS_STATS_CSV, BTTS_STATS_FIELDS, btts_stats_rows, pk_team)
    if not ok1: print(f"  [FAIL] team_statistics post-write: {r1}"); sys.exit(1)
    if not ok2: print(f"  [FAIL] btts_statistics post-write: {r2}"); sys.exit(1)
    print(f"  [OK] both outputs match in-memory structurally")
    print()

    # ---- Read-only integrity ----
    print("--- READ-ONLY INTEGRITY ---")
    post_sha = {
        "matches.csv": sha256_file(MATCHES_CSV),
        "teams.csv": sha256_file(TEAMS_CSV),
        "teams_canonical.csv": sha256_file(TEAMS_CANONICAL_CSV),
        "leagues.csv": sha256_file(LEAGUES_CSV),
        "season_meta.csv": sha256_file(SEASON_META_CSV),
        "canonical_teams.csv": sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }
    for k in pre_sha:
        same = pre_sha[k] == post_sha[k]
        print(f"  [{'SAME ' if same else 'DIFF '}] {k}")
        if not same:
            print(f"    [FAIL] input changed during F1!")
            sys.exit(1)
    print()

    # ---- Report ----
    print("--- WRITE REPORT ---")
    report_content = build_report(pf, team_stats_rows, btts_stats_rows, actions,
                                   det_msg, validation_notes, vf123_ok, vf123_unknown)
    write_text_lf(REPORT_TXT, report_content)
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    # ---- Acceptance ----
    print("=" * 78)
    print("F1 v2 ACCEPTANCE")
    print("=" * 78)

    checks = [
        ("V-F1-23 team whitelist PASS",         vf123_ok),
        ("Tahap A: COMPLETE FT integrity PASS", True),
        ("Tahap B: reconciliation_ok",          pf["reconciliation_ok"]),
        ("N_ELIGIBLE > 0",                      pf["N_ELIGIBLE"] > 0),
        ("team_statistics.csv written/identical",
            actions.get("team_statistics.csv") in ("WROTE", "SKIP_IDENTICAL")),
        ("btts_statistics.csv written/identical",
            actions.get("btts_statistics.csv") in ("WROTE", "SKIP_IDENTICAL")),
        ("V-F1-5c distinct (team_id, season)",  v_f1_5c_ok),
        ("V-F1-5a SUM==2*N_ELIGIBLE",           v_f1_5a_ok),
        ("V-F1-5b per-season sum",              v_f1_5b_ok),
        ("V-F1-4 matches=home+away",            True),
        ("V-F1-14/15 independent from disk",    mismatches == 0),
        ("V-F1-9/10/20 btts consistency",       True),
        ("V-F1-12 recent windows",              True),
        ("V-F1-13 per-season btts aggregate",   v_f1_13_ok),
        ("Determinism 2x rebuild",              det_ok),
        ("All inputs SHA-256 unchanged",        all(pre_sha[k] == post_sha[k] for k in pre_sha)),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  team_statistics rows : {len(team_stats_rows)}")
    print(f"  btts_statistics rows : {len(btts_stats_rows)}")
    print(f"  N_ELIGIBLE           : {pf['N_ELIGIBLE']}")
    print()

    if all_ok:
        print("STATUS: F1 v2 PASS ✅")
        print()
        print("NEXT: User approval → F2 (H2H statistics)")
        sys.exit(0)
    else:
        print("STATUS: F1 v2 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
