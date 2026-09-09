from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.app.runner import BrowserRunTarget, CheckRunner
from backend.app.script_modules import capture_module_bundle


def _bundle(project_id: int = 1) -> dict:
    return capture_module_bundle(
        project_id,
        None,
        [
            {
                "name": "shared",
                "source": '''async def verify(ctx):
    return True
''',
            }
        ],
    )


class LibraryValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_payload_carries_frozen_bundle_without_project_lookup_fields(self) -> None:
        bundle = _bundle()
        payload = CheckRunner._worker_check_payload(
            {
                "id": 7,
                "project_id": 1,
                "type": "api",
                "script": 'async def check(ctx):\n    await ctx.modules.use("shared").verify(ctx)\n',
                "_module_bundle": bundle,
                "_library_validation": True,
            }
        )

        self.assertEqual(payload["module_bundle"], bundle)
        self.assertNotIn("_module_bundle", payload)
        self.assertTrue(payload["_library_validation"])

    async def test_scheduled_missing_module_becomes_observable_failure_run(self) -> None:
        runner = object.__new__(CheckRunner)
        check = {
            "id": 88,
            "project_id": 1,
            "type": "api",
            "name": "定时模块预检",
            "script": 'async def check(ctx):\n    await ctx.modules.use("missing").verify(ctx)\n',
        }
        persisted = {"id": 880, "check_id": 88, "status": "failed", "affects_health": True}
        submit = AsyncMock(return_value=persisted)
        runner._submit = submit

        with patch("backend.app.runner.storage.get_check", return_value=check), patch(
            "backend.app.runner.storage.get_project", return_value={"id": 1, "status": "active"}
        ), patch("backend.app.runner.storage.get_published_library", return_value=None):
            result = await runner.run_check(88, trigger="scheduled")

        self.assertEqual(result, persisted)
        submitted = submit.await_args.args[0]
        self.assertIn("_module_preflight_error", submitted)
        self.assertIn("_module_preflight_diagnostics", submitted)
        self.assertEqual(submit.await_args.args[1], "scheduled")

    def test_aggregate_prioritizes_failed_node_over_success(self) -> None:
        result = CheckRunner._aggregate_group_run(
            [
                {"id": 1, "status": "ok", "failure_kind": "none"},
                {"id": 2, "status": "failed", "failure_kind": "target"},
            ]
        )

        self.assertEqual(result["id"], 2)
        self.assertEqual(result["status"], "failed")

    async def test_local_validation_preserves_original_id_and_never_records_health(self) -> None:
        runner = object.__new__(CheckRunner)
        bundle = _bundle()
        check = {
            "id": 41,
            "project_id": 1,
            "type": "api",
            "name": "候选验证",
            "script": 'async def check(ctx):\n    await ctx.modules.use("shared").verify(ctx)\n',
        }
        local = {"runner_id": "local", "enabled": True, "available": True}
        submit = AsyncMock(return_value={"id": 501, "check_id": 41, "status": "ok", "affects_health": False})
        runner._resolve_check_runners = Mock(return_value=[local])
        runner._resolve_browser_targets = Mock(return_value=[])
        runner._submit = submit

        with patch("backend.app.runner.storage.runner_metadata", return_value={}):
            result = await runner.run_library_validation(check, bundle)

        self.assertEqual(result["check_id"], 41)
        self.assertEqual(result["original_check_id"], 41)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["runs"]), 1)
        submitted = submit.await_args.args[0]
        self.assertEqual(submitted["original_check_id"], 41)
        self.assertEqual(submitted["_module_bundle"], bundle)
        self.assertTrue(submit.await_args.kwargs["record_status"] is False)
        self.assertTrue(submit.await_args.kwargs["notify"] is False)

    async def test_distributed_validation_returns_every_node_and_non_success_status(self) -> None:
        runner = object.__new__(CheckRunner)
        bundle = _bundle()
        check = {
            "id": 42,
            "project_id": 1,
            "type": "api",
            "name": "候选验证",
            "script": 'async def check(ctx):\n    await ctx.modules.use("shared").verify(ctx)\n',
        }
        remote_a = {"runner_id": "runner-a", "enabled": True, "available": True}
        remote_b = {"runner_id": "runner-b", "enabled": True, "available": True}
        runs = [
            {"id": 601, "status": "ok", "failure_kind": "none", "affects_health": False},
            {"id": 602, "status": "failed", "failure_kind": "target", "affects_health": False},
        ]
        runner._resolve_check_runners = Mock(return_value=[remote_a, remote_b])
        runner._resolve_browser_targets = Mock(
            return_value=[
                BrowserRunTarget(remote_a, ""),
                BrowserRunTarget(remote_b, ""),
            ]
        )
        distributed = AsyncMock(return_value={"id": 602, "status": "failed", "runs": runs})
        runner._run_distributed_check = distributed

        result = await runner.run_library_validation(check, bundle)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["runs"], runs)
        self.assertEqual([item["id"] for item in result["runs"]], [601, 602])
        self.assertEqual(distributed.await_args.args[0]["original_check_id"], 42)
        self.assertTrue(distributed.await_args.args[0]["_library_validation"])

    async def test_validation_group_keeps_matrix_without_status_transition_or_notification(self) -> None:
        runner = object.__new__(CheckRunner)
        check = {"id": 43, "original_check_id": 43, "_library_validation": True}
        runs = [
            {"id": 701, "status": "ok", "failure_kind": "none"},
            {"id": 702, "status": "failed", "failure_kind": "runner"},
        ]

        with patch("backend.app.runner.storage.update_check_status") as update_status, patch(
            "backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock
        ) as maybe_notify:
            aggregate = await runner._finish_distributed_group(check, runs, "library-validation")

        self.assertEqual(aggregate["status"], "failed")
        self.assertEqual(aggregate["runs"], runs)
        self.assertFalse(aggregate["affects_health"])
        self.assertEqual(aggregate["observation_kind"], "library_validation")
        update_status.assert_not_called()
        maybe_notify.assert_not_awaited()

    async def test_old_remote_node_fails_before_dispatch_without_health_or_alert_side_effects(self) -> None:
        runner = CheckRunner()
        bundle = _bundle()
        node = {
            "runner_id": "old-node",
            "name": "旧节点",
            "address": "https://old-node.example",
            "network_region": "edge",
            "enabled": True,
            "available": True,
            "status": "available",
            "role": "child",
            "metadata": {"capabilities": []},
        }
        check = {
            "id": 44,
            "project_id": 1,
            "type": "api",
            "name": "候选验证",
            "script": 'async def check(ctx):\n    await ctx.modules.use("shared").verify(ctx)\n',
            "_module_bundle": bundle,
            "_library_validation": True,
        }
        persisted = {"id": 801, "check_id": 44, "status": "skipped", "failure_kind": "runner", "affects_health": False}

        with patch("backend.app.runner.storage.get_probe_runner", return_value=node), patch(
            "backend.app.runner.storage.runner_metadata", return_value={}
        ), patch("backend.app.runner.storage.can_schedule_runner", return_value=True), patch(
            "backend.app.runner.storage.get_settings", return_value={}
        ), patch("backend.app.runner.storage.create_run", return_value={"id": 801, "check_id": 44, "status": "pending"}), patch(
            "backend.app.runner.storage.get_run", side_effect=[None, persisted]
        ), patch("backend.app.runner.storage.finish_run", return_value=persisted), patch(
            "backend.app.runner.storage.update_run_notification"
        ) as update_notification, patch(
            "backend.app.runner.storage.mark_probe_runner_unavailable"
        ) as mark_unavailable, patch("backend.app.runner.storage.now_iso", return_value="2026-01-01T00:00:00+08:00"), patch(
            "backend.app.runner.notifier.notify_system_error", new_callable=AsyncMock
        ) as notify_system_error, patch(
            "backend.app.runner.notifier.notify_runner_unavailable", new_callable=AsyncMock
        ) as notify_runner_unavailable, patch.object(
            CheckRunner, "_call_remote_runner", new_callable=AsyncMock
        ) as call_remote:
            result = await runner._dispatch_runner(
                check,
                BrowserRunTarget(node, ""),
                "library-validation",
                "rg-validation",
                capacity_managed=True,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["affects_health"])
        update_notification.assert_called_once_with(801, "not_required", channel=None, error=None, sent_at=None)
        mark_unavailable.assert_not_called()
        notify_system_error.assert_not_awaited()
        notify_runner_unavailable.assert_not_awaited()
        call_remote.assert_not_awaited()

    async def test_ui_inspection_uses_the_same_frozen_module_bundle_path(self) -> None:
        bundle = _bundle()
        settings = {"max_task_runtime_seconds": 5, "browser_headless": True}
        payload = {
            "project_id": 1,
            "entry_url": "https://example.test",
            "setup_script": 'async def setup(ctx, page):\n    await ctx.modules.use("shared").verify(ctx)\n',
            "timeout_ms": 1000,
            "_module_bundle": bundle,
        }

        async def inspect_func(_payload: dict, _settings: dict, *, ctx: object, setup_func: object) -> dict:
            assert setup_func is not None
            await setup_func(ctx, None)  # type: ignore[misc]
            return {"result": "ok"}

        with patch("backend.app.runner.storage.get_settings", return_value=settings), patch(
            "backend.app.runner.storage.settings_for_project", return_value=settings
        ):
            runner = CheckRunner()
            result = await runner._inspect_ui(payload, inspect_func, "页面扫描")

        self.assertEqual(result["result"], "ok")
        self.assertEqual(result["diagnostics"]["modules"], {
            "project_id": 1,
            "release_id": None,
            "release_hash": None,
            "content_hash": bundle["content_hash"],
            "module_hashes": {"shared": bundle["modules"][0]["hash"]},
        })
