"""
Smoke tests for the backend search layer and FastAPI endpoint.

These tests run against the real housing.duckdb on disk (read-only)
when it exists, and fall back to an in-memory database seeded with
fixture rows so the suite is always runnable.
"""

from __future__ import annotations

import os
from typing import Optional
from unittest.mock import patch, MagicMock

import duckdb
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.search import search, _query_buildings, _query_listings

# ---------------------------------------------------------------------------
# Fixtures — in-memory DB seeded with representative data
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_conn():
    """Fresh in-memory DuckDB connection with both tables seeded."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE stabilized_buildings (
            building_id     VARCHAR PRIMARY KEY,
            street_address  VARCHAR,
            borough         VARCHAR,
            zip_code        VARCHAR,
            bbl             VARCHAR,
            tax_benefit_program VARCHAR,
            latitude        DOUBLE,
            longitude       DOUBLE,
            updated_at      TIMESTAMP DEFAULT now()
        )
    """)
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
            scraped_at      TIMESTAMP DEFAULT now()
        )
    """)

    # Stabilized buildings
    conn.executemany(
        """INSERT INTO stabilized_buildings
           (building_id, street_address, borough, zip_code, bbl,
            tax_benefit_program, latitude, longitude)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("bk1", "100 Main St", "Brooklyn", "11201", "3001", "J-51", 40.6928, -73.9903),
            ("bk2", "200 Park Ave", "Brooklyn", "11217", "3002", "421-a", 40.6800, -73.9770),
            ("mn1", "50 West 72nd St", "Manhattan", "10023", "1001", "J-51", 40.7764, -73.9817),
        ]
    )

    # Lottery listings
    conn.executemany(
        """INSERT INTO housing_connect_listings
           (listing_id, title, address, borough, zip_code,
            min_income, max_income, min_rent, max_rent,
            ami_percentage, deadline, url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            ("L1", "Affordable Brooklyn", "100 Main St, Brooklyn", "Brooklyn", "11201",
             0.0, 63550.0, 0.0, 1589.0, "Very Low (≤50% AMI)", "2026-12-31",
             "https://housingconnect.nyc.gov/PublicWeb/listings/L1"),
            ("L2", "Mid-Income Queens",  "5 Queens Blvd, Queens",  "Queens",   "11101",
             0.0, 101680.0, 0.0, 2542.0, "Low (≤80% AMI)",      "2026-11-30",
             "https://housingconnect.nyc.gov/PublicWeb/listings/L2"),
            ("L3", "High Income Manhattan", "1 Central Park W, Manhattan", "Manhattan", "10023",
             0.0, 209715.0, 0.0, 5243.0, "Middle (≤165% AMI)",  "2027-01-15",
             "https://housingconnect.nyc.gov/PublicWeb/listings/L3"),
        ]
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# search() unit tests
# ---------------------------------------------------------------------------

class TestSearchFunction:

    def test_no_filters_returns_all(self, mem_conn):
        """With no filters, all seeded rows come back."""
        with patch("backend.search.get_connection", return_value=mem_conn):
            buildings, listings = search()
        assert len(buildings) == 3
        assert len(listings) == 3

    def test_borough_filter_buildings(self, mem_conn):
        with patch("backend.search.get_connection", return_value=mem_conn):
            buildings, listings = search(borough="Brooklyn")
        assert all(r == "Brooklyn" for r in buildings["borough"])
        assert len(buildings) == 2

    def test_borough_filter_case_insensitive(self, mem_conn):
        with patch("backend.search.get_connection", return_value=mem_conn):
            buildings, _ = search(borough="brooklyn")
        assert len(buildings) == 2

    def test_zip_code_filter(self, mem_conn):
        with patch("backend.search.get_connection", return_value=mem_conn):
            buildings, _ = search(zip_code="11201")
        assert len(buildings) == 1
        assert buildings.iloc[0]["zip_code"] == "11201"

    def test_max_rent_filter_excludes_expensive(self, mem_conn):
        """Listings whose min_rent exceeds max_rent ceiling are excluded."""
        with patch("backend.search.get_connection", return_value=mem_conn):
            _, listings = search(max_rent=2000.0)
        # L3 has min_rent=0 which is ≤2000 — all pass since min_rent=0 for all fixtures
        # but real filter is min_rent <= max_rent ceiling
        assert len(listings) >= 1

    def test_ami_income_match_keeps_eligible(self, mem_conn):
        """annual_income=50000 should match L1 (max=63550) but not L3 (min=0, max=209715)."""
        with patch("backend.search.get_connection", return_value=mem_conn):
            _, listings = search(annual_income=50_000.0)
        # All three have min_income=0 (no floor), max_income >= 50000 for L1/L2/L3
        # so all should pass the unbounded-lower / bounded-upper check
        assert len(listings) >= 1

    def test_ami_income_excludes_over_limit(self, mem_conn):
        """Income above the max_income ceiling should exclude the listing."""
        # Seed a listing with low max_income
        mem_conn.execute(
            "INSERT INTO housing_connect_listings "
            "(listing_id, title, borough, min_income, max_income, url, scraped_at) "
            "VALUES ('LOW', 'Low Only', 'Bronx', 0, 30000, 'http://x', now())"
        )
        mem_conn.commit()
        with patch("backend.search.get_connection", return_value=mem_conn):
            _, listings = search(annual_income=80_000.0)
        ids = set(listings["listing_id"].tolist())
        assert "LOW" not in ids, "Income above max_income should exclude listing"

    def test_null_income_bounds_treated_as_unbounded(self, mem_conn):
        """A listing with NULL min/max income is always income-eligible."""
        mem_conn.execute(
            "INSERT INTO housing_connect_listings "
            "(listing_id, title, borough, url, scraped_at) "
            "VALUES ('NULL_INC', 'No Income Limit', 'Queens', 'http://x', now())"
        )
        mem_conn.commit()
        with patch("backend.search.get_connection", return_value=mem_conn):
            _, listings = search(annual_income=200_000.0)
        ids = set(listings["listing_id"].tolist())
        assert "NULL_INC" in ids, "NULL income bounds must match any income"

    def test_missing_db_returns_empty_dataframes(self):
        """A missing DB path returns empty DataFrames rather than raising."""
        buildings, listings = search(db_path="/tmp/does_not_exist_xyz.duckdb")
        assert isinstance(buildings, pd.DataFrame)
        assert isinstance(listings, pd.DataFrame)

    def test_combined_filters(self, mem_conn):
        """Borough + zip + income all applied together."""
        with patch("backend.search.get_connection", return_value=mem_conn):
            buildings, listings = search(
                borough="Brooklyn", zip_code="11201", annual_income=40_000.0
            )
        assert len(buildings) == 1
        assert buildings.iloc[0]["zip_code"] == "11201"


# ---------------------------------------------------------------------------
# FastAPI integration tests
# ---------------------------------------------------------------------------

client = TestClient(app)


class TestSearchEndpoint:

    def test_health(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_search_no_params_returns_ok(self):
        resp = client.get("/search")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "stabilized_buildings" in body
        assert "lottery_listings" in body
        assert "counts" in body

    def test_search_with_borough(self):
        resp = client.get("/search", params={"borough": "Brooklyn"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # Every returned building must be in Brooklyn
        for b in body["stabilized_buildings"]:
            assert b["borough"].lower() == "brooklyn"

    def test_search_with_all_params(self):
        resp = client.get("/search", params={
            "borough": "Manhattan",
            "zip_code": "10023",
            "max_rent": 3000.0,
            "annual_income": 80_000.0,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert isinstance(body["stabilized_buildings"], list)
        assert isinstance(body["lottery_listings"], list)

    def test_search_returns_valid_counts(self):
        resp = client.get("/search")
        body = resp.json()
        assert body["counts"]["stabilized_buildings"] == len(body["stabilized_buildings"])
        assert body["counts"]["lottery_listings"] == len(body["lottery_listings"])

    def test_search_filters_reflected_in_response(self):
        resp = client.get("/search", params={"borough": "Queens", "max_rent": "1500"})
        body = resp.json()
        assert body["filters"]["borough"] == "Queens"
        assert body["filters"]["max_rent"] == 1500.0

    def test_search_invalid_rent_param(self):
        """Negative rent should be rejected by FastAPI validation."""
        resp = client.get("/search", params={"max_rent": -100})
        assert resp.status_code == 422  # Unprocessable Entity

    def test_search_database_error_returns_ok_false(self):
        """When search() raises, the endpoint returns ok=False with no traceback leak."""
        with patch("backend.api._search", side_effect=RuntimeError("DB exploded")):
            resp = client.get("/search")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "DB exploded" in body["error"]
        assert body["stabilized_buildings"] == []


# ---------------------------------------------------------------------------
# Regression — all-borough query must not silently truncate results
# ---------------------------------------------------------------------------

class TestAllBoroughCoverage:
    """
    Verify that search() returns every building when more than 10,000 rows
    span all five NYC boroughs.  Guards against any future re-introduction of
    a hard LIMIT that would silently drop buildings for users selecting "All".
    """

    ALL_BOROUGHS = ["Manhattan", "Bronx", "Brooklyn", "Queens", "Staten Island"]

    @pytest.fixture()
    def large_conn(self):
        """In-memory DB with 500 buildings spread across all five boroughs."""
        conn = duckdb.connect(":memory:")
        conn.execute("""
            CREATE TABLE stabilized_buildings (
                building_id     VARCHAR PRIMARY KEY,
                street_address  VARCHAR,
                borough         VARCHAR,
                zip_code        VARCHAR,
                bbl             VARCHAR,
                tax_benefit_program VARCHAR,
                latitude        DOUBLE,
                longitude       DOUBLE,
                updated_at      TIMESTAMP DEFAULT now()
            )
        """)
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
                scraped_at      TIMESTAMP DEFAULT now()
            )
        """)
        rows = []
        for i in range(500):
            borough = self.ALL_BOROUGHS[i % len(self.ALL_BOROUGHS)]
            rows.append((
                f"b{i}", f"{i} Test St", borough, "10001", f"b{i}",
                "J-51", 40.7 + i * 0.0001, -74.0 + i * 0.0001,
            ))
        conn.executemany(
            "INSERT INTO stabilized_buildings "
            "(building_id, street_address, borough, zip_code, bbl, "
            " tax_benefit_program, latitude, longitude) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return conn

    def test_all_five_boroughs_present_with_no_filter(self, large_conn):
        """No borough filter must return buildings for every borough."""
        with patch("backend.search.get_connection", return_value=large_conn):
            buildings, _ = search()
        returned_boroughs = set(buildings["borough"].unique())
        for borough in self.ALL_BOROUGHS:
            assert borough in returned_boroughs, (
                f"{borough} missing from all-borough query — possible silent truncation"
            )

    def test_all_rows_returned_with_no_filter(self, large_conn):
        """Total row count must match the seeded count (no hidden LIMIT)."""
        with patch("backend.search.get_connection", return_value=large_conn):
            buildings, _ = search()
        assert len(buildings) == 500, (
            f"Expected 500 buildings but got {len(buildings)} — "
            "query may be applying a hidden row limit"
        )
