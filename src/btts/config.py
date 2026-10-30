"""Path & konstanta global. Deterministic, tanpa side effect."""
from pathlib import Path

# Root project = 2 level di atas file ini (src/btts/config.py)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR        = PROJECT_ROOT / "data"
RAW_DIR         = DATA_DIR / "raw"
PROCESSED_DIR   = DATA_DIR / "processed"
MAPPINGS_DIR    = DATA_DIR / "mappings"
FEATURES_DIR    = DATA_DIR / "features"
PREDICTIONS_DIR = DATA_DIR / "predictions"
BACKTEST_DIR    = DATA_DIR / "backtest"
REPORTS_DIR     = DATA_DIR / "reports"

# CSV schema filenames (Phase 2+)
MATCHES_CSV         = PROCESSED_DIR / "matches.csv"
TEAMS_CSV           = PROCESSED_DIR / "teams.csv"
TEAM_ALIASES_CSV    = MAPPINGS_DIR  / "team_aliases.csv"
LEAGUES_CSV         = PROCESSED_DIR / "leagues.csv"
TEAM_STATS_CSV      = PROCESSED_DIR / "team_statistics.csv"
BTTS_STATS_CSV      = PROCESSED_DIR / "btts_statistics.csv"
H2H_STATS_CSV       = PROCESSED_DIR / "h2h_statistics.csv"
MATCH_FEATURES_CSV  = FEATURES_DIR  / "match_features.csv"
PREDICTIONS_CSV     = PREDICTIONS_DIR / "predictions.csv"
BACKTEST_RESULTS_CSV = BACKTEST_DIR / "backtest_results.csv"

# Ensure dirs exist (safe, idempotent)
for _d in (RAW_DIR, PROCESSED_DIR, MAPPINGS_DIR, FEATURES_DIR,
           PREDICTIONS_DIR, BACKTEST_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
