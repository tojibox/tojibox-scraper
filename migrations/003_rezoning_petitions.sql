-- 003_rezoning_petitions.sql
-- Rezoning petition records — populated by the 6-hour rezoning scraper.
-- Source: Raleigh Planning ArcGIS
--   https://maps.raleighnc.gov/arcgis/rest/services/Planning/ZoningPetitions/MapServer/0/query

CREATE TABLE rezoning_petitions (
  -- identity
  id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  petition_number   TEXT    NOT NULL,             -- e.g. 'Z-29-2023'
  county_id         TEXT    NOT NULL DEFAULT 'raleigh_nc',

  -- petitioner info
  petitioner        TEXT,
  address           TEXT,
  location          TEXT,

  -- zoning
  current_zoning    TEXT,
  proposed_zoning   TEXT,

  -- workflow status
  status            TEXT,                         -- e.g. 'Pending', 'Approved', 'Denied'
  action            TEXT,
  vote_result       TEXT,
  meeting_date      DATE,
  meeting_type      TEXT,

  -- linked parcel PINs (one petition can affect multiple parcels)
  pins              TEXT[]  DEFAULT '{}',

  -- documentation
  legislation_url   TEXT,
  file_number       TEXT,

  -- raw attributes from ArcGIS
  raw_properties    JSONB   DEFAULT '{}',

  -- timestamps
  first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- one petition per number per county
ALTER TABLE rezoning_petitions
  ADD CONSTRAINT uq_petitions_number_county UNIQUE (petition_number, county_id);

-- lookup indexes
CREATE INDEX idx_petitions_number     ON rezoning_petitions (petition_number);
CREATE INDEX idx_petitions_county     ON rezoning_petitions (county_id);
CREATE INDEX idx_petitions_status     ON rezoning_petitions (status);
CREATE INDEX idx_petitions_updated    ON rezoning_petitions (updated_at DESC);
CREATE INDEX idx_petitions_pins       ON rezoning_petitions USING GIN (pins);
