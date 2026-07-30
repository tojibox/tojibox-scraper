-- 002_parcels.sql
-- Wake County parcel records — populated by the daily parcel scraper.
-- Source: Wake County ArcGIS REST API
--   https://maps.wakegov.com/arcgis/rest/services/Property/Parcels/MapServer/0/query

CREATE TABLE parcels (
  -- identity
  id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  pin             TEXT        NOT NULL,           -- Wake County PIN_NUM e.g. '0716756963'
  reid            TEXT,                           -- Real estate ID (REID)
  county_id       TEXT        NOT NULL DEFAULT 'raleigh_nc',

  -- location
  site_address    TEXT,
  city            TEXT,
  zipcode         TEXT,

  -- ownership
  owner           TEXT,

  -- valuation (USD)
  total_value_assd  NUMERIC,
  land_val          NUMERIC,
  bldg_val          NUMERIC,
  sale_price        NUMERIC,

  -- physical
  heated_area     NUMERIC,                        -- sq ft
  calc_area       NUMERIC,                        -- acres
  year_built      SMALLINT,
  units           NUMERIC,

  -- classification
  type_and_use    TEXT,
  land_class      TEXT,
  design_style    TEXT,

  -- geometry (GeoJSON polygon from ArcGIS)
  geometry        JSONB,

  -- raw ArcGIS attributes (full record preserved)
  raw_properties  JSONB       DEFAULT '{}',

  -- timestamps
  first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- one parcel per PIN per county
ALTER TABLE parcels
  ADD CONSTRAINT uq_parcels_pin_county UNIQUE (pin, county_id);

-- lookup indexes
CREATE INDEX idx_parcels_pin           ON parcels (pin);
CREATE INDEX idx_parcels_county        ON parcels (county_id);
CREATE INDEX idx_parcels_owner         ON parcels (owner);
CREATE INDEX idx_parcels_address_trgm  ON parcels USING GIN (site_address gin_trgm_ops);
CREATE INDEX idx_parcels_updated_at    ON parcels (updated_at DESC);
