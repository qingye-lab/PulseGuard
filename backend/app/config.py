from __future__ import annotations

import os
import secrets
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.getenv("PULSEGUARD_DATA_DIR", PROJECT_ROOT / "data")).resolve()
REPORTS_DIR = Path(os.getenv("PULSEGUARD_REPORTS_DIR", PROJECT_ROOT / "reports")).resolve()
DB_PATH = Path(os.getenv("PULSEGUARD_DB_PATH", DATA_DIR / "pulseguard.db")).resolve()
BACKUPS_DIR = Path(os.getenv("PULSEGUARD_BACKUPS_DIR", DATA_DIR / "backups")).resolve()
STATIC_DIR = Path(os.getenv("PULSEGUARD_STATIC_DIR", PROJECT_ROOT / "frontend" / "dist")).resolve()
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE_ENV_VALUES

HOST = os.getenv("PULSEGUARD_HOST", "127.0.0.1")
PORT = int(os.getenv("PULSEGUARD_PORT", "8787"))
TIMEZONE = os.getenv("TZ", "Asia/Shanghai")
ALERT_DETAIL_BASE_URL = (os.getenv("PULSEGUARD_ALERT_DETAIL_BASE_URL") or "http://localhost:8787").rstrip("/")
APP_VERSION = os.getenv("PULSEGUARD_VERSION", "0.1.0").strip() or "0.1.0"
BUILD_SHA = os.getenv("PULSEGUARD_BUILD_SHA", "unknown").strip() or "unknown"
# Deployment branding, not application data. Empty by default so a public build
# carries no team or company identity; internal deployments set it explicitly.
TEAM_NAME = os.getenv("PULSEGUARD_TEAM_NAME", "").strip()
NODE_ROLE = os.getenv("PULSEGUARD_NODE_ROLE", "main").strip().lower() or "main"
WORKER_TOKEN_FILE = Path(os.getenv("PULSEGUARD_WORKER_TOKEN_FILE", DATA_DIR / "worker-token")).resolve()
RUNNER_HEALTH_POLL_SECONDS = max(10, int(os.getenv("PULSEGUARD_RUNNER_HEALTH_POLL_SECONDS", "30")))
RELAY_ENABLED = _env_flag("PULSEGUARD_RELAY_ENABLED")
RELAY_PUBLIC_HOST = os.getenv("PULSEGUARD_RELAY_PUBLIC_HOST", "").strip()
RELAY_PUBLIC_PORT = int(os.getenv("PULSEGUARD_RELAY_PUBLIC_PORT", "9443"))
RELAY_INTERNAL_HOST = os.getenv("PULSEGUARD_RELAY_INTERNAL_HOST", "pulseguard-relay").strip() or "pulseguard-relay"
RELAY_INTERNAL_LISTEN_HOST = os.getenv("PULSEGUARD_RELAY_INTERNAL_LISTEN_HOST", RELAY_INTERNAL_HOST).strip() or RELAY_INTERNAL_HOST
RELAY_INTERNAL_PORT_START = int(os.getenv("PULSEGUARD_RELAY_INTERNAL_PORT_START", "18001"))
RELAY_INTERNAL_PORT_END = int(os.getenv("PULSEGUARD_RELAY_INTERNAL_PORT_END", "18100"))
RELAY_CONTROL_HOST = os.getenv("PULSEGUARD_RELAY_CONTROL_HOST", "127.0.0.1").strip() or "127.0.0.1"
RELAY_CONTROL_PORT = int(os.getenv("PULSEGUARD_RELAY_CONTROL_PORT", "18000"))
RELAY_CONTROL_URL = (
    os.getenv("PULSEGUARD_RELAY_CONTROL_URL", f"http://{RELAY_INTERNAL_HOST}:{RELAY_CONTROL_PORT}").strip().rstrip("/")
)
RELAY_DEPLOY_COMMAND_TTL_HOURS = max(1, int(os.getenv("PULSEGUARD_RELAY_DEPLOY_COMMAND_TTL_HOURS", "24")))
RELAY_DIR = Path(os.getenv("PULSEGUARD_RELAY_DIR", DATA_DIR / "relay")).resolve()
RELAY_CERT_FILE = Path(os.getenv("PULSEGUARD_RELAY_CERT_FILE", RELAY_DIR / "relay.crt")).resolve()
RELAY_KEY_FILE = Path(os.getenv("PULSEGUARD_RELAY_KEY_FILE", RELAY_DIR / "relay.key")).resolve()
RELAY_CONTROL_TOKEN_FILE = Path(os.getenv("PULSEGUARD_RELAY_CONTROL_TOKEN_FILE", RELAY_DIR / "control-token")).resolve()
RELAY_MAX_BODY_BYTES = max(1024 * 1024, int(os.getenv("PULSEGUARD_RELAY_MAX_BODY_BYTES", str(20 * 1024 * 1024))))
RELAY_MAX_CONCURRENT_STREAMS = max(1, int(os.getenv("PULSEGUARD_RELAY_MAX_CONCURRENT_STREAMS", "2")))
RELAY_STREAM_IDLE_TIMEOUT_SECONDS = max(30, int(os.getenv("PULSEGUARD_RELAY_STREAM_IDLE_TIMEOUT_SECONDS", "900")))
_RELAY_PORT_QUARANTINE_DEFAULT_SECONDS = max(10 * 60, RELAY_STREAM_IDLE_TIMEOUT_SECONDS + 60)
RELAY_PORT_QUARANTINE_SECONDS = max(
    _RELAY_PORT_QUARANTINE_DEFAULT_SECONDS,
    int(os.getenv("PULSEGUARD_RELAY_PORT_QUARANTINE_SECONDS", str(_RELAY_PORT_QUARANTINE_DEFAULT_SECONDS))),
)
RELAY_MAX_PUBLIC_CONNECTIONS = max(1, int(os.getenv("PULSEGUARD_RELAY_MAX_PUBLIC_CONNECTIONS", "256")))
RELAY_HELLO_TIMEOUT_SECONDS = max(1, int(os.getenv("PULSEGUARD_RELAY_HELLO_TIMEOUT_SECONDS", "10")))
RELAY_MAX_FRAME_BYTES = max(4096, int(os.getenv("PULSEGUARD_RELAY_MAX_FRAME_BYTES", str(1024 * 1024))))
RELAY_AUTH_BACKOFF_BASE_SECONDS = max(0.0, float(os.getenv("PULSEGUARD_RELAY_AUTH_BACKOFF_BASE_SECONDS", "0.25")))
RELAY_AUTH_BACKOFF_MAX_SECONDS = max(
    RELAY_AUTH_BACKOFF_BASE_SECONDS,
    float(os.getenv("PULSEGUARD_RELAY_AUTH_BACKOFF_MAX_SECONDS", "5")),
)
TREND_BACKFILL_ON_STARTUP = _env_flag("PULSEGUARD_TREND_BACKFILL_ON_STARTUP")


def relay_control_token() -> str:
    env_token = os.getenv("PULSEGUARD_RELAY_CONTROL_TOKEN", "").strip()
    if env_token:
        return env_token
    RELAY_CONTROL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if RELAY_CONTROL_TOKEN_FILE.exists():
        token = RELAY_CONTROL_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = f"pgrc_{secrets.token_urlsafe(32)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(RELAY_CONTROL_TOKEN_FILE, flags, 0o600)
    except FileExistsError:
        for _ in range(50):
            token = RELAY_CONTROL_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
            time.sleep(0.02)
        raise RuntimeError("relay control token file is empty")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(token + "\n")
    return token


def _new_worker_token() -> str:
    return f"pgrn_{secrets.token_urlsafe(32)}"


def _worker_token() -> tuple[str, str]:
    env_token = os.getenv("PULSEGUARD_WORKER_TOKEN", "").strip()
    if env_token:
        return env_token, "env"
    if NODE_ROLE != "worker":
        return "", "unset"
    WORKER_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if WORKER_TOKEN_FILE.exists():
        token = WORKER_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token, str(WORKER_TOKEN_FILE)
    token = _new_worker_token()
    WORKER_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    try:
        WORKER_TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token, str(WORKER_TOKEN_FILE)


WORKER_NAME = os.getenv("PULSEGUARD_WORKER_NAME", "worker").strip() or "worker"
WORKER_RUNNER_ID = os.getenv("PULSEGUARD_WORKER_RUNNER_ID", os.getenv("PULSEGUARD_RUNNER_ID", WORKER_NAME)).strip() or WORKER_NAME
WORKER_ADDRESS = os.getenv("PULSEGUARD_WORKER_ADDRESS", "").strip()
WORKER_REGION = os.getenv("PULSEGUARD_WORKER_REGION", "local").strip() or "local"
WORKER_PRINT_TOKEN = _env_flag("PULSEGUARD_WORKER_PRINT_TOKEN", True)
WORKER_IMAGE = os.getenv("PULSEGUARD_WORKER_IMAGE", "").strip()
WORKER_UPDATE_IMAGE = os.getenv("PULSEGUARD_WORKER_UPDATE_IMAGE", WORKER_IMAGE).strip()
WORKER_UPDATER_URL = os.getenv("PULSEGUARD_WORKER_UPDATER_URL", "").strip().rstrip("/")
WORKER_TOKEN, WORKER_TOKEN_SOURCE = _worker_token()

SCREENSHOTS_DIR = REPORTS_DIR / "screenshots"
TRACES_DIR = REPORTS_DIR / "traces"
RESPONSES_DIR = REPORTS_DIR / "responses"


def ensure_runtime_dirs() -> None:
    for directory in (DATA_DIR, BACKUPS_DIR, REPORTS_DIR, SCREENSHOTS_DIR, TRACES_DIR, RESPONSES_DIR, RELAY_DIR):
        directory.mkdir(parents=True, exist_ok=True)
