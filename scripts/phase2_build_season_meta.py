#!/usr/bin/env python3
"""
Phase 2 — D4: BUILD season_meta.csv
====================================

Input : LOCKED constants (from Phase 1 & Phase 2 decisions)
Output: data/processed/season_meta.csv (4 rows)

Schema (LOCKED):
  season         : int (start_year convention: 2023, 2024, 2025, 2026)
  season_label   : str "YYYY/YY+1" e.g. "2024/25"
  season_status  : {COMPLETED, IN_PROGRESS}
  priority       : {HISTORICAL_BACKUP, SECONDARY, PRIMARY, RECENT_BOOST}
  weight         : float in [0.0, 1.0]
  train_eligible : int 0 or 1
  snapshot_date  : "YYYY-MM-DD" or "" (empty for non-snapshot seasons)
  notes          : str

Rules:
  - Deterministic (sorted by season)
  - Idempotent: identical -> SKIP; different -> backup then write
  - Validation: 4 rows, unique int season, weight∈[0,1],
                train_eligible∈{0,1}, snapshot_date consistent

LOCKED data (from user decisions, DO NOT CHANGE):
  2023/24 : HISTORICAL_BACKUP, weight 0.0, train_eligible 0, no snapshot
  2024/25 : SECONDARY,         weight 0.4, train_eligible 1, no snapshot
  2025/26 : PRIMARY,           weight 1.0, train_eligible 1, no snapshot
  2026/27 : RECENT_BOOST,      weight 0.8, train_eligible 1, snapshot 2026-10-30

TIDAK mengerjakan:
  - Baca raw CSV
  - matches.csv / teams.csv
  - BTTS stats / ML features
"""
import sys
import csv
import shutil
import hashlib
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
BACKUP_DIR    = DATA_DIR / "backups"
REPORTS_DIR   = DATA_DIR / "reports"

OUT_CSV = PROCESSED_DIR / "season_meta.csv"

FIELDNAMES = [
    "season", "season_label", "season_status", "priority",
    "weight", "train_eligible", "snapshot_date", "notes"
]

# LOCKED seasons (per user Phase 1/2 decisions)
LOCKED_SEASONS = [
    {
        "season":         2023,
        "season_label":   "2023/24",
        "season_status":  "COMPLETED",
        "priority":       "HISTORICAL_BACKUP",
        "weight":         0.0,
        "train_eligible": 0,
        "snapshot_date":  "",
        "notes":          "backup-only, excluded from matches.csv",
    },
    {
        "season":         2024,
        "season_label":   "2024/25",
        "season_status":  "COMPLETED",
        "priority":       "SECONDARY",
        "weight":         0.4,
        "train_eligible": 1,
        "snapshot_date":  "",
        "notes":          "full-season, weight=experimental",
    },
    {
        "season":         2025,
        "season_label":   "2025/26",
        "season_status":  "COMPLETED",
        "priority":       "PRIMARY",
        "weight":         1.0,
        "train_eligible": 1,
        "snapshot_date":  "",
        "notes":          "full-season, primary training",
    },
    {
        "season":         2026,
        "season_label":   "2026/27",
        "season_status":  "IN_PROGRESS",
        "priority":       "RECENT_BOOST",
        "weight":         0.8,
        "train_eligible": 1,
        "snapshot_date":  "2026-10-30",
        "notes":          "partial-season snapshot frozen 2026-10-30",
    },
]

VALID_STATUS   = {"COMPLETED", "IN_PROGRESS"}
VALID_PRIORITY = {"HISTORICAL_BACKUP", "SECONDARY", "PRIMARY", "RECENT_BOOST"}

# ---------- Helpers ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _coerce_row(r: dict) -> dict:
    """Normalize a row for comparison: ints/floats to canonical str form."""
    return {
        "season":         str(int(r["season"])),
        "season_label":   str(r["season_label"]).strip(),
        "season_status":  str(r["season_status"]).strip(),
        "priority":       str(r["priority"]).strip(),
        "weight":         f"{float(r['weight']):.4f}",
        "train_eligible": str(int(r["train_eligible"])),
        "snapshot_date":  str(r.get("snapshot_date", "")).strip(),
        "notes":          str(r.get("notes", "")).strip(),
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

# ---------- Validate ----------

def validate_rows(rows: list) -> list:
    """
    Validate rows. Handles typed (in-memory) OR str (CSV read-back).
    Returns list of problem strings; empty = valid.
    """
    problems = []
    if len(rows) != 4:
        problems.append(f"expected 4 rows, got {len(rows)}")
        return problems

    seasons = []
    for r in rows:
        try:
            s = int(r["season"])
            seasons.append(s)
        except (ValueError, TypeError):
            problems.append(f"season not int-parseable: {r['season']!r}")
            continue

        label = str(r["season_label"]).strip()
        # season_label must match s and s+1 (start_year convention)
        expected_label = f"{s}/{str(s+1)[-2:]}"
        if label != expected_label:
            problems.append(f"season_label mismatch for {s}: "
                            f"{label!r} != expected {expected_label!r}")

        status = str(r["season_status"]).strip()
        if status not in VALID_STATUS:
            problems.append(f"invalid season_status {status!r} for {s}")

        priority = str(r["priority"]).strip()
        if priority not in VALID_PRIORITY:
            problems.append(f"invalid priority {priority!r} for {s}")

        try:
            w = float(r["weight"])
            if not (0.0 <= w <= 1.0):
                problems.append(f"weight out of [0,1] for {s}: {w}")
        except (ValueError, TypeError):
            problems.append(f"weight not float-parseable for {s}: {r['weight']!r}")

        try:
            te = int(r["train_eligible"])
            if te not in (0, 1):
                problems.append(f"train_eligible not 0/1 for {s}: {te}")
        except (ValueError, TypeError):
            problems.append(f"train_eligible not int-parseable for {s}: "
                            f"{r['train_eligible']!r}")

        snap = str(r.get("snapshot_date", "")).strip()
        if status == "IN_PROGRESS":
            if snap == "":
                problems.append(f"IN_PROGRESS season {s} requires snapshot_date")
            else:
                # Validate ISO date format
                try:
                    datetime.strptime(snap, "%Y-%m-%d")
                except ValueError:
                    problems.append(f"snapshot_date not ISO (YYYY-MM-DD) for {s}: "
                                    f"{snap!r}")
        else:
            if snap != "":
                problems.append(f"COMPLETED season {s} should have empty "
                                f"snapshot_date, got {snap!r}")

    if len(set(seasons)) != len(seasons):
        problems.append(f"duplicate season values: {seasons}")
    if set(seasons) != {2023, 2024, 2025, 2026}:
        problems.append(f"season set mismatch: got {set(seasons)}, "
                        f"expected {{2023, 2024, 2025, 2026}}")

    return problems

# ---------- Main ----------

def main():
    print("=" * 78)
    print("PHASE 2 — D4: BUILD season_meta.csv")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Output       : {OUT_CSV}")
    print()

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- Build (sorted by season) ----
    print("--- BUILD (LOCKED constants) ---")
    rows = sorted(LOCKED_SEASONS, key=lambda r: r["season"])
    for r in rows:
        snap = r["snapshot_date"] if r["snapshot_date"] else "-"
        print(f"  {r['season']} ({r['season_label']}) "
              f"{r['season_status']:12s} {r['priority']:18s} "
              f"weight={r['weight']:.1f} train={r['train_eligible']} "
              f"snap={snap}")
    print()

    # ---- Validate pre-write ----
    print("--- VALIDATE (pre-write) ---")
    problems = validate_rows(rows)
    if problems:
        print("  [FAIL] Validation problems:")
        for p in problems:
            print(f"    - {p}")
        print()
        print("STATUS: D4 FAIL ❌")
        sys.exit(1)
    print("  [OK] 4 rows, unique seasons, statuses/priorities valid, "
          "weights in [0,1], snapshot dates consistent.")
    print()

    # ---- Idempotent write ----
    print("--- WRITE (idempotent) ---")
    if file_equals_csv(OUT_CSV, rows):
        print(f"  [SKIP] {OUT_CSV.relative_to(PROJECT_ROOT)} already identical.")
        write_action = "SKIP_IDENTICAL"
    else:
        if OUT_CSV.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = BACKUP_DIR / f"phase2_D4_season_meta_{ts}.csv"
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
    report_path = REPORTS_DIR / f"phase2_d4_report_{ts}.txt"
    report_lines = [
        f"# Phase 2 — D4 Report",
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
        f"| season | season_label | season_status | priority | weight | train_eligible | snapshot_date |",
        f"|---|---|---|---|---|---|---|",
    ]
    for r in verify_rows:
        snap = r["snapshot_date"] if r["snapshot_date"] else "-"
        report_lines.append(
            f"| {r['season']} | {r['season_label']} | {r['season_status']} "
            f"| {r['priority']} | {r['weight']} | {r['train_eligible']} | {snap} |"
        )
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"  [OK] Report: {report_path.relative_to(PROJECT_ROOT)}")
    print()

    # ---- Acceptance ----
    print("=" * 78)
    print("D4 ACCEPTANCE")
    print("=" * 78)
    verify_seasons = [int(r["season"]) for r in verify_rows]
    verify_weights = [float(r["weight"]) for r in verify_rows]
    verify_te = [int(r["train_eligible"]) for r in verify_rows]

    checks = [
        ("Output file exists",                    OUT_CSV.exists()),
        ("Exactly 4 rows",                        len(verify_rows) == 4),
        ("Fields match schema",                   verify_fields == FIELDNAMES),
        ("Season set = {2023,2024,2025,2026}",    set(verify_seasons) == {2023, 2024, 2025, 2026}),
        ("No duplicate season",                   len(set(verify_seasons)) == 4),
        ("All weights in [0.0, 1.0]",             all(0.0 <= w <= 1.0 for w in verify_weights)),
        ("All train_eligible ∈ {0,1}",            all(te in (0, 1) for te in verify_te)),
        ("All season_status valid",
            all(r["season_status"] in VALID_STATUS for r in verify_rows)),
        ("All priority valid",
            all(r["priority"] in VALID_PRIORITY for r in verify_rows)),
        ("Weight matches locked values",
            dict(zip(verify_seasons, verify_weights)) == {2023: 0.0, 2024: 0.4, 2025: 1.0, 2026: 0.8}),
        ("Train_eligible matches locked values",
            dict(zip(verify_seasons, verify_te)) == {2023: 0, 2024: 1, 2025: 1, 2026: 1}),
        ("Priority matches locked values",
            {int(r["season"]): r["priority"] for r in verify_rows} ==
            {2023: "HISTORICAL_BACKUP", 2024: "SECONDARY", 2025: "PRIMARY", 2026: "RECENT_BOOST"}),
        ("Snapshot_date: only 2026 non-empty",
            {int(r["season"]): r["snapshot_date"] for r in verify_rows if r["snapshot_date"]} ==
            {2026: "2026-10-30"}),
    ]
    all_ok = all(ok for _, ok in checks)
    for desc, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {desc}")
    print()
    if all_ok:
        print("STATUS: D4 PASS ✅")
        print()
        print("NEXT: User approval → D5 (build teams.csv)")
        sys.exit(0)
    else:
        print("STATUS: D4 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
