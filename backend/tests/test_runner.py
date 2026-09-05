from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.app.runner import BrowserRunTarget, CheckRunner, RunFailure, RunnerEnvironmentFailure, _TASK_DURATION_ATTR


def local_runner_stub(available: bool = True) -> dict[str, object]:
    return {
        "runner_id": "local",
        "name": "local",
        "address": "127.0.0.1",
        "network_region": "local",
        "browser_version": "",
        "status": "ok" if available else "offline",
        "enabled": available,
        "available": available,
        "role": "local",
    }


class DraftRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._runner_state_patch = patch.multiple(
            "backend.app.runner.storage",
            get_probe_runner=Mock(return_value=None),
            can_schedule_runner=Mock(return_value=True),
            get_project=Mock(return_value={"id": 1, "status": "active"}),
        )
        self._runner_state_patch.start()
        self.addCleanup(self._runner_state_patch.stop)

    def test_draft_run_does_not_update_task_status_or_send_notification(self) -> None:
        check = {
            "project_id": 1,
            "name": "草稿调试",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "script": "async def check(ctx):\n    print('draft ok')\n",
            "tags": "",
        }
        result_data = {
            "status": "ok",
            "finished_at": "2026-01-01T00:00:01+08:00",
            "duration_ms": 10,
            "error_message": None,
            "error_stack": None,
            "logs": "draft ok",
            "screenshot_path": None,
            "trace_path": None,
            "response_path": None,
            "request_snapshot": None,
            "response_snapshot": None,
            "failure_kind": "none",
        }

        persisted_run = {
            "id": 101,
            "project_id": 1,
            "check_id": 0,
            "check_name": "草稿调试",
            "check_type": "api",
            "status": "ok",
            "notification_status": "not_required",
            "observation_kind": "draft",
            "affects_health": False,
            "runner_id": "local",
        }

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value={"max_task_runtime_seconds": 60, "browser_type": "chromium", "browser_headless": True},
        ), patch(
            "backend.app.runner.storage.get_probe_runner",
            return_value={"runner_id": "local", "name": "本机节点", "network_region": "local", "address": "", "browser_version": ""},
        ), patch.object(
            CheckRunner,
            "_execute_core",
            new_callable=AsyncMock,
            return_value=result_data,
        ) as execute_core, patch(
            "backend.app.runner.storage.create_run", return_value={"id": 101}
        ) as create_run, patch(
            "backend.app.runner.storage.finish_run", return_value=persisted_run
        ) as finish_run, patch(
            "backend.app.runner.storage.get_run", return_value=persisted_run
        ) as get_run, patch(
            "backend.app.runner.storage.update_run_notification"
        ) as update_run_notification, patch(
            "backend.app.runner.storage.update_check_status"
        ) as update_check_status, patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify:
            runner = CheckRunner()
            result = asyncio.run(runner.run_draft(check))

        self.assertEqual(result["id"], 101)
        self.assertEqual(result["check_id"], 0)
        self.assertEqual(result["check_name"], "草稿调试")
        self.assertEqual(result["check_type"], "api")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notification_status"], "not_required")
        self.assertEqual(result["observation_kind"], "draft")
        self.assertFalse(result["affects_health"])
        self.assertEqual(result["runner_id"], "local")
        execute_core.assert_awaited_once()
        create_run.assert_called_once()
        finish_run.assert_called_once_with(101, unittest.mock.ANY)
        get_run.assert_called_once_with(101)
        update_run_notification.assert_called_once_with(101, "not_required", channel=None, error=None, sent_at=None)
        update_check_status.assert_not_called()
        maybe_notify.assert_not_called()

    def test_structured_api_check_runs_without_loading_user_script(self) -> None:
        check = {
            "project_id": 1,
            "id": 7,
            "name": "无脚本接口",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"status_code","expected_status":200}]',
            "script": "",
            "tags": "",
        }
        created_run = {"id": 124, "status": "running"}
        finished_run = {"id": 124, "check_id": 7, "status": "ok"}

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value={"max_task_runtime_seconds": 60, "browser_type": "chromium", "browser_headless": True},
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ) as create_run, patch(
            "backend.app.runner.storage.finish_run", return_value=finished_run
        ), patch(
            "backend.app.runner.storage.get_run", return_value=finished_run
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "ok", "previous_status": None}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify, patch(
            "backend.app.runner.run_structured_api_check", new_callable=AsyncMock
        ) as run_structured_api_check, patch.object(
            CheckRunner, "_load_check_function", side_effect=AssertionError("script should not load")
        ):
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(7, trigger="manual"))

        self.assertEqual(result, finished_run)
        manual_payload = create_run.call_args.args[0]
        self.assertEqual(manual_payload["_run"]["trigger"], "manual")
        self.assertEqual(manual_payload["_run"]["observation_kind"], "observation")
        self.assertTrue(manual_payload["_run"]["affects_health"])
        run_structured_api_check.assert_awaited_once()
        maybe_notify.assert_awaited_once()

    def test_duration_excludes_preparation_and_notification_latency(self) -> None:
        check = {
            "project_id": 1,
            "id": 27,
            "name": "Precise duration API",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": "[]",
            "script": "async def check(ctx): pass",
            "tags": "",
        }
        created_run = {"id": 270, "status": "running"}
        captured_duration: list[int] = []

        async def actual_task(_ctx: object) -> None:
            await asyncio.sleep(0.01)

        def load_check_function(*_args: object, **_kwargs: object) -> object:
            time.sleep(0.2)
            return actual_task

        def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            captured_duration.append(int(data["duration_ms"]))
            return {"id": run_id, "check_id": 27, **data}

        async def slow_notify(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(0.2)

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value={"max_task_runtime_seconds": 60, "browser_type": "chromium", "browser_headless": True},
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch(
            "backend.app.runner.storage.finish_run", side_effect=finish_run
        ), patch(
            "backend.app.runner.storage.get_run", return_value=None
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "ok", "previous_status": None}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock, side_effect=slow_notify
        ), patch.object(
            CheckRunner, "_load_check_function", side_effect=load_check_function
        ):
            result = asyncio.run(CheckRunner().run_check(27, trigger="manual"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(captured_duration), 1)
        self.assertLess(captured_duration[0], 150)

    def test_target_failure_records_runner_metadata_and_failure_kind(self) -> None:
        check = {
            "project_id": 1,
            "id": 17,
            "name": "Runner metadata API",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"status_code","expected_status":200}]',
            "script": "",
            "tags": "",
        }
        settings = {
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "local_runner_name": "office-runner",
            "local_runner_address": "10.0.0.8",
            "local_runner_region": "office-lan",
        }
        created_run = {"id": 170, "status": "running"}
        finished_run = {"id": 170, "check_id": 17, "status": "failed"}

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value=settings,
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch(
            "backend.app.runner.storage.finish_run", return_value=finished_run
        ) as finish_run, patch(
            "backend.app.runner.storage.get_run", return_value=finished_run
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "failed", "previous_status": None}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ), patch(
            "backend.app.runner.run_structured_api_check", new_callable=AsyncMock, side_effect=RunFailure("target failed")
        ):
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(17))

        self.assertEqual(result, finished_run)
        finish_payload = finish_run.call_args.args[1]
        self.assertEqual(finish_payload["status"], "failed")
        self.assertEqual(finish_payload["failure_kind"], "target")
        self.assertEqual(finish_payload["runner_name"], "office-runner")
        self.assertEqual(finish_payload["runner_address"], "10.0.0.8")
        self.assertEqual(finish_payload["runner_region"], "office-lan")
        self.assertEqual(finish_payload["runner_browser_version"], "")
        self.assertEqual(finish_payload["browser_type"], "")

    def test_target_failure_retry_can_recover_with_single_run_record(self) -> None:
        check = {
            "project_id": 1,
            "id": 19,
            "name": "Retry API",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"status_code","expected_status":200}]',
            "script": "",
            "tags": "",
        }
        settings = {
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "api_retry_attempts": 1,
            "local_runner_name": "office-runner",
            "local_runner_address": "10.0.0.8",
            "local_runner_region": "office-lan",
        }
        created_run = {"id": 190, "status": "running"}
        finished_runs: list[dict[str, object]] = []
        attempt_count = 0

        async def run_attempt(*_args: object, **_kwargs: object) -> int:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                exc = RunFailure("flaky")
                setattr(exc, _TASK_DURATION_ATTR, 9000)
                raise exc
            return 1200

        def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            finished = {"id": run_id, "check_id": 19, **data}
            finished_runs.append(finished)
            return finished

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value=settings,
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ) as create_run, patch(
            "backend.app.runner.storage.finish_run", side_effect=finish_run
        ), patch(
            "backend.app.runner.storage.get_run", return_value=None
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "healthy", "previous_status": "unknown"}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ), patch.object(
            CheckRunner, "_run_check_attempt", side_effect=run_attempt
        ):
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(19))

        self.assertEqual(result["status"], "ok")
        create_run.assert_called_once()
        self.assertEqual(attempt_count, 2)
        self.assertEqual(len(finished_runs), 1)
        self.assertEqual(finished_runs[0]["status"], "ok")
        self.assertEqual(finished_runs[0]["duration_ms"], 1200)
        self.assertIn("本次尝试失败，立即重试：flaky", str(finished_runs[0]["logs"]))

    def test_retry_duration_uses_final_failed_attempt_instead_of_cumulative_total(self) -> None:
        check = {
            "project_id": 1,
            "id": 29,
            "name": "Retry duration API",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"status_code","expected_status":200}]',
            "script": "",
            "tags": "",
        }
        settings = {
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "api_retry_attempts": 1,
        }
        created_run = {"id": 290, "status": "running"}
        finished_runs: list[dict[str, object]] = []
        attempt_count = 0

        async def run_attempt(*_args: object, **_kwargs: object) -> int:
            nonlocal attempt_count
            attempt_count += 1
            exc = RunFailure(f"flaky-{attempt_count}")
            if attempt_count == 1:
                setattr(exc, _TASK_DURATION_ATTR, 9000)
                raise exc
            setattr(exc, _TASK_DURATION_ATTR, 1200)
            raise exc

        def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            finished = {"id": run_id, "check_id": 29, **data}
            finished_runs.append(finished)
            return finished

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value=settings,
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch(
            "backend.app.runner.storage.finish_run", side_effect=finish_run
        ), patch(
            "backend.app.runner.storage.get_run", return_value=None
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "healthy", "previous_status": "unknown"}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ), patch.object(
            CheckRunner, "_run_check_attempt", side_effect=run_attempt
        ):
            result = asyncio.run(CheckRunner().run_check(29))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_message"], "flaky-2")
        self.assertEqual(attempt_count, 2)
        self.assertEqual(len(finished_runs), 1)
        self.assertEqual(finished_runs[0]["duration_ms"], 1200)

    def test_runner_environment_failure_persists_non_health_run(self) -> None:
        check = {
            "project_id": 1,
            "id": 18,
            "name": "Runner browser failure",
            "type": "ui",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com",
            "method": "",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"title_contains","expected_text":"Example"}]',
            "script": "",
            "tags": "",
        }
        settings = {
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "local_runner_name": "office-runner",
            "local_runner_address": "10.0.0.8",
            "local_runner_region": "office-lan",
        }
        created_run = {"id": 180, "status": "running"}

        def finish_runner_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            return {
                "id": run_id,
                "check_id": 18,
                "observation_kind": "observation",
                "notification_status": None,
                **data,
            }

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value=settings,
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch("backend.app.runner.storage.finish_run", side_effect=finish_runner_run) as finish_run, patch(
            "backend.app.runner.storage.get_run", return_value=None
        ), patch(
            "backend.app.runner.storage.discard_incomplete_run", return_value=True
        ) as discard_incomplete_run, patch(
            "backend.app.runner.storage.update_run_notification"
        ), patch(
            "backend.app.runner.storage.update_check_status"
        ) as update_check_status, patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify, patch(
            "backend.app.runner.notifier.notify_system_error",
            new_callable=AsyncMock,
            return_value={"sent": False, "reason": "disabled"},
        ) as notify_system_error, patch(
            "backend.app.runner.run_structured_ui_check", new_callable=AsyncMock, side_effect=RunnerEnvironmentFailure("browser failed")
        ):
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(18))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        self.assertEqual(result["observation_kind"], "observation")
        finish_run.assert_called_once()
        discard_incomplete_run.assert_not_called()
        update_check_status.assert_not_called()
        maybe_notify.assert_not_called()
        notify_system_error.assert_awaited_once()

    def test_playwright_target_crash_persists_system_run_not_target_failure(self) -> None:
        check = {
            "project_id": 1,
            "id": 19,
            "name": "浏览器崩溃",
            "type": "ui",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com",
            "method": "",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"title_contains","expected_text":"Example"}]',
            "script": "",
            "tags": "",
        }
        settings = {
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "local_runner_name": "office-runner",
            "local_runner_address": "10.0.0.8",
            "local_runner_region": "office-lan",
        }
        created_run = {"id": 181, "status": "running"}

        def finish_runner_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            return {
                "id": run_id,
                "check_id": 19,
                "observation_kind": "observation",
                "notification_status": None,
                **data,
            }

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value=settings,
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch("backend.app.runner.storage.finish_run", side_effect=finish_runner_run) as finish_run, patch(
            "backend.app.runner.storage.get_run", return_value=None
        ), patch(
            "backend.app.runner.storage.discard_incomplete_run", return_value=True
        ) as discard_incomplete_run, patch(
            "backend.app.runner.storage.update_run_notification"
        ), patch(
            "backend.app.runner.storage.update_check_status"
        ) as update_check_status, patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify, patch(
            "backend.app.runner.notifier.notify_system_error",
            new_callable=AsyncMock,
            return_value={"sent": False, "reason": "disabled"},
        ) as notify_system_error, patch(
            "backend.app.runner.run_structured_ui_check", new_callable=AsyncMock, side_effect=RuntimeError("Page.title: Target crashed")
        ):
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(19))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        self.assertEqual(result["observation_kind"], "observation")
        finish_run.assert_called_once()
        discard_incomplete_run.assert_not_called()
        update_check_status.assert_not_called()
        maybe_notify.assert_not_called()
        notify_system_error.assert_awaited_once()

    def test_system_alert_delivery_outcome_is_persisted_on_runner_run(self) -> None:
        check = {"id": 20, "project_id": 1, "name": "Runner system failure", "type": "api"}
        result = {
            "id": 182,
            "check_id": 20,
            "status": "failed",
            "failure_kind": "runner",
            "affects_health": False,
            "error_message": "runner unavailable",
            "finished_at": "2026-01-01T00:00:01+08:00",
        }
        refreshed = {
            **result,
            "notification_status": "sent",
            "notification_channel": "Ops webhook",
            "notification_sent_at": "2026-01-01T00:00:02+08:00",
        }

        with patch(
            "backend.app.runner.notifier.notify_system_error",
            new_callable=AsyncMock,
            return_value={"sent": True, "status": "sent", "channel": "Ops webhook"},
        ), patch(
            "backend.app.runner.storage.now_iso",
            return_value="2026-01-01T00:00:02+08:00",
        ), patch(
            "backend.app.runner.storage.update_run_notification"
        ) as update_run_notification, patch(
            "backend.app.runner.storage.get_run",
            return_value=refreshed,
        ):
            persisted = asyncio.run(
                CheckRunner()._notify_system_result(
                    check,
                    "scheduled",
                    182,
                    result,
                    {"system_alerts_enabled": True},
                    source="runner",
                )
            )

        update_run_notification.assert_called_once_with(
            182,
            "sent",
            channel="Ops webhook",
            error=None,
            sent_at="2026-01-01T00:00:02+08:00",
        )
        self.assertEqual(persisted["notification_status"], "sent")
        self.assertEqual(persisted["notification_channel"], "Ops webhook")

    def test_runner_fallback_does_not_overwrite_completed_target_run(self) -> None:
        completed = {
            "id": 183,
            "check_id": 21,
            "status": "failed",
            "failure_kind": "target",
            "affects_health": True,
            "error_message": "target assertion failed",
        }

        with patch(
            "backend.app.runner.storage.get_run",
            return_value=completed,
        ), patch(
            "backend.app.runner.storage.finish_run"
        ) as finish_run, patch(
            "backend.app.runner.storage.update_run_notification"
        ) as update_run_notification:
            result = CheckRunner._finish_runner_result(
                {"id": 21, "name": "Target check", "type": "api"},
                "scheduled",
                183,
                {
                    "status": "failed",
                    "error_message": "late runner failure",
                },
            )

        self.assertEqual(result, completed)
        finish_run.assert_not_called()
        update_run_notification.assert_not_called()

    def test_structured_ui_check_runs_without_loading_user_script(self) -> None:
        check = {
            "project_id": 1,
            "id": 8,
            "name": "无脚本 UI",
            "type": "ui",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 15000,
            "entry_url": "https://example.com",
            "method": "",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"title_contains","expected_text":"Example"}]',
            "script": "",
            "tags": "",
        }
        created_run = {"id": 125, "status": "running"}
        finished_run = {"id": 125, "check_id": 8, "status": "ok"}

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value={"max_task_runtime_seconds": 60, "browser_type": "chromium", "browser_headless": True},
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch("backend.app.runner.storage.finish_run", return_value=finished_run), patch(
            "backend.app.runner.storage.get_run", return_value=finished_run
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "ok", "previous_status": None}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify, patch(
            "backend.app.runner.run_structured_ui_check", new_callable=AsyncMock
        ) as run_structured_ui_check, patch.object(
            CheckRunner, "_load_check_function", side_effect=AssertionError("script should not load")
        ):
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(8))

        self.assertEqual(result, finished_run)
        run_structured_ui_check.assert_awaited_once()
        ctx = run_structured_ui_check.call_args.args[0]
        diagnostics = ctx.request_snapshot["diagnostics"]
        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["stage"], "prepare")  # execution is mocked in this test
        self.assertEqual(diagnostics["variables"], {})
        self.assertEqual(
            {key: value for key, value in ctx.request_snapshot.items() if key != "diagnostics"},
            {
                "type": "ui",
                "url": "https://example.com",
                "timeout_ms": 15000,
                "viewport_mode": "web",
            },
        )
        self.assertEqual(diagnostics["request"], {key: value for key, value in ctx.request_snapshot.items() if key != "diagnostics"})
        maybe_notify.assert_awaited_once()

    def test_structured_ui_check_loads_setup_script_without_running_advanced_script(self) -> None:
        check = {
            "project_id": 1,
            "id": 9,
            "name": "带前置脚本 UI",
            "type": "ui",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 15000,
            "entry_url": "https://example.com/dashboard",
            "method": "",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"title_contains","expected_text":"Dashboard"}]',
            "setup_script": "async def setup(ctx, page):\n    ctx.log('setup ok')\n",
            "script": "async def check(ctx):\n    raise AssertionError('advanced script should not run')\n",
            "tags": "",
        }
        created_run = {"id": 126, "status": "running"}
        finished_run = {"id": 126, "check_id": 9, "status": "ok"}
        setup_func = AsyncMock()

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch(
            "backend.app.runner.storage.get_settings",
            return_value={"max_task_runtime_seconds": 60, "browser_type": "chromium", "browser_headless": True},
        ), patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.create_run", return_value=created_run
        ), patch("backend.app.runner.storage.finish_run", return_value=finished_run), patch(
            "backend.app.runner.storage.get_run", return_value=finished_run
        ), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "ok", "previous_status": None}
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ), patch(
            "backend.app.runner.run_structured_ui_check", new_callable=AsyncMock
        ) as run_structured_ui_check, patch.object(
            CheckRunner, "_load_check_function", return_value=setup_func
        ) as load_check_function:
            runner = CheckRunner()
            result = asyncio.run(runner.run_check(9))

        self.assertEqual(result, finished_run)
        load_check_function.assert_called_once()
        self.assertEqual(load_check_function.call_args.kwargs["function_name"], "setup")
        self.assertIs(run_structured_ui_check.call_args.kwargs["setup_func"], setup_func)

    def test_ui_inspect_loads_setup_script_for_scan(self) -> None:
        setup_func = AsyncMock()
        settings = {
            "max_concurrency": 2,
            "max_ui_concurrency": 1,
            "max_queue_size": 50,
            "max_task_runtime_seconds": 60,
            "default_ui_timeout_ms": 15000,
            "browser_type": "chromium",
            "browser_headless": True,
        }
        payload = {
            "type": "ui",
            "entry_url": "https://example.com/dashboard",
            "timeout_ms": 15000,
            "viewport_mode": "web",
            "setup_script": "async def setup(ctx, page):\n    ctx.log('scan setup')\n",
        }
        inspect_result = {"title": "Dashboard", "url": "https://example.com/dashboard", "candidates": [], "screenshot": ""}

        with patch.object(CheckRunner, "_max_concurrency", return_value=2), patch.object(
            CheckRunner, "_max_ui_concurrency", return_value=1
        ), patch.object(
            CheckRunner, "_max_queue_size", return_value=50
        ), patch(
            "backend.app.runner.storage.get_settings", return_value=settings
        ), patch(
            "backend.app.ui_assertions.inspect_ui_page", new_callable=AsyncMock, return_value=inspect_result
        ) as inspect_ui_page, patch.object(
            CheckRunner, "_load_check_function", return_value=setup_func
        ) as load_check_function:
            runner = CheckRunner()
            result = asyncio.run(runner.inspect_ui(payload))

        self.assertEqual(result, inspect_result)
        load_check_function.assert_called_once()
        self.assertEqual(load_check_function.call_args.kwargs["function_name"], "setup")
        inspect_ui_page.assert_awaited_once()
        self.assertIs(inspect_ui_page.call_args.kwargs["setup_func"], setup_func)
        self.assertEqual(inspect_ui_page.call_args.kwargs["ctx"].entry_url, payload["entry_url"])


class RunnerQueueTests(unittest.TestCase):
    def test_queue_capacity_skips_excess_submissions(self) -> None:
        async def scenario() -> list[dict[str, object]]:
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            async def execute(
                check: dict[str, object],
                trigger: str,
                run_id: int,
                record_status: bool = True,
                notify: bool = True,
                runner_metadata: dict[str, object] | None = None,
            ) -> dict[str, object]:
                if check["id"] == 1:
                    first_started.set()
                    await release_first.wait()
                else:
                    await asyncio.sleep(0.01)
                return {"id": run_id, "check_id": check["id"], "status": "ok", "trigger": trigger}

            with runner_patches(max_concurrency=1, max_queue_size=1):
                runner = CheckRunner()
                runner._execute = execute  # type: ignore[method-assign]
                first = asyncio.create_task(runner.run_check(1))
                await first_started.wait()
                second = asyncio.create_task(runner.run_check(2))
                for _ in range(20):
                    if runner.runtime_status()["queue"]["queued"] == 1:
                        break
                    await asyncio.sleep(0)
                third = await runner.run_check(3)
                release_first.set()
                results = [await first, await second, third]
                await runner.shutdown()
                return results

        results = asyncio.run(scenario())

        self.assertEqual([result["status"] for result in results].count("skipped"), 1)
        skipped = next(result for result in results if result["status"] == "skipped")
        self.assertIn("执行队列已满", str(skipped["error_message"]))

    def test_job_watchdog_timeout_releases_same_check_slot(self) -> None:
        async def scenario() -> tuple[
            dict[str, object],
            dict[str, object],
            dict[str, object],
            dict[str, object],
            int,
            int,
        ]:
            execute_calls = 0

            async def execute(
                check: dict[str, object],
                trigger: str,
                run_id: int,
                record_status: bool = True,
                notify: bool = True,
                runner_metadata: dict[str, object] | None = None,
            ) -> dict[str, object]:
                nonlocal execute_calls
                execute_calls += 1
                if execute_calls == 1:
                    await asyncio.Event().wait()
                return {"id": run_id, "check_id": check["id"], "status": "ok", "trigger": trigger}

            def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
                return {"id": run_id, "check_id": 1, **data}

            with runner_patches(max_concurrency=1, max_queue_size=1), patch(
                "backend.app.runner.storage.finish_run", side_effect=finish_run
            ), patch("backend.app.runner.storage.get_run", return_value=None), patch(
                "backend.app.runner.storage.update_run_notification"
            ) as update_run_notification, patch(
                "backend.app.runner.storage.update_check_status"
            ) as update_check_status, patch(
                "backend.app.runner.storage.discard_incomplete_run", return_value=True
            ) as discard_incomplete_run, patch(
                "backend.app.runner.notifier.notify_system_error",
                new_callable=AsyncMock,
                return_value={"sent": False, "reason": "disabled"},
            ) as notify_system_error:
                runner = CheckRunner()
                runner._execute = execute  # type: ignore[method-assign]
                with patch.object(runner, "_job_timeout_seconds", return_value=0.05):
                    first = await runner.run_check(1)
                    after_timeout = runner.runtime_status()
                    second = await runner.run_check(1)
                    after_second = runner.runtime_status()
                await runner.shutdown()
                return (
                    first,
                    second,
                    after_timeout,
                    after_second,
                    update_run_notification.call_count,
                    update_check_status.call_count,
                    discard_incomplete_run.call_count,
                    notify_system_error.await_count,
                )

        first, second, after_timeout, after_second, notification_count, status_update_count, discard_count, system_alert_count = asyncio.run(
            scenario()
        )

        self.assertEqual(first["status"], "timeout")
        self.assertEqual(first["failure_kind"], "runner")
        self.assertFalse(first["affects_health"])
        self.assertIn("单任务总时长限制", str(first["error_message"]))
        self.assertEqual(after_timeout["active_checks"], 0)
        self.assertEqual(after_timeout["workers"]["running"], 0)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(after_second["active_checks"], 0)
        self.assertEqual(notification_count, 2)
        self.assertEqual(status_update_count, 0)
        self.assertEqual(discard_count, 0)
        self.assertEqual(system_alert_count, 1)

    def test_job_watchdog_does_not_count_queue_wait_time(self) -> None:
        async def scenario() -> tuple[list[dict[str, object]], list[int], int]:
            started_ids: list[int] = []
            first_started = asyncio.Event()

            async def execute(
                check: dict[str, object],
                trigger: str,
                run_id: int,
                record_status: bool = True,
                notify: bool = True,
                runner_metadata: dict[str, object] | None = None,
            ) -> dict[str, object]:
                check_id = int(check["id"])
                started_ids.append(check_id)
                if check_id == 1:
                    first_started.set()
                    await asyncio.sleep(0.08)
                return {"id": run_id, "check_id": check_id, "status": "ok", "trigger": trigger}

            with runner_patches(max_concurrency=1, max_queue_size=1), patch(
                "backend.app.runner.storage.get_run", return_value=None
            ), patch(
                "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
            ) as notify_system_error:
                runner = CheckRunner()
                runner._execute = execute  # type: ignore[method-assign]

                def timeout_for(check: dict[str, object], _settings: dict[str, object]) -> float:
                    return 1.0 if int(check["id"]) == 1 else 0.05

                with patch.object(runner, "_job_timeout_seconds", side_effect=timeout_for):
                    first = asyncio.create_task(runner.run_check(1))
                    await first_started.wait()
                    second = asyncio.create_task(runner.run_check(2))
                    results = [await first, await second]
                await runner.shutdown()
                return results, started_ids, notify_system_error.await_count

        results, started_ids, system_alert_count = asyncio.run(scenario())

        self.assertEqual([result["status"] for result in results], ["ok", "ok"])
        self.assertEqual(started_ids, [1, 2])
        self.assertEqual(system_alert_count, 0)

    def test_job_watchdog_does_not_overwrite_completed_run(self) -> None:
        async def scenario() -> tuple[dict[str, object], int, dict[str, object]]:
            completed_run: dict[str, object] = {}
            finish_run_mock: Mock | None = None

            async def execute(
                check: dict[str, object],
                trigger: str,
                run_id: int,
                record_status: bool = True,
                notify: bool = True,
                runner_metadata: dict[str, object] | None = None,
            ) -> dict[str, object]:
                if finish_run_mock is None:
                    raise AssertionError("finish_run mock is not ready")
                finish_run_mock(
                    run_id,
                    {
                        "status": "failed",
                        "failure_kind": "target",
                        "affects_health": True,
                        "error_message": "target failed before notifier",
                    },
                )
                await asyncio.Event().wait()
                return {"id": run_id, "check_id": check["id"], "status": "ok", "trigger": trigger}

            def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
                completed_run.clear()
                completed_run.update({"id": run_id, "check_id": 1, **data})
                return dict(completed_run)

            def get_run(_run_id: int) -> dict[str, object] | None:
                return dict(completed_run) if completed_run else None

            with runner_patches(max_concurrency=1, max_queue_size=1), patch(
                "backend.app.runner.storage.finish_run", side_effect=finish_run
            ) as patched_finish_run, patch(
                "backend.app.runner.storage.get_run", side_effect=get_run
            ), patch(
                "backend.app.runner.storage.update_run_notification"
            ), patch(
                "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
            ) as notify_system_error:
                finish_run_mock = patched_finish_run
                runner = CheckRunner()
                runner._execute = execute  # type: ignore[method-assign]
                with patch.object(runner, "_job_timeout_seconds", return_value=0.05):
                    result = await runner.run_check(1)
                    after_timeout = runner.runtime_status()
                await runner.shutdown()
                return result, patched_finish_run.call_count, after_timeout, notify_system_error.await_count

        result, finish_run_count, after_timeout, system_alert_count = asyncio.run(scenario())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "target")
        self.assertEqual(result["error_message"], "target failed before notifier")
        self.assertEqual(finish_run_count, 1)
        self.assertEqual(after_timeout["active_checks"], 0)
        self.assertEqual(system_alert_count, 1)

    def test_job_watchdog_returns_when_timeout_record_cannot_be_written(self) -> None:
        async def scenario() -> tuple[dict[str, object], dict[str, object]]:
            async def execute(
                check: dict[str, object],
                trigger: str,
                run_id: int,
                record_status: bool = True,
                notify: bool = True,
                runner_metadata: dict[str, object] | None = None,
            ) -> dict[str, object]:
                await asyncio.Event().wait()
                return {"id": run_id, "check_id": check["id"], "status": "ok", "trigger": trigger}

            with runner_patches(max_concurrency=1, max_queue_size=1), patch(
                "backend.app.runner.storage.finish_run", side_effect=OSError("disk I/O error")
            ), patch("backend.app.runner.storage.get_run", return_value=None), patch(
                "backend.app.runner.storage.update_run_notification"
            ), patch(
                "backend.app.runner.storage.discard_incomplete_run", return_value=True
            ) as discard_incomplete_run, patch(
                "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
            ) as notify_system_error:
                runner = CheckRunner()
                runner._execute = execute  # type: ignore[method-assign]
                with patch.object(runner, "_job_timeout_seconds", return_value=0.05):
                    result = await asyncio.wait_for(runner.run_check(1), timeout=1.0)
                    after_timeout = runner.runtime_status()
                await runner.shutdown()
                return result, after_timeout, discard_incomplete_run.call_count, notify_system_error.await_count

        result, after_timeout, discard_count, system_alert_count = asyncio.run(scenario())

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        self.assertIn("单任务总时长限制", str(result["error_message"]))
        self.assertEqual(after_timeout["active_checks"], 0)
        self.assertEqual(discard_count, 0)
        self.assertEqual(system_alert_count, 1)

    def test_ui_jobs_are_capped_independently_from_global_concurrency(self) -> None:
        active_ui = 0
        max_active_ui = 0

        async def scenario() -> list[dict[str, object]]:
            nonlocal active_ui, max_active_ui

            async def execute(
                check: dict[str, object],
                trigger: str,
                run_id: int,
                record_status: bool = True,
                notify: bool = True,
                runner_metadata: dict[str, object] | None = None,
            ) -> dict[str, object]:
                nonlocal active_ui, max_active_ui
                active_ui += 1
                max_active_ui = max(max_active_ui, active_ui)
                try:
                    await asyncio.sleep(0.03)
                    return {"id": run_id, "check_id": check["id"], "status": "ok", "trigger": trigger}
                finally:
                    active_ui -= 1

            with runner_patches(max_concurrency=2, max_ui_concurrency=1, check_type="ui"):
                runner = CheckRunner()
                runner._execute = execute  # type: ignore[method-assign]
                results = await asyncio.gather(runner.run_check(1), runner.run_check(2))
                await runner.shutdown()
                return results

        results = asyncio.run(scenario())

        self.assertEqual([result["status"] for result in results], ["ok", "ok"])
        self.assertEqual(max_active_ui, 1)


class DistributedRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._project_patch = patch(
            "backend.app.runner.storage.get_project", return_value={"id": 1, "status": "active"}
        )
        self._project_patch.start()
        self.addCleanup(self._project_patch.stop)

    def test_worker_settings_include_browser_prewarm_configuration(self) -> None:
        settings = {
            "max_task_runtime_seconds": 60,
            "browser_headless": True,
            "browser_type": "chromium",
            "enabled_browser_types": ["chromium", "firefox"],
            "prewarmed_browser_types": ["chromium"],
            "browser_pool_sizes": {"chromium": 3, "firefox": 1, "webkit": 1},
            "browser_recycle_after_runs": 12,
            "browser_proxy": "",
            "browser_viewport": "1440x900",
            "success_response_artifacts_enabled": True,
            "api_pool_size": 4,
            "api_retry_attempts": 1,
            "ui_retry_attempts": 1,
            "environment_variables": [],
        }

        payload = CheckRunner()._worker_settings(settings)

        self.assertEqual(payload["prewarmed_browser_types"], ["chromium"])
        self.assertEqual(payload["browser_pool_sizes"]["chromium"], 3)
        self.assertEqual(payload["browser_recycle_after_runs"], 12)
        self.assertEqual(payload["api_pool_size"], 4)

    def test_worker_run_reloads_resource_pool_from_main_settings(self) -> None:
        async def scenario() -> None:
            runner = CheckRunner()
            settings = {
                "max_task_runtime_seconds": 60,
                "api_pool_size": 4,
                "browser_type": "chromium",
                "browser_headless": True,
                "enabled_browser_types": ["chromium"],
                "prewarmed_browser_types": ["chromium"],
                "browser_pool_sizes": {"chromium": 3, "firefox": 1, "webkit": 1},
            }
            check = {"id": 41, "project_id": 1, "name": "worker api", "type": "api"}
            runner.resources.reload = AsyncMock()  # type: ignore[method-assign]
            runner._execute_core = AsyncMock(return_value={"status": "ok"})  # type: ignore[method-assign]

            result = await runner.execute_worker_run(check, "scheduled", 410, settings)

            self.assertEqual(result["status"], "ok")
            runner.resources.reload.assert_awaited_once_with(  # type: ignore[attr-defined]
                settings,
                api_pool_size=4,
                browser_pool_sizes_value={"chromium": 3, "firefox": 1, "webkit": 1},
            )

        asyncio.run(scenario())

    def test_selected_parallel_uses_only_schedulable_runners_when_any_exist(self) -> None:
        check = {
            "id": 40,
            "name": "Selected relay runners",
            "type": "api",
            "runner_selection_mode": "selected_parallel",
            "runner_ids": ["edge-pending", "edge-ready"],
        }
        runners = [
            {"runner_id": "edge-pending", "enabled": True, "available": False, "status": "connecting"},
            {"runner_id": "edge-ready", "enabled": True, "available": True, "status": "available"},
        ]

        with patch("backend.app.runner.storage.list_probe_runners_by_ids", return_value=runners), patch(
            "backend.app.runner.storage.can_schedule_runner",
            side_effect=lambda runner: bool(runner.get("available")),
        ):
            selected = CheckRunner()._resolve_check_runners(check)

        self.assertEqual([runner["runner_id"] for runner in selected], ["edge-ready"])

    def test_selected_parallel_returns_no_runners_when_selected_runners_are_not_schedulable(self) -> None:
        check = {
            "id": 40,
            "name": "Selected unavailable relay runners",
            "type": "api",
            "runner_selection_mode": "selected_parallel",
            "runner_ids": ["edge-expired"],
        }
        runners = [{"runner_id": "edge-expired", "enabled": True, "available": False, "status": "expired"}]

        with patch("backend.app.runner.storage.list_probe_runners_by_ids", return_value=runners), patch(
            "backend.app.runner.storage.can_schedule_runner",
            return_value=False,
        ):
            selected = CheckRunner()._resolve_check_runners(check)

        self.assertEqual(selected, [])

    def test_missing_runner_ids_does_not_force_unavailable_local_runner(self) -> None:
        check = {
            "id": 40,
            "name": "Legacy local runner",
            "type": "api",
            "runner_selection_mode": "selected_parallel",
            "runner_ids": [],
        }
        local = {"runner_id": "local", "enabled": False, "available": False, "role": "local", "status": "offline"}

        with patch("backend.app.runner.storage.get_probe_runner", return_value=local), patch(
            "backend.app.runner.storage.can_schedule_runner",
            return_value=False,
        ):
            selected = CheckRunner()._resolve_check_runners(check)

        self.assertEqual(selected, [])

    def test_round_robin_all_returns_no_runners_when_none_are_schedulable(self) -> None:
        check = {
            "id": 41,
            "name": "Round robin no runners",
            "type": "api",
            "runner_selection_mode": "round_robin_all",
        }

        with patch("backend.app.runner.storage.list_schedulable_probe_runners", return_value=[]), patch(
            "backend.app.runner.storage.next_runner_cursor"
        ) as next_runner_cursor:
            selected = CheckRunner()._resolve_check_runners(check)

        self.assertEqual(selected, [])
        next_runner_cursor.assert_not_called()

    def test_dispatch_rechecks_runner_schedulability_before_remote_call(self) -> None:
        async def scenario() -> tuple[dict[str, object], int, int]:
            runner = {
                "runner_id": "edge-1",
                "name": "Edge",
                "address": "http://edge:8788",
                "network_region": "edge",
                "enabled": True,
                "available": True,
                "status": "available",
                "role": "child",
            }
            stale = {**runner, "available": False, "status": "unavailable"}
            target = BrowserRunTarget(runner=runner, browser_type="")
            check = {"id": 42, "project_id": 1, "name": "Remote stale", "type": "api"}

            with patch("backend.app.runner.storage.get_probe_runner", return_value=stale), patch(
                "backend.app.runner.storage.can_schedule_runner", return_value=False
            ), patch("backend.app.runner.storage.get_settings", return_value={}), patch(
                "backend.app.runner.storage.create_run", return_value={"id": 420, "check_id": 42, "status": "pending"}
            ), patch("backend.app.runner.storage.discard_incomplete_run", return_value=True), patch(
                "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
            ) as notify_system_error, patch.object(
                CheckRunner,
                "_call_remote_runner",
                new_callable=AsyncMock,
                side_effect=AssertionError("unavailable runner should not receive work"),
            ) as call_remote_runner:
                result = await CheckRunner()._dispatch_runner(check, target, "scheduled", "rg-stale", capacity_managed=True)
                return result, notify_system_error.await_count, call_remote_runner.await_count

        result, system_alert_count, remote_call_count = asyncio.run(scenario())

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertEqual(system_alert_count, 1)
        self.assertEqual(remote_call_count, 0)

    def test_remote_runner_system_failure_marks_runner_unavailable(self) -> None:
        async def scenario() -> tuple[dict[str, object], int, object]:
            runner = {
                "runner_id": "edge-1",
                "name": "Edge",
                "address": "http://edge:8788",
                "network_region": "edge",
                "enabled": True,
                "available": True,
                "status": "available",
                "role": "child",
            }
            target = BrowserRunTarget(runner=runner, browser_type="")
            check = {"id": 43, "project_id": 1, "name": "Remote system failure", "type": "api"}
            worker_result = {
                "ok": True,
                "run": {
                    "status": "failed",
                    "finished_at": "2026-01-01T00:00:01+08:00",
                    "duration_ms": 0,
                    "error_message": "Page.title: Target crashed",
                    "logs": "Page.title: Target crashed",
                    "failure_kind": "runner",
                    "affects_health": False,
                },
            }

            with patch("backend.app.runner.storage.get_probe_runner", return_value=runner), patch(
                "backend.app.runner.storage.can_schedule_runner", return_value=True
            ), patch("backend.app.runner.storage.get_settings", return_value={}), patch(
                "backend.app.runner.storage.create_run", return_value={"id": 430, "check_id": 43, "status": "pending"}
            ), patch("backend.app.runner.storage.start_run"), patch(
                "backend.app.runner.storage.discard_incomplete_run", return_value=True
            ), patch("backend.app.runner.storage.mark_probe_runner_unavailable", return_value={**runner, "available": False, "status": "unavailable"}) as mark_unavailable, patch(
                "backend.app.runner.storage.should_notify_probe_runner_unavailable", return_value=True
            ), patch("backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock), patch(
                "backend.app.runner.notifier.notify_runner_unavailable", new_callable=AsyncMock
            ) as notify_runner_unavailable, patch.object(
                CheckRunner,
                "_call_remote_runner",
                new_callable=AsyncMock,
                return_value=worker_result,
            ):
                result = await CheckRunner()._dispatch_runner(check, target, "scheduled", "rg-system", capacity_managed=True)
                return result, notify_runner_unavailable.await_count, mark_unavailable.call_args

        result, unavailable_alert_count, mark_unavailable_call = asyncio.run(scenario())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        self.assertEqual(mark_unavailable_call.args, ("edge-1",))
        self.assertEqual(mark_unavailable_call.kwargs, {"status": "unhealthy"})
        self.assertEqual(unavailable_alert_count, 1)

    def test_ui_browser_targets_expand_per_runner_and_browser_type(self) -> None:
        check = {
            "id": 41,
            "name": "Browser matrix",
            "type": "ui",
            "browser_selection_mode": "selected_parallel",
            "browser_types": ["chromium", "firefox"],
        }
        runners = [
            {
                "runner_id": "local",
                "enabled": True,
                "available": True,
                "installed_browser_types": ["chromium", "firefox"],
            },
            {
                "runner_id": "edge-1",
                "enabled": True,
                "available": True,
                "installed_browser_types": ["chromium"],
            },
        ]

        with patch(
            "backend.app.runner.storage.get_settings",
            return_value={
                "enabled_browser_types": ["chromium", "firefox"],
                "prewarmed_browser_types": ["chromium"],
                "browser_pool_sizes": {"chromium": 5, "firefox": 2, "webkit": 1},
                "browser_type": "chromium",
            },
        ), patch(
            "backend.app.runner.browser_capabilities",
            return_value={"installed_browser_types": ["chromium", "firefox"], "available_browser_types": ["chromium", "firefox"], "browser_type_status": {}},
        ):
            targets = CheckRunner()._resolve_browser_targets(check, runners)

        self.assertEqual(
            [(target.runner["runner_id"], target.browser_type, target.skip_reason) for target in targets],
            [
                ("local", "chromium", ""),
                ("local", "firefox", ""),
                ("edge-1", "chromium", ""),
                ("edge-1", "firefox", "执行节点未安装 browser type：firefox"),
            ],
        )

    def test_remote_runner_timeout_covers_retry_budget(self) -> None:
        runner = CheckRunner()
        timeout = runner._remote_runner_timeout_seconds(
            {"type": "ui", "timeout_ms": 10000},
            {"max_task_runtime_seconds": 60, "ui_retry_attempts": 1},
        )

        self.assertEqual(timeout, 135.0)

    def test_remote_runner_timeout_error_is_actionable(self) -> None:
        class TimeoutClient:
            async def __aenter__(self) -> "TimeoutClient":
                return self

            async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
                return None

            async def post(self, *args: object, **kwargs: object) -> httpx.Response:
                raise httpx.ReadTimeout("")

        async def scenario() -> None:
            with patch(
                "backend.app.runner.storage.get_settings",
                return_value={"max_task_runtime_seconds": 60, "ui_retry_attempts": 1},
            ), patch("backend.app.runner.httpx.AsyncClient", return_value=TimeoutClient()):
                await CheckRunner()._call_remote_runner(
                    {"runner_id": "edge-1", "address": "http://10.0.0.8:8788", "_token": "pgrn_token"},
                    {"id": 31, "name": "Remote timeout", "type": "ui", "timeout_ms": 10000},
                    "manual",
                    310,
                )

        with self.assertRaisesRegex(RuntimeError, "执行节点调用超时，等待 135 秒后未返回"):
            asyncio.run(scenario())

    def test_disabled_local_runner_is_skipped_without_executing_probe(self) -> None:
        check = {
            "project_id": 1,
            "id": 31,
            "name": "Disabled local runner",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"status_code","expected_status":200}]',
            "script": "",
            "tags": "",
            "runner_selection_mode": "selected_parallel",
            "runner_ids": ["local"],
        }
        local_runner = {
            "runner_id": "local",
            "name": "local",
            "address": "127.0.0.1",
            "network_region": "local",
            "browser_version": "",
            "status": "offline",
            "enabled": False,
            "available": False,
            "role": "local",
        }

        def create_run(payload: dict[str, object], status: str, message: str = "") -> dict[str, object]:
            runner_payload = payload.get("_runner") if isinstance(payload.get("_runner"), dict) else {}
            return {"id": 310, "check_id": 31, "status": status, "error_message": message, "affects_health": False, **runner_payload}

        def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            return {"id": run_id, "check_id": 31, "affects_health": True, **data}

        with patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.list_probe_runners_by_ids", return_value=[local_runner]
        ), patch("backend.app.runner.storage.get_settings", return_value={}), patch(
            "backend.app.runner.storage.create_run", side_effect=create_run
        ) as create_run, patch("backend.app.runner.storage.finish_run", side_effect=finish_run) as finish_run_mock, patch(
            "backend.app.runner.storage.get_run", return_value=None
        ), patch("backend.app.runner.storage.discard_incomplete_run", return_value=True) as discard_incomplete_run, patch(
            "backend.app.runner.storage.update_run_notification"
        ) as update_run_notification, patch(
            "backend.app.runner.storage.update_check_status"
        ) as update_check_status, patch(
            "backend.app.runner.storage.get_probe_runner", return_value=local_runner
        ), patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify, patch(
            "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
        ) as notify_system_error, patch.object(
            CheckRunner, "_execute", new_callable=AsyncMock, side_effect=AssertionError("disabled local runner should not execute")
        ) as execute:
            result = asyncio.run(CheckRunner().run_check(31, trigger="manual"))

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        execute.assert_not_awaited()
        created_payload = create_run.call_args.args[0]
        self.assertFalse(created_payload["_run"]["affects_health"])
        finish_run_mock.assert_not_called()
        discard_incomplete_run.assert_not_called()
        update_run_notification.assert_called_once_with(310, "not_required", channel=None, error=None, sent_at=None)
        update_check_status.assert_not_called()
        maybe_notify.assert_not_called()
        notify_system_error.assert_not_awaited()

    def test_local_distributed_runner_system_failure_sends_system_alert(self) -> None:
        async def scenario() -> tuple[dict[str, object], int]:
            check = {"id": 32, "project_id": 1, "name": "Local distributed", "type": "api"}
            target = BrowserRunTarget(
                runner={
                    "runner_id": "local",
                    "name": "local",
                    "address": "127.0.0.1",
                    "network_region": "local",
                    "available": True,
                    "enabled": True,
                    "role": "local",
                },
                browser_type="",
            )
            settings = {
                "max_task_runtime_seconds": 60,
                "browser_type": "chromium",
                "browser_headless": True,
            }
            system_payload = {
                "status": "failed",
                "finished_at": "2026-01-01T00:00:01+08:00",
                "duration_ms": 0,
                "error_message": "Page.title: Target crashed",
                "error_stack": None,
                "logs": "Page.title: Target crashed",
                "screenshot_path": None,
                "trace_path": None,
                "response_path": None,
                "request_snapshot": None,
                "response_snapshot": None,
                "failure_kind": "runner",
                "affects_health": False,
            }

            with patch("backend.app.runner.storage.get_settings", return_value=settings), patch(
                "backend.app.runner.storage.create_run", return_value={"id": 320, "check_id": 32, "status": "pending"}
            ), patch("backend.app.runner.storage.start_run"), patch(
                "backend.app.runner.storage.discard_incomplete_run", return_value=True
            ), patch.object(
                CheckRunner,
                "_execute_core",
                new_callable=AsyncMock,
                return_value=system_payload,
            ), patch(
                "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
            ) as notify_system_error:
                result = await CheckRunner()._dispatch_runner(check, target, "manual", "rg-local", capacity_managed=True)
                return result, notify_system_error.await_count

        result, system_alert_count = asyncio.run(scenario())

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        self.assertEqual(result["observation_kind"], "runner")
        self.assertEqual(system_alert_count, 1)

    def test_runner_only_distributed_group_does_not_update_target_health(self) -> None:
        check = {"id": 32, "project_id": 1, "name": "Runner only", "type": "api"}
        runner_failure = {
            "id": 320,
            "check_id": 32,
            "status": "skipped",
            "failure_kind": "runner",
            "affects_health": True,
        }

        with patch("backend.app.runner.storage.update_check_status") as update_check_status, patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify:
            result = asyncio.run(CheckRunner()._finish_distributed_group(check, [runner_failure], "manual"))

        self.assertEqual(result, runner_failure)
        update_check_status.assert_not_called()
        maybe_notify.assert_not_called()

    def test_distributed_group_target_failure_wins_over_success_and_updates_once(self) -> None:
        check = {"id": 33, "project_id": 1, "name": "Aggregate target failure", "type": "api"}
        success = {
            "id": 331,
            "check_id": 33,
            "status": "ok",
            "failure_kind": "none",
            "affects_health": True,
        }
        target_failure = {
            "id": 332,
            "check_id": 33,
            "status": "failed",
            "failure_kind": "target",
            "affects_health": True,
        }

        with patch(
            "backend.app.runner.storage.update_check_status",
            return_value={"current_status": "failing", "previous_status": "healthy"},
        ) as update_check_status, patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify:
            result = asyncio.run(CheckRunner()._finish_distributed_group(check, [success, target_failure], "manual"))

        self.assertEqual(result, target_failure)
        update_check_status.assert_called_once_with(33, target_failure)
        maybe_notify.assert_awaited_once()


def runner_patches(max_concurrency: int = 2, max_ui_concurrency: int = 1, max_queue_size: int = 50, check_type: str = "api"):
    run_ids = {"value": 0}

    def check_for(check_id: int, **_: object) -> dict[str, object]:
        return {
            "id": check_id,
            "project_id": 1,
            "name": f"任务 {check_id}",
            "type": check_type,
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com/health",
            "method": "GET" if check_type == "api" else "",
            "headers_json": "{}",
            "body": "",
            "assertions_json": '[{"type":"status_code","expected_status":200}]' if check_type == "api" else '[{"type":"title_contains","expected_text":"Example"}]',
            "setup_script": "",
            "script": "",
            "tags": "",
        }

    def create_run(check: dict[str, object], status: str = "running", error_message: str | None = None) -> dict[str, object]:
        run_ids["value"] += 1
        return {
            "id": run_ids["value"],
            "check_id": int(check.get("id") or 0),
            "status": status,
            "error_message": error_message,
        }

    settings = {
        "max_concurrency": max_concurrency,
        "max_ui_concurrency": max_ui_concurrency,
        "max_queue_size": max_queue_size,
        "max_task_runtime_seconds": 60,
        "browser_type": "chromium",
        "browser_headless": True,
    }
    return patch.multiple(
        "backend.app.runner.storage",
        get_settings=Mock(return_value=settings),
        get_check=Mock(side_effect=check_for),
        get_project=Mock(return_value={"id": 1, "status": "active"}),
        get_probe_runner=Mock(return_value=local_runner_stub()),
        can_schedule_runner=Mock(return_value=True),
        create_run=Mock(side_effect=create_run),
        get_run=Mock(return_value=None),
        start_run=Mock(),
    )


if __name__ == "__main__":
    unittest.main()
