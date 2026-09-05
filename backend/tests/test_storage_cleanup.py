from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app import storage


class StorageCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        db_patch = patch.object(storage, "DB_PATH", Path(self.temp.name) / "cleanup.db")
        artifact_patch = patch.object(storage, "cleanup_old_artifacts", return_value={})
        db_patch.start()
        artifact_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(artifact_patch.stop)
        storage.init_db()
        self.check = storage.list_checks("api")[0]

    def seed_runs(
        self, count: int, *, status: str = "ok", days: int = 3,
        check: dict | None = None, duration: int | None = 100, draft: bool = False,
    ) -> list[int]:
        check = check or self.check
        timestamp = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        ids = []
        with storage._connect() as conn:
            for _ in range(count):
                cursor = conn.execute(
                    """INSERT INTO runs(project_id, check_id, check_name, check_type, status,
                                        started_at, finished_at, duration_ms, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (check["project_id"], 0 if draft else check["id"], check["name"], check["type"], status,
                     timestamp, None if status in {"pending", "running"} else timestamp, duration, timestamp),
                )
                ids.append(int(cursor.lastrowid))
        return ids

    def test_cleanup_limits_batches_and_archives_every_run_exactly_once(self) -> None:
        self.seed_runs(1201)
        batch_sizes: list[int] = []
        real_archive = storage._archive_runs

        def archive(conn: sqlite3.Connection, ids: list[int]) -> None:
            self.assertTrue(conn.in_transaction)
            batch_sizes.append(len(ids))
            real_archive(conn, ids)

        with patch.object(storage, "_archive_runs", side_effect=archive):
            removed = storage.cleanup_old_data({"run_retention_days": 1})
        self.assertEqual(removed, 1201)
        self.assertEqual(batch_sizes, [500, 500, 201])
        archives = storage.list_run_archives()
        self.assertEqual(sum(row["run_count"] for row in archives), 1201)
        self.assertEqual(sum(row["duration_sum_ms"] for row in archives), 120100)
        self.assertEqual(storage.cleanup_old_data({"run_retention_days": 1}), 0)
        self.assertEqual(storage.list_run_archives(), archives)

    def test_existing_database_gets_idempotent_cleanup_index_and_batch_query_uses_it(self) -> None:
        self.seed_runs(3)
        with storage._connect() as conn:
            conn.execute("DROP INDEX idx_runs_cleanup_terminal")
        storage.init_db()
        storage.init_db()
        with storage._connect(read_only=True) as conn:
            plan = conn.execute(
                f"EXPLAIN QUERY PLAN {storage._RUN_CLEANUP_BATCH_SQL}",
                (storage.now_iso(), 500),
            ).fetchall()
            details = " ".join(str(row["detail"]) for row in plan)
        self.assertIn("SEARCH runs USING INDEX idx_runs_cleanup_terminal", details)
        self.assertNotIn("USE TEMP B-TREE", details)

    def test_failure_rolls_back_current_batch_and_retry_does_not_double_archive(self) -> None:
        self.seed_runs(1001)
        real_archive = storage._archive_runs
        calls = 0

        def fail_second_batch(conn: sqlite3.Connection, ids: list[int]) -> None:
            nonlocal calls
            calls += 1
            real_archive(conn, ids)
            if calls == 2:
                raise RuntimeError("interrupted batch")

        with patch.object(storage, "_archive_runs", side_effect=fail_second_batch):
            with self.assertRaisesRegex(RuntimeError, "interrupted batch"):
                storage.cleanup_old_data({"run_retention_days": 1})
        self.assertEqual(storage.list_runs_page()["total"], 501)
        self.assertEqual(sum(row["run_count"] for row in storage.list_run_archives()), 500)
        self.assertEqual(storage.cleanup_old_data({"run_retention_days": 1}), 501)
        self.assertEqual(sum(row["run_count"] for row in storage.list_run_archives()), 1001)
        self.assertEqual(storage.list_runs_page()["total"], 0)

    def test_cleanup_preserves_active_and_recent_runs_and_clears_both_status_references(self) -> None:
        expired = self.seed_runs(2)
        pending = self.seed_runs(1, status="pending")[0]
        running = self.seed_runs(1, status="running")[0]
        recent = self.seed_runs(1, days=0)[0]
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO check_status(check_id, current_status, monitor_status,
                                            last_run_id, last_scheduled_run_id, updated_at)
                   VALUES (?, 'healthy', 'healthy', ?, ?, '')""",
                (self.check["id"], expired[0], expired[1]),
            )
        self.assertEqual(storage.cleanup_old_data({"run_retention_days": 1}), 2)
        remaining = {run["id"] for run in storage.list_runs()}
        self.assertEqual(remaining, {pending, running, recent})
        check = storage.get_check(self.check["id"], refresh_stale=False)
        self.assertIsNone(check["last_run_id"])
        self.assertIsNone(check["last_scheduled_run_id"])
        result = storage.finish_run(running, {"status": "ok", "finished_at": storage.now_iso(), "duration_ms": 50})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(storage.start_run(pending)["status"], "running")

    def test_cleanup_keeps_project_archive_groups_separate_and_omits_drafts(self) -> None:
        other_project = storage.create_project("Other archive project")
        other_check = storage.create_check({**self.check, "name": "Other archive task"}, project_id=other_project["id"])
        self.seed_runs(2, status="failed", duration=None)
        self.seed_runs(3, status="failed", duration=70, check=other_check)
        self.seed_runs(1, status="skipped", draft=True)
        self.assertEqual(storage.cleanup_old_data({"run_retention_days": 1}), 6)
        archives = {item["project_id"]: item for item in storage.list_run_archives()}
        self.assertEqual(archives[self.check["project_id"]]["run_count"], 2)
        self.assertEqual(archives[self.check["project_id"]]["duration_sample_count"], 0)
        self.assertEqual(archives[other_project["id"]]["run_count"], 3)
        self.assertEqual(archives[other_project["id"]]["duration_sum_ms"], 210)

    def test_two_cleanup_callers_do_not_double_archive_and_readers_still_progress(self) -> None:
        self.seed_runs(1001)
        archive_started = threading.Event()
        release_archive = threading.Event()
        real_archive = storage._archive_runs
        blocked_once = False

        def blocked_archive(conn: sqlite3.Connection, ids: list[int]) -> None:
            nonlocal blocked_once
            real_archive(conn, ids)
            if not blocked_once:
                blocked_once = True
                archive_started.set()
                self.assertTrue(release_archive.wait(5))

        with ThreadPoolExecutor(max_workers=3) as pool, patch.object(storage, "_archive_runs", side_effect=blocked_archive):
            first = pool.submit(storage.cleanup_old_data, {"run_retention_days": 1})
            try:
                self.assertTrue(archive_started.wait(5))
                second = pool.submit(storage.cleanup_old_data, {"run_retention_days": 1})
                page = pool.submit(storage.list_runs_page).result(timeout=2)
                self.assertEqual(page["total"], 1001)
            finally:
                release_archive.set()
            self.assertEqual(first.result(timeout=5) + second.result(timeout=5), 1001)
        self.assertEqual(sum(row["run_count"] for row in storage.list_run_archives()), 1001)

    def test_artifact_reference_updates_respect_sqlite_variable_limit(self) -> None:
        ids = self.seed_runs(1003, days=0)
        paths = [f"responses/response-{run_id}.json" for run_id in ids]
        with storage._connect() as conn:
            conn.executemany("UPDATE runs SET response_path = ? WHERE id = ?", zip(paths, ids))
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 500)
            storage._clear_artifact_paths(conn, "response_path", paths)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs WHERE response_path IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1003)
            with self.assertRaises(ValueError):
                storage._clear_artifact_paths(conn, "logs", paths)


if __name__ == "__main__":
    unittest.main()
