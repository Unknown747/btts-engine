#!/usr/bin/env python3
"""
Phase 2 — D3: BUILD leagues.csv
================================

Fix (v2): validate_rows() coerces tier to int before checking, because
CSV read-back yields str values ('1' instead of 1). This is normal CSV
behavior, not a data issue.

Input : config/leagues.py (PRIMARY_LEAGUES static dict)
Output: data/processed/leagues.csv

Schema (LOCKED):
  league_code   : str, primary key
  league_name   : str
  country       : str
  tier          : int-as-str in CSV (all 1)
  source        : str ("football-data.co.uk")
  active_status : str ("ACTIVE")

Rules:
  - Deterministic (sorted by league_code)
  - Idempotent: identical -> SKIP; different -> backup then write
  - Validation: 6 rows, unique code, no null, tier==1 (int-coerced)
"""
import sys
import csv
import shutil
import hashlib
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config.leagues import PRIMARY_LEAGUES

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
BACKUP_DIR    = DATA_DIR / "backups"
REPORTS_DIR   = DATA_DIR / "reports"

OUT_CSV = PROCESSED_DIR / "leagues.csv"

EXPECTED_CODES = {"E0", "D1", "SP1", "I1", "F1", "N1"}

FIELDNAMES = [
    "league_code", "league_name", "country", "tier", "source", "active_status"
]

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _coerce_row(r: dict) -> dict:
    """Normalize a row for comparison: tier -> str."""
    return {
        "league_code":   str(r["league_code"]).strip(),
        "league_name":   str(r["league_name"]).strip(),
        "country":       str(r["country"]).strip(),
        "tier":          str(int(r["tier"])),  # normalize to canonical str-int
        "source":        str(r["source"]).strip(),
        "active_status": str(r["active_status"]).strip(),
    }

def file_equals_csv(path: Path, expected_rows: list) -> bool:
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            rows = [dict(r) for r in reader]
    except Exception:
        return False
    if fields != FIELDNAMES:
        return False
    if len(rows) != len(expected_rows):
        return False
    norm_existing = [_coerce_row(r) for r in rows]
    norm_expected = [_coerce_row(r) for r in expected_rows]
    return norm_existing == norm_expected

# ---------- Build ----------

def build_league_rows() -> list:
    rows = []
    for code in sorted(PRIMARY_LEAGUES.keys()):
        meta = PRIMARY_LEAGUES[code]
        rows.append({
            "league_code":   code,
            "league_name":   meta["name"],
            "country":       meta["country"],
            "tier":          int(meta.get("tier", 1)),
            "source":        "football-data.co.uk",
            "active_status": "ACTIVE",
        })
    return rows

# ---------- Validate (FIXED) ----------

def validate_rows(rows: list) -> list:
    """
    Validate rows. Handles tier as int (in-memory) OR str (CSV read-back).
    Returns list of problem strings; empty = valid.
    """
    problems = []
    if len(rows) != 6:
        problems.append(f"expected 6 rows, got {len(rows)}")

    codes = [str(r["league_code"]).strip() for r in rows]
    if len(set(codes)) != len(codes):
        problems.append(f"duplicate league_code: {codes}")
    if set(codes) != EXPECTED_CODES:
        problems.append(f"league_code set mismatch: got {set(codes)}, "
                        f"expected {EXPECTED_CODES}")

    for r in rows:
        code = str(r["league_code"]).strip()
        # required non-empty
        for field in FIELDNAMES:
            val = r.get(field)
            if val is None or str(val).strip() == "":
                problems.append(f"empty field '{field}' in {code}")

        # tier: coerce to int, must equal 1
        try:
            tier_int = int(r["tier"])
            if tier_int != 1:
                problems.append(f"tier != 1 in {code}: {tier_int}")
        except (ValueError, TypeError):
            problems.append(f"tier not int-parseable in {code}: {r['tier']!r}")

        if str(r["source"]).strip() != "football-data.co.uk":
            problems.append(f"source unexpected in {code}: {r['source']!r}")
        if str(r["active_status"]).strip() != "ACTIVE":
            problems.append(f"active_status unexpected in {code}: {r['active_status']!r}")
    return problems

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 2 — D3: BUILD leagues.csv (v2)")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Output       : {OUT_CSV}")
    print()

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Build ----
    print("--- BUILD (from config/leagues.py PRIMARY_LEAGUES) ---")
    rows = build_league_rows()
    for r in rows:
        print(f"  {r['league_code']:4s} {r['league_name']:20s} "
              f"({r['country']}, tier={r['tier']}, {r['active_status']})")
    print()

    # ---- Validate pre-write ----
    print("--- VALIDATE (pre-write) ---")
    problems = validate_rows(rows)
    if problems:
        print("  [FAIL] Validation problems:")
        for p in problems:
            print(f"    - {p}")
        print()
        print("STATUS: D3 FAIL ❌")
        sys.exit(1)
    print(f"  [OK] 6 rows, unique codes, all fields populated, tier=1 all.")
    print()

    # ---- Idempotent write ----
    print("--- WRITE (idempotent) ---")
    if file_equals_csv(OUT_CSV, rows):
        print(f"  [SKIP] {OUT_CSV.relative_to(PROJECT_ROOT)} already identical.")
        write_action = "SKIP_IDENTICAL"
    else:
        if OUT_CSV.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = BACKUP_DIR / f"phase2_D3_leagues_{ts}.csv"
            shutil.copy2(OUT_CSV, backup_path)
            print(f"  [BACKUP] Existing differs. Saved -> "
                  f"{backup_path.relative_to(PROJECT_ROOT)}")
        with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        print(f"  [OK] Wrote {OUT_CSV.relative_to(PROJECT_ROOT)}")
        write_action = "WROTE"
    print()

    # ---- Verify post-write ----
    print("--- VERIFY (post-write) ---")
    if not OUT_CSV.exists():
        print("  [FAIL] Output file missing after write!")
        sys.exit(1)
    with open(OUT_CSV, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        verify_fields = reader.fieldnames or []
        verify_rows = [dict(r) for r in reader]
    print(f"  fields       : {verify_fields}")
    print(f"  rows         : {len(verify_rows)}")
    print(f"  sha256       : {sha256_file(OUT_CSV)}")
    print(f"  size         : {OUT_CSV.stat().st_size} bytes")

    problems = validate_rows(verify_rows)
    if problems:
        print("  [FAIL] Post-write validation problems:")
        for p in problems:
            print(f"    - {p}")
        sys.exit(1)
    print(f"  [OK] Post-write validation PASS")
    print()

    # ---- Report ----
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORTS_DIR / f"phase2_d3_report_{ts}.txt"
    report_lines = [
        f"# Phase 2 — D3 Report (v2)",
        f"Timestamp: {ts}",
        f"Status: PASS",
        f"",
        f"## Output",
        f"Path: {OUT_CSV.relative_to(PROJECT_ROOT)}",
        f"Rows: {len(verify_rows)}",
        f"Fields: {','.join(verify_fields)}",
        f"SHA-256: {sha256_file(OUT_CSV)}",
        f"Size: {OUT_CSV.stat().st_size} bytes",
        f"Write action: {write_action}",
        f"",
        f"## Content",
        f"| league_code | league_name | country | tier | source | active_status |",
        f"|---|---|---|---|---|---|",
    ]
    for r in verify_rows:
        report_lines.append(
            f"| {r['league_code']} | {r['league_name']} | {r['country']} "
            f"| {r['tier']} | {r['source']} | {r['active_status']} |"
        )
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"  [OK] Report: {report_path.relative_to(PROJECT_ROOT)}")
    print()

    # ---- Acceptance ----
    print("=" * 78)
    print("D3 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("Output file exists",              OUT_CSV.exists()),
        ("Exactly 6 rows",                  len(verify_rows) == 6),
        ("Fields match schema",             verify_fields == FIELDNAMES),
        ("League codes exact set",          set(str(r["league_code"]) for r in verify_rows) == EXPECTED_CODES),
        ("No duplicate league_code",        len(set(str(r["league_code"]) for r in verify_rows)) == 6),
        ("All tier parse to 1",             all(_tier_ok(r) for r in verify_rows)),
        ("All source == football-data",     all(str(r["source"]) == "football-data.co.uk" for r in verify_rows)),
        ("All active_status == ACTIVE",     all(str(r["active_status"]) == "ACTIVE" for r in verify_rows)),
        ("No null/empty fields",            all(all(str(r[f]).strip() for f in FIELDNAMES) for r in verify_rows)),
    ]
    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print()
    if all_ok:
        print("STATUS: D3 PASS ✅")
        print()
        print("NEXT: User approval → D4 (build season_meta.csv)")
        sys.exit(0)
    else:
        print("STATUS: D3 FAIL ❌")
        sys.exit(1)

def _tier_ok(r):
    try:
        return int(r["tier"]) == 1
    except (ValueError, TypeError):
        return False

if __name__ == "__main__":
    main()
