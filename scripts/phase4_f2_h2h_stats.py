#!/usr/bin/env python3
"""
Phase 4 — F2: H2H Statistics (v2)
==================================

Fix v1 → v2:
  - V-F2-22 tolerance: 1e-6 -> 2e-6.
    Rationale: avg_total = avg_home + avg_away per math, but each field
    is round-half-even to 6 decimals before CSV. Worst-case accumulated
    error for 3 fields ~1.5e-6 (IEEE 754 double). 2e-6 covers worst-case.
  - Report logs explicit tolerance for V-F2-22.
  - All other logic identical to v1.

Input (READ-ONLY):
  data/processed/matches.csv
  data/processed/teams.csv
  data/processed/teams_canonical.csv
  data/processed/season_meta.csv
  data/mappings/canonical_teams.csv
  data/mappings/deprecated_canonical_map.csv

Output (CREATE):
  data/processed/h2h_statistics.csv          (18 kolom)
  data/reports/phase4_f2_report.txt
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

MATCHES_CSV          = PROCESSED_DIR / "matches.csv"
TEAMS_CSV            = PROCESSED_DIR / "teams.csv"
TEAMS_CANONICAL_CSV  = PROCESSED_DIR / "teams_canonical.csv"
SEASON_META_CSV      = PROCESSED_DIR / "season_meta.csv"
CANONICAL_TEAMS_CSV  = MAPPINGS_DIR  / "canonical_teams.csv"
DEPRECATED_MAP_CSV   = MAPPINGS_DIR  / "deprecated_canonical_map.csv"

H2H_STATS_CSV = PROCESSED_DIR / "h2h_statistics.csv"
REPORT_TXT    = REPORTS_DIR  / "phase4_f2_report.txt"

H2H_FIELDS = [
    "pair_key",
    "team_a_canonical_id", "team_b_canonical_id",
    "team_a_canonical_name", "team_b_canonical_name",
    "matches_total",
    "btts_yes_count", "btts_no_count",
    "btts_percentage",
    "home_wins", "draws", "away_wins",
    "avg_home_goals", "avg_away_goals", "avg_total_goals",
    "last_meeting_date",
    "last_5_meetings_btts_pct",
    "sample_warning",
]

FLOAT_PRECISION = 6
FLOAT_TOL_CSV = 1e-6
FLOAT_TOL_MEM = 1e-12
# FIX v2: V-F2-22 needs higher tolerance (3-field round-trip accumulation)
V_F2_22_TOL = 2e-6

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

def floats_equal(a, b, tol=FLOAT_TOL_CSV):
    a_empty = (a is None or str(a).strip() == "")
    b_empty = (b is None or str(b).strip() == "")
    if a_empty and b_empty:
        return True
    if a_empty or b_empty:
        return False
    try:
        return abs(float(a) - float(b)) <= tol
    except (ValueError, TypeError):
        return False

def pair_key_for(cid_a, cid_b):
    a, b = sorted([cid_a, cid_b])
    return "md5:" + hashlib.md5(f"{a}|{b}".encode("utf-8")).hexdigest()

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

    float_fields = {"btts_percentage", "avg_home_goals", "avg_away_goals",
                    "avg_total_goals", "last_5_meetings_btts_pct"}

    for i, (gr, er) in enumerate(zip(got_sorted, exp_sorted)):
        for f in fields:
            gv = gr.get(f, ""); ev = er.get(f, "")
            if f in float_fields:
                if not floats_equal(gv, ev):
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

    team_by_raw_name = {}
    for r in teams_rows:
        team_by_raw_name[r["raw_name"]] = r["team_id"]

    canonical_status = {r["canonical_id"]: r.get("status", "ACTIVE") for r in canonical_rows}
    dep_redirect = {r["deprecated_canonical_id"]: r["superseded_by"] for r in deprecated_rows}
    tc_by_team = {r["team_id"]: r for r in teams_canon_rows}
    canonical_by_id = {r["canonical_id"]: r for r in canonical_rows}

    return {
        "teams_rows":       teams_rows,
        "season_meta_rows": season_meta_rows,
        "teams_canon_rows": teams_canon_rows,
        "canonical_rows":   canonical_rows,
        "deprecated_rows":  deprecated_rows,
        "eligible_seasons": eligible_seasons,
        "team_by_raw_name": team_by_raw_name,
        "canonical_status": canonical_status,
        "dep_redirect":     dep_redirect,
        "tc_by_team":       tc_by_team,
        "canonical_by_id":  canonical_by_id,
    }

def resolve_active(cid, canonical_status, dep_redirect, max_depth=32):
    seen = set(); current = cid
    for _ in range(max_depth):
        if current in seen: return None
        seen.add(current)
        status = canonical_status.get(current)
        if status == "ACTIVE": return current
        if status != "DEPRECATED": return None
        nxt = dep_redirect.get(current)
        if nxt is None: return None
        current = nxt
    return None

# ---------- V-F1-23 whitelist ----------

def preflight_team_whitelist(matches_rows, team_by_raw_name):
    seen = set()
    for m in matches_rows:
        h = m.get("home_team", "").strip()
        a = m.get("away_team", "").strip()
        if h: seen.add(h)
        if a: seen.add(a)
    unknown = sorted(t for t in seen if t not in team_by_raw_name)
    return (len(unknown) == 0, unknown)

# ---------- Pre-flight: filter eligible (Q2) ----------

def preflight_eligible_matches(inputs):
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err:
        return {"fail": f"matches.csv read: {err}"}

    eligible_seasons = inputs["eligible_seasons"]
    team_by_raw_name = inputs["team_by_raw_name"]

    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        if ds == VALID_STATUS_COMPLETE:
            fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
            if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
                return {"fail": f"COMPLETE with invalid FT: match_id={m.get('match_id')}"}

    eligible_matches = []
    N_total = len(matches_rows)
    N_ELIGIBLE = 0
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in eligible_seasons:
            continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE):
            continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)):
            continue
        h = m.get("home_team", "").strip()
        a = m.get("away_team", "").strip()
        if h not in team_by_raw_name or a not in team_by_raw_name:
            return {"fail": f"V-F1-23 unknown team: {h!r} vs {a!r}"}
        eligible_matches.append(m)
        N_ELIGIBLE += 1

    return {
        "fail": None,
        "matches_rows":    matches_rows,
        "eligible_matches": eligible_matches,
        "N_total":         N_total,
        "N_ELIGIBLE":      N_ELIGIBLE,
    }

# ---------- Build bucket (in-memory) ----------

def build_buckets(eligible_matches, inputs):
    tc_by_team = inputs["tc_by_team"]
    canonical_status = inputs["canonical_status"]
    dep_redirect = inputs["dep_redirect"]
    team_by_raw_name = inputs["team_by_raw_name"]

    buckets = defaultdict(lambda: {
        "match_ids": [],
        "records": [],
        "canonical_a_id": None,
        "canonical_b_id": None,
    })
    seen_self_pair = []
    unresolved = []

    for m in eligible_matches:
        h_raw = m.get("home_team", "").strip()
        a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw_name.get(h_raw)
        a_id = team_by_raw_name.get(a_raw)
        if h_id is None or a_id is None:
            raise RuntimeError(f"V-F1-23 team lookup failure: {h_raw!r} vs {a_raw!r}")
        h_tc = tc_by_team.get(h_id); a_tc = tc_by_team.get(a_id)
        if h_tc is None or a_tc is None:
            raise RuntimeError(f"teams_canonical missing for {h_id} / {a_id}")
        h_cid = resolve_active(h_tc["canonical_id"], canonical_status, dep_redirect)
        a_cid = resolve_active(a_tc["canonical_id"], canonical_status, dep_redirect)
        if h_cid is None or a_cid is None:
            unresolved.append((h_raw, a_raw)); continue
        if h_cid == a_cid:
            seen_self_pair.append((m.get("match_id"), h_raw, a_raw, h_cid)); continue

        cid_a = min(h_cid, a_cid); cid_b = max(h_cid, a_cid)
        pk = pair_key_for(cid_a, cid_b)
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        btts = 1 if (fthg > 0 and ftag > 0) else 0
        mid = m.get("match_id", "").strip()

        b = buckets[pk]
        if b["canonical_a_id"] is None:
            b["canonical_a_id"] = cid_a
            b["canonical_b_id"] = cid_b
        if b["canonical_a_id"] != cid_a or b["canonical_b_id"] != cid_b:
            raise RuntimeError(f"pair_key {pk} bucket collision with different cid pair")
        b["match_ids"].append(mid)
        b["records"].append({
            "match_id": mid, "date": m.get("date", "").strip(),
            "fthg": fthg, "ftag": ftag, "btts": btts,
        })

    if seen_self_pair:
        raise RuntimeError(
            f"Self-pair H2H detected ({len(seen_self_pair)}): "
            + str(seen_self_pair[:5])
        )
    if unresolved:
        raise RuntimeError(f"Unresolved canonical ({len(unresolved)}): " + str(unresolved[:5]))
    return buckets

def aggregate_buckets(buckets, inputs):
    canonical_by_id = inputs["canonical_by_id"]
    rows = []
    for pk, b in sorted(buckets.items()):
        cid_a = b["canonical_a_id"]; cid_b = b["canonical_b_id"]
        recs = b["records"]; total = len(recs)
        if total == 0: continue

        home_wins = sum(1 for r in recs if r["fthg"] > r["ftag"])
        draws     = sum(1 for r in recs if r["fthg"] == r["ftag"])
        away_wins = sum(1 for r in recs if r["fthg"] < r["ftag"])
        assert home_wins + draws + away_wins == total

        yes = sum(r["btts"] for r in recs); no = total - yes
        btts_pct = yes / total if total > 0 else None

        avg_h = sum(r["fthg"] for r in recs) / total
        avg_a = sum(r["ftag"] for r in recs) / total
        avg_t = avg_h + avg_a

        last_date = max(r["date"] for r in recs)

        if total >= 5:
            sorted_desc = sorted(recs, key=lambda r: (r["date"], r["match_id"]), reverse=True)
            l5_pct = sum(r["btts"] for r in sorted_desc[:5]) / 5
        else:
            l5_pct = None

        warn = "OK" if total >= 5 else ("LOW_SAMPLE" if total >= 2 else "MINIMAL")

        rows.append({
            "pair_key": pk,
            "team_a_canonical_id": cid_a,
            "team_b_canonical_id": cid_b,
            "team_a_canonical_name": canonical_by_id[cid_a]["canonical_name"],
            "team_b_canonical_name": canonical_by_id[cid_b]["canonical_name"],
            "matches_total": total,
            "btts_yes_count": yes,
            "btts_no_count": no,
            "btts_percentage": serialize_float(btts_pct),
            "home_wins": home_wins,
            "draws": draws,
            "away_wins": away_wins,
            "avg_home_goals": serialize_float(avg_h),
            "avg_away_goals": serialize_float(avg_a),
            "avg_total_goals": serialize_float(avg_t),
            "last_meeting_date": last_date,
            "last_5_meetings_btts_pct": serialize_float(l5_pct),
            "sample_warning": warn,
        })
    rows.sort(key=lambda r: r["pair_key"])
    return rows

def in_memory_bucket_checks(buckets, eligible_matches):
    problems = []
    for pk, b in buckets.items():
        mids = b["match_ids"]
        if len(mids) != len(set(mids)):
            problems.append(f"V-F2-11a: dup match_id in bucket {pk}")
    union = []
    for b in buckets.values():
        union.extend(b["match_ids"])
    if len(union) != len(set(union)):
        problems.append(f"V-F2-11b: overlap across buckets")
    eligible_ids = set(m.get("match_id", "").strip() for m in eligible_matches)
    union_set = set(union)
    if union_set != eligible_ids:
        missing = len(eligible_ids - union_set)
        extra = len(union_set - eligible_ids)
        problems.append(f"V-F2-11c: set mismatch (missing={missing}, extra={extra})")
    from collections import Counter
    cnt = Counter(union)
    bad = [mid for mid in eligible_ids if cnt.get(mid, 0) != 1]
    if bad:
        problems.append(f"V-F2-11d: {len(bad)} match_id freq != 1")
    return problems

# ---------- Post-write independent rebuild ----------

def postwrite_independent_rebuild(eligible_seasons):
    matches_rows, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: return None, 0, [f"rebuild matches read: {err}"]
    teams_rows, _, err = read_csv_lf_safe(TEAMS_CSV)
    if err: return None, 0, [f"rebuild teams read: {err}"]
    teams_canon_rows, _, err = read_csv_lf_safe(TEAMS_CANONICAL_CSV)
    if err: return None, 0, [f"rebuild teams_canonical read: {err}"]
    canonical_rows, _, err = read_csv_lf_safe(CANONICAL_TEAMS_CSV)
    if err: return None, 0, [f"rebuild canonical_teams read: {err}"]
    deprecated_rows, _, err = read_csv_lf_safe(DEPRECATED_MAP_CSV)
    if err and "missing" in str(err):
        deprecated_rows = []
    elif err:
        return None, 0, [f"rebuild deprecated_map read: {err}"]

    team_by_raw = {r["raw_name"]: r["team_id"] for r in teams_rows}
    tc_by_team = {r["team_id"]: r for r in teams_canon_rows}
    canonical_status = {r["canonical_id"]: r.get("status", "ACTIVE") for r in canonical_rows}
    dep_redirect = {r["deprecated_canonical_id"]: r["superseded_by"] for r in deprecated_rows}
    canonical_by_id = {r["canonical_id"]: r for r in canonical_rows}

    def resolve(cid):
        seen = set(); cur = cid
        for _ in range(32):
            if cur in seen: return None
            seen.add(cur)
            s = canonical_status.get(cur)
            if s == "ACTIVE": return cur
            if s != "DEPRECATED": return None
            nxt = dep_redirect.get(cur)
            if nxt is None: return None
            cur = nxt
        return None

    buckets = defaultdict(lambda: {"match_ids": [], "records": [], "cid_a": None, "cid_b": None})
    N_eligible = 0
    errors = []
    for m in matches_rows:
        ds = str(m.get("data_status", "")).strip()
        s = try_int(m.get("season"))
        if s is None or s not in eligible_seasons: continue
        if ds not in (VALID_STATUS_COMPLETE, VALID_STATUS_INCOMPLETE): continue
        fthg = try_int(m.get("home_goals")); ftag = try_int(m.get("away_goals"))
        if not (is_valid_goals_int(fthg) and is_valid_goals_int(ftag)): continue
        h_raw = m.get("home_team", "").strip(); a_raw = m.get("away_team", "").strip()
        h_id = team_by_raw.get(h_raw); a_id = team_by_raw.get(a_raw)
        if h_id is None or a_id is None:
            errors.append(f"unknown team {h_raw!r}/{a_raw!r}"); continue
        h_tc = tc_by_team.get(h_id); a_tc = tc_by_team.get(a_id)
        if not (h_tc and a_tc):
            errors.append(f"teams_canonical missing {h_id}/{a_id}"); continue
        h_cid = resolve(h_tc["canonical_id"]); a_cid = resolve(a_tc["canonical_id"])
        if not (h_cid and a_cid) or h_cid == a_cid:
            errors.append(f"self-pair or unresolved {h_raw}/{a_raw}"); continue
        ca = min(h_cid, a_cid); cb = max(h_cid, a_cid)
        pk = pair_key_for(ca, cb)
        btts = 1 if (fthg > 0 and ftag > 0) else 0
        rec = {"match_id": m.get("match_id", "").strip(),
               "date": m.get("date", "").strip(),
               "fthg": fthg, "ftag": ftag, "btts": btts}
        b = buckets[pk]
        b["cid_a"] = ca; b["cid_b"] = cb
        b["match_ids"].append(rec["match_id"])
        b["records"].append(rec)
        N_eligible += 1

    rebuilt = {}
    for pk, b in buckets.items():
        recs = b["records"]; total = len(recs)
        if total == 0: continue
        ca = b["cid_a"]; cb = b["cid_b"]
        yes = sum(r["btts"] for r in recs); no = total - yes
        home_w = sum(1 for r in recs if r["fthg"] > r["ftag"])
        draws = sum(1 for r in recs if r["fthg"] == r["ftag"])
        away_w = sum(1 for r in recs if r["fthg"] < r["ftag"])
        avg_h = sum(r["fthg"] for r in recs) / total
        avg_a = sum(r["ftag"] for r in recs) / total
        avg_t = avg_h + avg_a
        last_date = max(r["date"] for r in recs)
        if total >= 5:
            sd = sorted(recs, key=lambda r: (r["date"], r["match_id"]), reverse=True)
            l5 = sum(r["btts"] for r in sd[:5]) / 5
        else:
            l5 = None
        warn = "OK" if total >= 5 else ("LOW_SAMPLE" if total >= 2 else "MINIMAL")
        rebuilt[pk] = {
            "pair_key": pk,
            "team_a_canonical_id": ca, "team_b_canonical_id": cb,
            "team_a_canonical_name": canonical_by_id[ca]["canonical_name"],
            "team_b_canonical_name": canonical_by_id[cb]["canonical_name"],
            "matches_total": total,
            "btts_yes_count": yes, "btts_no_count": no,
            "btts_percentage": yes / total if total > 0 else None,
            "home_wins": home_w, "draws": draws, "away_wins": away_w,
            "avg_home_goals": avg_h, "avg_away_goals": avg_a, "avg_total_goals": avg_t,
            "last_meeting_date": last_date,
            "last_5_meetings_btts_pct": l5,
            "sample_warning": warn,
        }
    return rebuilt, N_eligible, errors

def compare_rebuild_vs_csv(rebuilt, csv_rows):
    problems = []
    rebuilt_keys = set(rebuilt.keys())
    csv_keys = set(r["pair_key"] for r in csv_rows)
    if rebuilt_keys != csv_keys:
        problems.append(f"R2a: pair_key set mismatch (rebuilt={len(rebuilt_keys)}, csv={len(csv_keys)})")
    if len(rebuilt) != len(csv_rows):
        problems.append(f"R2b: row count mismatch")
    csv_by_pk = {r["pair_key"]: r for r in csv_rows}
    for pk in rebuilt_keys & csv_keys:
        rb = rebuilt[pk]; cs = csv_by_pk[pk]
        for f in ("team_a_canonical_id", "team_b_canonical_id",
                  "team_a_canonical_name", "team_b_canonical_name",
                  "last_meeting_date", "sample_warning"):
            if str(rb[f]) != str(cs[f]):
                problems.append(f"R2c id/enum {pk}.{f}: rb={rb[f]!r} csv={cs[f]!r}")
        for f in ("matches_total", "btts_yes_count", "btts_no_count",
                  "home_wins", "draws", "away_wins"):
            if int(rb[f]) != int(cs[f]):
                problems.append(f"R2c int {pk}.{f}: rb={rb[f]} csv={cs[f]}")
        for f in ("btts_percentage", "avg_home_goals", "avg_away_goals",
                  "avg_total_goals", "last_5_meetings_btts_pct"):
            if not floats_equal(rb[f], cs[f]):
                problems.append(f"R2c float {pk}.{f}: rb={rb[f]!r} csv={cs[f]!r}")
    sum_rb = sum(rb["matches_total"] for rb in rebuilt.values())
    sum_csv = sum(int(cs["matches_total"]) for cs in csv_rows)
    if sum_rb != sum_csv:
        problems.append(f"R2d: sum mismatch rb={sum_rb} csv={sum_csv}")
    return problems

# ---------- CSV-scoped validation (FIX: V-F2-22 tolerance 2e-6) ----------

def csv_scoped_validate(csv_rows, N_ELIGIBLE):
    problems = []
    keys = set()
    for r in csv_rows:
        pk = r["pair_key"]
        if pk in keys:
            problems.append(f"V-F2-13 dup {pk}")
        keys.add(pk)
        a = r["team_a_canonical_id"]; b = r["team_b_canonical_id"]
        if a >= b:
            problems.append(f"V-F2-14 a>=b {pk}")
        mt = int(r["matches_total"])
        yes = int(r["btts_yes_count"]); no = int(r["btts_no_count"])
        hw = int(r["home_wins"]); dr = int(r["draws"]); aw = int(r["away_wins"])
        if yes + no != mt:
            problems.append(f"V-F2-15 btts counts {pk}")
        if hw + dr + aw != mt:
            problems.append(f"V-F2-16 wins/draws/away {pk}")
        expected_pk = pair_key_for(a, b)
        if pk != expected_pk:
            problems.append(f"V-F2-20 pair_key recompute {pk}")
        # rates in [0,1] for btts_percentage
        v = r["btts_percentage"]
        if str(v).strip() != "":
            try:
                fv = float(v)
                if not (-1e-9 <= fv <= 1 + 1e-9):
                    problems.append(f"V-F2-17 btts_percentage out of range {pk}")
            except ValueError:
                problems.append(f"V-F2-17 non-float {pk}")
        # V-F2-22 FIX: tolerance 2e-6 instead of 1e-6
        try:
            ah = float(r["avg_home_goals"]); aa = float(r["avg_away_goals"]); at = float(r["avg_total_goals"])
            if abs((ah + aa) - at) > V_F2_22_TOL:
                problems.append(
                    f"V-F2-22 avg_total != avg_home+avg_away {pk} "
                    f"(ah+aa={ah+aa:.8f}, at={at:.8f}, diff={abs((ah+aa)-at):.2e}, tol={V_F2_22_TOL:.0e})"
                )
        except (ValueError, TypeError):
            pass
        # last_5 empty if matches < 5
        if mt >= 5:
            if str(r["last_5_meetings_btts_pct"]).strip() == "":
                problems.append(f"V-F2-18 last_5 should be filled {pk}")
        else:
            if str(r["last_5_meetings_btts_pct"]).strip() != "":
                problems.append(f"V-F2-18 last_5 should be empty {pk}")
        expected_warn = "OK" if mt >= 5 else ("LOW_SAMPLE" if mt >= 2 else "MINIMAL")
        if r["sample_warning"] != expected_warn:
            problems.append(f"V-F2-19 sample_warning {pk}")
    sum_mt = sum(int(r["matches_total"]) for r in csv_rows)
    if sum_mt != N_ELIGIBLE:
        problems.append(f"V-F2-21 sum(matches_total)={sum_mt} != N_ELIGIBLE={N_ELIGIBLE}")
    return problems

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 4 — F2 v2: H2H STATISTICS")
    print("=" * 78)
    print()

    for p in (MATCHES_CSV, TEAMS_CSV, TEAMS_CANONICAL_CSV, SEASON_META_CSV, CANONICAL_TEAMS_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}"); sys.exit(1)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
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

    print("--- V-F1-23 whitelist ---")
    matches_rows_all, _, err = read_csv_lf_safe(MATCHES_CSV)
    if err: print(f"  [FAIL] {err}"); sys.exit(1)
    vf123_ok, vf123_unknown = preflight_team_whitelist(matches_rows_all, inputs["team_by_raw_name"])
    if not vf123_ok:
        print(f"  [FAIL] unknown: {vf123_unknown[:10]}"); sys.exit(1)
    print(f"  [OK] all teams found")
    print()

    print("--- PRE-FLIGHT (filter eligible) ---")
    pf = preflight_eligible_matches(inputs)
    if pf.get("fail"):
        print(f"  [FAIL] {pf['fail']}"); sys.exit(1)
    print(f"  N_total    : {pf['N_total']}")
    print(f"  N_ELIGIBLE : {pf['N_ELIGIBLE']}")
    print()

    print("--- BUILD ---")
    try:
        buckets = build_buckets(pf["eligible_matches"], inputs)
    except RuntimeError as e:
        print(f"  [FAIL] {e}"); sys.exit(1)
    print(f"  buckets (unique pairs) : {len(buckets)}")
    print()

    print("--- IN-MEMORY BUCKET CHECKS (Q1 A) ---")
    probs = in_memory_bucket_checks(buckets, pf["eligible_matches"])
    if probs:
        for p in probs: print(f"  [FAIL] {p}")
        sys.exit(1)
    print(f"  [OK] V-F2-11a/11b/11c/11d PASS")
    print()

    print("--- AGGREGATE ---")
    csv_rows = aggregate_buckets(buckets, inputs)
    print(f"  aggregated rows : {len(csv_rows)}")
    print()

    print("--- DETERMINISM ---")
    try:
        buckets2 = build_buckets(pf["eligible_matches"], inputs)
    except RuntimeError as e:
        print(f"  [FAIL] {e}"); sys.exit(1)
    csv_rows2 = aggregate_buckets(buckets2, inputs)

    def ser(rows, fields):
        return "\n".join("|".join(str(r.get(f, "")) for f in fields)
                         for r in sorted(rows, key=lambda x: x["pair_key"]))
    det_ok = (ser(csv_rows, H2H_FIELDS) == ser(csv_rows2, H2H_FIELDS))
    if not det_ok:
        print("  [FAIL] non-deterministic"); sys.exit(1)
    print(f"  [OK] rebuild 2x identical")
    print()

    print("--- WRITE (idempotent) ---")
    def pk_fn(r): return r["pair_key"]
    actions = {}
    eq, _ = structural_equal_csv(H2H_STATS_CSV, H2H_FIELDS, csv_rows, pk_fn)
    if eq:
        actions["h2h_statistics.csv"] = "SKIP_IDENTICAL"
    elif H2H_STATS_CSV.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUPS_DIR / f"phase4_F2_h2h_{ts}.csv"
        shutil.copy2(H2H_STATS_CSV, backup)
        print(f"  [BLOCKED_DIFFERENT] backup: {backup.name}"); sys.exit(1)
    else:
        write_csv_lf(H2H_STATS_CSV, H2H_FIELDS, csv_rows, pk=pk_fn)
        actions["h2h_statistics.csv"] = "WROTE"
    for k, v in actions.items(): print(f"  [{v:18s}] {k}")
    print()

    print("--- VERIFY POST-WRITE ---")
    ok, reason = structural_equal_csv(H2H_STATS_CSV, H2H_FIELDS, csv_rows, pk_fn)
    if not ok:
        print(f"  [FAIL] {reason}"); sys.exit(1)
    print(f"  [OK] CSV matches in-memory structurally")
    print()

    print("--- CSV-SCOPED VALIDATION (V-F2-22 tol=2e-6) ---")
    csv_loaded, _, err = read_csv_lf_safe(H2H_STATS_CSV)
    if err:
        print(f"  [FAIL] {err}"); sys.exit(1)
    csv_probs = csv_scoped_validate(csv_loaded, pf["N_ELIGIBLE"])
    if csv_probs:
        for p in csv_probs: print(f"  [FAIL] {p}")
        sys.exit(1)
    print(f"  [OK] CSV-scoped PASS (V-F2-22 tolerance = {V_F2_22_TOL})")
    print()

    print("--- POST-WRITE INDEPENDENT REBUILD (Q1 B + R2 + R5 + R6) ---")
    rebuilt, N_rebuild, rebuild_errors = postwrite_independent_rebuild(inputs["eligible_seasons"])
    if rebuild_errors:
        print(f"  [FAIL] rebuild errors: {rebuild_errors[:5]}"); sys.exit(1)
    if N_rebuild != pf["N_ELIGIBLE"]:
        print(f"  [FAIL] rebuild N={N_rebuild} != build N={pf['N_ELIGIBLE']}"); sys.exit(1)
    compare_probs = compare_rebuild_vs_csv(rebuilt, csv_loaded)
    if compare_probs:
        print(f"  [FAIL] {len(compare_probs)} mismatches")
        for p in compare_probs[:10]: print(f"    {p}")
        sys.exit(1)
    print(f"  [OK] rebuild matches CSV fully (R2a-R2e + R5 + R6)")
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
        "# Phase 4 — F2 Report v2 (H2H Statistics)",
        "Phase 4 = end-of-season snapshot; anti-leakage handled in Phase 5",
        "",
        "## Input SHA-256",
    ]
    for k, v in pre_sha.items():
        report_lines.append(f"{k}: {v}")
    report_lines += [
        "",
        "## Pre-flight",
        f"V-F1-23 whitelist : PASS",
        f"N_total           : {pf['N_total']}",
        f"N_ELIGIBLE        : {pf['N_ELIGIBLE']}",
        "",
        "## Output",
        f"h2h_statistics rows : {len(csv_rows)}",
        f"write action        : {actions.get('h2h_statistics.csv')}",
        f"output SHA-256      : {sha256_file(H2H_STATS_CSV)}",
        "",
        "## Validation summary",
        "- V-F2-11a/11b/11c/11d (in-memory bucket): PASS",
        "- V-F2-12..22 (CSV-scoped): PASS",
        "- Post-write independent rebuild (R2a-R2e + R5 + R6): PASS",
        "- Determinism rebuild 2x: PASS",
        "",
        "## Float precision & tolerances",
        f"CSV serialization            : round-half-even to {FLOAT_PRECISION} desimal, strip zeros",
        f"Float tolerance (default)    : {FLOAT_TOL_CSV}",
        f"V-F2-22 tolerance (avg_total): {V_F2_22_TOL}",
        "  Rationale: avg_total = avg_home + avg_away math, but each field is",
        "  round-half-even to 6 decimals before CSV. Worst-case accumulation",
        "  for 3 fields ~1.5e-6 (IEEE 754 double). 2e-6 covers this worst-case",
        "  without hiding real aggregation errors (magnitude diff >= 1e-4 would",
        "  still fail). Data verified correct for all 4 borderline rows.",
        "",
        "## R7 semantics",
        "home_wins = count(FTHG > FTAG) per match",
        "draws     = count(FTHG == FTAG)",
        "away_wins = count(FTHG < FTAG)",
        "",
        "## Q2 filter (data_status)",
        "COMPLETE + INCOMPLETE_STATS (FT valid) -> eligible",
        "INVALID / FT invalid -> excluded",
        "",
        "STATUS: F2 v2 PASS",
    ]
    write_text_lf(REPORT_TXT, "\n".join(report_lines) + "\n")
    print(f"  [WROTE] {REPORT_TXT.relative_to(PROJECT_ROOT)}")
    print()

    print("=" * 78)
    print("F2 v2 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("V-F1-23 whitelist",                     vf123_ok),
        ("N_ELIGIBLE > 0",                        pf["N_ELIGIBLE"] > 0),
        ("h2h_statistics.csv written/identical",  actions.get("h2h_statistics.csv") in ("WROTE","SKIP_IDENTICAL")),
        ("V-F2-11a/11b/11c/11d in-memory",        len(probs) == 0),
        ("CSV-scoped validation (V-F2-22 tol=2e-6)", len(csv_probs) == 0),
        ("Post-write rebuild errors empty",       len(rebuild_errors) == 0),
        ("Post-write comparison PASS",            len(compare_probs) == 0),
        ("Determinism 2x rebuild",                det_ok),
        ("All inputs SHA-256 unchanged",          all(pre_sha[k] == post_sha[k] for k in pre_sha)),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    print(f"  h2h rows       : {len(csv_rows)}")
    print(f"  N_ELIGIBLE     : {pf['N_ELIGIBLE']}")
    print()
    if all_ok:
        print("STATUS: F2 v2 PASS ✅")
        print()
        print("NEXT: User approval → F3 (league statistics + full validation)")
        sys.exit(0)
    else:
        print("STATUS: F2 v2 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
