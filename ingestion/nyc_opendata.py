"""
NYC Open Data ingestion module.

Ingests rent-stabilized / tax-benefit building records from two authoritative
NYC Open Data sources:

Primary — NYC MapPLUTO (DOF assessor data):
  Endpoint : https://data.cityofnewyork.us/resource/64uk-42ks.json
  Filter   : residential multifamily buildings (bldgclass C* or D*) with an
             active tax exemption (exempttot > 0), which indicates a J-51 or
             421-a programme that confers rent-stabilization obligations.
  Fields   : bbl, address, borough, zipcode, latitude, longitude,
             bldgclass, exempttot, unitsres, yearbuilt

Supplementary — 421-a(16) Affordable New York Programme:
  Endpoint : https://data.cityofnewyork.us/resource/pq4c-wbq4.json
  Fields   : reported_property_addresses, reported_borough, postcode,
             latitude, longitude

Borough codes used by MapPLUTO: BK, BX, MN, QN, SI
"""

import hashlib
import logging
import time
from typing import Optional

import duckdb
import requests

from db.schema import DB_PATH, init_db

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
PLUTO_ENDPOINT = "https://data.cityofnewyork.us/resource/64uk-42ks.json"
A421_ENDPOINT  = "https://data.cityofnewyork.us/resource/pq4c-wbq4.json"
GEOSEARCH_URL  = "https://geosearch.planninglabs.nyc/v2/search"

PAGE_LIMIT = 1000

# MapPLUTO uses 2-letter uppercase borough codes
BOROUGH_TO_PLUTO: dict[str, str] = {
    "manhattan":     "MN",
    "bronx":         "BX",
    "brooklyn":      "BK",
    "queens":        "QN",
    "staten island": "SI",
}

PLUTO_TO_FULL: dict[str, str] = {
    "MN": "Manhattan",
    "BX": "Bronx",
    "BK": "Brooklyn",
    "QN": "Queens",
    "SI": "Staten Island",
}


def _normalise_borough(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().lower()
    # Try full-name map first
    full = {
        "manhattan": "Manhattan", "bronx": "Bronx", "brooklyn": "Brooklyn",
        "queens": "Queens", "staten island": "Staten Island",
        "mn": "Manhattan", "bx": "Bronx", "bk": "Brooklyn",
        "qn": "Queens", "si": "Staten Island",
    }
    return full.get(key, raw.strip().title())


def _make_building_id(address: str, borough: str, zip_code: str) -> str:
    key = f"{address}|{borough}|{zip_code}".lower()
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Geocoding fallback
# ---------------------------------------------------------------------------
def _geocode(
    address: str,
    borough: str = "",
    retries: int = 2,
) -> tuple[Optional[float], Optional[float]]:
    """Resolve lat/lon via NYC GeoSearch. Returns (lat, lon) or (None, None)."""
    query = f"{address}, {borough}, New York City" if borough else f"{address}, New York City"
    for attempt in range(retries):
        try:
            resp = requests.get(GEOSEARCH_URL, params={"text": query, "size": 1}, timeout=8)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if features:
                coords = features[0]["geometry"]["coordinates"]  # [lon, lat]
                return float(coords[1]), float(coords[0])
        except Exception as exc:
            logger.debug("GeoSearch attempt %d for '%s': %s", attempt + 1, address, exc)
            time.sleep(0.3)
    return None, None


# ---------------------------------------------------------------------------
# MapPLUTO fetch — authoritative DOF tax-benefit (J-51 / 421-a) data
# ---------------------------------------------------------------------------
def _pluto_where_clause(
    borough: Optional[str],
    zip_code: Optional[str],
) -> str:
    """Build the $where clause for a MapPLUTO query."""
    # Base: residential multifamily with active tax exemption
    clauses = [
        "(bldgclass LIKE 'C%' OR bldgclass LIKE 'D%')",
        "exempttot > 0",
        "unitsres > 0",
    ]
    if borough:
        pluto_code = BOROUGH_TO_PLUTO.get(borough.lower().strip())
        if pluto_code:
            clauses.append(f"borough='{pluto_code}'")
    if zip_code:
        clauses.append(f"zipcode='{zip_code.strip()}'")
    return " AND ".join(clauses)


def _fetch_pluto_page(
    borough: Optional[str],
    zip_code: Optional[str],
    limit: int,
    offset: int,
) -> list[dict]:
    params = {
        "$limit": limit,
        "$offset": offset,
        "$order": "bbl",
        "$select": "bbl,address,borough,zipcode,latitude,longitude,bldgclass,exempttot,unitsres,yearbuilt",
        "$where": _pluto_where_clause(borough, zip_code),
    }
    try:
        resp = requests.get(PLUTO_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("MapPLUTO API error: %s", exc)
        return []


def _normalise_pluto(raw: dict) -> Optional[dict]:
    """Map a MapPLUTO record to the stabilized_buildings schema."""
    address = (raw.get("address") or "").strip()
    if not address:
        return None

    pluto_boro = (raw.get("borough") or "").strip().upper()
    borough = PLUTO_TO_FULL.get(pluto_boro, pluto_boro.title())
    zip_code = (raw.get("zipcode") or "").strip()
    bbl = (raw.get("bbl") or "").strip()
    # Remove decimal suffix MapPLUTO sometimes appends
    if "." in bbl:
        bbl = bbl.split(".")[0]

    bldgclass  = raw.get("bldgclass", "")
    exempttot  = raw.get("exempttot", "0")
    yearbuilt  = raw.get("yearbuilt", "")

    # Tax-benefit programme label
    if bldgclass.startswith("C"):
        prog = "J-51 / Walk-up Apt (Tax Exempt)"
    elif bldgclass.startswith("D"):
        prog = "421-a / Elevator Apt (Tax Exempt)"
    else:
        prog = f"Tax-Exempt Residential ({bldgclass})"

    # Append year-built info when available
    if yearbuilt and yearbuilt != "0":
        prog += f" — Built {yearbuilt}"

    try:
        lat = float(raw["latitude"])  if raw.get("latitude")  else None
        lon = float(raw["longitude"]) if raw.get("longitude") else None
    except (TypeError, ValueError):
        lat, lon = None, None

    building_id = _make_building_id(address, borough, zip_code)

    return {
        "building_id": building_id,
        "street_address": address,
        "borough": borough,
        "zip_code": zip_code,
        "bbl": bbl,
        "tax_benefit_program": prog,
        "latitude": lat,
        "longitude": lon,
    }


# ---------------------------------------------------------------------------
# 421-a(16) supplementary source
# ---------------------------------------------------------------------------
def _fetch_421a_page(
    borough: Optional[str],
    zip_code: Optional[str],
    limit: int,
    offset: int,
) -> list[dict]:
    params: dict = {"$limit": limit, "$offset": offset, "$order": "no"}
    clauses: list[str] = []
    if borough:
        clauses.append(f"upper(reported_borough)='{borough.upper()}'")
    if zip_code:
        clauses.append(f"postcode='{zip_code.strip()}'")
    if clauses:
        params["$where"] = " AND ".join(clauses)
    try:
        resp = requests.get(A421_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("421-a API error: %s", exc)
        return []


def _normalise_421a(raw: dict) -> Optional[dict]:
    """Map a 421-a(16) record to the stabilized_buildings schema."""
    address = (raw.get("reported_property_addresses") or "").strip()
    if not address:
        return None

    borough_raw = (raw.get("reported_borough") or raw.get("presumed_borough") or "").strip()
    borough = _normalise_borough(borough_raw)
    zip_code = (raw.get("postcode") or "").strip()

    try:
        lat = float(raw["latitude"])  if raw.get("latitude")  else None
        lon = float(raw["longitude"]) if raw.get("longitude") else None
    except (TypeError, ValueError):
        lat, lon = None, None

    building_id = _make_building_id(address, borough or "", zip_code)

    return {
        "building_id": building_id,
        "street_address": address,
        "borough": borough,
        "zip_code": zip_code,
        "bbl": "",
        "tax_benefit_program": "421-a(16) Affordable New York",
        "latitude": lat,
        "longitude": lon,
    }


# ---------------------------------------------------------------------------
# Upsert helper
# ---------------------------------------------------------------------------
def _upsert_buildings(
    conn: duckdb.DuckDBPyConnection,
    records: list[dict],
    geocode_missing: bool = False,
    geocode_sample: int = 50,
) -> int:
    inserted = 0
    geocoded = 0

    for rec in records:
        if not rec:
            continue

        if geocode_missing and rec["latitude"] is None:
            if geocode_sample == 0 or geocoded < geocode_sample:
                lat, lon = _geocode(rec["street_address"], rec.get("borough") or "")
                rec["latitude"] = lat
                rec["longitude"] = lon
                if lat:
                    geocoded += 1
                    time.sleep(0.12)

        try:
            conn.execute(
                """
                INSERT INTO stabilized_buildings
                    (building_id, street_address, borough, zip_code,
                     bbl, tax_benefit_program, latitude, longitude, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())
                ON CONFLICT (building_id) DO UPDATE SET
                    street_address      = EXCLUDED.street_address,
                    borough             = EXCLUDED.borough,
                    zip_code            = EXCLUDED.zip_code,
                    bbl                 = EXCLUDED.bbl,
                    tax_benefit_program = EXCLUDED.tax_benefit_program,
                    latitude            = COALESCE(EXCLUDED.latitude,
                                                   stabilized_buildings.latitude),
                    longitude           = COALESCE(EXCLUDED.longitude,
                                                   stabilized_buildings.longitude),
                    updated_at          = now()
                """,
                [
                    rec["building_id"],
                    rec["street_address"],
                    rec["borough"],
                    rec["zip_code"],
                    rec["bbl"],
                    rec["tax_benefit_program"],
                    rec["latitude"],
                    rec["longitude"],
                ],
            )
            inserted += 1
        except Exception as exc:
            logger.warning("Upsert failed for %s: %s", rec.get("building_id"), exc)

    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def ingest_stabilized_buildings(
    borough: Optional[str] = None,
    zip_code: Optional[str] = None,
    max_records: int = 5000,
    geocode_missing: bool = False,
    geocode_sample: int = 50,
    include_421a: bool = True,
    db_path: str = DB_PATH,
) -> int:
    """
    Ingest rent-stabilized / tax-benefit building records from NYC Open Data.

    Primary source: NYC MapPLUTO — residential multifamily buildings (C/D
    building class) with active DOF tax exemptions (exempttot > 0), which
    indicates J-51 or 421-a programmes that confer rent-stabilization
    obligations. Includes full address and pre-geocoded lat/lon from the DOF
    assessor database.

    Supplementary source: 421-a(16) Affordable New York registrations.

    Args:
        borough:        Borough name ('Brooklyn', 'Manhattan', etc.) or None.
        zip_code:       5-digit zip code or None.
        max_records:    Cap on MapPLUTO records per call.
        geocode_missing: Geocode records that lack coordinates via GeoSearch.
        geocode_sample: Max records to geocode per call when geocode_missing.
        include_421a:   Also ingest from the 421-a(16) programme dataset.
        db_path:        Path to the DuckDB file.

    Returns:
        Total records upserted.
    """
    conn = init_db(db_path)
    total = 0

    # ── MapPLUTO (primary) ────────────────────────────────────────────────
    logger.info(
        "MapPLUTO ingestion start | borough=%s zip=%s max=%d",
        borough, zip_code, max_records,
    )
    offset = 0
    while total < max_records:
        batch_size = min(PAGE_LIMIT, max_records - total)
        raw = _fetch_pluto_page(borough=borough, zip_code=zip_code,
                                limit=batch_size, offset=offset)
        if not raw:
            break

        normalised = [_normalise_pluto(r) for r in raw]
        normalised = [r for r in normalised if r]
        count = _upsert_buildings(conn, normalised,
                                  geocode_missing=geocode_missing,
                                  geocode_sample=geocode_sample)
        total += count
        offset += len(raw)
        logger.info("MapPLUTO batch offset=%d fetched=%d upserted=%d total=%d",
                    offset - len(raw), len(raw), count, total)
        if len(raw) < batch_size:
            break

    logger.info("MapPLUTO ingestion complete. Upserted: %d", total)

    # ── 421-a(16) (supplementary) ─────────────────────────────────────────
    if include_421a:
        logger.info("421-a(16) ingestion start | borough=%s zip=%s", borough, zip_code)
        a_offset = 0
        a_total = 0
        while True:
            raw_a = _fetch_421a_page(borough=borough, zip_code=zip_code,
                                     limit=PAGE_LIMIT, offset=a_offset)
            if not raw_a:
                break
            norm_a = [_normalise_421a(r) for r in raw_a]
            norm_a = [r for r in norm_a if r]
            count_a = _upsert_buildings(conn, norm_a)
            a_total += count_a
            a_offset += len(raw_a)
            if len(raw_a) < PAGE_LIMIT:
                break
        total += a_total
        logger.info("421-a(16) ingestion complete. Upserted: %d", a_total)

    conn.close()
    logger.info("Total ingestion complete. Grand total upserted: %d", total)
    return total


if __name__ == "__main__":
    import sys
    borough_arg = sys.argv[1] if len(sys.argv) > 1 else None
    zip_arg     = sys.argv[2] if len(sys.argv) > 2 else None
    n = ingest_stabilized_buildings(
        borough=borough_arg,
        zip_code=zip_arg,
        max_records=2000,
    )
    print(f"Done. Upserted {n} records.")
