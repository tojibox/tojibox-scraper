# togibox-scraper

Data pipeline and on-chain oracle for [Togibox](https://github.com/togibox) — a port of ZoneProof to **GIWA** (OP-Stack EVM L2, testnet "GIWA Sepolia", chain ID 91342). Scrapes Wake County / Raleigh NC parcel and rezoning-petition data, detects changes, and commits Merkle-batched change events to `TogiboxOracle.sol` on GIWA.

## What it does

- **Parcel scraping** — daily pull of ~434k Wake County parcel records from ArcGIS (owner, assessed value, land class, geometry)
- **Zoning scraping** — 6-hourly pull of Raleigh zoning districts (ArcGIS), grouped by `ZONE_CASE` into rezoning petitions
- **Petition scraping** — 4-hourly HTML scrape of Raleigh Planning's active + finalized rezoning case pages (data ArcGIS doesn't have: status, address, PDF links)
- **Spatial enrichment** — one-time/periodic job that intersects petition polygons against parcel geometries to populate `rezoning_petitions.pins[]`
- **Change detection** — SHA-256 fingerprints per record; only changed records get written to `change_events`
- **Merkle-commit pipeline** — reads pending `change_events`, builds a Merkle tree, calls `TogiboxOracle.commitBatch()` on GIWA, marks events committed in Postgres
- **On-chain oracle contract** — `TogiboxOracle.sol`: stores Merkle roots per batch, indexes affected parcel PINs, exposes `verify(leaf, proof, batchId)` for anyone to cryptographically confirm a change event is real

## Tech stack

- **Python / FastAPI** — scrapers, thin API wrapper (`/health`, manual run triggers)
- **psycopg2** — direct PostgreSQL connection (Supabase), avoids the REST API's 1k-row limit
- **APScheduler** — cron-based scraper scheduling (standalone process)
- **Shapely** — spatial intersection for PIN enrichment (STRtree index)
- **Loguru** — structured logging
- **Node.js / ethers.js** — Merkle tree construction + on-chain commit (`pipeline/processor.mjs`)
- **Hardhat** — Solidity compilation + deployment to GIWA Sepolia
- **Solidity 0.8.20** — `TogiboxOracle.sol`

## Folder structure

```
togibox-scraper/
├── main.py                  # FastAPI wrapper — /health + manual /run/* trigger endpoints
├── config.py                 # Settings loaded from .env
├── requirements.txt           # Python dependencies
├── .env                        # Local secrets (never committed)
├── .env.example                 # Environment variable template
│
├── scrapers/
│   ├── db.py                   # psycopg2 connection factory + batch upsert helpers
│   ├── utils.py                 # fingerprinting, date conversion, local raw-data storage
│   ├── parcel_scraper.py        # Wake County ArcGIS parcel scraper
│   ├── zoning_scraper.py        # Raleigh zoning ArcGIS scraper (grouped by ZONE_CASE)
│   ├── petition_scraper.py      # Raleigh Planning HTML rezoning-case scraper
│   ├── spatial_enrichment.py    # Shapely STRtree PIN enrichment for petitions
│   └── scheduler.py             # APScheduler cron runner — `python -m scrapers.scheduler`
│
├── migrations/                  # Versioned SQL, applied via direct psycopg2/psql connection
│   ├── 001_extensions.sql
│   ├── 002_parcels.sql
│   ├── 003_rezoning_petitions.sql
│   ├── 004_fingerprints.sql
│   ├── 005_change_events.sql
│   ├── 006_parcel_history.sql
│   ├── 007_merkle_batches.sql
│   └── 008_oracle_runs.sql
│
├── contracts/
│   ├── src/TogiboxOracle.sol    # Batch-commit + Merkle-proof verification contract
│   ├── hardhat.config.js        # giwaSepolia network (chain 91342)
│   ├── package.json
│   └── scripts/deploy.js        # `npx hardhat run scripts/deploy.js --network giwaSepolia`
│
├── pipeline/
│   ├── processor.mjs             # Canonical batch committer — Merkle tree → commitBatch() on GIWA
│   └── package.json
│
├── cre/                           # Optional/future Chainlink CRE DON-consensus workflow
│   ├── workflow.ts                #   (not the primary path — see file header comment)
│   ├── config.json
│   └── project.yaml
│
└── data/                          # Local raw-data snapshots (gitignored)
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in DB_HOST/PORT/USER/PASSWORD/NAME, GIWA_PRIVATE_KEY, etc.
```

## Running the scrapers

```bash
# One-off manual runs
python -m scrapers.parcel_scraper
python -m scrapers.zoning_scraper
python -m scrapers.petition_scraper
python -m scrapers.spatial_enrichment   # after parcels + petitions have geometry

# Cron scheduler (long-running process — separate from the API)
python -m scrapers.scheduler

# API — /health + manual /run/* triggers
python main.py
# or: uvicorn main:app --reload --port 8001
```

## Running migrations

No migration runner script is included yet — apply the SQL files in order against your Supabase/Postgres instance, e.g.:

```bash
for f in migrations/*.sql; do
  psql "$DATABASE_URL" -f "$f"
done
```

(`DATABASE_URL` — build from `DB_HOST/PORT/USER/PASSWORD/NAME` in `.env`, or use the Supabase dashboard's connection string.)

**If you're pointing at a fresh, empty Postgres project**, run all of `001`–`009` in order.

**If you're reusing an existing project that already has this schema** (e.g.
the same Supabase project as a prior Hedera-based version of this project) —
`001`–`008` are `CREATE TABLE` statements that will fail with "relation
already exists" against a DB that already has them. Only run `009_add_giwa_columns.sql`,
which is purely additive (new nullable columns + indexes, `IF NOT EXISTS`
throughout) and safe to layer on top without touching or renaming anything
the existing schema/pipeline already relies on:

```bash
psql "$DATABASE_URL" -f migrations/009_add_giwa_columns.sql
```

## Compiling and deploying the contract

```bash
cd contracts
npm install
npm run compile
# Requires GIWA_PRIVATE_KEY (funded GIWA Sepolia testnet key) and ORACLE_ADDRESS in ../.env
npm run deploy:testnet
# Copy the printed contract address into ../.env as TOGIBOX_ORACLE_ADDRESS
```

## Running the commit pipeline

```bash
cd pipeline
npm install
npm run process
```

Reads pending `change_events` from the oracle API (`GET /api/oracle/pending-events` — served by the sibling `togibox-api` repo), builds a Merkle tree, submits `commitBatch()` to `TogiboxOracle.sol` on GIWA, and marks the events committed in Postgres.

## Environment variables

| Variable | Description |
|----------|-------------|
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | Direct Postgres connection (Supabase) |
| `COUNTY_ID` | County identifier, default `raleigh_nc` |
| `ARCGIS_PARCELS_URL` / `ARCGIS_ZONING_URL` | ArcGIS REST endpoints (defaults point at Wake County / Raleigh) |
| `ARCGIS_PAGE_SIZE` | ArcGIS pagination size, default 2000 |
| `SUPABASE_BATCH` | Rows per INSERT/UPSERT batch, default 100 |
| `GIWA_RPC_URL` | GIWA Sepolia RPC endpoint, default `https://sepolia-rpc.giwa.io/` |
| `GIWA_PRIVATE_KEY` | Deployer / pipeline signer private key (hex) — **never commit a real value** |
| `ORACLE_ADDRESS` | EOA authorized to call `commitBatch()` (deploy-time constructor arg) |
| `TOGIBOX_ORACLE_ADDRESS` | Deployed `TogiboxOracle.sol` address — filled in after deploy |
| `BATCH_SIZE` | Max change events per pipeline commit batch, default 100 |
| `API_PORT` | Port for `main.py` FastAPI app, default 8001 |
| `REZONING_CRON_SCHEDULE` / `PARCEL_CRON_SCHEDULE` / `PETITION_CRON_SCHEDULE` | Crontab expressions for `scrapers/scheduler.py` |
| `LOG_LEVEL` | Loguru log level, default `INFO` |

## Known limitations / TODOs

- No migration runner script (`migrations/runner.py`-style) — apply `migrations/*.sql` manually in order for now.
- The CRE workflow (`cre/workflow.ts`) POSTs to `${apiUrl}/api/oracle/commit-root`, which is not implemented in any sibling repo yet. It's optional/future scaffolding — the tested path is `pipeline/processor.mjs`.
- `contracts/`, `pipeline/`, and `cre/` all depend on `TogiboxOracle.sol`'s compiled artifact (`contracts/artifacts/src/TogiboxOracle.sol/TogiboxOracle.json`) — run `npm run compile` in `contracts/` before running `pipeline/processor.mjs`.
