"""Real API/runner fixture. Run only with a new, caller-owned temporary directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
from contextlib import asynccontextmanager


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir() or any(root.iterdir()):
        raise SystemExit("Fixture requires an existing empty temporary directory")
    for key in list(os.environ):
        if key.startswith("PULSEGUARD_"):
            del os.environ[key]
    os.environ.update({
        "PULSEGUARD_DATA_DIR": str(root / "data"),
        "PULSEGUARD_DB_PATH": str(root / "data" / "test.db"),
        "PULSEGUARD_REPORTS_DIR": str(root / "reports"),
        "PULSEGUARD_BACKUPS_DIR": str(root / "backups"),
    })
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    import uvicorn
    from fastapi.responses import JSONResponse
    from backend.app import storage
    from backend.app.main import app
    from backend.app.runner import CheckRunner
    from backend.app.scheduler import PulseScheduler
    from backend.app.automation_jobs import BatchExecutor
    from backend.app.schemas import CheckCreate

    storage.init_db()
    for check in storage.list_checks():
        storage.delete_check(check["id"])
    storage.update_settings({"prewarmed_browser_types": [], "api_pool_size": 1})
    project_id = storage.default_project_id()
    other_id = storage.create_project("Other MCP fixture project")["id"]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    target_requests = 0

    async def target():
        nonlocal target_requests
        target_requests += 1
        return JSONResponse({"status": "ok" if target_requests == 1 else "failed"}, status_code=200 if target_requests == 1 else 503)

    # Insert ahead of the existing SPA catch-all.
    app.add_api_route("/fixture/target", target, methods=["GET"])
    app.router.routes.insert(0, app.router.routes.pop())
    payload = CheckCreate(
        name="MCP contract probe", type="api", entry_url=f"http://127.0.0.1:{port}/fixture/target",
        assertions_json='[{"type":"status_code","expected_status":200}]', tags="smoke",
        headers_json='{"Authorization":"Bearer fixture-secret"}',
    ).model_dump()
    check = storage.create_check(payload, project_id=project_id)
    other = storage.create_check({**payload, "name": "Other project probe"}, project_id=other_id)

    @asynccontextmanager
    async def lifespan(_):
        runner = CheckRunner()
        await runner.start()
        app.state.runner = runner
        app.state.scheduler = PulseScheduler(runner)  # Not started; no scheduled target requests.
        batches = BatchExecutor(runner)
        batches.start()
        # Exercise real API, storage and runner; omit unrelated scheduler/node pollers.
        try:
            (root / "ready.json").write_text(json.dumps({
                "url": f"http://127.0.0.1:{port}", "project_id": project_id,
                "check_id": check["id"], "other_project_id": other_id, "other_check_id": other["id"],
            }), encoding="utf-8")
            yield
        finally:
            await batches.stop()
            await runner.shutdown()

    app.router.lifespan_context = lifespan
    try:
        uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
