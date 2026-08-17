"""
NYC Housing Aggregator — Streamlit Application

Features:
  - Auto-bootstrap: seeds DB on first boot if empty
  - Background scheduler: syncs Housing Connect every 24 h
  - Interactive map (PyDeck), lottery table, building directory
  - Database inspector with SQL runner

Run with:
    streamlit run app.py --server.port 5000 --server.address 0.0.0.0
"""

import streamlit as st

# ── Page config (must be first Streamlit call) ────────────────────────────
st.set_page_config(
    page_title="NYC Affordable Housing Finder",
    page_icon="🏙️",
    layout="wide",
    initial_sidebar_state="expanded",
)

import logging
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

logger = logging.getLogger(__name__)

from db.schema import init_db, get_table_counts, get_connection, DB_PATH, get_last_sync, record_ingestion

# ── Constants ─────────────────────────────────────────────────────────────
BOROUGHS = ["All", "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]
COLOR_BUILDINGS = [30, 144, 255, 180]
COLOR_LISTINGS  = [255, 99,  71,  210]

# ─────────────────────────────────────────────────────────────────────────
# STEP 1 — Auto bootstrap: init DB and seed if empty
# @st.cache_resource runs once per app lifecycle
# ─────────────────────────────────────────────────────────────────────────

@st.cache_resource
def _bootstrap_db():
    """Initialise tables and auto-seed if both tables are empty."""
    init_db()
    counts = get_table_counts()
    if counts["stabilized_buildings"] == 0 and counts["housing_connect_listings"] == 0:
        return "seed_needed"
    return "ready"


bootstrap_state = _bootstrap_db()

# ─────────────────────────────────────────────────────────────────────────
# STEP 2 — Empty-state detection: check live table counts
# ─────────────────────────────────────────────────────────────────────────
_counts_now = get_table_counts()
_db_is_empty = (
    _counts_now["stabilized_buildings"] == 0
    and _counts_now["housing_connect_listings"] == 0
)

# ─────────────────────────────────────────────────────────────────────────
# STEP 3 — Background scheduler: sync listings every 24 h
# ─────────────────────────────────────────────────────────────────────────

@st.cache_resource
def _start_scheduler():
    """Start APScheduler background job once per app lifecycle."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        def _sync_job():
            try:
                from ingestion.scraper import scrape_housing_connect
                n = scrape_housing_connect()
                logger.info("[scheduler] Synced %d listings", n)
            except Exception as exc:
                logger.warning("[scheduler] Sync failed: %s", exc)

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(_sync_job, "interval", hours=24, id="housing_sync",
                          misfire_grace_time=3600)
        scheduler.start()
        logger.info("[scheduler] Started — next run in 24 h")
        return scheduler
    except Exception as exc:
        logger.warning("[scheduler] Could not start: %s", exc)
        return None


_start_scheduler()


# ── Sidebar ───────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🏙️ NYC Housing Finder")
    st.caption("Rent-stabilized buildings & active Housing Connect lotteries")
    st.markdown("---")

    borough = st.selectbox("Borough", BOROUGHS, index=0)

    zip_code = st.text_input(
        "ZIP Code (optional)",
        placeholder="e.g. 10025",
        max_chars=5,
    )

    max_rent = st.slider(
        "Max Monthly Rent ($)",
        min_value=0,
        max_value=10_000,
        value=3_000,
        step=50,
        format="$%d",
    )

    annual_income = st.number_input(
        "Annual Household Income ($)",
        min_value=0,
        max_value=500_000,
        value=60_000,
        step=1_000,
    )

    st.markdown("---")
    _btn_label = (
        "🌱 Load Initial Data" if _db_is_empty else "🔄 Refresh Data / Sync Listings"
    )
    refresh = st.button(_btn_label, use_container_width=True)

    # ── Last synced timestamp ─────────────────────────────────────────────
    try:
        sync_info = get_last_sync()
        last_run = sync_info.get("last_run_at")
        if last_run is not None:
            import datetime
            # DuckDB returns a datetime; format it nicely
            if hasattr(last_run, "strftime"):
                ts_str = last_run.strftime("%-m/%-d/%Y %-I:%M %p")
            else:
                ts_str = str(last_run)[:16]
            st.caption(f"🕐 Last synced: {ts_str}")
        else:
            st.caption("🕐 Not yet synced — click Refresh or wait for the nightly job.")
    except Exception:
        pass

    st.markdown("---")
    st.markdown(
        "**Map legend**\n\n🔵 Rent-stabilized buildings  \n🔴 Active lottery listings"
    )
    st.markdown("---")
    st.caption(
        "Data: [NYC MapPLUTO](https://data.cityofnewyork.us/resource/64uk-42ks.json) · "
        "[Housing Connect](https://housingconnect.nyc.gov)"
    )

# ── Manual refresh ────────────────────────────────────────────────────────
if refresh:
    borough_filter = None if borough == "All" else borough
    zip_filter = zip_code.strip() or None

    with st.spinner("Syncing stabilized buildings from NYC Open Data…"):
        try:
            from ingestion.nyc_opendata import ingest_stabilized_buildings
            n = ingest_stabilized_buildings(
                borough=borough_filter,
                zip_code=zip_filter,
                max_records=50_000,
            )
            record_ingestion("stabilized_buildings", status="ok", rows_affected=n)
            st.sidebar.success(f"✅ Synced {n:,} stabilized buildings.")
        except Exception as exc:
            record_ingestion("stabilized_buildings", status="error", error_msg=str(exc))
            st.sidebar.warning(f"⚠️ Open Data sync issue: {exc}")

    with st.spinner("Syncing Housing Connect lottery listings…"):
        try:
            from ingestion.scraper import scrape_housing_connect
            m = scrape_housing_connect()
            record_ingestion("housing_connect", status="ok", rows_affected=m)
            st.sidebar.success(f"✅ Synced {m:,} Housing Connect listings.")
        except Exception as exc:
            record_ingestion("housing_connect", status="error", error_msg=str(exc))
            st.sidebar.warning(f"⚠️ Housing Connect scrape issue: {exc}")

    st.rerun()

# ── First-run onboarding card (shown when both tables are still empty) ────
if _db_is_empty:
    st.markdown(
        """
        <style>
        .onboarding-card {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 60%, #0f3460 100%);
            border: 1px solid #e94560;
            border-radius: 16px;
            padding: 3rem 2.5rem;
            text-align: center;
            max-width: 680px;
            margin: 4rem auto;
        }
        .onboarding-icon { font-size: 4rem; margin-bottom: 1rem; }
        .onboarding-title {
            font-size: 2rem; font-weight: 700;
            color: #f5f5f5; margin-bottom: 0.75rem;
        }
        .onboarding-sub {
            font-size: 1.05rem; color: #a0a0b0; margin-bottom: 0.5rem; line-height: 1.6;
        }
        </style>
        <div class="onboarding-card">
          <div class="onboarding-icon">🏙️</div>
          <div class="onboarding-title">No data yet</div>
          <div class="onboarding-sub">
            The database is empty. Click <strong>🌱 Load Initial Data</strong> in the
            sidebar to fetch NYC rent-stabilized buildings and active Housing Connect
            lottery listings.<br><br>
            <em style="color:#7a7a9a">First-time load takes roughly 60 seconds.</em>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.stop()

# ── Load & filter data ────────────────────────────────────────────────────
try:
    from backend.search import search
    borough_filter = None if borough == "All" else borough
    zip_filter = zip_code.strip() or None
    buildings_df, listings_df = search(
        borough=borough_filter,
        zip_code=zip_filter,
        max_rent=float(max_rent),
        annual_income=float(annual_income),
    )
except Exception as exc:
    st.warning(f"Could not load search results: {exc}")
    buildings_df = pd.DataFrame()
    listings_df  = pd.DataFrame()

# ── Summary metrics ───────────────────────────────────────────────────────
m1, m2 = st.columns(2)
m1.metric(
    "🏢 Stabilized Buildings Found",
    f"{len(buildings_df):,}",
    help="Rent-stabilized / tax-exempt multifamily buildings matching your filters.",
)
m2.metric(
    "🎟️ Active Lotteries (Income Match)",
    f"{len(listings_df):,}",
    help="Open Housing Connect lotteries where your income falls within the AMI range.",
)

st.markdown("---")

# ── Tabs ──────────────────────────────────────────────────────────────────
tab_map, tab_lotteries, tab_directory, tab_inspector = st.tabs([
    "🗺️ Interactive Map",
    "🎟️ Active Lotteries",
    "🏢 Building Directory",
    "📊 Database Inspector",
])

# ─────────────────────────────────────────────────────────────────────────
# TAB 1 — Interactive Map
# ─────────────────────────────────────────────────────────────────────────
with tab_map:
    map_buildings = pd.DataFrame()
    map_listings  = pd.DataFrame()

    if not buildings_df.empty and {"latitude", "longitude"}.issubset(buildings_df.columns):
        map_buildings = buildings_df.dropna(subset=["latitude", "longitude"]).copy()

    if not listings_df.empty and {"latitude", "longitude"}.issubset(listings_df.columns):
        map_listings = listings_df.dropna(subset=["latitude", "longitude"]).copy()

    if map_buildings.empty and map_listings.empty:
        st.info("No map data available. Click **Refresh Data** to load buildings and listings.")
    else:
        try:
            import pydeck as pdk

            layers = []

            if not map_buildings.empty:
                mb = map_buildings.copy()
                if "street_address" not in mb.columns:
                    mb["street_address"] = ""
                if "tax_benefit_program" not in mb.columns:
                    mb["tax_benefit_program"] = ""
                mb["type"]    = "Rent-Stabilized Building"
                mb["details"] = mb["tax_benefit_program"]
                mb["address"] = (
                    mb["street_address"] + ", "
                    + mb.get("borough", pd.Series("", index=mb.index)).fillna("") + " "
                    + mb.get("zip_code", pd.Series("", index=mb.index)).fillna("")
                ).str.strip(", ")
                layers.append(pdk.Layer(
                    "ScatterplotLayer",
                    data=mb[["latitude", "longitude", "type", "details", "address"]].to_dict(orient="records"),
                    get_position=["longitude", "latitude"],
                    get_fill_color=COLOR_BUILDINGS,
                    get_radius=35,
                    pickable=True,
                    auto_highlight=True,
                ))

            if not map_listings.empty:
                ml = map_listings.copy()
                rent_range = ml.apply(
                    lambda r: (
                        f"${r['min_rent']:,.0f}–${r['max_rent']:,.0f}/mo"
                        if pd.notna(r.get("min_rent")) and pd.notna(r.get("max_rent"))
                        else "—"
                    ),
                    axis=1,
                )
                ml["type"]    = ml.get("title", pd.Series("Housing Connect Lottery", index=ml.index)).fillna("Housing Connect Lottery")
                ml["details"] = (
                    "Rent: " + rent_range
                    + " · AMI: " + ml.get("ami_percentage", pd.Series("—", index=ml.index)).fillna("—")
                    + " · Deadline: " + ml.get("deadline", pd.Series("—", index=ml.index)).fillna("—")
                )
                ml["address"] = ml.get("address", pd.Series("", index=ml.index)).fillna("")
                layers.append(pdk.Layer(
                    "ScatterplotLayer",
                    data=ml[["latitude", "longitude", "type", "details", "address"]].to_dict(orient="records"),
                    get_position=["longitude", "latitude"],
                    get_fill_color=COLOR_LISTINGS,
                    get_radius=55,
                    pickable=True,
                    auto_highlight=True,
                ))

            all_lats, all_lons = [], []
            for df in [map_buildings, map_listings]:
                if not df.empty:
                    all_lats += df["latitude"].dropna().tolist()
                    all_lons += df["longitude"].dropna().tolist()

            center_lat = sum(all_lats) / len(all_lats) if all_lats else 40.7128
            center_lon = sum(all_lons) / len(all_lons) if all_lons else -74.0060

            tooltip = {
                "html": "<b>{type}</b><br/>{details}<br/>Address: {address}",
                "style": {"backgroundColor": "steelblue", "color": "white"},
            }

            st.pydeck_chart(
                pdk.Deck(
                    layers=layers,
                    initial_view_state=pdk.ViewState(
                        latitude=center_lat, longitude=center_lon, zoom=11, pitch=0
                    ),
                    tooltip=tooltip,
                    map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
                ),
                use_container_width=True,
            )

            lc, rc = st.columns(2)
            lc.caption(f"🔵 {len(map_buildings):,} buildings with coordinates")
            rc.caption(f"🔴 {len(map_listings):,} lottery listings with coordinates")

        except Exception as exc:
            st.error(f"Map render error: {exc}")

# ─────────────────────────────────────────────────────────────────────────
# TAB 2 — Active Lotteries
# ─────────────────────────────────────────────────────────────────────────
with tab_lotteries:
    if listings_df.empty:
        st.info(
            "No active lottery listings match your filters. "
            "Try broadening your search or click **Refresh Data**."
        )
    else:
        disp = listings_df.copy()
        for col in ("min_income", "max_income"):
            if col in disp.columns:
                disp[col] = disp[col].apply(lambda v: f"${v:,.0f}" if pd.notna(v) else "—")
        for col in ("min_rent", "max_rent"):
            if col in disp.columns:
                disp[col] = disp[col].apply(lambda v: f"${v:,.0f}/mo" if pd.notna(v) else "—")

        rename_map = {
            "title": "Listing Name", "address": "Address", "borough": "Borough",
            "zip_code": "ZIP", "min_income": "Min Income", "max_income": "Max Income",
            "min_rent": "Min Rent", "max_rent": "Max Rent",
            "ami_percentage": "AMI Tier", "deadline": "Application Deadline", "url": "Apply",
        }
        display_cols = [c for c in rename_map if c in disp.columns]
        disp = disp[display_cols].rename(columns=rename_map)

        col_config = {}
        if "Apply" in disp.columns:
            col_config["Apply"] = st.column_config.LinkColumn(
                "Apply", display_text="Apply →",
                help="Open the Housing Connect application page",
            )

        st.dataframe(disp, column_config=col_config, use_container_width=True, hide_index=True)
        st.caption(f"Showing {len(listings_df):,} matching active lottery listings")

# ─────────────────────────────────────────────────────────────────────────
# TAB 3 — Stabilized Building Directory
# ─────────────────────────────────────────────────────────────────────────
with tab_directory:
    if buildings_df.empty:
        st.info("No stabilized buildings found. Click **Refresh Data** to ingest from NYC Open Data.")
    else:
        search_term = st.text_input(
            "Search buildings",
            placeholder="Filter by address, ZIP code, or tax program…",
        )

        display_df = buildings_df.copy()
        if search_term:
            term = search_term.strip()
            mask = pd.Series(False, index=display_df.index)
            for col in ["street_address", "zip_code", "borough", "tax_benefit_program"]:
                if col in display_df.columns:
                    mask |= display_df[col].str.contains(term, case=False, na=False)
            display_df = display_df[mask]

        rename_map = {
            "street_address": "Address", "borough": "Borough",
            "zip_code": "ZIP", "bbl": "BBL", "tax_benefit_program": "Tax Program",
        }
        display_cols = [c for c in rename_map if c in display_df.columns]
        st.dataframe(
            display_df[display_cols].rename(columns=rename_map),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(f"Showing {len(display_df):,} of {len(buildings_df):,} buildings")

# ─────────────────────────────────────────────────────────────────────────
# TAB 4 — Database Inspector
# ─────────────────────────────────────────────────────────────────────────
with tab_inspector:
    st.subheader("📊 Database Inspector")

    # Live record counts
    try:
        counts = get_table_counts()
        c1, c2 = st.columns(2)
        c1.metric("🏢 Total Stabilized Buildings", f"{counts['stabilized_buildings']:,}")
        c2.metric("🎟️ Total Housing Connect Listings", f"{counts['housing_connect_listings']:,}")
    except Exception as exc:
        st.warning(f"Could not read table counts: {exc}")

    st.markdown("---")
    st.subheader("🔍 SQL Query Runner")
    st.caption("Read-only SELECT queries only. Results limited to 500 rows.")

    # Example query shortcuts
    EXAMPLE_QUERIES = {
        "Sample Buildings":      "SELECT * FROM stabilized_buildings LIMIT 10",
        "Sample Lotteries":      "SELECT * FROM housing_connect_listings LIMIT 10",
        "Count by Borough":      (
            "SELECT borough, COUNT(*) AS buildings\n"
            "FROM stabilized_buildings\n"
            "GROUP BY borough\n"
            "ORDER BY buildings DESC"
        ),
        "Lotteries by Borough":  (
            "SELECT borough, COUNT(*) AS lotteries\n"
            "FROM housing_connect_listings\n"
            "GROUP BY borough\n"
            "ORDER BY lotteries DESC"
        ),
        "Recent Listings":       (
            "SELECT title, borough, zip_code, min_rent, max_rent, deadline\n"
            "FROM housing_connect_listings\n"
            "ORDER BY scraped_at DESC\n"
            "LIMIT 20"
        ),
    }

    btn_cols = st.columns(len(EXAMPLE_QUERIES))
    selected_example = st.session_state.get("_sql_example", "")

    for i, (label, query) in enumerate(EXAMPLE_QUERIES.items()):
        if btn_cols[i].button(label, key=f"ex_{i}", use_container_width=True):
            st.session_state["_sql_example"] = query
            st.rerun()

    default_sql = st.session_state.get("_sql_example", "SELECT * FROM stabilized_buildings LIMIT 10")

    sql_query = st.text_area(
        "SQL Query",
        value=default_sql,
        height=120,
        placeholder="SELECT * FROM stabilized_buildings LIMIT 10",
    )

    run_sql = st.button("▶ Run Query", type="primary")

    if run_sql:
        stripped = sql_query.strip().upper()
        if not stripped.startswith("SELECT"):
            st.error("Only SELECT statements are permitted.")
        else:
            try:
                conn = get_connection(DB_PATH)
                result_df = conn.execute(sql_query).fetchdf().head(500)
                conn.close()
                st.success(f"Query returned {len(result_df):,} row(s).")
                st.dataframe(result_df, use_container_width=True, hide_index=True)
            except Exception as exc:
                st.error(f"Query error: {exc}")
