-- 006_parcel_history.sql
-- Per-parcel denormalized history with Merkle proof storage.
-- One row per (pin, change_event). Populated by the blockchain pipeline
-- after each on-chain Merkle commit.
-- Fast lookup: "show me everything that ever changed for PIN X"

CREATE TABLE parcel_history (
  id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),

  -- which parcel
  pin               TEXT        NOT NULL,
  county_id         TEXT        NOT NULL DEFAULT 'raleigh_nc',

  -- link back to the raw change event
  event_id          UUID        NOT NULL REFERENCES change_events (id),

  -- which on-chain snapshot this is included in
  snapshot_index    BIGINT,                       -- TogiboxOracle.sol snapshots[] index
  batch_id          UUID,

  -- Merkle proof for this leaf at this snapshot
  merkle_leaf_hash  TEXT,
  merkle_proof      JSONB,                        -- array of sibling hashes
  merkle_root       TEXT,                         -- root at time of commit

  -- what changed (denormalized from change_events for fast reads)
  event_type        change_event_type NOT NULL,
  changed_fields    TEXT[]      NOT NULL DEFAULT '{}',
  before_state      JSONB,
  after_state       JSONB       NOT NULL,

  -- when this history record was created
  changed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- primary access pattern: all history for a PIN ordered by time
CREATE INDEX idx_parcel_history_pin         ON parcel_history (pin, changed_at DESC);
CREATE INDEX idx_parcel_history_county      ON parcel_history (county_id);
CREATE INDEX idx_parcel_history_snapshot    ON parcel_history (snapshot_index);
CREATE INDEX idx_parcel_history_batch       ON parcel_history (batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_parcel_history_event_type  ON parcel_history (event_type, changed_at DESC);
