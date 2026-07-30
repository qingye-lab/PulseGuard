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

Backend:

```bash
uv run python -m unittest discover -s backend/tests -p 'test_*.py' -v
```

Frontend:

```bash
cd frontend
npm run build
```

Docker and lockfile:

```powershell
uv lock --check
.\scripts\deploy.ps1
```

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

## Data boundaries

- SQLite data lives under `data/`;
- online backups live under `data/backups/`;
- trend rollups use the `trend_rollups` aggregation table;
- screenshots, Trace, response bodies, and archived summaries live under `reports/`;
- common secrets, authorization headers, cookies, and read-only tokens are redacted from public outputs;
- historical trend backfill is disabled by default.

To backfill historical trends once:

```text
PULSEGUARD_TREND_BACKFILL_ON_STARTUP=true
```

Disable it again after the one-time startup.

## API reference

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Application health |
| `GET /api/metrics.json` | JSON metrics |
| `GET /api/metrics` | Prometheus metrics |
| `GET /api/read-only/snapshot` | Token-protected read-only snapshot |
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
