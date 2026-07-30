"""
spatial_enrichment.py
---------------------
One-time (and periodic) script that populates rezoning_petitions.pins[]
by spatially intersecting each petition polygon against parcel geometries.

Why this is needed:
  - The ArcGIS zoning layer gives us petition polygons (the rezoned area boundary)
  - The parcels table has individual parcel polygons
  - A spatial intersection tells us which PINs fall inside each petition boundary
  - Once pins[] is populated, any API query "show history for PIN X" just does:
      SELECT * FROM rezoning_petitions WHERE 'X' = ANY(pins)

Strategy:
  1. Load all parcels that have geometry from the DB (~434k rows)
     - Only load pin + geometry (skip heavy fields)
  2. Build a Shapely STRtree spatial index over parcel geometries
  3. For each petition with geometry but empty pins[], query the tree
     - Candidate parcels whose bounding boxes intersect the petition polygon
     - Verify with actual Shapely intersection
  4. Batch-update rezoning_petitions.pins[]

Scale notes:
  - 434k parcel polygons loaded once into memory (~500MB)
  - 1,572 petition polygons to enrich
  - STRtree query: O(log n) per petition → fast
  - Runtime: ~5-15 min depending on polygon complexity
"""

import json
import time
from loguru import logger
from shapely.geometry import shape, mapping
from shapely.strtree import STRtree

from scrapers.db import get_connection
from scrapers.utils import chunk

BATCH_SIZE = 50   # petition batches for DB updates


def _load_parcel_geometries(conn) -> tuple:
    """
    Load all parcels with geometry from DB.
    Returns (pins list, shapely shapes list) — parallel arrays.
    """
    logger.info("[enrich] Loading parcel geometries from DB …")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pin, geometry
            FROM parcels
            WHERE geometry IS NOT NULL
        """)
        rows = cur.fetchall()

    pins   = []
    shapes = []
    errors = 0

    for pin, geo in rows:
        try:
            geo_dict = geo if isinstance(geo, dict) else json.loads(geo)
            s = shape(geo_dict)
            if s.is_valid and not s.is_empty:
                pins.append(pin)
                shapes.append(s)
        except Exception:
            errors += 1

    logger.info(f"[enrich] Loaded {len(pins):,} valid parcel geometries ({errors} errors)")
    return pins, shapes


def _load_petition_geometries(conn) -> list:
    """
    Load all petitions that have geometry but empty/null pins[].
    Returns list of (petition_number, shapely_shape).
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT petition_number, geometry
            FROM rezoning_petitions
            WHERE geometry IS NOT NULL
              AND (pins IS NULL OR array_length(pins, 1) IS NULL)
        """)
        rows = cur.fetchall()

    result = []
    errors = 0
    for petition_number, geo in rows:
        try:
            geo_dict = geo if isinstance(geo, dict) else json.loads(geo)
            s = shape(geo_dict)
            if s.is_valid and not s.is_empty:
                result.append((petition_number, s))
        except Exception:
            errors += 1

    logger.info(f"[enrich] {len(result)} petitions need PIN enrichment ({errors} bad geometries)")
    return result


def _update_pins(conn, updates: list):
    """
    Batch update pins[] for a list of (petition_number, pins_list) tuples.
    """
    with conn.cursor() as cur:
        for petition_number, pins in updates:
            cur.execute(
                "UPDATE rezoning_petitions SET pins = %s, updated_at = NOW() WHERE petition_number = %s",
                (pins, petition_number),
            )
    conn.commit()


def run():
    logger.info("=" * 70)
    logger.info("[enrich] START — spatial PIN enrichment for rezoning petitions")
    logger.info("=" * 70)

    conn = get_connection()

    try:
        # ── Step 1: Load all parcel geometries into memory ────────────────────
        parcel_pins, parcel_shapes = _load_parcel_geometries(conn)
        if not parcel_pins:
            logger.warning("[enrich] No parcel geometries found — aborting")
            return

        # ── Step 2: Build spatial index ───────────────────────────────────────
        logger.info("[enrich] Building STRtree spatial index …")
        t0 = time.time()
        tree = STRtree(parcel_shapes)
        logger.info(f"[enrich] Index built in {time.time()-t0:.1f}s")

        # ── Step 3: Load petition geometries needing enrichment ───────────────
        petitions = _load_petition_geometries(conn)
        if not petitions:
            logger.info("[enrich] All petitions already have pins — nothing to do")
            return

        # ── Step 4: Intersect petition polygons with parcel tree ──────────────
        enriched = 0
        empty    = 0
        updates  = []

        for i, (petition_number, petition_geom) in enumerate(petitions):
            # Query index for candidate parcels (bounding box overlap)
            candidate_idxs = tree.query(petition_geom)

            # Verify actual intersection
            found_pins = []
            for idx in candidate_idxs:
                try:
                    if parcel_shapes[idx].intersects(petition_geom):
                        found_pins.append(parcel_pins[idx])
                except Exception:
                    pass

            updates.append((petition_number, found_pins))

            if found_pins:
                enriched += 1
            else:
                empty += 1

            if (i + 1) % 100 == 0:
                logger.info(f"[enrich] Processed {i+1}/{len(petitions)} — {enriched} enriched so far")

        # ── Step 5: Batch write to DB ─────────────────────────────────────────
        logger.info(f"[enrich] Writing {len(updates)} updates to DB …")
        for batch in chunk(updates, BATCH_SIZE):
            _update_pins(conn, batch)

        logger.info("=" * 70)
        logger.info("[enrich] DONE")
        logger.info(f"[enrich]   petitions processed : {len(petitions)}")
        logger.info(f"[enrich]   with pins found     : {enriched}")
        logger.info(f"[enrich]   no intersection     : {empty}")
        logger.info("=" * 70)

    finally:
        conn.close()


if __name__ == "__main__":
    run()
