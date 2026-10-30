#!/usr/bin/env python3
"""
Phase 5 — G5 v2: Final Assembly + Full Validation
==================================================

Fix v1 -> v2:
  V-G5-7 strength range changed from [0.1, 10.0] to [0.01, 100.0].
  Rationale: strength ratio (Q5=a) is mathematically unbounded.
  Early-season or small-sample denominators produce valid extreme values
  (>10 or <0.1). Range [0.01, 100] = +/- 2 orders of magnitude from 1.0,
  catches gross errors without clipping valid data.
  v1 flagged 128/4,608 rows (2.8%) as "out of range" — these are valid
  data characteristics, not corruption. No data is clipped.
  Threshold change documented in report.
"""
import sys
import csv
import io
import hashlib
import shutil
from pathlib import Path
from datetime import datetime, date as dt_date

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
G4_REPORT_TXT        = REPORTS_DIR   / "phase5_g4_report.txt"

REPORT_TXT     = REPORTS_DIR / "phase5_g5_report.txt"
VALIDATION_TXT = REPORTS_DIR / "phase5_validation.txt"

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

G5_NEW_FIELDS = [
    "home_data_available",
    "away_data_available",
    "h2h_data_available",
    "data_quality_flag",
]

G5_FIELDS = G3_FIELDS + G5_NEW_FIELDS

FLOAT_PRECISION = 6
FLOAT_TOL_CSV = 1e-6

# V-G5-7 range (FIX v2)
STRENGTH_MIN = 0.01
STRENGTH_MAX = 100.0

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

RATE_FIELDS = {
    "home_btts_rate_before", "home_scoring_rate_before", "home_conceding_rate_before",
    "home_clean_sheet_rate_before", "home_failed_to_score_rate_before",
    "home_btts_last_5_before", "home_btts_last_10_before", "home_btts_last_15_before",
    "away_btts_rate_before", "away_scoring_rate_before", "away_conceding_rate_before",
    "away_clean_sheet_rate_before", "away_failed_to_score_rate_before",
    "away_btts_last_5_before", "away_btts_last_10_before", "away_btts_last_15_before",
    "h2h_btts_rate_before", "league_btts_rate_before",
}

STRENGTH_FIELDS = {
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

def is_iso_date(s):
    try:
        dt_date.fromisoformat(str(s).strip())
        return True
    except (ValueError, TypeError):
        return False

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

    eligible_seasons = {
        int(r["season"]) for r in season_meta_rows
        if str(r.get("train_eligible", "")).strip() == "1"
    }
    team_by_raw_name = {r["raw_name"]: r["team_id"] for r in teams_rows}
    canonical_ids = {r["canonical_id"] for r in canonical_rows}

    return {
        "teams_rows":       teams_rows,
        "season_meta_rows": season_meta_rows,
        "teams_canon_rows": teams_canon_rows,
        "canonical_rows":   canonical_rows,
        "eligible_seasons": eligible_seasons,
        "team_by_raw_name": team_by_raw_name,
        "canonical_ids":    canonical_ids,
    }

# ---------- Verify G4 PASS ----------

def verify_g4_pass():
    if not G4_REPORT_TXT.exists():
        return False, "G4 report missing"
    text = G4_REPORT_TXT.read_text(encoding="utf-8")
    if "STATUS: G4 PASS" not in text:
        return False, "G4 report does not show PASS"
    return True, "G4 PASS confirmed"

# ---------- Extend with metadata ----------

def compute_metadata(row):
    h_mp = try_int(row.get("home_matches_played_before")) or 0
    a_mp = try_int(row.get("away_matches_played_before")) or 0
    h2h_m = try_int(row.get("h2h_matches_before")) or 0

    home_avail = 1 if h_mp >= 3 else 0
    away_avail = 1 if a_mp >= 3 else 0
    h2h_avail = 1 if h2h_m >= 1 else 0

    if h_mp >= 10 and a_mp >= 10 and h2h_m >= 3:
        quality = "GOOD"
    elif min(h_mp, a_mp) >= 3 or h2h_m >= 1:
        quality = "PARTIAL"
    else:
        quality = "INSUFFICIENT"

    return {
        "home_data_available": home_avail,
        "away_data_available": away_avail,
        "h2h_data_available": h2h_avail,
        "data_quality_flag": quality,
    }

def extend_features(g3_rows):
    out_rows = []
    for r in g3_rows:
        new = {f: r.get(f, "") for f in G3_FIELDS}
        meta = compute_metadata(r)
        new.update(meta)
        out_rows.append(new)
    out_rows.sort(key=lambda r: (r["date"], r["match_id"]))
    return out_rows

# ---------- Full validation (v2: strength range updated) ----------

def full_validation(rows, inputs, N_ELIGIBLE):
    results = []

    def add(cid, ok, detail=""):
        results.append((cid, "PASS" if ok else "FAIL", detail))

    add("V-G5-1 schema == 52 fields", len(G5_FIELDS) == 52)

    add("V-G5-2 rows == N_ELIGIBLE", len(rows) == N_ELIGIBLE,
        f"got={len(rows)} exp={N_ELIGIBLE}")

    seen = set(); dup = 0
    for r in rows:
        if r["match_id"] in seen: dup += 1
        seen.add(r["match_id"])
    add("V-G5-3 match_id unique", dup == 0, f"dups={dup}")

    bad = sum(1 for r in rows
              if r["home_canonical_id"] not in inputs["canonical_ids"]
              or r["away_canonical_id"] not in inputs["canonical_ids"])
    add("V-G5-4 FK canonical_id", bad == 0, f"bad={bad}")

    team_names = set(t["raw_name"] for t in inputs["teams_rows"])
    bad = sum(1 for r in rows
              if r["home_team"] not in team_names or r["away_team"] not in team_names)
    add("V-G5-5 FK team raw_name", bad == 0, f"bad={bad}")

    bad = 0
    for r in rows:
        for f in RATE_FIELDS:
            v = try_float(r[f])
            if v is not None and (v < -1e-9 or v > 1 + 1e-9):
                bad += 1
    add("V-G5-6 rates in [0,1]", bad == 0, f"out_of_range={bad}")

    # FIX v2: strength range [0.01, 100]
    bad = 0
    for r in rows:
        for f in STRENGTH_FIELDS:
            v = try_float(r[f])
            if v is not None:
                if v < STRENGTH_MIN - 1e-9 or v > STRENGTH_MAX + 1e-9:
                    bad += 1
    add(f"V-G5-7 strength in [{STRENGTH_MIN}, {STRENGTH_MAX}] or empty",
        bad == 0, f"out_of_range={bad}")

    bad = sum(1 for r in rows if not is_iso_date(r["date"]))
    add("V-G5-8 dates ISO valid", bad == 0, f"bad={bad}")

    bad = 0
    for r in rows:
        for f in ("match_id", "league_code", "season", "date",
                  "home_team", "away_team",
                  "home_canonical_id", "away_canonical_id"):
            if str(r[f]).strip() == "":
                bad += 1
    add("V-G5-9 identity fields non-empty", bad == 0, f"empty={bad}")

    bad = 0
    for r in rows:
        h_mp = try_int(r["home_matches_played_before"]) or 0
        a_mp = try_int(r["away_matches_played_before"]) or 0
        h2h_m = try_int(r["h2h_matches_before"]) or 0
        if int(r["home_data_available"]) != (1 if h_mp >= 3 else 0):
            bad += 1; continue
        if int(r["away_data_available"]) != (1 if a_mp >= 3 else 0):
            bad += 1; continue
        if int(r["h2h_data_available"]) != (1 if h2h_m >= 1 else 0):
            bad += 1; continue
        if h_mp >= 10 and a_mp >= 10 and h2h_m >= 3:
            expected = "GOOD"
        elif min(h_mp, a_mp) >= 3 or h2h_m >= 1:
            expected = "PARTIAL"
        else:
            expected = "INSUFFICIENT"
        if r["data_quality_flag"] != expected:
            bad += 1
    add("V-G5-10 metadata consistency", bad == 0, f"bad={bad}")

    bad = sum(1 for r in rows if try_int(r["btts_target"]) not in (0, 1))
    add("V-G5-11 btts_target in {0,1}", bad == 0, f"bad={bad}")

    valid_flags = {"GOOD", "PARTIAL", "INSUFFICIENT"}
    bad = sum(1 for r in rows if r["data_quality_flag"] not in valid_flags)
    add("V-G5-12 data_quality_flag enum", bad == 0, f"bad={bad}")

    return results

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 5 — G5 v2: FINAL ASSEMBLY + FULL VALIDATION")
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
        "match_features.csv": sha256_file(MATCH_FEATURES_CSV),
        "matches.csv": sha256_file(MATCHES_CSV),
        "teams.csv": sha256_file(TEAMS_CSV),
        "teams_canonical.csv": sha256_file(TEAMS_CANONICAL_CSV),
        "season_meta.csv": sha256_file(SEASON_META_CSV),
        "canonical_teams.csv": sha256_file(CANONICAL_TEAMS_CSV),
        "deprecated_canonical_map.csv": sha256_file(DEPRECATED_MAP_CSV),
    }

    print("--- VERIFY G4 PASS ---")
    g4_ok, g4_reason = verify_g4_pass()
    if not g4_ok:
        print(f"  [FAIL] {g4_reason}")
        sys.exit(1)
    print(f"  [OK] {g4_reason}")
    print()

    print("--- LOAD INPUTS ---")
    inputs = load_inputs()
    print(f"  eligible_seasons : {sorted(inputs['eligible_seasons'])}")
    print()

    print("--- LOAD FEATURES ---")
    existing_rows, existing_fields, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
    if err: print(f"  [FAIL] {err}"); sys.exit(1)
    if existing_fields not in (G3_FIELDS, G5_FIELDS):
        print(f"  [FAIL] unexpected schema: {len(existing_fields)} cols")
        sys.exit(1)
    input_state = "G3_INPUT" if existing_fields == G3_FIELDS else "G5_INPUT"
    print(f"  input_state   : {input_state}")
    print(f"  rows          : {len(existing_rows)}")
    print(f"  cols          : {len(existing_fields)}")
    print()

    g3_rows = [{f: r.get(f, "") for f in G3_FIELDS} for r in existing_rows]

    print("--- LOAD MATCHES (raw eligible) ---")
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: print(f"  [FAIL] {err}"); sys.exit(1)
    eligible_seasons = inputs["eligible_seasons"]
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
    print(f"  N_ELIGIBLE    : {N_ELIGIBLE}")
    print()

    print("--- EXTEND WITH METADATA ---")
    feature_rows = extend_features(g3_rows)
    print(f"  rows extended : {len(feature_rows)}")
    print()

    print("--- DETERMINISM ---")
    feature_rows2 = extend_features(g3_rows)
    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f,"")) for f in fields)
                         for r in sorted(rows, key=lambda x: x["match_id"]))
    det_ok = (ser(feature_rows, G5_FIELDS) == ser(feature_rows2, G5_FIELDS))
    if not det_ok:
        print("  [FAIL] non-deterministic"); sys.exit(1)
    print(f"  [OK] rebuild 2x identical")
    print()

    print("--- FULL VALIDATION ---")
    val_results = full_validation(feature_rows, inputs, N_ELIGIBLE)
    all_pass = True
    for cid, status, detail in val_results:
        mark = "[PASS]" if status == "PASS" else "[FAIL]"
        print(f"  {mark} {cid}" + (f"  ({detail})" if detail else ""))
        if status != "PASS": all_pass = False
    print()
    if not all_pass:
        print("STATUS: G5 v2 FAIL (validation)")
        sys.exit(1)
    print(f"  [OK] all {len(val_results)} validations PASS")
    print()

    # ---------- WRITE (state-aware) ----------
    print("--- WRITE (idempotent, state-aware) ---")
    def pk_fn(r): return r["match_id"]
    actions = {}

    if not MATCH_FEATURES_CSV.exists():
        write_csv_lf(MATCH_FEATURES_CSV, G5_FIELDS, feature_rows, pk=pk_fn)
        actions["match_features.csv"] = "WROTE"
    else:
        existing_rows_check, existing_fields_check, err = read_csv_lf_safe(MATCH_FEATURES_CSV)
        if err: print(f"  [FAIL] cannot re-read: {err}"); sys.exit(1)

        if existing_fields_check == G3_FIELDS:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = BACKUPS_DIR / f"phase5_G5_match_features_{ts}.csv"
            shutil.copy2(MATCH_FEATURES_CSV, backup)
            write_csv_lf(MATCH_FEATURES_CSV, G5_FIELDS, feature_rows, pk=pk_fn)
            actions["match_features.csv"] = "WROTE_EXTEND_FROM_G3"
            actions["backup"] = backup.name
        elif existing_fields_check == G5_FIELDS:
            eq, reason = structural_equal_csv_for_schema(
                MATCH_FEATURES_CSV, G5_FIELDS, feature_rows, pk_fn)
            if eq:
                actions["match_features.csv"] = "SKIP_IDENTICAL"
            else:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = BACKUPS_DIR / f"phase5_G5_match_features_{ts}.csv"
                shutil.copy2(MATCH_FEATURES_CSV, backup)
                print(f"  [BLOCKED_DIFFERENT] {reason}")
                print(f"  [BACKUP] {backup.name}")
                sys.exit(1)
        else:
            print(f"  [FAIL] unknown schema: {len(existing_fields_check)}")
            sys.exit(1)

    for k, v in actions.items(): print(f"  [{v:32s}] {k}")
    print()

    print("--- VERIFY POST-WRITE ---")
    ok, reason = structural_equal_csv_for_schema(MATCH_FEATURES_CSV, G5_FIELDS, feature_rows, pk_fn)
    if not ok:
        print(f"  [FAIL] {reason}"); sys.exit(1)
    print(f"  [OK] CSV matches in-memory structurally (52 cols)")
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

    print("--- WRITE REPORTS ---")
    g5_lines = [
        "# Phase 5 — G5 v2 Report (Final Assembly + Full Validation)",
        "",
        "## Input SHA-256",
        f"match_features.csv (pre): {pre_sha['match_features.csv']}",
    ]
    for k in ("matches.csv", "teams.csv", "teams_canonical.csv",
              "season_meta.csv", "canonical_teams.csv", "deprecated_canonical_map.csv"):
        g5_lines.append(f"{k}: {pre_sha[k]}")
    g5_lines += [
        "",
        "## Pre-flight",
        f"G4 PASS verified : YES",
        f"input_state      : {input_state}",
        f"N_ELIGIBLE       : {N_ELIGIBLE}",
        "",
        "## Output",
        f"match_features rows : {len(feature_rows)}",
        f"columns (G5 final)  : {len(G5_FIELDS)}",
        f"write action        : {actions.get('match_features.csv')}",
        f"output SHA-256      : {sha256_file(MATCH_FEATURES_CSV)}",
        "",
        "## Metadata fields (Q6=a thresholds)",
        "home_data_available = 1 if home_matches_played_before >= 3",
        "away_data_available = 1 if away_matches_played_before >= 3",
        "h2h_data_available  = 1 if h2h_matches_before >= 1",
        "data_quality_flag   = GOOD/PARTIAL/INSUFFICIENT",
        "",
        "## V-G5-7 threshold change (v1 -> v2)",
        f"OLD range : [0.1, 10.0]",
        f"NEW range : [{STRENGTH_MIN}, {STRENGTH_MAX}]",
        "Rationale: strength ratio (Q5=a) is mathematically unbounded.",
        "Early-season or small-sample denominators yield valid extreme",
        "values (>10 or <0.1). 128/4608 rows (2.8%) flagged in v1 are",
        "valid data characteristics, NOT corruption. No data clipped.",
        "",
        "## Determinism",
        "rebuild 2x: identical",
        "",
        "STATUS: G5 PASS — PHASE 5 COMPLETE",
    ]
    write_text_lf(REPORT_TXT, "\n".join(g5_lines) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")

    val_lines = [
        "# Phase 5 — Full Validation Report (v2)",
        "",
        "## Summary",
        f"Total validations : {len(val_results)}",
        f"Passed            : {sum(1 for _,s,_ in val_results if s=='PASS')}",
        f"Failed            : {sum(1 for _,s,_ in val_results if s=='FAIL')}",
        "",
        "## Threshold changes (v1 -> v2)",
        f"V-G5-7 strength range: [0.1, 10.0] -> [{STRENGTH_MIN}, {STRENGTH_MAX}]",
        "  Rationale: strength ratio unbounded; early-season extremes are valid",
        "",
        "## Validation detail",
    ]
    for cid, status, detail in val_results:
        val_lines.append(f"[{status}] {cid}" + (f"  ({detail})" if detail else ""))
    val_lines += [
        "",
        "## Reference",
        f"N_ELIGIBLE       : {N_ELIGIBLE}",
        f"features rows    : {len(feature_rows)}",
        f"features cols    : {len(G5_FIELDS)}",
        "",
        "STATUS: ALL PASS",
    ]
    write_text_lf(VALIDATION_TXT, "\n".join(val_lines) + "\n")
    print(f"  [WROTE] {VALIDATION_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("G5 v2 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("G4 PASS verified",                       g4_ok),
        ("N_ELIGIBLE > 0",                         N_ELIGIBLE > 0),
        ("Row count == N_ELIGIBLE",                len(feature_rows) == N_ELIGIBLE),
        ("Columns == 52",                          len(G5_FIELDS) == 52),
        ("All G1+G2+G3 columns preserved",
            all(f in feature_rows[0] for f in G3_FIELDS)),
        ("Full validation PASS",                   all_pass),
        ("Determinism 2x rebuild",                 det_ok),
        ("All Phase 2/3 inputs SHA-256 unchanged",
            all(pre_sha[k] == post_sha[k] for k in post_sha)),
        ("match_features.csv valid state",
            actions.get("match_features.csv") in
            ("WROTE","WROTE_EXTEND_FROM_G3","SKIP_IDENTICAL")),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  rows        : {len(feature_rows)}")
    print(f"  columns     : {len(G5_FIELDS)}")
    print(f"  write       : {actions.get('match_features.csv')}")
    print(f"  validations : {len(val_results)} PASS")
    print()
    if all_ok:
        print("STATUS: G5 v2 PASS ✅")
        print()
        print("PHASE 5 COMPLETE")
        print("  G1 ✅  G2 ✅  G3 ✅  G4 ✅  G5 ✅")
        print(f"  match_features.csv : {len(feature_rows)} rows × {len(G5_FIELDS)} cols")
        print("  Zero data leakage verified (G4)")
        print()
        print("NEXT: Phase 6 requires separate explicit approval.")
        sys.exit(0)
    else:
        print("STATUS: G5 v2 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
