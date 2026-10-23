from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from backend.app import async_storage, automation_jobs, main, storage
from backend.app.runner import CheckRunner


class AsyncStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_probe_settles_health_and_notification_despite_repeated_cancellation(self):
        for cancel_at in ('result', 'health', 'notification'):
            with self.subTest(cancel_at=cancel_at):
                entered, release = asyncio.Event(), threading.Event()
                loop = asyncio.get_running_loop()
                row = {'id': 73, 'status': 'running', 'notification_status': 'pending'}
                events = []

                def pause(stage):
                    if stage == cancel_at:
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(2):
                            raise RuntimeError('test finalization wait timed out')

                def finish(_run_id, data):
                    row.update(data)
                    events.append('result')
                    pause('result')
                    return dict(row)

                def health(*_args):
                    events.append('health')
                    pause('health')
                    return {'status': 'failed'}

                async def notify(*_args):
                    events.append('notification')
                    await asyncio.to_thread(pause, 'notification')
                    row['notification_status'] = 'sent'

                runner = CheckRunner.__new__(CheckRunner)
                runner._execute_core = AsyncMock(return_value={
                    'status': 'failed', 'failure_kind': 'target', 'affects_health': True,
                })
                with patch.object(storage, 'get_settings', return_value={}), \
                     patch.object(storage, 'settings_for_project', return_value={}), \
                     patch.object(storage, 'finish_run', side_effect=finish), \
                     patch.object(storage, 'get_run', side_effect=lambda *_: dict(row)), \
                     patch.object(storage, 'update_check_status', side_effect=health), \
                     patch('backend.app.runner.notifier.maybe_notify', side_effect=notify):
                    task = asyncio.create_task(runner._execute({'id': 1}, 'scheduled', 73))
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    await asyncio.sleep(0)
                    task.cancel()
                    self.assertFalse(task.done())
                    release.set()
                    result = await task
                self.assertEqual(events, ['result', 'health', 'notification'])
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['notification_status'], 'sent')

    async def test_watchdog_during_finalization_returns_real_outcome(self):
        started = asyncio.Event()

        async def finish():
            started.set()
            await asyncio.sleep(0.04)
            return {'status': 'ok'}

        result = await asyncio.wait_for(CheckRunner._complete_outcome(finish()), timeout=0.01)
        self.assertTrue(started.is_set())
        self.assertEqual(result['status'], 'ok')

    async def test_finalization_failure_is_not_hidden_by_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def finish():
            started.set()
            await release.wait()
            raise RuntimeError('health write failed')

        task = asyncio.create_task(CheckRunner._complete_outcome(finish()))
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaisesRegex(RuntimeError, 'health write failed'):
            await task

    async def test_repeated_cancellation_finishes_job_future_and_releases_admission(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def read(*args):
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            return {'id': 5, 'status': 'skipped'}
        runner = CheckRunner.__new__(CheckRunner)
        runner._lock = asyncio.Lock()
        runner._active_checks = {1}
        runner._running_jobs, runner._queued_jobs = 1, 0
        runner._run_job_body = AsyncMock(side_effect=asyncio.CancelledError)
        runner._finish_cancelled_job = Mock(return_value=None)
        job = SimpleNamespace(check={'id': 1}, run_id=5, trigger='test', manage_capacity=True,
                              manage_active=True, started=True, future=loop.create_future())
        with patch.object(storage, 'get_run', side_effect=read):
            task = asyncio.create_task(runner._run_job(job))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(job.future.result()['status'], 'skipped')
        self.assertEqual(runner._running_jobs, 0)
        self.assertEqual(runner._active_checks, set())

    async def test_cancelled_settings_read_does_not_create_unowned_probe_coroutine(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def read():
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            return {}
        runner = CheckRunner.__new__(CheckRunner)
        runner._execute = AsyncMock()
        job = SimpleNamespace(check={}, trigger='test', run_id=1, record_status=True, notify=False, runner_metadata={})
        with patch.object(storage, 'get_settings', side_effect=read):
            task = asyncio.create_task(runner._execute_started_job(job))
            await asyncio.wait_for(entered.wait(), 1)
            runner._execute.assert_not_called()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_pending_runner_write_does_not_block_health_http(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def blocked(*args):
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            return {'runner_id': 'child'}
        with patch.object(main, '_fetch_runner_health', new=AsyncMock(return_value={})), \
             patch.object(storage, 'mark_probe_runner_available', side_effect=blocked), \
             patch.object(storage, 'get_settings', return_value={}), \
             patch.object(main, '_schedule_runner_browser_install'):
            poll = asyncio.create_task(main._poll_runner_health({'runner_id': 'child'}, asyncio.Semaphore(1), asyncio.Semaphore(1), {}, {}))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                    response = await asyncio.wait_for(client.get('/api/health'), 1)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], 'ok')
                self.assertFalse(poll.done(), 'Database operation must still be pending while HTTP responds')
            finally:
                release.set()
                await poll

    async def test_storage_failure_does_not_mark_healthy_remote_offline(self):
        with patch.object(main, '_fetch_runner_health', new=AsyncMock(return_value={})), \
             patch.object(storage, 'mark_probe_runner_available', side_effect=RuntimeError('database locked')), \
             patch.object(storage, 'mark_probe_runner_unavailable') as unavailable, \
             patch.object(main, '_schedule_runner_unavailable_notification') as notify:
            with self.assertRaisesRegex(RuntimeError, 'database locked'):
                await main._poll_runner_health({'runner_id': 'child'}, asyncio.Semaphore(1), asyncio.Semaphore(1), {}, {})
        unavailable.assert_not_called()
        notify.assert_not_called()

    async def test_cancelled_insert_settles_run_before_propagating_repeated_cancellation(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def insert(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            return {'id': 73}
        with patch.object(storage, 'create_run', side_effect=insert), \
             patch.object(storage, 'finish_run') as finish, patch.object(storage, 'update_run_notification'):
            task = asyncio.create_task(async_storage.create_run({}, 'pending'))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args[0], 73)
        self.assertEqual(finish.call_args.args[1]['status'], 'skipped')

    async def test_cancelled_batch_claim_is_unknown_and_never_replayed(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        claimed = {'batch_id': 'batch', 'check_id': 3}
        def claim():
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
            return claimed
        runner = SimpleNamespace(runtime_status=lambda: {}, run_check=AsyncMock())
        with patch.object(automation_jobs, 'claim', side_effect=claim), patch.object(automation_jobs, 'finish') as finish:
            task = asyncio.create_task(automation_jobs.BatchExecutor(runner)._loop())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(finish.call_args.args, (claimed, 'unknown'))
        runner.run_check.assert_not_awaited()

    async def test_cancelled_validation_audit_does_not_leave_unowned_queued_validation(self):
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def audit(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
        row = {'id': 8, 'candidate_revision': 1, 'status': 'queued'}
        with patch.object(storage, 'create_project_library_validation', return_value=row), \
             patch.object(storage, 'record_audit_event', side_effect=audit), \
             patch.object(storage, 'finish_project_library_validation') as finish, \
             patch.object(main, '_track_background_task') as track:
            task = asyncio.create_task(main.validate_library(SimpleNamespace(candidate_revision=1, include_disabled=False), None, 1))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finish.assert_called_once_with(1, 8, 'cancelled', checks=[], diagnostics=[])
        track.assert_not_called()


if __name__ == '__main__':
    unittest.main()
