# PulseGuard Operations Reference

This document covers local development, browser lifecycle, distributed runners, Relay security, worker updates, token rotation, networking, troubleshooting, data boundaries, and API endpoints.

Start with the project [README](../README.en.md) for positioning, capabilities, and quick setup.

## Local development

Backend:

```bash
uv sync
uv run python -m playwright install chromium
uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787 --reload
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

The frontend defaults to `http://127.0.0.1:5173`. To override the port and backend proxy:

```powershell
cd frontend
$env:VITE_DEV_API_TARGET="http://127.0.0.1:8787"
npm run dev -- --host 127.0.0.1 --port 5175
```

## Browser lifecycle

UI tasks support `chromium`, `firefox`, and `webkit`.

Key settings:

- `enabled_browser_types`: browser types users may select;
- `prewarmed_browser_types`: types prewarmed on startup or settings changes;
- `browser_pool_sizes`: empty `BrowserContext` pool per browser type;
- `browser_recycle_after_runs`: recycle threshold for long-lived browser processes;
- `similar_failure_retention_count`: rolling detail retention for repeated failures;
- `trace_artifacts_enabled`: optional failure Trace capture, disabled by default.

Each prewarmed type keeps one Playwright browser process and an independent pool of empty contexts. A UI run leases a context/page exclusively, closes the context afterward, and lets the pool replenish it.

Page inspection treats the outermost `cursor:pointer` hit area as an interactive candidate and limits repeated low-stability component families. Repeated card, name, city/mileage, price, and similar fields prefer a repeated-region plus relative-field selector. The candidate menu shows only selectable selector values; PulseGuard retains and validates their live match counts internally. Count rules created from repeated regions default to “at least 1” to avoid false failures when a dynamic list changes length. By default inspection analyzes only the DOM loaded naturally by the page; it does not scroll to trigger lazy loading or infinite lists. When post-scroll content is required, the task setup script must perform that scroll explicitly. Element-count assertions wait within the task timeout for their count condition to remain stable instead of sampling only once immediately after initial page load.

Runner selection is resolved before browser-type selection. Multiple runners and browser types produce a runner × browser matrix of run records. Unsupported combinations are recorded as Runner failures rather than target failures.

## Runner model

PulseGuard always includes the built-in `local` runner. Remote workers only:

- expose authenticated health and execution endpoints;
- execute tasks dispatched by the main node;
- return results and evidence;
- report version, build SHA, image, and installed browser types.

Workers do not run the scheduler, write the main database, or send business alerts.

### Direct worker

The main node must reach:

```text
http://<worker>:8788
```

Add the worker address and token in the main console, then test the connection.

### Relay worker

Relay mode is intended for workers that cannot expose inbound ports. The worker connects outbound to the main Relay, and the main service reaches the worker through an internal Docker network.

Enable Relay on the main node:

```bash
export PULSEGUARD_RELAY_PUBLIC_HOST="<public host or IP>"
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

The default public TCP port is `9443`. To use `443`:

```bash
export PULSEGUARD_RELAY_PUBLIC_PORT=443
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

## Relay security

The main node creates a self-signed TLS certificate under `data/relay/` and exposes its server fingerprint. A worker verifies the fingerprint before sending its Relay token.

Existing Relay deployments do not silently regenerate missing certificates. Restore `data/relay/` from backup to preserve fingerprint pinning.

Credential rules:

- the database stores only the Relay-token hash;
- worker tokens are encrypted with Fernet;
- the default encryption key file is `runner-token.key`;
- backups and migrations must include that key;
- generated deployment commands contain sensitive credentials and should be handled accordingly.

The Relay control plane defaults to the internal-only address:

```text
http://pulseguard-relay-internal:18000
```

Do not publish that port. Database state invalidation is authoritative; control-plane revocation only accelerates stale-session termination.

Relay protections include connection limits, hello timeouts, frame-size limits, disabled WebSocket compression, invalid-auth backoff, heartbeat timeouts, per-session stream limits, and internal-port quarantine.

Relevant environment variables:

```text
PULSEGUARD_RELAY_MAX_PUBLIC_CONNECTIONS
PULSEGUARD_RELAY_HELLO_TIMEOUT_SECONDS
PULSEGUARD_RELAY_MAX_FRAME_BYTES
PULSEGUARD_RELAY_AUTH_BACKOFF_BASE_SECONDS
PULSEGUARD_RELAY_AUTH_BACKOFF_MAX_SECONDS
PULSEGUARD_RELAY_STREAM_IDLE_TIMEOUT_SECONDS
PULSEGUARD_RELAY_PORT_QUARANTINE_SECONDS
```

## Worker deployment

Source mode:

```bash
uv run python -m backend.app.worker --host 0.0.0.0 --port 8788
```

With metadata:

```bash
uv run python -m backend.app.worker --host 0.0.0.0 --port 8788 --name office-worker-1 --region office-lan
```

Docker example:

```bash
git clone <your-pulseguard-repository-url>
cd PulseGuard
export PULSEGUARD_WORKER_NAME="$(hostname)"
export PULSEGUARD_WORKER_REGION="default"
export COMPOSE_PROJECT_NAME="pulseguard-worker"
export COMPOSE_PROFILES="updater"
export PULSEGUARD_WORKER_UPDATER_URL="http://pulseguard-worker-updater:8790"
docker compose -f docker-compose.worker.yml -f docker-compose.worker.build.yml up --build -d
docker update --restart unless-stopped pulseguard-worker pulseguard-worker-updater
docker logs --tail 80 pulseguard-worker
```

If source access is unavailable, build and publish a worker image from another environment:

```bash
export PULSEGUARD_WORKER_IMAGE="registry.example.com/pulseguard-worker:local"
export PULSEGUARD_WORKER_UPDATE_IMAGE="registry.example.com/pulseguard-worker:local"
export PULSEGUARD_WORKER_ALLOWED_IMAGE_PREFIXES="registry.example.com/pulseguard-worker"
docker compose -f docker-compose.worker.yml up -d
```

`PULSEGUARD_WORKER_ALLOWED_IMAGE_PREFIXES` is validated on repository boundaries rather than as a raw string prefix. A normal entry allows tags and digests only for that exact repository. Use an explicit namespace entry such as `registry.example.com/team/*` only when every repository below it is trusted; prefix lookalikes are rejected.

## Controlled worker updates

The updater profile mounts the Docker socket but only updates PulseGuard services declared in Compose. It does not accept arbitrary commands.

The update path:

1. pulls or locates the target image;
2. recreates the worker and Relay client when applicable;
3. preserves runner credentials and fingerprint configuration;
4. checks `/api/worker/health`;
5. attempts rollback when validation fails.

## Token inspection and rotation

Show the current worker token:

```bash
docker compose -f docker-compose.worker.yml exec pulseguard-worker python -m app.worker --show-token
```

Rotate and restart:

```bash
docker compose -f docker-compose.worker.yml exec pulseguard-worker python -m app.worker --rotate-token
docker compose -f docker-compose.worker.yml restart pulseguard-worker
```

When the container is stopped:

```bash
docker compose -f docker-compose.worker.yml run --rm pulseguard-worker python -m app.worker --rotate-token
```

After rotating a direct-worker token, update the credential in the main console and test the connection. For Relay-token rotation, regenerate and execute the deployment command.

## Run grouping and scheduling

- one multi-runner trigger uses a shared `run_group_id`;
- multiple runners and browser types generate matrix records;
- any target failure on an available runner fails the aggregate run;
- unavailable runners are recorded with `failure_kind=runner` and do not affect target health;
- each task owns its round-robin cursor;
- the scheduler keeps a 30-second misfire grace period.

## Network requirements

- direct mode: the main node must reach `/api/worker/health` and `/api/worker/run`; prefer HTTPS, while new or changed HTTP/bare-host runners require explicit insecure-transport acknowledgement and existing saved HTTP runners migrate as acknowledged;
- Relay mode: only the public Relay TCP port is required;
- worker APIs use `Authorization: Bearer <token>`; direct HTTP does not encrypt the token, task payload, or environment variables and is only suitable for an acknowledged trusted network;
- direct workers do not need the main-node address;
- Relay clients connect outbound;
- screenshots, optional Trace files, and response bodies are returned as base64 JSON payloads;
- oversized evidence is reported as an error and never treated as success.

## Troubleshooting

- **Connection test fails:** verify reachability, firewall rules, address, port, and listen interface.
- **Relay remains connecting:** inspect Relay and relay-client logs, fingerprint, and the public port.
- **Relay authentication fails:** regenerate the command and redeploy; the previous token is invalid.
- **Runner unavailable:** verify the stored address and token.
- **Run shows a Runner failure:** inspect worker state, browser installation, dependencies, and logs before treating it as a target issue.

## Validation

Quick verification (backend tests, frontend behavior tests and type checks):

```bash
uv run --frozen python scripts/verify.py quick
uv run --frozen python scripts/verify.py quick --backend-pattern test_runner.py
```

Full verification (also builds production assets, checks bundle budgets and runs a real browser smoke workflow):

```bash
uv run --frozen python -m playwright install chromium
uv run --frozen python scripts/verify.py full
```

Backend-only checks use `uv run --frozen python scripts/test_backend.py`, optionally with `--pattern test_runner.py`. This entry point isolates data, database, reports and backups before importing the app, initializes a temporary database, and discards inherited instance paths, tokens and node roles. Tests must never read the live `data/pulseguard.db`.

Full verification requires browser tests to execute; missing Chromium is a failure. Quick verification only skips browser tests when the executable is absent, and still fails for other launch errors. Each step reports its duration, result and log location. The `ui` mode runs only the build and browser smoke test. The smoke workflow starts a temporary instance, creates and debugs a check, inspects run history, switches projects and exercises trend and candidate selection with the keyboard, retaining screenshots and a trace. It needs no live instance or external probe target.

CI runs full verification for pull requests and existing push branches. Pull requests do not publish images. Use targeted checks during development and full verification before submitting changes.

Lockfile check:

```powershell
uv lock --check
```

Deployment changes the running instance. Run `.\scripts\deploy.ps1` separately; it is not a validation command.

## Script entry points

Generic/API task:

```python
async def check(ctx):
    response = await ctx.request()
    ctx.assert_status(response, 200)
```

UI setup:

```python
async def setup(ctx):
    page = await ctx.new_page()
    await page.goto(ctx.entry_url)
    return page
```

Custom Python scripts are trusted local capabilities, not a security sandbox.

## Project isolation and one-time migration

- Check, run, trend, status, metrics, project-alert, and project-bundle requests require `X-PulseGuard-Project-Id`. Missing scope returns 400 and an unknown project returns 404.
- Archived projects remain readable, while writes, execution, reruns, imports, and version restores return 409. The scheduler loads checks from active projects only.
- Read-only tokens must include at least one project and `/api/read-only/snapshot` validates both the token scope and project header.
- `pulseguard.config` v4 contains global settings, runners, and members only. `pulseguard.project` v1 contains the selected project's checks and alert settings only.

The application does not mutate a legacy database during startup. Run the read-only preflight first:

```powershell
uv run python scripts/migrate-projects.py
```

After an application-consistent backup and during the release write pause, apply explicitly:

```powershell
uv run python scripts/migrate-projects.py --apply
```

The script creates an online SQLite backup under `data/backups/`, checks its integrity, and reports table counts and SHA-256 values before and after migration. A failed migration rolls back as one transaction. Rollback requires both the previous application version and the pre-migration database backup; old and new versions must not share a database.

## Data boundaries

- SQLite data lives under `data/`;
- online backups live under `data/backups/`; `database_backup_retention` counts all retained copies, including the newly created one. The next backup creation prunes by file time. Restore sources and pre-restore safety copies are protected, so a restore can retain two copies when the limit is one;
- deployment and data-sync recovery points use `scripts/deployment_backups.py` under `/opt/pulseguard-backups/managed-<id>/`. They share the online-backup retention setting but count separately. Only `complete`, after deployment health and data acceptance, prunes older completed points. See the [backup retention decision](decisions/2026-09-15-backup-retention.md);
- restoring pauses new admissions and scheduling, then waits up to 30 seconds for accepted work; timeout returns 409 without restoring. Safety backup, restore, initialization, and failure compensation share exclusive maintenance access; client cancellation does not release admission protection early;
- audited queries use read-only SQLite snapshots while writes are serialized; retention archives and deletes terminal runs in bounded atomic batches, preserves unfinished runs, and can resume without double-counting;
- restoration is main-node-only; failed compensation or runtime settings reload returns 503 and keeps run admissions paused until operator recovery/configuration repair and restart. API/UI inspection previews are diagnostics, not persisted run admissions;
- **Operations → Database status** reports the database file, WAL, and reclaimable free pages without modifying data; compaction still requires a backup and a write-pause maintenance window;
- trend rollups use the `trend_rollups` aggregation table;
- screenshots, Trace, response bodies, and archived summaries live under `reports/`;
- common secrets, authorization headers, cookies, and read-only tokens are redacted from public outputs;
- historical trend backfill is disabled by default.

The console does not support phone viewports; H5 probe capabilities are unchanged. The editor runtime and workers are local, on-demand resources without a public CDN dependency. See [performance scope and verification](performance-optimization.md).

Before deployment, inspect the SQLite runtime with `uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"`. Prefer an [official WAL-reset fix](https://sqlite.org/wal.html#walresetbug): 3.51.3+ or the documented 3.50.7 / 3.44.6 backports. Do not run external writable/checkpoint tools or multiple application processes against the active database.

To backfill historical trends once:

```text
PULSEGUARD_TREND_BACKFILL_ON_STARTUP=true
```

Disable it again after the one-time startup.

## Console request timings

The main console GET endpoints (`health`, `projects`, `settings`, `project-alert-settings`, `runtime`, `checks`, `runners`, `runs-page`, `overview`, `status-page`, `maintenance`) return `Server-Timing` and a generated `X-Request-ID`. Requests taking at least 500 ms produce a `slow_api_request` JSON log with fixed route, status, durations, project ID and counts; query strings, headers, SQL and business payloads are excluded.

`server` ends at response headers and excludes browser/network time and waiting before ASGI entry. `dispatch`, present on health/page/overview/status/maintenance handlers, includes dependency resolution and thread dispatch. Database gate/writer waits, connection/setup/finalization, pagination count/fetch and overview summary/trends have separate timings. These phases can overlap and must not be added together. Correlate long-tail browser timings with the request ID, server log and host I/O/CPU metrics. See the [performance investigation and local verification](reviews/2026-09-14-volcano-api-performance.md).

## API reference

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Application health |
| `GET /api/maintenance` | Instance-wide maintenance notice: `enabled`, `title`, `message`, `starts_at`, `ends_at`; no project header required |
| `GET /api/status-page` | Current project status, summary, incidents and maintenance, without overview trend computation |
| `GET /api/runs-page` / `GET /api/overview` | Current project exact pagination and overview |
| `GET /api/projects` / `POST /api/projects` | List and create projects |
| `GET /api/database-health` | Read-only database capacity and free-page health |
| `GET /api/metrics.json` | JSON metrics |
| `GET /api/metrics` | Prometheus metrics |
| `GET /api/read-only/snapshot` | Token-protected read-only snapshot |
| `GET /api/project-alert-settings` / `PUT /api/project-alert-settings` | Current-project execution alerts |
| `GET /api/config/export` | Export a `pulseguard.config` v4 system bundle |
| `GET /api/project-config/export` | Export a `pulseguard.project` v1 project bundle |
| `GET /api/runners` / `POST /api/runners` | Runner list and direct-runner creation |
| `GET /api/relay/status` | Relay status, address, and fingerprint |
| `POST /api/runners/provision` | Create a Relay runner and one-time deployment command |
| `POST /api/runners/{runner_id}/provision/regenerate` | Rotate a Relay deployment command |
| `POST /api/runners/{runner_id}/update` | Request a controlled worker update |
| `GET /api/worker/health` | Worker health |
| `POST /api/worker/run` | Worker execution |
| `POST /api/worker/browser-types/install` | Install enabled browser types |
| `GET /api/runs?runner_id=...` | Filter runs by runner |
| `GET /api/runs?run_group_id=...` | Inspect one multi-runner trigger |
| `GET /api/runs/{id}` | Run snapshot details |
| `GET /api/monitoring-trends` | Overall trends |
| `GET /api/checks/{id}/trend` | Per-task trend |
| `POST /api/heartbeats/{key}` | Passive heartbeat ingestion |
