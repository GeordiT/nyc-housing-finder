"""
Database schema initialization for the NYC Housing Aggregator.
Uses DuckDB for fast local querying and caching.
"""

import duckdb
import os
from pathlib import Path

# Default database path
DB_PATH = os.environ.get("HOUSING_DB_PATH", "housing.duckdb")


def get_connection(db_path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection, creating the file if it doesn't exist."""
    conn = duckdb.connect(db_path)
    return conn


def init_db(db_path: str = DB_PATH) -> duckdb.DuckDBPyConnection:
    """
    Initialize the database and create tables if they don't exist.
    Returns an open connection.
    """
    conn = get_connection(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS stabilized_buildings (
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

    _ensure_ingestion_log(conn)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS housing_connect_listings (
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
    # Safe migration: add lat/lon columns to existing tables that predate this change
    for col in ("latitude", "longitude"):
        try:
            conn.execute(f"ALTER TABLE housing_connect_listings ADD COLUMN {col} DOUBLE")
        except Exception:
            pass  # column already exists

    conn.commit()
    print(f"[db] Database initialized at: {os.path.abspath(db_path)}")
    return conn


def _ensure_ingestion_log(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the ingestion_log table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingestion_log (
            job_name    VARCHAR PRIMARY KEY,
            last_run_at TIMESTAMP,
            status      VARCHAR,
            rows_affected INTEGER,
            error_msg   VARCHAR
        )
    """)
    conn.commit()


def record_ingestion(
    job_name: str,
    status: str,
    rows_affected: int = 0,
    error_msg: str = None,
    db_path: str = DB_PATH,
) -> None:
    """Upsert a row in ingestion_log recording the outcome of a run."""
    conn = get_connection(db_path)
    try:
        _ensure_ingestion_log(conn)
        conn.execute(
            """
            INSERT INTO ingestion_log (job_name, last_run_at, status, rows_affected, error_msg)
            VALUES (?, now(), ?, ?, ?)
            ON CONFLICT (job_name) DO UPDATE SET
                last_run_at   = now(),
                status        = EXCLUDED.status,
                rows_affected = EXCLUDED.rows_affected,
                error_msg     = EXCLUDED.error_msg
            """,
            [job_name, status, rows_affected, error_msg],
        )
        conn.commit()
    finally:
        conn.close()


def get_last_sync(db_path: str = DB_PATH) -> dict:
    """
    Return the most-recent ingestion timestamp across all jobs.
    Returns {'last_run_at': datetime | None, 'status': str | None}.
    """
    conn = get_connection(db_path)
    try:
        _ensure_ingestion_log(conn)
        row = conn.execute(
            "SELECT MAX(last_run_at), MAX(status) FROM ingestion_log"
        ).fetchone()
        return {"last_run_at": row[0] if row else None, "status": row[1] if row else None}
    except Exception:
        return {"last_run_at": None, "status": None}
    finally:
        conn.close()


def get_table_counts(db_path: str = DB_PATH) -> dict:
    """Return row counts for both tables."""
    conn = get_connection(db_path)
    try:
        buildings = conn.execute(
            "SELECT COUNT(*) FROM stabilized_buildings"
        ).fetchone()[0]
        listings = conn.execute(
            "SELECT COUNT(*) FROM housing_connect_listings"
        ).fetchone()[0]
        return {"stabilized_buildings": buildings, "housing_connect_listings": listings}
    finally:
        conn.close()


if __name__ == "__main__":
    conn = init_db()
    counts = get_table_counts()
    print(f"[db] Table counts: {counts}")
    conn.close()
