-- 007_merkle_batches.sql
-- One row per on-chain Merkle commit.
-- Written by the blockchain pipeline after successfully writing to
-- TogiboxOracle.sol on GIWA (OP-Stack EVM L2).

CREATE TABLE merkle_batches (
  id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id                UUID        UNIQUE NOT NULL DEFAULT gen_random_uuid(),

  -- Merkle tree details
  merkle_root             TEXT        NOT NULL,
  tree_depth              INT,
  leaf_count              INT         NOT NULL,
  changes_count           INT         NOT NULL,

  -- on-chain references
  snapshot_index          BIGINT,                   -- TogiboxOracle.sol snapshots[] index
  evm_tx_hash             TEXT,                     -- GIWA EVM tx that wrote the root
  evm_block               BIGINT,
  ethereum_tx_hash        TEXT,                     -- Sepolia CCIP relay tx (Stage 5)

  -- status
  status                  TEXT        NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'committed', 'failed')),
  error_message           TEXT,

  -- timestamps
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  committed_at            TIMESTAMPTZ
);

CREATE INDEX idx_merkle_batches_status      ON merkle_batches (status);
CREATE INDEX idx_merkle_batches_committed   ON merkle_batches (committed_at DESC) WHERE committed_at IS NOT NULL;
CREATE INDEX idx_merkle_batches_snapshot    ON merkle_batches (snapshot_index) WHERE snapshot_index IS NOT NULL;
