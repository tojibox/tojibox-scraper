-- 009_add_giwa_columns.sql
-- Additive migration for the Tojibox/GIWA oracle pipeline, layered onto the
-- SAME Supabase project the (Hedera-based) ZoneProof hackathon submission
-- already uses. This does not modify, rename, or drop any existing
-- Hedera-related column — 001-008 in this repo are near-identical copies
-- of what already created this schema and should NOT be re-run here (the
-- tables already exist). This file is the only migration that actually
-- needs to run against the shared project.
--
-- change_events gets its own GIWA-specific "has this been committed, and
-- where" bookkeeping, separate from the existing committed_at/batch_id/
-- evm_snapshot_index columns that ZoneProof's Hedera pipeline already
-- writes to. Without this split, the two independent commit pipelines
-- would race to mark the same row committed — Tojibox committing an
-- event to GIWA would make it look already-committed to ZoneProof's
-- Hedera pipeline (and vice versa), silently breaking whichever pipeline
-- ran second.

ALTER TABLE change_events
  ADD COLUMN IF NOT EXISTS giwa_committed_at       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS giwa_batch_id            UUID,
  ADD COLUMN IF NOT EXISTS giwa_evm_snapshot_index  BIGINT;

CREATE INDEX IF NOT EXISTS idx_change_events_giwa_uncommitted
  ON change_events (detected_at)
  WHERE giwa_committed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_change_events_giwa_batch
  ON change_events (giwa_batch_id)
  WHERE giwa_batch_id IS NOT NULL;

-- merkle_batches: GIWA's equivalent of hedera_evm_tx_hash / hedera_evm_block.
-- No row-level conflict here even though the table is shared — Tojibox's
-- pipeline always INSERTs a fresh batch_id (UUID) per commit, so its rows
-- never overlap with ZoneProof's existing Hedera batch rows. Those rows
-- simply have evm_tx_hash / evm_block left NULL.
ALTER TABLE merkle_batches
  ADD COLUMN IF NOT EXISTS evm_tx_hash TEXT,
  ADD COLUMN IF NOT EXISTS evm_block   BIGINT;
