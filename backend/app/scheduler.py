from __future__ import annotations

import zlib
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.base import STATE_PAUSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import storage
from .config import TIMEZONE
from .runner import CheckRunner


class PulseScheduler:
    def __init__(self, runner: CheckRunner) -> None:
        self.runner = runner
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    def start(self) -> None:
        paused = storage.is_deployment_window_active()
        if not self.scheduler.running:
            self.scheduler.start(paused=paused)
        elif paused:
            self.scheduler.pause()
        self.scheduler.add_job(
            storage.cleanup_old_data,
            "interval",
            hours=24,
            id="cleanup-old-runs",
            replace_existing=True,
        )
        self.scheduler.add_job(
            storage.refresh_stale_statuses,
            "interval",
            minutes=1,
            id="refresh-stale-checks",
            replace_existing=True,
        )
        self.refresh_all()

    def pause(self) -> None:
        if self.scheduler.running:
            self.scheduler.pause()

    def resume(self) -> None:
        if self.scheduler.running:
            self.scheduler.resume()
        self.refresh_all()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def refresh_all(self) -> None:
        for job in list(self.scheduler.get_jobs()):
            if job.id.startswith("check-"):
                self.scheduler.remove_job(job.id)
        for check in storage.list_checks(enabled_only=True, active_projects_only=True):
            self.sync_check(int(check["id"]), check=check)

    def sync_check(self, check_id: int, check: dict[str, Any] | None = None) -> None:
        job_id = self._job_id(check_id)
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        check = check or storage.get_check(check_id)
        project = storage.get_project(int(check.get("project_id") or 0)) if check else None
        if not check or not check.get("enabled") or not project or project.get("status") != "active":
            return

        interval_seconds = max(5, int(check.get("interval_seconds") or 300))
        offset = self._stagger_offset_seconds(check_id, interval_seconds)
        self.scheduler.add_job(
            self._run_scheduled,
            "interval",
            seconds=interval_seconds,
            # Without a per-check offset every check sharing an interval is
            # registered at the same instant and fires as one burst, so a burst
            # larger than the queue turns real observations into skips.  The
            # offset is derived from the check id so restarts keep the same
            # slot instead of reshuffling the whole fleet.
            start_date=datetime.now().astimezone() + timedelta(seconds=offset),
            jitter=self._stagger_jitter_seconds(interval_seconds),
            id=job_id,
            args=[check_id],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
            replace_existing=True,
        )

    @staticmethod
    def _stagger_offset_seconds(check_id: int, interval_seconds: int) -> int:
        """Spread a check's first fire deterministically across its own period."""
        interval_seconds = max(1, int(interval_seconds))
        digest = zlib.crc32(f"pulseguard-check-{int(check_id)}".encode("utf-8"))
        return digest % interval_seconds

    @staticmethod
    def _stagger_jitter_seconds(interval_seconds: int) -> int:
        """Small per-fire dispersion so drift cannot re-converge the fleet."""
        return max(1, min(30, int(max(1, int(interval_seconds)) * 0.1)))

    async def _run_scheduled(self, check_id: int) -> None:
        await self.runner.run_check(check_id, trigger="scheduled")

    def runtime_status(self) -> dict[str, Any]:
        now = datetime.now().astimezone()
        jobs = [job for job in self.scheduler.get_jobs() if job.id.startswith("check-")]
        next_times = [job.next_run_time for job in jobs if job.next_run_time is not None]
        overdue = sum(1 for next_time in next_times if next_time < now)
        return {
            "running": self.scheduler.running,
            "paused": self.scheduler.state == STATE_PAUSED,
            "scheduled_checks": len(jobs),
            "next_due_at": min(next_times).isoformat(timespec="seconds") if next_times else None,
            "overdue_jobs": overdue,
            "capacity": self._capacity_status(jobs),
        }

    @staticmethod
    def _capacity_status(jobs: list[Any]) -> dict[str, Any]:
        """Report scheduled demand so queue skips can be attributed, not guessed.

        Staggering spreads a burst but does not create throughput.  Surfacing
        the required run rate lets an operator see whether skipped runs mean a
        burst collision or genuinely insufficient capacity.
        """
        intervals: list[int] = []
        for job in jobs:
            interval = getattr(getattr(job, "trigger", None), "interval", None)
            seconds = getattr(interval, "total_seconds", None)
            if callable(seconds):
                intervals.append(max(1, int(seconds())))
        required_per_minute = sum(60.0 / interval for interval in intervals)
        return {
            "required_runs_per_minute": round(required_per_minute, 2),
            "shortest_interval_seconds": min(intervals) if intervals else None,
            "measured_checks": len(intervals),
        }

    @staticmethod
    def _job_id(check_id: int) -> str:
        return f"check-{check_id}"
