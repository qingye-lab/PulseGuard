from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import main, run_statistics, storage


class ConsoleReadPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        db_patch = patch.object(storage, "DB_PATH", Path(directory.name) / "console.db")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        storage.init_db()
        self.project_id = storage.default_project_id()
        self.other_id = int(storage.create_project("Other project")["id"])
        self.started = datetime.now().astimezone().replace(microsecond=0)
        with storage._connect() as conn:
            conn.executemany(
                """INSERT INTO runs (
                    project_id, check_id, check_name, check_type, status, started_at,
                    finished_at, duration_ms, observation_kind, affects_health, created_at,
                    runner_id, failure_kind, logs
                ) VALUES (?, 1, 'Read performance', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (project, kind, status, (self.started - timedelta(minutes=i)).isoformat(),
                     self.started.isoformat(), i + 10, observation, health, self.started.isoformat(),
                     f"runner-{i % 2}", failure, "x" * 8192)
                    for i, (project, kind, status, observation, health, failure) in enumerate([
                        (self.project_id, "api", "ok", "observation", 1, "none"),
                        (self.other_id, "api", "ok", "observation", 1, "none"),
                        (self.project_id, "ui", "failed", "observation", 1, "target"),
                        (self.project_id, "ui", "timeout", "observation", 1, "target"),
                        (self.project_id, "api", "ok", "draft", 0, "none"),
                        (self.project_id, "api", "failed", "observation", 0, "runner"),
                    ])
                ],
            )

    @contextmanager
    def traced(self, callback):
        original = storage._connect

        @contextmanager
        def connect(**kwargs):
            with original(**kwargs) as conn:
                conn.set_trace_callback(callback)
                yield conn

        with patch.object(storage, "_connect", connect):
            yield

    def plan(self, sql: str, params: list | tuple = ()) -> str:
        with storage._connect(read_only=True) as conn:
            return " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))

    def direct_ids(self, filters: dict, limit: int = 100) -> list[int]:
        clauses, params = storage._run_filter_clauses(filters)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with storage._connect(read_only=True) as conn:
            return [
                int(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM runs{where} ORDER BY started_at DESC, id DESC LIMIT ?",
                    [*params, limit],
                )
            ]

    def test_existing_database_backfills_projection_without_changing_runs(self) -> None:
        with storage._connect() as conn:
            for name in storage._RUN_RECORD_TRIGGERS:
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute("DROP TABLE run_records")
            conn.execute("DELETE FROM schema_versions WHERE version=?", (storage.RUN_RECORDS_SCHEMA_VERSION,))
            before = [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")]
        storage.init_db()
        with storage._connect(read_only=True) as conn:
            self.assertEqual(before, [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")])
            projected = [tuple(row) for row in conn.execute(
                f"SELECT {', '.join(storage._RUN_RECORD_COLUMNS)} FROM run_records ORDER BY id"
            )]
            source = [tuple(row) for row in conn.execute(
                f"SELECT {', '.join(storage._RUN_RECORD_COLUMNS)} FROM runs ORDER BY id"
            )]
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger', 'index')"
            )}
        self.assertEqual(projected, source)
        self.assertIn("run_records", names)
        for name in (*storage._RUN_RECORD_TRIGGERS, "idx_run_records_project_kind_started"):
            self.assertIn(name, names)
        statements: list[str] = []
        with self.traced(statements.append):
            storage.init_db()
        self.assertFalse(any("INSERT INTO run_records" in sql and "SELECT" in sql for sql in statements))

    def test_tab_filters_count_and_page_only_from_the_projection(self) -> None:
        cases = (
            ({"check_type": "api"}, 2, "idx_run_records_project_kind_type_started"),
            ({"status": "failed"}, 3, "idx_run_records_project_kind_failures_started"),
            ({"check_type": "ui", "status": "failed"}, 2, "idx_run_records_project_kind_failures_started"),
            ({"check_type": "api", "status": "failed"}, 1, "idx_run_records_project_kind_failures_started"),
        )
        for extra, expected_total, index_name in cases:
            filters = {"project_id": self.project_id, "observation_kind": "observation", **extra}
            clauses, params = storage._run_filter_clauses(filters)
            where = " WHERE " + " AND ".join(clauses)
            count_sql = f"SELECT COUNT(*) FROM {storage._run_record_count_source(filters)}{where}"
            page_sql = (
                f"SELECT id FROM {storage._run_record_page_source(filters)}{where} "
                "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            count_plan = self.plan(count_sql, params)
            page_plan = self.plan(page_sql, [*params, 2, 0])
            self.assertIn(index_name, count_plan, filters)
            self.assertIn(index_name, page_plan, filters)
            self.assertNotIn("SEARCH runs", count_plan)
            self.assertNotIn("SCAN runs", count_plan)
            self.assertNotIn("SEARCH runs", page_plan)
            self.assertNotIn("SCAN runs", page_plan)
            self.assertNotIn("TEMP B-TREE", page_plan)
            self.assertEqual(storage.list_runs_page(filters, page=1, page_size=2)["total"], expected_total)

    def test_sparse_tabs_page_ids_before_reading_wide_rows_with_or_without_kind(self) -> None:
        with storage._connect() as conn:
            conn.executemany(
                """INSERT INTO runs(project_id, check_id, check_name, check_type, status,
                    started_at, created_at, observation_kind, logs)
                    VALUES (?, 1, 'Recent success', 'api', 'ok', ?, ?, 'observation', ?)""",
                [(self.project_id, (self.started + timedelta(seconds=i + 1)).isoformat(),
                  self.started.isoformat(), "x" * 8192) for i in range(1000)],
            )
        for kind in ("observation", ""):
            for extra in ({"status": "failed"}, {"check_type": "ui"},
                          {"check_type": "ui", "status": "failed"}):
                filters = {"project_id": self.project_id, "observation_kind": kind, **extra}
                with self.subTest(filters=filters):
                    clauses, params = storage._run_filter_clauses(filters)
                    where = " WHERE " + " AND ".join(clauses)
                    sql = (
                        f"SELECT id FROM {storage._run_record_page_source(filters)}{where} "
                        "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?"
                    )
                    plan = self.plan(sql, [*params, 2, 0])
                    self.assertNotIn("SEARCH runs", plan)
                    self.assertNotIn("SCAN runs", plan)
                    self.assertNotIn("TEMP B-TREE", plan)
                    self.assertIn("run_records", plan)
                    expected = self.direct_ids(filters)
                    statements: list[str] = []
                    with self.traced(statements.append):
                        first = storage.list_runs_page(filters, page=1, page_size=2)
                        second = storage.list_runs_page(filters, page=2, page_size=2)
                    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
                    self.assertTrue(any("FROM run_records" in sql for sql in selects))
                    self.assertTrue(any("FROM runs WHERE id IN" in sql for sql in selects))
                    self.assertEqual([row["id"] for row in first["items"] + second["items"]], expected[:4])

    def test_noncovered_filters_keep_their_full_semantics(self) -> None:
        filters = {"project_id": self.project_id, "status": "failed", "runner_id": "runner-1"}
        expected = self.direct_ids(filters)
        page = storage.list_runs_page(filters)
        self.assertEqual([row["id"] for row in page["items"]], expected)
        self.assertEqual(page["total"], len(expected))

    def test_pagination_and_filters_keep_exact_results_and_order_without_sorting(self) -> None:
        filters = {"project_id": self.project_id, "observation_kind": "observation"}
        expected = self.direct_ids(filters)
        first = storage.list_runs_page(filters, page=1, page_size=2)
        second = storage.list_runs_page(filters, page=2, page_size=2)
        self.assertEqual(first["total"], len(expected))
        self.assertEqual(second["total"], len(expected))
        self.assertEqual([row["id"] for row in first["items"] + second["items"]], expected[:4])
        narrowed = storage.list_runs_page({**filters, "check_type": "ui", "status": "failed"})
        self.assertEqual(narrowed["total"], 2)
        self.assertEqual({row["status"] for row in narrowed["items"]}, {"failed", "timeout"})
        statements: list[str] = []
        with self.traced(statements.append):
            storage.list_runs_page(filters, page=2, page_size=2)
        select = next(sql for sql in statements if "ORDER BY started_at DESC, id DESC LIMIT" in sql)
        self.assertIn("FROM run_records", select)
        plan = self.plan(select)
        self.assertIn("idx_run_records_project_kind_started", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_keyword_count_scans_the_narrow_table_instead_of_random_row_lookups(self) -> None:
        filters = {"project_id": self.project_id, "observation_kind": "observation", "q": "Read performance"}
        statements: list[str] = []
        with self.traced(statements.append):
            page = storage.list_runs_page(filters, page_size=2)
        count_sql = next(sql for sql in statements if sql.lstrip().upper().startswith("SELECT COUNT"))
        self.assertIn("NOT INDEXED", count_sql)
        count_plan = self.plan(count_sql)
        self.assertIn("SCAN run_records", count_plan)
        self.assertNotIn("SEARCH runs", count_plan)
        page_sql = next(sql for sql in statements if "ORDER BY started_at DESC, id DESC LIMIT" in sql)
        self.assertIn("idx_run_records_project_kind_started", self.plan(page_sql))
        self.assertEqual(page["total"], len(self.direct_ids(filters)))
        self.assertEqual([row["id"] for row in page["items"]], self.direct_ids(filters)[:2])

    def test_statistics_read_the_projection_not_wide_history(self) -> None:
        statements: list[str] = []
        with self.traced(statements.append):
            stats = run_statistics.get_run_statistics(
                self.project_id,
                (self.started - timedelta(hours=1)).isoformat(),
                (self.started + timedelta(hours=1)).isoformat(),
                observation_kind="observation",
            )
        retained = next(sql for sql in statements if "substr(started_at, 1, 10)" in sql)
        oldest = next(sql for sql in statements if "MIN(started_at)" in sql)
        self.assertIn("FROM run_records", retained)
        self.assertIn("FROM run_records", oldest)
        self.assertNotIn("FROM runs", retained)
        self.assertNotIn("FROM runs", oldest)
        self.assertEqual(stats["total"], 4)

    def test_overview_scan_is_covering_and_summary_does_not_scan_trends(self) -> None:
        statements: list[str] = []
        with storage._connect(read_only=True) as conn:
            conn.set_trace_callback(statements.append)
            trends = storage._overview_trends(conn, now=self.started, project_id=self.project_id)
            conn.set_trace_callback(None)
            select = next(sql for sql in statements if "FROM runs" in sql)
            plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + select))
        self.assertIn("USING COVERING INDEX", plan)
        self.assertIn("project_id=? AND affects_health=?", plan)
        series = {item["check_type"]: item for item in trends[0]["series"]}
        self.assertEqual(series["api"]["runs"], 1)
        self.assertEqual(series["ui"]["failure_count"], 2)
        with patch.object(storage, "_overview_trends", side_effect=AssertionError("unneeded trend scan")):
            summary = storage.get_status_summary(self.project_id)
            response = TestClient(main.app).get(
                "/api/status-page", headers={"X-PulseGuard-Project-Id": str(self.project_id)}
            )
        self.assertEqual(summary["today_runs"], 3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["runs_today"], 3)
        self.assertNotIn("overview_trends", response.headers["server-timing"])

    def test_maintenance_is_lightweight_and_uses_the_same_sanitized_payload(self) -> None:
        settings = {
            "maintenance_enabled": True,
            "maintenance_title": "Pause token-secret-123",
            "maintenance_message": "Contact token-secret-123",
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True}
            ],
            "notification_channels": [{"webhook": "https://private.example"}],
        }
        with patch.object(storage, "get_settings", return_value=settings), patch.object(
            storage, "get_status_summary", side_effect=AssertionError("unneeded history query")
        ), patch.object(storage, "get_overview", side_effect=AssertionError("unneeded overview")):
            response = TestClient(main.app).get("/api/maintenance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), main._maintenance_payload(settings))
        self.assertEqual(set(response.json()), {"enabled", "title", "message", "starts_at", "ends_at"})
        self.assertNotIn("token-secret-123", response.text)
        self.assertNotIn("private.example", response.text)

    def test_pagination_response_reports_server_and_query_phases(self) -> None:
        response = TestClient(main.app).get(
            "/api/runs-page?observation_kind=observation&page_size=2",
            headers={"X-PulseGuard-Project-Id": str(self.project_id)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 4)
        for name in ["dispatch", "db_gate", "db_connect", "runs_count", "runs_fetch", "server"]:
            self.assertIn(f"{name};dur=", response.headers["server-timing"])
        self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
