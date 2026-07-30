// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * TogiboxOracle
 * -------------
 * On-chain receipt book for rezoning change events in Wake County / Raleigh NC.
 *
 * Deployed on GIWA (OP-Stack EVM L2). Called by the CRE workflow (or the
 * standalone pipeline processor) after consensus is reached on a batch of
 * rezoning change events.
 *
 * What it stores:
 *   - A Merkle root for each committed batch of rezoning events
 *   - Which parcel PINs were affected in each batch
 *   - GIWA block timestamp (network time, not server time)
 *
 * What it enables:
 *   - getPinHistory(pin)       → all batch IDs that touched this parcel
 *   - getBatch(batchId)        → Merkle root + timestamp for a batch
 *   - verify(leaf, proof, id)  → cryptographic proof an event is real
 *
 * Trust model:
 *   Only the authorised oracle address (set to the pipeline/CRE workflow EOA)
 *   can call commitBatch(). Owner can rotate the oracle address.
 */
contract TogiboxOracle {

    // ── Data structures ────────────────────────────────────────────────────────

    struct Batch {
        bytes32 merkleRoot;    // root of the Merkle tree for this batch
        uint256 leafCount;     // number of change events in this batch
        uint256 timestamp;     // block.timestamp when committed
        uint256 fromEventSeq;  // first change_event sequence in this batch
        uint256 toEventSeq;    // last change_event sequence in this batch
        string  countyId;      // e.g. "raleigh_nc"
    }

    // ── Storage ────────────────────────────────────────────────────────────────

    mapping(uint256 => Batch)   public batches;
    uint256                     public batchCount;

    // keccak256(abi.encodePacked(pin)) → ordered list of batch IDs
    mapping(bytes32 => uint256[]) private _pinBatches;

    address public owner;
    address public oracle;   // authorised submitter (pipeline/CRE workflow EOA)

    // ── Events ─────────────────────────────────────────────────────────────────

    event BatchCommitted(
        uint256 indexed batchId,
        bytes32         merkleRoot,
        uint256         leafCount,
        uint256         timestamp,
        string          countyId
    );

    event ParcelRezoningRecorded(
        bytes32 indexed pinHash,
        uint256 indexed batchId,
        string          petitionNumber
    );

    event OracleUpdated(address indexed previous, address indexed next);

    // ── Constructor ────────────────────────────────────────────────────────────

    constructor(address _oracle) {
        owner  = msg.sender;
        oracle = _oracle;
    }

    // ── Modifiers ──────────────────────────────────────────────────────────────

    modifier onlyOracle() {
        require(
            msg.sender == oracle || msg.sender == owner,
            "TogiboxOracle: caller is not the oracle"
        );
        _;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "TogiboxOracle: caller is not the owner");
        _;
    }

    // ── Write ─────────────────────────────────────────────────────────────────

    /**
     * @notice Commit a new batch of rezoning change events.
     *
     * Called by the pipeline processor (or CRE workflow) after consensus.
     *
     * @param merkleRoot      Root of the Merkle tree built over leaf hashes
     * @param leafCount       Number of change events in this batch
     * @param fromEventSeq    First change_event DB sequence in batch
     * @param toEventSeq      Last change_event DB sequence in batch
     * @param countyId        County identifier string (e.g. "raleigh_nc")
     * @param pinHashes       keccak256 of each unique PIN affected in this batch
     * @param petitionNumbers Petition number string for each pinHash entry
     *
     * @return batchId  The ID assigned to this batch (starts at 0)
     */
    function commitBatch(
        bytes32          merkleRoot,
        uint256          leafCount,
        uint256          fromEventSeq,
        uint256          toEventSeq,
        string  calldata countyId,
        bytes32[] calldata pinHashes,
        string[]  calldata petitionNumbers
    ) external onlyOracle returns (uint256 batchId) {
        require(leafCount > 0,              "TogiboxOracle: empty batch");
        require(merkleRoot != bytes32(0),   "TogiboxOracle: zero merkle root");
        require(
            pinHashes.length == petitionNumbers.length,
            "TogiboxOracle: array length mismatch"
        );

        batchId = batchCount;
        unchecked { batchCount++; }

        batches[batchId] = Batch({
            merkleRoot:    merkleRoot,
            leafCount:     leafCount,
            timestamp:     block.timestamp,
            fromEventSeq:  fromEventSeq,
            toEventSeq:    toEventSeq,
            countyId:      countyId
        });

        for (uint256 i = 0; i < pinHashes.length; ) {
            _pinBatches[pinHashes[i]].push(batchId);
            emit ParcelRezoningRecorded(pinHashes[i], batchId, petitionNumbers[i]);
            unchecked { i++; }
        }

        emit BatchCommitted(batchId, merkleRoot, leafCount, block.timestamp, countyId);
    }

    // ── Read ──────────────────────────────────────────────────────────────────

    /**
     * @notice Get all batch IDs that contain a rezoning event for a given PIN.
     * @param pin  Raw parcel PIN string (e.g. "1703743741")
     */
    function getPinHistory(string calldata pin)
        external view
        returns (uint256[] memory)
    {
        return _pinBatches[keccak256(abi.encodePacked(pin))];
    }

    /**
     * @notice Get all batch IDs using a pre-hashed PIN.
     * @param pinHash  keccak256(abi.encodePacked(pin))
     */
    function getPinHistoryByHash(bytes32 pinHash)
        external view
        returns (uint256[] memory)
    {
        return _pinBatches[pinHash];
    }

    /**
     * @notice Get batch metadata by ID.
     */
    function getBatch(uint256 batchId)
        external view
        returns (
            bytes32 merkleRoot,
            uint256 leafCount,
            uint256 timestamp,
            uint256 fromEventSeq,
            uint256 toEventSeq,
            string  memory countyId
        )
    {
        Batch storage b = batches[batchId];
        return (
            b.merkleRoot,
            b.leafCount,
            b.timestamp,
            b.fromEventSeq,
            b.toEventSeq,
            b.countyId
        );
    }

    /**
     * @notice Verify that a leaf is included in a committed batch.
     *
     * Anyone can call this to verify a change event from Supabase is real.
     *
     * Leaf construction (must match pipeline/processor.mjs):
     *   leaf = keccak256(abi.encodePacked(eventId, eventType, petitionNumber, detectedAt, afterStateHash))
     *
     * @param leaf      The leaf hash to verify
     * @param proof     Sibling hashes from the Merkle proof path
     * @param batchId   The batch the leaf claims to belong to
     */
    function verify(
        bytes32          leaf,
        bytes32[] calldata proof,
        uint256          batchId
    ) external view returns (bool) {
        require(batchId < batchCount, "TogiboxOracle: batch does not exist");
        return _verifyProof(proof, batches[batchId].merkleRoot, leaf);
    }

    // ── Admin ─────────────────────────────────────────────────────────────────

    /**
     * @notice Rotate the authorised oracle address.
     * Used when the pipeline/CRE workflow EOA changes.
     */
    function setOracle(address _oracle) external onlyOwner {
        emit OracleUpdated(oracle, _oracle);
        oracle = _oracle;
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "TogiboxOracle: zero address");
        owner = newOwner;
    }

    // ── Internal ──────────────────────────────────────────────────────────────

    /**
     * Standard binary Merkle proof verification.
     * Pairs are sorted before hashing so leaf order doesn't matter.
     */
    function _verifyProof(
        bytes32[] memory proof,
        bytes32          root,
        bytes32          leaf
    ) internal pure returns (bool) {
        bytes32 computed = leaf;
        for (uint256 i = 0; i < proof.length; ) {
            bytes32 sibling = proof[i];
            computed = computed < sibling
                ? keccak256(abi.encodePacked(computed, sibling))
                : keccak256(abi.encodePacked(sibling, computed));
            unchecked { i++; }
        }
        return computed == root;
    }
}
