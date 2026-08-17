"""
Background scheduler for the NYC Housing Aggregator.

Runs nightly ingestion jobs automatically so the database stays current
without any manual user action.  Uses APScheduler's BackgroundScheduler
so it operates in a thread alongside the Streamlit process.

Usage (called from app.py once at startup via st.cache_resource):
    from scheduler import start_scheduler
    start_scheduler()
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from db.schema import DB_PATH, record_ingestion

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def _job_scrape_housing_connect() -> None:
    """Pull active Housing Connect lottery listings via the scraper pipeline."""
    logger.info("[scheduler] Housing Connect scrape job starting")
    try:
        from ingestion.scraper import scrape_housing_connect
        n = scrape_housing_connect(db_path=DB_PATH)
        record_ingestion("housing_connect", status="ok", rows_affected=n)
        logger.info("[scheduler] Housing Connect scrape complete — %d rows", n)
    except Exception as exc:
        logger.warning("[scheduler] Housing Connect scrape failed: %s", exc)
        record_ingestion("housing_connect", status="error", error_msg=str(exc))


def _job_ingest_buildings() -> None:
    """Pull rent-stabilised building records from NYC Open Data."""
    logger.info("[scheduler] Stabilized buildings ingestion job starting")
    try:
        from ingestion.nyc_opendata import ingest_stabilized_buildings
        n = ingest_stabilized_buildings(max_records=50_000, db_path=DB_PATH)
        record_ingestion("stabilized_buildings", status="ok", rows_affected=n)
        logger.info("[scheduler] Buildings ingestion complete — %d rows", n)
    except Exception as exc:
        logger.warning("[scheduler] Buildings ingestion failed: %s", exc)
        record_ingestion("stabilized_buildings", status="error", error_msg=str(exc))


# ---------------------------------------------------------------------------
# Scheduler singleton
# ---------------------------------------------------------------------------

def start_scheduler(
    hour: int = 3,
    minute: int = 0,
    timezone: str = "America/New_York",
) -> BackgroundScheduler:
    """
    Create, configure, and start the background scheduler.

    Jobs run nightly at *hour*:*minute* ET (default 03:00).
    Returns the running scheduler instance.

    Failures in individual jobs are caught and logged; they do not crash
    the scheduler or the Streamlit process.
    """
    scheduler = BackgroundScheduler(timezone=timezone)

    nightly = CronTrigger(hour=hour, minute=minute, timezone=timezone)

    scheduler.add_job(
        _job_scrape_housing_connect,
        trigger=nightly,
        id="housing_connect_nightly",
        name="Housing Connect nightly scrape",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1 h late if the process was asleep
    )

    scheduler.add_job(
        _job_ingest_buildings,
        trigger=nightly,
        id="stabilized_buildings_nightly",
        name="Stabilized buildings nightly ingest",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.start()
    logger.info(
        "[scheduler] Background scheduler started — jobs run nightly at %02d:%02d %s",
        hour, minute, timezone,
    )
    return scheduler
