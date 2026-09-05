from __future__ import annotations

from datetime import datetime
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

        self.scheduler.add_job(
            self._run_scheduled,
            "interval",
            seconds=max(5, int(check.get("interval_seconds") or 300)),
            id=job_id,
            args=[check_id],
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
            replace_existing=True,
        )

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
        }

    @staticmethod
    def _job_id(check_id: int) -> str:
        return f"check-{check_id}"
