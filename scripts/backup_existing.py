#!/usr/bin/env python3
"""
Phase 1 — Backup semua CSV di data/raw/ sebelum operasi apapun.
Backup disimpan di data/raw_backup/YYYYMMDD_HHMMSS/.
Idempotent: kalau sudah ada backup hari ini, tetap buat baru (timestamp).
"""
import sys, shutil
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR      = PROJECT_ROOT / "data" / "raw"
BACKUP_ROOT  = PROJECT_ROOT / "data" / "raw_backup"

def main():
    if not RAW_DIR.exists():
        print(f"[FAIL] {RAW_DIR} tidak ada")
        sys.exit(1)

    files = sorted(RAW_DIR.glob("*.csv"))
    if not files:
        print("[INFO] Tidak ada file CSV untuk di-backup (skip, bukan FAIL).")
        sys.exit(0)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_ROOT / ts
    dst.mkdir(parents=True, exist_ok=True)

    total = 0
    for f in files:
        shutil.copy2(f, dst / f.name)
        total += f.stat().st_size

    print("=" * 60)
    print("BACKUP RAW CSV")
    print("=" * 60)
    print(f"Source : {RAW_DIR}")
    print(f"Dest   : {dst}")
    print(f"Files  : {len(files)}")
    print(f"Bytes  : {total:,}")
    print()
    print("STATUS: PASS")

if __name__ == "__main__":
    main()
