from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from backend.app import main
from backend.app.runner import BrowserRunTarget, CheckRunner, RuntimeSettingsReloadError


class DatabaseMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings_patch = patch("backend.app.runner.storage.get_settings", return_value={})
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.runner = CheckRunner()
        self.runner.reload_settings = AsyncMock()
        self.scheduler = Mock()
        self.scheduler.runtime_status.return_value = {"paused": False}
        self.request = Request({"type": "http", "app": SimpleNamespace(
            state=SimpleNamespace(runner=self.runner, scheduler=self.scheduler)
        )})
        self.check = {"id": 1, "project_id": 1, "name": "API", "type": "api"}

    async def test_maintenance_rejects_new_drafts_and_groups_without_recording_runs(self) -> None:
        with patch("backend.app.runner.storage.create_run") as create_run:
            async with self.runner.maintenance_window():
                local = await self.runner._submit(self.check, "manual")
                draft = await self.runner._submit({**self.check, "id": 0}, "draft", manage_active=False)
                group = await self.runner._run_distributed_check(
                    self.check, [BrowserRunTarget({"runner_id": "remote"}, "")], "post-deploy"
                )
                with self.assertRaisesRegex(RuntimeError, "维护正在进行"):
                    async with self.runner.maintenance_window():
                        self.fail("a second maintenance window was admitted")
            self.assertFalse(self.runner._maintenance_active)
        for result in (local, draft, group):
            self.assertEqual(result["id"], 0)
            self.assertEqual(result["status"], "skipped")
        create_run.assert_not_called()

    async def test_accepted_job_drains_while_new_admissions_are_paused(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def execute(job):
            entered.set()
            await release.wait()
            await self.runner._complete_job(job, {"id": job.run_id, "status": "ok"})

        with patch("backend.app.runner.storage.create_run", return_value={"id": 10}), patch.object(
            self.runner, "_run_job", side_effect=execute
        ):
            submitted = asyncio.create_task(self.runner._submit(self.check, "manual"))
            await asyncio.wait_for(entered.wait(), 2)
            async with self.runner.maintenance_window():
                self.assertFalse((await self.runner.wait_for_idle(0))["idle"])
                release.set()
                self.assertEqual((await asyncio.wait_for(submitted, 2))["status"], "ok")
                self.assertTrue((await self.runner.wait_for_idle(0))["idle"])

    async def test_already_counted_group_child_can_finish_during_maintenance(self) -> None:
        async def execute(job):
            await self.runner._complete_job(job, {"id": job.run_id, "status": "ok"})

        with patch("backend.app.runner.storage.create_run", return_value={"id": 11}), patch.object(
            self.runner, "_run_job", side_effect=execute
        ):
            async with self.runner.maintenance_window():
                result = await self.runner._submit(
                    self.check, "scheduled", manage_active=False, manage_capacity=False
                )
        self.assertEqual(result["status"], "ok")

    async def test_restore_timeout_preserves_database_and_reopens_admissions(self) -> None:
        self.runner.wait_for_idle = AsyncMock(return_value={"idle": False})
        with patch("backend.app.main.storage.restore_database_backup") as restore, patch(
            "backend.app.main.storage.is_deployment_window_active", return_value=False
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.restore_database_backup("backup.db", self.request)
        self.assertEqual(raised.exception.status_code, 409)
        restore.assert_not_called()
        self.runner.reload_settings.assert_not_called()
        self.scheduler.resume.assert_called_once()
        self.assertFalse(self.runner._maintenance_active)

    async def test_restore_waits_for_idle_reloads_and_preserves_scheduler_pause(self) -> None:
        self.scheduler.runtime_status.return_value = {"paused": True}
        self.runner.wait_for_idle = AsyncMock(return_value={"idle": True})
        expected = {"restored": {"filename": "backup.db"}, "safety_backup": {"filename": "safety.db"}}

        def restore(_filename):
            self.assertTrue(self.runner._maintenance_active)
            self.runner.wait_for_idle.assert_awaited_once_with(30)
            return expected

        with patch("backend.app.main.storage.restore_database_backup", side_effect=restore), patch(
            "backend.app.main.storage.record_audit_event"
        ), patch("backend.app.main.storage.is_deployment_window_active", return_value=False):
            result = await main.restore_database_backup("backup.db", self.request)
        self.assertEqual(result, expected)
        self.runner.reload_settings.assert_awaited_once()
        self.scheduler.resume.assert_not_called()
        self.scheduler.refresh_all.assert_called_once()
        self.assertFalse(self.runner._maintenance_active)

    async def test_cancellation_keeps_admissions_closed_until_restore_thread_finishes(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def restore(_filename):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release restore")
            return {"restored": {}, "safety_backup": {}}

        with patch("backend.app.main.storage.restore_database_backup", side_effect=restore), patch(
            "backend.app.main.storage.is_deployment_window_active", return_value=False
        ), patch("backend.app.main.storage.record_audit_event"):
            task = asyncio.create_task(main.restore_database_backup("backup.db", self.request))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(self.runner._maintenance_active)
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(self.runner._maintenance_active)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            finally:
                release.set()
        self.runner.reload_settings.assert_awaited_once()
        self.assertFalse(self.runner._maintenance_active)

    async def test_missing_backup_returns_404_without_leaving_runner_paused(self) -> None:
        with patch("backend.app.main.storage.restore_database_backup", side_effect=ValueError("数据库备份不存在")), patch(
            "backend.app.main.storage.is_deployment_window_active", return_value=False
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.restore_database_backup("missing.db", self.request)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertFalse(self.runner._maintenance_active)
        self.runner.reload_settings.assert_not_called()

    async def test_unrecoverable_restore_keeps_scheduler_and_submissions_paused(self) -> None:
        with patch("backend.app.main.storage.restore_database_backup", side_effect=main.storage.DatabaseRestoreRecoveryError(
            "恢复与补偿均失败，请使用安全备份恢复数据库"
        )), patch("backend.app.main.storage.is_deployment_window_active", return_value=False):
            with self.assertRaises(main.storage.DatabaseRestoreRecoveryError):
                await main.restore_database_backup("broken.db", self.request)
        self.assertTrue(self.runner._maintenance_active)
        self.scheduler.resume.assert_not_called()
        self.scheduler.refresh_all.assert_not_called()
        self.runner.reload_settings.assert_not_called()
        with patch("backend.app.runner.storage.create_run") as create_run:
            self.assertEqual((await self.runner._submit(self.check, "manual"))["id"], 0)
        create_run.assert_not_called()

    async def test_runtime_reload_failure_keeps_execution_paused_after_successful_restore(self) -> None:
        self.runner.reload_settings.side_effect = RuntimeError("pool reload failed")
        with patch("backend.app.main.storage.restore_database_backup", return_value={"restored": {}, "safety_backup": {}}):
            with self.assertRaisesRegex(RuntimeSettingsReloadError, "数据库已恢复"):
                await main.restore_database_backup("valid.db", self.request)
        self.assertTrue(self.runner._maintenance_active)
        self.scheduler.resume.assert_not_called()
        self.scheduler.refresh_all.assert_not_called()

    async def test_restore_on_worker_without_scheduler_is_rejected_before_database_access(self) -> None:
        self.request.app.state.scheduler = None
        with patch("backend.app.main.storage.restore_database_backup") as restore:
            with self.assertRaises(HTTPException) as raised:
                await main.restore_database_backup("backup.db", self.request)
        self.assertEqual(raised.exception.status_code, 409)
        restore.assert_not_called()

    async def test_worker_execution_is_counted_and_rejected_during_maintenance(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def execute(*_args):
            entered.set()
            await release.wait()
            return {"id": 91, "status": "ok"}

        with patch.object(self.runner, "_execute_worker_run", side_effect=execute) as execute_worker:
            accepted = asyncio.create_task(self.runner.execute_worker_run(self.check, "remote", 91, {}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                async with self.runner.maintenance_window():
                    self.assertFalse((await self.runner.wait_for_idle(0))["idle"])
                    rejected = await self.runner.execute_worker_run(self.check, "remote", 92, {})
                    self.assertEqual(rejected["status"], "skipped")
                    execute_worker.assert_awaited_once()
                    release.set()
                    await asyncio.wait_for(accepted, 2)
                    self.assertTrue((await self.runner.wait_for_idle(0))["idle"])
            finally:
                release.set()
                await accepted

    async def test_manual_recovery_errors_are_returned_as_actionable_503(self) -> None:
        response = await main.database_recovery_required_handler(
            self.request, RuntimeSettingsReloadError("数据库已恢复，请检查配置并重启服务")
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("数据库已恢复", response.body.decode())
