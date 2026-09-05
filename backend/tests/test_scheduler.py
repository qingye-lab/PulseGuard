from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from backend.app.scheduler import PulseScheduler


class PulseSchedulerTests(unittest.TestCase):
    def test_check_jobs_tolerate_short_event_loop_delays(self) -> None:
        runner = Mock()
        scheduler = PulseScheduler(runner)
        scheduler.scheduler = Mock()
        scheduler.scheduler.get_job.return_value = None
        check = {"id": 7, "project_id": 1, "enabled": True, "interval_seconds": 60}

        with patch("backend.app.scheduler.storage.get_check", return_value=check), patch(
            "backend.app.scheduler.storage.get_project", return_value={"id": 1, "status": "active"}
        ):
            scheduler.sync_check(7)

        add_job = scheduler.scheduler.add_job
        self.assertEqual(add_job.call_args.kwargs["misfire_grace_time"], 30)
        self.assertEqual(add_job.call_args.kwargs["max_instances"], 1)
        self.assertTrue(add_job.call_args.kwargs["coalesce"])


if __name__ == "__main__":
    unittest.main()
