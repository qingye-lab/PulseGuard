"""Verified source-only systemd releases; never replace the live database.

Run on the host with an uploaded manifest and stage directory. Dependency or
schema changes require their own migration/build procedure before this entry.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import deployment_backups


UNITS = ("pulseguard.service", "pulseguard-relay.service")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".next")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def api(path: str, *, project_id: int | None = None, post: bool = False, timeout: int = 120):
    headers = {} if project_id is None else {"X-PulseGuard-Project-Id": str(project_id)}
    request = urllib.request.Request("http://127.0.0.1:8787" + path, headers=headers,
                                     method="POST" if post else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def verify_console(project_ids: list[int]) -> list[dict]:
    if api("/api/health").get("status") != "ok":
        raise RuntimeError("Health acceptance failed")
    results = []
    for project_id in project_ids:
        for extra in ({}, {"status": "failed"}, {"type": "api"}, {"type": "ui"},
                      {"type": "ui", "status": "failed"}):
            query = urllib.parse.urlencode({"observation_kind": "observation", "page_size": 12, **extra})
            started = time.monotonic()
            page = api("/api/runs-page?" + query, project_id=project_id)
            if (type(page.get("total")) is not int or page["total"] < 0
                    or not isinstance(page.get("items"), list) or len(page["items"]) != min(page["total"], 12)
                    or page.get("page") != 1 or page.get("page_size") != 12):
                raise RuntimeError("Invalid pagination response")
            for row in page["items"]:
                if row.get("project_id") != project_id or row.get("observation_kind") != "observation":
                    raise RuntimeError("Pagination project/source scope differs")
                if extra.get("status") == "failed" and row.get("status") not in ("failed", "timeout"):
                    raise RuntimeError("Pagination status filter differs")
                if extra.get("type") and row.get("check_type") != extra["type"]:
                    raise RuntimeError("Pagination type filter differs")
            results.append({"project_id": project_id, "filters": extra, "total": page["total"],
                            "rows": len(page["items"]), "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
    return results


def validate_files(files: dict) -> None:
    for name in files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.parts[0] not in {"backend", "scripts", "docs"}:
            raise ValueError("Release path is outside source scope")


def capture_originals(root: Path, work: Path, files: dict) -> dict:
    validate_files(files)
    originals = {}
    for name, expected in files.items():
        target = root / name
        actual = digest(target) if target.exists() else None
        if actual != expected["baseline"]:
            raise RuntimeError("Concurrent source change: " + name)
        originals[name] = {"existed": target.exists(), "sha256": actual}
        if target.exists():
            original = work / "original" / name
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, original)
    # The inverse exists before the first replacement, including absent files.
    save(work / "originals.json", originals)
    return originals


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".release-next")
    shutil.copy2(source, temporary)
    if target.exists():
        os.chmod(temporary, target.stat().st_mode & 0o777)
    os.replace(temporary, target)


def replace_files(root: Path, work: Path, files: dict) -> None:
    for name, expected in files.items():
        candidate = work / "stage" / name
        if digest(candidate) != expected["sha256"]:
            raise RuntimeError("Candidate digest differs: " + name)
        atomic_copy(candidate, root / name)
        if digest(root / name) != expected["sha256"]:
            raise RuntimeError("Deployed digest differs: " + name)


def restore_files(root: Path, work: Path, originals: dict) -> None:
    failures = []
    for name, original in originals.items():
        try:
            target = root / name
            if original["existed"]:
                atomic_copy(work / "original" / name, target)
                if digest(target) != original["sha256"]:
                    raise RuntimeError("Restored digest differs")
            elif target.exists():
                target.unlink()
        except Exception as exc:
            failures.append(name + ": " + type(exc).__name__)
    if failures:
        raise RuntimeError("Incomplete source rollback: " + ", ".join(failures))


def service(action: str) -> None:
    subprocess.run(["systemctl", action, *UNITS], check=True, timeout=180)


def start_services() -> None:
    # Let systemd honor unit ordering while Relay reconnects during API startup.
    # Waiting for API readiness before starting Relay creates an avoidable gap
    # in which the first live health poll can report the stopped transport.
    service("start")
    deadline = time.monotonic() + 180
    while True:
        try:
            if api("/api/health", timeout=3).get("status") == "ok":
                break
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("API startup deadline exceeded")
        time.sleep(1)
    service("is-active")


def drain(reason: str) -> None:
    if api("/api/runtime").get("deployment", {}).get("active"):
        raise RuntimeError("Another maintenance window is active")
    runtime = api("/api/deployment/prepare?wait_seconds=300&reason=" + reason, post=True, timeout=310)["runner"]
    if not runtime.get("idle") or any(runtime.get(k) != 0 for k in
            ("queued", "running", "active_checks", "in_flight_jobs", "worker_jobs")):
        raise RuntimeError("Runner did not drain")


def settle(reason: str) -> None:
    state = api("/api/runtime").get("deployment", {})
    if state.get("active") and state.get("reason") == reason:
        api("/api/deployment/complete?run_enabled=false", post=True)
    if api("/api/runtime").get("deployment", {}).get("active"):
        raise RuntimeError("Maintenance did not settle")


def fingerprint(db: Path) -> dict:
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
        return {name: hashlib.sha256(json.dumps(conn.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall(),
                ensure_ascii=False, default=str).encode()).hexdigest() for name in ("checks", "projects", "settings")}


def preflight(root: Path, work: Path) -> dict:
    database = root / "data/pulseguard.db"
    scratch = work / "preflight.db"
    if scratch.exists():
        raise RuntimeError("Preflight scratch already exists")
    shadow = work / "shadow/backend/app"
    shutil.copytree(root / "backend/app", shadow, ignore=shutil.ignore_patterns("__pycache__"))
    for path in (work / "stage/backend/app").glob("*.py"):
        shutil.copy2(path, shadow / path.name)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as source, \
         closing(sqlite3.connect(scratch)) as target:
        source.backup(target)
    before = fingerprint(scratch)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PULSEGUARD_")}
    env.update(PYTHONPATH=str(work / "shadow/backend"), PULSEGUARD_DATA_DIR=str(work / "preflight-data"),
               PULSEGUARD_DB_PATH=str(scratch), PULSEGUARD_REPORTS_DIR=str(work / "preflight-reports"),
               PULSEGUARD_BACKUPS_DIR=str(work / "preflight-backups"))
    try:
        subprocess.run([sys.executable, "-c", "from app import storage; storage.init_db(recover_interrupted=False)"],
                       env=env, cwd=work, check=True, timeout=600)
        if fingerprint(scratch) != before:
            raise RuntimeError("Existing database migration changed configuration")
        return {"configuration_intact": True, "database_bytes": scratch.stat().st_size}
    finally:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(scratch) + suffix)
            if path.exists():
                path.unlink()


def snapshot(root: Path, work: Path, manifest: dict) -> Path:
    reason = "snapshot-" + manifest["id"]
    stopped = False
    try:
        drain(reason)
        stopped = True
        started = time.monotonic()
        service("stop")

        def resume(_snapshot):
            nonlocal stopped
            start_services()
            settle(reason)
            stopped = False
            save(work / "snapshot-window.json", {"window_seconds": round(time.monotonic() - started, 1)})

        return deployment_backups.create(Path("/opt/pulseguard-backups"), manifest["id"], root, on_snapshot=resume)
    finally:
        if stopped:
            start_services()
        settle(reason)


def apply(root: Path, work: Path, manifest: dict) -> dict:
    reason = "release-" + manifest["id"]
    backup = Path("/opt/pulseguard-backups") / ("managed-" + manifest["id"])
    deployment_backups._manifest(backup)
    # Verify without pruning before any cutover; acceptance remains pending.
    retention = int(api("/api/settings")["database_backup_retention"])
    deployment_backups.complete(backup.parent, manifest["id"], retention, dry_run=True)
    originals = capture_originals(root, work, manifest["files"])
    before = fingerprint(root / "data/pulseguard.db")
    protected = {p: digest(root / p) for p in (".env", "data/runner-token.key", "data/relay/relay.key",
                 "data/relay/relay.crt", "data/relay/control-token") if (root / p).exists()}
    stopped = False
    try:
        drain(reason)
        stopped = True
        started = time.monotonic()
        service("stop")
        replace_files(root, work, manifest["files"])
        start_services()
        downtime = time.monotonic() - started
        if fingerprint(root / "data/pulseguard.db") != before or any(digest(root / p) != sha for p, sha in protected.items()):
            raise RuntimeError("Configuration or protected files changed")
        projects = [int(p["id"]) for p in api("/api/projects") if p["status"] == "active"]
        if not projects:
            raise RuntimeError("No active project to accept")
        pages = verify_console(projects)
        settle(reason)
    except BaseException:
        if stopped:
            service("stop")
            restore_files(root, work, originals)
            start_services()
        settle(reason)
        raise
    # Completion is after application acceptance and deliberately outside the
    # rollback block: a retention failure must not undo accepted source code.
    previous = manifest.get("previous_backup_id")
    if previous:
        previous_point = deployment_backups._target(backup.parent, previous)
        if deployment_backups._manifest(previous_point)["state"] == "pending":
            deployment_backups.complete(backup.parent, previous, retention)
    deployment_backups.complete(backup.parent, manifest["id"], retention)
    return {"state": "accepted", "window_seconds": round(downtime, 1), "pages": pages,
            "backup_state": deployment_backups._manifest(backup)["state"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "snapshot", "apply"))
    parser.add_argument("work", type=Path)
    args = parser.parse_args()
    work = args.work.resolve(strict=True)
    manifest = json.loads((work / "manifest.json").read_text())
    root = Path("/opt/pulseguard")
    import fcntl
    with open("/run/pulseguard-source-release.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {"state": "running", "mode": args.mode}
        save(work / (args.mode + "-result.json"), state)
        try:
            if args.mode == "preflight":
                state.update(preflight(root, work), state="ready")
            elif args.mode == "snapshot":
                state.update(backup=str(snapshot(root, work, manifest)), state="ready")
            else:
                state.update(apply(root, work, manifest))
        except BaseException as exc:
            state.update(state="failed", error=type(exc).__name__ + ": " + str(exc)[:300])
            raise
        finally:
            save(work / (args.mode + "-result.json"), state)


if __name__ == "__main__":
    main()
