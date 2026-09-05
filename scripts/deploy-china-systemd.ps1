param(
    [string]$RemoteEnvPath = "D:\project\PulseGuard\remote.env",
    [string]$RemoteRoot = "/opt/pulseguard",
    [string]$PublicHost = "",
    [int]$ApiPort = 8787,
    [int]$RelayPort = 9443,
    [string]$PyPiMirror = "https://mirrors.ivolces.com/pypi/simple",
    [string]$UvVersion = "0.9.30",
    [switch]$SkipTests,
    [switch]$SkipFrontendBuild,
    [switch]$SkipBrowserInstall,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $true
}

function Read-RemoteEnv {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "remote env file not found: $Path"
    }

    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith("#") -or -not $text.Contains("=")) {
            continue
        }
        $key, $value = $text.Split("=", 2)
        $values[$key.Trim()] = $value.Trim()
    }
    foreach ($required in @("user", "ip")) {
        if ([string]::IsNullOrWhiteSpace($values[$required])) {
            throw "remote env missing '$required': $Path"
        }
    }
    return $values
}

function Invoke-Native {
    param([Parameter(Mandatory = $true)][string]$File, [Parameter(Mandatory = $true)][string[]]$Arguments)

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

foreach ($tool in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "missing required command: $tool"
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$remote = Read-RemoteEnv -Path $RemoteEnvPath
if ([string]::IsNullOrWhiteSpace($PublicHost)) {
    $PublicHost = $remote.ip
}
$target = "$($remote.user)@$($remote.ip)"
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("pulseguard-deploy-" + [guid]::NewGuid().ToString("N"))
$payload = Join-Path $tempRoot "pulseguard-source.tar.gz"
$remoteDeployId = [guid]::NewGuid().ToString("N")
$remotePayload = "/tmp/pulseguard-source-$remoteDeployId.tar.gz"
$remoteScript = "/tmp/apply-china-systemd-deploy-$remoteDeployId.sh"
$originalProcessEnvironment = @{}
foreach ($name in @("DISPLAY", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "PULSEGUARD_SSH_PASSWORD")) {
    $originalProcessEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
if (-not [string]::IsNullOrWhiteSpace($remote.password)) {
    $askPassPath = Join-Path $tempRoot "ssh-askpass.cmd"
    $askPassBody = "@echo off`r`npowershell.exe -NoProfile -NonInteractive -Command `"[Console]::Out.Write(`$env:PULSEGUARD_SSH_PASSWORD)`"`r`n"
    [IO.File]::WriteAllText($askPassPath, $askPassBody, [Text.Encoding]::ASCII)
    $env:DISPLAY = "pulseguard-ssh-askpass"
    $env:SSH_ASKPASS = $askPassPath
    $env:SSH_ASKPASS_REQUIRE = "force"
    $env:PULSEGUARD_SSH_PASSWORD = $remote.password
}
try {
    Write-Host "Checking remote host capacity, services, listeners, images, and recent logs."
    Invoke-Native -File "ssh" -Arguments @(
        "-o", "ConnectTimeout=12",
        "-o", "NumberOfPasswordPrompts=1",
        $target,
        "hostname; uname -sr; df -h / /tmp /opt; systemctl is-active pulseguard.service pulseguard-relay.service; ss -ltnp | grep -E ':(8787|9443) ' || true; docker image inspect node:22-bookworm ghcr.io/astral-sh/uv:0.9.30 mcr.microsoft.com/playwright/python:v1.49.1-noble python:3.12-alpine --format '{{.RepoTags}} {{.Id}}' 2>/dev/null || true; /opt/pulseguard/.venv/bin/python -m playwright --version 2>/dev/null || true; find /root/.cache/ms-playwright -maxdepth 1 -type d -name 'chromium-*' -printf '%f`n' 2>/dev/null | tail -n 5; journalctl -u pulseguard.service -u pulseguard-relay.service --since=-3min --reverse --no-pager -n 80"
    )
    if ($PreflightOnly) {
        return
    }
    Push-Location $repoRoot
    try {
        if (-not $SkipTests) {
            Invoke-Native -File "uv" -Arguments @("run", "python", "-m", "unittest", "discover", "-s", "backend/tests", "-p", "test_*.py", "-v")
        }
        if (-not $SkipFrontendBuild) {
            Push-Location "frontend"
            try {
                Invoke-Native -File "npm" -Arguments @("run", "build")
            }
            finally {
                Pop-Location
            }
        }
        $archiveItems = @(
            "assets",
            "backend",
            "docs",
            "frontend",
            "scripts",
            ".dockerignore",
            ".env.example",
            "AGENTS.md",
            "DESIGN.md",
            "Dockerfile",
            "Dockerfile.worker",
            "Dockerfile.worker-updater",
            "LICENSE",
            "NOTICE",
            "PRODUCT.md",
            "PulseGuard_PRD.md",
            "README.en.md",
            "README.md",
            "docker-compose.relay-worker.yml",
            "docker-compose.relay.yml",
            "docker-compose.worker.build.yml",
            "docker-compose.worker.yml",
            "docker-compose.yml",
            "pyproject.toml",
            "uv.lock"
        ) | Where-Object { Test-Path -LiteralPath $_ }
        Invoke-Native -File "tar" -Arguments (@(
            "--exclude=frontend/node_modules",
            "--exclude=node_modules",
            "--exclude=.pytest_cache",
            "--exclude=*.log",
            "-czf",
            $payload
        ) + $archiveItems)
    }
    finally {
        Pop-Location
    }

    $browserInstall = if ($SkipBrowserInstall) { "0" } else { "1" }
    $remoteApply = @"
set -euo pipefail
remote_root="$RemoteRoot"
payload="$remotePayload"
remote_script="$remoteScript"
public_host="$PublicHost"
api_port="$ApiPort"
relay_port="$RelayPort"
pypi_mirror="$PyPiMirror"
uv_version="$UvVersion"
browser_install="$browserInstall"
stamp=`$(date +%Y%m%d-%H%M%S)
work="/tmp/pulseguard-release-`${stamp}"

if [ "`$(id -u)" != "0" ]; then
  echo "deploy-china-systemd requires a root SSH user because it writes systemd units and may install Playwright system dependencies" >&2
  exit 1
fi

mkdir -p "`${remote_root}" /opt/pulseguard-backups "`${work}" "`${remote_root}/data" "`${remote_root}/reports"
tar -xzf "`${payload}" -C "`${work}"

uv_bootstrap="`${work}/.uv-bootstrap"
python3 -m venv "`${uv_bootstrap}"
"`${uv_bootstrap}/bin/python" -m pip install -i "`${pypi_mirror}" "uv==`${uv_version}"
python3 -m venv "`${remote_root}/.venv"
cd "`${work}"
VIRTUAL_ENV="`${remote_root}/.venv" "`${uv_bootstrap}/bin/uv" sync \
  --active \
  --frozen \
  --no-dev \
  --no-install-project \
  --default-index "`${pypi_mirror}"

active_db="`${remote_root}/data/pulseguard.db"
backup_db="/opt/pulseguard-backups/pulseguard-before-deploy-`${stamp}.db"
preflight_db="`${work}/pulseguard-migration-preflight.db"
if [ -f "`${active_db}" ]; then
  "`${remote_root}/.venv/bin/python" - "`${active_db}" "`${backup_db}" "`${preflight_db}" <<'PY'
import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

source_path, backup_path, preflight_path = map(Path, sys.argv[1:4])
with sqlite3.connect(source_path, timeout=30) as source, sqlite3.connect(backup_path) as backup:
    source.backup(backup)
shutil.copy2(backup_path, preflight_path)
with backup_path.open("rb") as backup_file:
    digest = hashlib.file_digest(backup_file, "sha256").hexdigest()
print(f"database backup ready: {backup_path} sha256={digest}")
PY
  PULSEGUARD_DB_PATH="`${preflight_db}" \
  PULSEGUARD_DATA_DIR="`${work}/migration-data" \
  PULSEGUARD_REPORTS_DIR="`${work}/migration-reports" \
  PULSEGUARD_BACKUPS_DIR="`${work}/migration-backups" \
  PULSEGUARD_RUNNER_TOKEN_KEY_FILE="`${remote_root}/data/runner-token.key" \
  PYTHONPATH="`${work}/backend" \
    "`${remote_root}/.venv/bin/python" - <<'PY'
from app import storage

storage.migrate_projects(apply=True)
storage.init_db()
print("database migration preflight passed")
PY
fi
if [ "`${browser_install}" = "1" ]; then
  if [ "`$(id -u)" = "0" ]; then
    "`${remote_root}/.venv/bin/python" -m playwright install --with-deps chromium
  else
    "`${remote_root}/.venv/bin/python" -m playwright install chromium
  fi
fi

systemctl stop pulseguard.service pulseguard-relay.service 2>/dev/null || true
source_backup="/opt/pulseguard-backups/source-before-deploy-`${stamp}.tar.gz"
cutover_backup_db="/opt/pulseguard-backups/pulseguard-before-cutover-`${stamp}.db"
cutover_db="`${work}/pulseguard-cutover.db"
tar -czf "`${source_backup}" -C "`${remote_root}" --exclude=data --exclude=reports --exclude=.venv . 2>/dev/null || true
echo "source backup ready: `${source_backup}"
if [ -f "`${active_db}" ]; then
  "`${remote_root}/.venv/bin/python" - "`${active_db}" "`${cutover_backup_db}" "`${cutover_db}" <<'PY'
import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

source_path, backup_path, cutover_path = map(Path, sys.argv[1:4])
with sqlite3.connect(source_path, timeout=30) as source, sqlite3.connect(backup_path) as backup:
    source.execute("PRAGMA busy_timeout=30000")
    source.backup(backup)
shutil.copy2(backup_path, cutover_path)
with backup_path.open("rb") as backup_file:
    digest = hashlib.file_digest(backup_file, "sha256").hexdigest()
print(f"cutover database backup ready: {backup_path} sha256={digest}")
PY
  set +e
  PULSEGUARD_DB_PATH="`${cutover_db}" \
  PULSEGUARD_DATA_DIR="`${work}/cutover-data" \
  PULSEGUARD_REPORTS_DIR="`${work}/cutover-reports" \
  PULSEGUARD_BACKUPS_DIR="`${work}/cutover-backups" \
  PULSEGUARD_RUNNER_TOKEN_KEY_FILE="`${remote_root}/data/runner-token.key" \
  PYTHONPATH="`${work}/backend" \
    "`${remote_root}/.venv/bin/python" - <<'PY'
import sqlite3

from app import storage

result = storage.migrate_projects(apply=True)
storage.init_db()
with sqlite3.connect(storage.DB_PATH, timeout=30) as conn:
    checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
print(f"database cutover checkpoint passed: {checkpoint}")
print(f"database cutover migration passed: state={result['state']} applied={result['applied']}")
PY
  migration_status=`$?
  set -e
  if [ "`${migration_status}" -ne 0 ]; then
    echo "database cutover migration failed; active database was not replaced" >&2
    systemctl start pulseguard.service pulseguard-relay.service 2>/dev/null || true
    exit "`${migration_status}"
  fi
  rm -f "`${active_db}-wal" "`${active_db}-shm" "`${cutover_db}-wal" "`${cutover_db}-shm"
  mv "`${cutover_db}" "`${active_db}"
fi
find "`${remote_root}" -mindepth 1 -maxdepth 1 ! -name data ! -name reports ! -name .venv -exec rm -rf {} +
tar -xzf "`${payload}" -C "`${remote_root}"
cat > "`${remote_root}/.env" <<EOF_ENV
PULSEGUARD_HOST=0.0.0.0
PULSEGUARD_PORT=`${api_port}
PULSEGUARD_DATA_DIR=`${remote_root}/data
PULSEGUARD_REPORTS_DIR=`${remote_root}/reports
PULSEGUARD_STATIC_DIR=`${remote_root}/frontend/dist
PULSEGUARD_ALERT_DETAIL_BASE_URL=http://`${public_host}:`${api_port}
PULSEGUARD_RELAY_ENABLED=true
PULSEGUARD_RELAY_PUBLIC_HOST=`${public_host}
PULSEGUARD_RELAY_PUBLIC_PORT=`${relay_port}
PULSEGUARD_RELAY_INTERNAL_HOST=127.0.0.1
PULSEGUARD_RELAY_INTERNAL_LISTEN_HOST=127.0.0.1
PULSEGUARD_RELAY_CONTROL_HOST=127.0.0.1
PULSEGUARD_RELAY_CONTROL_URL=http://127.0.0.1:18000
PYTHONPATH=`${remote_root}/backend
EOF_ENV

cat > /etc/systemd/system/pulseguard.service <<EOF_UNIT
[Unit]
Description=PulseGuard API
After=network.target

[Service]
WorkingDirectory=`${remote_root}
EnvironmentFile=`${remote_root}/.env
Environment=PYTHONPATH=`${remote_root}/backend
ExecStart=`${remote_root}/.venv/bin/uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port `${api_port}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF_UNIT

cat > /etc/systemd/system/pulseguard-relay.service <<EOF_UNIT
[Unit]
Description=PulseGuard Relay
After=network.target pulseguard.service

[Service]
WorkingDirectory=`${remote_root}
EnvironmentFile=`${remote_root}/.env
Environment=PYTHONPATH=`${remote_root}/backend
ExecStart=`${remote_root}/.venv/bin/python -m app.relay_server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF_UNIT

systemctl daemon-reload
systemctl enable pulseguard.service pulseguard-relay.service >/dev/null
systemctl restart pulseguard.service pulseguard-relay.service
systemctl is-active pulseguard.service pulseguard-relay.service
rm -rf "`${work}" "`${payload}" "`${remote_script}"
"@
    $remoteApplyPath = Join-Path $tempRoot "apply-china-systemd-deploy.sh"
    $remoteApply | Set-Content -LiteralPath $remoteApplyPath -Encoding ASCII

    Write-Host "Uploading source payload to $target. OpenSSH may prompt for the remote password."
    Invoke-Native -File "ssh" -Arguments @($target, "mkdir -p '$RemoteRoot'")
    Invoke-Native -File "scp" -Arguments @($payload, "${target}:$remotePayload")
    Invoke-Native -File "scp" -Arguments @($remoteApplyPath, "${target}:$remoteScript")
    Invoke-Native -File "ssh" -Arguments @($target, "bash '$remoteScript'")

    $remoteUrl = "http://$PublicHost`:$ApiPort"
    $deadline = (Get-Date).AddSeconds(120)
    do {
        try {
            $health = Invoke-RestMethod -Method GET -Uri "$remoteUrl/api/health" -TimeoutSec 5
            $settings = Invoke-RestMethod -Method GET -Uri "$remoteUrl/api/settings" -TimeoutSec 10
            $relay = Invoke-RestMethod -Method GET -Uri "$remoteUrl/api/relay/status" -TimeoutSec 10
            if ($health.status -eq "ok" -and $settings -and $relay) {
                Write-Host "PulseGuard is healthy: $remoteUrl"
                return
            }
        }
        catch {
            Start-Sleep -Seconds 3
        }
    } while ((Get-Date) -lt $deadline)

    throw "PulseGuard did not become healthy within 120 seconds: $remoteUrl"
}
finally {
    foreach ($name in $originalProcessEnvironment.Keys) {
        $value = $originalProcessEnvironment[$name]
        if ($null -eq $value) {
            [Environment]::SetEnvironmentVariable($name, $null, "Process")
        }
        else {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
