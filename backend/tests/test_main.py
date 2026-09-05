from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app import storage
from backend.app.main import app


class FakeConcurrentRunner:
    def __init__(self) -> None:
        self.running = 0
        self.max_running = 0

    async def run_check(
        self, check_id: int, trigger: str = "manual", project_id: int | None = None
    ) -> dict[str, object]:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(0.01)
            return {"id": check_id, "check_id": check_id, "trigger": trigger}
        finally:
            self.running -= 1


class FakeInspectRunner:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    async def inspect_ui_rules(self, payload: dict[str, object]) -> dict[str, object]:
        self.payload = payload
        return {
            "title": "Example",
            "url": payload["entry_url"],
            "results": [{"id": "visible", "type": "element_visible", "selector": "#app", "status": "ok", "count": 1, "message": "selector 当前匹配正常"}],
        }


class FakeScheduler:
    def __init__(self) -> None:
        self.synced: list[int] = []
        self.pause_count = 0
        self.resume_count = 0
        self.refresh_count = 0

    def sync_check(self, check_id: int) -> None:
        self.synced.append(check_id)

    def pause(self) -> None:
        self.pause_count += 1

    def resume(self) -> None:
        self.resume_count += 1
        self.refresh_all()

    def refresh_all(self) -> None:
        self.refresh_count += 1

    def runtime_status(self) -> dict[str, object]:
        return {
            "running": True,
            "paused": self.pause_count > self.resume_count,
            "scheduled_checks": len(self.synced),
            "next_due_at": None,
            "overdue_jobs": 0,
        }


class FakeDeploymentRunner:
    def __init__(self) -> None:
        self.waits: list[int] = []
        self.runs: list[tuple[int, str]] = []

    async def wait_for_idle(self, timeout_seconds: int) -> dict[str, object]:
        self.waits.append(timeout_seconds)
        return {"idle": True, "queued": 0, "running": 0, "active_checks": 0}

    async def run_check(
        self, check_id: int, trigger: str = "manual", project_id: int | None = None
    ) -> dict[str, object]:
        self.runs.append((check_id, trigger))
        return {"id": check_id, "check_id": check_id, "trigger": trigger}

    def runtime_status(self) -> dict[str, object]:
        return {"queue": {"queued": 0}, "workers": {"running": 0}, "active_checks": 0}


class ProjectScopedRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[main_module._project_context] = lambda: 1
        app.dependency_overrides[main_module._active_project_context] = lambda: 1
        self._refresh_stale_patch = patch("backend.app.main.storage.refresh_stale_statuses")
        self._refresh_stale_patch.start()

    def tearDown(self) -> None:
        self._refresh_stale_patch.stop()
        app.dependency_overrides.pop(main_module._project_context, None)
        app.dependency_overrides.pop(main_module._active_project_context, None)


class FrontendRouteTests(unittest.TestCase):
    def test_health_route_returns_app_identity(self) -> None:
        response = TestClient(app).get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "name": "PulseGuard", "team": ""})

    def test_database_health_route_returns_storage_capacity_signals(self) -> None:
        payload = {
            "status": "normal",
            "file_size_bytes": 4096,
            "wal_size_bytes": 0,
            "page_size_bytes": 4096,
            "page_count": 1,
            "freelist_pages": 0,
            "estimated_used_bytes": 4096,
            "reclaimable_bytes": 0,
            "reclaimable_ratio": 0.0,
            "journal_mode": "wal",
            "maintenance_recommended": False,
        }
        with patch("backend.app.main.storage.get_database_health", return_value=payload):
            response = TestClient(app).get("/api/database-health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)

    def test_frontend_root_static_file_is_served_before_spa_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            static_dir = Path(tmpdir)
            (static_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
            (static_dir / "favicon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")

            with patch("backend.app.main.STATIC_DIR", static_dir):
                root = TestClient(app).get("/")
                favicon = TestClient(app).get("/favicon.svg")
                fallback = TestClient(app).get("/unknown-route")

        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.headers["cache-control"], "no-cache, must-revalidate")
        self.assertEqual(favicon.status_code, 200)
        self.assertIn("image/svg+xml", favicon.headers["content-type"])
        self.assertIn("<svg", favicon.text)
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.headers["cache-control"], "no-cache, must-revalidate")
        self.assertIn('id="root"', fallback.text)


class RunAllRouteTests(ProjectScopedRouteTests):
    def test_run_all_submits_enabled_checks_concurrently(self) -> None:
        runner = FakeConcurrentRunner()

        with patch.object(app.state, "runner", runner, create=True), patch(
            "backend.app.main.storage.list_checks",
            return_value=[
                {"id": 1, "name": "API 1", "type": "api", "project_id": 1},
                {"id": 2, "name": "API 2", "type": "api", "project_id": 1},
            ],
        ) as list_checks:
            response = TestClient(app).post("/api/checks/run-all?type=api")

        self.assertEqual(response.status_code, 200)
        self.assertEqual([run["check_id"] for run in response.json()["runs"]], [1, 2])
        self.assertGreaterEqual(runner.max_running, 2)
        list_checks.assert_called_once_with("api", enabled_only=True, project_id=1, active_projects_only=False)

    def test_batch_enable_route_uses_selected_ids_and_syncs_matches(self) -> None:
        scheduler = FakeScheduler()
        checks_by_id = {
            1: {"id": 1, "name": "API 1", "type": "api"},
            2: {"id": 2, "name": "API 2", "type": "api"},
        }

        with patch.object(app.state, "scheduler", scheduler, create=True), patch(
            "backend.app.main.storage.get_check",
            side_effect=lambda check_id, **_: checks_by_id.get(check_id),
        ) as get_check, patch(
            "backend.app.main.storage.batch_set_check_enabled",
            return_value=2,
        ) as batch_set_enabled, patch(
            "backend.app.main.storage.select_checks_for_batch"
        ) as select_checks, patch(
            "backend.app.main.storage.record_audit_event"
        ) as record_audit_event:
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "enable", "type": "api", "ids": [1, 2], "expected_count": 2},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"matched": 2, "changed": 2, "ids": [1, 2], "runs": []})
        self.assertEqual(get_check.call_count, 2)
        select_checks.assert_not_called()
        batch_set_enabled.assert_called_once_with([1, 2], True, project_id=1)
        record_audit_event.assert_called_once()
        self.assertEqual(scheduler.synced, [1, 2])

    def test_batch_disable_route_updates_storage_and_syncs_matches(self) -> None:
        scheduler = FakeScheduler()
        checks_by_id = {5: {"id": 5, "name": "API 5", "type": "api"}}

        with patch.object(app.state, "scheduler", scheduler, create=True), patch(
            "backend.app.main.storage.get_check",
            side_effect=lambda check_id, **_: checks_by_id.get(check_id),
        ) as get_check, patch(
            "backend.app.main.storage.batch_set_check_enabled",
            return_value=1,
        ) as batch_set_enabled, patch(
            "backend.app.main.storage.select_checks_for_batch"
        ) as select_checks, patch(
            "backend.app.main.storage.record_audit_event"
        ) as record_audit_event:
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "disable", "type": "api", "ids": [5], "expected_count": 1},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"matched": 1, "changed": 1, "ids": [5], "runs": []})
        get_check.assert_called_once_with(5, refresh_stale=False, project_id=1)
        select_checks.assert_not_called()
        batch_set_enabled.assert_called_once_with([5], False, project_id=1)
        record_audit_event.assert_called_once()
        self.assertEqual(scheduler.synced, [5])

    def test_batch_route_rejects_removed_interval_action(self) -> None:
        with patch("backend.app.main.storage.get_check") as get_check, patch(
            "backend.app.main.storage.batch_update_check_interval"
        ) as batch_update_interval:
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "update_interval", "type": "api", "ids": [1], "expected_count": 1},
            )

        self.assertEqual(response.status_code, 422)
        get_check.assert_not_called()
        batch_update_interval.assert_not_called()

    def test_batch_route_requires_selected_ids(self) -> None:
        with patch("backend.app.main.storage.get_check") as get_check, patch(
            "backend.app.main.storage.batch_set_check_enabled"
        ) as batch_set_enabled:
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "disable", "type": "api", "expected_count": 0},
            )

        self.assertEqual(response.status_code, 422)
        get_check.assert_not_called()
        batch_set_enabled.assert_not_called()

    def test_batch_run_route_uses_enabled_selection_and_manual_batch_trigger(self) -> None:
        runner = FakeConcurrentRunner()
        checks_by_id = {
            1: {"id": 1, "name": "API 1", "type": "api", "enabled": True},
            2: {"id": 2, "name": "API 2", "type": "api", "enabled": True},
        }

        with patch.object(app.state, "runner", runner, create=True), patch(
            "backend.app.main.storage.get_check",
            side_effect=lambda check_id, **_: checks_by_id.get(check_id),
        ) as get_check, patch("backend.app.main.storage.select_checks_for_batch") as select_checks:
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "run", "type": "api", "ids": [1, 2], "expected_count": 2},
            )

        body: dict[str, Any] = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["matched"], 2)
        self.assertEqual(body["changed"], 0)
        self.assertEqual(body["ids"], [1, 2])
        self.assertEqual([run["trigger"] for run in body["runs"]], ["manual-batch", "manual-batch"])
        self.assertGreaterEqual(runner.max_running, 2)
        self.assertEqual(get_check.call_count, 2)
        select_checks.assert_not_called()

    def test_batch_run_route_rejects_disabled_selection(self) -> None:
        runner = FakeConcurrentRunner()
        checks_by_id = {
            1: {"id": 1, "name": "API 1", "type": "api", "enabled": True},
            2: {"id": 2, "name": "API 2", "type": "api", "enabled": False},
        }

        with patch.object(app.state, "runner", runner, create=True), patch(
            "backend.app.main.storage.get_check",
            side_effect=lambda check_id, **_: checks_by_id.get(check_id),
        ):
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "run", "type": "api", "ids": [1, 2], "expected_count": 2},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("选中任务包含已禁用项", response.json()["detail"])
        self.assertEqual(runner.max_running, 0)

    def test_batch_route_rejects_stale_expected_count(self) -> None:
        checks_by_id = {1: {"id": 1, "name": "API 1", "type": "api"}}

        with patch("backend.app.main.storage.get_check", side_effect=lambda check_id, **_: checks_by_id.get(check_id)), patch(
            "backend.app.main.storage.batch_set_check_enabled"
        ) as batch_set_enabled:
            response = TestClient(app).post(
                "/api/checks/batch",
                json={"action": "disable", "type": "api", "ids": [1, 2], "expected_count": 2},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("选中任务数量已变化", response.json()["detail"])
        batch_set_enabled.assert_not_called()

    def test_inspect_ui_rules_route_delegates_to_runner(self) -> None:
        runner = FakeInspectRunner()
        payload = {
            "type": "ui",
            "entry_url": "https://example.com",
            "timeout_ms": 15000,
            "viewport_mode": "web",
            "viewport_width": 1440,
            "viewport_height": 900,
            "setup_script": "",
            "assertions_json": '[{"id":"visible","type":"element_visible","selector":"#app"}]',
        }

        with patch.object(app.state, "runner", runner, create=True), patch("backend.app.main.storage.get_settings", return_value={}):
            response = TestClient(app).post("/api/checks/inspect-ui-rules", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["status"], "ok")
        self.assertEqual(json.loads(str(runner.payload["assertions_json"])), json.loads(payload["assertions_json"]))


class DeploymentRouteTests(unittest.TestCase):
    def test_prepare_deployment_marks_window_pauses_scheduler_and_waits_for_runner(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            runner = FakeDeploymentRunner()
            scheduler = FakeScheduler()

            with patch.object(app.state, "runner", runner, create=True), patch.object(
                app.state, "scheduler", scheduler, create=True
            ):
                response = TestClient(app).post("/api/deployment/prepare?wait_seconds=0&reason=test-deploy")

        body: dict[str, Any] = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["deployment"]["active"])
        self.assertEqual(body["deployment"]["reason"], "test-deploy")
        self.assertEqual(runner.waits, [0])
        self.assertEqual(scheduler.pause_count, 1)
        self.assertTrue(body["scheduler"]["paused"])

    def test_complete_deployment_reruns_enabled_checks_before_resuming_scheduler(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            storage.start_deployment_window("test-deploy")
            runner = FakeDeploymentRunner()
            scheduler = FakeScheduler()

            with patch.object(app.state, "runner", runner, create=True), patch.object(
                app.state, "scheduler", scheduler, create=True
            ), patch(
                "backend.app.main.storage.list_checks",
                return_value=[
                    {"id": 10, "name": "API 10", "type": "api", "enabled": True, "project_id": 1},
                    {"id": 11, "name": "API 11", "type": "api", "enabled": True, "project_id": 1},
                ],
            ) as list_checks:
                response = TestClient(app).post("/api/deployment/complete")

        body: dict[str, Any] = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["deployment"]["active"])
        self.assertEqual(runner.runs, [(10, "post-deploy"), (11, "post-deploy")])
        self.assertEqual(runner.waits, [300])
        self.assertEqual(body["discarded_incomplete_runs"], 0)
        list_checks.assert_called_once_with(None, enabled_only=True, project_id=None, active_projects_only=True)
        self.assertEqual(scheduler.refresh_count, 1)
        self.assertEqual(scheduler.resume_count, 1)


class HeartbeatRouteTests(unittest.TestCase):
    def test_record_heartbeat_route_persists_json_payload(self) -> None:
        heartbeat = {
            "key": "nightly-job",
            "status": "ok",
            "message": "done",
            "payload": {"build": 42},
            "received_at": "2026-06-08T12:00:00+08:00",
            "updated_at": "2026-06-08T12:00:00+08:00",
        }

        with patch("backend.app.main.storage.get_settings", return_value={}), patch(
            "backend.app.main.storage.record_heartbeat", return_value=heartbeat
        ) as record_heartbeat:
            response = TestClient(app).post(
                "/api/heartbeats/nightly-job",
                json={"status": "ok", "message": "done", "payload": {"build": 42}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["heartbeat"]["payload"], {"build": 42})
        record_heartbeat.assert_called_once_with("nightly-job", status="ok", message="done", payload={"build": 42})

    def test_heartbeat_routes_mask_sensitive_payload_fields(self) -> None:
        settings = {"read_only_token": "read-token-123", "environment_variables": []}
        heartbeat = {
            "key": "nightly-job",
            "status": "ok",
            "message": "read-token-123 done",
            "payload": {"Authorization": "Bearer literal-token", "build": 42},
            "received_at": "2026-06-08T12:00:00+08:00",
            "updated_at": "2026-06-08T12:00:00+08:00",
        }

        with patch("backend.app.main.storage.get_settings", return_value=settings), patch(
            "backend.app.main.storage.record_heartbeat", return_value=heartbeat
        ) as record_heartbeat:
            response = TestClient(app).post(
                "/api/heartbeats/nightly-job",
                json={"status": "ok", "message": "read-token-123 done", "payload": {"Authorization": "Bearer literal-token", "build": 42}},
            )

        self.assertEqual(response.status_code, 200)
        record_heartbeat.assert_called_once_with("nightly-job", status="ok", message="*** done", payload={"Authorization": "***", "build": 42})
        response_text = json.dumps(response.json(), ensure_ascii=False)
        self.assertNotIn("read-token-123", response_text)
        self.assertNotIn("Bearer literal-token", response_text)

        with patch("backend.app.main.storage.get_settings", return_value=settings), patch(
            "backend.app.main.storage.get_heartbeat", return_value=heartbeat
        ):
            detail = TestClient(app).get("/api/heartbeats/nightly-job")

        self.assertEqual(detail.status_code, 200)
        detail_text = json.dumps(detail.json(), ensure_ascii=False)
        self.assertNotIn("read-token-123", detail_text)
        self.assertNotIn("Bearer literal-token", detail_text)

    def test_record_heartbeat_route_rejects_invalid_status(self) -> None:
        response = TestClient(app).post("/api/heartbeats/nightly-job", json={"status": "unknown"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("心跳状态必须是 ok 或 failed", response.json()["detail"])

    def test_heartbeat_detail_route_returns_404_when_missing(self) -> None:
        with patch("backend.app.main.storage.get_heartbeat", return_value=None):
            response = TestClient(app).get("/api/heartbeats/missing-job")

        self.assertEqual(response.status_code, 404)


class RunnerRouteTests(unittest.TestCase):
    def test_worker_health_reports_version_and_update_metadata(self) -> None:
        token = "pgrn_workerhealthtoken1234567890"
        with patch("backend.app.main.WORKER_TOKEN", token), patch("backend.app.main.APP_VERSION", "0.2.0"), patch(
            "backend.app.main.BUILD_SHA", "abc123"
        ), patch("backend.app.main.WORKER_IMAGE", "pulseguard-worker:old"), patch(
            "backend.app.main.WORKER_UPDATE_IMAGE", "pulseguard-worker:new"
        ), patch("backend.app.main.WORKER_UPDATER_URL", "http://pulseguard-worker-updater:8790"), patch(
            "backend.app.main._worker_update_status_for_health", return_value={}
        ):
            response = TestClient(app).get("/api/worker/health", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(response.status_code, 200)
        metadata = response.json()["metadata"]
        self.assertEqual(metadata["version"], "0.2.0")
        self.assertEqual(metadata["build_sha"], "abc123")
        self.assertTrue(metadata["update_supported"])
        self.assertTrue(metadata["update_available"])

    def test_worker_health_uses_updater_current_image_to_avoid_update_misreporting(self) -> None:
        token = "pgrn_workerhealthtoken1234567890"
        with patch("backend.app.main.WORKER_TOKEN", token), patch("backend.app.main.WORKER_IMAGE", "pulseguard-worker:old"), patch(
            "backend.app.main.WORKER_UPDATE_IMAGE", "pulseguard-worker:new"
        ), patch("backend.app.main.WORKER_UPDATER_URL", "http://pulseguard-worker-updater:8790"), patch(
            "backend.app.main._worker_update_status_for_health",
            return_value={"status": "succeeded", "current_image": "pulseguard-worker:new", "message": "worker updated"},
        ):
            response = TestClient(app).get("/api/worker/health", headers={"Authorization": f"Bearer {token}"})

        self.assertEqual(response.status_code, 200)
        metadata = response.json()["metadata"]
        self.assertEqual(metadata["image"], "pulseguard-worker:new")
        self.assertEqual(metadata["update_current_image"], "pulseguard-worker:new")
        self.assertEqual(metadata["update_status"], "succeeded")
        self.assertFalse(metadata["update_available"])

    def test_worker_update_requires_auth_and_proxies_to_updater(self) -> None:
        token = "pgrn_workerupdatetoken1234567890"
        result = {"ok": True, "message": "accepted"}
        with patch("backend.app.main.WORKER_TOKEN", token), patch(
            "backend.app.main._worker_updater_request", new=AsyncMock(return_value=result)
        ) as worker_updater_request:
            response = TestClient(app).post(
                "/api/worker/update",
                headers={"Authorization": f"Bearer {token}"},
                json={"target_image": "pulseguard-worker:new", "update_id": "upd-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        worker_updater_request.assert_awaited_once_with(
            "POST",
            "/update",
            {"target_image": "pulseguard-worker:new", "update_id": "upd-1", "force": False},
        )

    def test_child_runner_health_triggers_missing_enabled_browser_install(self) -> None:
        runner = {"runner_id": "office-1", "address": "http://10.0.0.8:8788", "role": "child"}
        health = {"installed_browser_types": ["chromium"], "available_browser_types": ["chromium"]}

        async def scenario() -> None:
            with patch(
                "backend.app.main.storage.get_settings",
                return_value={
                    "browser_type": "chromium",
                    "enabled_browser_types": ["chromium", "firefox"],
                    "prewarmed_browser_types": ["chromium"],
                    "browser_pool_sizes": {"chromium": 5, "firefox": 5, "webkit": 5},
                },
            ), patch("backend.app.main._worker_request", new=AsyncMock(return_value={"ok": True})) as worker_request:
                await main_module._install_missing_enabled_browser_types_on_runner(runner, health)

            worker_request.assert_awaited_once_with(
                runner,
                "POST",
                "/api/worker/browser-types/install",
                {"browser_types": ["firefox"]},
                timeout=10,
            )

        asyncio.run(scenario())

    def test_create_runner_stores_supplied_token_without_returning_secret(self) -> None:
        token = "pgrn_abcdefghijklmnopqrstuvwxyzABCDE1234567890_-"
        runner = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8788",
            "network_region": "office-lan",
            "browser_version": "",
            "status": "offline",
            "enabled": True,
            "role": "child",
            "available": False,
            "token_set": True,
            "token_hint": "90_-",
            "token_value": token,
        }

        with patch("backend.app.main.storage.create_probe_runner", return_value=runner) as create_probe_runner, patch(
            "backend.app.main.storage.record_audit_event"
        ) as record_audit_event, patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).post(
                "/api/runners",
                json={
                    "name": "Office Runner",
                    "address": "10.0.0.8",
                    "network_region": "office-lan",
                    "token": f"token: {token}",
                    "allow_insecure_http": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("token", payload)
        self.assertNotIn("token_value", payload)
        create_probe_runner.assert_called_once()
        self.assertEqual(create_probe_runner.call_args.args[0]["role"], "child")
        self.assertEqual(create_probe_runner.call_args.args[0]["address"], "http://10.0.0.8:8788")
        self.assertEqual(create_probe_runner.call_args.args[0]["token"], token)
        self.assertTrue(create_probe_runner.call_args.args[0]["allow_insecure_http"])
        record_audit_event.assert_called_once()

    def test_create_runner_rejects_http_without_explicit_confirmation(self) -> None:
        response = TestClient(app).post(
            "/api/runners",
            json={
                "name": "Office Runner",
                "address": "10.0.0.8",
                "network_region": "office-lan",
                "token": "pgrn_abcdefghijklmnopqrstuvwxyzABCDE1234567890_-",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("明文传输", response.text)

    def test_worker_request_rejects_unconfirmed_manual_http_before_sending_token(self) -> None:
        runner = {
            "runner_id": "unsafe-http",
            "role": "child",
            "connection_mode": "manual",
            "address": "http://10.0.0.8:8788",
            "allow_insecure_http": False,
        }

        with patch("backend.app.main.storage.get_probe_runner_token") as get_token, patch(
            "backend.app.main.httpx.AsyncClient"
        ) as httpx_client:
            with self.assertRaises(HTTPException) as context:
                asyncio.run(main_module._worker_request(runner, "GET", "/api/worker/health"))

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("明文传输", str(context.exception.detail))
        get_token.assert_not_called()
        httpx_client.assert_not_called()

    def test_create_runner_rejects_non_worker_token(self) -> None:
        response = TestClient(app).post(
            "/api/runners",
            json={
                "name": "Office Runner",
                "address": "10.0.0.8",
                "network_region": "office-lan",
                "token": "token: 复制整段日志但没有有效令牌",
                "allow_insecure_http": True,
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("pgrn_", response.text)

    def test_provision_runner_returns_relay_deployment_command(self) -> None:
        runner = {
            "runner_id": "edge-1",
            "name": "Edge Runner",
            "address": "http://pulseguard-relay:18001",
            "network_region": "edge",
            "status": "pending_deployment",
            "enabled": True,
            "role": "child",
            "connection_mode": "relay",
            "available": False,
            "token_set": True,
            "token_hint": "abc123",
            "relay_token": "pgrl_relay-token",
            "worker_token": "pgrn_worker-token",
            "relay_token_version": 1,
            "deploy_command_expires_at": "2026-06-19T00:00:00+08:00",
        }

        with patch("backend.app.main.RELAY_ENABLED", True), patch(
            "backend.app.main.RELAY_PUBLIC_HOST", "203.0.113.10"
        ), patch("backend.app.main.RELAY_PUBLIC_PORT", 9443), patch(
            "backend.app.main.relay_fingerprint", return_value="abc" * 21 + "a"
        ), patch(
            "backend.app.main.storage.provision_probe_runner", return_value=runner
        ) as provision_probe_runner, patch("backend.app.main.storage.record_audit_event"), patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).post(
                "/api/runners/provision",
                json={
                    "name": "Edge Runner",
                    "network_region": "edge",
                    "target_platform": "linux",
                    "compose_url": "https://deploy.example.com/docker-compose.relay-worker.yml",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["connection_mode"], "relay")
        self.assertIn("deployment", payload)
        self.assertIn("PULSEGUARD_RELAY_URL", payload["deployment"]["command"])
        self.assertIn(
            "https://deploy.example.com/docker-compose.relay-worker.yml",
            payload["deployment"]["command"],
        )
        self.assertEqual(payload["deployment"]["compose_url"], "https://deploy.example.com/docker-compose.relay-worker.yml")
        self.assertNotIn("codex/github-publish", payload["deployment"]["command"])
        self.assertIn("pgrl_relay-token", payload["deployment"]["command"])
        self.assertIn("pgrn_worker-token", payload["deployment"]["command"])
        self.assertIn("COMPOSE_PROFILES='updater'", payload["deployment"]["command"])
        self.assertIn("PULSEGUARD_WORKER_UPDATER_URL='http://pulseguard-worker-updater:8790'", payload["deployment"]["command"])
        self.assertIn("pulseguard-worker-updater", payload["deployment"]["command"])
        self.assertNotIn("relay_token", payload)
        self.assertNotIn("worker_token", payload)
        provision_probe_runner.assert_called_once()

    def test_provision_runner_rejects_invalid_compose_url(self) -> None:
        with patch("backend.app.main.RELAY_ENABLED", True), patch(
            "backend.app.main.RELAY_PUBLIC_HOST", "203.0.113.10"
        ), patch("backend.app.main.relay_fingerprint", return_value="abc" * 21 + "a"):
            response = TestClient(app).post(
                "/api/runners/provision",
                json={
                    "name": "Edge Runner",
                    "network_region": "edge",
                    "target_platform": "linux",
                    "compose_url": "docker-compose.relay-worker.yml",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("http(s)", response.text)

    def test_regenerate_runner_provision_uses_supplied_compose_url(self) -> None:
        runner = {
            "runner_id": "edge-1",
            "name": "Edge Runner",
            "address": "http://pulseguard-relay:18001",
            "network_region": "edge",
            "status": "pending_deployment",
            "enabled": True,
            "role": "child",
            "connection_mode": "relay",
            "available": False,
            "token_set": True,
            "token_hint": "abc123",
            "relay_token": "pgrl_rotated-token",
            "worker_token": "pgrn_worker-token",
            "relay_token_version": 2,
            "deploy_command_expires_at": "2026-06-19T00:00:00+08:00",
        }

        with patch("backend.app.main.RELAY_ENABLED", True), patch(
            "backend.app.main.RELAY_PUBLIC_HOST", "203.0.113.10"
        ), patch("backend.app.main.RELAY_PUBLIC_PORT", 9443), patch(
            "backend.app.main.relay_fingerprint", return_value="abc" * 21 + "a"
        ), patch(
            "backend.app.main.storage.regenerate_probe_runner_provision", return_value=runner
        ), patch("backend.app.main._revoke_relay_session") as revoke_relay_session, patch(
            "backend.app.main.storage.record_audit_event"
        ), patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).post(
                "/api/runners/edge-1/provision/regenerate",
                json={
                    "name": "Edge Runner",
                    "network_region": "edge",
                    "target_platform": "powershell",
                    "compose_url": "https://deploy.example.com/custom-relay-worker.yml",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["deployment"]["target_platform"], "powershell")
        self.assertEqual(payload["deployment"]["compose_url"], "https://deploy.example.com/custom-relay-worker.yml")
        self.assertIn("https://deploy.example.com/custom-relay-worker.yml", payload["deployment"]["command"])
        self.assertNotIn("codex/github-publish", payload["deployment"]["command"])
        revoke_relay_session.assert_not_called()

    def test_regenerate_runner_provision_does_not_revoke_existing_relay_session(self) -> None:
        runner = {
            "runner_id": "edge-1",
            "name": "Edge Runner",
            "address": "http://pulseguard-relay:18001",
            "network_region": "edge",
            "status": "pending_deployment",
            "enabled": True,
            "role": "child",
            "connection_mode": "relay",
            "available": False,
            "token_set": True,
            "token_hint": "abc123",
            "relay_token": "pgrl_rotated-token",
            "worker_token": "pgrn_worker-token",
            "relay_token_version": 2,
            "deploy_command_expires_at": "2026-06-19T00:00:00+08:00",
        }

        with patch("backend.app.main.RELAY_ENABLED", True), patch(
            "backend.app.main.RELAY_CONTROL_URL", "http://pulseguard-relay:18000"
        ), patch("backend.app.main.RELAY_PUBLIC_HOST", "203.0.113.10"), patch(
            "backend.app.main.RELAY_PUBLIC_PORT", 9443
        ), patch("backend.app.main.relay_fingerprint", return_value="abc" * 21 + "a"), patch(
            "backend.app.main.relay_control_token", return_value="pgrc_secret"
        ), patch("backend.app.main.httpx.Client") as httpx_client, patch(
            "backend.app.main.storage.regenerate_probe_runner_provision", return_value=runner
        ), patch(
            "backend.app.main.storage.record_audit_event"
        ) as record_audit_event, patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).post(
                "/api/runners/edge-1/provision/regenerate",
                json={
                    "name": "Edge Runner",
                    "network_region": "edge",
                    "target_platform": "linux",
                    "compose_url": "https://deploy.example.com/custom-relay-worker.yml",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["relay_token_version"], 2)
        self.assertEqual(record_audit_event.call_args.args[5], {"relay_control_revoked": False})
        httpx_client.assert_not_called()

    def test_update_child_runner_normalizes_bare_address(self) -> None:
        current = {"runner_id": "office-1", "name": "Office Runner", "role": "child"}
        updated = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8788",
            "network_region": "office-lan",
            "role": "child",
        }

        with patch("backend.app.main.storage.get_probe_runner", return_value=current), patch(
            "backend.app.main.storage.update_probe_runner", return_value=updated
        ) as update_probe_runner, patch("backend.app.main.storage.record_audit_event"), patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).put(
                "/api/runners/office-1",
                json={
                    "name": "Office Runner",
                    "address": "10.0.0.8",
                    "network_region": "office-lan",
                    "allow_insecure_http": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(update_probe_runner.call_args.args[1]["address"], "http://10.0.0.8:8788")
        self.assertTrue(update_probe_runner.call_args.args[1]["allow_insecure_http"])

    def test_update_local_runner_preserves_local_address(self) -> None:
        current = {"runner_id": "local", "name": "local", "role": "local"}
        updated = {
            "runner_id": "local",
            "name": "local",
            "address": "127.0.0.1",
            "network_region": "local",
            "role": "local",
        }

        with patch("backend.app.main.storage.get_probe_runner", return_value=current), patch(
            "backend.app.main.storage.update_probe_runner", return_value=updated
        ) as update_probe_runner, patch("backend.app.main.storage.record_audit_event"), patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).put(
                "/api/runners/local",
                json={"name": "local", "address": "127.0.0.1", "network_region": "local"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(update_probe_runner.call_args.args[1]["address"], "127.0.0.1")

    def test_disabling_relay_runner_revokes_session(self) -> None:
        current = {"runner_id": "edge-1", "name": "Edge Runner", "role": "child", "connection_mode": "relay"}
        updated = {
            "runner_id": "edge-1",
            "name": "Edge Runner",
            "address": "http://pulseguard-relay:18001",
            "network_region": "edge",
            "role": "child",
            "connection_mode": "relay",
            "enabled": False,
        }

        with patch("backend.app.main.storage.get_probe_runner", return_value=current), patch(
            "backend.app.main.storage.update_probe_runner", return_value=updated
        ), patch("backend.app.main._revoke_relay_session") as revoke_relay_session, patch(
            "backend.app.main.storage.record_audit_event"
        ), patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).put(
                "/api/runners/edge-1",
                json={"name": "Edge Runner", "address": "http://pulseguard-relay:18001", "network_region": "edge", "enabled": False},
            )

        self.assertEqual(response.status_code, 200)
        revoke_relay_session.assert_called_once_with("edge-1", "runner disabled")

    def test_delete_relay_runner_deletes_before_best_effort_revoke(self) -> None:
        current = {"runner_id": "edge-1", "name": "Edge Runner", "role": "child", "connection_mode": "relay"}
        calls: list[str] = []

        def delete_probe_runner_side_effect(runner_id: str) -> bool:
            calls.append(f"delete:{runner_id}")
            return True

        def revoke_relay_session_side_effect(runner_id: str, reason: str) -> bool:
            calls.append(f"revoke:{runner_id}:{reason}")
            return False

        with patch("backend.app.main.storage.get_probe_runner", return_value=current), patch(
            "backend.app.main._revoke_relay_session", side_effect=revoke_relay_session_side_effect
        ) as revoke_relay_session, patch(
            "backend.app.main.storage.delete_probe_runner", side_effect=delete_probe_runner_side_effect
        ) as delete_probe_runner, patch(
            "backend.app.main.storage.record_audit_event"
        ):
            response = TestClient(app).delete("/api/runners/edge-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["delete:edge-1", "revoke:edge-1:runner deleted"])
        revoke_relay_session.assert_called_once_with("edge-1", "runner deleted")
        delete_probe_runner.assert_called_once_with("edge-1")

    def test_rotate_runner_token_returns_new_one_time_token_response(self) -> None:
        runner = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8787",
            "network_region": "office-lan",
            "browser_version": "",
            "status": "ok",
            "enabled": True,
            "role": "child",
            "available": True,
            "token_set": True,
            "token_hint": "secret",
            "token": "runner-token-secret-2",
            "token_value": "runner-token-secret-2",
        }

        with patch("backend.app.main.storage.rotate_probe_runner_token", return_value=runner) as rotate_probe_runner_token, patch(
            "backend.app.main.storage.record_audit_event"
        ) as record_audit_event, patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).post("/api/runners/office-1/rotate-token")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["token"], "runner-token-secret-2")
        self.assertNotIn("token_value", payload)
        rotate_probe_runner_token.assert_called_once_with("office-1")
        record_audit_event.assert_called_once()

    def test_runner_update_route_pushes_update_to_child_worker(self) -> None:
        runner = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8788",
            "role": "child",
        }
        worker_result = {"ok": True, "message": "accepted"}

        with patch("backend.app.main.storage.get_probe_runner", return_value=runner), patch(
            "backend.app.main.storage.mark_probe_runner_updating"
        ) as mark_probe_runner_updating, patch(
            "backend.app.main.storage.restore_probe_runner_status"
        ) as restore_probe_runner_status, patch(
            "backend.app.main._worker_request", new=AsyncMock(return_value=worker_result)
        ) as worker_request, patch("backend.app.main.storage.record_audit_event") as record_audit_event:
            response = TestClient(app).post(
                "/api/runners/office-1/update",
                json={"target_image": "pulseguard-worker:new"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker"], worker_result)
        mark_probe_runner_updating.assert_called_once_with("office-1")
        restore_probe_runner_status.assert_not_called()
        worker_request.assert_awaited_once_with(
            runner,
            "POST",
            "/api/worker/update",
            {"target_image": "pulseguard-worker:new", "force": False},
            timeout=10,
        )
        record_audit_event.assert_called_once()

    def test_runner_update_route_restores_runner_status_when_dispatch_fails(self) -> None:
        runner = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8788",
            "role": "child",
            "status": "available",
        }

        with patch("backend.app.main.storage.get_probe_runner", return_value=runner), patch(
            "backend.app.main.storage.mark_probe_runner_updating"
        ) as mark_probe_runner_updating, patch(
            "backend.app.main.storage.restore_probe_runner_status"
        ) as restore_probe_runner_status, patch(
            "backend.app.main._worker_request", new=AsyncMock(side_effect=HTTPException(status_code=400, detail="updater unavailable"))
        ), patch("backend.app.main.storage.record_audit_event") as record_audit_event:
            response = TestClient(app).post(
                "/api/runners/office-1/update",
                json={"target_image": "pulseguard-worker:new"},
            )

        self.assertEqual(response.status_code, 400)
        mark_probe_runner_updating.assert_called_once_with("office-1")
        restore_probe_runner_status.assert_called_once_with("office-1", "available")
        record_audit_event.assert_not_called()

    def test_runner_heartbeat_route_upserts_runner(self) -> None:
        runner = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8787",
            "network_region": "office-lan",
            "browser_version": "chromium 120.0",
            "status": "ok",
            "metadata": {"capability": "ui"},
            "last_seen_at": "2026-06-08T12:00:00+08:00",
            "updated_at": "2026-06-08T12:00:00+08:00",
        }

        with patch("backend.app.main.storage.upsert_probe_runner", return_value=runner) as upsert_probe_runner, patch(
            "backend.app.main.storage.verify_probe_runner_token", return_value=runner
        ) as verify_probe_runner_token, patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).post(
                "/api/runners/heartbeat",
                headers={"Authorization": "Bearer token-1"},
                json={
                    "runner_id": "office-1",
                    "name": "Office Runner",
                    "address": "http://10.0.0.8:8787",
                    "network_region": "office-lan",
                    "browser_version": "chromium 120.0",
                    "status": "ok",
                    "metadata": {"capability": "ui"},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["runner"]["runner_id"], "office-1")
        verify_probe_runner_token.assert_called_once_with("office-1", "token-1")
        upsert_probe_runner.assert_called_once()
        self.assertEqual(upsert_probe_runner.call_args.args[0]["network_region"], "office-lan")

    def test_runner_test_connection_fetches_worker_health_and_marks_available(self) -> None:
        runner = {
            "runner_id": "office-1",
            "name": "Office Runner",
            "address": "http://10.0.0.8:8788",
            "network_region": "office-lan",
            "role": "child",
            "enabled": True,
            "available": False,
        }
        health = {
            "ok": True,
            "name": "worker",
            "address": "http://10.0.0.8:8788",
            "status": "ok",
            "browser_version": "chromium 120",
            "metadata": {"node_role": "worker"},
        }

        with patch("backend.app.main.storage.get_probe_runner", return_value=runner), patch(
            "backend.app.main._fetch_runner_health", new=AsyncMock(return_value=health)
        ) as fetch_runner_health, patch(
            "backend.app.main.storage.mark_probe_runner_available"
        ) as mark_probe_runner_available:
            response = TestClient(app).post("/api/runners/office-1/test")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker"], health)
        fetch_runner_health.assert_awaited_once_with(runner)
        mark_probe_runner_available.assert_called_once_with("office-1", health)

    def test_runners_route_lists_probe_runners(self) -> None:
        runners = [
            {
                "runner_id": "office-1",
                "name": "Office Runner",
                "address": "http://10.0.0.8:8787",
                "network_region": "office-lan",
                "browser_version": "chromium 120.0",
                "status": "ok",
                "metadata": {},
                "last_seen_at": "2026-06-08T12:00:00+08:00",
                "updated_at": "2026-06-08T12:00:00+08:00",
            }
        ]

        with patch("backend.app.main.storage.list_probe_runners", return_value=runners) as list_probe_runners, patch(
            "backend.app.main.storage.get_settings", return_value={}
        ):
            response = TestClient(app).get("/api/runners?limit=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["runner_id"], "office-1")
        list_probe_runners.assert_called_once_with(limit=10)


class RunRouteTests(ProjectScopedRouteTests):
    def test_run_detail_uses_ui_snapshot_request_info_without_loading_current_check(self) -> None:
        run = {
            "id": 41,
            "check_id": 7,
            "check_name": "Checkout UI",
            "check_type": "ui",
            "status": "failed",
            "started_at": "2026-06-12T10:00:00+08:00",
            "finished_at": "2026-06-12T10:00:01+08:00",
            "duration_ms": 1200,
            "request_snapshot": json.dumps(
                {
                    "type": "ui",
                    "url": "https://run.example.com/checkout",
                    "timeout_ms": 9000,
                    "viewport_mode": "h5",
                }
            ),
            "response_snapshot": json.dumps(
                {
                    "page": {
                        "url": "https://current-response.example.com/checkout",
                        "viewport_mode": "web",
                    },
                    "timings": {"assertion_timeout_ms": 10000},
                }
            ),
            "trigger": "manual",
            "observation_kind": "observation",
            "affects_health": True,
            "created_at": "2026-06-12T10:00:00+08:00",
        }
        settings = {
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ]
        }

        with patch("backend.app.main.storage.get_run", return_value=run), patch(
            "backend.app.main.storage.get_check"
        ) as get_check, patch(
            "backend.app.main.storage.get_settings", return_value=settings
        ):
            response = TestClient(app).get("/api/runs/41")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["check"]["id"], 7)
        self.assertEqual(payload["check"]["name"], "Checkout UI")
        self.assertEqual(payload["check"]["type"], "ui")
        self.assertEqual(payload["check"]["entry_url"], "https://run.example.com/checkout")
        self.assertEqual(payload["check"]["timeout_ms"], 9000)
        self.assertEqual(payload["check"]["viewport_mode"], "h5")
        get_check.assert_not_called()

    def test_run_detail_keeps_api_snapshots_without_extra_request_config(self) -> None:
        run = {
            "id": 42,
            "check_id": 8,
            "check_name": "Checkout API",
            "check_type": "api",
            "status": "ok",
            "started_at": "2026-06-12T10:00:00+08:00",
            "finished_at": "2026-06-12T10:00:01+08:00",
            "duration_ms": 321,
            "request_snapshot": json.dumps({"method": "POST", "url": "https://run.example.com/api", "headers": {}}),
            "response_snapshot": json.dumps({"status_code": 200, "body": "{}"}),
            "trigger": "manual",
            "observation_kind": "observation",
            "affects_health": True,
            "created_at": "2026-06-12T10:00:00+08:00",
        }

        with patch("backend.app.main.storage.get_run", return_value=run), patch(
            "backend.app.main.storage.get_check"
        ) as get_check, patch("backend.app.main.storage.get_settings", return_value={}):
            response = TestClient(app).get("/api/runs/42")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["check"])
        self.assertEqual(json.loads(payload["request_snapshot"])["url"], "https://run.example.com/api")
        self.assertEqual(json.loads(payload["response_snapshot"])["status_code"], 200)
        get_check.assert_not_called()

    def test_monitoring_trends_route_passes_filters(self) -> None:
        payload = {"period": "7d", "start": "s", "end": "e", "summaries": [], "tasks": {"items": [], "total": 0, "page": 2, "page_size": 12}}
        with patch("backend.app.main.storage.list_monitoring_trends", return_value=payload) as list_monitoring_trends:
            response = TestClient(app).get("/api/monitoring-trends?period=7d&type=api&q=health&page=2&page_size=12")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        list_monitoring_trends.assert_called_once_with(
            period="7d",
            start=None,
            end=None,
            check_type="api",
            q="health",
            page=2,
            page_size=12,
            hour_start=None,
            hour_end=None,
            project_id=1,
        )

    def test_check_trend_route_returns_404_for_missing_check(self) -> None:
        with patch("backend.app.main.storage.get_check_trend", return_value=None):
            response = TestClient(app).get("/api/checks/404/trend")

        self.assertEqual(response.status_code, 404)

    def test_runs_route_accepts_run_group_filter(self) -> None:
        with patch("backend.app.main.storage.list_runs", return_value=[]) as list_runs:
            response = TestClient(app).get("/api/runs?run_group_id=rg-test&limit=25")

        self.assertEqual(response.status_code, 200)
        filters = list_runs.call_args.args[0]
        self.assertEqual(filters["run_group_id"], "rg-test")
        self.assertEqual(list_runs.call_args.kwargs["limit"], 25)

    def test_runs_page_route_accepts_run_group_filter(self) -> None:
        with patch(
            "backend.app.main.storage.list_runs_page",
            return_value={"items": [], "total": 0, "page": 1, "page_size": 20},
        ) as list_runs_page:
            response = TestClient(app).get("/api/runs-page?run_group_id=rg-test&page=1&page_size=20")

        self.assertEqual(response.status_code, 200)
        filters = list_runs_page.call_args.args[0]
        self.assertEqual(filters["run_group_id"], "rg-test")


class OperationsRouteTests(ProjectScopedRouteTests):
    def test_status_page_returns_sanitized_internal_snapshot(self) -> None:
        settings = {
            "maintenance_enabled": True,
            "maintenance_title": "DB maintenance",
            "maintenance_message": "Read-only window",
            "maintenance_starts_at": "2026-06-08T22:00:00+08:00",
            "maintenance_ends_at": "2026-06-08T23:00:00+08:00",
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ],
        }
        checks = [
            {
                "id": 1,
                "name": "API",
                "type": "api",
                "enabled": True,
                "tags": "public",
                "current_status": "failed",
                "last_run_at": "2026-06-08T12:00:00+08:00",
                "last_error": "token-secret-123 failed",
                "headers_json": '{"Authorization":"Bearer token-secret-123"}',
                "script": "async def check(ctx): pass",
            }
        ]
        runs = [
            {
                "id": 2,
                "check_id": 1,
                "check_name": "API",
                "check_type": "api",
                "status": "failed",
                "started_at": "2026-06-08T12:00:00+08:00",
                "duration_ms": 120,
                "failure_kind": "target",
                "runner_name": "local",
                "runner_region": "local",
                "error_message": "token-secret-123 failed",
                "error_stack": "secret stack",
                "response_snapshot": '{"body":"token-secret-123"}',
            }
        ]

        with patch("backend.app.main.storage.get_settings", return_value=settings), patch(
            "backend.app.main.storage.get_overview", return_value={"failing_count": 1, "today_runs": 3}
        ), patch("backend.app.main.storage.list_checks", return_value=checks), patch(
            "backend.app.main.storage.list_recent_business_incidents", return_value=runs
        ):
            response = TestClient(app).get("/api/status-page")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["maintenance"]["enabled"])
        self.assertEqual(payload["summary"]["checks_failing"], 1)
        self.assertEqual(payload["checks"][0]["status"], "failed")
        self.assertEqual(payload["recent_incidents"][0]["failure_kind"], "target")
        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("token-secret-123", payload_text)
        self.assertNotIn("headers_json", payload_text)
        self.assertNotIn("script", payload_text)
        self.assertNotIn("error_stack", payload_text)

    def test_status_page_excludes_manual_verification_failures(self) -> None:
        runs = [
            {
                "id": 2,
                "check_id": 1,
                "check_name": "API",
                "check_type": "api",
                "status": "failed",
                "started_at": "2026-06-08T12:00:00+08:00",
                "failure_kind": "target",
                "affects_health": False,
            }
        ]

        with patch("backend.app.main.storage.get_settings", return_value={}), patch(
            "backend.app.main.storage.get_overview", return_value={}
        ), patch("backend.app.main.storage.list_checks", return_value=[]), patch(
            "backend.app.main.storage.list_recent_business_incidents", return_value=[]
        ):
            response = TestClient(app).get("/api/status-page")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["recent_incidents"], [])

    def test_read_only_snapshot_requires_configured_token(self) -> None:
        with patch("backend.app.main.storage.validate_read_only_token", return_value=False):
            unconfigured = TestClient(app).get("/api/read-only/snapshot")

        self.assertEqual(unconfigured.status_code, 403)

        with patch("backend.app.main.storage.validate_read_only_token", return_value=False):
            denied = TestClient(app).get("/api/read-only/snapshot?token=wrong")

        self.assertEqual(denied.status_code, 403)

        settings = {
            "read_only_token": "secret",
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ],
        }
        checks = [{"id": 1, "headers_json": '{"Authorization":"Bearer token-secret-123"}'}]
        runs = [{"id": 2, "response_snapshot": '{"body":"token-secret-123"}'}]
        overview = {
            "today_runs": 1,
            "latest_run": {
                "id": 3,
                "check_id": 1,
                "check_name": "API",
                "check_type": "api",
                "status": "failed",
                "error_stack": "stack token-secret-123",
                "response_snapshot": '{"body":"token-secret-123"}',
            },
            "recent_failures": [
                {
                    "id": 4,
                    "check_id": 1,
                    "check_name": "API",
                    "check_type": "api",
                    "status": "failed",
                    "logs": "token-secret-123",
                    "request_snapshot": '{"headers":{"Authorization":"token-secret-123"}}',
                }
            ],
        }
        with patch("backend.app.main.storage.validate_read_only_token", return_value=True), patch(
            "backend.app.main.storage.get_settings", return_value=settings
        ), patch(
            "backend.app.main.storage.get_overview", return_value=overview
        ), patch("backend.app.main.storage.list_checks", return_value=checks), patch("backend.app.main.storage.list_runs", return_value=runs):
            allowed = TestClient(app).get("/api/read-only/snapshot?token=secret")

        self.assertEqual(allowed.status_code, 200)
        payload = allowed.json()
        self.assertEqual(payload["overview"]["today_runs"], 1)
        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("token-secret-123", payload_text)
        self.assertNotIn("headers_json", payload_text)
        self.assertNotIn("response_snapshot", payload_text)
        self.assertNotIn("request_snapshot", payload_text)
        self.assertNotIn("error_stack", payload_text)
        self.assertNotIn("logs", payload_text)

    def test_read_only_snapshot_accepts_bearer_token(self) -> None:
        with patch("backend.app.main.storage.validate_read_only_token", return_value=True), patch(
            "backend.app.main.storage.get_settings", return_value={"read_only_token": "secret"}
        ), patch(
            "backend.app.main.storage.get_overview", return_value={"today_runs": 1}
        ), patch("backend.app.main.storage.list_checks", return_value=[]), patch("backend.app.main.storage.list_runs", return_value=[]):
            response = TestClient(app).get("/api/read-only/snapshot", headers={"Authorization": "Bearer secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overview"]["today_runs"], 1)

    def test_audit_events_and_version_list_mask_secret_values(self) -> None:
        settings = {
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ],
        }
        with patch("backend.app.main.storage.get_settings", return_value=settings), patch(
            "backend.app.main.storage.list_audit_events",
            return_value=[{"id": 1, "payload": {"message": "token-secret-123"}}],
        ):
            audit_response = TestClient(app).get("/api/audit-events")

        self.assertEqual(audit_response.status_code, 200)
        audit_text = json.dumps(audit_response.json(), ensure_ascii=False)
        self.assertNotIn("token-secret-123", audit_text)
        self.assertIn("***", audit_text)

        versions = [{"id": 2, "check_id": 9, "snapshot": {"headers_json": '{"Authorization":"token-secret-123"}'}}]
        with patch("backend.app.main.storage.get_check", return_value={"id": 9}), patch(
            "backend.app.main.storage.get_settings", return_value=settings
        ), patch("backend.app.main.storage.list_check_versions", return_value=versions):
            version_response = TestClient(app).get("/api/checks/9/versions")

        self.assertEqual(version_response.status_code, 200)
        version_text = json.dumps(version_response.json(), ensure_ascii=False)
        self.assertNotIn("token-secret-123", version_text)
        self.assertIn("***", version_text)

    def test_metrics_routes_return_json_and_prometheus_text(self) -> None:
        overview = {
            "failing_count": 1,
            "today_runs": 3,
            "trends": [
                {
                    "key": "24h",
                    "series": [
                        {"check_type": "ui", "runs": 1, "success_count": 1, "failure_count": 0},
                        {"check_type": "api", "runs": 3, "success_count": 2, "failure_count": 1},
                    ],
                }
            ],
        }
        checks = [{"id": 1, "enabled": True}, {"id": 2, "enabled": False}]

        with patch("backend.app.main.storage.get_overview", return_value=overview), patch(
            "backend.app.main.storage.list_checks", return_value=checks
        ):
            json_response = TestClient(app).get("/api/metrics.json")
            text_response = TestClient(app).get("/api/metrics")

        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json_response.json()["checks_total"], 2)
        self.assertIn("pulseguard_checks_total 2", text_response.text)
        self.assertIn("pulseguard_success_rate_24h 75.0", text_response.text)

    def test_failure_summary_masks_error_and_reports_runner_kind(self) -> None:
        settings = {
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ],
        }
        run = {
            "id": 8,
            "check_id": 3,
            "check_name": "API",
            "status": "failed",
            "failure_kind": "runner",
            "runner_name": "local",
            "runner_region": "office",
            "duration_ms": 25,
            "error_message": "token-secret-123 browser failed",
        }

        with patch("backend.app.main.storage.get_run", return_value=run), patch(
            "backend.app.main.storage.get_previous_successful_run", return_value=None
        ), patch(
            "backend.app.main.storage.get_settings", return_value=settings
        ):
            response = TestClient(app).get("/api/runs/8/failure-summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["failure_kind"], "runner")
        self.assertIn("Runner", payload["summary"])
        self.assertNotIn("token-secret-123", json.dumps(payload, ensure_ascii=False))

    def test_failure_summary_uses_assertion_evidence_and_success_baseline(self) -> None:
        current_assertions = [
            {
                "rule": "元素可见",
                "path": "span.store-tag:nth-of-type(2)",
                "status": "failed",
                "actual": "未找到",
                "message": "校验失败：元素未找到：span.store-tag:nth-of-type(2)",
            },
            {
                "rule": "组件不为空",
                "path": "h3.store-title",
                "status": "failed",
                "actual": "未找到",
                "message": "校验失败：元素未找到：h3.store-title",
            },
            {"rule": "文本出现", "path": "详情", "status": "ok", "actual": "出现", "message": "文本出现"},
        ]
        baseline_assertions = [
            {**current_assertions[0], "status": "ok", "actual": "可见", "message": "元素可见"},
            {**current_assertions[1], "status": "ok", "actual": "购车门店", "message": "组件不为空"},
            current_assertions[2],
        ]
        run = {
            "id": 2941,
            "check_id": 11,
            "check_name": "商品详情",
            "check_type": "ui",
            "status": "failed",
            "failure_kind": "target",
            "runner_name": "local",
            "runner_region": "local",
            "duration_ms": 8869,
            "error_message": "UI 结构化校验失败",
            "response_snapshot": json.dumps(
                {"page": {"url": "https://example.test/new"}, "assertions": current_assertions},
                ensure_ascii=False,
            ),
        }
        baseline = {
            "id": 885,
            "response_snapshot": json.dumps(
                {"page": {"url": "https://example.test/old"}, "assertions": baseline_assertions},
                ensure_ascii=False,
            ),
        }

        with patch("backend.app.main.storage.get_run", return_value=run), patch(
            "backend.app.main.storage.get_previous_successful_run", return_value=baseline
        ), patch("backend.app.main.storage.get_settings", return_value={}):
            response = TestClient(app).get("/api/runs/2941/failure-summary")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["summary"], "2/3 项 UI 校验失败，其中 2 项为相对最近成功运行的新增失败。")
        self.assertNotIn("error_message", payload)
        self.assertIn({"label": "失败来源", "value": "目标页面/API"}, payload["signals"])
        self.assertIn({"label": "对比基线", "value": "成功运行 #885"}, payload["signals"])
        self.assertNotIn("span.store-tag:nth-of-type(2)", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(
            payload["next_steps"],
            [
                "当前访问地址与成功运行 #885 不同，确认地址变更是否符合预期。",
                "在“对比”中查看 2 项新增失败的变化。",
                "在“校验”中逐项确认页面内容或校验规则是否需要更新。",
            ],
        )

    def test_failure_summary_uses_masked_target_error_when_assertions_are_unavailable(self) -> None:
        run = {
            "id": 9,
            "check_id": 3,
            "check_name": "API",
            "check_type": "api",
            "status": "failed",
            "failure_kind": "target",
            "duration_ms": 25,
            "error_message": "token-secret-123 connection refused",
        }
        settings = {
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ],
        }

        with patch("backend.app.main.storage.get_run", return_value=run), patch(
            "backend.app.main.storage.get_previous_successful_run", return_value=None
        ), patch("backend.app.main.storage.get_settings", return_value=settings):
            response = TestClient(app).get("/api/runs/9/failure-summary")

        payload = response.json()
        self.assertEqual(payload["summary"], "目标检查失败：*** connection refused")
        self.assertNotIn("token-secret-123", json.dumps(payload, ensure_ascii=False))

    def test_run_archives_route_returns_archive_summaries(self) -> None:
        archive = {
            "id": 1,
            "archive_date": "2026-06-01",
            "check_type": "api",
            "status": "ok",
            "run_count": 4,
            "duration_sum_ms": 1200,
            "duration_sample_count": 4,
            "last_run_at": "2026-06-01T23:00:00+08:00",
            "updated_at": "2026-06-08T10:00:00+08:00",
        }
        with patch("backend.app.main.storage.list_run_archives", return_value=[archive]) as list_run_archives:
            response = TestClient(app).get("/api/run-archives?limit=30")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [archive])
        list_run_archives.assert_called_once_with(limit=30, project_id=1)

    def test_restore_check_version_updates_existing_check_and_syncs_scheduler(self) -> None:
        scheduler = FakeScheduler()
        snapshot = check_snapshot("Restored API")
        restored = {**snapshot, "id": 9, "created_at": "now", "updated_at": "now", "current_status": None, "consecutive_failures": 0}

        with patch.object(app.state, "scheduler", scheduler, create=True), patch(
            "backend.app.main.storage.get_check_version",
            return_value={"id": 3, "check_id": 9, "snapshot": snapshot},
        ), patch("backend.app.main.storage.get_check", return_value={**restored, "name": "Current API"}), patch(
            "backend.app.main.storage.record_check_version"
        ) as record_check_version, patch(
            "backend.app.main.storage.update_check", return_value=restored
        ) as update_check, patch(
            "backend.app.main.storage.record_audit_event"
        ) as record_audit_event:
            response = TestClient(app).post("/api/check-versions/3/restore")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Restored API")
        update_check.assert_called_once()
        record_check_version.assert_called_once()
        record_audit_event.assert_called_once()
        self.assertEqual(scheduler.synced, [9])


def check_snapshot(name: str) -> dict[str, object]:
    return {
        "name": name,
        "type": "api",
        "enabled": True,
        "interval_seconds": 300,
        "timeout_ms": 10000,
        "entry_url": "https://example.com/health",
        "viewport_mode": "web",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": '[{"type":"status_code","enabled":true,"expected_status":200}]',
        "setup_script": "",
        "script": "",
        "tags": "audit",
        "alert_policy_json": "{}",
    }


if __name__ == "__main__":
    unittest.main()
