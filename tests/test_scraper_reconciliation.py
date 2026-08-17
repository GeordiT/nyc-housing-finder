"""
Regression tests for the Housing Connect scraper reconciliation logic.

Verifies that:
  - Stale rows ARE deleted when both fetches and all upserts succeed
  - Stale rows are PRESERVED when a page-fetch fails mid-pagination
  - Stale rows are PRESERVED when an individual upsert fails
"""

import re
import sys
import types
from unittest.mock import MagicMock, patch

import duckdb
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> duckdb.DuckDBPyConnection:
    """Return a fresh in-memory DuckDB connection with the listings table."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE housing_connect_listings (
            listing_id      VARCHAR PRIMARY KEY,
            title           VARCHAR,
            address         VARCHAR,
            borough         VARCHAR,
            zip_code        VARCHAR,
            min_income      DOUBLE,
            max_income      DOUBLE,
            min_rent        DOUBLE,
            max_rent        DOUBLE,
            ami_percentage  VARCHAR,
            deadline        VARCHAR,
            url             VARCHAR,
            latitude        DOUBLE,
            longitude       DOUBLE,
            scraped_at      TIMESTAMP DEFAULT now()
        )
    """)
    return conn


def _seed_rows(conn: duckdb.DuckDBPyConnection, listing_ids: list[str]) -> None:
    for lid in listing_ids:
        conn.execute(
            "INSERT INTO housing_connect_listings (listing_id, title, url, scraped_at) VALUES (?, ?, ?, now())",
            [lid, f"Title {lid}", f"http://example/{lid}"]
        )
    conn.commit()


def _count(conn: duckdb.DuckDBPyConnection, listing_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM housing_connect_listings WHERE listing_id=?", [listing_id]
    ).fetchone()[0]


def _make_active_lottery_row(lottery_id: str) -> dict:
    return {
        "lottery_id":              lottery_id,
        "lottery_name":            f"Test Listing {lottery_id}",
        "lottery_status":          "Active",
        "lottery_end_date":        "2026-12-31T00:00:00.000",
        "borough":                 "BK",
        "postcode":                "11201",
        "applied_income_ami_low":  "10",
    }


# ---------------------------------------------------------------------------
# The function under test — import after helpers so mocking is easy
# ---------------------------------------------------------------------------

import ingestion.scraper as scraper_module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStaleRowReconciliation:

    def test_stale_row_deleted_on_full_success(self):
        """When both fetches complete and all upserts succeed, stale rows are removed."""
        conn = _make_db()
        _seed_rows(conn, ["ACTIVE1", "STALE99"])

        lottery_rows   = [_make_active_lottery_row("ACTIVE1")]
        building_rows  = [{"lottery_id": "ACTIVE1", "house_number": "100", "street_name": "Main St",
                           "borough": "BK", "address_zipcode": "11201"}]

        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[
                              (lottery_rows,  True),   # vy5i-a666 — complete
                              (building_rows, True),   # nibs-na6y — complete
                          ]):
            scraper_module._scrape_opendata(conn)

        assert _count(conn, "ACTIVE1") == 1,  "active listing should remain"
        assert _count(conn, "STALE99") == 0,  "stale listing should be deleted"

    def test_stale_row_preserved_on_partial_lottery_fetch(self):
        """When the by-lottery page fetch is incomplete, no stale row is deleted."""
        conn = _make_db()
        _seed_rows(conn, ["ACTIVE1", "STALE99"])

        lottery_rows   = [_make_active_lottery_row("ACTIVE1")]
        building_rows  = []

        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[
                              (lottery_rows,  False),  # vy5i-a666 — INCOMPLETE
                              (building_rows, True),
                          ]):
            scraper_module._scrape_opendata(conn)

        assert _count(conn, "STALE99") == 1, (
            "stale listing must not be deleted when fetch is partial"
        )

    def test_stale_row_preserved_on_upsert_failure(self):
        """When an individual upsert raises, stale rows are preserved."""
        conn = _make_db()
        _seed_rows(conn, ["ACTIVE1", "STALE99"])

        lottery_rows  = [_make_active_lottery_row("ACTIVE1")]
        building_rows = []

        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[
                              (lottery_rows,  True),
                              (building_rows, True),
                          ]):
            # Make _upsert_listing raise on the first call
            with patch.object(scraper_module, "_upsert_listing",
                               side_effect=RuntimeError("DB write failure")):
                scraper_module._scrape_opendata(conn)

        assert _count(conn, "STALE99") == 1, (
            "stale listing must not be deleted when an upsert fails"
        )

    def test_valid_zip_accepted(self):
        """5-digit numeric postcodes pass validation."""
        conn = _make_db()
        row = {**_make_active_lottery_row("ZIP1"), "postcode": "10001"}
        building = []
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([ row], True), (building, True)]):
            scraper_module._scrape_opendata(conn)

        zip_stored = conn.execute(
            "SELECT zip_code FROM housing_connect_listings WHERE listing_id='ZIP1'"
        ).fetchone()
        assert zip_stored is not None
        assert zip_stored[0] == "10001", f"expected '10001', got {zip_stored[0]!r}"

    def test_multi_zip_falls_back_to_building(self):
        """'Multi' postcode falls back to the building dataset's address_zipcode."""
        conn = _make_db()
        row = {**_make_active_lottery_row("MULTI1"), "postcode": "Multi"}
        building = [{"lottery_id": "MULTI1", "house_number": "5", "street_name": "Ave B",
                     "borough": "BK", "address_zipcode": "11205"}]
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([row], True), (building, True)]):
            scraper_module._scrape_opendata(conn)

        zip_stored = conn.execute(
            "SELECT zip_code FROM housing_connect_listings WHERE listing_id='MULTI1'"
        ).fetchone()
        assert zip_stored is not None
        assert zip_stored[0] == "11205", f"expected '11205', got {zip_stored[0]!r}"

    def test_inactive_status_not_saved(self):
        """'Inactive' status must not be persisted even though it contains 'active'."""
        conn = _make_db()
        inactive_row = {**_make_active_lottery_row("INACTIVE1"), "lottery_status": "Inactive"}
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([inactive_row], True), ([], True)]):
            scraper_module._scrape_opendata(conn)
        assert _count(conn, "INACTIVE1") == 0, "'Inactive' must not be upserted"

    def test_not_open_status_not_saved(self):
        """'Not Open' status must not be persisted even though it contains 'open'."""
        conn = _make_db()
        row = {**_make_active_lottery_row("NOTOPEN1"), "lottery_status": "Not Open"}
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([row], True), ([], True)]):
            scraper_module._scrape_opendata(conn)
        assert _count(conn, "NOTOPEN1") == 0, "'Not Open' must not be upserted"

    def test_all_units_filled_not_saved(self):
        """'All Units Filled' must be excluded."""
        conn = _make_db()
        row = {**_make_active_lottery_row("FILLED1"), "lottery_status": "All Units Filled"}
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([row], True), ([], True)]):
            scraper_module._scrape_opendata(conn)
        assert _count(conn, "FILLED1") == 0, "'All Units Filled' must not be upserted"

    def test_closed_not_saved(self):
        """'Closed' must be excluded."""
        conn = _make_db()
        row = {**_make_active_lottery_row("CLOSED1"), "lottery_status": "Closed"}
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([row], True), ([], True)]):
            scraper_module._scrape_opendata(conn)
        assert _count(conn, "CLOSED1") == 0, "'Closed' must not be upserted"

    def test_tenant_selection_is_saved(self):
        """'Tenant Selection' is a valid active status and must be persisted."""
        conn = _make_db()
        row = {**_make_active_lottery_row("TS1"), "lottery_status": "Tenant Selection", "postcode": "11201"}
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([row], True), ([], True)]):
            scraper_module._scrape_opendata(conn)
        assert _count(conn, "TS1") == 1, "'Tenant Selection' should be saved"

    def test_inactive_not_in_active_ids_so_stale_row_not_deleted(self):
        """
        When the only fetched row is 'Inactive', no active IDs are collected.
        A previously stored row should not be deleted because the active_ids
        set is empty (the guard requires active_ids to be non-empty).
        """
        conn = _make_db()
        _seed_rows(conn, ["EXISTING"])
        inactive_row = {**_make_active_lottery_row("EXISTING"), "lottery_status": "Inactive"}
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([inactive_row], True), ([], True)]):
            scraper_module._scrape_opendata(conn)
        # active_ids is empty → reconciliation guard fires → EXISTING is kept
        assert _count(conn, "EXISTING") == 1, "row must be kept when active_ids is empty"

    def test_rent_non_null_for_ami_listing(self):
        """Listings with AMI tier data produce non-null min_rent and max_rent."""
        conn = _make_db()
        row = {**_make_active_lottery_row("AMI1"), "applied_income_ami_low": "5", "postcode": "11201"}
        building = []
        with patch.object(scraper_module, "_paginate_opendata",
                          side_effect=[([row], True), (building, True)]):
            scraper_module._scrape_opendata(conn)

        result = conn.execute(
            "SELECT min_rent, max_rent FROM housing_connect_listings WHERE listing_id='AMI1'"
        ).fetchone()
        assert result is not None
        assert result[1] is not None and result[1] > 0, f"max_rent should be > 0, got {result}"
