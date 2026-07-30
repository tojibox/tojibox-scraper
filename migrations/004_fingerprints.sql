-- 004_fingerprints.sql
-- Fingerprint tables for change detection.
-- Each scraper run hashes key fields per record and compares against the stored
-- fingerprint. Only records whose hash changed are written to change_events.

-- parcel fingerprints: detect ownership, value, or zoning changes
CREATE TABLE parcel_fingerprints (
  pin               TEXT        PRIMARY KEY,
  county_id         TEXT        NOT NULL DEFAULT 'raleigh_nc',

  -- SHA-256 of (owner || total_value_assd || land_class || type_and_use)
  fingerprint_hash  TEXT        NOT NULL,

  last_checked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- petition fingerprints: detect status, vote_result, or meeting_date changes
CREATE TABLE petition_fingerprints (
  petition_number   TEXT        PRIMARY KEY,
  county_id         TEXT        NOT NULL DEFAULT 'raleigh_nc',

  -- SHA-256 of (status || vote_result || meeting_date || action)
  fingerprint_hash  TEXT        NOT NULL,

  last_checked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_parcel_fp_county    ON parcel_fingerprints  (county_id);
CREATE INDEX idx_petition_fp_county  ON petition_fingerprints (county_id);
