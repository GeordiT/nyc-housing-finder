"""
Unified query and filtering layer for the NYC Housing Aggregator.

Accepts: borough, zip_code, max_rent, annual_income
Returns: (buildings_df, listings_df) as pandas DataFrames
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from db.schema import DB_PATH, get_connection

logger = logging.getLogger(__name__)


def search(
    borough: Optional[str] = None,
    zip_code: Optional[str] = None,
    max_rent: Optional[float] = None,
    annual_income: Optional[float] = None,
    db_path: str = DB_PATH,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Query the database for matching stabilized buildings and lottery listings.

    Args:
        borough:       Borough name ('Manhattan', 'Brooklyn', …) or None for all.
        zip_code:      5-digit zip code string or None.
        max_rent:      Maximum monthly rent ceiling (applied to listings max_rent).
        annual_income: Household gross annual income for AMI matching.
        db_path:       Path to the DuckDB file.

    Returns:
        Tuple of (buildings_df, listings_df).
    """
    conn = get_connection(db_path)
    try:
        buildings_df = _query_buildings(conn, borough, zip_code)
        listings_df = _query_listings(conn, borough, zip_code, max_rent, annual_income)
    finally:
        conn.close()

    return buildings_df, listings_df


def _query_buildings(conn, borough: Optional[str], zip_code: Optional[str]) -> pd.DataFrame:
    """Query stabilized_buildings with optional filters."""
    where, params = _build_where(
        [
            ("borough", borough, "upper(borough) = upper(?)"),
            ("zip_code", zip_code, "zip_code = ?"),
        ]
    )
    sql = f"SELECT * FROM stabilized_buildings{where} ORDER BY borough, street_address"
    try:
        result = conn.execute(sql, params).fetchdf()
        return result
    except Exception as exc:
        logger.error("buildings query failed: %s", exc)
        return pd.DataFrame()


def _query_listings(
    conn,
    borough: Optional[str],
    zip_code: Optional[str],
    max_rent: Optional[float],
    annual_income: Optional[float],
) -> pd.DataFrame:
    """Query housing_connect_listings with income-range AMI matching."""
    conditions = []
    params: list = []

    if borough:
        conditions.append("upper(borough) = upper(?)")
        params.append(borough)
    if zip_code:
        conditions.append("zip_code = ?")
        params.append(zip_code)
    if max_rent is not None:
        # Keep listings whose minimum rent is at or below the ceiling
        conditions.append("(min_rent IS NULL OR min_rent <= ?)")
        params.append(max_rent)
    if annual_income is not None:
        # AMI match: income within [min_income, max_income]; null = unbounded
        conditions.append(
            "(min_income IS NULL OR min_income <= ?) AND (max_income IS NULL OR max_income >= ?)"
        )
        params.extend([annual_income, annual_income])

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM housing_connect_listings{where} ORDER BY deadline, title LIMIT 1000"
    try:
        result = conn.execute(sql, params).fetchdf()
        return result
    except Exception as exc:
        logger.error("listings query failed: %s", exc)
        return pd.DataFrame()


def _build_where(filters: list[tuple]) -> tuple[str, list]:
    """Build a WHERE clause from (column, value, template) tuples."""
    conditions = []
    params: list = []
    for _col, value, template in filters:
        if value is not None:
            conditions.append(template)
            params.append(value)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    return where, params
