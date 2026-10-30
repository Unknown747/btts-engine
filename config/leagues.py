"""
Definisi liga football-data.co.uk
HANYA berisi metadata. Tidak ada data pertandingan di sini.
"""

# Enam liga utama (prioritas training)
PRIMARY_LEAGUES = {
    "E0":  {"name": "Premier League",   "country": "England", "tier": 1},
    "D1":  {"name": "Bundesliga",       "country": "Germany", "tier": 1},
    "SP1": {"name": "La Liga",          "country": "Spain",   "tier": 1},
    "I1":  {"name": "Serie A",          "country": "Italy",   "tier": 1},
    "F1":  {"name": "Ligue 1",          "country": "France",  "tier": 1},
    "N1":  {"name": "Eredivisie",       "country": "Netherlands", "tier": 1},
}

# Liga tambahan (aktifkan setelah validasi)
EXTRA_LEAGUES = {
    "E1":  {"name": "Championship",           "country": "England"},
    "E2":  {"name": "League One",             "country": "England"},
    "E3":  {"name": "League Two",             "country": "England"},
    "D2":  {"name": "2. Bundesliga",          "country": "Germany"},
    "SP2": {"name": "Segunda Division",       "country": "Spain"},
    "I2":  {"name": "Serie B",                "country": "Italy"},
    "F2":  {"name": "Ligue 2",                "country": "France"},
    "SC0": {"name": "Scottish Premiership",   "country": "Scotland"},
    "SC1": {"name": "Scottish Championship",  "country": "Scotland"},
    "B1":  {"name": "Belgium Pro League",     "country": "Belgium"},
    "P1":  {"name": "Portugal Primeira Liga", "country": "Portugal"},
    "T1":  {"name": "Turkey Super Lig",       "country": "Turkey"},
    "G1":  {"name": "Greece Super League",    "country": "Greece"},
}

ALL_LEAGUES = {**PRIMARY_LEAGUES, **EXTRA_LEAGUES}

# Musim yang didukung football-data.co.uk (format: YYYY untuk season start year)
# Contoh: 2023 berarti season 2023/2024
AVAILABLE_SEASONS = list(range(2010, 2026))

# Base URL football-data.co.uk
BASE_URL = "https://www.football-data.co.uk/mmz4281"

# Nama file CSV format: {season}/{league}.csv
# Contoh: 2324/E0.csv = season 2023/2024 Premier League

def season_code(start_year: int) -> str:
    """Konversi 2023 -> '2324' (format football-data.co.uk)"""
    if start_year < 2000 or start_year > 2099:
        raise ValueError(f"Invalid season start year: {start_year}")
    end_year = start_year + 1
    return f"{str(start_year)[-2:]}{str(end_year)[-2:]}"

def download_url(league_code: str, start_year: int) -> str:
    """Generate URL CSV untuk liga + musim."""
    sc = season_code(start_year)
    return f"{BASE_URL}/{sc}/{league_code}.csv"
