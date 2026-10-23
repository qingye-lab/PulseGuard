"""Regression tests for alert-delivery reliability gaps.

Each test here pins a behaviour where a real failure previously produced no
alert, or produced a health verdict that silently skipped recovery
confirmation.  They are grouped in one module because they all answer the same
product question: when something is actually broken, does an operator hear
about it?
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.app import monitoring, notifier, storage
from backend.app.monitoring import effective_health_status, next_health_state
from backend.app.runner import BrowserRunTarget, CheckRunner
from backend.app.scheduler import PulseScheduler


class StaleHealthSeparationTests(unittest.TestCase):
    """`stale` describes observation freshness, not target health."""

    def test_stale_observation_keeps_the_last_effective_health_state(self) -> None:
        previous = {"monitor_status": "stale", "health_status": "failing", "consecutive_failures": 2}

        self.assertEqual(effective_health_status(previous), "failing")

    def test_legacy_rows_without_health_status_still_degrade_to_unknown(self) -> None:
        self.assertEqual(effective_health_status({"monitor_status": "stale"}), "unknown")
        self.assertEqual(effective_health_status({"monitor_status": "healthy"}), "healthy")
        self.assertEqual(effective_health_status(None), "unknown")

    def test_health_status_is_never_itself_stale(self) -> None:
        previous = {"monitor_status": "stale", "health_status": "stale"}

        self.assertEqual(effective_health_status(previous), "unknown")

    def test_success_after_stale_still_requires_recovery_confirmation(self) -> None:
        settings = {"recovery_confirmation_count": 2, "api_failure_confirmation_count": 2}
        previous = {
            "monitor_status": "stale",
            "health_status": "failing",
            "consecutive_failures": 2,
            "consecutive_successes": 0,
        }

        first = next_health_state(previous, {"status": "ok", "check_type": "api"}, settings)
        second = next_health_state(
            {
                "monitor_status": first["current_status"],
                "health_status": first["current_status"],
                "consecutive_failures": first["consecutive_failures"],
                "consecutive_successes": first["consecutive_successes"],
            },
            {"status": "ok", "check_type": "api"},
            settings,
        )

        self.assertEqual(first["current_status"], "suspected_recovery")
        self.assertEqual(first["previous_status"], "failing")
        self.assertEqual(second["current_status"], "healthy")

    def test_failure_after_stale_keeps_counting_toward_the_same_incident(self) -> None:
        settings = {"api_failure_confirmation_count": 2}
        previous = {"monitor_status": "stale", "health_status": "suspected_failing", "consecutive_failures": 1}

        state = next_health_state(previous, {"status": "failed", "failure_kind": "target", "check_type": "api"}, settings)

        self.assertEqual(state["current_status"], "failing")
        self.assertEqual(state["consecutive_failures"], 2)


class StaleHealthPersistenceTests(unittest.TestCase):
    """The separation has to survive a round trip through SQLite."""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        self.enterContext(patch.object(storage, "DB_PATH", Path(temp_dir.name) / "pulseguard.db"))
        storage.init_db()
        self.check = storage.create_check(
            {
                "name": "recovery probe",
                "type": "api",
                "project_id": 1,
                "enabled": True,
                "interval_seconds": 5,
                "entry_url": "https://example.com",
            }
        )

    def _record(self, status: str, failure_kind: str = "none") -> dict[str, object]:
        run = storage.create_run({**self.check, "_run": monitoring.run_metadata("scheduled")}, "pending")
        storage.start_run(int(run["id"]))
        finished = storage.finish_run(
            int(run["id"]),
            {
                "status": status,
                "finished_at": storage.now_iso(),
                "duration_ms": 1,
                "failure_kind": failure_kind,
                "logs": "",
            },
        )
        return storage.update_check_status(int(self.check["id"]), finished)

    def _status_row(self) -> dict[str, object]:
        with storage._connect(read_only=True) as conn:
            row = conn.execute("SELECT * FROM check_status WHERE check_id = ?", (int(self.check["id"]),)).fetchone()
        return dict(row)

    def test_going_stale_preserves_failing_health_and_keeps_the_incident_open(self) -> None:
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "api_failure_confirmation_count": 1}):
            self._record("failed", "target")
        self.assertEqual(self._status_row()["monitor_status"], "failing")

        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "stale_after_intervals": 1}):
            with storage._connect() as conn:
                conn.execute(
                    "UPDATE check_status SET last_scheduled_at = '2000-01-01T00:00:00+00:00' WHERE check_id = ?",
                    (int(self.check["id"]),),
                )
            self.assertEqual(storage.refresh_stale_statuses(), 1)

        row = self._status_row()
        self.assertEqual(row["monitor_status"], "stale")
        self.assertEqual(row["health_status"], "failing")

        open_cycles = storage.list_anomaly_cycles(status="open")
        self.assertEqual(len(open_cycles), 1)

    def test_first_success_after_stale_does_not_resolve_the_incident(self) -> None:
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "api_failure_confirmation_count": 1}):
            self._record("failed", "target")
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "stale_after_intervals": 1}):
            with storage._connect() as conn:
                conn.execute(
                    "UPDATE check_status SET last_scheduled_at = '2000-01-01T00:00:00+00:00' WHERE check_id = ?",
                    (int(self.check["id"]),),
                )
            storage.refresh_stale_statuses()

        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "recovery_confirmation_count": 2}):
            transition = self._record("ok")

        self.assertEqual(transition["previous_status"], "failing")
        self.assertEqual(transition["current_status"], "suspected_recovery")
        self.assertEqual(len(storage.list_anomaly_cycles(status="open")), 1)

    def test_second_success_after_stale_resolves_and_reports_recovery(self) -> None:
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "api_failure_confirmation_count": 1}):
            self._record("failed", "target")
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "stale_after_intervals": 1}):
            with storage._connect() as conn:
                conn.execute(
                    "UPDATE check_status SET last_scheduled_at = '2000-01-01T00:00:00+00:00' WHERE check_id = ?",
                    (int(self.check["id"]),),
                )
            storage.refresh_stale_statuses()

        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "recovery_confirmation_count": 2}):
            self._record("ok")
            transition = self._record("ok")

        self.assertEqual(transition["previous_status"], "suspected_recovery")
        self.assertEqual(transition["current_status"], "healthy")
        self.assertEqual(storage.list_anomaly_cycles(status="open"), [])

    def test_legacy_database_backfills_health_status_from_monitor_status(self) -> None:
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "api_failure_confirmation_count": 1}):
            self._record("failed", "target")

        with storage._connect() as conn:
            conn.execute("ALTER TABLE check_status DROP COLUMN health_status")

        storage.init_db()

        row = self._status_row()
        self.assertEqual(row["monitor_status"], "failing")
        self.assertEqual(row["health_status"], "failing")

    def test_legacy_expired_rows_are_not_given_an_invented_health_verdict(self) -> None:
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "api_failure_confirmation_count": 1}):
            self._record("failed", "target")
        with storage._connect() as conn:
            conn.execute("ALTER TABLE check_status DROP COLUMN health_status")
            conn.execute(
                "UPDATE check_status SET monitor_status = 'stale', current_status = 'stale' WHERE check_id = ?",
                (int(self.check["id"]),),
            )

        storage.init_db()

        self.assertIsNone(self._status_row()["health_status"])

    def test_legacy_raw_failure_is_normalized_before_health_backfill(self) -> None:
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "api_failure_confirmation_count": 1}):
            self._record("failed", "target")
        with storage._connect() as conn:
            conn.execute("ALTER TABLE check_status DROP COLUMN health_status")
            conn.execute("ALTER TABLE check_status DROP COLUMN monitor_status")
            conn.execute("UPDATE check_status SET current_status = 'failed'")

        storage.init_db()
        storage.init_db()

        row = self._status_row()
        self.assertEqual(row["monitor_status"], "failing")
        self.assertEqual(row["health_status"], "failing")
        with patch.object(storage, "get_settings", return_value={**storage.get_settings(), "recovery_confirmation_count": 2}):
            transition = self._record("ok")
        self.assertEqual(transition["current_status"], "suspected_recovery")
        self.assertEqual(len(storage.list_anomaly_cycles(status="open")), 1)


class RunnerUnavailableNotificationTests(unittest.IsolatedAsyncioTestCase):
    """A node going offline must alert regardless of which path noticed it."""

    def setUp(self) -> None:
        self.runner = {
            "runner_id": "edge-1",
            "name": "edge-1",
            "role": "child",
            "enabled": True,
            "available": False,
            "connection_mode": "manual",
            "status": "offline",
            "unavailable_notified_at": None,
        }

    async def test_health_poll_discovering_an_offline_node_sends_the_alert(self) -> None:
        from backend.app import main

        with patch.object(main, "_fetch_runner_health", new=AsyncMock(side_effect=ConnectionError("unreachable"))), patch.object(
            main.storage, "mark_probe_runner_unavailable", return_value=dict(self.runner)
        ) as mark_unavailable, patch.object(
            main.notifier, "notify_runner_unavailable_if_needed", new=AsyncMock()
        ) as notify:
            notification_tasks = {}
            await main._poll_runner_health(
                dict(self.runner),
                asyncio.Semaphore(1),
                asyncio.Semaphore(1),
                {},
                notification_tasks,
            )
            await asyncio.gather(*notification_tasks.values())

        mark_unavailable.assert_called_once_with("edge-1")
        notify.assert_awaited_once()

    async def test_notification_is_skipped_when_the_node_is_manually_disabled(self) -> None:
        disabled = {**self.runner, "enabled": False}

        with patch.object(notifier.storage, "get_probe_runner", return_value=disabled), patch.object(
            notifier, "notify_runner_unavailable", new=AsyncMock()
        ) as notify:
            await notifier.notify_runner_unavailable_if_needed(disabled)

        notify.assert_not_awaited()

    async def test_notification_is_deduplicated_once_already_reported(self) -> None:
        reported = {**self.runner, "unavailable_notified_at": storage.now_iso()}

        with patch.object(notifier.storage, "get_probe_runner", return_value=reported), patch.object(
            notifier, "notify_runner_unavailable", new=AsyncMock()
        ) as notify:
            await notifier.notify_runner_unavailable_if_needed(reported)

        notify.assert_not_awaited()

    async def test_active_deployment_suppresses_the_node_offline_alert(self) -> None:
        with patch.object(notifier.storage, "get_probe_runner", return_value=self.runner), patch.object(
            notifier.storage,
            "get_deployment_state",
            return_value={"active": True, "finished_at": None},
        ), patch.object(notifier, "notify_runner_unavailable", new=AsyncMock()) as notify:
            result = await notifier.notify_runner_unavailable_if_needed(self.runner)

        self.assertEqual(result, {"status": "skipped", "reason": "deployment"})
        notify.assert_not_awaited()

    async def test_recently_finished_deployment_suppresses_the_node_offline_alert(self) -> None:
        finished_at = (datetime.now().astimezone() - timedelta(seconds=30)).isoformat(timespec="seconds")
        with patch.object(notifier.storage, "get_probe_runner", return_value=self.runner), patch.object(
            notifier.storage,
            "get_deployment_state",
            return_value={"active": False, "finished_at": finished_at},
        ), patch.object(notifier, "notify_runner_unavailable", new=AsyncMock()) as notify:
            result = await notifier.notify_runner_unavailable_if_needed(self.runner)

        self.assertEqual(result, {"status": "skipped", "reason": "deployment"})
        notify.assert_not_awaited()

    async def test_deployment_state_read_failure_does_not_hide_the_alert(self) -> None:
        with patch.object(notifier.storage, "get_probe_runner", return_value=self.runner), patch.object(
            notifier.storage, "get_deployment_state", side_effect=RuntimeError("database unavailable")
        ), patch.object(
            notifier, "notify_runner_unavailable", new=AsyncMock(return_value={"status": "sent"})
        ) as notify:
            result = await notifier.notify_runner_unavailable_if_needed(self.runner)

        self.assertEqual(result["status"], "sent")
        notify.assert_awaited_once()

    async def test_node_offline_alert_resumes_after_the_deployment_grace(self) -> None:
        grace = storage.DEPLOYMENT_UNAVAILABLE_ALERT_GRACE_SECONDS
        finished_at = (datetime.now().astimezone() - timedelta(seconds=grace + 60)).isoformat(timespec="seconds")
        with patch.object(notifier.storage, "get_probe_runner", return_value=self.runner), patch.object(
            notifier.storage,
            "get_deployment_state",
            return_value={"active": False, "finished_at": finished_at},
        ), patch.object(
            notifier, "notify_runner_unavailable", new=AsyncMock(return_value={"status": "sent"})
        ) as notify:
            result = await notifier.notify_runner_unavailable_if_needed(self.runner)

        self.assertEqual(result["status"], "sent")
        notify.assert_awaited_once()

    async def test_concurrent_notification_is_deduplicated_and_cancellation_allows_retry(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def deliver(*_args):
            entered.set()
            await release.wait()
            return {"status": "sent"}

        with patch.object(notifier.storage, "get_probe_runner", return_value=self.runner), patch.object(
            notifier.storage,
            "get_deployment_state",
            return_value={"active": False, "finished_at": None},
        ), patch.object(
            notifier, "notify_runner_unavailable", side_effect=deliver
        ) as send:
            first = asyncio.create_task(notifier.notify_runner_unavailable_if_needed(self.runner))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                concurrent = await notifier.notify_runner_unavailable_if_needed("edge-1", [{"id": 1}])
                self.assertEqual(concurrent["reason"], "in_progress")
                send.assert_awaited_once()
            finally:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
            release.set()
            result = await notifier.notify_runner_unavailable_if_needed(self.runner)
            self.assertEqual(result["status"], "sent")
            self.assertEqual(send.await_count, 2)


class RunnerUnavailableDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """A failed delivery must not be recorded as a delivered alert."""

    def setUp(self) -> None:
        self.runner = {"runner_id": "edge-1", "name": "edge-1", "role": "child", "enabled": True, "available": False}
        self.settings = {
            "system_alerts_enabled": True,
            "alert_delivery_attempts": 1,
            "system_notification_channel_ids": ["c1"],
            "system_notification_channels": [
                {"id": "c1", "enabled": True, "webhook_type": "generic", "webhook_url": "https://example.com/hook", "name": "ops"}
            ],
        }

    async def test_successful_delivery_marks_the_node_as_notified(self) -> None:
        with patch.object(notifier.storage, "get_settings", return_value=self.settings), patch.object(
            notifier, "_send_with_retry", new=AsyncMock()
        ), patch.object(notifier.storage, "mark_probe_runner_unavailable_notified") as mark_notified, patch.object(
            notifier.storage, "mark_probe_runner_unavailable_notify_failed"
        ) as mark_failed:
            result = await notifier.notify_runner_unavailable(self.runner, [])

        self.assertEqual(result["status"], "sent")
        mark_notified.assert_called_once_with("edge-1")
        mark_failed.assert_not_called()

    async def test_total_delivery_failure_is_not_recorded_as_notified(self) -> None:
        with patch.object(notifier.storage, "get_settings", return_value=self.settings), patch.object(
            notifier, "_send_with_retry", new=AsyncMock(side_effect=RuntimeError("webhook down"))
        ), patch.object(notifier.storage, "mark_probe_runner_unavailable_notified") as mark_notified, patch.object(
            notifier.storage, "mark_probe_runner_unavailable_notify_failed"
        ) as mark_failed:
            result = await notifier.notify_runner_unavailable(self.runner, [])

        self.assertEqual(result["status"], "failed")
        mark_notified.assert_not_called()
        mark_failed.assert_called_once_with("edge-1")

    async def test_disabled_system_alerts_still_suppress_repeat_attempts(self) -> None:
        with patch.object(notifier.storage, "get_settings", return_value={"system_alerts_enabled": False}), patch.object(
            notifier.storage, "mark_probe_runner_unavailable_notified"
        ) as mark_notified:
            result = await notifier.notify_runner_unavailable(self.runner, [])

        self.assertEqual(result["status"], "disabled")
        mark_notified.assert_called_once_with("edge-1")

    def test_failed_delivery_allows_a_controlled_retry_after_the_cooldown(self) -> None:
        recent = {
            "runner_id": "edge-1",
            "role": "child",
            "enabled": True,
            "available": False,
            "unavailable_notified_at": None,
            "unavailable_notify_failed_at": storage.now_iso(),
        }
        self.assertFalse(storage.should_notify_probe_runner_unavailable(recent))

        stale_failure = {**recent, "unavailable_notify_failed_at": "2000-01-01T00:00:00+00:00"}
        self.assertTrue(storage.should_notify_probe_runner_unavailable(stale_failure))


class RelayRestartAlertSuppressionTests(unittest.TestCase):
    """Restarting the relay transport is maintenance, not a child node failure."""

    @staticmethod
    def _relay_runner(**overrides: object) -> dict[str, object]:
        runner: dict[str, object] = {
            "runner_id": "edge-1",
            "role": "child",
            "enabled": True,
            "available": False,
            "status": "unavailable",
            "connection_mode": "relay",
            "was_available": True,
            "unavailable_since": None,
            "relay_last_seen_at": None,
            "unavailable_notified_at": None,
            "unavailable_notify_failed_at": None,
        }
        runner.update(overrides)
        return runner

    @staticmethod
    def _ago(seconds: float) -> str:
        return (datetime.now().astimezone() - timedelta(seconds=seconds)).isoformat(timespec="seconds")

    def test_a_relay_restart_inside_the_reconnect_window_is_not_reported(self) -> None:
        runner = self._relay_runner(unavailable_since=self._ago(30))

        self.assertFalse(storage.should_notify_probe_runner_unavailable(runner))

    def test_an_outage_that_outlasts_the_reconnect_window_is_reported(self) -> None:
        runner = self._relay_runner(unavailable_since=self._ago(storage.RELAY_UNAVAILABLE_ALERT_GRACE_SECONDS + 60))

        self.assertTrue(storage.should_notify_probe_runner_unavailable(runner))

    def test_a_host_that_never_reported_a_disconnect_is_measured_from_its_heartbeat(self) -> None:
        without_disconnect = self._relay_runner(relay_last_seen_at=self._ago(30))
        after_grace = self._relay_runner(
            relay_last_seen_at=self._ago(storage.RELAY_UNAVAILABLE_ALERT_GRACE_SECONDS + 60)
        )

        self.assertFalse(storage.should_notify_probe_runner_unavailable(without_disconnect))
        self.assertTrue(storage.should_notify_probe_runner_unavailable(after_grace))

    def test_a_relay_outage_with_no_timestamp_at_all_is_still_reported(self) -> None:
        runner = self._relay_runner()

        self.assertTrue(storage.should_notify_probe_runner_unavailable(runner))

    def test_a_relay_restart_never_produces_the_other_failure_states(self) -> None:
        for status in ("unhealthy", "auth_failed"):
            with self.subTest(status=status):
                runner = self._relay_runner(status=status, unavailable_since=self._ago(1))
                self.assertTrue(storage.should_notify_probe_runner_unavailable(runner))

    def test_a_relay_node_that_was_never_available_is_not_reported(self) -> None:
        runner = self._relay_runner(was_available=False, unavailable_since=self._ago(600))

        self.assertFalse(storage.should_notify_probe_runner_unavailable(runner))

    def test_a_manually_configured_node_keeps_alerting_on_the_first_failure(self) -> None:
        runner = self._relay_runner(connection_mode="manual", unavailable_since=self._ago(1))

        self.assertTrue(storage.should_notify_probe_runner_unavailable(runner))


class WorkerExecutionTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """Worker runs need the same total-runtime watchdog as local runs."""

    async def test_worker_timeout_during_context_close_returns_browser_capacity(self) -> None:
        from backend.tests.test_resource_pool import FakePlaywright, browser_pool, settings

        with patch.object(storage, "get_settings", return_value={}):
            runner = CheckRunner()
        fake = FakePlaywright()
        pool = browser_pool(fake, size=1)
        runner.resources.browsers["chromium"] = pool
        await pool.start(settings())
        closing, release = asyncio.Event(), asyncio.Event()

        async def attempt(ctx, *_args):
            lease = await pool.acquire_context(settings(), {})
            original_close = lease.context.close

            async def close():
                closing.set()
                await release.wait()
                await original_close()

            lease.context.close = close
            ctx._browser_context = lease.context
            ctx._browser_context_lease = lease
            return 1

        with patch.object(runner.resources, "reload", new=AsyncMock()), patch.object(
            runner, "_run_check_attempt", side_effect=attempt
        ), patch.object(runner, "_worker_job_timeout_seconds", return_value=0.05):
            task = asyncio.create_task(runner.execute_worker_run(
                {"id": 5, "project_id": 1, "type": "ui", "name": "probe", "timeout_ms": 1000},
                "scheduled", 14, {},
            ))
            try:
                await asyncio.wait_for(closing.wait(), 1)
                await asyncio.sleep(0.08)
                release.set()
                result = await asyncio.wait_for(task, 1)
                self.assertEqual(result["status"], "timeout")
                self.assertEqual(runner._worker_jobs, 0)
                self.assertEqual(pool.snapshot()["in_use"], 0)
                lease = await asyncio.wait_for(pool.acquire_context(settings(), {}), 1)
                await pool.release_context(lease)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await pool.shutdown()

    async def test_hanging_worker_run_returns_a_runner_timeout_and_frees_the_slot(self) -> None:
        runner = CheckRunner()
        check = {"id": 5, "project_id": 1, "type": "api", "name": "probe", "timeout_ms": 1000}

        async def never_finishes(*_args, **_kwargs):
            await asyncio.Event().wait()

        with patch.object(runner, "_execute_core", side_effect=never_finishes), patch.object(
            runner.resources, "reload", new=AsyncMock()
        ), patch.object(runner, "_worker_job_timeout_seconds", return_value=0.05), patch.object(
            storage, "get_settings", return_value={}
        ):
            result = await asyncio.wait_for(
                runner.execute_worker_run(check, "remote", 11, {}),
                timeout=5,
            )

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertFalse(result["affects_health"])
        self.assertIn("超过", str(result["error_message"]))
        self.assertEqual(runner.runtime_status()["workers"]["running"], 0)
        self.assertEqual(runner._worker_jobs, 0)

    async def test_hanging_resource_preparation_is_also_bounded(self) -> None:
        runner = CheckRunner()
        check = {"id": 5, "project_id": 1, "type": "ui", "name": "probe", "timeout_ms": 1000}

        async def never_finishes(*_args, **_kwargs):
            await asyncio.Event().wait()

        with patch.object(runner.resources, "reload", side_effect=never_finishes), patch.object(
            runner, "_worker_job_timeout_seconds", return_value=0.05
        ), patch.object(storage, "get_settings", return_value={}):
            result = await asyncio.wait_for(
                runner.execute_worker_run(check, "remote", 12, {}),
                timeout=5,
            )

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["failure_kind"], "runner")
        self.assertEqual(runner._worker_jobs, 0)

    async def test_normal_worker_run_is_unaffected(self) -> None:
        runner = CheckRunner()
        check = {"id": 5, "project_id": 1, "type": "api", "name": "probe", "timeout_ms": 1000}
        expected = {"status": "ok", "failure_kind": "none"}

        with patch.object(runner, "_execute_core", new=AsyncMock(return_value=expected)), patch.object(
            runner.resources, "reload", new=AsyncMock()
        ), patch.object(storage, "get_settings", return_value={}):
            result = await runner.execute_worker_run(check, "remote", 13, {})

        self.assertEqual(result, expected)


class SchedulerStaggerTests(unittest.TestCase):
    """Same-interval checks must not all fire in the same instant."""

    def _scheduler(self) -> PulseScheduler:
        scheduler = PulseScheduler(Mock())
        scheduler.scheduler = Mock()
        scheduler.scheduler.get_job.return_value = None
        return scheduler

    def _sync(self, scheduler: PulseScheduler, check_id: int, interval: int = 300) -> dict[str, object]:
        check = {"id": check_id, "project_id": 1, "enabled": True, "interval_seconds": interval}
        with patch("backend.app.scheduler.storage.get_check", return_value=check), patch(
            "backend.app.scheduler.storage.get_project", return_value={"id": 1, "status": "active"}
        ):
            scheduler.sync_check(check_id, check=check)
        return scheduler.scheduler.add_job.call_args.kwargs

    def test_checks_sharing_an_interval_get_distinct_start_offsets(self) -> None:
        scheduler = self._scheduler()

        starts = [self._sync(scheduler, check_id)["start_date"] for check_id in range(1, 61)]

        spread = max(starts) - min(starts)
        self.assertGreater(spread.total_seconds(), 60)
        self.assertGreaterEqual(len({start.isoformat() for start in starts}), 50)

    def test_stagger_offset_stays_inside_one_interval_and_is_stable(self) -> None:
        scheduler = self._scheduler()

        first = self._sync(scheduler, 7, interval=300)["start_date"]
        second = self._sync(scheduler, 7, interval=300)["start_date"]

        self.assertEqual(
            PulseScheduler._stagger_offset_seconds(7, 300),
            PulseScheduler._stagger_offset_seconds(7, 300),
        )
        self.assertLess(PulseScheduler._stagger_offset_seconds(7, 300), 300)
        self.assertGreaterEqual(PulseScheduler._stagger_offset_seconds(7, 300), 0)
        self.assertLess(abs((second - first).total_seconds()), 5)

    def test_short_intervals_are_not_delayed_beyond_their_own_period(self) -> None:
        for interval in (5, 10, 30):
            with self.subTest(interval=interval):
                self.assertLess(PulseScheduler._stagger_offset_seconds(42, interval), interval)

    def test_runtime_status_reports_scheduling_capacity(self) -> None:
        scheduler = self._scheduler()
        scheduler.scheduler.get_jobs.return_value = []
        scheduler.scheduler.state = 0

        status = scheduler.runtime_status()

        self.assertIn("capacity", status)
        self.assertIn("required_runs_per_minute", status["capacity"])


class MultiNodeAlertSummaryTests(unittest.TestCase):
    """A multi-node verdict must say which nodes disagreed."""

    def test_alert_body_lists_every_node_result(self) -> None:
        check = {"id": 1, "name": "checkout", "type": "api"}
        run = {"id": 9, "status": "failed", "error_message": "boom", "duration_ms": 12, "finished_at": "2026-01-01T00:00:00+08:00"}
        group_runs = [
            {"runner_name": "beijing", "runner_region": "cn-north", "status": "failed", "failure_kind": "target", "error_message": "timeout"},
            {"runner_name": "shanghai", "runner_region": "cn-east", "status": "ok", "failure_kind": "none"},
            {"runner_name": "shenzhen", "runner_region": "cn-south", "status": "skipped", "failure_kind": "runner", "error_message": "节点不可用"},
        ]

        _, text = notifier._format_message(
            check,
            run,
            {"consecutive_failures": 1, "current_status": "failing"},
            False,
            {},
            group_runs=group_runs,
        )

        self.assertIn("beijing", text)
        self.assertIn("shanghai", text)
        self.assertIn("shenzhen", text)
        self.assertIn("cn-north", text)

    def test_single_node_alert_keeps_its_current_shape(self) -> None:
        check = {"id": 1, "name": "checkout", "type": "api"}
        run = {"id": 9, "status": "failed", "error_message": "boom", "duration_ms": 12}
        only_node = [{"runner_name": "local", "status": "failed", "failure_kind": "target"}]

        _, text = notifier._format_message(check, run, {"consecutive_failures": 1}, False, {}, group_runs=only_node)

        self.assertNotIn("节点结果", text)

    def test_distributed_group_passes_the_node_matrix_to_the_notifier(self) -> None:
        async def scenario() -> tuple[object, ...]:
            runner = CheckRunner()
            check = {"id": 3, "project_id": 1, "type": "api", "name": "probe"}
            runs = [
                {"id": 1, "status": "failed", "failure_kind": "target", "affects_health": True, "runner_name": "a"},
                {"id": 2, "status": "ok", "failure_kind": "none", "affects_health": True, "runner_name": "b"},
            ]
            with patch.object(storage, "update_check_status", return_value={"current_status": "failing"}), patch(
                "backend.app.runner.notifier.maybe_notify", new=AsyncMock()
            ) as maybe_notify:
                await runner._finish_distributed_group(check, runs, "scheduled")
            return maybe_notify.await_args

        await_args = asyncio.run(scenario())

        self.assertEqual(await_args.kwargs["group_runs"], await_args.kwargs["group_runs"])
        self.assertEqual(len(await_args.kwargs["group_runs"]), 2)


if __name__ == "__main__":
    unittest.main()
