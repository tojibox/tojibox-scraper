-- 005_change_events.sql
-- Canonical append-only log of every detected change.
-- Written by scrapers; read by the blockchain pipeline.
-- Never updated after insert — blockchain fields are filled in by the pipeline.

CREATE TYPE change_event_type AS ENUM (
  'new_parcel',
  'parcel_owner_change',
  'parcel_value_change',
  'parcel_zoning_change',
  'new_petition',
  'petition_status_change',
  'petition_vote_change'
);

CREATE TABLE change_events (
  id                    UUID              PRIMARY KEY DEFAULT gen_random_uuid(),

  -- what changed
  event_type            change_event_type NOT NULL,
  county_id             TEXT              NOT NULL DEFAULT 'raleigh_nc',

  -- which record (at least one must be set)
  pin                   TEXT,
  petition_number       TEXT,

  -- diff
  changed_fields        TEXT[]            NOT NULL DEFAULT '{}',
  before_state          JSONB,
  after_state           JSONB             NOT NULL,

  -- when the scraper detected this
  detected_at           TIMESTAMPTZ       NOT NULL DEFAULT NOW(),

  -- Merkle leaf: keccak256(pin || JSON(after_state)) — set by pipeline
  merkle_leaf_hash      TEXT,

  -- blockchain commit fields — set by pipeline after on-chain write
  batch_id              UUID,
  evm_snapshot_index    BIGINT,
  committed_at          TIMESTAMPTZ,

  CONSTRAINT chk_has_subject CHECK (pin IS NOT NULL OR petition_number IS NOT NULL)
);

-- scraper writes by event type and time
CREATE INDEX idx_change_events_type       ON change_events (event_type, detected_at DESC);
CREATE INDEX idx_change_events_pin        ON change_events (pin, detected_at DESC) WHERE pin IS NOT NULL;
CREATE INDEX idx_change_events_petition   ON change_events (petition_number, detected_at DESC) WHERE petition_number IS NOT NULL;
CREATE INDEX idx_change_events_county     ON change_events (county_id, detected_at DESC);

-- pipeline reads uncommitted events to build next Merkle batch
CREATE INDEX idx_change_events_uncommitted
  ON change_events (detected_at)
  WHERE committed_at IS NULL;

-- batch grouping (pipeline uses this to relate events → merkle_batches row)
CREATE INDEX idx_change_events_batch      ON change_events (batch_id) WHERE batch_id IS NOT NULL;
