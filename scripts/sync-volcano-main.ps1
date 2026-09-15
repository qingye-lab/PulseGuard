param(
    [string]$RemoteEnvPath = "D:\project\PulseGuard\remote.env",
    [string]$LocalServiceUrl = "http://127.0.0.1:8787",
    [string]$RemoteRoot = "/opt/pulseguard",
    [ValidateSet("auto", "systemd", "docker")]
    [string]$DeploymentMode = "auto",
    [switch]$IncludeReports
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

function Get-CheckFingerprint {
    param([Parameter(Mandatory = $true)][object[]]$Checks)

    $fields = @(
        "id",
        "name",
        "type",
        "enabled",
        "interval_seconds",
        "timeout_ms",
        "entry_url",
        "viewport_mode",
        "method",
        "headers_json",
        "body",
        "assertions_json",
        "setup_script",
        "script",
        "tags",
        "alert_policy_json",
        "runner_selection_mode",
        "runner_ids",
        "browser_selection_mode",
        "browser_types"
    )
    $items = $Checks |
        Sort-Object id |
        ForEach-Object {
            $definition = [ordered]@{}
            foreach ($field in $fields) {
                $definition[$field] = $_.$field
            }
            $definition | ConvertTo-Json -Depth 20 -Compress
        }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($items -join "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToBase64String($sha.ComputeHash($bytes))
    }
    finally {
        $sha.Dispose()
    }
}

$LocalServiceUrl = $LocalServiceUrl.TrimEnd("/")
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$dataDir = Join-Path $repoRoot "data"
$reportsDir = Join-Path $repoRoot "reports"
$remote = Read-RemoteEnv -Path $RemoteEnvPath
$target = "$($remote.user)@$($remote.ip)"
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("pulseguard-sync-" + [guid]::NewGuid().ToString("N"))
$stage = Join-Path $tempRoot "stage"
$payload = Join-Path $tempRoot "pulseguard-main-data.tar.gz"
$syncId = [guid]::NewGuid().ToString("N")
$remotePayload = "/tmp/pulseguard-main-data-$syncId.tar.gz"
$remoteScript = "/tmp/apply-main-data-sync-$syncId.sh"
$remoteBackupTool = "/tmp/pulseguard-deployment-backups-$syncId.py"

New-Item -ItemType Directory -Force -Path (Join-Path $stage "data") | Out-Null
try {
    $health = Invoke-RestMethod -Method GET -Uri "$LocalServiceUrl/api/health" -TimeoutSec 10
    if ($health.status -ne "ok") {
        throw "local PulseGuard health is not ok"
    }
    $localChecks = @(Invoke-RestMethod -Method GET -Uri "$LocalServiceUrl/api/checks" -TimeoutSec 30)
    $localCheckFingerprint = Get-CheckFingerprint -Checks $localChecks

    $backup = Invoke-RestMethod -Method POST -Uri "$LocalServiceUrl/api/database-backups" -TimeoutSec 60
    $backupPath = Join-Path $dataDir ("backups\" + $backup.filename)
    if (-not (Test-Path -LiteralPath $backupPath)) {
        throw "database backup was created but not found: $backupPath"
    }

    Copy-Item -LiteralPath $backupPath -Destination (Join-Path $stage "data\pulseguard.db") -Force

    $runnerKey = Join-Path $dataDir "runner-token.key"
    if (Test-Path -LiteralPath $runnerKey) {
        Copy-Item -LiteralPath $runnerKey -Destination (Join-Path $stage "data\runner-token.key") -Force
    }

    $relayDir = Join-Path $dataDir "relay"
    if (Test-Path -LiteralPath $relayDir) {
        Copy-Item -LiteralPath $relayDir -Destination (Join-Path $stage "data\relay") -Recurse -Force
    }

    if ($IncludeReports -and (Test-Path -LiteralPath $reportsDir)) {
        Copy-Item -LiteralPath $reportsDir -Destination (Join-Path $stage "reports") -Recurse -Force
    }

    tar -czf $payload -C $stage .
    if ($LASTEXITCODE -ne 0) {
        throw "tar failed with exit code $LASTEXITCODE"
    }

    $remoteApply = @"
set -euo pipefail
cd "$RemoteRoot"
stamp=`$(date +%Y%m%d-%H%M%S)
mode="$DeploymentMode"
if [ "`${mode}" = "auto" ]; then
  if systemctl list-unit-files --type=service --no-legend pulseguard.service 2>/dev/null | grep -q '^pulseguard\.service'; then
    mode="systemd"
  else
    mode="docker"
  fi
fi
mkdir -p /opt/pulseguard-backups data reports
python3 -c 'import sys; assert sys.version_info >= (3, 11), "backup tooling requires Python 3.11+"'
if [ "`${mode}" = "systemd" ]; then
  systemctl stop pulseguard.service pulseguard-relay.service || true
  if systemctl is-active --quiet pulseguard.service || systemctl is-active --quiet pulseguard-relay.service; then
    echo "services must be stopped before creating a sync recovery point" >&2
    exit 1
  fi
else
  docker compose -f docker-compose.yml -f docker-compose.relay.yml stop pulseguard pulseguard-relay
fi
if ! python3 "$remoteBackupTool" create --id "$syncId" --app-root "$RemoteRoot" --include-reports; then
  if [ "`${mode}" = "systemd" ]; then
    systemctl start pulseguard.service pulseguard-relay.service
  else
    docker compose -f docker-compose.yml -f docker-compose.relay.yml up -d --no-build
  fi
  exit 1
fi
work="/opt/pulseguard-sync-`${stamp}"
rm -rf "`${work}"
mkdir -p "`${work}"
tar -xzf "$remotePayload" -C "`${work}"
if [ -f "`${work}/data/pulseguard.db" ]; then
  rm -f data/pulseguard.db-wal data/pulseguard.db-shm
  cp -f "`${work}/data/pulseguard.db" data/pulseguard.db
fi
if [ -f "`${work}/data/runner-token.key" ]; then
  cp -f "`${work}/data/runner-token.key" data/runner-token.key
  chmod 600 data/runner-token.key
fi
if [ -d "`${work}/data/relay" ]; then
  rm -rf data/relay
  cp -a "`${work}/data/relay" data/relay
fi
if [ -d "`${work}/reports" ]; then
  cp -a "`${work}/reports/." reports/
fi
rm -rf "`${work}" "$remotePayload"
if [ "`${mode}" = "systemd" ]; then
  systemctl restart pulseguard.service pulseguard-relay.service
  systemctl is-active pulseguard.service pulseguard-relay.service
else
  docker compose -f docker-compose.yml -f docker-compose.relay.yml up -d --no-build
  docker compose -f docker-compose.yml -f docker-compose.relay.yml ps
fi
"@
    $remoteApplyPath = Join-Path $tempRoot "apply-main-data-sync.sh"
    $remoteApply | Set-Content -LiteralPath $remoteApplyPath -Encoding ASCII

    Write-Host "Created local DB backup: $($backup.filename)"
    Write-Host "Uploading data payload to $target. OpenSSH may prompt for the remote password."
    Invoke-Native -File "scp" -Arguments @($payload, "${target}:$remotePayload")
    Invoke-Native -File "scp" -Arguments @($remoteApplyPath, "${target}:$remoteScript")
    Invoke-Native -File "scp" -Arguments @((Join-Path $PSScriptRoot "deployment_backups.py"), "${target}:$remoteBackupTool")
    Invoke-Native -File "ssh" -Arguments @($target, "bash $remoteScript")

    $remoteUrl = "http://$($remote.ip):8787"
    $deadline = (Get-Date).AddSeconds(120)
    do {
        try {
            $remoteHealth = Invoke-RestMethod -Method GET -Uri "$remoteUrl/api/health" -TimeoutSec 5
        }
        catch {
            Start-Sleep -Seconds 3
            continue
        }
        if ($remoteHealth.status -eq "ok") {
            $remoteChecks = @(Invoke-RestMethod -Method GET -Uri "$remoteUrl/api/checks" -TimeoutSec 30)
            $remoteCheckFingerprint = Get-CheckFingerprint -Checks $remoteChecks
            if ($remoteChecks.Count -ne $localChecks.Count -or $remoteCheckFingerprint -ne $localCheckFingerprint) {
                throw "remote check definitions do not match local backup after sync"
            }
            $remoteSettings = Invoke-RestMethod -Method GET -Uri "$remoteUrl/api/settings" -TimeoutSec 30
            $retention = [int]$remoteSettings.database_backup_retention
            Invoke-Native -File "ssh" -Arguments @($target, "python3 '$remoteBackupTool' complete --id '$syncId' --retention $retention")
            Write-Host "Verified check definitions: $($remoteChecks.Count)"
            Write-Host "Remote PulseGuard is healthy: $remoteUrl"
            return
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)

    throw "remote PulseGuard did not become healthy within 120 seconds"
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
