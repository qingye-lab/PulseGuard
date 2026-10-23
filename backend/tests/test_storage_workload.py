from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import relay_server, storage


class StorageWorkloadTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.enterContext(patch.object(storage, "DB_PATH", root / "workload.db"))
        storage.init_db()
        self.project_id = storage.default_project_id()

    def seed(self, conn: sqlite3.Connection, count: int, status: str = "ok") -> list[int]:
        timestamp = "2026-01-01T00:00:00+08:00"
        ids = []
        for _ in range(count):
            ids.append(int(conn.execute(
                """INSERT INTO runs(project_id, check_id, check_name, check_type, status,
                       started_at, created_at, duration_ms, logs)
                   VALUES (?, 1, 'Workload', 'api', ?, ?, ?, 12, ?)""",
                (self.project_id, status, timestamp, timestamp, "x" * 8192),
            ).lastrowid))
        return ids

    def test_startup_recovery_avoids_wide_history_scan_and_is_idempotent(self) -> None:
        with storage._connect() as conn:
            completed = self.seed(conn, 1200)
            active = self.seed(conn, 1, "pending") + self.seed(conn, 1, "running")
            before = [tuple(r) for r in conn.execute("SELECT * FROM runs WHERE status='ok'")]
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            storage._mark_interrupted_runs(conn)
            conn.set_trace_callback(None)
            updates = [s for s in statements if "UPDATE runs" in s]
            self.assertTrue(updates)
            for sql in updates:
                plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
                self.assertNotIn("SCAN runs", plan)
            self.assertEqual(before, [tuple(r) for r in conn.execute("SELECT * FROM runs WHERE status='ok'")])
            rows = list(conn.execute("SELECT status, finished_at, failure_kind FROM runs WHERE status='skipped'"))
            self.assertEqual(len(rows), len(active))
            self.assertTrue(all(r["finished_at"] and r["failure_kind"] == "runner" for r in rows))
            once = [tuple(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]
            storage._mark_interrupted_runs(conn)
            self.assertEqual(once, [tuple(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")])
            self.assertEqual(len(completed), 1200)

    def test_deployment_discard_handles_more_runs_than_sqlite_bind_limit(self) -> None:
        with storage._connect() as conn:
            completed = self.seed(conn, 2)
            active = self.seed(conn, 1001, "pending")
            check_id = storage.list_checks()[0]["id"]
            conn.execute(
                "INSERT INTO check_status(check_id, current_status, last_run_id, last_scheduled_run_id, updated_at) "
                "VALUES (?, 'unknown', ?, ?, '')",
                (check_id, active[0], active[-1]),
            )
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
            self.assertEqual(storage._discard_incomplete_runs(conn), len(active))
            self.assertEqual([r[0] for r in conn.execute("SELECT id FROM runs ORDER BY id")], completed)
            row = conn.execute("SELECT last_run_id, last_scheduled_run_id FROM check_status WHERE check_id=?", (check_id,)).fetchone()
            self.assertEqual(tuple(row), (None, None))
            self.assertEqual(storage._discard_incomplete_runs(conn), 0)

    def test_artifact_reconciliation_scans_history_once_for_many_paths(self) -> None:
        with storage._connect() as conn:
            ids = self.seed(conn, 1003)
            paths = [f"responses/legacy-{i}.json" for i in ids]
            conn.executemany("UPDATE runs SET response_path=? WHERE id=?", zip(paths, ids))
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
            storage._clear_artifact_paths(conn, "response_path", paths[:-1] + paths[:1])
            conn.set_trace_callback(None)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs WHERE response_path IS NOT NULL").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT response_path FROM runs WHERE id=?", (ids[-1],)).fetchone()[0], paths[-1])
            updates = [s for s in statements if "UPDATE runs" in s]
            self.assertEqual(len(updates), 1, "path batches must not each rescan the wide runs table")
            storage._clear_artifact_paths(conn, "response_path", paths[-1:])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs WHERE response_path IS NOT NULL").fetchone()[0], 0)

    def test_trend_backfill_reads_only_scalar_columns_in_bounded_batches(self) -> None:
        with storage._connect() as conn:
            ids = self.seed(conn, 1001)
            recorded: list[int] = []
            batch_sizes: list[int] = []

            class BoundedCursor:
                def __init__(self, cursor):
                    self.cursor = cursor

                def fetchall(self):
                    raise AssertionError("historical trend backfill must not load all rows")

                def fetchmany(self, size):
                    rows = self.cursor.fetchmany(size)
                    batch_sizes.append(len(rows))
                    return rows

                def close(self):
                    self.cursor.close()

            class Connection:
                def execute(self, sql, params=()):
                    cursor = conn.execute(sql, params)
                    return BoundedCursor(cursor) if "FROM runs" in sql else cursor

            def record(_conn, row):
                self.assertNotIn("logs", row.keys())
                self.assertNotIn("response_snapshot", row.keys())
                recorded.append(row["id"])

            with patch.object(storage, "_record_trend_rollups", side_effect=record):
                storage._backfill_trend_rollups(Connection())
                storage._backfill_trend_rollups(Connection())
            self.assertEqual(recorded, ids)
            self.assertLessEqual(max(batch_sizes), storage._CLEANUP_BATCH_SIZE)

    def test_artifact_lookup_is_removed_after_failure_and_can_be_retried(self) -> None:
        with storage._connect() as conn:
            run_id = self.seed(conn, 1)[0]
            conn.execute("UPDATE runs SET trace_path='traces/old.zip' WHERE id=?", (run_id,))

            class FailingConnection:
                def execute(self, sql, params=()):
                    if "UPDATE runs" in sql:
                        raise sqlite3.OperationalError("injected update failure")
                    return conn.execute(sql, params)

                def executemany(self, sql, params):
                    return conn.executemany(sql, params)

            with self.assertRaisesRegex(sqlite3.OperationalError, "injected update failure"):
                storage._clear_artifact_paths(FailingConnection(), "trace_path", ["traces/old.zip"])
            self.assertEqual(conn.execute("SELECT trace_path FROM runs WHERE id=?", (run_id,)).fetchone()[0], "traces/old.zip")
            storage._clear_artifact_paths(conn, "trace_path", ["traces/old.zip"])
            self.assertIsNone(conn.execute("SELECT trace_path FROM runs WHERE id=?", (run_id,)).fetchone()[0])

    def test_relay_restart_does_not_recover_tasks_owned_by_the_main_process(self) -> None:
        with storage._connect() as conn:
            ids = self.seed(conn, 1, "pending") + self.seed(conn, 1, "running")
            conn.execute(
                """INSERT INTO script_library_validations(project_id, candidate_revision, candidate_release_hash,
                   candidate_content_hash, candidate_modules_json, status, created_at, updated_at)
                   VALUES (?, 1, 'release', 'content', '[]', 'running', '', '')""", (self.project_id,),
            )
        with patch.object(relay_server, "ensure_relay_certificate"), patch.object(relay_server, "relay_control_token"):
            relay_server._prepare_runtime()
        with storage._connect(read_only=True) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT status FROM runs ORDER BY id")], ["pending", "running"])
            self.assertEqual(conn.execute("SELECT status FROM script_library_validations").fetchone()[0], "running")
        # The owning main process still settles both rows on its own restart.
        storage.init_db()
        with storage._connect(read_only=True) as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT status FROM runs ORDER BY id")], ["skipped"] * len(ids))
            self.assertEqual(conn.execute("SELECT status FROM script_library_validations").fetchone()[0], "cancelled")


if __name__ == "__main__":
    unittest.main()
