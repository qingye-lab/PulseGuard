from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
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
        self.synced_check_ids: list[int] = []
        self.refresh_count = 0

    def sync_check(self, check_id: int) -> None:
        self.synced_check_ids.append(check_id)

    def refresh_all(self) -> None:
        self.refresh_count += 1


class ConfigTransferApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        storage.init_db()

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

    def test_export_contains_transfer_envelope_and_redacts_secret_values_by_default(self) -> None:
        task_policy = {
            "alert_cooldown_minutes": 15,
            "recovery_notification": False,
            "notification_channel_ids": ["ding"],
            "member_ids": ["alice"],
        }
        tag_policy = {
            "id": "critical-api",
            "name": "Critical API",
            "tag": "critical",
            "enabled": True,
            "alert_cooldown_minutes": 10,
            "recovery_notification": False,
            "notification_channel_ids": ["ding"],
        }
        storage.update_settings(
            {
                "alerts_enabled": True,
                "notification_channels": [
                    {
                        "id": "ding",
                        "name": "DingTalk",
                        "type": "dingtalk",
                        "enabled": True,
                        "webhook_url": "${DING_WEBHOOK}",
                        "dingtalk_secret": "SEC-dingtalk-signing-secret",
                    }
                ],
                "members": [
                    {
                        "id": "alice",
                        "name": "Alice",
                        "feishu_open_id": "ou_alice_private",
                        "wecom_user_id": "alice.private",
                        "wecom_mobile": "13800000001",
                        "dingtalk_user_id": "",
                        "dingtalk_mobile": "",
                    }
                ],
                "alert_tag_policies": [
                    {
                        **tag_policy,
                        "webhook_url": "https://tag-policy-webhook.example/hook",
                        "dingtalk_secret": "SEC-tag-policy-secret",
                    }
                ],
                "environment_variables": [
                    {"id": "host", "name": "API_HOST", "value": "https://api.internal.example", "secret": False},
                    {"id": "explicit", "name": "SESSION_ID", "value": "session-secret-123", "secret": True},
                    {"id": "named", "name": "SERVICE_TOKEN", "value": "token-secret-456", "secret": False},
                ],
                "read_only_token": "read-token-secret-999",
            }
        )
        storage.create_check(
            api_check_payload(
                "Secret-bearing API",
                entry_url="${API_HOST}/health",
                alert_policy_json=json.dumps(task_policy, ensure_ascii=False),
            )
        )
        storage.create_probe_runner(
            {
                "runner_id": "edge-secret",
                "name": "Edge Runner",
                "address": "http://10.0.0.8:8787",
                "network_region": "edge",
                "token": "runner-secret-export",
                "allow_insecure_http": True,
            }
        )

        with patch.dict("os.environ", {"PROCESS_PASSWORD": "process-secret-789"}, clear=False):
            response = self.client.get("/api/config/export")

        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertLessEqual({"version", "exported_at", "settings", "checks"}, set(exported))
        self.assertIsInstance(exported["checks"], list)
        self.assertTrue(exported["version"])
        datetime.fromisoformat(str(exported["exported_at"]).replace("Z", "+00:00"))

        exported_text = json.dumps(exported, ensure_ascii=False)
        self.assertIn("Secret-bearing API", exported_text)
        self.assertIn("https://api.internal.example", exported_text)
        self.assertNotIn("SEC-dingtalk-signing-secret", exported_text)
        self.assertNotIn("SEC-tag-policy-secret", exported_text)
        self.assertNotIn("https://tag-policy-webhook.example/hook", exported_text)
        self.assertNotIn("session-secret-123", exported_text)
        self.assertNotIn("token-secret-456", exported_text)
        self.assertNotIn("read-token-secret-999", exported_text)
        self.assertNotIn("process-secret-789", exported_text)
        self.assertNotIn("ou_alice_private", exported_text)
        self.assertNotIn("alice.private", exported_text)
        self.assertNotIn("13800000001", exported_text)
        self.assertNotIn("runner-secret-export", exported_text)
        self.assertEqual(exported["settings"]["read_only_token"], "")
        self.assertEqual(exported["settings"]["members"], [])
        self.assertIn("runners", exported)
        exported_runner = next(runner for runner in exported["runners"] if runner["runner_id"] == "edge-secret")
        self.assertEqual(exported_runner["role"], "child")
        self.assertTrue(exported_runner["allow_insecure_http"])
        self.assertNotIn("token", exported_runner)
        self.assertNotIn("token_value", exported_runner)

        exported_check = next(check for check in exported["checks"] if check["name"] == "Secret-bearing API")
        self.assertEqual(
            json.loads(exported_check["alert_policy_json"]),
            {key: value for key, value in task_policy.items() if key != "member_ids"},
        )

        tag_policies = exported["settings"]["alert_tag_policies"]
        self.assertEqual(tag_policies, [tag_policy])
        tag_policy_text = json.dumps(tag_policies, ensure_ascii=False)
        self.assertNotIn("webhook", tag_policy_text.lower())
        self.assertNotIn("secret", tag_policy_text.lower())

        variables = {item["id"]: item for item in exported["settings"]["environment_variables"]}
        self.assertFalse(variables["host"]["secret"])
        self.assertEqual(variables["host"]["value"], "https://api.internal.example")
        self.assertTrue(variables["explicit"]["secret"])
        self.assertNotEqual(variables["explicit"].get("value"), "session-secret-123")
        self.assertTrue(variables["named"]["secret"])
        self.assertNotEqual(variables["named"].get("value"), "token-secret-456")

        full_response = self.client.get("/api/config/export?redact=false")
        self.assertEqual(full_response.status_code, 200)
        full_export = full_response.json()
        self.assertEqual(full_export["settings"]["members"][0]["id"], "alice")
        full_check = next(check for check in full_export["checks"] if check["name"] == "Secret-bearing API")
        self.assertEqual(json.loads(full_check["alert_policy_json"])["member_ids"], ["alice"])
        full_runner = next(runner for runner in full_export["runners"] if runner["runner_id"] == "edge-secret")
        self.assertTrue(full_runner["token_set"])
        self.assertNotIn("token", full_runner)
        self.assertNotIn("runner-secret-export", json.dumps(full_export, ensure_ascii=False))

    def test_import_preview_reports_counts_and_does_not_persist_changes(self) -> None:
        before_check_count = len(storage.list_checks())
        before_settings = storage.get_settings()
        payload = {
            "bundle": transfer_config(
                settings={"default_interval_seconds": 77, "browser_viewport": "1280x720"},
                checks=[api_check_payload("Preview API")],
            ),
            "replace_existing": False,
        }

        response = self.client.post("/api/config/import-preview", json=payload)

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertLessEqual({"counts", "issues", "warnings", "summary"}, set(preview))
        self.assertIsInstance(preview["issues"], list)
        self.assertIsInstance(preview["warnings"], list)
        self.assertTrue(preview["summary"])
        self.assertEqual(preview["counts"].get("checks"), 1)
        self.assertEqual(preview["counts"].get("settings"), 2)

        self.assertEqual(len(storage.list_checks()), before_check_count)
        self.assertIsNone(find_check_by_name("Preview API"))
        self.assertEqual(storage.get_settings()["default_interval_seconds"], before_settings["default_interval_seconds"])
        self.assertEqual(storage.get_settings()["browser_viewport"], before_settings["browser_viewport"])

    def test_import_apply_creates_checks_and_updates_settings(self) -> None:
        task_policy = {
            "alert_cooldown_minutes": 25,
            "recovery_notification": False,
            "notification_channel_ids": ["ops"],
        }
        tag_policy = {
            "id": "tag-ops",
            "name": "Ops Tag",
            "tag": "ops",
            "enabled": True,
            "alert_cooldown_minutes": 20,
            "recovery_notification": True,
            "notification_channel_ids": ["ops"],
        }
        payload = {
            "bundle": transfer_config(
                settings={
                    "default_interval_seconds": 42,
                    "browser_viewport": "1365x768",
                    "alert_tag_policies": [
                        {
                            **tag_policy,
                            "webhook_url": "https://imported-tag-policy.example/hook",
                            "dingtalk_secret": "SEC-imported-tag-secret",
                        }
                    ],
                },
                checks=[
                    api_check_payload(
                        "Imported API",
                        entry_url="https://import.example.com/health",
                        alert_policy_json=json.dumps(task_policy, ensure_ascii=False),
                    ),
                    ui_check_payload("Imported UI", entry_url="https://import.example.com/dashboard"),
                ],
            ),
            "replace_existing": True,
        }

        response = self.client.post("/api/config/import", json=payload)

        self.assertEqual(response.status_code, 200)
        imported_api = find_check_by_name("Imported API")
        imported_ui = find_check_by_name("Imported UI")
        self.assertIsNotNone(imported_api)
        self.assertIsNotNone(imported_ui)
        self.assertEqual(imported_api["type"], "api")
        self.assertEqual(imported_api["entry_url"], "https://import.example.com/health")
        self.assertEqual(json.loads(imported_api["alert_policy_json"]), task_policy)
        self.assertEqual(imported_ui["type"], "ui")
        self.assertEqual(imported_ui["viewport_mode"], "h5")

        settings = storage.get_settings()
        self.assertEqual(settings["default_interval_seconds"], 42)
        self.assertEqual(settings["browser_viewport"], "1365x768")
        self.assertEqual(settings["alert_tag_policies"], [tag_policy])
        tag_policy_text = json.dumps(settings["alert_tag_policies"], ensure_ascii=False)
        self.assertNotIn("webhook", tag_policy_text.lower())
        self.assertNotIn("secret", tag_policy_text.lower())

    def test_import_apply_creates_runners_and_preserves_check_runner_strategy(self) -> None:
        check_payload = api_check_payload("Imported Runner API", entry_url="https://import.example.com/runner")
        check_payload.update({"runner_selection_mode": "selected_parallel", "runner_ids": ["edge-import"]})
        payload = {
            "bundle": transfer_config(
                settings={},
                runners=[
                    {
                        "runner_id": "edge-import",
                        "name": "Imported Edge Runner",
                        "address": "http://10.0.0.12:8787",
                        "network_region": "edge",
                        "enabled": True,
                    }
                ],
                checks=[check_payload],
            ),
            "replace_existing": True,
        }

        preview = self.client.post("/api/config/import-preview", json=payload)
        response = self.client.post("/api/config/import", json=payload)

        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["counts"].get("runners"), 1)
        self.assertIn("未包含认证信息", "；".join(preview.json()["warnings"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["runners_imported"], 1)

        runner = storage.get_probe_runner("edge-import")
        imported_check = find_check_by_name("Imported Runner API")
        self.assertIsNotNone(runner)
        self.assertEqual(runner["address"], "http://10.0.0.12:8787")
        self.assertFalse(runner["enabled"])
        self.assertFalse(runner["token_set"])
        self.assertTrue(runner["allow_insecure_http"])
        self.assertNotIn("token", runner)
        self.assertIsNotNone(imported_check)
        self.assertEqual(imported_check["runner_selection_mode"], "selected_parallel")
        self.assertEqual(imported_check["runner_ids"], ["edge-import"])

    def test_current_config_requires_explicit_manual_http_confirmation(self) -> None:
        for confirmation in (None, False):
            with self.subTest(confirmation=confirmation):
                runner = {
                    "runner_id": "edge-current-http",
                    "name": "Current HTTP Runner",
                    "address": "http://10.0.0.13:8787",
                    "network_region": "edge",
                    "enabled": True,
                }
                if confirmation is not None:
                    runner["allow_insecure_http"] = confirmation
                bundle = transfer_config(settings={}, runners=[runner], checks=[])
                bundle["version"] = config_transfer.EXPORT_SCHEMA_VERSION

                preview = self.client.post(
                    "/api/config/import-preview",
                    json={"bundle": bundle, "replace_existing": False},
                )

                self.assertEqual(preview.status_code, 200)
                self.assertFalse(preview.json()["valid"])
                self.assertTrue(any("allow_insecure_http=true" in issue for issue in preview.json()["issues"]))

    def test_import_apply_without_replace_preserves_existing_duplicate_name_policy(self) -> None:
        existing = storage.create_check(api_check_payload("Duplicate API", entry_url="https://old.example.com/health"))
        payload = {
            "bundle": transfer_config(
                settings={"default_api_timeout_ms": 12345},
                checks=[api_check_payload("Duplicate API", entry_url="https://new.example.com/health")],
            ),
            "replace_existing": False,
        }

        response = self.client.post("/api/config/import", json=payload)

        self.assertEqual(response.status_code, 200)
        result_text = json.dumps(response.json(), ensure_ascii=False).lower()
        original = storage.get_check(int(existing["id"]))
        duplicates = [check for check in storage.list_checks() if check["name"].startswith("Duplicate API")]
        imported_duplicates = [check for check in duplicates if int(check["id"]) != int(existing["id"])]

        self.assertEqual(original["entry_url"], "https://old.example.com/health")
        if imported_duplicates:
            self.assertTrue(all(check["name"] != "Duplicate API" for check in imported_duplicates))
            self.assertIn("renamed", result_text)
        else:
            self.assertEqual(len(duplicates), 1)
            self.assertIn("skipped", result_text)


def transfer_config(
    settings: dict[str, Any],
    checks: list[dict[str, Any]],
    runners: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "exported_at": "2026-06-08T00:00:00+00:00",
        "settings": settings,
        "runners": runners or [],
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


def ui_check_payload(name: str, entry_url: str = "https://import.example.com/ui") -> dict[str, Any]:
    return {
        "name": name,
        "type": "ui",
        "enabled": True,
        "interval_seconds": 180,
        "timeout_ms": 15000,
        "entry_url": entry_url,
        "viewport_mode": "h5",
        "method": "",
        "headers_json": "{}",
        "body": "",
        "assertions_json": json.dumps(
            [{"id": "title-ok", "type": "title_contains", "enabled": True, "expected_text": "OK"}]
        ),
        "setup_script": "async def setup(ctx, page):\n    pass\n",
        "script": "",
        "tags": "imported,ui",
    }


def find_check_by_name(name: str) -> dict[str, Any] | None:
    return next((check for check in storage.list_checks() if check["name"] == name), None)


if __name__ == "__main__":
    unittest.main()
