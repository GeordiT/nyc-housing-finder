"""
FastAPI wrapper around the search query layer.

Exposes a single GET /search endpoint that accepts the four filter params
and returns both stabilized buildings and lottery listings as JSON.

Run locally:
    uvicorn backend.api:app --port 8000 --reload

Or from the project root:
    python -m backend.api
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from backend.search import search as _search
from db.schema import DB_PATH

logger = logging.getLogger(__name__)

app = FastAPI(
    title="NYC Housing Aggregator API",
    description=(
        "Query rent-stabilized buildings and active Housing Connect lottery listings "
        "by borough, ZIP code, rent ceiling, and household income."
    ),
    version="1.0.0",
)

# Allow the Streamlit frontend (same host, different port) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _df_to_records(df) -> list[dict[str, Any]]:
    """Convert a pandas DataFrame to a JSON-serialisable list of dicts."""
    if df is None or df.empty:
        return []
    # Replace NaN/NaT with None so JSON serialisation doesn't emit 'NaN'
    return df.where(df.notna(), other=None).to_dict(orient="records")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/search", summary="Search stabilized buildings and lottery listings")
def search(
    borough: Optional[str] = Query(
        default=None,
        description="Borough name: Manhattan, Brooklyn, Bronx, Queens, or Staten Island.",
        examples=["Brooklyn"],
    ),
    zip_code: Optional[str] = Query(
        default=None,
        description="5-digit ZIP code.",
        examples=["11201"],
    ),
    max_rent: Optional[float] = Query(
        default=None,
        description="Maximum monthly rent ceiling in dollars.",
        ge=0,
        examples=[2000.0],
    ),
    annual_income: Optional[float] = Query(
        default=None,
        description="Household gross annual income in dollars for AMI matching.",
        ge=0,
        examples=[60000.0],
    ),
    db_path: str = Query(
        default=DB_PATH,
        include_in_schema=False,
    ),
) -> dict[str, Any]:
    """
    Return matching rent-stabilized buildings and active Housing Connect
    lottery listings.  All parameters are optional; omitting all returns
    up to the configured row limits for each table.

    **AMI matching**: a lottery listing is included when `annual_income` falls
    within `[min_income, max_income]`.  A null bound is treated as unbounded
    (i.e. `min_income IS NULL` means no income floor).
    """
    try:
        buildings_df, listings_df = _search(
            borough=borough or None,
            zip_code=zip_code or None,
            max_rent=max_rent,
            annual_income=annual_income,
            db_path=db_path,
        )
        return {
            "ok": True,
            "filters": {
                "borough": borough,
                "zip_code": zip_code,
                "max_rent": max_rent,
                "annual_income": annual_income,
            },
            "counts": {
                "stabilized_buildings": len(buildings_df),
                "lottery_listings": len(listings_df),
            },
            "stabilized_buildings": _df_to_records(buildings_df),
            "lottery_listings": _df_to_records(listings_df),
        }
    except Exception as exc:
        logger.error("/search error: %s", exc, exc_info=True)
        return {
            "ok": False,
            "error": str(exc),
            "stabilized_buildings": [],
            "lottery_listings": [],
        }


@app.get("/health", summary="Health check")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.api:app", host="0.0.0.0", port=port, reload=False)
