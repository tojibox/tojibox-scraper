"""
petition_scraper.py
-------------------
Scrapes Raleigh Planning HTML pages for rezoning petition data.

Sources:
  - Active cases:   raleighnc.gov/planning/services/rezoning-process/rezoning-cases
  - Finalized cases: raleighnc.gov/planning/services/finalized-rezoning-cases

Provides data the ArcGIS Zoning MapServer does NOT have:
  - address      : location text from active cases table
  - pins[]       : PIN numbers extracted from iMAPS map links
  - status       : pending / approved / denied / withdrawn
  - proposed_zoning / current_zoning (from "Change Requested" text)
  - petition_number in normalized form

Normalization:
  HTML "Z-09-25"    → canonical "Z-9-2025"
  ArcGIS "Z-9-2024" → canonical "Z-9-2024"  (already matches)

Flow:
  1. Open oracle_run record
  2. Scrape active cases page   → 30-60 pending petitions
  3. Scrape finalized cases page → 1000+ historical petitions
  4. Normalize + deduplicate
  5. Load stored fingerprints
  6. Diff → changed/new petitions
  7. Upsert rezoning_petitions   (100/batch)
  8. Insert change_events        (100/batch)
  9. Upsert petition_fingerprints (100/batch)
 10. Close oracle_run record
"""

import json
import re
import time
import uuid
import html as html_module
import requests
from datetime import datetime, timezone
from loguru import logger

from config import SUPABASE_BATCH, DATA_DIR, COUNTY_ID
from scrapers.db import get_connection, execute_batch, fetch_all_fingerprints
from scrapers.utils import fingerprint, now_iso, save_raw, chunk

# ── Source URLs ────────────────────────────────────────────────────────────────
ACTIVE_CASES_URL    = "https://raleighnc.gov/planning/services/rezoning-process/rezoning-cases"
FINALIZED_CASES_URL = "https://raleighnc.gov/planning/services/finalized-rezoning-cases"

# Match any petition-type case number prefix
CASE_RE = re.compile(r'^(Z|TCZ|AX|CP|TA|TC)-', re.I)


# ── Petition number normalization ──────────────────────────────────────────────

def normalize_petition_number(raw: str) -> str:
    """
    Converts any format to canonical: PREFIX-NUM[SUFFIX]-FULLYEAR
      "Z-09-25"    → "Z-9-2025"
      "Z-22-24"    → "Z-22-2024"
      "Z-22B-2014" → "Z-22B-2014"  (already canonical)
      "TCZ-32-25"  → "TCZ-32-2025"
    """
    if not raw:
        return raw
    raw = raw.strip()
    m = re.match(r'^([A-Z]+)-(\d+)([A-Z]?)-(\d{2,4})$', raw.upper())
    if not m:
        return raw
    prefix, num_str, suffix, year_str = m.groups()
    num  = int(num_str)
    year = int(year_str)
    if year < 100:
        year += 2000
    return f"{prefix}-{num}{suffix}-{year}"


def parse_case_number_from_status(cell_text: str) -> str:
    """
    Active cases table merges case number + status in one cell:
    "Z-22-24 Second Neighborhood Meeting Required" → "Z-22-24"
    """
    m = re.match(r'^([A-Z]+-\d+[A-Z]?-\d{2,4})', cell_text.strip(), re.I)
    return m.group(1) if m else ""


def parse_status_from_cell(cell_text: str) -> str:
    """
    Extract status keyword from the combined case/status cell.
    "Z-15-25 City Council Public Hearing 05/19/26" → "City Council Public Hearing"
    "Z-38-25  Denied 05/05/26"                     → "Denied"
    "Z-22-24 Second Neighborhood Meeting Required"  → "Active"
    """
    # Strip the case number prefix
    text = re.sub(r'^[A-Z]+-\d+[A-Z]?-\d{2,4}\s*', '', cell_text.strip(), flags=re.I).strip()
    text = re.sub(r'\s+', ' ', text)

    lower = text.lower()
    if any(k in lower for k in ['denied', 'withdrawn', 'disapproved']):
        return "Denied" if 'denied' in lower else "Withdrawn"
    if 'city council' in lower:
        return "Pending City Council"
    if 'planning commission' in lower:
        return "Pending Planning Commission"
    if 'neighborhood meeting' in lower:
        return "Active"
    if text:
        return "Active"
    return "Active"


def parse_meeting_date_from_status(cell_text: str) -> str:
    """
    Extract next hearing date from status cell if present.
    "Z-15-25 City Council Public Hearing 05/19/26" → "2026-05-19"
    """
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', cell_text)
    if not m:
        return None
    mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if yr < 100:
        yr += 2000
    try:
        return datetime(yr, mo, day).date().isoformat()
    except Exception:
        return None


def parse_final_action(action_text: str) -> tuple:
    """
    "Approved 01-06-26" → ("Approved", "2026-01-06")
    "WITHDRAWN 10-01-25" → ("Withdrawn", "2025-10-01")
    "Denied 04-07-26"   → ("Denied", "2026-04-07")
    """
    action_text = action_text.strip()
    lower = action_text.lower()

    if 'approved' in lower:
        status = "Approved"
    elif 'withdrawn' in lower:
        status = "Withdrawn"
    elif 'denied' in lower:
        status = "Denied"
    else:
        status = action_text.split()[0].title() if action_text else "Unknown"

    # Try to extract date MM-DD-YY or MM/DD/YY
    m = re.search(r'(\d{2})-(\d{2})-(\d{2,4})', action_text)
    if not m:
        m = re.search(r'(\d{2})/(\d{2})/(\d{2,4})', action_text)

    action_date = None
    if m:
        mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            action_date = datetime(yr, mo, day).date().isoformat()
        except Exception:
            pass

    return status, action_date


def parse_change_requested(text: str) -> tuple:
    """
    "12.08 AC FROM CX-5 w/TOD TO CX-20-CU w/TOD" → (from_zone, to_zone)
    Returns (current_zoning, proposed_zoning).
    """
    # Pattern: FROM <zone> TO <zone>
    m = re.search(r'FROM\s+([^\s]+(?:\s+w/\S+)*)\s+TO\s+([^\s]+(?:\s+w/\S+)*)', text, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def extract_pins_from_links(links: list) -> list:
    """Extract PIN numbers from iMAPS map links."""
    pins = []
    for link in links:
        m = re.search(r'[?&]pin=([0-9,]+)', link, re.I)
        if m:
            pins.extend(m.group(1).split(','))
    return [p.strip() for p in pins if p.strip()]


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _fetch_page(url: str, retries: int = 3) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TojiboxOracle/1.0)"}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                logger.warning(f"[petition:fetch] retry {attempt+1}/{retries} in {wait}s — {exc}")
                time.sleep(wait)
            else:
                raise


# ── HTML table parser ──────────────────────────────────────────────────────────

def _parse_tables(html_content: str) -> list:
    """
    Extract all rows from all <table> elements. Returns list of dicts.
    Rows that start with a petition case number are included.
    """
    tables = re.findall(r'<table[^>]*>(.*?)</table>', html_content, re.S | re.I)
    results = []
    for t in tables:
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', t, re.S | re.I)
        headers = []
        for row in rows:
            cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S | re.I)
            links = re.findall(r'href=["\']([^"\']+)["\']', row)
            clean = [
                html_module.unescape(
                    re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', c))
                ).strip()
                for c in cells
            ]
            if not clean:
                continue

            # Detect header rows
            if not headers and any(
                any(k in x.lower() for k in ['case number', 'change requested', 'final action', 'location'])
                for x in clean
            ):
                headers = clean
                continue

            # Only keep rows that start with a petition number
            if clean and CASE_RE.match(clean[0].split()[0] if clean[0].split() else ""):
                row_d = {}
                for i, val in enumerate(clean):
                    key = headers[i] if i < len(headers) else f"col_{i}"
                    row_d[key] = val
                row_d['_links'] = [l for l in links if l.startswith('http')]
                results.append(row_d)

    return results


# ── Scrape active cases ────────────────────────────────────────────────────────

def scrape_active_cases() -> list:
    """
    Returns list of petition dicts from the active rezoning cases page.
    Each dict has: petition_number, address, pins, status, meeting_date, application_url, council_district
    """
    logger.info(f"[petition:active] Fetching {ACTIVE_CASES_URL}")
    html = _fetch_page(ACTIVE_CASES_URL)
    rows = _parse_tables(html)
    logger.info(f"[petition:active] Parsed {len(rows)} rows")

    petitions = []
    for row in rows:
        # The case number cell is typically first or the "Case Number, Application & Status" col
        raw_case = ""
        status_text = ""
        for k, v in row.items():
            if 'case' in k.lower() or k == 'col_0':
                raw_case = v
                status_text = v
                break

        case_num_raw = parse_case_number_from_status(raw_case)
        if not case_num_raw:
            continue

        case_num = normalize_petition_number(case_num_raw)
        status   = parse_status_from_cell(status_text)
        meeting_date = parse_meeting_date_from_status(status_text)

        # Location / address column
        address = ""
        for k, v in row.items():
            if 'location' in k.lower():
                address = v
                break

        council_district = row.get('Council District', '')

        # Extract PINs from iMAPS links
        pins = extract_pins_from_links(row.get('_links', []))

        # PDF application link
        app_url = next(
            (l for l in row.get('_links', []) if 'blob.core' in l and '-ORD' not in l),
            None
        )

        petitions.append({
            "petition_number":   case_num,
            "petition_number_raw": case_num_raw,
            "county_id":         COUNTY_ID,
            "address":           address or None,
            "pins":              pins,
            "status":            status,
            "meeting_date":      meeting_date,
            "council_district":  council_district,
            "application_url":   app_url,
            "source":            "active_cases_page",
        })

    return petitions


# ── Scrape finalized cases ─────────────────────────────────────────────────────

def scrape_finalized_cases() -> list:
    """
    Returns list of petition dicts from the finalized rezoning cases page.
    Each dict has: petition_number, current_zoning, proposed_zoning, status, meeting_date, legislation_url
    """
    logger.info(f"[petition:finalized] Fetching {FINALIZED_CASES_URL}")
    html = _fetch_page(FINALIZED_CASES_URL)
    rows = _parse_tables(html)
    logger.info(f"[petition:finalized] Parsed {len(rows)} rows")

    petitions = []
    for row in rows:
        raw_case = row.get('Case Number', '') or row.get('col_0', '')
        if not raw_case:
            continue
        # The cell sometimes has just the case number or includes extra text
        m = re.match(r'^([A-Z]+-\d+[A-Z]?-\d{2,4})', raw_case.strip(), re.I)
        if not m:
            continue
        case_num_raw = m.group(1)
        case_num = normalize_petition_number(case_num_raw)

        change_text  = row.get('Change Requested', '')
        action_text  = row.get('Final Action', '')

        from_zone, to_zone = parse_change_requested(change_text)
        status, action_date = parse_final_action(action_text)

        ordinance_link = next(
            (l for l in row.get('_links', []) if 'blob.core' in l),
            None
        )

        petitions.append({
            "petition_number":   case_num,
            "petition_number_raw": case_num_raw,
            "county_id":         COUNTY_ID,
            "current_zoning":    from_zone,
            "proposed_zoning":   to_zone,
            "status":            status,
            "vote_result":       status,
            "meeting_date":      action_date,
            "legislation_url":   ordinance_link,
            "change_requested":  change_text,
            "source":            "finalized_cases_page",
        })

    return petitions


# ── Merge active + finalized into unified petition records ─────────────────────

def _merge_petitions(active: list, finalized: list) -> dict:
    """
    Merge active and finalized into a single dict keyed by canonical petition number.
    Active cases take precedence for status; finalized provides zoning details.
    """
    merged = {}

    for p in finalized:
        key = p['petition_number']
        merged[key] = {
            "petition_number":  key,
            "county_id":        COUNTY_ID,
            "petitioner":       None,
            "address":          None,
            "pins":             [],
            "current_zoning":   p.get("current_zoning"),
            "proposed_zoning":  p.get("proposed_zoning"),
            "status":           p.get("status"),
            "vote_result":      p.get("vote_result"),
            "action":           p.get("status"),
            "meeting_date":     p.get("meeting_date"),
            "legislation_url":  p.get("legislation_url"),
            "raw_properties": json.dumps({
                "change_requested": p.get("change_requested"),
                "source": p["source"],
            }),
        }

    for p in active:
        key = p['petition_number']
        if key in merged:
            # Enrich existing finalized record with address and PINs
            merged[key]['address']  = p.get('address') or merged[key].get('address')
            merged[key]['pins']     = p.get('pins') or merged[key].get('pins', [])
            merged[key]['status']   = p.get('status')   # active status overrides
            merged[key]['meeting_date'] = p.get('meeting_date') or merged[key]['meeting_date']
            existing_raw = json.loads(merged[key].get('raw_properties') or '{}')
            existing_raw['active_status_raw'] = p.get('status')
            existing_raw['council_district']  = p.get('council_district')
            existing_raw['application_url']   = p.get('application_url')
            merged[key]['raw_properties'] = json.dumps(existing_raw)
        else:
            # New pending petition not in finalized list yet
            merged[key] = {
                "petition_number":  key,
                "county_id":        COUNTY_ID,
                "petitioner":       None,
                "address":          p.get('address'),
                "pins":             p.get('pins', []),
                "current_zoning":   None,
                "proposed_zoning":  None,
                "status":           p.get('status'),
                "vote_result":      None,
                "action":           None,
                "meeting_date":     p.get('meeting_date'),
                "legislation_url":  p.get('application_url'),
                "raw_properties": json.dumps({
                    "source": "active_cases_page",
                    "council_district": p.get('council_district'),
                }),
            }

    return merged


# ── SQL templates ──────────────────────────────────────────────────────────────

UPSERT_PETITION_SQL = """
INSERT INTO rezoning_petitions (
    petition_number, county_id,
    petitioner, address, pins,
    current_zoning, proposed_zoning,
    status, vote_result, action,
    meeting_date, legislation_url,
    raw_properties, last_scraped_at, updated_at
) VALUES (
    %(petition_number)s, %(county_id)s,
    %(petitioner)s, %(address)s, %(pins)s,
    %(current_zoning)s, %(proposed_zoning)s,
    %(status)s, %(vote_result)s, %(action)s,
    %(meeting_date)s, %(legislation_url)s,
    %(raw_properties)s, %(last_scraped_at)s, %(updated_at)s
)
ON CONFLICT (petition_number, county_id) DO UPDATE SET
    petitioner      = COALESCE(EXCLUDED.petitioner, rezoning_petitions.petitioner),
    address         = COALESCE(EXCLUDED.address, rezoning_petitions.address),
    pins            = CASE WHEN array_length(EXCLUDED.pins, 1) > 0
                           THEN EXCLUDED.pins
                           ELSE rezoning_petitions.pins END,
    current_zoning  = COALESCE(EXCLUDED.current_zoning, rezoning_petitions.current_zoning),
    proposed_zoning = COALESCE(EXCLUDED.proposed_zoning, rezoning_petitions.proposed_zoning),
    status          = EXCLUDED.status,
    vote_result     = COALESCE(EXCLUDED.vote_result, rezoning_petitions.vote_result),
    action          = COALESCE(EXCLUDED.action, rezoning_petitions.action),
    meeting_date    = COALESCE(EXCLUDED.meeting_date, rezoning_petitions.meeting_date),
    legislation_url = COALESCE(EXCLUDED.legislation_url, rezoning_petitions.legislation_url),
    raw_properties  = EXCLUDED.raw_properties,
    last_scraped_at = EXCLUDED.last_scraped_at,
    updated_at      = EXCLUDED.updated_at
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
    logger.info("[petition] START — Raleigh Planning petition scraper")
    logger.info("=" * 70)

    conn = get_connection()
    run_id = _open_run(conn)
    logger.info(f"[petition] oracle_run id={run_id}")

    total_fetched = 0
    total_changed = 0
    total_skipped = 0

    try:
        # ── Step 1: Scrape both pages ─────────────────────────────────────────
        active    = scrape_active_cases()
        finalized = scrape_finalized_cases()
        logger.info(f"[petition] active={len(active)}, finalized={len(finalized)}")

        # ── Step 2: Save raw locally ──────────────────────────────────────────
        save_raw(active,    "petitions", "active_cases",    DATA_DIR)
        save_raw(finalized, "petitions", "finalized_cases", DATA_DIR)

        # ── Step 3: Merge ─────────────────────────────────────────────────────
        merged = _merge_petitions(active, finalized)
        total_fetched = len(merged)
        logger.info(f"[petition] Merged → {total_fetched} unique petitions")

        # ── Step 4: Load stored fingerprints ──────────────────────────────────
        stored_fps = fetch_all_fingerprints(conn, "petition_fingerprints", "petition_number")
        logger.info(f"[petition] Loaded {len(stored_fps)} stored fingerprints")

        # ── Step 5: Diff ──────────────────────────────────────────────────────
        changed_rows = []
        fp_rows      = []
        event_rows   = []

        for pnum, pdata in merged.items():
            new_fp = fingerprint(
                pnum,
                pdata.get("status"),
                pdata.get("current_zoning"),
                pdata.get("proposed_zoning"),
                pdata.get("meeting_date"),
                ",".join(sorted(pdata.get("pins") or [])),
            )
            old_fp = stored_fps.get(pnum)

            if new_fp == old_fp:
                continue

            total_changed += 1
            ts = now_iso()

            changed_rows.append({
                **pdata,
                "pins":           pdata.get("pins") or [],
                "last_scraped_at": ts,
                "updated_at":      ts,
            })

            fp_rows.append({
                "petition_number":  pnum,
                "county_id":        COUNTY_ID,
                "fingerprint_hash": new_fp,
                "last_checked_at":  ts,
            })

            event_type = "new_petition" if old_fp is None else "petition_status_change"
            event_rows.append({
                "id":              str(uuid.uuid4()),
                "event_type":      event_type,
                "county_id":       COUNTY_ID,
                "petition_number": pnum,
                "changed_fields":  ["status", "address", "pins", "current_zoning", "proposed_zoning"],
                "before_state":    None,
                "after_state":     json.dumps({
                    "petition_number":  pnum,
                    "status":           pdata.get("status"),
                    "address":          pdata.get("address"),
                    "current_zoning":   pdata.get("current_zoning"),
                    "proposed_zoning":  pdata.get("proposed_zoning"),
                    "meeting_date":     pdata.get("meeting_date"),
                    "pin_count":        len(pdata.get("pins") or []),
                }),
                "detected_at": ts,
            })

        unchanged = total_fetched - total_changed
        logger.info(f"[petition] Diff: {total_changed} changed/new, {unchanged} unchanged")

        # ── Step 6: Upsert petitions (100/batch) ──────────────────────────────
        if changed_rows:
            for i, batch in enumerate(chunk(changed_rows, SUPABASE_BATCH)):
                execute_batch(conn, UPSERT_PETITION_SQL, batch)
                logger.info(f"[petition] Upserted petitions batch {i+1} ({len(batch)} rows)")

        # ── Step 7: Insert change_events (100/batch) ──────────────────────────
        if event_rows:
            for i, batch in enumerate(chunk(event_rows, SUPABASE_BATCH)):
                execute_batch(conn, INSERT_CHANGE_EVENT_SQL, batch)
                logger.info(f"[petition] Inserted change_events batch {i+1} ({len(batch)} rows)")

        # ── Step 8: Upsert fingerprints (100/batch) ───────────────────────────
        if fp_rows:
            for i, batch in enumerate(chunk(fp_rows, SUPABASE_BATCH)):
                execute_batch(conn, UPSERT_FP_SQL, batch)
                logger.info(f"[petition] Upserted fingerprints batch {i+1} ({len(batch)} rows)")

        # ── Step 9: Close run ─────────────────────────────────────────────────
        _close_run(conn, run_id, "completed", total_fetched, total_changed, total_skipped)

        logger.info("=" * 70)
        logger.info("[petition] DONE")
        logger.info(f"[petition]   total petitions : {total_fetched}")
        logger.info(f"[petition]   changed/new     : {total_changed}")
        logger.info(f"[petition]   unchanged        : {unchanged}")
        logger.info("=" * 70)

    except Exception as exc:
        logger.exception(f"[petition] FAILED — {exc}")
        _close_run(conn, run_id, "failed", total_fetched, total_changed, total_skipped, str(exc))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
