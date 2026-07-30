"""
Togibox scraper configuration.
All values come from environment variables; hardcoded defaults are safe for local dev only.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Database (direct psycopg2 — avoids Supabase REST 1k row limit) ────────────
DB_HOST     = os.getenv("DB_HOST", "")
DB_PORT     = int(os.getenv("DB_PORT", "5432"))
DB_USER     = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME     = os.getenv("DB_NAME", "postgres")

# ── County ────────────────────────────────────────────────────────────────────
COUNTY_ID   = os.getenv("COUNTY_ID", "raleigh_nc")

# ── ArcGIS endpoints ──────────────────────────────────────────────────────────
ARCGIS_PARCEL_URL = os.getenv(
    "ARCGIS_PARCELS_URL",
    "https://maps.wakegov.com/arcgis/rest/services/Property/Parcels/MapServer/0/query",
)
ARCGIS_ZONING_URL = os.getenv(
    "ARCGIS_ZONING_URL",
    "https://maps.raleighnc.gov/arcgis/rest/services/Planning/Zoning/MapServer/0/query",
)

# ── Batch sizes ───────────────────────────────────────────────────────────────
ARCGIS_PAGE_SIZE   = int(os.getenv("ARCGIS_PAGE_SIZE", "2000"))   # ArcGIS max
SUPABASE_BATCH     = int(os.getenv("SUPABASE_BATCH", "100"))       # rows per INSERT

# ── Local raw data storage ────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data" / "raw"

# ── Cron schedules ────────────────────────────────────────────────────────────
REZONING_CRON  = os.getenv("REZONING_CRON_SCHEDULE",  "0 */6 * * *")
PARCEL_CRON    = os.getenv("PARCEL_CRON_SCHEDULE",     "0 2 * * *")
PETITION_CRON  = os.getenv("PETITION_CRON_SCHEDULE",   "0 */4 * * *")

# ── API ───────────────────────────────────────────────────────────────────────
API_PORT = int(os.getenv("API_PORT", "8001"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
