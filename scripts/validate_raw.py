#!/usr/bin/env python3
"""
Phase 1 — Validasi isi SEMUA raw CSV yang relevan:
  - Eligible: 2425 (SECONDARY), 2526 (PRIMARY), 2627 (RECENT_BOOST)
  - Backup  : 2324 (HISTORICAL_BACKUP) — divalidasi juga, tapi tidak masuk training
  - Legacy  : 2223 — TIDAK divalidasi (di luar scope)

Cek per file:
  1. CSV readable (encoding utf-8 / latin-1)
  2. Kolom minimal ada: Date, HomeTeam, AwayTeam, FTHG, FTAG
  3. FTHG & FTAG parseable sebagai integer, tidak null untuk match selesai
  4. btts_result bisa dihitung dari (FTHG>0) & (FTAG>0)
  5. Date parseable (format dd/mm/yy atau dd/mm/yyyy)
  6. Duplikat: tidak ada duplikat (Date, HomeTeam, AwayTeam)
  7. Sample size report
  8. Date range report
  9. Reporting missing/incomplete rows

Output: 
  - Console report
  - data/reports/phase1_validation_report.csv
"""
import sys
import re
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

RAW_DIR     = PROJECT_ROOT / "data" / "raw"
REPORTS_DIR = PROJECT_ROOT / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV     = REPORTS_DIR / "phase1_validation_report.csv"

REQUIRED_COLS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]

# Scope validasi sesuai keputusan user
SEASON_SCOPE = {
    "2425": {"priority": "SECONDARY",      "weight": 0.4, "train_eligible": True},
    "2526": {"priority": "PRIMARY",        "weight": 1.0, "train_eligible": True},
    "2627": {"priority": "RECENT_BOOST",   "weight": 0.8, "train_eligible": True},
    "2324": {"priority": "HISTORICAL_BACKUP", "weight": 0.0, "train_eligible": False},
}

LEAGUES = ["E0", "D1", "SP1", "I1", "F1", "N1"]

def parse_date_safe(s):
    """football-data: dd/mm/yy atau dd/mm/yyyy."""
    if pd.isna(s):
        return pd.NaT
    s = str(s).strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return pd.NaT

def validate_one(path: Path) -> dict:
    r = {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "rows_total": 0,
        "rows_valid_goals": 0,
        "rows_missing_goals": 0,
        "rows_date_invalid": 0,
        "duplicates": 0,
        "date_min": None,
        "date_max": None,
        "missing_required_cols": [],
        "status": None,
        "error": None,
    }

    try:
        df = None
        for enc in ("utf-8", "latin-1"):
            try:
                df = pd.read_csv(path, encoding=enc, on_bad_lines="skip")
                break
            except UnicodeDecodeError:
                continue
        if df is None:
            raise ValueError("Cannot decode (utf-8 / latin-1 failed)")
    except Exception as e:
        r["status"] = "PARSE_ERROR"
        r["error"] = str(e)[:140]
        return r

    r["rows_total"] = len(df)

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    r["missing_required_cols"] = missing
    if missing:
        r["status"] = "MISSING_COLS"
        return r

    # Parse date
    dates = df["Date"].apply(parse_date_safe)
    r["rows_date_invalid"] = int(dates.isna().sum())
    valid_dates = dates.dropna()
    if len(valid_dates) > 0:
        r["date_min"] = valid_dates.min().strftime("%Y-%m-%d")
        r["date_max"] = valid_dates.max().strftime("%Y-%m-%d")

    # Parse goals
    hg = pd.to_numeric(df["FTHG"], errors="coerce")
    ag = pd.to_numeric(df["FTAG"], errors="coerce")
    goals_mask = hg.notna() & ag.notna()
    r["rows_valid_goals"]   = int(goals_mask.sum())
    r["rows_missing_goals"] = int((~goals_mask).sum())

    # Duplikat (Date, HomeTeam, AwayTeam)
    dup = df.duplicated(subset=["Date", "HomeTeam", "AwayTeam"]).sum()
    r["duplicates"] = int(dup)

    # Status akhir
    if r["rows_valid_goals"] < 30:
        r["status"] = "LOW_SAMPLE"
    elif r["duplicates"] > 0:
        r["status"] = "DUPLICATES"
    elif r["rows_missing_goals"] > r["rows_valid_goals"] * 0.5:
        r["status"] = "TOO_MANY_MISSING"
    else:
        r["status"] = "OK"
    return r

def main():
    print("=" * 78)
    print("PHASE 1 — VALIDATE RAW CSV (30 files relevant)")
    print("=" * 78)

    targets = []
    for league in LEAGUES:
        for code, meta in SEASON_SCOPE.items():
            f = RAW_DIR / f"{league}_{code}.csv"
            if f.exists():
                targets.append((league, code, meta, f))

    if not targets:
        print("[FAIL] Tidak ada file target. Jalankan download dulu.")
        sys.exit(1)

    print(f"Target files: {len(targets)}")
    print()

    header = f"{'File':18s} {'Rows':>5s} {'Goals':>6s} {'MissingG':>9s} {'Dup':>4s} {'DateRange':22s} {'Status':14s}"
    print(header)
    print("-" * len(header))

    results = []
    for league, code, meta, path in targets:
        r = validate_one(path)
        r["league"]     = league
        r["season_code"] = code
        r["priority"]   = meta["priority"]
        r["weight"]     = meta["weight"]
        r["train_eligible"] = meta["train_eligible"]
        results.append(r)

        dr = (f"{r['date_min']} → {r['date_max']}"
              if r["date_min"] and r["date_max"] else "-")
        print(f"{r['file']:18s} "
              f"{r['rows_total']:5d} "
              f"{r['rows_valid_goals']:6d} "
              f"{r['rows_missing_goals']:9d} "
              f"{r['duplicates']:4d} "
              f"{dr:22s} "
              f"{r['status']:14s}")
        if r["error"]:
            print(f"    └─ error: {r['error']}")
        if r["missing_required_cols"]:
            print(f"    └─ missing cols: {r['missing_required_cols']}")

    # Write report CSV
    report_df = pd.DataFrame(results)
    report_df.to_csv(OUT_CSV, index=False)
    print()
    print(f"Report CSV: {OUT_CSV}")

    # Summary
    print()
    print("=" * 78)
    print("SUMMARY")
    print("=" * 78)
    statuses = report_df["status"].value_counts().to_dict()
    for k, v in statuses.items():
        print(f"  {k:16s}: {v}")
    print(f"  {'total files':16s}: {len(results)}")

    # Eligible untuk training
    elig = report_df[(report_df["train_eligible"]) & (report_df["status"] == "OK")]
    print()
    print(f"ELIGIBLE FOR TRAINING (status=OK & train_eligible): {len(elig)} files")
    if len(elig):
        print(elig[["file","priority","weight","rows_valid_goals"]].to_string(index=False))

    # Kelulusan Phase 1
    print()
    print("=" * 78)
    print("PHASE 1 ACCEPTANCE CRITERIA")
    print("=" * 78)

    # Kriteria
    crit = []
    # 1. Semua 2526 (PRIMARY) harus ada & OK
    prim = report_df[report_df["season_code"] == "2526"]
    crit.append(("All PRIMARY (2526) files exist & status=OK",
                 len(prim) == 6 and (prim["status"] == "OK").all()))
    # 2. Semua 2425 (SECONDARY) harus ada & OK
    sec = report_df[report_df["season_code"] == "2425"]
    crit.append(("All SECONDARY (2425) files exist & status=OK",
                 len(sec) == 6 and (sec["status"] == "OK").all()))
    # 3. Minimal 5/6 2627 (RECENT_BOOST) OK
    rec = report_df[report_df["season_code"] == "2627"]
    rec_ok = int((rec["status"] == "OK").sum())
    crit.append((f"RECENT_BOOST (2627) minimum 5/6 OK (found {rec_ok}/6)",
                 rec_ok >= 5))
    # 4. Total eligible ≥ 15 file
    crit.append((f"Total train-eligible files ≥ 15 (found {len(elig)})",
                 len(elig) >= 15))
    # 5. Total valid rows eligible ≥ 3000
    total_rows = int(elig["rows_valid_goals"].sum()) if len(elig) else 0
    crit.append((f"Total valid goal-rows eligible ≥ 3000 (found {total_rows})",
                 total_rows >= 3000))

    all_pass = True
    for desc, ok in crit:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {desc}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("STATUS: PHASE 1 PASS ✅")
        print("NEXT  : Phase 2 — CSV Database schema (butuh approval user)")
        sys.exit(0)
    else:
        print("STATUS: PHASE 1 FAIL ❌")
        print("ACTION: Re-download HANYA file gagal:")
        bad = report_df[~report_df["status"].isin(["OK", "LOW_SAMPLE"])]
        for _, b in bad.iterrows():
            league = b["league"]
            # Map season_code balik ke start_year
            start = {"2425":2024,"2526":2025,"2627":2026,"2324":2023}[b["season_code"]]
            print(f"  python3 scripts/download_phase1_final.py "
                  f"--leagues {league} --seasons {start} --force")
        sys.exit(1)

if __name__ == "__main__":
    main()
