"""
zoning_scraper.py
-----------------
6-hour scraper for Raleigh zoning districts (~3,562 records).
Source: Raleigh Planning Zoning MapServer (layer 0).
Each unique ZONE_CASE (e.g. "Z-27B-2014") is treated as a rezoning petition.
Detects: new zone cases, changed zone type, changed effective date.

Flow:
  1. Open oracle_run record
  2. Fetch all zoning districts from ArcGIS
  3. Save raw data locally
  4. Group by ZONE_CASE (one petition can cover multiple polygons)
  5. Load stored petition fingerprints from DB
  6. Diff → collect new and changed cases
  7. Upsert rezoning_petitions         (100/batch)
  8. Insert change_events              (100/batch)
  9. Upsert petition_fingerprints      (100/batch)
 10. Close oracle_run record
"""

import json
import time
import uuid
import requests
from datetime import datetime, timezone
from loguru import logger

from config import (
    ARCGIS_ZONING_URL, SUPABASE_BATCH,
    DATA_DIR, COUNTY_ID,
)
from scrapers.db import get_connection, execute_batch, fetch_all_fingerprints
from scrapers.utils import fingerprint, ms_to_date, now_iso, save_raw, chunk

FIELDS = ",".join([
    "GLOBALID", "OBJECTID",
    "ZONE_TYPE", "ZONE_TYPE_DECODE",
    "ZONING", "CONDITIONAL",
    "ZN_CASE_NUM", "ZN_CASE_SUFFIX", "ZN_CASE_YEAR",
    "ZONE_CASE",
    "EFF_DATE",
    "ORDINANCE", "PLAN", "PLAN_NAME",
    "INTO_UDO", "COND_LINK",
])


# ── Fetch ──────────────────────────────────────────────────────────────────────

def _fetch_page(offset: int, limit: int = 1000, retries: int = 3) -> tuple:
    """Fetch one page. Returns (features, exceeded_limit)."""
    params = {
        "where": "1=1",
        "outFields": FIELDS,
        "returnGeometry": "true",
        "f": "geojson",
        "resultOffset": offset,
        "resultRecordCount": limit,
    }
    for attempt in range(retries):
        try:
            r = requests.get(ARCGIS_ZONING_URL, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            return data.get("features", []), data.get("exceededTransferLimit", False)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                logger.warning(f"[zoning:fetch] retry {attempt+1}/{retries} in {wait}s — {exc}")
                time.sleep(wait)
            else:
                raise


def _fetch_all() -> list:
    """Paginate through all zoning districts."""
    features, offset, page_size = [], 0, 1000
    while True:
        page, exceeded = _fetch_page(offset, page_size)
        if not page:
            break
        features.extend(page)
        logger.info(f"[zoning:fetch] offset={offset} → {len(page)} features (total so far: {len(features)})")
        offset += len(page)
        if not exceeded:
            break
        time.sleep(0.1)
    return features


# ── Transform & group ─────────────────────────────────────────────────────────

def _group_by_case(features: list) -> dict:
    """
    Group polygon features by ZONE_CASE.
    Each unique ZONE_CASE = one rezoning petition row.
    Multiple polygons can share a case (e.g. a rezoning covering several areas).
    """
    cases = {}
    for feat in features:
        p   = feat.get("properties") or {}
        geo = feat.get("geometry")

        case = (p.get("ZONE_CASE") or "").strip()
        if not case:
            continue

        if case not in cases:
            cases[case] = {
                "petition_number":  case,
                "county_id":        COUNTY_ID,
                "current_zoning":   p.get("ZONE_TYPE"),
                "proposed_zoning":  p.get("ZONING"),
                "zone_type_decode": p.get("ZONE_TYPE_DECODE"),
                "action":           "Conditional" if p.get("CONDITIONAL") else "Standard",
                "status":           "Approved",    # all records in this layer are effective
                "vote_result":      "Approved",
                "meeting_date":     ms_to_date(p.get("EFF_DATE")),
                "meeting_type":     "Zoning Effective Date",
                "legislation_url":  p.get("COND_LINK"),
                "file_number":      p.get("ORDINANCE"),
                "raw_properties":   json.dumps({
                    **p,
                    "EFF_DATE": ms_to_date(p.get("EFF_DATE")),
                }),
                "polygons":         [],
                "last_scraped_at":  now_iso(),
                "updated_at":       now_iso(),
            }

        if geo:
            cases[case]["polygons"].append(geo)

    # Flatten polygon list into a single JSONB geometry (MultiPolygon if many)
    for case_data in cases.values():
        polys = case_data.pop("polygons", [])
        if len(polys) == 1:
            case_data["geometry"] = json.dumps(polys[0])
        elif len(polys) > 1:
            case_data["geometry"] = json.dumps({
                "type": "MultiPolygon",
                "coordinates": [p["coordinates"] for p in polys if p.get("coordinates")],
            })
        else:
            case_data["geometry"] = None

    return cases


def _make_fingerprint(case_data: dict) -> str:
    return fingerprint(
        case_data["petition_number"],
        case_data.get("current_zoning"),
        case_data.get("proposed_zoning"),
        case_data.get("meeting_date"),
        case_data.get("action"),
    )


# ── SQL templates ──────────────────────────────────────────────────────────────

UPSERT_PETITION_SQL = """
INSERT INTO rezoning_petitions (
    petition_number, county_id,
    current_zoning, proposed_zoning, action, status, vote_result,
    meeting_date, meeting_type, legislation_url, file_number,
    geometry, raw_properties, last_scraped_at, updated_at
) VALUES (
    %(petition_number)s, %(county_id)s,
    %(current_zoning)s, %(proposed_zoning)s, %(action)s, %(status)s, %(vote_result)s,
    %(meeting_date)s, %(meeting_type)s, %(legislation_url)s, %(file_number)s,
    %(geometry)s, %(raw_properties)s, %(last_scraped_at)s, %(updated_at)s
)
ON CONFLICT (petition_number, county_id) DO UPDATE SET
    current_zoning   = EXCLUDED.current_zoning,
    proposed_zoning  = EXCLUDED.proposed_zoning,
    action           = EXCLUDED.action,
    status           = EXCLUDED.status,
    vote_result      = EXCLUDED.vote_result,
    meeting_date     = EXCLUDED.meeting_date,
    meeting_type     = EXCLUDED.meeting_type,
    legislation_url  = EXCLUDED.legislation_url,
    file_number      = EXCLUDED.file_number,
    geometry         = EXCLUDED.geometry,
    raw_properties   = EXCLUDED.raw_properties,
    last_scraped_at  = EXCLUDED.last_scraped_at,
    updated_at       = EXCLUDED.updated_at
"""

UPSERT_FP_SQL = """
INSERT INTO petition_fingerprints (petition_number, county_id, fingerprint_hash, last_checked_at)
VALUES (%(petition_number)s, %(county_id)s, %(fingerprint_hash)s, %(last_checked_at)s)
ON CONFLICT (petition_number) DO UPDATE SET
    fingerprint_hash = EXCLUDED.fingerprint_hash,
    last_checked_at  = EXCLUDED.last_checked_at
"""

INSERT_CHANGE_EVENT_SQL = """
INSERT INTO change_events (
    id, event_type, county_id, petition_number,
    changed_fields, before_state, after_state, detected_at
) VALUES (
    %(id)s, %(event_type)s, %(county_id)s, %(petition_number)s,
    %(changed_fields)s, %(before_state)s, %(after_state)s, %(detected_at)s
)
"""


# ── Oracle run helpers ─────────────────────────────────────────────────────────

def _open_run(conn) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oracle_runs (id, run_type, status, started_at) VALUES (%s, 'rezoning', 'running', NOW())",
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
    logger.info("[zoning] START — Raleigh zoning scraper")
    logger.info("=" * 70)

    conn = get_connection()
    run_id = _open_run(conn)
    logger.info(f"[zoning] oracle_run id={run_id}")

    total_fetched = 0
    total_changed = 0
    total_skipped = 0

    try:
        # ── Step 1: Fetch all zoning districts ────────────────────────────────
        features = _fetch_all()
        total_fetched = len(features)
        logger.info(f"[zoning] Fetched {total_fetched} features from ArcGIS")

        # ── Step 2: Save raw data locally ─────────────────────────────────────
        raw_payload = [
            {"properties": f.get("properties"), "geometry_type": (f.get("geometry") or {}).get("type")}
            for f in features
        ]
        save_raw(raw_payload, "zoning", "zoning", DATA_DIR)

        # ── Step 3: Group by ZONE_CASE ────────────────────────────────────────
        cases = _group_by_case(features)
        logger.info(f"[zoning] Grouped into {len(cases)} unique zone cases")

        skipped = total_fetched - len(cases)   # features with no ZONE_CASE
        total_skipped = skipped
        if skipped:
            logger.info(f"[zoning] Skipped {skipped} features (no ZONE_CASE)")

        # ── Step 4: Load stored fingerprints ──────────────────────────────────
        stored_fps = fetch_all_fingerprints(conn, "petition_fingerprints", "petition_number")
        logger.info(f"[zoning] Loaded {len(stored_fps)} stored fingerprints")

        # ── Step 5: Diff ──────────────────────────────────────────────────────
        changed_cases  = []
        new_fp_rows    = []
        event_rows     = []

        for case_num, case_data in cases.items():
            new_fp = _make_fingerprint(case_data)
            old_fp = stored_fps.get(case_num)

            if new_fp == old_fp:
                continue    # no change

            total_changed += 1
            changed_cases.append(case_data)

            new_fp_rows.append({
                "petition_number": case_num,
                "county_id":       COUNTY_ID,
                "fingerprint_hash": new_fp,
                "last_checked_at": now_iso(),
            })

            event_type = "new_petition" if old_fp is None else "petition_status_change"
            event_rows.append({
                "id":               str(uuid.uuid4()),
                "event_type":       event_type,
                "county_id":        COUNTY_ID,
                "petition_number":  case_num,
                "changed_fields":   ["current_zoning", "proposed_zoning", "meeting_date", "action"],
                "before_state":     None,
                "after_state":      json.dumps({
                    "petition_number": case_num,
                    "current_zoning":  case_data.get("current_zoning"),
                    "proposed_zoning": case_data.get("proposed_zoning"),
                    "status":          case_data.get("status"),
                    "vote_result":     case_data.get("vote_result"),
                    "meeting_date":    case_data.get("meeting_date"),
                    "action":          case_data.get("action"),
                }),
                "detected_at": now_iso(),
            })

        logger.info(f"[zoning] Diff complete — {total_changed} changed, {len(stored_fps) - total_changed} unchanged")

        # ── Step 6: Upsert rezoning_petitions (100/batch) ─────────────────────
        if changed_cases:
            for i, batch in enumerate(chunk(changed_cases, SUPABASE_BATCH)):
                execute_batch(conn, UPSERT_PETITION_SQL, batch)
                logger.info(f"[zoning] Upserted petitions batch {i+1} ({len(batch)} rows)")

        # ── Step 7: Insert change_events (100/batch) ──────────────────────────
        if event_rows:
            for i, batch in enumerate(chunk(event_rows, SUPABASE_BATCH)):
                execute_batch(conn, INSERT_CHANGE_EVENT_SQL, batch)
                logger.info(f"[zoning] Inserted change_events batch {i+1} ({len(batch)} rows)")

        # ── Step 8: Upsert petition_fingerprints (100/batch) ──────────────────
        if new_fp_rows:
            for i, batch in enumerate(chunk(new_fp_rows, SUPABASE_BATCH)):
                execute_batch(conn, UPSERT_FP_SQL, batch)
                logger.info(f"[zoning] Upserted fingerprints batch {i+1} ({len(batch)} rows)")

        # ── Step 9: Close run ─────────────────────────────────────────────────
        _close_run(conn, run_id, "completed", total_fetched, total_changed, total_skipped)

        logger.info("=" * 70)
        logger.info("[zoning] DONE")
        logger.info(f"[zoning]   features fetched : {total_fetched}")
        logger.info(f"[zoning]   unique cases     : {len(cases)}")
        logger.info(f"[zoning]   changed/new      : {total_changed}")
        logger.info(f"[zoning]   skipped          : {total_skipped}")
        logger.info("=" * 70)

    except Exception as exc:
        logger.exception(f"[zoning] FAILED — {exc}")
        _close_run(conn, run_id, "failed", total_fetched, total_changed, total_skipped, str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
