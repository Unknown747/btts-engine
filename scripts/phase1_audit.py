#!/usr/bin/env python3
"""
Phase 1 — Project Audit
Cek environment, dependencies, struktur folder, dan akses internet.
Tidak mengubah apapun. Read-only.
"""
import sys
import os
import platform
import importlib
from pathlib import Path

# Tambahkan project root ke sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_PY = (3, 10)
REQUIRED_MODULES = ["pandas", "numpy", "requests", "scipy", "sklearn"]
OPTIONAL_MODULES = ["xgboost", "lightgbm", "matplotlib"]

def check_python():
    v = sys.version_info
    ok = v >= REQUIRED_PY
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] Python {v.major}.{v.minor}.{v.micro} (required >= {REQUIRED_PY[0]}.{REQUIRED_PY[1]})")
    return ok

def check_module(name):
    try:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "unknown")
        print(f"[PASS] module '{name}' version {ver}")
        return True
    except ImportError:
        print(f"[FAIL] module '{name}' NOT installed")
        return False

def check_optional(name):
    try:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "unknown")
        print(f"[INFO] optional '{name}' version {ver}")
    except ImportError:
        print(f"[WARN] optional '{name}' not installed (OK for Phase 1)")

def check_dirs():
    dirs = ["config", "data/raw", "data/processed", "data/mappings",
            "data/features", "data/predictions", "data/backtest",
            "data/reports", "scripts", "src/btts"]
    all_ok = True
    for d in dirs:
        p = PROJECT_ROOT / d
        if p.exists():
            print(f"[PASS] dir exists: {d}")
        else:
            print(f"[FAIL] dir missing: {d}")
            all_ok = False
    return all_ok

def check_internet():
    try:
        import requests
        r = requests.head("https://www.football-data.co.uk",
                          timeout=8, allow_redirects=True)
        if r.status_code < 400:
            print(f"[PASS] football-data.co.uk reachable (HTTP {r.status_code})")
            return True
        print(f"[WARN] football-data.co.uk returned {r.status_code}")
        return False
    except Exception as e:
        print(f"[WARN] internet check failed: {e}")
        return False

def main():
    print("=" * 60)
    print("BTTS ENGINE — PHASE 1 AUDIT")
    print("=" * 60)
    print(f"OS       : {platform.system()} {platform.release()} {platform.machine()}")
    print(f"Project  : {PROJECT_ROOT}")
    print()

    results = {}

    print("--- PYTHON ---")
    results["python"] = check_python()
    print()

    print("--- REQUIRED MODULES ---")
    req_ok = True
    for m in REQUIRED_MODULES:
        if not check_module(m):
            req_ok = False
    results["required_modules"] = req_ok
    print()

    print("--- OPTIONAL MODULES ---")
    for m in OPTIONAL_MODULES:
        check_optional(m)
    print()

    print("--- DIRECTORIES ---")
    results["dirs"] = check_dirs()
    print()

    print("--- INTERNET ---")
    results["internet"] = check_internet()
    print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for k, v in results.items():
        print(f"  {k:20s}: {'PASS' if v else 'WARN/FAIL'}")
    print()

    critical_ok = results["python"] and results["required_modules"] and results["dirs"]
    if critical_ok:
        print("STATUS: PASS (critical checks OK)")
        print("NEXT  : Jalankan scripts/download_historical.py")
        sys.exit(0)
    else:
        print("STATUS: FAIL (perbaiki critical checks dulu)")
        print("TIP   : pip install -r requirements.txt")
        sys.exit(1)

if __name__ == "__main__":
    main()
