# NYC Housing Finder

A Streamlit web app that aggregates **NYC rent-stabilized buildings** and **active Housing Connect lottery listings** into a single searchable map and directory.

## Features

- **Interactive map** — PyDeck scatter-plot of stabilized buildings (blue) and open lotteries (gold) on a dark CARTO basemap
- **Housing Connect lotteries** — live listings pulled from NYC Open Data, filtered by borough, ZIP, max rent, and AMI-based income
- **Rent-stabilized directory** — 10 000 + buildings from MapPLUTO and the 421-a(16) dataset, searchable and paginatable
- **SQL inspector** — run ad-hoc DuckDB queries directly in the browser
- **Auto-refresh** — APScheduler syncs lottery data every 24 hours in the background

## Data Sources

| Dataset | NYC Open Data ID |
|---------|-----------------|
| MapPLUTO (stabilized buildings) | `64uk-42ks` |
| 421-a(16) tax-benefit buildings | `pq4c-wbq4` |
| Housing Connect lotteries | `vy5i-a666` |
| Housing Connect buildings | `nibs-na6y` |

Stabilized buildings: BldgClass C/D + `exempttot > 0`.  
Active lottery statuses: `"active"` and `"tenant selection"`.

## Stack

| Layer | Technology |
|-------|-----------|
| UI | Streamlit 1.x |
| Database | DuckDB (local file `housing.duckdb`) |
| Maps | PyDeck (CARTO dark-matter, no Mapbox key required) |
| Scraping | Playwright + BeautifulSoup4 (gracefully skipped in sandboxed envs) |
| Scheduling | APScheduler `BackgroundScheduler` |
| API wrapper | FastAPI (`backend/api.py`) |
| AMI reference | 2024 NYC AMI = $127,100; rent estimated at HUD 30%-of-income rule |

## Getting Started

### Requirements

```
python 3.11+
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Install Playwright browsers (optional — only needed for live scraping):

```bash
playwright install chromium
```

### Run

```bash
streamlit run app.py --server.port 5000 --server.address 0.0.0.0
```

On first boot the app seeds the database automatically from NYC Open Data (no manual step needed).

### Environment

No API keys or secrets are required. All data is fetched from public NYC Open Data endpoints.

## Project Layout

```
├── app.py                  # Streamlit UI (map, lotteries, directory, SQL inspector)
├── main.py                 # CLI entry-point
├── scheduler.py            # Standalone APScheduler runner
├── backend/
│   ├── api.py              # FastAPI wrapper (GET /search, GET /health)
│   └── search.py           # Core search logic (borough, ZIP, rent, AMI filters)
├── db/
│   └── schema.py           # DuckDB init, connection helpers, table-count utils
├── ingestion/
│   ├── nyc_opendata.py     # MapPLUTO + 421-a(16) ingest
│   └── scraper.py          # Housing Connect scraper (Open Data + Playwright fallback)
└── tests/
    ├── test_scraper_reconciliation.py
    └── test_search.py
```

## Tests

```bash
pytest tests/
```

18 search tests + 12 scraper-reconciliation tests, all using an in-memory DuckDB fixture.

## Notes

- `housing.duckdb` is excluded from version control (binary, auto-generated on first boot).
- Playwright's `libgbm.so.1` is unavailable in some sandboxed environments (e.g. Replit NixOS); the scraper catches the import error and falls back to the Open Data API path.
- DuckDB `ON CONFLICT` clauses use `now()` (not `CURRENT_TIMESTAMP`) due to a binder limitation in DuckDB's current release.

## License

MIT
