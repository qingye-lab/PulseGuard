# PulseGuard

[简体中文](./README.md) | [English](./README.en.md)

<p align="center">
  <img src="./assets/brand/pulseguard-brand-card.en.svg" alt="PulseGuard - Local and cross-network UI/API probe console" width="100%" />
</p>

<p align="center">
  <strong>Continuously check pages, APIs, and network targets from one console while keeping evidence that helps explain failures.</strong>
</p>

PulseGuard is a local-first **UI/API probing and lightweight monitoring console** for individuals, small teams, and internal environments.

The console targets desktop and laptop browsers; phone viewport compatibility is explicitly out of scope. H5 page mode and mobile target viewports for UI probes remain supported.

It combines scheduled checks, draft debugging, run history, failure evidence, health state, alerts, and distributed execution in a single-instance system. Typical targets include internal admin pages, login prerequisites, health endpoints, batch heartbeats, certificates, TCP/DNS, and basic network reachability.

PulseGuard is not a public status-page service, a full E2E test-management platform, or an incident/on-call system. It solves a narrower problem:

> When services are spread across local machines, LANs, and isolated networks, how can a team verify that they still work and distinguish a target failure from an execution-environment failure?

## Use cases

- Check internal pages that require login or setup context;
- Monitor APIs, health endpoints, and important response fields;
- Receive passive heartbeats from batch jobs, schedulers, or devices;
- Monitor TLS expiry, HTTP content, redirects, assets, TCP, and DNS;
- Coordinate local, LAN, and cross-network runners from one main node;
- Keep screenshots, response bodies, and optional Trace files for failed runs;
- Separate target failures from browser, runner, dependency, and database failures.

## Core capabilities

### UI and API probes

- Scheduled, manual, batch, and rerun execution;
- Structured UI steps, setup scripts, and multiple browser types;
- API request configuration, field preview, assertions, and advanced scripts;
- Draft runs that do not affect formal health or alert statistics.

### Trustworthy health state

PulseGuard does not treat every transient failure as an outage. The main transition is:

```text
healthy → suspected failure → failed → suspected recovery → healthy
```

It also models unknown, stale observation, and disabled states. Browser crashes, missing dependencies, runner unavailability, and database errors are classified as Runner failures rather than target failures.

### Evidence and trends

- Error summaries, screenshots, and response bodies;
- Optional Playwright Trace capture, disabled by default;
- 24-hour, 7-day, 30-day, and custom-range trends;
- Average successful latency, P95, P99, and failure counts;
- Rolling retention for repeated failures to prevent unbounded evidence growth.

### Distributed execution

PulseGuard supports three execution sources:

```text
main node
├── local: built-in local execution
├── direct worker: main node reaches a LAN worker directly
└── relay worker: worker connects outbound without exposing an inbound port
```

Multi-runner and multi-browser runs share a run group and retain runner state, browser type, failure source, duration, and evidence per result.

### Alerts and read-only outputs

- Project isolation for checks, runs, trends, anomaly cycles, versions, audit, and execution alerts;
- Project channels for execution alerts and global-only channels for system failures;
- Project-default, tag-level, and task-level execution alert policies;
- Cooldown and recovery notifications;
- Project-scoped JSON, Prometheus, and token-protected read-only snapshots;
- Separate system and project bundles with sanitized export and import preview.

## Quick start

Docker Compose is the recommended path:

```bash
git clone https://github.com/liyanqing90/PulseGuard.git
cd PulseGuard
docker compose up --build -d
```

The default published address is `0.0.0.0:8787`:

- Local: `http://127.0.0.1:8787`
- LAN: `http://<local IP>:8787`

Check the deployment:

```bash
docker compose ps
docker compose logs --tail 80 pulseguard
curl http://127.0.0.1:8787/api/health
```

To publish only on localhost:

```env
PULSEGUARD_PUBLISH_HOST=127.0.0.1
PULSEGUARD_HOST=0.0.0.0
PULSEGUARD_PORT=8787
PULSEGUARD_PUBLISH_PORT=8787
PULSEGUARD_ALERT_DETAIL_BASE_URL=http://127.0.0.1:8787
```

## Relay: outbound-only workers

Enable Relay on the main node:

```bash
export PULSEGUARD_RELAY_PUBLIC_HOST="<public host or IP>"
docker compose -f docker-compose.yml -f docker-compose.relay.yml up --build -d
```

Only the public TCP port `9443` is required by default. Workers do not publish `8788` on the host; the relay client connects outbound to the main node.

Relay uses self-signed TLS, server-fingerprint pinning, one-time deployment credentials, hashed/encrypted token storage, and an internal-only control network. See the [operations reference](./docs/operations-reference.en.md) for the complete model.

## Technology

| Layer | Technology |
| --- | --- |
| Backend | FastAPI, SQLite, Playwright Python, uv |
| Frontend | React, TypeScript, Vite, Ant Design |
| Runtime | Docker Compose or local processes |
| Storage | SQLite, `data/`, `reports/` |
| Remote execution | Direct workers and WebSocket Relay |

## Product boundaries

- Single-instance and local/LAN-first;
- SQLite by default, with no required external database;
- Workers execute tasks but do not own scheduling, alerts, or source-of-truth data;
- Custom Python scripts are trusted local capabilities, not a security sandbox;
- Evidence uses retention policies rather than permanent full-artifact storage;
- Recording is not the primary path; structured rules, templates, and setup scripts are preferred.

## Documentation

| Document | Contents |
| --- | --- |
| [Operations reference](./docs/operations-reference.en.md) | Local development, browser pools, runners, Relay, tokens, API, and troubleshooting |
| [中文运维参考](./docs/operations-reference.md) | Full Chinese operations reference |
| [China deployment](./docs/china-deployment.md) | Mirrors and dependency setup for mainland networks |
| [Volcano deployment](./docs/volcano-deployment.md) | Main-node, local-worker, and migration workflow |

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

Lockfile and deployment checks:

```powershell
uv lock --check
.\scripts\deploy.ps1
```

## Project layout

```text
backend/                    FastAPI backend, storage, runners, alerts, tests
frontend/                   React application, components, and styles
data/                       SQLite data
reports/                    screenshots, Trace, response bodies, summaries
docs/                       deployment, design, and operations docs
Dockerfile                  main-node image
docker-compose.yml          main-node deployment
docker-compose.relay.yml    Relay service
Dockerfile.worker           worker image
docker-compose.worker.yml   worker deployment
```

---

PulseGuard is created and maintained by [Qingye](https://github.com/liyanqing90).
