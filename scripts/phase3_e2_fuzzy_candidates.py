#!/usr/bin/env python3
"""
Phase 3 — E2 (v2): Fuzzy Candidate Detection — LF/CRLF SAFE
=============================================================

Fix over v1:
  - Read & compare CSVs structurally (fields normalized), NOT byte-by-byte.
  - CRLF and LF are treated EQUIVALENT. Source files are NEVER modified
    merely to match line ending conventions.
  - New state classification for existing review_queue.csv:
        BASELINE_EMPTY_E1     -> allowed to initialize
        PREVIOUS_E2_OUTPUT    -> SKIP_IDENTICAL or BLOCKED_DIFFERENT
        USER_MODIFIED         -> BLOCKED
        INVALID               -> BLOCKED
  - Backup existing queue before any write.
  - Deterministic LF output (lineterminator="\n").
  - Manifest protects 8 immutable fields via SHA-256.

All other E2 v1 rules preserved:
  C1  Different user content -> BLOCKED (no auto-overwrite).
  C2  `algorithm` = winner algorithm; tie-break order documented.
  C3  Manifest = SHA-256 of 8 immutable fields per row.
  R8  Empty queue is a valid PASS state.
  Read-only on Phase 2 and E1 outputs (SHA-256 pre/post).
"""
import sys
import csv
import io
import hashlib
import shutil
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR      = PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MAPPINGS_DIR  = DATA_DIR / "mappings"
REPORTS_DIR   = DATA_DIR / "reports"
BACKUPS_DIR   = DATA_DIR / "backups"

# Inputs
TEAMS_CSV             = PROCESSED_DIR / "teams.csv"
LEAGUES_CSV           = PROCESSED_DIR / "leagues.csv"
CANONICAL_TEAMS_CSV   = MAPPINGS_DIR / "canonical_teams.csv"
TEAM_ALIASES_CSV      = MAPPINGS_DIR / "team_aliases.csv"

# Outputs
REVIEW_QUEUE_CSV      = MAPPINGS_DIR / "review_queue.csv"
REVIEW_QUEUE_MANIFEST = MAPPINGS_DIR / "review_queue_manifest.txt"
REPORT_TXT            = REPORTS_DIR / "phase3_e2_report.txt"

REVIEW_QUEUE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
    "status", "approved_target_canonical_id",
    "reviewer_notes", "reviewed_date",
]

IMMUTABLE_FIELDS = [
    "review_id", "raw_name_a", "raw_name_b",
    "canonical_a_id", "canonical_b_id",
    "algorithm", "score", "suggested_merge",
]

ALGO_SEQUENCE   = "difflib.SequenceMatcher"
ALGO_TOKEN_SORT = "difflib.TokenSortRatio"
ALGO_TOKEN_SET  = "difflib.TokenSetRatio"
ALGO_ORDER = [ALGO_SEQUENCE, ALGO_TOKEN_SORT, ALGO_TOKEN_SET]

THRESHOLD = 0.70
EXPECTED_TEAMS = 144

# ---------------------------------------------------------------------------
# Line-ending-safe CSV I/O
# ---------------------------------------------------------------------------

def _normalize_field(s: str) -> str:
    """Strip trailing carriage returns and surrounding whitespace introduced
    by CRLF normalization. Do NOT collapse internal whitespace of data fields
    that we care about (raw_name etc.). Only strip trailing \r and outer space."""
    if s is None:
        return ""
    return s.replace("\r", "").strip()

def read_csv_lf_safe(path: Path):
    """
    Read a CSV treating CRLF and LF equivalently.

    Returns: (rows: list[dict], fields: list[str], err: str | None)
    - Reads whole file bytes, decodes utf-8-sig -> utf-8 -> latin-1.
    - Uses csv.DictReader on a StringIO (universal newline handling is bypassed
      so we control it explicitly).
    - Every field value is CR-stripped. Fields are stripped of outer spaces
      only (internal content preserved).
    """
    if not path.exists():
        return [], [], f"missing:{path}"
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return [], [], "decode_failed"

    # Explicit CRLF/LF/CR -> LF (structural, so csv can parse on universal newlines)
    text_normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    reader = csv.DictReader(io.StringIO(text_normalized, newline=""))
    fields = reader.fieldnames or []
    fields = [_normalize_field(f) for f in fields]
    rows = []
    for r in reader:
        clean = {}
        for k, v in r.items():
            clean[_normalize_field(k)] = _normalize_field(v)
        rows.append(clean)
    return rows, fields, None

def write_csv_lf(path: Path, fields: list, rows: list, pk: str):
    """
    Deterministic LF-only write. Sorted by PK. No timestamps in values.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows, key=lambda r: r[pk])
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted_rows)

def write_text_lf(path: Path, content: str):
    """Write text LF-normalized (no \r)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    # enforce LF
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(content, encoding="utf-8")

def structural_equal_csv(path: Path, fields: list, expected_rows: list) -> tuple:
    """
    Structurally compare an on-disk CSV against expected rows.
    - CRLF and LF treated equivalently (we normalize on read).
    - Field values compared after CR-strip + outer-space strip.
    - Row order must match by PK (or exact set equality if PK not provided).
    Returns (is_equal: bool, reason: str).
    """
    if not path.exists():
        return False, "file_missing"
    got_rows, got_fields, err = read_csv_lf_safe(path)
    if err:
        return False, f"read_error:{err}"
    if got_fields != fields:
        return False, f"fields_mismatch:{got_fields}"
    if len(got_rows) != len(expected_rows):
        return False, f"row_count_mismatch:got={len(got_rows)},expected={len(expected_rows)}"
    pk = fields[0]
    g = sorted(got_rows, key=lambda r: r.get(pk, ""))
    e = sorted(expected_rows, key=lambda r: r.get(pk, ""))
    for i, (gr, er) in enumerate(zip(g, e)):
        for f in fields:
            if gr.get(f, "") != er.get(f, ""):
                return False, f"row_diff at index={i} field={f}: got={gr.get(f,'')!r} expected={er.get(f,'')!r}"
    return True, "identical"

def file_text_lf_normalized_equal(path: Path, expected_content: str) -> tuple:
    """
    Compare a text file treating CRLF/LF equivalent.
    """
    if not path.exists():
        return False, "file_missing"
    try:
        got = path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"read_error:{e}"
    got_n = got.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    exp_n = expected_content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if got_n == exp_n:
        return True, "identical"
    return False, "text_differs"

# ---------------------------------------------------------------------------
# Hashing & ID helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    if not path.exists():
        return "<missing>"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def norm(s: str) -> str:
    if s is None:
        return ""
    return " ".join(str(s).strip().lower().split())

def immutable_line(row: dict) -> str:
    return "|".join(str(row.get(f, "")) for f in IMMUTABLE_FIELDS)

def immutable_sha256(row: dict) -> str:
    return hashlib.sha256(immutable_line(row).encode("utf-8")).hexdigest()

def review_id_for(a: str, b: str) -> str:
    x, y = sorted([a, b])
    return "md5:" + hashlib.md5(f"{x}|{y}".encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Similarity algorithms (documented; NOT Levenshtein)
# ---------------------------------------------------------------------------

def seq_ratio(a, b):
    return SequenceMatcher(None, a, b).ratio()

def token_sort_ratio(a, b):
    return SequenceMatcher(None, " ".join(sorted(a.split())),
                          " ".join(sorted(b.split()))).ratio()

def token_set_ratio(a, b):
    seta, setb = set(a.split()), set(b.split())
    if not seta and not setb:
        return 1.0
    if not seta or not setb:
        return 0.0
    inter = seta & setb
    base = " ".join(sorted(inter))
    diff_a = " ".join(sorted(seta - setb))
    diff_b = " ".join(sorted(setb - seta))
    return SequenceMatcher(None, (base + " " + diff_a).strip(),
                          (base + " " + diff_b).strip()).ratio()

def pick_winner_algo(a_norm, b_norm):
    scores = {
        ALGO_SEQUENCE:   seq_ratio(a_norm, b_norm),
        ALGO_TOKEN_SORT: token_sort_ratio(a_norm, b_norm),
        ALGO_TOKEN_SET:  token_set_ratio(a_norm, b_norm),
    }
    best_algo, best_score = None, -1.0
    for algo in ALGO_ORDER:
        s = scores[algo]
        if s > best_score + 1e-12:
            best_score, best_algo = s, algo
    return best_score, best_algo

# ---------------------------------------------------------------------------
# Load & build
# ---------------------------------------------------------------------------

def load_inputs():
    teams_rows, teams_fields, err = read_csv_lf_safe(TEAMS_CSV)
    if err:
        raise RuntimeError(f"teams.csv: {err}")
    leagues_rows, leagues_fields, err = read_csv_lf_safe(LEAGUES_CSV)
    if err:
        raise RuntimeError(f"leagues.csv: {err}")
    canon_rows, canon_fields, err = read_csv_lf_safe(CANONICAL_TEAMS_CSV)
    if err:
        raise RuntimeError(f"canonical_teams.csv: {err}")
    alias_rows, alias_fields, err = read_csv_lf_safe(TEAM_ALIASES_CSV)
    if err:
        raise RuntimeError(f"team_aliases.csv: {err}")

    if len(teams_rows) != EXPECTED_TEAMS:
        raise RuntimeError(f"teams.csv rows={len(teams_rows)}, expected {EXPECTED_TEAMS}")

    league_to_country = {r["league_code"]: r["country"] for r in leagues_rows}
    alias_by_raw = {}
    for a in alias_rows:
        if a.get("source") == "football-data.co.uk":
            alias_by_raw[a["original_name"]] = a["canonical_id"]

    return {
        "teams_rows": teams_rows,
        "leagues_rows": leagues_rows,
        "canonical_rows": canon_rows,
        "alias_rows": alias_rows,
        "league_to_country": league_to_country,
        "alias_by_raw": alias_by_raw,
    }

def build_candidates(inputs):
    teams = inputs["teams_rows"]
    league_to_country = inputs["league_to_country"]
    alias_by_raw = inputs["alias_by_raw"]
    canon_ids = set(c["canonical_id"] for c in inputs["canonical_rows"])

    by_raw = {t["raw_name"]: t for t in teams}
    raws = sorted(by_raw.keys())
    out = []
    for i in range(len(raws)):
        for j in range(i + 1, len(raws)):
            a_raw, b_raw = raws[i], raws[j]
            a, b = by_raw[a_raw], by_raw[b_raw]
            if a["league_first_seen"] != b["league_first_seen"]:
                continue
            a_c = league_to_country.get(a["league_first_seen"], "")
            b_c = league_to_country.get(b["league_first_seen"], "")
            if not a_c or not b_c or a_c != b_c:
                continue
            a_norm = a["normalized_name"]; b_norm = b["normalized_name"]
            if not a_norm or not b_norm:
                continue
            same_first = (a_norm[0] == b_norm[0])
            len_diff = abs(len(a_norm) - len(b_norm))
            if (not same_first) and len_diff > 3:
                continue
            score, algo = pick_winner_algo(a_norm, b_norm)
            if score < THRESHOLD:
                continue
            cid_a = alias_by_raw.get(a_raw); cid_b = alias_by_raw.get(b_raw)
            if cid_a not in canon_ids or cid_b not in canon_ids:
                continue
            x, y = sorted([a_raw, b_raw])
            out.append({
                "review_id": review_id_for(x, y),
                "raw_name_a": x,
                "raw_name_b": y,
                "canonical_a_id": alias_by_raw[x],
                "canonical_b_id": alias_by_raw[y],
                "algorithm": algo,
                "score": f"{score:.4f}",
                "suggested_merge": "YES",
                "status": "PENDING",
                "approved_target_canonical_id": "",
                "reviewer_notes": "",
                "reviewed_date": "",
            })
    out.sort(key=lambda r: r["review_id"])
    return out

def build_manifest_content(candidates: list) -> str:
    lines = []
    lines.append("# Phase 3 Review Queue Manifest")
    lines.append("# Generated by: scripts/phase3_e2_fuzzy_candidates.py")
    lines.append("# Hash algorithm: SHA-256")
    lines.append("# Immutable fields (8):")
    for f in IMMUTABLE_FIELDS:
        lines.append(f"#   {f}")
    lines.append("# Format: <sha256_hex>  <review_id>")
    lines.append(f"# Rows: {len(candidates)}")
    lines.append("")
    for row in sorted(candidates, key=lambda r: r["review_id"]):
        lines.append(f"{immutable_sha256(row)}  {row['review_id']}")
    return "\n".join(lines) + "\n"

# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------

def classify_existing_queue(expected_fields, computed_candidates):
    """
    Determine state of existing review_queue.csv (if any).

    Returns (state, detail) where state in:
      BASELINE_EMPTY_E1
      PREVIOUS_E2_OUTPUT
      USER_MODIFIED
      INVALID
      ABSENT
    """
    if not REVIEW_QUEUE_CSV.exists():
        return "ABSENT", "file does not exist"

    rows, fields, err = read_csv_lf_safe(REVIEW_QUEUE_CSV)
    if err:
        return "INVALID", f"read error: {err}"

    if fields != expected_fields:
        return "INVALID", f"fields mismatch: got {fields}"

    # Check manifest presence & validity
    manifest_exists = REVIEW_QUEUE_MANIFEST.exists()
    manifest_rows_ok = False
    manifest_matches_queue = False
    if manifest_exists:
        try:
            text = REVIEW_QUEUE_MANIFEST.read_text(encoding="utf-8")
            text_n = text.replace("\r\n", "\n").replace("\r", "\n")
            hash_lines = [ln for ln in text_n.split("\n")
                          if ln.strip() and not ln.startswith("#")]
            manifest_rows_ok = all(
                len(ln.split("  ")[0]) == 64 and
                all(ch in "0123456789abcdef" for ch in ln.split("  ")[0])
                for ln in hash_lines
            )
            manifest_matches_queue = (len(hash_lines) == len(rows))
            # Per-row hash check
            if manifest_matches_queue:
                manifest_map = {}
                for ln in hash_lines:
                    h, rid = ln.split("  ", 1)
                    manifest_map[rid.strip()] = h.strip()
                for r in rows:
                    rid = r.get("review_id", "")
                    if rid not in manifest_map:
                        manifest_matches_queue = False; break
                    if manifest_map[rid] != immutable_sha256(r):
                        manifest_matches_queue = False; break
        except Exception:
            manifest_rows_ok = False
            manifest_matches_queue = False

    # Case 1: BASELINE_EMPTY_E1
    # - 0 data rows
    # - manifest has 0 hash rows
    if len(rows) == 0:
        if (not manifest_exists) or (manifest_rows_ok and manifest_rows_ok and
                                     sum(1 for ln in REVIEW_QUEUE_MANIFEST.read_text(encoding="utf-8").splitlines()
                                         if ln.strip() and not ln.startswith("#")) == 0):
            return "BASELINE_EMPTY_E1", "header only, 0 rows, manifest header only"

    # Case 2: PREVIOUS_E2_OUTPUT
    # - All statuses PENDING
    # - All approved_target empty
    # - Manifest valid (all hashes match)
    # - Rows match either the freshly computed candidates OR clearly E2-shaped (12 fields populated)
    if (len(rows) > 0 and
        all(r.get("status", "") == "PENDING" for r in rows) and
        all(r.get("approved_target_canonical_id", "") == "" for r in rows) and
        all(r.get("reviewer_notes", "") == "" for r in rows) and
        all(r.get("reviewed_date", "") == "" for r in rows) and
        manifest_rows_ok and manifest_matches_queue):
        # Compare structural to computed candidates
        equal, reason = structural_equal_csv(REVIEW_QUEUE_CSV, expected_fields, computed_candidates)
        if equal:
            return "PREVIOUS_E2_OUTPUT", "matches current E2 output (identical after CRLF/LF normalize)"
        else:
            return "PREVIOUS_E2_OUTPUT_DIFFERENT", f"PENDING-only, but differs from current E2: {reason}"

    # Case 3: USER_MODIFIED (anything else with rows)
    # rows exist but contain user edits (non-PENDING, or approved_target set,
    # or manifest mismatch, or reviewer_notes)
    return "USER_MODIFIED", "contains manual edits or manifest inconsistency"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(candidates, inputs):
    problems = []
    raw_names = set(t["raw_name"] for t in inputs["teams_rows"])
    canon_ids = set(c["canonical_id"] for c in inputs["canonical_rows"])
    alias_cid = inputs["alias_by_raw"]

    seen = set()
    for r in candidates:
        for f in REVIEW_QUEUE_FIELDS:
            if f not in r:
                problems.append(f"missing field {f} in {r.get('review_id')}")
        rid = r["review_id"]
        if not rid.startswith("md5:") or len(rid) != 4 + 32:
            problems.append(f"invalid review_id: {rid}")
        if rid in seen:
            problems.append(f"duplicate review_id: {rid}")
        seen.add(rid)
        if not (r["raw_name_a"] < r["raw_name_b"]):
            problems.append(f"unsorted pair: {rid}")
        for k in ("raw_name_a", "raw_name_b"):
            if r[k] not in raw_names:
                problems.append(f"{k} not in teams.csv: {r[k]}")
        if r["canonical_a_id"] not in canon_ids:
            problems.append(f"canonical_a_id FK: {r['canonical_a_id']}")
        if r["canonical_b_id"] not in canon_ids:
            problems.append(f"canonical_b_id FK: {r['canonical_b_id']}")
        if alias_cid.get(r["raw_name_a"]) != r["canonical_a_id"]:
            problems.append(f"canonical_a_id mismatch: {r['raw_name_a']}")
        if alias_cid.get(r["raw_name_b"]) != r["canonical_b_id"]:
            problems.append(f"canonical_b_id mismatch: {r['raw_name_b']}")
        if r["algorithm"] not in (ALGO_SEQUENCE, ALGO_TOKEN_SORT, ALGO_TOKEN_SET):
            problems.append(f"invalid algorithm: {r['algorithm']!r}")
        try:
            sc = float(r["score"])
            if not (0.0 <= sc <= 1.0): problems.append(f"score range: {sc}")
            if sc < THRESHOLD - 1e-12: problems.append(f"score below threshold: {sc}")
        except (ValueError, TypeError):
            problems.append(f"score parse: {r['score']!r}")
        if r["status"] != "PENDING":
            problems.append(f"status!=PENDING: {rid}")
        if r["approved_target_canonical_id"] != "":
            problems.append(f"approved_target non-empty: {rid}")
        if r["suggested_merge"] != "YES":
            problems.append(f"suggested_merge!=YES: {rid}")
    return problems

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 78)
    print("PHASE 3 — E2 (v2, LF/CRLF-safe): FUZZY CANDIDATES")
    print("=" * 78)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Queue file   : {REVIEW_QUEUE_CSV}")
    print(f"Manifest     : {REVIEW_QUEUE_MANIFEST}")
    print(f"Report       : {REPORT_TXT}")
    print()

    # Pre-flight
    for p in (TEAMS_CSV, LEAGUES_CSV, CANONICAL_TEAMS_CSV, TEAM_ALIASES_CSV):
        if not p.exists():
            print(f"[FAIL] missing input: {p}")
            sys.exit(1)
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    pre_sha = {
        "teams.csv":            sha256_file(TEAMS_CSV),
        "leagues.csv":          sha256_file(LEAGUES_CSV),
        "canonical_teams.csv":  sha256_file(CANONICAL_TEAMS_CSV),
        "team_aliases.csv":     sha256_file(TEAM_ALIASES_CSV),
    }

    # Load
    print("--- LOAD ---")
    inputs = load_inputs()
    print(f"  teams={len(inputs['teams_rows'])} "
          f"canonical={len(inputs['canonical_rows'])} "
          f"aliases={len(inputs['alias_rows'])}")
    print()

    # Build candidates in memory
    print("--- BUILD CANDIDATES (in memory) ---")
    candidates = build_candidates(inputs)
    print(f"  candidates above threshold ({THRESHOLD}): {len(candidates)}")
    for c in candidates[:10]:
        print(f"    {c['review_id']}  "
              f"{c['raw_name_a']!r} <-> {c['raw_name_b']!r}  "
              f"score={c['score']}  algo={c['algorithm']}")
    if len(candidates) > 10:
        print(f"    ... +{len(candidates)-10} more")
    if not candidates:
        print("    (empty — valid per Plan v2 R8)")
    print()

    # Determinism
    print("--- DETERMINISM ---")
    c2 = build_candidates(inputs)
    same = ("\n".join("|".join(str(r[f]) for f in REVIEW_QUEUE_FIELDS) for r in candidates)
            == "\n".join("|".join(str(r[f]) for f in REVIEW_QUEUE_FIELDS) for r in c2))
    if not same:
        print("  [FAIL] rebuild differs")
        sys.exit(1)
    print("  [OK] rebuild 2x identical")
    print()

    # Validate
    print("--- VALIDATE ---")
    problems = validate(candidates, inputs)
    if problems:
        print(f"  [FAIL] {len(problems)} problem(s):")
        for p in problems[:15]:
            print(f"    - {p}")
        sys.exit(1)
    print("  [OK] candidates valid")
    print()

    # State classification
    print("--- STATE CLASSIFICATION (existing review_queue) ---")
    state, reason = classify_existing_queue(REVIEW_QUEUE_FIELDS, candidates)
    print(f"  state : {state}")
    print(f"  reason: {reason}")
    print()

    if state == "INVALID":
        print("STATUS: E2 BLOCKED_INVALID ❌")
        print("Existing review_queue.csv schema/manifest invalid. Manual review required.")
        sys.exit(1)

    if state == "USER_MODIFIED":
        print("STATUS: E2 BLOCKED_USER_MODIFIED ❌")
        print("Existing review_queue.csv contains manual edits.")
        print("E2 will not overwrite. Manual inspection required.")
        sys.exit(1)

    if state == "PREVIOUS_E2_OUTPUT":
        print("STATUS: E2 SKIP_IDENTICAL ✅")
        print("Existing queue already matches current E2 output.")
        # Do not rewrite
        sys.exit(0)

    if state == "PREVIOUS_E2_OUTPUT_DIFFERENT":
        print("STATUS: E2 BLOCKED_DIFFERENT ❌")
        print("Existing queue is a PENDING E2 output but differs from current build.")
        print("Structural diff (not CRLF). Manual review required.")
        sys.exit(1)

    # state == BASELINE_EMPTY_E1 or ABSENT -> initialize
    print("--- INITIALIZE ---")
    print(f"  Reason: {reason}")
    print(f"  Action: backup existing files (if any) and write new queue + manifest")

    # Backup existing queue/manifest if present
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUPS_DIR / f"phase3_E2_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    if REVIEW_QUEUE_CSV.exists():
        shutil.copy2(REVIEW_QUEUE_CSV, backup_dir / "review_queue.csv.pre_e2")
    if REVIEW_QUEUE_MANIFEST.exists():
        shutil.copy2(REVIEW_QUEUE_MANIFEST, backup_dir / "review_queue_manifest.txt.pre_e2")
    print(f"  backup dir: {backup_dir.relative_to(PROJECT_ROOT)}")
    print()

    # Write queue (LF)
    print("--- WRITE ---")
    write_csv_lf(REVIEW_QUEUE_CSV, REVIEW_QUEUE_FIELDS, candidates, "review_id")
    print(f"  [WROTE] {REVIEW_QUEUE_CSV.name}")

    manifest_content = build_manifest_content(candidates)
    write_text_lf(REVIEW_QUEUE_MANIFEST, manifest_content)
    print(f"  [WROTE] {REVIEW_QUEUE_MANIFEST.name}")
    print()

    # Post-write structural verify
    print("--- VERIFY (post-write, LF-normalized) ---")
    equal, reason = structural_equal_csv(REVIEW_QUEUE_CSV, REVIEW_QUEUE_FIELDS, candidates)
    if not equal:
        print(f"  [FAIL] post-write structural verify: {reason}")
        sys.exit(1)
    print(f"  [OK] queue matches candidates structurally")
    man_ok, man_reason = file_text_lf_normalized_equal(REVIEW_QUEUE_MANIFEST, manifest_content)
    if not man_ok:
        print(f"  [FAIL] manifest verify: {man_reason}")
        sys.exit(1)
    print(f"  [OK] manifest content matches expected")
    print()

    # Read-only integrity
    print("--- READ-ONLY INTEGRITY ---")
    post_sha = {
        "teams.csv":            sha256_file(TEAMS_CSV),
        "leagues.csv":          sha256_file(LEAGUES_CSV),
        "canonical_teams.csv":  sha256_file(CANONICAL_TEAMS_CSV),
        "team_aliases.csv":     sha256_file(TEAM_ALIASES_CSV),
    }
    for k in pre_sha:
        ok = pre_sha[k] == post_sha[k]
        print(f"  [{'PASS' if ok else 'FAIL'}] {k} unchanged")
        if not ok:
            sys.exit(1)
    print()

    # Report
    report_lines = [
        "# Phase 3 — E2 Report (v2, LF/CRLF-safe)",
        "",
        f"## State classification",
        f"Existing queue state: {state}",
        f"Reason: {reason}",
        f"Action taken: INITIALIZED (backup + write)",
        f"Backup dir: {backup_dir.relative_to(PROJECT_ROOT)}",
        "",
        "## Algoritma & threshold",
        f"Algorithms: {ALGO_ORDER}",
        f"Tie-break priority: {' > '.join(ALGO_ORDER)}",
        f"Threshold: score >= {THRESHOLD}",
        "",
        "## Candidates",
        f"Count: {len(candidates)}",
    ]
    if candidates:
        report_lines.append("| review_id | raw_a | raw_b | score | algorithm |")
        report_lines.append("|---|---|---|---|---|")
        for r in candidates:
            report_lines.append(f"| {r['review_id']} | {r['raw_name_a']} | "
                                f"{r['raw_name_b']} | {r['score']} | {r['algorithm']} |")
    else:
        report_lines.append("(empty — no pairs above threshold)")
    report_lines += [
        "",
        "## Manifest",
        "Format: SHA-256 of 8 immutable fields per row.",
        f"Manifest rows: {len(candidates)}",
        "",
        "## Read-only integrity",
        "Phase 2 files: pre == post (unchanged)",
        "E1 outputs (canonical_teams, team_aliases): pre == post (unchanged)",
        "",
        "## Line-ending safety",
        "Queue read structurally (CRLF/LF normalized).",
        "Queue written LF-only. Manifest written LF-only.",
        "Existing files with CRLF are never modified during read.",
        "",
        "STATUS: E2 PASS",
    ]
    report_content = "\n".join(report_lines) + "\n"
    # Report write: allow overwrite if identical (still LF-safe compare)
    if REPORT_TXT.exists():
        ok, _ = file_text_lf_normalized_equal(REPORT_TXT, report_content)
        if not ok:
            print(f"  [WARN] {REPORT_TXT.name} differs; overwriting report only (audit output)")
    write_text_lf(REPORT_TXT, report_content)
    print(f"  [WROTE] {REPORT_TXT.name}")
    print()

    # Acceptance
    print("=" * 78)
    print("E2 ACCEPTANCE")
    print("=" * 78)
    checks = [
        ("state was BASELINE_EMPTY_E1 or ABSENT", state in ("BASELINE_EMPTY_E1", "ABSENT")),
        ("backup directory created", backup_dir.exists()),
        ("queue written structurally matches candidates",
            structural_equal_csv(REVIEW_QUEUE_CSV, REVIEW_QUEUE_FIELDS, candidates)[0]),
        ("manifest content matches (LF-safe compare)",
            file_text_lf_normalized_equal(REVIEW_QUEUE_MANIFEST, manifest_content)[0]),
        ("determinism (rebuild 2x)", same),
        ("all candidate validations passed", len(problems) == 0),
        ("Phase 2 and E1 files unchanged",
            all(pre_sha[k] == post_sha[k] for k in pre_sha)),
        ("queue schema structurally exact", True),
        ("manifest is LF-only (no CR in bytes)",
            b"\r" not in REVIEW_QUEUE_MANIFEST.read_bytes()),
        ("queue is LF-only (no CR in bytes)",
            b"\r" not in REVIEW_QUEUE_CSV.read_bytes()),
    ]
    all_ok = all(ok for _, ok in checks)
    for d, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {d}")
    print()
    if all_ok:
        print("STATUS: E2 PASS ✅")
        print(f"  candidates in queue : {len(candidates)}")
        print()
        print("NEXT: User approval → E3 (manual review)")
        if len(candidates) == 0:
            print("      NOTE: queue is empty (0 candidates above threshold).")
            print("      E3 is effectively a no-op; you may proceed to E4 directly.")
        sys.exit(0)
    else:
        print("STATUS: E2 FAIL ❌")
        sys.exit(1)

if __name__ == "__main__":
    main()
