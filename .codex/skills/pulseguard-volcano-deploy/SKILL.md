---
name: pulseguard-volcano-deploy
description: Deploy, repair, verify, or sync PulseGuard on the Volcano public main node. Use when the task mentions PulseGuard deployment, Volcano, 115.190.188.243, remote.env, Docker base image reuse, China-network deployment, systemd fallback, relay port 9443, or local-to-Volcano data sync.
---

# PulseGuard Volcano Deploy

## Defaults

- Treat the current PulseGuard repo root as the source to deploy.
- Read SSH target values from `D:\project\PulseGuard\remote.env`; use `user` and `ip`, but never echo or pass `password` on a command line.
- Treat `/opt/pulseguard` as the remote application root.
- Use `http://115.190.188.243:8787` as the public API/UI base URL and relay port `9443`.
- Keep local PulseGuard services running unless the user explicitly asks to stop them.
- Preserve remote `data/`, `reports/`, `.venv`, `data/relay/`, `runner-token.key`, backups, WAL files, and SHM files.
- Standard relay child-node deployments should enable the updater profile by default (`COMPOSE_PROFILES=updater` and `PULSEGUARD_WORKER_UPDATER_URL=http://pulseguard-worker-updater:8790`) unless the user explicitly disables updater.

## Preflight

1. Inspect local state:
   - `git status --short`
   - `git diff --stat`
   - `Dockerfile*`, `docker-compose*.yml`, `pyproject.toml`, `uv.lock`, and `frontend/package-lock.json`
2. Run proportional local gates before deploy unless a recent identical run is already known:
   - `uv run python -m unittest discover -s backend/tests -p 'test_*.py' -v`
   - from `frontend/`: `npm run build`
3. Probe the public remote API:
   - `/api/health`
   - `/api/settings`
   - `/api/checks`
   - `/api/runners`
   - `/api/relay/status`
4. Probe the remote host over SSH for disk, Docker, existing images, service status, ports, and recent logs.

Use OpenSSH `SSH_ASKPASS` or an interactive password prompt when no SSH key is available. Do not put the remote password in command arguments, process lists, logs, or final reports.

## Image Reuse Decision

Prefer Docker Compose only when required base images are already available or registry pulls are proven healthy. Required base images are:

- `node:22-bookworm`
- `ghcr.io/astral-sh/uv:0.9.30`
- `mcr.microsoft.com/playwright/python:v1.49.1-noble`
- `python:3.12-alpine` for worker updater images

Check with:

```sh
docker image inspect node:22-bookworm ghcr.io/astral-sh/uv:0.9.30 mcr.microsoft.com/playwright/python:v1.49.1-noble python:3.12-alpine
```

If only some bases exist, reuse what is available but do not force a cold Docker build through unreliable China-network registries. Prefer the systemd deployment path instead.

## Systemd Deployment

Use this path when Docker base images are missing, registry pulls are unreliable, or the remote is already running systemd:

```powershell
.\scripts\deploy-china-systemd.ps1 `
  -RemoteEnvPath "D:\project\PulseGuard\remote.env" `
  -PublicHost "115.190.188.243" `
  -RelayPort 9443
```

If local tests and frontend build were already run in the same turn, use `-SkipTests -SkipFrontendBuild`. If the remote already has the matching Playwright browser runtime installed, use `-SkipBrowserInstall` to avoid unnecessary downloads.

The deploy script must upload payloads and its apply script outside `/opt/pulseguard`, such as under `/tmp`. The remote apply step clears non-persistent entries inside `/opt/pulseguard`; putting the payload in that directory deletes it before extraction.

## Disk Recovery

### Deployment backup retention

For both standard and directed/incremental releases, use `scripts/deployment_backups.py`
instead of creating ad hoc backups. Stop instance and Relay writers, then call `create`
with a unique 32-character lowercase hexadecimal release ID and `--app-root`.
Use `--include-reports` for data sync. Recovery points include a compressed database,
full source, and runtime keys/Relay state; do not replace the source archive with a patch.
After health and data acceptance, call `complete` with the same ID and the current
`/api/settings` value of `database_backup_retention`. This counts the new recovery point
toward the limit and prunes only older completed managed groups. Never complete a failed
deployment. Pending, unknown, and legacy backups require explicit operator review.
See `docs/decisions/2026-09-15-backup-retention.md` for commands and recovery boundaries.

If the remote root filesystem is full, inspect first:

```sh
df -h / /tmp /opt
du -xh --max-depth=1 /tmp /var /opt 2>/dev/null | sort -h | tail
```

It is acceptable to remove clearly temporary, reproducible deployment or Playwright temp paths such as `/tmp/pulseguard-release-*`, `/tmp/pulseguard-source-*.tar.gz`, `/tmp/apply-china-systemd-deploy-*.sh`, or stale `/tmp/playwright-artifacts-*` after recording their size and age. Do not delete application data, reports, backups, runner keys, or relay state.

## Data Sync

Only sync local data after the Volcano service is healthy and the local service is also healthy:

```powershell
.\scripts\sync-volcano-main.ps1 `
  -RemoteEnvPath "D:\project\PulseGuard\remote.env" `
  -DeploymentMode auto
```

Use `-IncludeReports` only when screenshot, trace, and response-body history must move too.

## Verification

After deploy, verify all of these:

- `systemctl is-active pulseguard.service pulseguard-relay.service`
- `df -h / /tmp /opt`
- `journalctl -u pulseguard.service -u pulseguard-relay.service --since=-3min --reverse --no-pager -n 80`
- `GET http://115.190.188.243:8787/`
- `GET http://115.190.188.243:8787/members`
- `GET http://115.190.188.243:8787/api/health`
- `GET http://115.190.188.243:8787/api/settings`
- `GET http://115.190.188.243:8787/api/checks`
- `GET http://115.190.188.243:8787/api/runners`
- `GET http://115.190.188.243:8787/api/overview`
- `GET http://115.190.188.243:8787/api/relay/status`

Report endpoint status codes, check/runner counts, service status, disk free space, and any residual log errors.
