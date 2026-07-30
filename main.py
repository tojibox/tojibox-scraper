"""
Togibox Scraper API
--------------------
Thin FastAPI wrapper exposing:
  - GET  /health              — liveness check
  - POST /run/parcel          — trigger the Wake County parcel scraper
  - POST /run/zoning          — trigger the Raleigh zoning (ArcGIS) scraper
  - POST /run/petition        — trigger the Raleigh Planning petition scraper (HTML)
  - POST /run/enrich          — trigger spatial PIN enrichment
  - POST /run/all             — trigger parcel + zoning + petition scrapers

Scraper functions (scrapers/*.py) are synchronous/blocking (requests + psycopg2),
so triggers here run them in a background thread via BackgroundTasks + a thread
executor rather than blocking the event loop.

For scheduled/cron execution, run the scheduler as its own process instead:
  python -m scrapers.scheduler

Run this API:
  python main.py
  or: uvicorn main:app --reload --port 8001
"""

import sys
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI
from loguru import logger
from pydantic import BaseModel

from config import API_PORT, LOG_LEVEL
from scrapers import parcel_scraper, petition_scraper, spatial_enrichment, zoning_scraper


def configure_logging():
    Path("logs").mkdir(exist_ok=True)

    logger.remove()
    logger.add(
        sys.stdout,
        level=LOG_LEVEL,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | {message}"
        ),
    )
    logger.add(
        "logs/scraper_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        compression="zip",
    )


configure_logging()

app = FastAPI(
    title="Togibox Scraper API",
    description="Wake County parcel / rezoning petition scraper — manual trigger + health check",
    version="1.0.0",
)


class RunResponse(BaseModel):
    message: str


def _run_safely(name: str, fn):
    try:
        fn()
    except Exception as exc:
        logger.error(f"[api] {name} scraper run failed: {exc}")


def _run_all():
    _run_safely("zoning", zoning_scraper.run)
    _run_safely("parcel", parcel_scraper.run)
    _run_safely("petition", petition_scraper.run)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "togibox-scraper"}


@app.post("/run/parcel", response_model=RunResponse)
async def trigger_parcel(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_safely, "parcel", parcel_scraper.run)
    return RunResponse(message="Parcel scraper triggered")


@app.post("/run/zoning", response_model=RunResponse)
async def trigger_zoning(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_safely, "zoning", zoning_scraper.run)
    return RunResponse(message="Zoning scraper triggered")


@app.post("/run/petition", response_model=RunResponse)
async def trigger_petition(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_safely, "petition", petition_scraper.run)
    return RunResponse(message="Petition scraper triggered")


@app.post("/run/enrich", response_model=RunResponse)
async def trigger_enrich(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_safely, "spatial_enrichment", spatial_enrichment.run)
    return RunResponse(message="Spatial PIN enrichment triggered")


@app.post("/run/all", response_model=RunResponse)
async def trigger_all(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_all)
    return RunResponse(message="Zoning, parcel, and petition scrapers triggered")


if __name__ == "__main__":
    import uvicorn

    logger.info("Togibox Scraper API starting up")
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT, reload=True)
