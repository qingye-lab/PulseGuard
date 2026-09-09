from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import config_transfer, storage
from backend.app.main import app


class FakeRunner:
    def __init__(self) -> None:
        self.reload_count = 0

    async def reload_settings(self) -> None:
        self.reload_count += 1


class FakeScheduler:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_all(self) -> None:
        self.refresh_count += 1


class ConfigTransferApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        storage.init_db()
        self.project_id = storage.default_project_id()
        self.headers = {"X-PulseGuard-Project-Id": str(self.project_id)}

        self.runner = FakeRunner()
        self.scheduler = FakeScheduler()
        self.runner_patch = patch.object(app.state, "runner", self.runner, create=True)
        self.scheduler_patch = patch.object(app.state, "scheduler", self.scheduler, create=True)
        self.runner_patch.start()
        self.scheduler_patch.start()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.scheduler_patch.stop()
        self.runner_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_system_export_v4_contains_only_global_settings_and_runners(self) -> None:
        storage.update_settings(
            {
                "members": [{"id": "alice", "name": "Alice", "feishu_open_id": "ou-secret"}],
                "environment_variables": [
                    {"id": "secret", "name": "SERVICE_TOKEN", "value": "token-secret", "secret": True}
                ],
                "system_notification_channels": [
                    {
                        "id": "system",
                        "name": "System",
                        "type": "dingtalk",
                        "enabled": True,
                        "webhook_url": "https://example.test/system",
                        "dingtalk_secret": "SEC-sign-secret",
                    }
                ],
            }
        )
        storage.create_check(api_check_payload("Project task"), project_id=self.project_id)
        storage.create_probe_runner(
            {
                "runner_id": "edge-secret",
                "name": "Edge Runner",
                "address": "http://10.0.0.8:8787",
                "network_region": "edge",
                "token": "runner-secret",
                "allow_insecure_http": True,
            }
        )

        response = self.client.get("/api/config/export")

        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertEqual(exported["schema"], "pulseguard.config")
        self.assertEqual(exported["version"], 4)
        self.assertNotIn("checks", exported)
        self.assertNotIn("project", exported)
        self.assertNotIn("alerts", exported)
        self.assertEqual(exported["settings"]["members"], [])
        exported_text = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn("Project task", exported_text)
        self.assertNotIn("ou-secret", exported_text)
        self.assertNotIn("token-secret", exported_text)
        self.assertNotIn("sign-secret", exported_text)
        self.assertNotIn("runner-secret", exported_text)

    def test_project_export_v2_contains_only_selected_project_data(self) -> None:
        other = storage.create_project("Other")
        storage.create_check(api_check_payload("Shared name"), project_id=self.project_id)
        storage.create_check(api_check_payload("Other only"), project_id=int(other["id"]))
        storage.update_project_alert_settings(
            self.project_id,
            {
                "alerts_enabled": True,
                "notification_channels": [
                    {
                        "id": "ops",
                        "name": "Ops",
                        "type": "feishu",
                        "enabled": True,
                        "webhook_url": "https://example.test/project",
                    }
                ],
                "execution_notification_channel_ids": ["ops"],
            },
        )

        response = self.client.get("/api/project-config/export", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertEqual(exported["schema"], "pulseguard.project")
        self.assertEqual(exported["version"], 2)
        self.assertIn("Shared name", [check["name"] for check in exported["checks"]])
        self.assertNotIn("settings", exported)
        self.assertNotIn("runners", exported)
        self.assertNotIn("Other only", json.dumps(exported, ensure_ascii=False))
        self.assertNotIn("https://example.test/project", json.dumps(exported, ensure_ascii=False))

    def test_project_import_conflicts_are_computed_only_in_target_project(self) -> None:
        other = storage.create_project("Other")
        storage.create_check(api_check_payload("Duplicate"), project_id=int(other["id"]))
        bundle = project_config(checks=[api_check_payload("Duplicate")])

        first_preview = self.client.post(
            "/api/project-config/import-preview", json={"bundle": bundle}, headers=self.headers
        )
        applied = self.client.post(
            "/api/project-config/import", json={"bundle": bundle}, headers=self.headers
        )
        second_preview = self.client.post(
            "/api/project-config/import-preview", json={"bundle": bundle}, headers=self.headers
        )

        self.assertEqual(first_preview.status_code, 200)
        self.assertEqual(first_preview.json()["counts"]["conflicts"], 0)
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["checks_created"], 1)
        self.assertEqual(second_preview.json()["counts"]["conflicts"], 1)
        self.assertIsNotNone(find_check_by_name("Duplicate", self.project_id))
        self.assertEqual(self.scheduler.refresh_count, 1)

    def test_project_import_blocks_missing_member_and_channel_references(self) -> None:
        check = api_check_payload(
            "Invalid references",
            alert_policy_json=json.dumps(
                {"member_ids": ["missing-member"], "notification_channel_ids": ["missing-channel"]}
            ),
        )
        bundle = project_config(checks=[check])

        response = self.client.post(
            "/api/project-config/import-preview", json={"bundle": bundle}, headers=self.headers
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["valid"])
        issues = "；".join(response.json()["issues"])
        self.assertIn("missing-member", issues)
        self.assertIn("missing-channel", issues)
        self.assertIsNone(find_check_by_name("Invalid references", self.project_id))

    def replacement_bundle(self) -> dict[str, Any]:
        def channel(name: str) -> dict[str, Any]:
            return {"id": name, "name": name, "type": "dingtalk", "enabled": False,
                    "webhook_url": f"https://example.test/{name}"}
        storage.update_project_alert_settings(self.project_id, {"notification_channels": [channel("old")]})
        storage.create_check(api_check_payload("Replace", alert_policy_json=json.dumps(
            {"notification_channel_ids": ["old"]})), project_id=self.project_id)
        bundle = project_config([api_check_payload("Replace", alert_policy_json=json.dumps(
            {"notification_channel_ids": ["new"]}))], {"notification_channels": [channel("new")]})
        bundle["variables"] = [{"id": "v", "name": "VALUE", "value": "new", "secret": False}]
        return bundle

    def test_replacement_validates_final_channel_references(self) -> None:
        bundle = self.replacement_bundle()
        self.assertTrue(config_transfer.preview_project_import(bundle, self.project_id)["valid"])
        response = self.client.post("/api/project-config/import", headers=self.headers,
                                    json={"bundle": bundle, "replace_existing": True})
        self.assertEqual(response.status_code, 200, response.text)
        check = find_check_by_name("Replace", self.project_id)
        self.assertEqual(json.loads(check["alert_policy_json"])["notification_channel_ids"], ["new"])
        self.assertEqual(storage.get_project_alert_settings(self.project_id)["notification_channels"][0]["id"], "new")
        self.assertEqual(storage.get_project_variables(self.project_id)[0]["value"], "new")

    def test_failed_replacement_preserves_entire_project(self) -> None:
        bundle = self.replacement_bundle()
        storage.create_check(api_check_payload("Untouched", alert_policy_json=json.dumps(
            {"notification_channel_ids": ["old"]})), project_id=self.project_id)
        before = config_transfer.export_project(self.project_id, redact=False)
        check_id = find_check_by_name("Replace", self.project_id)["id"]
        versions = storage.list_check_versions(check_id)
        with self.assertRaisesRegex(ValueError, "Untouched"):
            config_transfer.apply_project_import(bundle, self.project_id, replace_existing=True)
        after = config_transfer.export_project(self.project_id, redact=False)
        for key in ("checks", "alerts", "variables"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(storage.list_check_versions(check_id), versions)

    def test_late_import_failure_rolls_back_tasks_and_versions(self) -> None:
        bundle = self.replacement_bundle()
        before = config_transfer.export_project(self.project_id, redact=False)
        with patch.object(storage, "update_project_alert_settings", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                config_transfer.apply_project_import(bundle, self.project_id, replace_existing=True)
        after = config_transfer.export_project(self.project_id, redact=False)
        for key in ("checks", "alerts", "variables"):
            self.assertEqual(before[key], after[key])

    def test_system_import_rejects_old_combined_bundle_and_imports_runners(self) -> None:
        old_bundle = {
            "version": 3,
            "settings": {},
            "runners": [],
            "checks": [api_check_payload("Old task")],
        }
        old_preview = self.client.post("/api/config/import-preview", json={"bundle": old_bundle})

        bundle = system_config(
            runners=[
                {
                    "runner_id": "edge-import",
                    "name": "Imported Edge",
                    "address": "http://10.0.0.12:8787",
                    "network_region": "edge",
                    "enabled": True,
                    "allow_insecure_http": True,
                }
            ]
        )
        preview = self.client.post("/api/config/import-preview", json={"bundle": bundle})
        applied = self.client.post("/api/config/import", json={"bundle": bundle})

        self.assertEqual(old_preview.status_code, 200)
        self.assertFalse(old_preview.json()["valid"])
        self.assertIn("旧 v3", "；".join(old_preview.json()["issues"]))
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()["valid"])
        self.assertEqual(preview.json()["counts"]["runners"], 1)
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["runners_imported"], 1)
        self.assertIsNotNone(storage.get_probe_runner("edge-import"))
        self.assertEqual(self.runner.reload_count, 1)

    def test_system_import_requires_explicit_manual_http_confirmation(self) -> None:
        runner = {
            "runner_id": "edge-current-http",
            "name": "Current HTTP Runner",
            "address": "http://10.0.0.13:8787",
            "network_region": "edge",
            "enabled": True,
        }
        preview = self.client.post(
            "/api/config/import-preview", json={"bundle": system_config(runners=[runner])}
        )

        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.json()["valid"])
        self.assertTrue(any("allow_insecure_http=true" in issue for issue in preview.json()["issues"]))


def system_config(
    settings: dict[str, Any] | None = None, runners: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "schema": "pulseguard.config",
        "version": config_transfer.EXPORT_SCHEMA_VERSION,
        "exported_at": "2026-09-02T00:00:00+08:00",
        "settings": settings or {},
        "runners": runners or [],
    }


def project_config(
    checks: list[dict[str, Any]], alerts: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema": "pulseguard.project",
        "version": config_transfer.PROJECT_EXPORT_SCHEMA_VERSION,
        "exported_at": "2026-09-02T00:00:00+08:00",
        "project": {"name": "Source"},
        "alerts": alerts or {},
        "checks": checks,
    }


def api_check_payload(
    name: str,
    entry_url: str = "https://import.example.com/api",
    alert_policy_json: str = "{}",
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "api",
        "enabled": True,
        "interval_seconds": 120,
        "timeout_ms": 10000,
        "entry_url": entry_url,
        "viewport_mode": "web",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": json.dumps(
            [{"id": "status-200", "type": "status_code", "enabled": True, "expected_status": 200}]
        ),
        "setup_script": "",
        "script": "",
        "tags": "imported",
        "alert_policy_json": alert_policy_json,
    }


def find_check_by_name(name: str, project_id: int) -> dict[str, Any] | None:
    return next(
        (check for check in storage.list_checks(project_id=project_id) if check["name"] == name),
        None,
    )


if __name__ == "__main__":
    unittest.main()
