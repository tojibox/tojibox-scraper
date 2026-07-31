/**
 * cre/workflow.ts
 *
 * Optional/future Chainlink CRE (Runtime Environment) workflow for the
 * Tojibox Oracle — DON-consensus variant of the batch committer.
 *
 * ── STATUS: NOT the primary path ────────────────────────────────────────────
 * The primary, tested way to commit batches is running `pipeline/processor.mjs`
 * directly (cron or manual trigger) — see ../pipeline/processor.mjs. This CRE
 * workflow is kept for documentation/optionality: it shows how the same
 * Merkle-root-then-commit logic could be distributed across DON nodes for
 * BFT consensus instead of trusting a single processor instance.
 *
 * Known wiring gap (carried over from the original ZoneProof/Hedera version,
 * not yet fixed): this workflow POSTs the agreed root to
 * `${apiUrl}/api/oracle/commit-root`. That endpoint does not exist in any
 * sibling repo yet — `tojibox-api` (a separate repo, not this one) would need
 * to implement it (accept {merkleRoot, leafCount, contractAddress, countyId},
 * hold the GIWA signer, and call TojiboxOracle.commitBatch()) before this
 * workflow path can run end-to-end. Until then, use pipeline/processor.mjs.
 *
 * What this does (once the endpoint above exists):
 *   1. Cron trigger fires on schedule
 *   2. Each DON node independently fetches pending rezoning events from the
 *      oracle API  →  computes SHA-256 leaf hashes  →  builds a Merkle tree
 *   3. BFT consensus: nodes compare their Merkle roots. If 2/3+ match, the
 *      root is accepted. A single divergent node (tampered data / bad API)
 *      cannot affect the outcome.
 *   4. The agreed root is submitted to TojiboxOracle.sol on GIWA via
 *      a bridge call to the oracle API's commit-root endpoint.
 *
 * Deploy to CRE:
 *   chainlink workflow deploy --config config.json workflow.ts
 *
 * GIWA note:
 *   GIWA is a standard OP-Stack EVM chain. If/when it's a listed CRE EVM
 *   target, this workflow could call runtime.evm.contractWrite() directly
 *   instead of bridging through the oracle API's HTTP endpoint.
 */

import {
  CronCapability,
  HTTPClient,
  handler,
  consensusMedianAggregation,
  Runner,
  type NodeRuntime,
  type Runtime,
} from "@chainlink/cre-sdk"
import { ethers } from "ethers"

// ── Workflow config (injected from config.json at runtime) ─────────────────────

type Config = {
  schedule:           string   // cron expression, e.g. "0 */1 * * *"
  apiUrl:             string   // oracle API base, e.g. "http://oracle-api:8001"
  batchSize:          number   // max events per commit batch
  countyId:           string   // "raleigh_nc"
  contractAddress:    string   // TojiboxOracle.sol on GIWA
  giwaRpc:            string   // "https://sepolia-rpc.giwa.io/"
  giwaChainId:        number   // 91342 (GIWA Sepolia testnet)
  consensusThreshold: number   // 0.67 = 2/3 of nodes must agree
}

// ── Per-node computation result ────────────────────────────────────────────────

type NodeResult = {
  merkleRootAsInt: bigint   // bytes32 Merkle root as BigInt for consensus
  leafCount:       bigint   // number of events in this batch
  eventIdsJson:    bigint   // unused by consensus — root alone is the signal
}

// ── Merkle tree (same algorithm as pipeline/processor.mjs) ────────────────────

/**
 * Build a binary Merkle tree from bytes32 hex leaf values.
 * Internal nodes: keccak256(sorted(left, right)).
 * Matches the _verifyProof logic in TojiboxOracle.sol.
 */
function buildMerkleRoot(leaves: string[]): string {
  if (leaves.length === 0) throw new Error("No leaves")

  let level = [...leaves]

  while (level.length > 1) {
    const next: string[] = []
    for (let i = 0; i < level.length; i += 2) {
      const left  = level[i]
      const right = i + 1 < level.length ? level[i + 1] : level[i] // duplicate last if odd
      const [a, b] = left <= right ? [left, right] : [right, left]  // sort for determinism
      next.push(ethers.keccak256(ethers.concat([a, b])))
    }
    level = next
  }

  return level[0]
}

// ── Per-node function (runs independently on every DON node) ──────────────────

/**
 * Each DON node calls this independently.
 * Steps:
 *   1. GET /api/oracle/pending-events  →  list of events with pre-computed leaf_hash
 *   2. Build Merkle tree over the leaf hashes
 *   3. Return the root as BigInt so consensusMedianAggregation can compare nodes
 *
 * If all nodes see the same data from the same deterministic API, they will
 * all compute the same root → median == exact match → consensus.
 * If one node is compromised or the API is tampered, its root diverges → excluded.
 */
const computeMerkleRoot = (nodeRuntime: NodeRuntime<Config>): NodeResult => {
  nodeRuntime.log(`[node] Fetching pending events from ${nodeRuntime.config.apiUrl}`)

  const httpClient  = new HTTPClient()
  const response    = httpClient.sendRequest(nodeRuntime, {
    url:    `${nodeRuntime.config.apiUrl}/api/oracle/pending-events?limit=${nodeRuntime.config.batchSize}`,
    method: "GET" as const,
  }).result()

  const bodyText = new TextDecoder().decode(response.body)
  const body     = JSON.parse(bodyText) as { count: number; events: Array<{ id: string; leaf_hash: string }> }

  nodeRuntime.log(`[node] Received ${body.count} pending events`)

  if (body.count === 0) {
    return { merkleRootAsInt: 0n, leafCount: 0n, eventIdsJson: 0n }
  }

  const leaves    = body.events.map(e => e.leaf_hash)
  const root      = buildMerkleRoot(leaves)

  nodeRuntime.log(`[node] Merkle root: ${root}`)

  return {
    merkleRootAsInt: BigInt(root),
    leafCount:       BigInt(body.count),
    eventIdsJson:    0n,           // only root matters for consensus
  }
}

// ── Workflow trigger handler ───────────────────────────────────────────────────

/**
 * Fired by the cron trigger.
 *
 * The runtime.runInNodeMode() call distributes computeMerkleRoot across all
 * DON nodes and uses consensusMedianAggregation to agree on the result.
 * Since all nodes compute the same deterministic root from the same ordered
 * data source, median == exact root when nodes are healthy.
 */
const onCronTrigger = (runtime: Runtime<Config>): void => {
  runtime.log("TojiboxOracle workflow triggered — computing batch Merkle root")

  // ── Step 1: Distributed computation + BFT consensus ───────────────────────

  const consensusResult = runtime.runInNodeMode(
    computeMerkleRoot,
    consensusMedianAggregation<bigint>()   // for identical values, median = exact match
  )().result()

  if (consensusResult.leafCount === 0n) {
    runtime.log("No pending events. Nothing to commit.")
    return
  }

  const merkleRootHex = "0x" + consensusResult.merkleRootAsInt.toString(16).padStart(64, "0")
  const leafCount     = Number(consensusResult.leafCount)

  runtime.log(`Consensus reached  →  root: ${merkleRootHex}  events: ${leafCount}`)

  // ── Step 2: Submit to GIWA via oracle bridge ──────────────────────────────
  //
  // See the header comment: /api/oracle/commit-root is not implemented by any
  // sibling repo yet. tojibox-api would need to add it (hold the GIWA signer,
  // validate the root against pending events, call TojiboxOracle.commitBatch())
  // for this path to run end-to-end. Until then, use pipeline/processor.mjs.

  const httpClient = new HTTPClient()

  runtime.runInNodeMode(
    (nodeRuntime: NodeRuntime<Config>) => {
      const payload = JSON.stringify({
        merkleRoot:      merkleRootHex,
        leafCount,
        contractAddress: nodeRuntime.config.contractAddress,
        countyId:        nodeRuntime.config.countyId,
      })

      const resp = httpClient.sendRequest(nodeRuntime, {
        url:     `${nodeRuntime.config.apiUrl}/api/oracle/commit-root`,
        method:  "POST" as const,
        body:    new TextEncoder().encode(payload),
        headers: { "Content-Type": "application/json" },
      }).result()

      const respBody = JSON.parse(new TextDecoder().decode(resp.body))
      nodeRuntime.log(`[node] Bridge response: ${JSON.stringify(respBody)}`)

      return { ok: respBody.success ? 1n : 0n }
    },
    consensusMedianAggregation<bigint>()
  )()

  runtime.log(`Batch committed  →  root: ${merkleRootHex}`)
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

const initWorkflow = (runtime: Runtime<Config>): void => {
  const cron = new CronCapability<Config>()
  handler(cron.trigger(runtime.config.schedule), onCronTrigger)
}

export async function main() {
  const runner = await Runner.newRunner<Config>()
  await runner.run(initWorkflow)
}
