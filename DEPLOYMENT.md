# Deploying to Railway

This repo needs **two separate Railway services** from the same GitHub repo,
since it contains two independent long-running processes with different
runtimes (Python at the repo root, Node.js in `pipeline/`). Railway supports
this: create two services in one project, each pointing at
`tojibox/tojibox-scraper` with its own Root Directory and Start Command.

## Service 1 — scheduler (worker, no public networking)

Runs the scraper cron loop (`scrapers/scheduler.py`, APScheduler-based)
continuously in-process. No HTTP surface, so no domain/networking needed.

1. **New Service** → **Deploy from GitHub repo** → `tojibox/tojibox-scraper`.
2. **Settings**:
   - Root Directory: `/` (repo root — leave default)
   - A `Dockerfile` at the repo root handles the build and defaults to
     `python -m scrapers.scheduler` as its `CMD`, so no Start Command
     override is needed. (The Dockerfile also installs `libpq5` at build
     time — Railway's auto-detected Python builder doesn't include it,
     which `psycopg2-binary` needs at runtime.)
   - Networking: none needed — turn off/skip domain generation.
3. **Variables**:

   | Variable | Notes |
   |---|---|
   | `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | Shared Supabase project |
   | `COUNTY_ID` | `raleigh_nc` |
   | `ARCGIS_PARCELS_URL` / `ARCGIS_ZONING_URL` | Defaults are fine unless the source moved |
   | `REZONING_CRON_SCHEDULE` | `0 */6 * * *` |
   | `PARCEL_CRON_SCHEDULE` | `0 2 * * *` |
   | `PETITION_CRON_SCHEDULE` | `0 */4 * * *` |
   | `LOG_LEVEL` | `INFO` |

## Service 2 — merkle-committer (scheduled job)

Runs `pipeline/processor.mjs`: reads pending change events from tojibox-api,
builds a Merkle tree, and commits the batch to `TojiboxOracle.sol` on GIWA.
This should run periodically, not continuously — use Railway's **Cron Job**
deployment type rather than a persistent worker.

1. **New Service** → **Deploy from GitHub repo** → same repo again.
2. **Settings**:
   - Root Directory: `pipeline`
   - Deploy type: **Cron Job** (Railway dashboard has this as a service type)
   - Schedule: e.g. `0 * * * *` (hourly) — match or exceed `REZONING_CRON_SCHEDULE`'s cadence above
   - Start Command: `node processor.mjs`
3. **Variables**:

   | Variable | Notes |
   |---|---|
   | `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | Same shared Supabase project |
   | `GIWA_RPC_URL` | `https://sepolia-rpc.giwa.io/` |
   | `GIWA_PRIVATE_KEY` | Deployer/oracle wallet key — must be the address authorized to call `commitBatch()` |
   | `TOJIBOX_ORACLE_ADDRESS` | Deployed `TojiboxOracle` contract address |
   | `BATCH_SIZE` | `100` |
   | `ORACLE_API_URL` | **Required in production** — the tojibox-api Railway service's public URL (e.g. `https://tojibox-api-production.up.railway.app`). Without this it defaults to `localhost`, which will not reach anything once these are separate services. |

## Optional — manual-trigger API (Service 3)

`main.py` exposes `/health` and `POST /run/*` endpoints for manually
triggering a scrape outside the cron schedule. Not required for the pipeline
to function (the scheduler already runs scrapes automatically) — only add
this if you want that manual control surface exposed over HTTP.

- Root Directory: `/`
- Start Command (override the Dockerfile's default): `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Same DB/ArcGIS variables as Service 1.

## Not deployed here

`contracts/` and `cre/` are deploy tooling and optional/documentation-only
respectively, not runtime services.
