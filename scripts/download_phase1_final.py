#!/usr/bin/env python3
"""
Phase 1 Final — Download 12 file baru (2526 + 2627).
- TIDAK menimpa file existing tanpa backup.
- Skip 2425 karena sudah ada.
- Skip 2223 karena tidak dipakai.
- Retry 3x per file sebelum fail.
"""
import sys, time, argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import requests
from config.leagues import PRIMARY_LEAGUES, download_url, season_code

RAW_DIR = PROJECT_ROOT / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

TARGET_SEASONS = [2025, 2026]  # 2526, 2627

def download_one(league: str, start_year: int, force: bool = False,
                 timeout: int = 30, retries: int = 3) -> dict:
    url = download_url(league, start_year)
    sc = season_code(start_year)
    out = RAW_DIR / f"{league}_{sc}.csv"

    result = {"league": league, "season": start_year, "code": sc,
              "url": url, "path": str(out), "status": None,
              "bytes": 0, "error": None, "attempts": 0}

    if out.exists() and not force:
        result["status"] = "SKIP_EXISTS"
        result["bytes"] = out.stat().st_size
        return result

    headers = {"User-Agent": "BTTS-Engine/0.1 (+termux-ubuntu)"}
    for attempt in range(1, retries + 1):
        result["attempts"] = attempt
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code != 200:
                result["error"] = f"HTTP {r.status_code}"
                time.sleep(2)
                continue
            if not r.content or len(r.content) < 100:
                result["error"] = f"Body too small ({len(r.content)}b)"
                time.sleep(2)
                continue
            out.write_bytes(r.content)
            result["status"] = "OK"
            result["bytes"] = len(r.content)
            result["error"] = None
            return result
        except Exception as e:
            result["error"] = str(e)[:140]
            time.sleep(2)

    result["status"] = "FAIL"
    return result

def main():
    p = argparse.ArgumentParser(description="Phase 1 Final Download")
    p.add_argument("--leagues", nargs="+", default=list(PRIMARY_LEAGUES.keys()))
    p.add_argument("--seasons", nargs="+", type=int, default=TARGET_SEASONS)
    p.add_argument("--force", action="store_true",
                   help="Force re-download meskipun file existing (BACKUP dulu!)")
    p.add_argument("--delay", type=float, default=1.2)
    args = p.parse_args()

    print("=" * 66)
    print("PHASE 1 FINAL — DOWNLOAD 2526 + 2627")
    print("=" * 66)
    print(f"Leagues : {args.leagues}")
    print(f"Seasons : {args.seasons}")
    print(f"Force   : {args.force}")
    print(f"Output  : {RAW_DIR}")
    print()

    if args.force:
        print("[WARN] --force aktif. File existing akan DITIMPA.")
        print("[WARN] Pastikan sudah jalankan scripts/backup_existing.py dulu.")
        print()

    rows = []
    for league in args.leagues:
        for season in args.seasons:
            res = download_one(league, season, force=args.force)
            rows.append(res)
            tag = res["status"]
            size = f"{res['bytes']:,}b" if res["bytes"] else "-"
            extra = f" :: {res['error']}" if res["error"] else ""
            print(f"[{tag:12s}] {league} {season}/{season+1} "
                  f"({res['code']}) {size:>12s}{extra}")
            if tag == "OK":
                time.sleep(args.delay)

    print()
    print("=" * 66)
    print("SUMMARY DOWNLOAD")
    print("=" * 66)
    by_status = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    for k, v in sorted(by_status.items()):
        print(f"  {k:14s}: {v}")
    total_b = sum(r["bytes"] for r in rows)
    print(f"  total bytes    : {total_b:,}")

    failed = [r for r in rows if r["status"] == "FAIL"]
    if failed:
        print()
        print("FAILED FILES:")
        for r in failed:
            print(f"  - {r['league']} {r['season']}/{r['season']+1} "
                  f"[{r['error']}] (attempts={r['attempts']})")
        print()
        print("STATUS: FAIL — ulangi download file gagal saja")
        sys.exit(1)

    print()
    print("STATUS: PASS — semua target tersedia di data/raw/")
    print("NEXT  : python3 scripts/validate_raw.py")

if __name__ == "__main__":
    main()
