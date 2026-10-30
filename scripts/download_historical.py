#!/usr/bin/env python3
"""
Phase 1 — Downloader CSV historis dari football-data.co.uk
- Rate-limited (delay antar request)
- Skip jika file sudah ada (kecuali --force)
- Laporan PASS/FAIL per file
- TIDAK mengisi data dummy jika gagal
"""
import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests

from config.leagues import ALL_LEAGUES, PRIMARY_LEAGUES, download_url, season_code

RAW_DIR = PROJECT_ROOT / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

def download_one(league: str, start_year: int, force: bool = False,
                 delay: float = 1.0, timeout: int = 20) -> dict:
    url = download_url(league, start_year)
    sc = season_code(start_year)
    out = RAW_DIR / f"{league}_{sc}.csv"

    result = {
        "league": league,
        "season": start_year,
        "url": url,
        "path": str(out),
        "status": None,
        "bytes": 0,
        "error": None,
    }

    if out.exists() and not force:
        result["status"] = "SKIP_EXISTS"
        result["bytes"] = out.stat().st_size
        return result

    try:
        headers = {"User-Agent": "BTTS-Engine/0.1 (+termux-ubuntu)"}
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            result["status"] = "HTTP_ERROR"
            result["error"] = f"HTTP {r.status_code}"
            return result
        if not r.content or len(r.content) < 50:
            result["status"] = "EMPTY"
            result["error"] = "Response too small"
            return result
        out.write_bytes(r.content)
        result["status"] = "OK"
        result["bytes"] = len(r.content)
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)[:120]

    time.sleep(delay)  # rate limit
    return result

def main():
    p = argparse.ArgumentParser(description="Download historical CSV from football-data.co.uk")
    p.add_argument("--leagues", nargs="+", default=list(PRIMARY_LEAGUES.keys()),
                   help="League codes (e.g., E0 SP1 D1). Default: primary leagues.")
    p.add_argument("--seasons", nargs="+", type=int, default=[2023, 2024],
                   help="Season start years (e.g., 2023 2024). Default: 2023 2024.")
    p.add_argument("--force", action="store_true", help="Re-download even if file exists")
    p.add_argument("--delay", type=float, default=1.0, help="Delay between requests (sec)")
    args = p.parse_args()

    # Validasi league codes
    invalid = [l for l in args.leagues if l not in ALL_LEAGUES]
    if invalid:
        print(f"[FAIL] Unknown league codes: {invalid}")
        print(f"Valid: {sorted(ALL_LEAGUES.keys())}")
        sys.exit(1)

    print("=" * 60)
    print("BTTS ENGINE — DOWNLOAD HISTORICAL CSV")
    print("=" * 60)
    print(f"Leagues : {args.leagues}")
    print(f"Seasons : {args.seasons}")
    print(f"Force   : {args.force}")
    print(f"Output  : {RAW_DIR}")
    print()

    stats = {"OK": 0, "SKIP_EXISTS": 0, "HTTP_ERROR": 0,
             "EMPTY": 0, "ERROR": 0}
    total_bytes = 0

    for league in args.leagues:
        for season in args.seasons:
            res = download_one(league, season, force=args.force, delay=args.delay)
            stats[res["status"]] = stats.get(res["status"], 0) + 1
            total_bytes += res["bytes"]

            tag = res["status"]
            if tag == "OK":
                print(f"[{tag:11s}] {league} {season}/{season+1} -> {res['bytes']:,} bytes")
            elif tag == "SKIP_EXISTS":
                print(f"[{tag:11s}] {league} {season}/{season+1} (already exists)")
            else:
                print(f"[{tag:11s}] {league} {season}/{season+1} :: {res['error']}")

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:12s}: {v}")
    print(f"  total bytes : {total_bytes:,}")
    print()

    # Exit code: FAIL hanya jika tidak ada file yang berhasil/skip
    good = stats["OK"] + stats["SKIP_EXISTS"]
    if good == 0:
        print("STATUS: FAIL (no files downloaded)")
        sys.exit(1)
    print(f"STATUS: PASS ({good} file(s) available in data/raw/)")
    print("NEXT  : Jalankan scripts/check_data.py")

if __name__ == "__main__":
    main()
