from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.app.runner import BrowserRunTarget, CheckRunner


class RunnerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.node = {"runner_id": "edge-1", "role": "child", "connection_mode": "relay", "address": "http://127.0.0.1:18001", "enabled": True, "available": True, "status": "available"}
        self.enterContext(patch("backend.app.runner.storage.get_settings", return_value={}))
        self.enterContext(patch("backend.app.runner.storage.get_probe_runner", side_effect=lambda _: dict(self.node)))
        self.mark_available = self.enterContext(patch("backend.app.runner.storage.mark_probe_runner_available"))
        self.mark_unavailable = self.enterContext(patch("backend.app.runner.storage.mark_probe_runner_unavailable"))
        self.runner = CheckRunner()

    async def test_single_request_failure_does_not_replay_work_or_mark_healthy_node_offline(self) -> None:
        self.enterContext(patch("backend.app.runner.storage.can_schedule_runner", return_value=True))
        self.enterContext(patch("backend.app.runner.storage.create_run", return_value={"id": 42}))
        self.enterContext(patch("backend.app.runner.storage.start_run"))
        failed = {"status": "skipped", "failure_kind": "runner", "affects_health": False}
        self.runner._finish_runner_unavailable = Mock(return_value=failed)
        self.runner._notify_system_result = AsyncMock(return_value=failed)
        self.runner._notify_runner_unavailable_if_needed = AsyncMock()
        self.runner._call_remote_runner = AsyncMock(side_effect=RuntimeError("Server disconnected without sending a response"))
        with patch("backend.app.runner.fetch_runner_health", new=AsyncMock(return_value={"ok": True})) as health:
            result = await self.runner._dispatch_runner(
                {"id": 1, "project_id": 1, "type": "api", "name": "probe"},
                BrowserRunTarget(self.node, ""), "scheduled", "group-1",
            )
        self.assertEqual(result, failed)
        self.runner._call_remote_runner.assert_awaited_once()
        health.assert_awaited_once()
        self.mark_available.assert_not_called()
        self.mark_unavailable.assert_not_called()

    async def test_failed_health_recheck_marks_node_unavailable(self) -> None:
        with patch("backend.app.runner.fetch_runner_health", new=AsyncMock(side_effect=ConnectionError)):
            await self.runner._recheck_runner_after_failure(self.node)
        self.mark_unavailable.assert_called_once_with("edge-1")
        self.mark_available.assert_not_called()
        self.assertEqual(self.runner._runner_health_checks, {})

    async def test_health_recheck_has_total_deadline(self) -> None:
        async def blocked(_):
            await asyncio.Event().wait()
        with patch("backend.app.runner.fetch_runner_health", side_effect=blocked), patch("backend.app.runner.RUNNER_RECOVERY_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(self.runner._recheck_runner_after_failure(self.node), 1)
        self.mark_unavailable.assert_called_once()

    async def test_stale_failed_recheck_does_not_override_new_health_or_configuration(self) -> None:
        for change in ({"updated_at": "new-health-poll"}, {"enabled": False}, {"status": "updating"}, {"relay_token_version": 2}):
            with self.subTest(change=change):
                original = dict(self.node)
                async def failed(_):
                    self.node.update(change)
                    raise ConnectionError()
                with patch("backend.app.runner.fetch_runner_health", side_effect=failed):
                    await self.runner._recheck_runner_after_failure(original)
                self.mark_unavailable.assert_not_called()
                self.node = original

    async def test_concurrent_failures_share_one_recheck(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        async def health(_):
            entered.set()
            await release.wait()
            return {"ok": True}
        with patch("backend.app.runner.fetch_runner_health", side_effect=health) as fetch:
            first = asyncio.create_task(self.runner._recheck_runner_after_failure(self.node))
            await entered.wait()
            second = asyncio.create_task(self.runner._recheck_runner_after_failure(self.node))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(fetch.await_count, 1)
        self.assertEqual(self.runner._runner_health_checks, {})

    async def test_dispatch_waits_for_recheck_then_obeys_latest_unavailable_state(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        async def health(_):
            entered.set()
            await release.wait()
            self.node.update(status="unavailable", available=False)
            raise ConnectionError()
        self.enterContext(patch("backend.app.runner.storage.can_schedule_runner", side_effect=lambda r: r["available"]))
        self.enterContext(patch("backend.app.runner.storage.create_run", return_value={"id": 42}))
        self.runner._finish_runner_unavailable = Mock(return_value={"status": "skipped"})
        self.runner._notify_system_result = AsyncMock(return_value={"status": "skipped"})
        self.runner._notify_runner_unavailable_if_needed = AsyncMock()
        self.runner._call_remote_runner = AsyncMock()
        with patch("backend.app.runner.fetch_runner_health", side_effect=health):
            recovery = asyncio.create_task(self.runner._recheck_runner_after_failure(dict(self.node)))
            await entered.wait()
            dispatch = asyncio.create_task(self.runner._dispatch_runner(
                {"id": 1, "project_id": 1, "type": "api"}, BrowserRunTarget(dict(self.node), ""), "scheduled", "group-1",
            ))
            await asyncio.sleep(0)
            self.assertFalse(dispatch.done())
            self.runner._call_remote_runner.assert_not_awaited()
            release.set()
            await recovery
            self.assertEqual((await dispatch)["status"], "skipped")
        self.runner._call_remote_runner.assert_not_awaited()

    async def test_health_success_cannot_undo_concurrent_administrative_or_relay_changes(self) -> None:
        for change in ({"enabled": False}, {"status": "updating"}, {"status": "auth_failed"}, {"status": "connecting"}, {"address": "http://different:8788"}, {"relay_token_version": 2}, {"last_disconnect_reason": "client disconnected"}):
            with self.subTest(change=change):
                original = dict(self.node)
                async def health(_):
                    self.node.update(change)
                    return {"ok": True}
                with patch("backend.app.runner.fetch_runner_health", side_effect=health):
                    await self.runner._recheck_runner_after_failure(original)
                self.mark_available.assert_not_called()
                self.mark_unavailable.assert_not_called()
                self.node = original

    async def test_cancelling_waiter_does_not_cancel_shared_health_check(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        async def health(_):
            entered.set()
            await release.wait()
            return {"ok": True}
        with patch("backend.app.runner.fetch_runner_health", side_effect=health):
            waiter = asyncio.create_task(self.runner._recheck_runner_after_failure(self.node))
            await entered.wait()
            task = self.runner._runner_health_checks["edge-1"]
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertFalse(task.cancelled())
            release.set()
            await task
        self.mark_available.assert_not_called()
        self.mark_unavailable.assert_not_called()

    async def test_shutdown_cancels_recovery_tasks(self) -> None:
        entered = asyncio.Event()
        async def health(_):
            entered.set()
            await asyncio.Event().wait()
        self.runner.resources.shutdown = AsyncMock()
        with patch("backend.app.runner.fetch_runner_health", side_effect=health):
            waiter = asyncio.create_task(self.runner._recheck_runner_after_failure(self.node))
            await entered.wait()
            await self.runner.shutdown()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        self.assertEqual(self.runner._runner_health_checks, {})
