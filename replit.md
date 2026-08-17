# NYC Affordable Housing Finder

Aggregates, filters, and maps NYC rent-stabilized properties and active NYC Housing Connect lottery listings. Search by Borough, Zip Code, Rent Ceiling, and Household Income.

## Run & Operate

- `streamlit run app.py --server.port 5000 --server.address 0.0.0.0` — start the Streamlit app (managed by the "Streamlit App" workflow)
- `python3 -m ingestion.nyc_opendata` — run the NYC Open Data ingestion script directly (args: `[borough] [zip_code]`)
- `python3 -m db.schema` — initialize the database and print table counts

## Stack

- **Frontend / UI:** Streamlit 1.35+
- **Database:** DuckDB (local file `housing.duckdb`)
- **Data ingestion:** requests + NYC Open Data SODA API
- **Scraping:** Playwright (Chromium) + BeautifulSoup4
- **Mapping:** PyDeck (primary) / Folium (fallback)
- **Python:** 3.11

## Where things live

- `app.py` — Streamlit entry point (sidebar filters, tabs, map, tables)
- `db/schema.py` — DuckDB schema init; `init_db()`, `get_connection()`, `get_table_counts()`
- `ingestion/nyc_opendata.py` — HPD Multiple Dwelling Registrations ingestor (`ingest_stabilized_buildings()`)
- `ingestion/scraper.py` — Housing Connect Playwright scraper (`scrape_housing_connect()`)
- `backend/search.py` — Unified query + AMI-matching filter layer (`search()`)
- `.streamlit/config.toml` — Streamlit server config (port 5000, dark theme)
- `housing.duckdb` — local DuckDB data file (git-ignored)

## Architecture decisions

- **DuckDB over SQLite:** DuckDB handles analytical queries (range filters, joins, aggregates) much faster than SQLite at this data scale. No separate server process needed.
- **ON CONFLICT upsert:** Both tables use `building_id` / `listing_id` as primary keys and upsert on every ingestion run to prevent duplicates without full table clears.
- **GeoSearch fallback:** HPD records don't include lat/lon — `_geocode()` in `nyc_opendata.py` calls the NYC Planning GeoSearch API (`geosearch.planninglabs.nyc/v2/search`). Rate-limited to ~8 req/s.
- **Scraper stub for Task #1:** `ingestion/scraper.py` is a no-op stub during foundation setup; full Playwright implementation comes in Task #2.
- **`now()` not `CURRENT_TIMESTAMP`:** DuckDB's `ON CONFLICT DO UPDATE SET` clause does not accept `CURRENT_TIMESTAMP` as a function call — use `now()` instead.

## Product

Users enter their borough, zip code, rent ceiling, and annual income to find matching rent-stabilized addresses and active Housing Connect lottery listings on an interactive map, with sortable tables and direct application links.

## User preferences

- Streamlit for UI (not React/Next.js)
- Python backend — DuckDB for storage
- Modular code: ingestion, backend, and UI in separate files

## Gotchas

- **DuckDB upsert:** Use `now()` (not `CURRENT_TIMESTAMP`) in `ON CONFLICT DO UPDATE SET` clauses.
- **HPD field names:** The HPD registrations dataset (tesw-yqqr) uses `boro` (not `borough`) and uppercase values (e.g. `'BROOKLYN'`).
- **DOF endpoint 9bfa-xziz:** Does not exist on NYC Open Data — don't use it. HPD registrations is the primary rent-stabilized source.
- **Playwright Chromium:** Binaries downloaded to `.cache/ms-playwright/` — re-run `python3 -m playwright install chromium` if container is rebuilt.
- Always `python3 -m db.schema` or call `init_db()` before running ingestion on a fresh environment.
