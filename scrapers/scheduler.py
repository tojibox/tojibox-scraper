"""
scheduler.py
------------
APScheduler cron runner for both scrapers.
Schedules are configurable via env vars REZONING_CRON_SCHEDULE and PARCEL_CRON_SCHEDULE.

Run:
  python -m scrapers.scheduler
"""

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import REZONING_CRON, PARCEL_CRON, PETITION_CRON
from scrapers import parcel_scraper, zoning_scraper, petition_scraper


def run_parcel():
    logger.info("[scheduler] Firing parcel scraper job")
    try:
        parcel_scraper.run()
    except Exception as exc:
        logger.error(f"[scheduler] parcel scraper failed: {exc}")


def run_zoning():
    logger.info("[scheduler] Firing zoning scraper job")
    try:
        zoning_scraper.run()
    except Exception as exc:
        logger.error(f"[scheduler] zoning scraper failed: {exc}")


def run_petition():
    logger.info("[scheduler] Firing petition scraper job")
    try:
        petition_scraper.run()
    except Exception as exc:
        logger.error(f"[scheduler] petition scraper failed: {exc}")


def main():
    scheduler = BlockingScheduler(timezone="America/New_York")

    scheduler.add_job(
        run_zoning,
        CronTrigger.from_crontab(REZONING_CRON),
        id="zoning_scraper",
        name="Raleigh Zoning Scraper",
        misfire_grace_time=300,
        coalesce=True,
    )
    scheduler.add_job(
        run_parcel,
        CronTrigger.from_crontab(PARCEL_CRON),
        id="parcel_scraper",
        name="Wake County Parcel Scraper",
        misfire_grace_time=300,
        coalesce=True,
    )

    scheduler.add_job(
        run_petition,
        CronTrigger.from_crontab(PETITION_CRON),
        id="petition_scraper",
        name="Raleigh Planning Petition Scraper",
        misfire_grace_time=300,
        coalesce=True,
    )

    logger.info(f"[scheduler] Zoning    → {REZONING_CRON}")
    logger.info(f"[scheduler] Parcels   → {PARCEL_CRON}")
    logger.info(f"[scheduler] Petitions → {PETITION_CRON}")
    logger.info("[scheduler] Running — press Ctrl+C to stop")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("[scheduler] Stopped")


if __name__ == "__main__":
    main()
