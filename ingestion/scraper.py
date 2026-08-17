"""
Housing Connect scraper module.

Three-layer strategy:
  1. NYC Open Data API (primary)  — datasets vy5i-a666 (by lottery) and nibs-na6y (by building)
  2. Playwright (secondary)       — live SPA scrape; catches libgbm / launch failures gracefully
  3. BeautifulSoup (tertiary)     — requests + BS4 parse; also queries HC listing detail API

Any layer that fails logs and returns; the caller always gets a total count of rows saved.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import duckdb
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NYC_OPEN_DATA_BASE = "https://data.cityofnewyork.us/resource"
_HC_BASE = "https://housingconnect.nyc.gov"
_LISTING_URL = f"{_HC_BASE}/PublicWeb/listings/{{lottery_id}}"

# Exact lottery_status values (lowercased) that represent active lotteries.
# Uses equality, NOT substring matching, so "Inactive" cannot match "active"
# and "Not Open" cannot match "open".
# Verified values from vy5i-a666: 'Active', 'Tenant Selection', 'Closed', 'All Units Filled'.
_ACTIVE_STATUSES = {"active", "tenant selection"}

# ---------------------------------------------------------------------------
# NYC 2024 AMI definitions (HPD categorical tiers → % AMI bands → dollar ceiling)
# Tier names match the actual Open Data field suffix (applied_income_ami_<tier>).
#
# Each tier maps to its % AMI ceiling; income range for a listing is
#   min_income = 0 (or lowest tier floor), max_income = highest tier ceiling.
# Dollar values are 100 % AMI for a family of 4 in NYC 2024 ($127,100).
# ---------------------------------------------------------------------------

_AMI_TIER_ORDER = ["ext_low", "very_low", "low", "moderate", "middle"]

_AMI_TIER_BAND: dict[str, tuple[int, int]] = {
    #             (floor %, ceil %)
    "ext_low":   (0,   30),
    "very_low":  (0,   50),
    "low":       (0,   80),
    "moderate":  (0,  120),
    "middle":    (0,  165),
}

_AMI_TIER_LABEL: dict[str, str] = {
    "ext_low":  "Extremely Low (≤30% AMI)",
    "very_low": "Very Low (≤50% AMI)",
    "low":      "Low (≤80% AMI)",
    "moderate": "Moderate (≤120% AMI)",
    "middle":   "Middle (≤165% AMI)",
}

# 100 % AMI for a family of 4, NYC 2024 (HUD)
_AMI_100_PCT = 127_100.0

_BOROUGH_CODE_MAP: dict[str, str] = {
    "BK": "Brooklyn",
    "BX": "Bronx",
    "MN": "Manhattan",
    "QN": "Queens",
    "SI": "Staten Island",
    # spelled-out variants already match; include lowercase:
    "brooklyn": "Brooklyn",
    "bronx": "Bronx",
    "manhattan": "Manhattan",
    "queens": "Queens",
    "staten island": "Staten Island",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_borough(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _BOROUGH_CODE_MAP.get(str(raw).strip(), None) or _BOROUGH_CODE_MAP.get(str(raw).strip().lower(), None)


def _borough_from_zip(zip_code: str) -> Optional[str]:
    """Best-effort borough from 5-digit ZIP."""
    if not zip_code:
        return None
    z = str(zip_code).strip().zfill(5)
    try:
        zi = int(z)
    except ValueError:
        return None
    if 10451 <= zi <= 10475:
        return "Bronx"
    if 11201 <= zi <= 11256:
        return "Brooklyn"
    if 11004 <= zi <= 11109 or 11351 <= zi <= 11697:
        return "Queens"
    if 10301 <= zi <= 10314:
        return "Staten Island"
    if (10001 <= zi <= 10282) or (10300 <= zi <= 10308):
        return "Manhattan"
    return None


def _extract_ami_tiers(row: dict) -> list[str]:
    """Return list of present AMI tier names from an Open Data row."""
    tiers = []
    for tier in _AMI_TIER_ORDER:
        field = f"applied_income_ami_{tier}"
        val = row.get(field)
        # Field present and non-zero means units at this tier exist
        if val not in (None, "", "0", 0):
            try:
                if int(val) > 0:
                    tiers.append(tier)
            except (TypeError, ValueError):
                pass
    return tiers


def _ami_label(tiers: list[str]) -> Optional[str]:
    if not tiers:
        return None
    if len(tiers) == 1:
        return _AMI_TIER_LABEL[tiers[0]]
    labels = [_AMI_TIER_LABEL[t] for t in tiers]
    return " / ".join(labels)


def _ami_income_range(tiers: list[str]) -> tuple[Optional[float], Optional[float]]:
    """
    Derive (min_income, max_income) for a listing from its AMI tiers.

    Convention: the floor of the lowest tier is the minimum qualifying income
    (0 = no minimum floor) and the ceiling of the highest tier is the maximum
    qualifying income. Dollar values scale linearly from _AMI_100_PCT.
    """
    if not tiers:
        return None, None
    floor_pct = min(_AMI_TIER_BAND[t][0] for t in tiers)
    ceil_pct  = max(_AMI_TIER_BAND[t][1] for t in tiers)
    # min_income = 0 means "no minimum income required"
    min_income = round(_AMI_100_PCT * floor_pct / 100)  # 0.0 when floor_pct == 0
    max_income = round(_AMI_100_PCT * ceil_pct  / 100)
    return float(min_income), float(max_income)


def _ami_rent_range(tiers: list[str]) -> tuple[Optional[float], Optional[float]]:
    """
    Estimate (min_rent, max_rent) from AMI tiers using the HUD 30%-of-income rule.

    NYC affordable housing sets rents at ≤30% of a household's gross income at the
    applicable AMI ceiling.  Formula:
        max_rent = (AMI_pct / 100) × AMI_100_PCT × 0.30 / 12

    This is the HPD/HUD regulatory ceiling — a standard calculation, not a guess.
    min_rent uses the floor tier (0 → $0).
    """
    if not tiers:
        return None, None
    floor_pct = min(_AMI_TIER_BAND[t][0] for t in tiers)
    ceil_pct  = max(_AMI_TIER_BAND[t][1] for t in tiers)
    min_rent = round(_AMI_100_PCT * floor_pct / 100 * 0.30 / 12)
    max_rent = round(_AMI_100_PCT * ceil_pct  / 100 * 0.30 / 12)
    return float(min_rent), float(max_rent)


def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _fetch_opendata(dataset_id: str, params: dict) -> list[dict]:
    url = f"{_NYC_OPEN_DATA_BASE}/{dataset_id}.json"
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _upsert_listing(conn: duckdb.DuckDBPyConnection, listing: dict) -> None:
    conn.execute("""
        INSERT INTO housing_connect_listings
            (listing_id, title, address, borough, zip_code,
             min_income, max_income, min_rent, max_rent,
             ami_percentage, deadline, url, latitude, longitude, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (listing_id) DO UPDATE SET
            min_income    = COALESCE(EXCLUDED.min_income,    housing_connect_listings.min_income),
            max_income    = COALESCE(EXCLUDED.max_income,    housing_connect_listings.max_income),
            min_rent      = COALESCE(EXCLUDED.min_rent,      housing_connect_listings.min_rent),
            max_rent      = COALESCE(EXCLUDED.max_rent,      housing_connect_listings.max_rent),
            ami_percentage= COALESCE(EXCLUDED.ami_percentage,housing_connect_listings.ami_percentage),
            address       = COALESCE(EXCLUDED.address,       housing_connect_listings.address),
            borough       = COALESCE(EXCLUDED.borough,       housing_connect_listings.borough),
            zip_code      = COALESCE(EXCLUDED.zip_code,      housing_connect_listings.zip_code),
            deadline      = COALESCE(EXCLUDED.deadline,      housing_connect_listings.deadline),
            latitude      = COALESCE(EXCLUDED.latitude,      housing_connect_listings.latitude),
            longitude     = COALESCE(EXCLUDED.longitude,     housing_connect_listings.longitude),
            scraped_at    = now()
    """, [
        listing["listing_id"],
        listing.get("title"),
        listing.get("address"),
        listing.get("borough"),
        listing.get("zip_code"),
        listing.get("min_income"),
        listing.get("max_income"),
        listing.get("min_rent"),
        listing.get("max_rent"),
        listing.get("ami_percentage"),
        listing.get("deadline"),
        listing.get("url"),
        listing.get("latitude"),
        listing.get("longitude"),
    ])
    conn.commit()


# ---------------------------------------------------------------------------
# Layer 1 – NYC Open Data (primary)
# ---------------------------------------------------------------------------

def _paginate_opendata(dataset_id: str, order_field: str = ":id") -> tuple[list[dict], bool]:
    """
    Paginate through a Socrata dataset and return (rows, completed).

    ``completed`` is True only when pagination finished without a network/HTTP
    error on any page.  A False value means the returned list is a *partial*
    snapshot and callers MUST NOT use it to drive destructive operations such
    as stale-row deletion.
    """
    rows: list[dict] = []
    limit = 1000
    offset = 0
    completed = True
    while True:
        try:
            batch = _fetch_opendata(dataset_id, {"$limit": limit, "$offset": offset, "$order": order_field})
        except Exception as exc:
            logger.warning("[opendata] %s fetch error at offset %d: %s", dataset_id, offset, exc)
            completed = False  # partial fetch — mark as incomplete
            break
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
        time.sleep(0.15)
    return rows, completed


def _scrape_opendata(conn: duckdb.DuckDBPyConnection) -> int:
    """
    Pull Housing Connect listings from two NYC Open Data datasets:
      vy5i-a666 — by-lottery: AMI tiers, borough, postcode, lat/lon, status, dates
      nibs-na6y — by-building: house_number, street_name, address_zipcode, address_lat/lon

    Active statuses: "Active", "Tenant Selection".

    Stale-row deletion only runs when BOTH datasets were fetched without errors
    AND every active row was upserted without a DB failure.  Any partial fetch
    or upsert failure sets a reconciliation-abort flag so that cached rows are
    preserved rather than deleted.

    Returns the number of rows upserted.
    """
    logger.info("[opendata] Fetching vy5i-a666 (by-lottery)…")
    by_lottery, lottery_complete = _paginate_opendata("vy5i-a666", "lottery_id")
    logger.info("[opendata] Fetched %d rows from vy5i-a666 (complete=%s)", len(by_lottery), lottery_complete)

    logger.info("[opendata] Fetching nibs-na6y (by-building)…")
    by_building, building_complete = _paginate_opendata("nibs-na6y", "lottery_id")
    logger.info("[opendata] Fetched %d rows from nibs-na6y (complete=%s)", len(by_building), building_complete)

    # Only safe to reconcile if both fetches completed without errors
    fetch_complete = lottery_complete and building_complete

    # Build lookup: lottery_id → list of building rows (one lottery may span buildings)
    building_lookup: dict[str, dict] = {}
    for brow in by_building:
        lid = str(brow.get("lottery_id", "")).strip()
        if lid and lid not in building_lookup:
            building_lookup[lid] = brow  # first building suffices for address

    saved = 0
    upsert_failed = False   # any upsert failure aborts reconciliation
    active_ids: set[str] = set()
    for row in by_lottery:
        lottery_id = str(row.get("lottery_id", "")).strip()
        if not lottery_id:
            continue

        # Exact equality against the allow-list — substring matching would let
        # "Inactive" pass through as a match for "active".
        status_raw = str(row.get("lottery_status", "")).lower().strip()
        if status_raw not in _ACTIVE_STATUSES:
            continue

        brow = building_lookup.get(lottery_id, {})

        # --- ZIP code ---
        # vy5i-a666 uses "postcode"; nibs-na6y uses "address_zipcode".
        # Accept only 5-digit numeric postcodes; "Multi" and blanks fall back to building ZIP.
        def _valid_zip(v) -> Optional[str]:
            s = str(v or "").strip()[:5]
            return s if re.fullmatch(r"\d{5}", s) else None

        zip_code = (
            _valid_zip(row.get("postcode"))
            or _valid_zip(brow.get("address_zipcode"))
        )

        # --- Borough ---
        borough = (
            _normalize_borough(row.get("borough"))
            or _normalize_borough(brow.get("borough"))
            or _borough_from_zip(zip_code or "")
        )

        # --- Address ---
        house_num = str(brow.get("house_number") or "").strip()
        street    = str(brow.get("street_name")  or "").strip()
        if house_num and street:
            address = f"{house_num} {street}, {borough or 'New York'}, NY {zip_code or ''}".strip(", ")
        else:
            # Fall back to lottery name as a location hint
            address = str(row.get("lottery_name") or "").strip() or None

        # --- Title ---
        title = str(row.get("lottery_name") or f"Housing Connect Listing {lottery_id}").strip()

        # --- AMI / Income / Rent ---
        tiers = _extract_ami_tiers(row) or _extract_ami_tiers(brow)
        ami_label_str = _ami_label(tiers)
        min_income, max_income = _ami_income_range(tiers)
        # Rent ceiling derived from HUD 30%-of-income rule (HPD standard formula):
        #   max_rent = AMI_ceiling_income × 0.30 / 12
        min_rent, max_rent = _ami_rent_range(tiers)

        # --- Deadline ---
        deadline_raw = row.get("lottery_end_date") or None
        deadline = str(deadline_raw).split("T")[0] if deadline_raw else None

        # --- Lat / Lon ---
        # vy5i-a666 has lottery-level lat/lon; nibs-na6y has building-level.
        # "Multiple" signals a multi-building lottery with no single centroid.
        def _safe_coord(v) -> Optional[float]:
            try:
                f = float(v)
                return f if f != 0.0 else None
            except (TypeError, ValueError):
                return None

        lat = (
            _safe_coord(row.get("latitude"))
            or _safe_coord(brow.get("address_latitude"))
        )
        lon = (
            _safe_coord(row.get("longitude"))
            or _safe_coord(brow.get("address_longitude"))
        )

        listing = {
            "listing_id":   lottery_id,
            "title":        title[:500],
            "address":      address[:300] if address else None,
            "borough":      borough,
            "zip_code":     zip_code,
            "min_income":   min_income,
            "max_income":   max_income,
            "min_rent":     min_rent,
            "max_rent":     max_rent,
            "ami_percentage": ami_label_str,
            "deadline":     deadline,
            "url":          _LISTING_URL.format(lottery_id=lottery_id),
            "latitude":     lat,
            "longitude":    lon,
        }
        try:
            _upsert_listing(conn, listing)
            saved += 1
            active_ids.add(lottery_id)
        except Exception as exc:
            logger.warning("[opendata] upsert failed for %s: %s", lottery_id, exc)
            upsert_failed = True  # any DB failure prevents stale-row deletion

    # --- Stale-row reconciliation ---
    # Only safe to delete when:
    #   1. Both Socrata fetches completed without a page-level error, AND
    #   2. Every active row was upserted without a DB failure
    # This ensures active_ids is a complete, trustworthy picture of current listings.
    reconcile_safe = fetch_complete and not upsert_failed and bool(active_ids)
    if reconcile_safe:
        placeholders = ", ".join("?" * len(active_ids))
        stale_count = conn.execute(
            f"SELECT COUNT(*) FROM housing_connect_listings WHERE listing_id NOT IN ({placeholders})",
            list(active_ids)
        ).fetchone()[0]
        if stale_count:
            conn.execute(
                f"DELETE FROM housing_connect_listings WHERE listing_id NOT IN ({placeholders})",
                list(active_ids)
            )
            logger.info("[opendata] Removed %d stale listings no longer active", stale_count)
        conn.commit()
    elif not reconcile_safe and active_ids:
        logger.warning(
            "[opendata] Skipping stale-row deletion: fetch_complete=%s, upsert_failed=%s — "
            "cached rows preserved to avoid data loss on partial refresh",
            fetch_complete, upsert_failed
        )

    logger.info("[opendata] Upserted %d active listings", saved)
    return saved


# ---------------------------------------------------------------------------
# Layer 2 – Playwright (secondary, best-effort)
# ---------------------------------------------------------------------------

def _scrape_playwright(conn: duckdb.DuckDBPyConnection) -> int:
    """Navigate the Housing Connect SPA; gracefully handles missing libgbm."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.info("[playwright] Not installed — skipping.")
        return 0

    url = f"{_HC_BASE}/PublicWeb/search-lotteries"
    saved = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.set_default_timeout(20_000)
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_selector(
                "[class*='listing-card'], [class*='LotteryCard'], article",
                timeout=15_000
            )
            html = page.content()
            browser.close()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        saved = _parse_html_listings(conn, soup, source="playwright")

    except Exception as exc:
        msg = str(exc)
        if any(k in msg for k in ("libgbm", "chrome-headless-shell", "TargetClosedError", "BrowserType.launch")):
            logger.info("[playwright] Browser unavailable (%s) — skipping.", type(exc).__name__)
        else:
            logger.warning("[playwright] Scrape failed (%s: %s) — skipping.", type(exc).__name__, exc)

    return saved


# ---------------------------------------------------------------------------
# Layer 3 – requests + BeautifulSoup (tertiary)
# ---------------------------------------------------------------------------

_SESSION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}


def _try_hc_api_rent(listing_id: str) -> tuple[Optional[float], Optional[float]]:
    """
    Attempt to fetch rent data from the Housing Connect internal API for one listing.
    The SPA calls something like /api/public/lotteries/{id}/units.
    Returns (min_rent, max_rent) or (None, None) on any failure.
    """
    endpoints = [
        f"{_HC_BASE}/api/public/lotteries/{listing_id}/units",
        f"{_HC_BASE}/api/public/lotteries/{listing_id}",
        f"{_HC_BASE}/api/public/listings/{listing_id}",
    ]
    for ep in endpoints:
        try:
            resp = requests.get(ep, headers=_SESSION_HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            rents: list[float] = []
            # Handle list-of-units or single-object responses
            items = data if isinstance(data, list) else [data]
            for item in items:
                for key in ("rent", "monthly_rent", "min_rent", "max_rent", "base_rent"):
                    v = _safe_float(item.get(key))
                    if v and v > 0:
                        rents.append(v)
                # Nested units array
                for unit in item.get("units", []):
                    v = _safe_float(unit.get("rent") or unit.get("monthly_rent"))
                    if v and v > 0:
                        rents.append(v)
            if rents:
                return min(rents), max(rents)
        except Exception:
            continue
    return None, None


def _scrape_bs4(conn: duckdb.DuckDBPyConnection) -> int:
    """
    Fetch the Housing Connect search page statically and parse any HTML cards.
    Also queries the Housing Connect internal API for rent data on active listings.
    """
    url = f"{_HC_BASE}/PublicWeb/search-lotteries"
    saved = 0

    # --- Static HTML parse ---
    try:
        resp = requests.get(url, headers=_SESSION_HEADERS, timeout=20)
        resp.raise_for_status()
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            saved += _parse_html_listings(conn, soup, source="bs4")
        except ImportError:
            logger.info("[bs4] BeautifulSoup4 not installed.")
    except Exception as exc:
        logger.warning("[bs4] Static page request failed: %s", exc)

    # --- Enrich active listings with rent via HC internal API ---
    try:
        rows = conn.execute(
            "SELECT listing_id FROM housing_connect_listings "
            "WHERE min_rent IS NULL AND url IS NOT NULL LIMIT 50"
        ).fetchall()
        listing_ids = [r[0] for r in rows]
        enriched = 0
        for lid in listing_ids:
            min_r, max_r = _try_hc_api_rent(lid)
            if min_r is not None:
                conn.execute(
                    "UPDATE housing_connect_listings SET min_rent=?, max_rent=?, scraped_at=now() WHERE listing_id=?",
                    [min_r, max_r, lid]
                )
                conn.commit()
                enriched += 1
        if enriched:
            logger.info("[bs4/api] Enriched %d listings with rent data", enriched)
    except Exception as exc:
        logger.warning("[bs4/api] Rent enrichment failed: %s", exc)

    return saved


# ---------------------------------------------------------------------------
# HTML card parser (shared by Playwright and BS4 layers)
# ---------------------------------------------------------------------------

def _parse_html_listings(conn: duckdb.DuckDBPyConnection, soup, source: str = "html") -> int:
    saved = 0
    cards = (
        soup.select("[class*='listing-card']")
        or soup.select("[class*='LotteryCard']")
        or soup.select("article")
    )

    if not cards:
        # Try extracting JSON embedded in <script> tags
        for script in soup.find_all("script"):
            text = script.string or ""
            if "lottery_id" in text or "lotteryId" in text:
                try:
                    m = re.search(r"(\[{.*?}\]|\{.*?\})", text, re.DOTALL)
                    if m:
                        data = json.loads(m.group(1))
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            listing = _map_json_card(item)
                            if listing:
                                try:
                                    _upsert_listing(conn, listing)
                                    saved += 1
                                except Exception:
                                    pass
                except Exception:
                    pass
        if saved:
            logger.info("[%s] %d rows from embedded JSON", source, saved)
        return saved

    for card in cards:
        link = card.find("a", href=True)
        lottery_id = None
        if link:
            m = re.search(r"/listings?/([A-Za-z0-9\-_]+)", link["href"])
            if m:
                lottery_id = m.group(1)
        if not lottery_id:
            lottery_id = card.get("data-id") or card.get("data-lottery-id")
        if not lottery_id:
            continue

        title_el = card.find(re.compile(r"^h[2-4]$"))
        title = title_el.get_text(strip=True) if title_el else card.get_text(" ", strip=True)[:120]

        # Try to extract rent from card text
        card_text = card.get_text(" ")
        rent_vals: list[float] = []
        for m in re.finditer(r"\$(\d[\d,]*)", card_text):
            v = _safe_float(m.group(1).replace(",", ""))
            if v and 200 < v < 10_000:  # plausible monthly rent
                rent_vals.append(v)

        # Deadline
        deadline = None
        for m in re.finditer(r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})", card_text):
            deadline = m.group(1)
            break

        listing = {
            "listing_id":   lottery_id,
            "title":        title[:500],
            "address":      None,
            "borough":      None,
            "zip_code":     None,
            "min_income":   None,
            "max_income":   None,
            "min_rent":     min(rent_vals) if rent_vals else None,
            "max_rent":     max(rent_vals) if rent_vals else None,
            "ami_percentage": None,
            "deadline":     deadline,
            "url":          _LISTING_URL.format(lottery_id=lottery_id),
        }
        try:
            _upsert_listing(conn, listing)
            saved += 1
        except Exception as exc:
            logger.debug("[%s] upsert failed for %s: %s", source, lottery_id, exc)

    logger.info("[%s] %d rows from HTML cards", source, saved)
    return saved


def _map_json_card(item: dict) -> Optional[dict]:
    lottery_id = (
        item.get("lottery_id") or item.get("lotteryId")
        or item.get("id") or item.get("ID")
    )
    if not lottery_id:
        return None
    lottery_id = str(lottery_id)
    tiers = _extract_ami_tiers(item)
    ami = _ami_label(tiers)
    min_income, max_income = _ami_income_range(tiers)
    zip_code = str(item.get("postcode") or item.get("address_zipcode") or item.get("zip_code") or item.get("zip") or "")[:5] or None
    borough = _normalize_borough(item.get("borough")) or _borough_from_zip(zip_code or "")
    return {
        "listing_id":   lottery_id,
        "title":        str(item.get("lottery_name") or item.get("title") or f"Listing {lottery_id}")[:500],
        "address":      item.get("address"),
        "borough":      borough,
        "zip_code":     zip_code,
        "min_income":   min_income,
        "max_income":   max_income,
        "min_rent":     _safe_float(item.get("min_rent")),
        "max_rent":     _safe_float(item.get("max_rent")),
        "ami_percentage": ami,
        "deadline":     item.get("deadline") or item.get("lottery_end_date"),
        "url":          _LISTING_URL.format(lottery_id=lottery_id),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_housing_connect(db_path: str = "housing.duckdb") -> int:
    """
    Run all three scraping layers and return total listings saved.

    Layers run in priority order; each failure is logged and does not abort
    subsequent layers.  The Open Data layer always runs first so real data
    is populated even when the browser environment is unavailable.

    Args:
        db_path: Path to the DuckDB database file.

    Returns:
        Total number of rows upserted / updated across all layers.
    """
    logger.info("=== Housing Connect scraper starting ===")

    from db.schema import get_connection, init_db
    init_db(db_path)
    conn = get_connection(db_path)

    total = 0

    for layer_name, layer_fn in [
        ("layer1/opendata",  _scrape_opendata),
        ("layer2/playwright", _scrape_playwright),
        ("layer3/bs4",       _scrape_bs4),
    ]:
        try:
            n = layer_fn(conn)
            logger.info("[%s] saved/updated %d rows", layer_name, n)
            total += n
        except Exception as exc:
            logger.error("[%s] unexpected error: %s", layer_name, exc, exc_info=True)

    conn.close()
    logger.info("=== Housing Connect scraper complete — total: %d ===", total)
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    count = scrape_housing_connect()
    print(f"Scraper finished — {count} listings saved.")
