/**
 * pipeline/processor.mjs
 *
 * Reads pending rezoning change events from the oracle API,
 * builds a Merkle tree, calls TojiboxOracle.commitBatch() on GIWA (OP-Stack EVM L2),
 * then marks the events as committed in Postgres.
 *
 * This is the canonical batch committer for Tojibox — run it directly (cron or
 * manual). See ../cre/workflow.ts for an optional/future Chainlink CRE DON-consensus
 * variant that is not the primary/tested path.
 *
 * Usage:
 *   cd pipeline && npm install && npm run process
 *
 * Required env vars (../.env):
 *   TOJIBOX_ORACLE_ADDRESS, GIWA_PRIVATE_KEY,
 *   DB_HOST/PORT/USER/PASSWORD/NAME, API_PORT
 */

import { ethers }              from 'ethers';
import pg                      from 'pg';
import dotenv                  from 'dotenv';
import { fileURLToPath }       from 'url';
import { dirname, join }       from 'path';
import { readFileSync }        from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = dirname(__filename);

dotenv.config({ path: join(__dirname, '../.env') });

// ── Config ─────────────────────────────────────────────────────────────────────

// Defaults to localhost for local dev, where the API and pipeline run on the
// same machine. In production these are separate deployed services (e.g. two
// different Railway services), so ORACLE_API_URL must be set to the deployed
// tojibox-api's public URL — "localhost" inside the pipeline's own container
// would otherwise point at itself, not at the API.
const API_BASE       = process.env.ORACLE_API_URL || `http://localhost:${process.env.API_PORT || 8001}`;
const CONTRACT_ADDR = process.env.TOJIBOX_ORACLE_ADDRESS;
const PRIV_KEY      = process.env.GIWA_PRIVATE_KEY;
const RPC_URL       = process.env.GIWA_RPC_URL || 'https://sepolia-rpc.giwa.io/';
const COUNTY_ID     = 'raleigh_nc';

// Max PINs per batch — each new PIN slot costs ~20k gas for SSTORE.
// 50 PINs × 20k = 1M gas, leaving headroom for base overhead + Merkle storage.
const MAX_PINS_PER_BATCH = 50;

// Events per batch — controls calldata size and DB update cost.
const BATCH_SIZE = parseInt(process.env.BATCH_SIZE || '100');

const artifact = JSON.parse(
  readFileSync(
    join(__dirname, '../contracts/artifacts/src/TojiboxOracle.sol/TojiboxOracle.json'),
    'utf8'
  )
);
const ABI = artifact.abi;

// ── DB pool ────────────────────────────────────────────────────────────────────

const { Pool } = pg;
const pool = new Pool({
  host:     process.env.DB_HOST,
  port:     parseInt(process.env.DB_PORT || '5432'),
  user:     process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
  ssl:      { rejectUnauthorized: false },
  max:      3,
});

// ── Merkle helpers ─────────────────────────────────────────────────────────────

/**
 * Build a binary Merkle tree from bytes32 hex leaf values.
 * Internal nodes are keccak256(sorted(left, right)) — matches TojiboxOracle._verifyProof.
 * Returns { root: bytes32, tree: levels[] } where tree[0] = leaves.
 */
function buildMerkleTree(leaves) {
  if (leaves.length === 0) throw new Error('Cannot build Merkle tree: no leaves');

  let level = [...leaves];
  const tree = [level];

  while (level.length > 1) {
    const next = [];
    for (let i = 0; i < level.length; i += 2) {
      const left  = level[i];
      const right = i + 1 < level.length ? level[i + 1] : level[i]; // duplicate last if odd
      const [a, b] = left <= right ? [left, right] : [right, left];  // sort for determinism
      next.push(ethers.keccak256(ethers.concat([a, b])));
    }
    level = next;
    tree.push(level);
  }

  return { root: level[0], tree };
}

/**
 * Generate a Merkle proof for the leaf at `leafIndex`.
 * Proof is an array of sibling hashes bottom-up.
 */
function getMerkleProof(tree, leafIndex) {
  const proof = [];
  let idx = leafIndex;
  for (let i = 0; i < tree.length - 1; i++) {
    const level   = tree[i];
    const sibling = idx % 2 === 0 ? idx + 1 : idx - 1;
    if (sibling < level.length) {
      proof.push(level[sibling]);
    }
    idx = Math.floor(idx / 2);
  }
  return proof;
}

// ── Main ───────────────────────────────────────────────────────────────────────

// Set once step 4 inserts this run's merkle_batches row — read by the
// failure handler below so cleanup only ever touches the row THIS run
// created. merkle_batches is shared with a separate Hedera-based
// pipeline that may have its own row genuinely sitting at status='pending'
// at the same moment; an unscoped "WHERE status = 'pending'" cleanup would
// wrongly fail that pipeline's in-flight batch too.
let currentBatchUUID = null;

async function run() {
  console.log('╔══════════════════════════════════════════╗');
  console.log('║   Tojibox Oracle — Batch Processor       ║');
  console.log('╚══════════════════════════════════════════╝');
  console.log(`Contract : ${CONTRACT_ADDR}`);
  console.log(`API      : ${API_BASE}`);
  console.log(`Network  : GIWA Sepolia (chain 91342)`);

  if (!CONTRACT_ADDR) throw new Error('TOJIBOX_ORACLE_ADDRESS not set in ../.env');
  if (!PRIV_KEY)      throw new Error('GIWA_PRIVATE_KEY not set in ../.env');

  // ── 1. Fetch pending events ────────────────────────────────────────────────

  console.log('\n[1/6] Fetching pending events from oracle API...');
  const resp = await fetch(`${API_BASE}/api/oracle/pending-events?limit=${BATCH_SIZE}`);
  if (!resp.ok) throw new Error(`API error ${resp.status}: ${await resp.text()}`);

  const body = await resp.json();
  // API returns { count, events } — take up to BATCH_SIZE
  const events = (body.events || []).slice(0, BATCH_SIZE);
  const count  = events.length;
  console.log(`      ${body.count} total pending; processing ${count} in this batch`);

  if (count === 0) {
    console.log('\nNothing to commit. Exiting cleanly.');
    await pool.end();
    return;
  }

  // ── 2. Build Merkle tree ───────────────────────────────────────────────────

  console.log('\n[2/6] Building Merkle tree...');
  const leaves = events.map(e => e.leaf_hash); // SHA-256 bytes32 from oracle API
  const { root: merkleRoot, tree } = buildMerkleTree(leaves);
  const treeDepth = tree.length - 1;

  console.log(`      Leaves : ${leaves.length}`);
  console.log(`      Depth  : ${treeDepth}`);
  console.log(`      Root   : ${merkleRoot}`);

  // Quick self-verification of first leaf
  if (leaves.length > 0) {
    const proof = getMerkleProof(tree, 0);
    let computed = leaves[0];
    for (const sibling of proof) {
      const [a, b] = computed <= sibling ? [computed, sibling] : [sibling, computed];
      computed = ethers.keccak256(ethers.concat([a, b]));
    }
    if (computed !== merkleRoot) throw new Error('Merkle self-verification failed — bug in buildMerkleTree');
    console.log('      Self-verify: OK');
  }

  // ── 3. Collect PIN → petition mappings ────────────────────────────────────

  console.log('\n[3/6] Collecting affected PINs...');
  const pinMap = new Map(); // pin → petition_number
  for (const ev of events) {
    const petition = ev.petition_number || '';
    if (ev.pin) {
      pinMap.set(ev.pin, petition);
    }
    if (Array.isArray(ev.affected_pins)) {
      for (const p of ev.affected_pins) {
        if (p) pinMap.set(String(p), petition);
      }
    }
  }

  // Cap PINs to avoid exceeding gas limit (~20k gas per new PIN slot)
  const allPins = Array.from(pinMap.entries()).slice(0, MAX_PINS_PER_BATCH);
  const pinHashes       = allPins.map(([p]) => ethers.keccak256(ethers.toUtf8Bytes(p)));
  const petitionNumbers = allPins.map(([, pet]) => pet);

  console.log(`      ${pinMap.size} unique PIN(s) found; storing ${pinHashes.length} on-chain`);
  if (pinMap.size > MAX_PINS_PER_BATCH) {
    console.log(`      (capped at ${MAX_PINS_PER_BATCH} — full PIN index in Postgres)`);
  }

  // ── 4. Insert pending merkle_batches row ──────────────────────────────────

  console.log('\n[4/6] Creating DB batch record (status=pending)...');
  const batchUUID = crypto.randomUUID();
  currentBatchUUID = batchUUID;
  await pool.query(
    `INSERT INTO merkle_batches
       (batch_id, merkle_root, tree_depth, leaf_count, changes_count, status)
     VALUES ($1, $2, $3, $4, $5, 'pending')`,
    [batchUUID, merkleRoot, treeDepth, count, count]
  );
  console.log(`      Batch UUID : ${batchUUID}`);

  // ── 5. Submit commitBatch() to GIWA ───────────────────────────────────────

  console.log('\n[5/6] Submitting commitBatch() to GIWA...');
  const provider = new ethers.JsonRpcProvider(RPC_URL);
  const wallet   = new ethers.Wallet(PRIV_KEY, provider);
  const contract = new ethers.Contract(CONTRACT_ADDR, ABI, wallet);

  let tx;
  try {
    tx = await contract.commitBatch(
      merkleRoot,
      BigInt(count),
      0n,
      BigInt(count - 1),
      COUNTY_ID,
      pinHashes,
      petitionNumbers
    );
  } catch (sendErr) {
    console.error('      commitBatch() send failed:', sendErr.message || sendErr);
    throw sendErr;
  }
  console.log(`      TX hash  : ${tx.hash}`);
  console.log('      Waiting for confirmation...');

  const receipt = await tx.wait(1);

  console.log(`      Block    : ${receipt.blockNumber}`);
  console.log(`      Gas used : ${receipt.gasUsed?.toString()}`);

  // Parse on-chain batchId from BatchCommitted event log
  let onChainBatchId = null;
  for (const log of (receipt.logs || [])) {
    try {
      const parsed = contract.interface.parseLog(log);
      if (parsed?.name === 'BatchCommitted') {
        onChainBatchId = parsed.args.batchId;
        break;
      }
    } catch { /* not our event */ }
  }
  if (onChainBatchId === null) {
    // Fallback: batchCount - 1 is the ID just written
    console.warn('      BatchCommitted log not found in receipt — reading batchCount from chain...');
    const currentCount = await contract.batchCount();
    onChainBatchId = currentCount - 1n;
  }
  console.log(`      On-chain batch ID : ${onChainBatchId.toString()}`);

  // ── 6. Commit changes to DB ────────────────────────────────────────────────

  console.log('\n[6/6] Marking events as committed in Postgres...');

  // Update merkle_batches row
  await pool.query(
    `UPDATE merkle_batches
     SET evm_tx_hash    = $1,
         evm_block      = $2,
         snapshot_index = $3,
         status         = 'committed',
         committed_at   = NOW()
     WHERE batch_id = $4`,
    [tx.hash, receipt.blockNumber, onChainBatchId.toString(), batchUUID]
  );

  // Bulk-update change_events with giwa_committed_at / giwa_batch_id /
  // giwa_evm_snapshot_index — GIWA-specific columns (see migrations/
  // 009_add_giwa_columns.sql), not the plain committed_at/batch_id/
  // evm_snapshot_index columns. This DB is shared with a separate
  // Hedera-based pipeline that already tracks its own commit status on
  // those columns for the same rows; writing there instead would mark
  // events "committed" from that pipeline's perspective too and hide
  // them from its own pending-events query. merkle_leaf_hash is safe to
  // share since it's a pure function of the event fields — identical
  // value no matter which pipeline computes it.
  const eventIds   = events.map(e => e.id);
  const leafHashes = leaves;

  await pool.query(
    `UPDATE change_events AS ce
     SET giwa_committed_at      = NOW(),
         giwa_batch_id           = $2::uuid,
         giwa_evm_snapshot_index = $3::bigint,
         merkle_leaf_hash        = v.leaf_hash
     FROM (
       SELECT unnest($1::uuid[]) AS event_id,
              unnest($4::text[]) AS leaf_hash
     ) v
     WHERE ce.id = v.event_id`,
    [eventIds, batchUUID, onChainBatchId.toString(), leafHashes]
  );

  console.log(`      ${count} event(s) marked committed`);

  // ── Done ───────────────────────────────────────────────────────────────────

  console.log('\n╔══════════════════════════════════════════════════════════╗');
  console.log('║  ✅ BATCH COMMITTED                                      ║');
  console.log('╚══════════════════════════════════════════════════════════╝');
  console.log(`  Events     : ${count}`);
  console.log(`  Batch #    : ${onChainBatchId.toString()}  (on-chain)`);
  console.log(`  Batch UUID : ${batchUUID}  (Postgres)`);
  console.log(`  Root       : ${merkleRoot}`);
  console.log(`  TX         : ${tx.hash}`);
  console.log(`  Block      : ${receipt.blockNumber}`);
  console.log('');
  console.log('  Anyone can verify an event with:');
  console.log(`  TojiboxOracle.verify(leafHash, proof, ${onChainBatchId.toString()})`);
  console.log(`  Contract: ${CONTRACT_ADDR}`);

  await pool.end();
}

run().catch(async err => {
  console.error('\n❌ Processor failed:', err.message || err);
  // Mark THIS run's batch row as 'failed', scoped by batch_id — not a bare
  // "WHERE status = 'pending'", which would also catch a concurrently
  // in-flight batch from the separate Hedera pipeline sharing this table.
  if (currentBatchUUID) {
    try {
      await pool.query(
        `UPDATE merkle_batches SET status = 'failed', error_message = $1
         WHERE batch_id = $2 AND status = 'pending'`,
        [err.message?.slice(0, 500) || 'unknown error', currentBatchUUID]
      );
    } catch { /* ignore cleanup failure */ }
  }
  await pool.end().catch(() => {});
  process.exit(1);
});
