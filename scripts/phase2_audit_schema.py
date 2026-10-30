#!/usr/bin/env python3
"""
Phase 2 — D1: AUDIT SOURCE SCHEMA (X4 variant)
================================================

D1 runs in TWO modes based on snapshot availability:
  - D1a (pre-snapshot): audit 12 files (2425 + 2526), snapshot PENDING
  - D1b (post-snapshot): audit 18 files (12 existing + 6 snapshot 2627)

Rule X4 (user-approved):
  SNAPSHOT_PENDING is NOT a failure. D1a PASS if 12 files OK.
  D1b is required later (after D2) to audit all 18 files.

Season convention (LOCKED):
  2023 = 2023/24 (2324)   -- excluded from Phase 2
  2024 = 2024/25 (2425)
  2025 = 2025/26 (2526)
  2026 = 2026/27 (2627)   -- snapshot, created in D2

TIDAK mengerjakan:
  - matches.csv / teams.csv / leagues.csv / season_meta.csv
  - BTTS statistics / ML features / fuzzy matching
  - Ubah raw / snapshot / processed
  - Membuat folder snapshot (that is D2 scope)
"""
import sys
import csv
import hashlib
from pathlib import Path
from datetime import datetime
from collections import Counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR     = PROJECT_ROOT / "data"
RAW_DIR      = DATA_DIR / "raw"
SNAPSHOT_DIR = RAW_DIR / "snapshots" / "2627" / "2026-10-30"
REPORTS_DIR  = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

LEAGUES = ["E0", "D1", "SP1", "I1", "F1", "N1"]
LEAGUE_NAMES = {
    "E0": "Premier League", "D1": "Bundesliga", "SP1": "La Liga",
    "I1": "Serie A",        "F1": "Ligue 1",    "N1": "Eredivisie",
}

REQUIRED_COLS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "HTHG", "HTAG"]

# ---------- Helpers ----------

def parse_date_iso(s):
    if s is None:
        return None, False
    s = str(s).strip()
    if not s:
        return None, False
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d"), True
        except ValueError:
            continue
    return None, False

def parse_int(s):
    if s is None:
        return None, False
    s = str(s).strip()
    if s == "" or s.lower() in ("nan", "none", "null", "-"):
        return None, False
    try:
        v = int(float(s))
        return v, True
    except (ValueError, TypeError):
        return None, False

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

def normalize_for_dupkey(home: str, away: str) -> tuple:
    def norm(x):
        return " ".join(str(x).strip().lower().split())
    return norm(home), norm(away)

# ---------- Per-file audit ----------

def audit_file(path: Path, league: str, season: int, season_code: str,
               season_label: str, priority: str, weight: float) -> dict:
    result = {
        "file": path.name,
        "path": str(path),
        "league_code": league,
        "league_name": LEAGUE_NAMES.get(league, ""),
        "season": season,
        "season_code": season_code,
        "season_label": season_label,
        "priority": priority,
        "weight": weight,
        "readable": False,
        "encoding": None,
        "rows_total": 0,
        "rows_complete": 0,
        "rows_incomplete_stats": 0,
        "rows_invalid": 0,
        "date_range_min": None,
        "date_range_max": None,
        "rows_date_invalid": 0,
        "rows_ft_valid": 0,
        "rows_ft_missing": 0,
        "rows_ht_valid": 0,
        "rows_ht_missing": 0,
        "rows_ft_negative": 0,
        "rows_ht_negative": 0,
        "duplicates": 0,
        "missing_required_cols": [],
        "error": None,
        "sha256": None,
    }
    result["sha256"] = sha256_file(path)

    rows, fields, enc, err = read_csv_robust(path)
    if err:
        result["error"] = f"parse_error: {err}"
        result["encoding"] = enc
        return result, []

    result["readable"] = True
    result["encoding"] = enc
    result["rows_total"] = len(rows)

    missing = [c for c in REQUIRED_COLS if c not in fields]
    result["missing_required_cols"] = missing
    if missing:
        result["error"] = f"missing_cols: {missing}"
        return result, []

    row_level = []
    dup_counter = Counter()
    date_iso_list = []

    for idx, r in enumerate(rows):
        date_iso, date_ok = parse_date_iso(r.get("Date"))
        if date_ok:
            date_iso_list.append(date_iso)
        else:
            result["rows_date_invalid"] += 1

        fthg, fthg_ok = parse_int(r.get("FTHG"))
        ftag, ftag_ok = parse_int(r.get("FTAG"))
        hthg, hthg_ok = parse_int(r.get("HTHG"))
        htag, htag_ok = parse_int(r.get("HTAG"))

        ft_ok = (fthg_ok and ftag_ok and fthg is not None and ftag is not None
                 and fthg >= 0 and ftag >= 0)
        ht_ok = (hthg_ok and htag_ok and hthg is not None and htag is not None
                 and hthg >= 0 and htag >= 0)

        if fthg_ok and fthg is not None and fthg < 0:
            result["rows_ft_negative"] += 1
        if ftag_ok and ftag is not None and ftag < 0:
            result["rows_ft_negative"] += 1
        if hthg_ok and hthg is not None and hthg < 0:
            result["rows_ht_negative"] += 1
        if htag_ok and htag is not None and htag < 0:
            result["rows_ht_negative"] += 1

        if ft_ok:
            result["rows_ft_valid"] += 1
        else:
            result["rows_ft_missing"] += 1

        if ht_ok:
            result["rows_ht_valid"] += 1
        else:
            result["rows_ht_missing"] += 1

        if not ft_ok:
            status = "INVALID"
            result["rows_invalid"] += 1
        elif not ht_ok:
            status = "INCOMPLETE_STATS"
            result["rows_incomplete_stats"] += 1
        else:
            status = "COMPLETE"
            result["rows_complete"] += 1

        if date_ok:
            nh, na = normalize_for_dupkey(r.get("HomeTeam", ""), r.get("AwayTeam", ""))
            key = (league, season, date_iso, nh, na)
            dup_counter[key] += 1

        row_level.append({
            "file": path.name,
            "row_idx": idx + 1,
            "league_code": league,
            "season": season,
            "date_raw": r.get("Date"),
            "date_iso": date_iso,
            "home_team": r.get("HomeTeam"),
            "away_team": r.get("AwayTeam"),
            "fthg": fthg, "ftag": ftag, "hthg": hthg, "htag": htag,
            "ft_ok": ft_ok, "ht_ok": ht_ok,
            "data_status": status,
        })

    result["duplicates"] = sum(v - 1 for v in dup_counter.values() if v > 1)

    if date_iso_list:
        result["date_range_min"] = min(date_iso_list)
        result["date_range_max"] = max(date_iso_list)

    return result, row_level

# ---------- Snapshot availability ----------

def snapshot_status():
    """Return (available: bool, missing_files: list[str])."""
    if not SNAPSHOT_DIR.exists():
        return False, [f"{league}_2627.csv (folder missing)" for league in LEAGUES]
    missing = []
    for league in LEAGUES:
        f = SNAPSHOT_DIR / f"{league}_2627.csv"
        if not f.exists():
            missing.append(f"{league}_2627.csv")
    return (len(missing) == 0), missing

# ---------- Main ----------

def main():
    print("=" * 78)
    snap_ok, snap_missing = snapshot_status()
    mode = "D1b (post-snapshot, full 18 files)" if snap_ok else "D1a (pre-snapshot, 12 files)"
    print(f"PHASE 2 — D1: AUDIT SOURCE SCHEMA — MODE: {mode}")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Snapshot dir : {SNAPSHOT_DIR}")
    print(f"Snapshot     : {'AVAILABLE' if snap_ok else 'SNAPSHOT_PENDING (D2)'}")
    if not snap_ok:
        print(f"  Missing ({len(snap_missing)}): {snap_missing[:3]}{'...' if len(snap_missing)>3 else ''}")
    print()

    # Build eligible season list based on snapshot availability
    eligible_seasons = [
        (2024, "2425", "2024/25", RAW_DIR, "SECONDARY",    0.4),
        (2025, "2526", "2025/26", RAW_DIR, "PRIMARY",      1.0),
    ]
    if snap_ok:
        eligible_seasons.append(
            (2026, "2627", "2026/27", SNAPSHOT_DIR, "RECENT_BOOST", 0.8)
        )

    audit_rows = []
    row_level_all = []
    sha_lines = []

    for season, code, label, src_dir, priority, weight in eligible_seasons:
        for league in LEAGUES:
            path = src_dir / f"{league}_{code}.csv"
            if not path.exists():
                print(f"[FAIL] missing: {path}")
                sys.exit(1)
            res, rowlevel = audit_file(path, league, season, code, label,
                                       priority, weight)
            audit_rows.append(res)
            row_level_all.extend(rowlevel)
            sha_lines.append(f"{res['sha256']}  {path.relative_to(PROJECT_ROOT)}")

    # Console per-file table
    print(f"{'File':30s} {'Rows':>5s} {'Comp':>5s} {'IncS':>5s} {'Inv':>4s} "
          f"{'Dup':>4s} {'HT_OK':>5s} {'FT_OK':>5s} {'DateRange':24s}")
    print("-" * 110)
    for r in audit_rows:
        dr = (f"{r['date_range_min']} → {r['date_range_max']}"
              if r['date_range_min'] else "-")
        print(f"{r['file']:30s} "
              f"{r['rows_total']:5d} "
              f"{r['rows_complete']:5d} "
              f"{r['rows_incomplete_stats']:5d} "
              f"{r['rows_invalid']:4d} "
              f"{r['duplicates']:4d} "
              f"{r['rows_ht_valid']:5d} "
              f"{r['rows_ft_valid']:5d} "
              f"{dr:24s}")

    # Totals
    print()
    print("=" * 78)
    print("TOTALS")
    print("=" * 78)
    tot = {
        "files_audited":      len(audit_rows),
        "rows_total":         sum(r["rows_total"] for r in audit_rows),
        "rows_complete":      sum(r["rows_complete"] for r in audit_rows),
        "rows_incomplete":    sum(r["rows_incomplete_stats"] for r in audit_rows),
        "rows_invalid":       sum(r["rows_invalid"] for r in audit_rows),
        "dup":                sum(r["duplicates"] for r in audit_rows),
        "ft_missing":         sum(r["rows_ft_missing"] for r in audit_rows),
        "ht_missing":         sum(r["rows_ht_missing"] for r in audit_rows),
        "ft_neg":             sum(r["rows_ft_negative"] for r in audit_rows),
        "ht_neg":             sum(r["rows_ht_negative"] for r in audit_rows),
        "date_invalid":       sum(r["rows_date_invalid"] for r in audit_rows),
    }
    for k, v in tot.items():
        print(f"  {k:18s}: {v}")

    print()
    print("PER SEASON")
    print("-" * 78)
    for season, code, label, _, _, _ in eligible_seasons:
        rs = [r for r in audit_rows if r["season"] == season]
        print(f"  {season} ({label}) code={code}: "
              f"files={len(rs)} rows={sum(r['rows_total'] for r in rs)} "
              f"complete={sum(r['rows_complete'] for r in rs)} "
              f"incomplete={sum(r['rows_incomplete_stats'] for r in rs)} "
              f"invalid={sum(r['rows_invalid'] for r in rs)}")
    if not snap_ok:
        print(f"  2026 (2026/27) code=2627: SNAPSHOT_PENDING (D2) — 0 files audited")

    print()
    print("PER LEAGUE")
    print("-" * 78)
    for league in LEAGUES:
        rs = [r for r in audit_rows if r["league_code"] == league]
        print(f"  {league:4s} {LEAGUE_NAMES[league]:15s}: "
              f"files={len(rs)} rows={sum(r['rows_total'] for r in rs)} "
              f"complete={sum(r['rows_complete'] for r in rs)} "
              f"incomplete={sum(r['rows_incomplete_stats'] for r in rs)} "
              f"invalid={sum(r['rows_invalid'] for r in rs)}")

    # INVALID details
    invalids = [r for r in row_level_all if r["data_status"] == "INVALID"]
    print()
    print("=" * 78)
    print("INVALID DETAILS (do NOT auto-fix — needs user decision)")
    print("=" * 78)
    if not invalids:
        print("  [OK] No INVALID rows found.")
    else:
        print(f"  Found {len(invalids)} INVALID rows:")
        for inv in invalids[:50]:
            print(f"    - {inv['file']} row#{inv['row_idx']} "
                  f"league={inv['league_code']} season={inv['season']} "
                  f"date_raw={inv['date_raw']} "
                  f"teams={inv['home_team']} vs {inv['away_team']} "
                  f"FTHG={inv['fthg']} FTAG={inv['ftag']}")
        if len(invalids) > 50:
            print(f"    ... and {len(invalids) - 50} more (see rowlevel CSV)")

    # INCOMPLETE_STATS details
    incompletes = [r for r in row_level_all if r["data_status"] == "INCOMPLETE_STATS"]
    print()
    print("=" * 78)
    print("INCOMPLETE_STATS DETAILS (FT ok, HT missing)")
    print("=" * 78)
    if not incompletes:
        print("  [OK] No INCOMPLETE_STATS rows found.")
    else:
        print(f"  Found {len(incompletes)} INCOMPLETE_STATS rows:")
        for inc in incompletes[:20]:
            print(f"    - {inc['file']} row#{inc['row_idx']} "
                  f"league={inc['league_code']} season={inc['season']} "
                  f"date={inc['date_iso']} "
                  f"teams={inc['home_team']} vs {inc['away_team']} "
                  f"FTHG={inc['fthg']} FTAG={inc['ftag']} "
                  f"HTHG={inc['hthg']} HTAG={inc['htag']}")
        if len(incompletes) > 20:
            print(f"    ... and {len(incompletes) - 20} more (see rowlevel CSV)")

    # Write reports
    print()
    print("=" * 78)
    print("WRITING REPORTS")
    print("=" * 78)

    mode_tag = "D1b" if snap_ok else "D1a"
    out_csv          = REPORTS_DIR / f"phase2_schema_audit_{mode_tag}.csv"
    out_rowlevel_csv = REPORTS_DIR / f"phase2_schema_audit_rowlevel_{mode_tag}.csv"
    out_raw_sha256   = REPORTS_DIR / f"phase2_audit_raw_sha256_{mode_tag}.txt"
    out_summary_txt  = REPORTS_DIR / f"phase2_d1_{mode_tag}_summary.txt"

    if audit_rows:
        with open(out_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(audit_rows[0].keys()))
            writer.writeheader()
            writer.writerows(audit_rows)
        print(f"  [OK] {out_csv}")

    if row_level_all:
        with open(out_rowlevel_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row_level_all[0].keys()))
            writer.writeheader()
            writer.writerows(row_level_all)
        print(f"  [OK] {out_rowlevel_csv} ({len(row_level_all)} rows)")

    out_raw_sha256.write_text("\n".join(sorted(sha_lines)) + "\n", encoding="utf-8")
    print(f"  [OK] {out_raw_sha256}")

    # Markdown-ish summary
    lines = []
    lines.append(f"# Phase 2 — D1 mode: {mode_tag}")
    lines.append(f"Snapshot status: {'AVAILABLE' if snap_ok else 'SNAPSHOT_PENDING (D2)'}")
    lines.append(f"Files audited: {tot['files_audited']}")
    lines.append(f"Rows total: {tot['rows_total']}")
    lines.append(f"COMPLETE: {tot['rows_complete']}")
    lines.append(f"INCOMPLETE_STATS: {tot['rows_incomplete']}")
    lines.append(f"INVALID: {tot['rows_invalid']}")
    lines.append(f"Duplicates: {tot['dup']}")
    lines.append(f"Date invalid: {tot['date_invalid']}")
    lines.append(f"FT negative: {tot['ft_neg']}")
    lines.append(f"HT negative: {tot['ht_neg']}")
    if not snap_ok:
        lines.append("Note: D1a PASS ≠ D1 final. D1b required after D2 to audit 18 files.")
    out_summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  [OK] {out_summary_txt}")

    # Acceptance criteria
    print()
    print("=" * 78)
    print(f"{mode_tag} ACCEPTANCE")
    print("=" * 78)

    problems = []
    if any(r["missing_required_cols"] for r in audit_rows):
        problems.append("missing_required_cols present")
    if tot["rows_invalid"] > 0:
        problems.append(f"{tot['rows_invalid']} INVALID rows need decision")
    if tot["dup"] > 0:
        problems.append(f"{tot['dup']} duplicates present")
    if tot["date_invalid"] > 0:
        problems.append(f"{tot['date_invalid']} invalid dates")
    if tot["ft_neg"] > 0 or tot["ht_neg"] > 0:
        problems.append("negative goals present")
    if not snap_ok:
        # D1a REQUIRES exactly 12 files (2 seasons × 6 leagues)
        if tot["files_audited"] != 12:
            problems.append(f"D1a expected 12 files, got {tot['files_audited']}")
    else:
        # D1b REQUIRES exactly 18 files
        if tot["files_audited"] != 18:
            problems.append(f"D1b expected 18 files, got {tot['files_audited']}")

    if problems:
        print(f"STATUS: {mode_tag} FAIL")
        for p in problems:
            print(f"  - {p}")
        print()
        print("ACTION: Fix reported problems. STOP.")
        sys.exit(1)

    print(f"STATUS: {mode_tag} PASS ✅")
    print(f"Files audited: {tot['files_audited']}")
    if not snap_ok:
        print("Snapshot 2627: SNAPSHOT_PENDING (D2) — this is D1a, NOT final D1")
        print()
        print("NEXT: User approval → D2 (backup + snapshot copy + SHA-256 verify)")
        print("      Then D1b (re-run this script after D2 → audit 18 files)")
    else:
        print("Snapshot 2627: AVAILABLE & audited (D1b = full D1 complete)")
        print()
        print("NEXT: User approval → D3 (build leagues.csv)")

    sys.exit(0)

if __name__ == "__main__":
    main()
