from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.app import storage, run_comparison
from backend.app.main import app
from backend.app.run_statistics import get_run_statistics


class RunHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        db = patch.object(storage, "DB_PATH", Path(self.temp.name) / "history.db")
        db.start()
        self.addCleanup(db.stop)
        artifacts = patch.object(storage, "cleanup_old_artifacts", return_value={})
        artifacts.start()
        self.addCleanup(artifacts.stop)
        storage.init_db()
        self.check = storage.list_checks("api")[0]
        self.project = self.check["project_id"]

    def seed(self, days=20, status="ok", check=None, when=None):
        check = check or self.check
        timestamp = (datetime.fromisoformat(when).astimezone().isoformat() if when else
                     (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds"))
        with storage._connect() as conn:
            return conn.execute("""INSERT INTO runs(project_id,check_id,check_name,check_type,status,
                started_at,created_at,finished_at,duration_ms,logs,error_stack,request_snapshot,response_snapshot)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (check["project_id"], check["id"], check["name"], check["type"], status,
                 timestamp, timestamp, timestamp if status != "running" else None, 123,
                 "log"*5000, "stack"*1000, '{"url":"https://example.com"}', '{"body":"large"}')).lastrowid

    def window(self):
        today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return (today - timedelta(days=200)).isoformat(), (today + timedelta(days=1)).isoformat()

    def test_details_expire_but_each_summary_and_active_run_survives(self):
        old, recent, active = self.seed(), self.seed(days=0), self.seed(status="running")
        self.assertEqual(storage.cleanup_old_data({"run_retention_days": 365, "run_detail_retention_days": 10}), 0)
        row = storage.get_run(old)
        self.assertEqual((row["status"], row["duration_ms"]), ("ok", 123))
        self.assertTrue(row["details_pruned_at"])
        for name in ("logs", "error_stack", "request_snapshot", "response_snapshot"):
            self.assertIsNone(row[name])
        for run_id in (recent, active):
            self.assertTrue(storage.get_run(run_id)["logs"])
            self.assertIsNone(storage.get_run(run_id)["details_pruned_at"])
        self.assertEqual(storage.list_runs_page()["total"], 3)
        stamp = row["details_pruned_at"]
        storage.cleanup_old_data({"run_retention_days": 365, "run_detail_retention_days": 10})
        self.assertEqual(storage.get_run(old)["details_pruned_at"], stamp)

    def test_existing_database_migrates_once_preserving_detail_policy_and_records(self):
        run_id = self.seed()
        with storage._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = 'run_detail_retention_days'")
            conn.execute("UPDATE settings SET value='10' WHERE key='run_retention_days'")
            conn.execute("DROP INDEX idx_runs_unpruned")
            conn.execute("ALTER TABLE runs DROP COLUMN details_pruned_at")
            conn.execute("DROP TABLE run_history_policy")
        storage.init_db()
        self.assertEqual(storage.get_settings()["run_retention_days"], 365)
        self.assertEqual(storage.get_settings()["run_detail_retention_days"], 10)
        self.assertTrue(storage.get_run(run_id)["logs"])
        storage.update_settings({"run_retention_days": 400, "run_detail_retention_days": 7})
        storage.init_db()
        self.assertEqual(storage.get_settings()["run_retention_days"], 400)
        self.assertEqual(storage.get_settings()["run_detail_retention_days"], 7)
        with storage._connect(read_only=True) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_counts_survive_summary_archive_without_double_counting_or_cross_project_leaks(self):
        self.seed(days=100)
        self.seed(days=2, status="preparation_failed")
        other = storage.create_project("Other history")
        other_check = storage.create_check({**self.check, "name": "Other"}, project_id=other["id"])
        self.seed(check=other_check)
        start, end = self.window()
        before = get_run_statistics(self.project, start, end)
        self.assertEqual(before["total"], 2)
        self.assertEqual(before["executed_terminal_count"], 1)
        storage.cleanup_old_data({"run_retention_days": 30, "run_detail_retention_days": 10})
        after = get_run_statistics(self.project, start, end)
        self.assertEqual(after["total"], before["total"])
        self.assertEqual(after["sources"], {"retained": 1, "archived": 1})
        self.assertFalse(after["coverage"]["complete"])
        filtered = get_run_statistics(self.project, start, end, observation_kind="observation")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["coverage"]["excluded_archive_count"], 1)
        self.assertTrue(filtered["warnings"])

    def test_half_open_range_excludes_end_and_rejects_ambiguous_dates(self):
        self.seed(when="2026-07-01T00:00:00+08:00")
        self.seed(when="2026-10-01T00:00:00+08:00")
        result = get_run_statistics(self.project, "2026-07-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
        self.assertEqual(result["total"], 1)
        for start, end in (("2026-07-01", "2026-10-01"), ("bad", "bad"),
                           ("2026-10-01T00:00:00+08:00", "2026-07-01T00:00:00+08:00")):
            with self.assertRaises(ValueError):
                get_run_statistics(self.project, start, end)

    def test_partial_archive_day_is_reported_as_incomplete(self):
        day = datetime(2026, 6, 10).astimezone()
        self.seed(when=(day + timedelta(hours=10)).isoformat())
        storage.cleanup_old_data({"run_retention_days": 1})
        result = get_run_statistics(self.project, (day + timedelta(hours=12)).isoformat(), (day + timedelta(days=1)).isoformat())
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["coverage"]["excluded_archive_count"], 1)

    def test_offset_filters_and_subsecond_statistics_use_the_requested_instant(self):
        instant = datetime(2026, 7, 1).astimezone()
        self.seed(when=instant.isoformat())
        self.seed(when=(instant + timedelta(seconds=1)).isoformat())
        start = (instant + timedelta(microseconds=500000)).astimezone(timezone.utc).isoformat()
        end = (instant + timedelta(seconds=2)).astimezone(timezone.utc).isoformat()
        self.assertEqual(get_run_statistics(self.project, start, end)["total"], 1)
        self.assertEqual(storage.list_runs_page({"start": start, "end": end, "project_id": self.project})["total"], 1)

    def test_pruned_comparison_does_not_claim_empty_snapshots_are_equal(self):
        self.seed(days=21)
        failed = self.seed(status="failed")
        storage.cleanup_old_data({"run_retention_days": 365, "run_detail_retention_days": 10})
        result = run_comparison.compare_with_previous_success(failed)
        self.assertFalse(result["available"])
        self.assertIn("清理", result["message"])

    def test_api_validates_query_and_keeps_project_scope(self):
        self.seed()
        client = TestClient(app)
        start, end = self.window()
        headers = {"X-PulseGuard-Project-Id": str(self.project)}
        result = client.get("/api/run-statistics", params={"start": start, "end": end}, headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["total"], 1)
        self.assertEqual(client.get("/api/run-statistics", params={"start": "bad", "end": end}, headers=headers).status_code, 400)
        self.assertEqual(client.get("/api/run-statistics", params={"start": start, "end": end, "type": "bad"}, headers=headers).status_code, 422)


if __name__ == "__main__":
    unittest.main()
