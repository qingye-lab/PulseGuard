from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.app import main as main_module
from backend.app.main import _format_validation_errors, app
from backend.app.schemas import CheckCreate, normalize_settings_values


class ValidationMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[main_module._project_context] = lambda: 1
        app.dependency_overrides[main_module._active_project_context] = lambda: 1

    def tearDown(self) -> None:
        app.dependency_overrides.pop(main_module._project_context, None)
        app.dependency_overrides.pop(main_module._active_project_context, None)

    def test_format_validation_errors_uses_user_facing_field_labels(self) -> None:
        message = _format_validation_errors(
            [
                {"loc": ("body", "name"), "type": "string_too_short", "msg": "String should have at least 1 character"},
                {"loc": ("body", "interval_seconds"), "type": "greater_than_equal", "msg": "Input should be greater than or equal to 5"},
            ]
        )

        self.assertEqual(message, "任务名称不能为空；执行频率低于允许范围")

    def test_create_check_validation_response_does_not_expose_internal_location(self) -> None:
        client = TestClient(app)

        response = client.post(
            "/api/checks",
            json={
                "type": "api",
                "enabled": True,
                "interval_seconds": 300,
                "timeout_ms": 10000,
                "entry_url": "https://example.com",
                "method": "GET",
                "headers_json": "{}",
                "body": "",
                "script": "async def check(ctx):\n    pass",
                "tags": "",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "任务名称不能为空")

    def test_debug_check_validation_response_uses_debug_route(self) -> None:
        client = TestClient(app)

        response = client.post(
            "/api/checks/debug",
            json={
                "type": "api",
                "enabled": True,
                "interval_seconds": 300,
                "timeout_ms": 10000,
                "entry_url": "https://example.com",
                "method": "GET",
                "headers_json": "{}",
                "body": "",
                "script": "async def check(ctx):\n    pass",
                "tags": "",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "任务名称不能为空")

    def test_custom_validation_response_keeps_business_message(self) -> None:
        client = TestClient(app)

        response = client.post(
            "/api/checks",
            json={
                "name": "接口任务",
                "type": "api",
                "enabled": True,
                "interval_seconds": 300,
                "timeout_ms": 10000,
                "entry_url": "https://example.com",
                "method": "GET",
                "headers_json": "[]",
                "body": "",
                "script": "async def check(ctx):\n    pass",
                "tags": "",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Headers 必须是 JSON Object")

    def test_api_check_can_use_assertions_without_script(self) -> None:
        payload = CheckCreate(
            name="无脚本接口",
            type="api",
            enabled=True,
            interval_seconds=300,
            timeout_ms=10000,
            entry_url="https://example.com",
            method="GET",
            headers_json="{}",
            body="",
            assertions_json='[{"type":"status_code","expected_status":200}]',
            script="",
            tags="",
        )

        self.assertEqual(payload.script, "")
        self.assertIn("status_code", payload.assertions_json)

    def test_api_check_requires_assertions_or_advanced_script(self) -> None:
        with self.assertRaisesRegex(ValueError, "接口任务请至少添加一个校验项"):
            CheckCreate(
                name="空接口",
                type="api",
                enabled=True,
                interval_seconds=300,
                timeout_ms=10000,
                entry_url="https://example.com",
                method="GET",
                headers_json="{}",
                body="",
                assertions_json="[]",
                script="",
                tags="",
            )

    def test_ui_check_can_use_assertions_without_script(self) -> None:
        payload = CheckCreate(
            name="无脚本 UI",
            type="ui",
            enabled=True,
            interval_seconds=300,
            timeout_ms=15000,
            entry_url="https://example.com",
            method="",
            headers_json="{}",
            body="",
            assertions_json='[{"type":"title_contains","expected_text":"Example"}]',
            setup_script="async def setup(ctx, page):\n    pass",
            script="",
            tags="",
        )

        self.assertEqual(payload.script, "")
        self.assertIn("async def setup", payload.setup_script)
        self.assertIn("title_contains", payload.assertions_json)

    def test_ui_setup_script_requires_setup_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "前置脚本必须定义"):
            CheckCreate(
                name="前置脚本错误",
                type="ui",
                enabled=True,
                interval_seconds=300,
                timeout_ms=15000,
                entry_url="https://example.com",
                method="",
                headers_json="{}",
                body="",
                assertions_json='[{"type":"title_contains","expected_text":"Example"}]',
                setup_script="async def check(ctx):\n    pass",
                script="",
                tags="",
            )

    def test_ui_check_requires_assertions_or_advanced_script(self) -> None:
        with self.assertRaisesRegex(ValueError, "UI 任务请至少添加一个校验项"):
            CheckCreate(
                name="空 UI",
                type="ui",
                enabled=True,
                interval_seconds=300,
                timeout_ms=15000,
                entry_url="https://example.com",
                method="",
                headers_json="{}",
                body="",
                assertions_json="[]",
                script="",
                tags="",
            )

    def test_inspect_api_route_returns_readable_request_error(self) -> None:
        client = TestClient(app)

        with patch("backend.app.main.inspect_api_response", new=AsyncMock(side_effect=httpx.ConnectError(""))):
            response = client.post(
                "/api/checks/inspect-api",
                json={
                    "type": "api",
                    "entry_url": "https://example.com",
                    "method": "GET",
                    "headers_json": "{}",
                    "body": "",
                    "timeout_ms": 10000,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "接口请求失败：ConnectError")

    def test_inspect_ui_route_returns_page_candidates(self) -> None:
        client = TestClient(app)
        runner = type(
            "FakeRunner",
            (),
            {
                "inspect_ui": AsyncMock(
                    return_value={
                        "title": "Example Domain",
                        "url": "https://example.com/",
                        "viewport": {"width": 1440, "height": 900},
                        "candidates": [{"selector": "text=Example Domain", "text": "Example Domain", "tag": "h1", "role": "heading"}],
                        "screenshot": "data:image/jpeg;base64,abc",
                    }
                )
            },
        )()

        with patch.object(app.state, "runner", runner, create=True):
            response = client.post(
                "/api/checks/inspect-ui",
                json={
                    "type": "ui",
                    "entry_url": "https://example.com",
                    "timeout_ms": 15000,
                    "setup_script": "async def setup(ctx, page):\n    ctx.log('scan setup')",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["candidates"][0]["selector"], "text=Example Domain")
        self.assertIn("async def setup", runner.inspect_ui.call_args.args[0]["setup_script"])

    def test_inspect_ui_setup_script_requires_setup_entry(self) -> None:
        client = TestClient(app)

        response = client.post(
            "/api/checks/inspect-ui",
            json={
                "type": "ui",
                "entry_url": "https://example.com",
                "timeout_ms": 15000,
                "setup_script": "async def check(ctx):\n    pass",
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "前置脚本必须定义 async def setup(ctx, page)")

    def test_settings_accept_environment_variables(self) -> None:
        normalized = normalize_settings_values(
            {
                "environment_variables": [
                    {"id": "host", "name": "API_HOST", "value": "https://api.example.com", "secret": False},
                    {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True},
                ]
            }
        )

        self.assertEqual(normalized["environment_variables"][0]["name"], "API_HOST")
        self.assertEqual(normalized["environment_variables"][1]["value"], "token-secret-123")
        self.assertTrue(normalized["environment_variables"][1]["secret"])

    def test_settings_accept_browser_recycle_after_runs(self) -> None:
        normalized = normalize_settings_values({"browser_recycle_after_runs": "12"})

        self.assertEqual(normalized["browser_recycle_after_runs"], 12)

    def test_environment_variable_name_validation_uses_user_facing_rule_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "NAME.*SERVICE_TOKEN"):
            normalize_settings_values(
                {
                    "environment_variables": [
                        {"id": "bad", "name": "1BAD", "value": "value", "secret": False},
                    ]
                }
            )

    def test_dingtalk_secret_accepts_variable_placeholder(self) -> None:
        normalized = normalize_settings_values(
            {
                "notification_channels": [
                    {
                        "id": "ding",
                        "name": "Ding",
                        "type": "dingtalk",
                        "enabled": True,
                        "webhook_url": "${DING_WEBHOOK}",
                        "dingtalk_secret": "${DING_SECRET}",
                    }
                ]
            }
        )

        self.assertEqual(normalized["notification_channels"][0]["webhook_url"], "${DING_WEBHOOK}")
        self.assertEqual(normalized["notification_channels"][0]["dingtalk_secret"], "${DING_SECRET}")

    def test_settings_validation_errors_use_user_facing_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "默认执行频率必须是数字"):
            normalize_settings_values({"default_interval_seconds": True})

    def test_alert_detail_base_url_requires_complete_http_url(self) -> None:
        self.assertEqual(
            normalize_settings_values({"alert_detail_base_url": "http://10.168.78.49:8787/"})["alert_detail_base_url"],
            "http://10.168.78.49:8787",
        )
        with self.assertRaisesRegex(ValueError, "http://"):
            normalize_settings_values({"alert_detail_base_url": "10.168.78.49:8787"})

    def test_settings_accept_alert_tag_policies_without_secret_material(self) -> None:
        normalized = normalize_settings_values(
            {
                "alert_tag_policies": [
                    {
                        "id": "critical-policy",
                        "tag": "critical",
                        "alert_cooldown_minutes": 5,
                        "recovery_notification": False,
                        "notification_channel_ids": ["ops", "backup"],
                        "webhook_url": "https://example.invalid/should-not-be-saved",
                        "dingtalk_secret": "SECshouldnotbesaved",
                    }
                ]
            }
        )

        policy = normalized["alert_tag_policies"][0]
        self.assertEqual(policy["id"], "critical-policy")
        self.assertEqual(policy["tag"], "critical")
        self.assertEqual(policy["alert_cooldown_minutes"], 5)
        self.assertFalse(policy["recovery_notification"])
        self.assertEqual(policy["notification_channel_ids"], ["ops", "backup"])
        self.assertNotIn("webhook_url", policy)
        self.assertNotIn("dingtalk_secret", policy)

    def test_settings_accept_separate_execution_and_system_alert_channels(self) -> None:
        normalized = normalize_settings_values(
            {
                "system_alerts_enabled": True,
                "execution_notification_channel_ids": ["ops", "", "biz"],
                "system_notification_channel_ids": ["infra", "ops"],
            }
        )

        self.assertTrue(normalized["system_alerts_enabled"])
        self.assertEqual(normalized["execution_notification_channel_ids"], ["ops", "biz"])
        self.assertEqual(normalized["system_notification_channel_ids"], ["infra", "ops"])

    def test_settings_reject_duplicate_alert_tag_policy_ids(self) -> None:
        with self.assertRaises(ValueError):
            normalize_settings_values(
                {
                    "alert_tag_policies": [
                        {"id": "critical-policy", "tag": "critical"},
                        {"id": "critical-policy", "tag": "checkout"},
                    ]
                }
            )

    def test_settings_reject_alert_tag_policy_empty_tag(self) -> None:
        with self.assertRaises(ValueError):
            normalize_settings_values(
                {
                    "alert_tag_policies": [
                        {"id": "empty-tag-policy", "tag": " "},
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
