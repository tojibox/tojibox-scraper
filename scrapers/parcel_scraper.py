"""
parcel_scraper.py
-----------------
Daily scraper for Wake County ArcGIS parcel records (~434k).
Detects changes in: owner, assessed value, land class, type/use.

Flow:
  1. Open oracle_run record
  2. Fetch all pages from ArcGIS (2000/page)
  3. Save raw GeoJSON locally
  4. Load stored fingerprints from DB
  5. Diff → collect new and changed records
  6. Upsert changed parcels  → parcels table          (100/batch)
  7. Insert change events    → change_events table     (100/batch)
  8. Upsert fingerprints     → parcel_fingerprints     (100/batch)
  9. Close oracle_run record with stats
"""

import json
import time
import uuid
import requests
from datetime import datetime, timezone
from loguru import logger

from config import (
    ARCGIS_PARCEL_URL, ARCGIS_PAGE_SIZE, SUPABASE_BATCH,
    DATA_DIR, COUNTY_ID,
)
from scrapers.db import get_connection, execute_batch, fetch_all_fingerprints
from scrapers.utils import fingerprint, ms_to_date, ms_to_datetime, now_iso, save_raw, chunk

# ── ArcGIS field selection ─────────────────────────────────────────────────────
FIELDS = ",".join([
    "PIN_NUM", "REID", "OWNER",
    "SITE_ADDRESS", "CITY", "ZIPNUM",
    "TOTAL_VALUE_ASSD", "LAND_VAL", "BLDG_VAL", "TOTSALPRICE",
    "HEATEDAREA", "CALC_AREA", "YEAR_BUILT", "UNITS",
    "TYPE_AND_USE", "TYPE_USE_DECODE",
    "LAND_CLASS", "LAND_CLASS_DECODE",
    "DESIGN_STYLE_DECODE", "PLANNING_JURISDICTION",
    "DEED_DATE", "SALE_DATE",
])


# ── Fetch ──────────────────────────────────────────────────────────────────────

def _fetch_page(offset: int, retries: int = 3) -> list:
    params = {
        "where": "1=1",
        "outFields": FIELDS,
        "returnGeometry": "true",
        "f": "geojson",
        "resultOffset": offset,
        "resultRecordCount": ARCGIS_PAGE_SIZE,
    }
    for attempt in range(retries):
        try:
            r = requests.get(ARCGIS_PARCEL_URL, params=params, timeout=60)
            r.raise_for_status()
            return r.json().get("features", [])
        except Exception as exc:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                logger.warning(f"[parcel:fetch] retry {attempt+1}/{retries} in {wait}s — {exc}")
                time.sleep(wait)
            else:
                raise


def fetch_all_parcels() -> list:
    """Pages through ArcGIS until no more records. Returns raw feature list."""
    features, offset = [], 0
    while True:
        logger.info(f"[parcel:fetch] offset={offset:,} …")
        page = _fetch_page(offset)
        if not page:
            break
        features.extend(page)
        offset += len(page)
        logger.info(f"[parcel:fetch] got {len(page)} → total {len(features):,}")
        if len(page) < ARCGIS_PAGE_SIZE:
            break
        time.sleep(0.1)          # polite pause between pages
    return features


# ── Transform ─────────────────────────────────────────────────────────────────

def _transform(feature: dict):
    """ArcGIS GeoJSON feature → parcels table row dict. Returns None if no PIN."""
    p   = feature.get("properties") or {}
    geo = feature.get("geometry")

    pin = (p.get("PIN_NUM") or "").strip()
    if not pin:
        return None

    return {
        "pin":              pin,
        "reid":             p.get("REID"),
        "county_id":        COUNTY_ID,
        "site_address":     p.get("SITE_ADDRESS"),
        "city":             p.get("CITY"),
        "zipcode":          str(p["ZIPNUM"]) if p.get("ZIPNUM") else None,
        "owner":            p.get("OWNER"),
        "total_value_assd": p.get("TOTAL_VALUE_ASSD"),
        "land_val":         p.get("LAND_VAL"),
        "bldg_val":         p.get("BLDG_VAL"),
        "sale_price":       p.get("TOTSALPRICE"),
        "heated_area":      p.get("HEATEDAREA"),
        "calc_area":        p.get("CALC_AREA"),
        "year_built":       int(p["YEAR_BUILT"]) if p.get("YEAR_BUILT") else None,
        "units":            p.get("UNITS"),
        "type_and_use":     p.get("TYPE_AND_USE"),
        "land_class":       p.get("LAND_CLASS"),
        "design_style":     p.get("DESIGN_STYLE_DECODE"),
        "geometry":         json.dumps(geo) if geo else None,
        "raw_properties":   json.dumps({
            **p,
            "DEED_DATE": ms_to_date(p.get("DEED_DATE")),
            "SALE_DATE": ms_to_date(p.get("SALE_DATE")),
        }),
        "last_scraped_at":  now_iso(),
        "updated_at":       now_iso(),
    }


def _make_fingerprint(row: dict) -> str:
    return fingerprint(
        row["pin"],
        row.get("owner"),
        row.get("total_value_assd"),
        row.get("land_class"),
        row.get("type_and_use"),
    )


# ── Change detection ───────────────────────────────────────────────────────────

def _detect_changed_fields(old_fp, new_fp: str, row: dict):
    """Returns list of field names we flag as changed (for logging/events)."""
    if old_fp is None:
        return ["new_parcel"]
    # Simplified — we know something changed because the hash differs.
    # We store 'before' state later from the existing DB row.
    return ["owner_or_value_or_class"]


def _classify_event(changed_fields: list[str]) -> str:
    if "new_parcel" in changed_fields:
        return "new_parcel"
    return "parcel_owner_change"     # broad catch-all for any parcel mutation


# ── SQL templates ──────────────────────────────────────────────────────────────

UPSERT_PARCEL_SQL = """
INSERT INTO parcels (
    pin, reid, county_id, site_address, city, zipcode,
    owner, total_value_assd, land_val, bldg_val, sale_price,
    heated_area, calc_area, year_built, units,
    type_and_use, land_class, design_style,
    geometry, raw_properties,
    last_scraped_at, updated_at
) VALUES (
    %(pin)s, %(reid)s, %(county_id)s, %(site_address)s, %(city)s, %(zipcode)s,
    %(owner)s, %(total_value_assd)s, %(land_val)s, %(bldg_val)s, %(sale_price)s,
    %(heated_area)s, %(calc_area)s, %(year_built)s, %(units)s,
    %(type_and_use)s, %(land_class)s, %(design_style)s,
    %(geometry)s, %(raw_properties)s,
    %(last_scraped_at)s, %(updated_at)s
)
ON CONFLICT (pin, county_id) DO UPDATE SET
    reid             = EXCLUDED.reid,
    site_address     = EXCLUDED.site_address,
    city             = EXCLUDED.city,
    zipcode          = EXCLUDED.zipcode,
    owner            = EXCLUDED.owner,
    total_value_assd = EXCLUDED.total_value_assd,
    land_val         = EXCLUDED.land_val,
    bldg_val         = EXCLUDED.bldg_val,
    sale_price       = EXCLUDED.sale_price,
    heated_area      = EXCLUDED.heated_area,
    calc_area        = EXCLUDED.calc_area,
    year_built       = EXCLUDED.year_built,
    units            = EXCLUDED.units,
    type_and_use     = EXCLUDED.type_and_use,
    land_class       = EXCLUDED.land_class,
    design_style     = EXCLUDED.design_style,
    geometry         = EXCLUDED.geometry,
    raw_properties   = EXCLUDED.raw_properties,
    last_scraped_at  = EXCLUDED.last_scraped_at,
    updated_at       = EXCLUDED.updated_at
"""

UPSERT_FINGERPRINT_SQL = """
INSERT INTO parcel_fingerprints (pin, county_id, fingerprint_hash, last_checked_at)
VALUES (%(pin)s, %(county_id)s, %(fingerprint_hash)s, %(last_checked_at)s)
ON CONFLICT (pin) DO UPDATE SET
    fingerprint_hash = EXCLUDED.fingerprint_hash,
    last_checked_at  = EXCLUDED.last_checked_at
"""

INSERT_CHANGE_EVENT_SQL = """
INSERT INTO change_events (
    id, event_type, county_id, pin,
    changed_fields, before_state, after_state, detected_at
) VALUES (
    %(id)s, %(event_type)s, %(county_id)s, %(pin)s,
    %(changed_fields)s, %(before_state)s, %(after_state)s, %(detected_at)s
)
"""


# ── Oracle run helpers ─────────────────────────────────────────────────────────

def _open_run(conn) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oracle_runs (id, run_type, status, started_at) VALUES (%s, 'parcel', 'running', NOW())",
            (run_id,),
        )
    conn.commit()
    return run_id


def _close_run(conn, run_id: str, status: str, fetched: int, changed: int, skipped: int, error: str = None):
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE oracle_runs SET
                status = %s, records_fetched = %s, changes_detected = %s,
                records_skipped = %s, error_message = %s, completed_at = NOW()
               WHERE id = %s""",
            (status, fetched, changed, skipped, error, run_id),
        )
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    logger.info("=" * 70)
    logger.info("[parcel] START — Wake County parcel scraper")
    logger.info("=" * 70)

    conn = get_connection()
    run_id = _open_run(conn)
    logger.info(f"[parcel] oracle_run id={run_id}")

    total_fetched  = 0
    total_changed  = 0
    total_skipped  = 0

    try:
        # ── Step 1: Fetch all parcels from ArcGIS ─────────────────────────────
        features = fetch_all_parcels()
        total_fetched = len(features)
        logger.info(f"[parcel] Fetched {total_fetched:,} features from ArcGIS")

        # ── Step 2: Save raw data locally ─────────────────────────────────────
        raw_payload = [
            {"properties": f.get("properties"), "geometry_type": (f.get("geometry") or {}).get("type")}
            for f in features
        ]
        save_raw(raw_payload, "parcels", "parcels", DATA_DIR)

        # ── Step 3: Transform ─────────────────────────────────────────────────
        rows = []
        for feat in features:
            row = _transform(feat)
            if row is None:
                total_skipped += 1
            else:
                rows.append(row)
        logger.info(f"[parcel] Transformed {len(rows):,} rows ({total_skipped} skipped — no PIN)")

        # ── Step 4: Load stored fingerprints ──────────────────────────────────
        stored_fps = fetch_all_fingerprints(conn, "parcel_fingerprints", "pin")
        logger.info(f"[parcel] Loaded {len(stored_fps):,} stored fingerprints")

        # ── Step 5: Diff ──────────────────────────────────────────────────────
        changed_rows  = []
        new_fp_rows   = []
        event_rows    = []

        for row in rows:
            pin    = row["pin"]
            new_fp = _make_fingerprint(row)
            old_fp = stored_fps.get(pin)

            if new_fp == old_fp:
                continue   # no change

            total_changed += 1

            # Parcel upsert row
            changed_rows.append(row)

            # Fingerprint update
            new_fp_rows.append({
                "pin":              pin,
                "county_id":        COUNTY_ID,
                "fingerprint_hash": new_fp,
                "last_checked_at":  now_iso(),
            })

            # Change event
            event_type = "new_parcel" if old_fp is None else "parcel_owner_change"
            event_rows.append({
                "id":             str(uuid.uuid4()),
                "event_type":     event_type,
                "county_id":      COUNTY_ID,
                "pin":            pin,
                "changed_fields": ["owner", "total_value_assd", "land_class", "type_and_use"],
                "before_state":   None,     # pipeline will backfill from DB snapshot
                "after_state":    json.dumps({
                    "owner":            row.get("owner"),
                    "total_value_assd": row.get("total_value_assd"),
                    "land_class":       row.get("land_class"),
                    "type_and_use":     row.get("type_and_use"),
                    "site_address":     row.get("site_address"),
                }),
                "detected_at": now_iso(),
            })

        logger.info(f"[parcel] Diff complete — {total_changed:,} changed, {len(stored_fps) - total_changed:,} unchanged")

        # ── Step 6: Upsert changed parcels (100/batch) ────────────────────────
        if changed_rows:
            for i, batch in enumerate(chunk(changed_rows, SUPABASE_BATCH)):
                execute_batch(conn, UPSERT_PARCEL_SQL, batch)
                logger.info(f"[parcel] Upserted parcels batch {i+1} ({len(batch)} rows)")

        # ── Step 7: Insert change events (100/batch) ──────────────────────────
        if event_rows:
            for i, batch in enumerate(chunk(event_rows, SUPABASE_BATCH)):
                execute_batch(conn, INSERT_CHANGE_EVENT_SQL, batch)
                logger.info(f"[parcel] Inserted change_events batch {i+1} ({len(batch)} rows)")

        # ── Step 8: Upsert fingerprints (100/batch) ───────────────────────────
        if new_fp_rows:
            for i, batch in enumerate(chunk(new_fp_rows, SUPABASE_BATCH)):
                execute_batch(conn, UPSERT_FINGERPRINT_SQL, batch)
                logger.info(f"[parcel] Upserted fingerprints batch {i+1} ({len(batch)} rows)")

        # ── Step 9: Close run ─────────────────────────────────────────────────
        _close_run(conn, run_id, "completed", total_fetched, total_changed, total_skipped)

        logger.info("=" * 70)
        logger.info(f"[parcel] DONE")
        logger.info(f"[parcel]   fetched  : {total_fetched:,}")
        logger.info(f"[parcel]   changed  : {total_changed:,}")
        logger.info(f"[parcel]   skipped  : {total_skipped}")
        logger.info("=" * 70)

    except Exception as exc:
        logger.exception(f"[parcel] FAILED — {exc}")
        _close_run(conn, run_id, "failed", total_fetched, total_changed, total_skipped, str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
