#!/usr/bin/env python3
"""
Phase 2 — D2: BACKUP + SNAPSHOT COPY + SHA-256 VERIFY
=====================================================

Fix (v2): Cross-check vs D1a SKIPS files not in D1a (expected for 2627
since D1a only audits 2425+2526). Cross-check is bonus safety for files
that WERE audited by D1a.

Tasks:
  1. Backup raw 2627 files to data/backups/phase2_D2_<ts>/
  2. Copy 6 files 2627 -> data/raw/snapshots/2627/2026-10-30/ (COPY, not MOVE)
  3. SHA-256 verify: original vs snapshot (must match 6/6)
  4. Cross-check vs D1a for 2425/2526 (SKIP if absent, FAIL if mismatch)
  5. Write SHA256SUMS.txt manifest inside snapshot folder
  6. Write backup checksums manifest

Safety:
  - NEVER overwrite existing snapshot (SKIP if identical, STOP if different)
  - NEVER modify/delete raw originals
  - NEVER delete old backups (each run uses fresh timestamp)
  - All operations verified with SHA-256

TIDAK mengerjakan:
  - D1b (audit 18 files)
  - D3 (build leagues.csv)
  - processed/ database
  - BTTS stats / ML features / fuzzy matching
"""
import sys
import hashlib
import shutil
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR     = PROJECT_ROOT / "data"
RAW_DIR      = DATA_DIR / "raw"
BACKUP_DIR   = DATA_DIR / "backups"
SNAPSHOT_DIR = RAW_DIR / "snapshots" / "2627" / "2026-10-30"
REPORTS_DIR  = DATA_DIR / "reports"

LEAGUES = ["E0", "D1", "SP1", "I1", "F1", "N1"]
SEASON_CODE = "2627"

D1A_SHA256_FILE = REPORTS_DIR / "phase2_audit_raw_sha256_D1a.txt"

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"

def load_d1a_sha256(path: Path) -> dict:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, rel = parts[0], parts[1].strip()
        result[rel] = sha
    return result

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 2 — D2: BACKUP + SNAPSHOT COPY + SHA-256 VERIFY")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Raw dir      : {RAW_DIR}")
    print(f"Snapshot dir : {SNAPSHOT_DIR}")
    print()

    # ---- Pre-flight: verify source files exist ----
    sources = []
    for league in LEAGUES:
        f = RAW_DIR / f"{league}_{SEASON_CODE}.csv"
        if not f.exists():
            print(f"[FAIL] Source file missing: {f}")
            sys.exit(1)
        sources.append((league, f))

    print(f"[OK] All 6 source files present in {RAW_DIR}/")
    print()

    # ---- Compute source SHA-256 ----
    print("--- SOURCE SHA-256 (raw originals) ---")
    source_sha = {}
    for league, f in sources:
        sha = sha256_file(f)
        source_sha[league] = sha
        size = f.stat().st_size
        print(f"  {f.name:15s} {human_size(size):>10s}  {sha}")
    print()

    # ---- Cross-check vs D1a (SKIP files not in D1a) ----
    print("--- CROSS-CHECK vs D1a REPORT ---")
    d1a_sha = load_d1a_sha256(D1A_SHA256_FILE)
    if not d1a_sha:
        print(f"  [INFO] {D1A_SHA256_FILE.name} not found — skipping cross-check.")
        print(f"         D1a may not have been run, or report was cleaned.")
    else:
        print(f"  [INFO] Loaded {len(d1a_sha)} SHA-256 entries from D1a report.")
        checked = 0
        skipped = 0
        mismatches = []
        for league, f in sources:
            rel = str(f.relative_to(PROJECT_ROOT))
            if rel not in d1a_sha:
                # Expected for 2627: D1a only audits 2425+2526
                print(f"  [SKIP] {f.name:15s} not in D1a (expected: 2627 not "
                      f"audited by D1a)")
                skipped += 1
                continue
            if d1a_sha[rel] != source_sha[league]:
                mismatches.append((league, f.name))
                print(f"  [FAIL] {f.name:15s} SHA mismatch vs D1a!")
                print(f"         d1a: {d1a_sha[rel]}")
                print(f"         now: {source_sha[league]}")
            else:
                print(f"  [OK]   {f.name:15s} SHA matches D1a")
                checked += 1
        if mismatches:
            print()
            print(f"  [FAIL] {len(mismatches)} file(s) changed since D1a: {mismatches}")
            print(f"  STOP — raw originals audited by D1a must not change.")
            sys.exit(1)
        print()
        print(f"  Result: {checked} verified against D1a, {skipped} skipped "
              f"(not in D1a scope).")
    print()

    # ---- Snapshot pre-flight: conflicts ----
    print("--- SNAPSHOT PRE-FLIGHT ---")
    existing_files = []
    if SNAPSHOT_DIR.exists():
        for league in LEAGUES:
            sf = SNAPSHOT_DIR / f"{league}_{SEASON_CODE}.csv"
            if sf.exists():
                existing_files.append((league, sf))
    if not existing_files:
        print(f"  [OK] No existing snapshot files. Will create fresh.")
    else:
        print(f"  [CHECK] Snapshot folder has {len(existing_files)} existing file(s):")
        for league, sf in existing_files:
            snap_sha = sha256_file(sf)
            if snap_sha == source_sha[league]:
                print(f"    - {sf.name}: SHA match -> will SKIP (not overwrite)")
            else:
                print(f"    - {sf.name}: SHA MISMATCH!")
                print(f"        existing: {snap_sha}")
                print(f"        source  : {source_sha[league]}")
                print(f"  [FAIL] Cannot overwrite existing snapshot with different content.")
                print(f"  STOP — resolve manually (rename folder or new dated snapshot).")
                sys.exit(1)
    print()

    # ---- STEP 1: Backup ----
    print("=" * 78)
    print("STEP 1: BACKUP")
    print("=" * 78)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = BACKUP_DIR / f"phase2_D2_{ts}"
    backup_root.mkdir(parents=True, exist_ok=False)
    backup_snapshot_dir = backup_root / "raw_2627_sources"
    backup_snapshot_dir.mkdir(parents=True, exist_ok=True)

    print(f"  Backup target: {backup_root}")
    backed_up = []
    for league, src in sources:
        dst = backup_snapshot_dir / src.name
        shutil.copy2(src, dst)
        bak_sha = sha256_file(dst)
        if bak_sha != source_sha[league]:
            print(f"  [FAIL] Backup SHA mismatch for {src.name}!")
            sys.exit(1)
        backed_up.append((league, dst, bak_sha))
        print(f"  [OK] {src.name:15s} -> {dst.relative_to(PROJECT_ROOT)} "
              f"({human_size(dst.stat().st_size)})")

    backup_manifest = backup_root / "BACKUP_SHA256SUMS.txt"
    with open(backup_manifest, "w", encoding="utf-8") as f:
        for league, dst, sha in backed_up:
            f.write(f"{sha}  {dst.relative_to(backup_root)}\n")
    print(f"  [OK] Backup manifest: {backup_manifest.relative_to(PROJECT_ROOT)}")
    print()

    # ---- STEP 2: Snapshot copy ----
    print("=" * 78)
    print("STEP 2: SNAPSHOT COPY (COPY, not MOVE)")
    print("=" * 78)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_files = []
    for league, src in sources:
        dst = SNAPSHOT_DIR / src.name
        if dst.exists():
            snap_sha = sha256_file(dst)
            if snap_sha == source_sha[league]:
                print(f"  [SKIP] {src.name:15s} exists with same SHA (not overwriting)")
                snapshot_files.append((league, dst, snap_sha))
                continue
            else:
                print(f"  [FAIL] {src.name}: existing differs from source!")
                sys.exit(1)
        shutil.copy2(src, dst)
        snap_sha = sha256_file(dst)
        if snap_sha != source_sha[league]:
            print(f"  [FAIL] Snapshot SHA mismatch for {src.name}!")
            sys.exit(1)
        snapshot_files.append((league, dst, snap_sha))
        print(f"  [OK] {src.name:15s} -> snapshots/2627/2026-10-30/ "
              f"({human_size(dst.stat().st_size)})")
    print()

    # ---- STEP 3: SHA-256 verification matrix ----
    print("=" * 78)
    print("STEP 3: SHA-256 VERIFICATION (6/6)")
    print("=" * 78)
    print(f"  {'League':6s} {'File':15s} {'Size':>10s} {'Match':>8s}")
    print("  " + "-" * 60)
    verify_fail = 0
    for league, sf, sf_sha in snapshot_files:
        src = RAW_DIR / f"{league}_{SEASON_CODE}.csv"
        src_size = src.stat().st_size
        match = (sf_sha == source_sha[league])
        if not match:
            verify_fail += 1
        print(f"  {league:6s} {sf.name:15s} {human_size(src_size):>10s} "
              f"{'✅ YES' if match else '❌ NO':>8s}")
    if verify_fail > 0:
        print(f"  [FAIL] {verify_fail} file(s) SHA mismatch!")
        sys.exit(1)
    print(f"  [OK] ALL 6/6 files SHA-256 identical.")
    print()

    # ---- STEP 4: Write snapshot manifest ----
    print("=" * 78)
    print("STEP 4: SNAPSHOT MANIFEST")
    print("=" * 78)
    manifest_path = SNAPSHOT_DIR / "SHA256SUMS.txt"
    lines = [f"{sf_sha}  {sf.name}" for _, sf, sf_sha in snapshot_files]
    header = (f"# Phase 2 D2 Snapshot Manifest\n"
              f"# Season: 2026 (2026/27)\n"
              f"# Snapshot: 2026-10-30\n"
              f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"# Files: {len(snapshot_files)}\n"
              f"# Manifest file (SHA256SUMS.txt) is EXCLUDED from checksums.\n")
    manifest_path.write_text(header + "\n".join(sorted(lines)) + "\n",
                             encoding="utf-8")
    print(f"  [OK] {manifest_path.relative_to(PROJECT_ROOT)}")
    print()

    # ---- STEP 5: Post-flight raw unchanged ----
    print("=" * 78)
    print("STEP 5: POST-FLIGHT — RAW ORIGINALS UNCHANGED?")
    print("=" * 78)
    for league, src in sources:
        sha_now = sha256_file(src)
        if sha_now != source_sha[league]:
            print(f"  [FAIL] {src.name}: raw original changed during D2!")
            sys.exit(1)
        print(f"  [OK] {src.name:15s} unchanged ({sha_now[:16]}...)")
    print()
    print("  [OK] All raw originals identical before & after D2.")
    print()

    # ---- D2 report ----
    print("=" * 78)
    print("D2 REPORT")
    print("=" * 78)
    report_path = REPORTS_DIR / f"phase2_d2_report_{ts}.txt"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_lines = [
        f"# Phase 2 — D2 Report",
        f"Timestamp: {ts}",
        f"Status: PASS",
        "",
        f"## Backup",
        f"Location: {backup_root.relative_to(PROJECT_ROOT)}",
        f"Files: {len(backed_up)}",
        f"Manifest: {backup_manifest.relative_to(PROJECT_ROOT)}",
        "",
        f"## Snapshot",
        f"Location: {SNAPSHOT_DIR.relative_to(PROJECT_ROOT)}",
        f"Files: {len(snapshot_files)}",
        f"Manifest: {manifest_path.relative_to(PROJECT_ROOT)}",
        "",
        f"## SHA-256 Verification",
        f"Result: 6/6 MATCH",
        "",
        "| League | File | SHA-256 |",
        "|--------|------|---------|",
    ]
    for league, sf, sf_sha in snapshot_files:
        report_lines.append(f"| {league} | {sf.name} | {sf_sha} |")
    report_lines.append("")
    report_lines.append(f"Raw originals unchanged: YES")
    report_lines.append(f"D1a cross-check: {'verified (with skip for non-D1a files)' if d1a_sha else 'SKIPPED (no D1a report)'}")
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"  [OK] {report_path.relative_to(PROJECT_ROOT)}")
    print()

    # ---- Final acceptance ----
    print("=" * 78)
    print("D2 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("Backup created (6 files)", len(backed_up) == 6),
        ("Snapshot exactly 6 files", len(snapshot_files) == 6),
        ("SHA-256 match 6/6", verify_fail == 0),
        ("Raw originals unchanged (post-flight)", True),
        ("Manifest written", manifest_path.exists()),
        ("No overwrite/move of raw", True),
        ("No deletion", True),
    ]
    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print()
    if all_ok:
        print("STATUS: D2 PASS ✅")
        print()
        print("NEXT: User approval → D1b (re-run phase2_audit_schema.py,")
        print("      now with 18 files including snapshot 2627).")
        print()
        print("DO NOT run D1b without explicit user approval.")
        sys.exit(0)
    else:
        print("STATUS: D2 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
