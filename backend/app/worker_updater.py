from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.getenv("PULSEGUARD_UPDATER_HOST", "0.0.0.0")
PORT = int(os.getenv("PULSEGUARD_UPDATER_PORT", "8790"))
TOKEN_FILE = Path(os.getenv("PULSEGUARD_WORKER_TOKEN_FILE", "/app/data/worker-token"))
STATUS_FILE = Path(os.getenv("PULSEGUARD_WORKER_UPDATE_STATUS_FILE", "/app/data/update-status.json"))
COMPOSE_FILE = os.getenv("PULSEGUARD_WORKER_COMPOSE_FILE", "/workspace/docker-compose.worker.yml")
WORKER_SERVICE = os.getenv("PULSEGUARD_WORKER_SERVICE", "pulseguard-worker")
WORKER_CONTAINER = os.getenv("PULSEGUARD_WORKER_CONTAINER", "pulseguard-worker")
RELAY_CLIENT_CONTAINER = os.getenv("PULSEGUARD_RELAY_CLIENT_CONTAINER", "pulseguard-relay-client")
UPDATE_SERVICES = tuple(
    service.strip()
    for service in os.getenv("PULSEGUARD_WORKER_UPDATE_SERVICES", WORKER_SERVICE).split(",")
    if service.strip()
) or (WORKER_SERVICE,)
DEFAULT_IMAGE = os.getenv("PULSEGUARD_WORKER_UPDATE_IMAGE") or os.getenv("PULSEGUARD_WORKER_IMAGE", "")
ALLOWED_PREFIXES = tuple(
    prefix.strip()
    for prefix in os.getenv("PULSEGUARD_WORKER_ALLOWED_IMAGE_PREFIXES", "pulseguard-worker").split(",")
    if prefix.strip()
)
HEALTH_URL = os.getenv("PULSEGUARD_WORKER_HEALTH_URL", "http://pulseguard-worker:8788/api/worker/health")
COMPOSE_PROJECT_NAME = os.getenv("COMPOSE_PROJECT_NAME", "pulseguard-worker")
_LOCK = threading.Lock()


def main() -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_status({"status": "idle", "message": "updater ready", "updated_at": _now()})
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"PulseGuard worker updater is ready on {HOST}:{PORT}", flush=True)
    server.serve_forever()


class Handler(BaseHTTPRequestHandler):
    server_version = "PulseGuardWorkerUpdater/1.0"

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path == "/health":
            self._json({"ok": True, "status": "ok"})
            return
        if self.path == "/status":
            self._json({"ok": True, "update": _status_with_runtime(_read_status())})
            return
        self._json({"detail": "not found"}, status=404)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path != "/update":
            self._json({"detail": "not found"}, status=404)
            return
        if _read_status().get("status") == "running":
            self._json({"detail": "update already running", "update": _read_status()}, status=409)
            return
        try:
            payload = self._read_body()
            target_image = _target_image(payload)
        except ValueError as exc:
            self._json({"detail": str(exc)}, status=400)
            return
        update_id = str(payload.get("update_id") or f"upd_{uuid.uuid4().hex[:12]}")
        force = bool(payload.get("force", False))
        status = {
            "status": "running",
            "update_id": update_id,
            "target_image": target_image,
            "started_at": _now(),
            "updated_at": _now(),
            "message": "update accepted",
        }
        _write_status(status)
        threading.Thread(target=_run_update, args=(update_id, target_image, force), daemon=True).start()
        self._json({"ok": True, "message": "update accepted", "update": status})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip()
        expected = _current_token()
        if not expected or token != expected:
            self._json({"detail": "invalid token"}, status=403)
            return False
        return True

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1024 * 1024))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return payload

    def _json(self, data: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _run_update(update_id: str, target_image: str, force: bool) -> None:
    previous_image = _current_container_image()
    try:
        _ensure_image_available(target_image)
        _compose_up(target_image)
        _wait_for_health()
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        if previous_image and previous_image != target_image and not force:
            try:
                _compose_up(previous_image)
                _wait_for_health()
                message = f"{message}; rolled back to {previous_image}"
            except Exception as rollback_exc:
                message = f"{message}; rollback failed: {rollback_exc}"
        _write_status(
            {
                "status": "failed",
                "update_id": update_id,
                "target_image": target_image,
                "previous_image": previous_image,
                "finished_at": _now(),
                "updated_at": _now(),
                "message": message[:1000],
            }
        )
        return
    _write_status(
        {
            "status": "succeeded",
            "update_id": update_id,
            "target_image": target_image,
            "previous_image": previous_image,
            "finished_at": _now(),
            "updated_at": _now(),
            "message": "worker updated",
        }
    )


def _compose_up(image: str) -> None:
    env = _compose_environment(image)
    env["PULSEGUARD_WORKER_IMAGE"] = image
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT_NAME
    _run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--no-deps", *UPDATE_SERVICES], env=env)


def _compose_environment(image: str) -> dict[str, str]:
    env = os.environ.copy()
    for container in (WORKER_CONTAINER, RELAY_CLIENT_CONTAINER):
        for key, value in _container_environment(container).items():
            env.setdefault(key, value)
    if "PULSEGUARD_RUNNER_ID" not in env and env.get("PULSEGUARD_WORKER_RUNNER_ID"):
        env["PULSEGUARD_RUNNER_ID"] = env["PULSEGUARD_WORKER_RUNNER_ID"]
    env["PULSEGUARD_WORKER_IMAGE"] = image
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT_NAME
    return env


def _wait_for_health(timeout_seconds: int = 90) -> None:
    import urllib.error
    import urllib.request

    token = _current_token()
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            request = urllib.request.Request(HEALTH_URL, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"worker health check failed after update: {last_error}")


def _target_image(payload: dict[str, Any]) -> str:
    image = str(payload.get("target_image") or DEFAULT_IMAGE).strip()
    if not image:
        raise ValueError("target image is not configured")
    if any(char.isspace() for char in image) or image.startswith("-"):
        raise ValueError("target image format is invalid")
    if ALLOWED_PREFIXES and not _image_repository_allowed(image, ALLOWED_PREFIXES):
        raise ValueError("target image is not allowed")
    return image


def _image_repository_allowed(image: str, allowed_entries: tuple[str, ...]) -> bool:
    repository = _image_repository(image)
    if not repository:
        return False
    for raw_entry in allowed_entries:
        entry = str(raw_entry or "").strip()
        namespace_allowed = entry.endswith("/") or entry.endswith("/*")
        if entry.endswith("/*"):
            entry = entry[:-1]
        allowed_repository = _image_repository(entry)
        if not allowed_repository:
            continue
        if namespace_allowed:
            if repository.startswith(f"{allowed_repository.rstrip('/')}/"):
                return True
        elif repository == allowed_repository:
            return True
    return False


def _image_repository(image: str) -> str:
    reference = str(image or "").strip().split("@", 1)[0]
    last_slash = reference.rfind("/")
    last_colon = reference.rfind(":")
    if last_colon > last_slash:
        reference = reference[:last_colon]
    return reference.rstrip("/")


def _ensure_image_available(image: str) -> None:
    try:
        _run(["docker", "pull", image])
    except Exception:
        if _image_exists(image):
            return
        raise


def _image_exists(image: str) -> bool:
    try:
        _run(["docker", "image", "inspect", image])
    except Exception:
        return False
    return True


def _current_token() -> str:
    env_token = os.getenv("PULSEGUARD_WORKER_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_container_image(container: str = WORKER_CONTAINER) -> str:
    try:
        output = _run(["docker", "inspect", "-f", "{{.Config.Image}}", container])
    except Exception:
        return ""
    return output.strip()


def _current_container_image_id(container: str = WORKER_CONTAINER) -> str:
    try:
        output = _run(["docker", "inspect", "-f", "{{.Image}}", container])
    except Exception:
        return ""
    return output.strip()


def _image_id(image: str) -> str:
    image = str(image or "").strip()
    if not image:
        return ""
    try:
        output = _run(["docker", "image", "inspect", "-f", "{{.Id}}", image])
    except Exception:
        return ""
    return output.strip()


def _update_available(current_image: str, target_image: str) -> bool:
    current_id = _current_container_image_id()
    target_id = _image_id(target_image)
    if current_id and target_id:
        return current_id != target_id
    return bool(current_image and target_image and current_image != target_image)


def _container_environment(container: str) -> dict[str, str]:
    try:
        output = _run(["docker", "inspect", "-f", "{{json .Config.Env}}", container])
        values = json.loads(output)
    except Exception:
        return {}
    if not isinstance(values, list):
        return {}
    result: dict[str, str] = {}
    for item in values:
        text = str(item)
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        if key:
            result[key] = value
    return result


def _status_with_runtime(status: dict[str, Any]) -> dict[str, Any]:
    result = dict(status)
    current_image = _current_container_image()
    target_image = str(result.get("target_image") or DEFAULT_IMAGE).strip()
    if current_image:
        result["current_image"] = current_image
    if target_image:
        result["target_image"] = target_image
    if current_image and target_image:
        result["update_available"] = _update_available(current_image, target_image)
    result["update_services"] = list(UPDATE_SERVICES)
    return result


def _run(command: list[str], env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(command, text=True, capture_output=True, env=env, timeout=600, check=False)
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        raise RuntimeError(output or f"command failed: {' '.join(command)}")
    return output


def _read_status() -> dict[str, Any]:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", "message": "status unavailable", "updated_at": _now()}
    return data if isinstance(data, dict) else {"status": "unknown", "message": "status invalid", "updated_at": _now()}


def _write_status(status: dict[str, Any]) -> None:
    with _LOCK:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
