-- 008_oracle_runs.sql
-- Execution log for every scraper run.
-- Each scraper (rezoning / parcel) inserts a row when it starts
-- and updates it when it completes or fails.

CREATE TYPE oracle_run_type AS ENUM ('rezoning', 'parcel');
CREATE TYPE oracle_run_status AS ENUM ('running', 'completed', 'failed');

CREATE TABLE oracle_runs (
  id                UUID              PRIMARY KEY DEFAULT gen_random_uuid(),

  run_type          oracle_run_type   NOT NULL,
  status            oracle_run_status NOT NULL DEFAULT 'running',

  -- stats filled in on completion
  records_fetched   INT,
  changes_detected  INT,
  records_skipped   INT,

  -- error detail on failure
  error_message     TEXT,
  error_detail      JSONB,

  -- timing
  started_at        TIMESTAMPTZ       NOT NULL DEFAULT NOW(),
  completed_at      TIMESTAMPTZ,
  duration_seconds  NUMERIC GENERATED ALWAYS AS (
    CASE WHEN completed_at IS NOT NULL
         THEN EXTRACT(EPOCH FROM (completed_at - started_at))
         ELSE NULL
    END
  ) STORED
);

-- latest runs per type (used by monitoring / status endpoint)
CREATE INDEX idx_oracle_runs_type_time  ON oracle_runs (run_type, started_at DESC);
CREATE INDEX idx_oracle_runs_status     ON oracle_runs (status, started_at DESC);
