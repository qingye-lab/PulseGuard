from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend.app.runner import BrowserRunTarget, CheckRunner


class DatabaseMaintenanceOrphanTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_group_keeps_its_shielded_local_child_in_drain_accounting(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        check = {"id": 1, "project_id": 1, "name": "API", "type": "api"}
        local = {"runner_id": "local", "enabled": True, "available": True}

        with patch("backend.app.runner.storage.get_settings", return_value={}):
            runner = CheckRunner()

            async def execute(job):
                entered.set()
                await release.wait()
                await runner._complete_job(job, {"id": job.run_id, "status": "ok"})

            with patch("backend.app.runner.storage.create_run", return_value={"id": 10}), patch(
                "backend.app.runner.storage.can_schedule_runner", return_value=True
            ), patch("backend.app.runner.storage.runner_metadata", return_value={}), patch.object(
                runner, "_run_job", side_effect=execute
            ):
                group = asyncio.create_task(runner._run_distributed_check(
                    check, [BrowserRunTarget(local, "")], "manual"
                ))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    group.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await group
                    self.assertEqual(sum(not task.done() for task in runner._jobs), 1)
                    # The parent group counters are gone, but its shielded local
                    # child still owns a run id from the pre-restore database.
                    async with runner.maintenance_window():
                        self.assertFalse((await runner.wait_for_idle(0))["idle"])
                        release.set()
                        await asyncio.wait_for(asyncio.gather(*runner._jobs), 2)
                        self.assertTrue((await runner.wait_for_idle(0))["idle"])
                finally:
                    release.set()
                    if not group.done():
                        group.cancel()
                    await asyncio.gather(group, *runner._jobs, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
