#!/usr/bin/env python3
"""
Phase 1 — Verifikasi kelengkapan CSV yang sudah di-download.
Cek kolom minimal, jumlah baris, dan status BTTS-able.
Tidak mengubah file. Read-only.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

RAW_DIR = PROJECT_ROOT / "data" / "raw"

# Kolom minimal agar BTTS bisa dihitung
REQUIRED_COLS = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]

def check_file(path: Path) -> dict:
    res = {"path": path.name, "rows": 0, "cols": 0,
           "status": None, "missing": [], "error": None}
    try:
        # Football-data CSV kadang punya encoding berbeda
        for enc in ("utf-8", "latin-1"):
            try:
                df = pd.read_csv(path, encoding=enc, on_bad_lines="skip")
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Cannot decode CSV")

        res["rows"] = len(df)
        res["cols"] = len(df.columns)

        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        res["missing"] = missing

        if missing:
            res["status"] = "MISSING_COLS"
        elif res["rows"] < 30:
            res["status"] = "LOW_ROWS"
        else:
            res["status"] = "OK"
    except Exception as e:
        res["status"] = "ERROR"
        res["error"] = str(e)[:120]
    return res

def main():
    print("=" * 60)
    print("BTTS ENGINE — CHECK DATA")
    print("=" * 60)
    print(f"Scanning: {RAW_DIR}")
    print()

    files = sorted(RAW_DIR.glob("*.csv"))
    if not files:
        print("[FAIL] No CSV files in data/raw/")
        print("TIP  : Jalankan scripts/download_historical.py dulu")
        sys.exit(1)

    counts = {}
    problems = []
    for f in files:
        r = check_file(f)
        counts[r["status"]] = counts.get(r["status"], 0) + 1

        if r["status"] == "OK":
            print(f"[OK]          {r['path']:20s} {r['rows']:4d} rows, {r['cols']} cols")
        elif r["status"] == "MISSING_COLS":
            print(f"[MISSING_COLS] {r['path']:20s} missing: {r['missing']}")
            problems.append(r)
        elif r["status"] == "LOW_ROWS":
            print(f"[LOW_ROWS]    {r['path']:20s} only {r['rows']} rows")
            problems.append(r)
        else:
            print(f"[ERROR]       {r['path']:20s} {r['error']}")
            problems.append(r)

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k, v in counts.items():
        print(f"  {k:14s}: {v}")
    print(f"  total files   : {len(files)}")
    print()

    if counts.get("OK", 0) == 0:
        print("STATUS: FAIL (no usable CSV)")
        sys.exit(1)

    print(f"STATUS: PASS ({counts['OK']} file(s) usable for Phase 2)")
    if problems:
        print(f"NOTE  : {len(problems)} file(s) need review")
    print("NEXT  : Phase 2 — CSV Database schema")

if __name__ == "__main__":
    main()
