#!/usr/bin/env python3
"""
Phase 1 — Cek ketersediaan file CSV di football-data.co.uk.
HANYA HEAD request. Tidak download. Tidak validasi isi.
Status ini BUKAN bukti CSV valid (lihat keputusan #6).
"""
import sys, argparse, time
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import requests
from config.leagues import PRIMARY_LEAGUES, download_url, season_code

def head_check(url: str, timeout: int = 15):
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": "BTTS-Engine/0.1"})
        if r.status_code == 200:
            size = r.headers.get("Content-Length", "?")
            return "OK", size
        return f"HTTP_{r.status_code}", "0"
    except Exception as e:
        return "ERR", str(e)[:60]

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--leagues", nargs="+", default=list(PRIMARY_LEAGUES.keys()))
    p.add_argument("--seasons", nargs="+", type=int,
                   default=[2022, 2023, 2024, 2025, 2026])
    p.add_argument("--delay", type=float, default=0.6)
    args = p.parse_args()

    print("=" * 62)
    print("AVAILABILITY CHECK (HEAD only — BUKAN validasi isi)")
    print("=" * 62)
    print(f"{'League':8s} {'Season':10s} {'Code':8s} {'HEAD':10s} {'Size':>10s}")
    print("-" * 62)

    available, missing = [], []
    for league in args.leagues:
        for s in args.seasons:
            url = download_url(league, s)
            status, info = head_check(url)
            sc = season_code(s)
            label = f"{s}/{s+1}"
            if status == "OK":
                print(f"{league:8s} {label:10s} {sc:8s} {'OK':10s} {info:>10s}")
                available.append((league, s, sc))
            else:
                print(f"{league:8s} {label:10s} {sc:8s} {status:10s} {'-':>10s}")
                missing.append((league, s, status))
            time.sleep(args.delay)

    print("-" * 62)
    print(f"AVAILABLE (HEAD 200): {len(available)}  |  NOT FOUND: {len(missing)}")
    if missing:
        print("\nNot found detail:")
        for league, s, st in missing:
            print(f"  - {league} {s}/{s+1}  [{st}]")
    print("\nNOTE: HEAD 200 ≠ CSV valid. Validasi isi wajib setelah download.")

if __name__ == "__main__":
    main()
