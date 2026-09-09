from __future__ import annotations

import copy
import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch


migration = importlib.import_module("scripts.migrate_usedcar_modules")
verification = importlib.import_module("scripts.verify_usedcar_migration")


class UsedcarMigrationSafetyTests(unittest.TestCase):
    project_id = 2

    def _inputs(self) -> tuple[list[dict[str, object]], dict[str, object]]:
        live: list[dict[str, object]] = []
        update: list[dict[str, object]] = []
        for check_id in migration.TARGET_IDS:
            script = "" if check_id in migration.STATIC_IDS else "async def check(ctx):\n    pass\n"
            payload: dict[str, object] = {
                "name": f"usedcar-{check_id}",
                "project_id": self.project_id,
                "type": "api",
                "enabled": True,
                "interval_seconds": 60,
                "timeout_ms": 10000,
                "entry_url": f"https://example.test/{check_id}",
                "method": "GET",
                "headers_json": "{}",
                "body": "",
                "assertions_json": "[]",
                "setup_script": "",
                "script": script,
                "tags": "usedcar",
                "alert_policy_json": "{}",
                "runner_selection_mode": "selected_parallel",
                "runner_ids": ["local"],
                "browser_selection_mode": "selected_parallel",
                "browser_types": [],
            }
            live.append({"id": check_id, **copy.deepcopy(payload)})
            update_payload = copy.deepcopy(payload)
            update_payload.pop("project_id", None)
            update.append({"id": check_id, "payload": update_payload})
        return live, {"checks": update}

    def _plan(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        live, update = self._inputs()
        source = (
            Path(__file__).resolve().parents[2] / "scripts" / "examples" / "usedcar_module.py"
        ).read_text(encoding="utf-8")
        plan = migration._build_plan(live, update, source, self.project_id)
        return plan, live

    def test_apply_rejects_full_configuration_drift_before_any_write(self) -> None:
        plan, live = self._plan()
        remote = copy.deepcopy(live)
        remote[0]["entry_url"] = "https://changed.example/34"
        calls: list[tuple[str, str]] = []

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            calls.append((kwargs.get("method", "GET") if isinstance(kwargs.get("method"), str) else "GET", path))
            return remote

        with patch.object(migration, "_api_json", side_effect=fake_api):
            with self.assertRaisesRegex(migration.MigrationPlanError, "完整配置指纹"):
                migration._apply(plan, "http://example.test", self.project_id, publish=True)
        self.assertEqual(calls, [("GET", "/api/checks?type=api")])

    def test_apply_requires_publish_gate(self) -> None:
        plan, _ = self._plan()
        with self.assertRaisesRegex(migration.MigrationPlanError, "--publish"):
            migration._apply(plan, "http://example.test", self.project_id, publish=False)

    def test_apply_rejects_nonempty_library_instead_of_implicit_merge(self) -> None:
        plan, live = self._plan()
        calls: list[tuple[str, str]] = []

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            calls.append((str(kwargs.get("method") or "GET"), path))
            if path == "/api/checks?type=api":
                return live
            if path == f"/api/projects/{self.project_id}/library":
                return {"draft_revision": 2, "modules": [{"name": "other", "source": "x"}], "published": None}
            raise AssertionError((path, kwargs))

        with patch.object(migration, "_api_json", side_effect=fake_api):
            with self.assertRaisesRegex(migration.MigrationPlanError, "非空"):
                migration._apply(plan, "http://example.test", self.project_id, publish=True)
        self.assertEqual(calls[-1], ("GET", f"/api/projects/{self.project_id}/library"))

    def test_apply_publishes_merged_module_then_canary_and_rolls_back_on_manual_failure(self) -> None:
        plan, live = self._plan()
        remote = copy.deepcopy(live)
        calls: list[tuple[str, str, object]] = []
        validation_statuses = iter(["queued", "running", "succeeded"])
        updated_41: dict[str, object] | None = None

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            nonlocal updated_41
            method = str(kwargs.get("method") or "GET")
            payload = kwargs.get("payload")
            calls.append((method, path, payload))
            if method == "GET" and path == "/api/checks?type=api":
                return remote
            if method == "GET" and path == f"/api/projects/{self.project_id}/library":
                return {"draft_revision": 3, "modules": [], "published": None}
            if method == "PUT" and path.endswith("/library/draft"):
                assert isinstance(payload, dict)
                names = [str(item["name"]) for item in payload["modules"]]
                if names != ["usedcar"]:
                    raise AssertionError(names)
                return {"draft_revision": 4}
            if method == "POST" and path.endswith("/library/validate"):
                return {"validation_id": 9}
            if method == "GET" and path.endswith("/library/validations/9"):
                return {"status": next(validation_statuses)}
            if method == "POST" and path.endswith("/library/publish"):
                return {"version_id": 12}
            if method == "POST" and path == "/api/checks/debug":
                return {"status": "ok"}
            if method == "GET" and path == "/api/checks/41":
                if updated_41 is None:
                    return remote[7]
                return {"id": 41, "project_id": self.project_id, **updated_41}
            if method == "PUT" and path == "/api/checks/41":
                updated_41 = dict(payload) if isinstance(payload, dict) else None
                return {"id": 41}
            if method == "POST" and path == "/api/checks/41/run":
                return {"status": "failed"}
            raise AssertionError((method, path, payload))

        with patch.object(migration, "_api_json", side_effect=fake_api), patch.object(migration.time, "sleep"):
            with self.assertRaisesRegex(migration.MigrationPlanError, "任务 41 手动运行.*已回滚"):
                migration._apply(plan, "http://example.test", self.project_id, publish=True)

        paths = [(method, path) for method, path, _ in calls]
        publish_index = paths.index(("POST", f"/api/projects/{self.project_id}/library/publish"))
        first_check_write = paths.index(("PUT", "/api/checks/41"))
        self.assertLess(publish_index, first_check_write)
        self.assertEqual(paths[-1], ("PUT", "/api/checks/41"))
        rollback_payload = calls[-1][2]
        self.assertEqual(rollback_payload, migration._check_update_payload(live[7]))

    def test_apply_rolls_back_when_canary_put_commits_before_transport_failure(self) -> None:
        plan, live = self._plan()
        remote = copy.deepcopy(live)
        calls: list[tuple[str, str, object]] = []
        updated_41: dict[str, object] | None = None
        put_failed = False

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            nonlocal updated_41, put_failed
            method = str(kwargs.get("method") or "GET")
            payload = kwargs.get("payload")
            calls.append((method, path, payload))
            if path == "/api/checks?type=api":
                return remote
            if path == f"/api/projects/{self.project_id}/library":
                return {"draft_revision": 1, "modules": [], "published": None}
            if method == "PUT" and path.endswith("/library/draft"):
                return {"draft_revision": 2}
            if method == "POST" and path.endswith("/library/validate"):
                return {"validation_id": 3}
            if method == "GET" and path.endswith("/library/validations/3"):
                return {"status": "succeeded"}
            if method == "POST" and path.endswith("/library/publish"):
                return {"version_id": 4}
            if method == "POST" and path == "/api/checks/debug":
                return {"status": "ok"}
            if method == "GET" and path == "/api/checks/41":
                if updated_41 is None:
                    return remote[7]
                return {"id": 41, "project_id": self.project_id, **updated_41}
            if method == "PUT" and path == "/api/checks/41":
                if not put_failed:
                    updated_41 = dict(payload) if isinstance(payload, dict) else None
                    put_failed = True
                    raise TimeoutError("write acknowledgement lost")
                return {"id": 41}
            raise AssertionError((method, path, payload))

        with patch.object(migration, "_api_json", side_effect=fake_api):
            with self.assertRaisesRegex(migration.MigrationPlanError, "迁移写入失败.*已回滚"):
                migration._apply(plan, "http://example.test", self.project_id, publish=True)

        self.assertEqual(calls[-1][0:2], ("PUT", "/api/checks/41"))
        self.assertEqual(calls[-1][2], migration._check_update_payload(live[7]))

    def test_verification_does_not_count_timeout_as_intended_negative(self) -> None:
        result = {
            "status": "timeout",
            "request_snapshot": json.dumps({"diagnostics": {"http_requests": [{}]}}),
            "response_snapshot": None,
        }
        row = verification._result_row(result, expected_requests=1, expected_assertions=4)
        self.assertEqual(row["status"], "timeout")
        self.assertFalse(row["intended_status_assertion_failed"])


if __name__ == "__main__":
    unittest.main()
