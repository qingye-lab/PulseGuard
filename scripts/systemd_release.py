"""Verified source-only systemd releases; never replace the live database.

Run on the host with an uploaded manifest and stage directory. Compatible DB
migrations require an explicit manifest flag and a successful copy preflight.
Dependency changes and incompatible contracts need their own build/migration.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
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
        base = api('/api/runs-page?observation_kind=observation&page_size=1', project_id=project_id)
        sample = next(iter(base.get('items', [])), {})
        cases = [{}, {"type": "api"}, {"type": "ui"}]
        cases += [{"status": value} for value in ('ok', 'failed', 'timeout', 'pending', 'running', 'skipped', 'preparation_failed')]
        cases += [{"notification_status": value} for value in ('sent', 'failed', 'suppressed', 'disabled', 'not_required')]
        cases += [{"observation_kind": value} for value in ('verification', 'draft')]
        for key in ('check_id', 'runner_id', 'run_group_id', 'trigger'):
            cases.append({key: sample.get(key) or (1 if key == 'check_id' else 'release-no-match')})
        cases.append({'q': str(sample.get('check_name') or 'release-no-match')[:12]})
        if sample.get('started_at'):
            cases += [{'start': sample['started_at']}, {'end': sample['started_at']},
                      {'start': sample['started_at'], 'end': sample['started_at']}]
        cases += [{'type': 'ui', 'status': 'failed'}, {'notification_status': 'failed', 'status': 'failed'},
                  {'notification_status': 'failed', 'page': 2},
                  {'notification_status': 'failed', 'page_size': 1},
                  {'page_size': 100},
                  {'type': 'api', 'notification_status': 'failed', 'runner_id': sample.get('runner_id') or 'release-no-match'},
                  {'q': 'release-no-such-check', 'notification_status': 'failed'},
                  {'type': sample.get('check_type') or 'api', 'status': sample.get('status') or 'ok',
                   'notification_status': sample.get('notification_status') or 'not_required',
                   'runner_id': sample.get('runner_id') or 'release-no-match',
                   'run_group_id': sample.get('run_group_id') or 'release-no-match',
                   'trigger': sample.get('trigger') or 'release-no-match',
                   'check_id': sample.get('check_id') or 1,
                   'q': str(sample.get('check_name') or 'release-no-match')[:12]}]
        for extra in cases:
            query = urllib.parse.urlencode({"observation_kind": "observation", "page_size": 12, **extra})
            started = time.monotonic()
            page = api("/api/runs-page?" + query, project_id=project_id)
            expected_page = extra.get('page', 1)
            expected_size = extra.get('page_size', 12)
            if (type(page.get("total")) is not int or page["total"] < 0
                    or not isinstance(page.get("items"), list)
                    or len(page["items"]) != min(max(page["total"] - (expected_page - 1) * expected_size, 0), expected_size)
                    or page.get("page") != expected_page or page.get("page_size") != expected_size):
                raise RuntimeError("Invalid pagination response")
            for row in page["items"]:
                if row.get("project_id") != project_id or row.get("observation_kind") != extra.get('observation_kind', "observation"):
                    raise RuntimeError("Pagination project/source scope differs")
                if extra.get("status") == "failed" and row.get("status") not in ("failed", "timeout"):
                    raise RuntimeError("Pagination status filter differs")
                if extra.get("type") and row.get("check_type") != extra["type"]:
                    raise RuntimeError("Pagination type filter differs")
                if extra.get('status') and extra['status'] != 'failed' and row.get('status') != extra['status']:
                    raise RuntimeError('Pagination exact status filter differs')
                for key in ('notification_status', 'runner_id', 'run_group_id', 'trigger', 'check_id'):
                    if key in extra and row.get(key) != extra[key]:
                        raise RuntimeError('Pagination filter differs: ' + key)
                if extra.get('q'):
                    with closing(sqlite3.connect(':memory:')) as predicate:
                        if not predicate.execute('SELECT ? LIKE ?', (row.get('check_name'), '%' + extra['q'] + '%')).fetchone()[0]:
                            raise RuntimeError('Pagination name filter differs')
                if extra.get('start') or extra.get('end'):
                    actual = datetime.fromisoformat(row['started_at']).astimezone()
                    if extra.get('start') and actual < datetime.fromisoformat(extra['start']).astimezone():
                        raise RuntimeError('Pagination start filter differs')
                    if extra.get('end') and actual > datetime.fromisoformat(extra['end']).astimezone():
                        raise RuntimeError('Pagination end filter differs')
            results.append({"project_id": project_id, "filters": extra, "total": page["total"],
                            "rows": len(page["items"]), "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
    return results


def migrate_database(root: Path) -> None:
    # Run migrations under the writer fence before starting API/Relay. Building
    # a large compatible index must not compete with the API readiness deadline.
    env = os.environ.copy()
    env.update(PYTHONPATH=str(root / 'backend'), PULSEGUARD_DB_PATH=str(root / 'data/pulseguard.db'))
    subprocess.run([sys.executable, '-c',
                    'from app import storage; storage.init_db(recover_interrupted=False)'],
                   cwd=root, env=env, check=True, timeout=1800)


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
    manifest = json.loads((work / 'manifest.json').read_text())
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
        subprocess.run([sys.executable, "-c", "import logging; logging.basicConfig(level=logging.INFO); "
                        "from app import storage; storage.init_db(recover_interrupted=False)"],
                       env=env, cwd=work, check=True, timeout=1800)
        if fingerprint(scratch) != before:
            raise RuntimeError("Existing database migration changed configuration")
        return {"configuration_intact": True, "database_bytes": scratch.stat().st_size,
                "manifest_sha256": digest(work / 'manifest.json'),
                "copy_retained": bool(manifest.get('retain_preflight_copy'))}
    finally:
        if not manifest.get('retain_preflight_copy'):
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
    if manifest.get('migrate_database'):
        try:
            preflight_result = json.loads((work / 'preflight-result.json').read_text())
            ready = (preflight_result.get('state') == 'ready' and preflight_result.get('configuration_intact')
                     and preflight_result.get('manifest_sha256') == digest(work / 'manifest.json'))
        except (OSError, ValueError):
            ready = False
        if not ready:
            raise RuntimeError('Compatible migration requires a successful preflight for the exact manifest')
    backup = Path("/opt/pulseguard-backups") / ("managed-" + manifest["id"])
    deployment_backups._manifest(backup)
    # Verify without pruning before any cutover; acceptance remains pending.
    retention = int(api("/api/settings")["database_backup_retention"])
    deployment_backups.complete(backup.parent, manifest["id"], retention, dry_run=True)
    originals = None
    before = fingerprint(root / "data/pulseguard.db")
    protected = {p: digest(root / p) for p in (".env", "data/runner-token.key", "data/relay/relay.key",
                 "data/relay/relay.crt", "data/relay/control-token") if (root / p).exists()}
    stopped = False
    try:
        drain(reason)
        stopped = True
        started = time.monotonic()
        service("stop")
        originals = capture_originals(root, work, manifest["files"])
        replace_files(root, work, manifest["files"])
        if manifest.get('migrate_database'):
            migrate_database(root)
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
            if originals is not None:
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
