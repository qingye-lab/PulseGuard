from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.app import storage


INSERT = """INSERT INTO runs(
    project_id, check_id, check_name, check_type, status, started_at, created_at,
    observation_kind, trigger, notification_status, runner_id, run_group_id,
    deduplicated_count, affects_health, logs)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


class RunRecordProjectionTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.db = root / "records.db"
        self.enterContext(patch.object(storage, "DB_PATH", self.db))
        self.enterContext(patch.object(storage, "BACKUPS_DIR", root / "backups"))
        self.enterContext(patch.object(storage, "ensure_runtime_dirs"))
        storage.init_db()
        self.project = storage.default_project_id()
        self.other = int(storage.create_project("Another project")["id"])
        self.started = datetime.now().astimezone().replace(microsecond=0)
        rows = [
            (self.project, 1, "Login flow", "api", "ok", "observation", "scheduled", "sent", "runner-a", "g1", 0, 1, 10),
            (self.project, 1, "Login flow", "api", "failed", "observation", "scheduled", "failed", "runner-a", "g1", 0, 1, 9),
            (self.project, 2, "Checkout UI", "ui", "timeout", "observation", "manual", "pending", "runner-b", "g1", 0, 1, 8),
            (self.project, 2, "Checkout UI", "ui", "ok", "observation", "manual", "not_required", "runner-b", "", 0, 1, 7),
            (self.project, 3, "API sweep", "api", "skipped", "verification", "manual", "disabled", "runner-c", "g2", 2, 0, 6),
            (self.project, 0, "Ad hoc", "api", "ok", "observation", "scheduled", "not_required", "local", "", 0, 1, 5),
            (self.project, 1, "Login flow", "api", "failed", "observation", "scheduled", "sent", "runner-a", "g1", 0, 1, 4),
            (self.project, 4, "Nightly UI", "ui", "failed", "observation", "scheduled", "failed", "runner-b", "g2", 0, 1, 3),
            (self.project, 5, "Login legacy", "api", "ok", "observation", "legacy", "not_required", "local", "", 5, 1, 2),
            (self.project, 1, "Login flow", "api", "running", "observation", "scheduled", None, "local", "", 0, 1, 1),
            (self.project, 2, "Checkout UI", "ui", "preparation_failed", "observation", "scheduled", "failed", "runner-b", "g2", 0, 0, 0),
            (self.other, 9, "Other Login", "api", "ok", "observation", "scheduled", "sent", "runner-x", "", 0, 1, 11),
        ]
        with storage._connect() as conn:
            conn.executemany(
                INSERT,
                [
                    (project, check_id, name, kind, status, stamp, stamp, observation, trigger,
                     notification, runner, group, merged, health, "x" * 1024)
                    for project, check_id, name, kind, status, observation, trigger, notification,
                    runner, group, merged, health, minutes in rows
                    for stamp in [(self.started - timedelta(minutes=minutes)).isoformat()]
                ],
            )

    def assert_projection_matches_runs(self):
        columns = ", ".join(storage._RUN_RECORD_COLUMNS)
        with storage._connect(read_only=True) as conn:
            runs = [tuple(row) for row in conn.execute(f"SELECT {columns} FROM runs ORDER BY id")]
            records = [tuple(row) for row in conn.execute(
                f"SELECT {columns} FROM run_records ORDER BY id")]
            orphans = conn.execute(
                "SELECT COUNT(*) FROM run_records WHERE id NOT IN (SELECT id FROM runs)"
            ).fetchone()[0]
        self.assertEqual(records, runs)
        self.assertEqual(orphans, 0)

    def expected_page(self, filters: dict, page: int = 1, page_size: int = 100):
        clauses, params = storage._run_filter_clauses(filters)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with storage._connect(read_only=True) as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM runs{where}", params).fetchone()[0])
            ids = [
                int(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM runs{where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                    [*params, page_size, (page - 1) * page_size],
                )
            ]
            rows = storage._load_runs_by_ids(conn, ids)
        return total, [storage._normalize_run(row) for row in rows]

    def test_backfill_is_batched_and_preserves_source_rows(self):
        with storage._connect() as conn:
            for name in storage._RUN_RECORD_TRIGGERS:
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("DROP TABLE run_records")
            conn.execute("DELETE FROM schema_versions WHERE version=?", (storage.RUN_RECORDS_SCHEMA_VERSION,))
            before = [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")]
        statements: list[str] = []
        original_connect = storage._connect

        from contextlib import contextmanager

        @contextmanager
        def traced(**kwargs):
            with original_connect(**kwargs) as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(storage, "_RUN_RECORD_BACKFILL_BATCH_SIZE", 2), patch.object(
            storage, "_connect", traced
        ):
            storage.init_db(recover_interrupted=False)
        backfills = [
            sql for sql in statements
            if sql.lstrip().startswith("INSERT INTO run_records") and "SELECT" in sql
        ]
        self.assertGreaterEqual(len(backfills), 6)
        with storage._connect(read_only=True) as conn:
            self.assertEqual(before, [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")])
        self.assert_projection_matches_runs()
        statements.clear()
        with patch.object(storage, "_connect", traced):
            storage.init_db()
        self.assertFalse(any(
            sql.lstrip().startswith("INSERT INTO run_records") and "SELECT" in sql
            for sql in statements
        ))

    def test_triggers_sync_projected_columns_and_delete_without_detail_rewrites(self):
        with sqlite3.connect(self.db) as conn:
            cursor = conn.execute(
                INSERT,
                (self.project, 1, "Trigger row", "api", "ok",
                 self.started.isoformat(), self.started.isoformat(), "observation",
                 "scheduled", "sent", "runner-a", "g1", 0, 1, "initial"),
            )
            run_id = int(cursor.lastrowid)
        self.assert_projection_matches_runs()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """UPDATE runs SET project_id=?, check_id=?, check_name=?, check_type=?, status=?,
                   started_at=?, notification_status=?, runner_id=?, run_group_id=?, trigger=?,
                   observation_kind=?, deduplicated_count=? WHERE id=?""",
                (self.other, 7, "Renamed", "ui", "timeout", self.started.isoformat(),
                 "failed", "runner-z", "g9", "manual", "verification", 4, run_id),
            )
            conn.commit()
        self.assert_projection_matches_runs()
        projected = None
        with storage._connect(read_only=True) as conn:
            projected = tuple(conn.execute(
                f"SELECT {', '.join(storage._RUN_RECORD_COLUMNS)} FROM run_records WHERE id=?",
                (run_id,),
            ).fetchone())
        with sqlite3.connect(self.db) as conn:
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            conn.execute(
                """UPDATE runs SET logs='changed', error_message='changed', error_stack='changed',
                   response_snapshot='changed', request_snapshot='changed', affects_health=0,
                   failure_kind='runner', failure_fingerprint='changed', duration_ms=99,
                   finished_at=?, runner_name='changed', runner_address='changed',
                   runner_region='changed', runner_browser_version='changed', browser_type='changed',
                   notification_channel='changed', notification_error='changed',
                   notification_sent_at=?, trend_recorded_at=?, details_pruned_at=? WHERE id=?""",
                (self.started.isoformat(), self.started.isoformat(),
                 self.started.isoformat(), self.started.isoformat(), run_id),
            )
            conn.commit()
            self.assertFalse(any("run_records" in sql for sql in statements if sql.lstrip().startswith("UPDATE")))
        with storage._connect(read_only=True) as conn:
            self.assertEqual(projected, tuple(conn.execute(
                f"SELECT {', '.join(storage._RUN_RECORD_COLUMNS)} FROM run_records WHERE id=?",
                (run_id,),
            ).fetchone()))
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            conn.commit()
        with storage._connect(read_only=True) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM run_records WHERE id=?", (run_id,)).fetchone())
        self.assert_projection_matches_runs()

    def test_scope_changes_and_rollback_keep_projection_exact(self):
        with storage._connect() as conn:
            before = [tuple(row) for row in conn.execute(
                f"SELECT {', '.join(storage._RUN_RECORD_COLUMNS)} FROM run_records ORDER BY id")]
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with storage._connect() as conn:
                conn.execute("UPDATE runs SET project_id=?, observation_kind='verification' WHERE project_id=?",
                             (self.other, self.project))
                conn.execute("DELETE FROM runs WHERE status='timeout'")
                conn.execute(INSERT, (self.other, 8, "Rolled back", "api", "ok",
                                      self.started.isoformat(), self.started.isoformat(), "observation",
                                      "manual", "sent", "", "", 0, 1, None))
                raise RuntimeError("rollback")
        with storage._connect(read_only=True) as conn:
            after = [tuple(row) for row in conn.execute(
                f"SELECT {', '.join(storage._RUN_RECORD_COLUMNS)} FROM run_records ORDER BY id")]
        self.assertEqual(after, before)
        self.assert_projection_matches_runs()
        with storage._connect() as conn:
            conn.execute("UPDATE runs SET project_id=?", (self.other,))
        self.assert_projection_matches_runs()
        self.assertEqual(storage.list_runs_page({"project_id": self.project})["total"], 0)
        self.assertEqual(storage.list_runs_page({"project_id": self.other})["total"], 12)
        with storage._connect() as conn:
            conn.execute("DELETE FROM runs")
        self.assert_projection_matches_runs()
        self.assertEqual(storage.list_runs_page()["total"], 0)

    def test_missing_trigger_rebuilds_projection_before_reads(self):
        with storage._connect(read_only=True) as conn:
            run_id = int(conn.execute(
                "SELECT id FROM runs WHERE status='ok' ORDER BY id LIMIT 1"
            ).fetchone()[0])
            before = conn.execute(
                "SELECT notification_status FROM run_records WHERE id=?", (run_id,)
            ).fetchone()[0]
        with storage._connect() as conn:
            conn.execute("DROP TRIGGER trg_runs_record_update")
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE runs SET notification_status='failed' WHERE id=?", (run_id,))
            conn.commit()
        with storage._connect(read_only=True) as conn:
            stale = conn.execute(
                "SELECT notification_status FROM run_records WHERE id=?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(stale, before)
        self.assertNotEqual(stale, "failed")
        storage.init_db()
        self.assert_projection_matches_runs()
        with storage._connect(read_only=True) as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_runs_record_update'"
            ).fetchone())
        with storage._connect() as conn:
            conn.execute("UPDATE runs SET notification_status='failed' WHERE status='ok'")
        self.assert_projection_matches_runs()

    def test_page_filter_and_count_parity_with_runs_for_every_filter(self):
        self.assert_projection_matches_runs()
        filters = [
            {},
            {"project_id": self.project},
            {"observation_kind": "observation"},
            {"project_id": self.project, "observation_kind": "observation"},
            {"project_id": self.project, "observation_kind": "verification"},
            {"project_id": self.project, "observation_kind": "observation", "status": "failed"},
            {"project_id": self.project, "observation_kind": "observation", "status": "timeout"},
            {"project_id": self.project, "observation_kind": "observation", "status": "running"},
            {"project_id": self.project, "observation_kind": "observation", "status": "preparation_failed"},
            {"project_id": self.project, "observation_kind": "observation", "check_type": "ui"},
            {"project_id": self.project, "observation_kind": "observation", "notification_status": "failed"},
            {"project_id": self.project, "observation_kind": "observation", "runner_id": "runner-a"},
            {"project_id": self.project, "observation_kind": "observation", "run_group_id": "g1"},
            {"project_id": self.project, "observation_kind": "observation", "trigger": "legacy"},
            {"project_id": self.project, "observation_kind": "observation", "q": "Login"},
            {"project_id": self.project, "observation_kind": "observation", "check_id": 1},
            {
                "project_id": self.project,
                "observation_kind": "observation",
                "start": (self.started - timedelta(minutes=7, seconds=30)).isoformat(),
                "end": (self.started - timedelta(minutes=2, seconds=30)).isoformat(),
            },
            {
                "project_id": self.project,
                "observation_kind": "observation",
                "check_type": "ui",
                "status": "failed",
            },
            {
                "project_id": self.project,
                "observation_kind": "observation",
                "q": "Login",
                "status": "failed",
            },
            {"project_id": self.project, "q": "Login"},
            {"q": "Login"},
        ]
        for case in filters:
            with self.subTest(filters=case):
                total, items = self.expected_page(case)
                page = storage.list_runs_page(case, page=1, page_size=100)
                self.assertEqual(page["total"], total)
                self.assertEqual(page["items"], items)

    def test_paginated_projection_items_keep_run_order(self):
        filters = {"project_id": self.project, "observation_kind": "observation"}
        expected_total, expected = self.expected_page(filters)
        collected = []
        for page_number in (1, 2, 3):
            page = storage.list_runs_page(filters, page=page_number, page_size=3)
            self.assertEqual(page["total"], expected_total)
            collected.extend(page["items"])
        self.assertEqual(collected, expected[:9])

    def test_history_cleanup_removes_projection_rows(self):
        old_stamp = (datetime.now().astimezone() - timedelta(days=3)).isoformat()
        with storage._connect() as conn:
            ids = []
            for index in range(2):
                cursor = conn.execute(
                    INSERT,
                    (self.project, 6, f"Expired {index}", "api", "ok", old_stamp, old_stamp,
                     "observation", "scheduled", "sent", "runner-a", "", 0, 1, None),
                )
                ids.append(int(cursor.lastrowid))
        with patch.object(storage, "cleanup_old_artifacts", return_value={}):
            removed = storage.cleanup_old_data({"run_retention_days": 1})
        self.assertEqual(removed, 2)
        self.assert_projection_matches_runs()
        with storage._connect(read_only=True) as conn:
            remaining = {int(row[0]) for row in conn.execute("SELECT id FROM run_records WHERE id IN (?, ?)", ids)}
        self.assertEqual(remaining, set())


if __name__ == "__main__":
    unittest.main()
