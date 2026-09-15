from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import main, storage


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

    def test_existing_database_gets_covering_indexes_without_changing_runs(self) -> None:
        with storage._connect() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_runs_project_kind_started")
            conn.execute("DROP INDEX IF EXISTS idx_runs_project_health_cover")
            before = [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")]
        storage.init_db()
        storage.init_db()
        with storage._connect(read_only=True) as conn:
            self.assertEqual(before, [tuple(row) for row in conn.execute("SELECT * FROM runs ORDER BY id")])
            count_plan = " ".join(str(row[3]) for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM runs WHERE project_id=? AND observation_kind=?",
                (self.project_id, "observation"),
            ))
        self.assertIn("USING COVERING INDEX", count_plan)
        self.assertIn("project_id=? AND observation_kind=?", count_plan)

    def test_pagination_and_filters_keep_exact_results_and_order_without_sorting(self) -> None:
        filters = {"project_id": self.project_id, "observation_kind": "observation"}
        expected = storage.list_runs(filters, limit=100, summary_only=True)
        first = storage.list_runs_page(filters, page=1, page_size=2)
        second = storage.list_runs_page(filters, page=2, page_size=2)
        self.assertEqual(first["total"], len(expected))
        self.assertEqual(second["total"], len(expected))
        self.assertEqual(first["items"] + second["items"], expected)
        narrowed = storage.list_runs_page({**filters, "check_type": "ui", "status": "failed"})
        self.assertEqual(narrowed["total"], 2)
        self.assertEqual({row["status"] for row in narrowed["items"]}, {"failed", "timeout"})
        statements: list[str] = []
        original_connect = storage._connect
        from contextlib import contextmanager

        @contextmanager
        def traced_connect(**kwargs):
            with original_connect(**kwargs) as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(storage, "_connect", traced_connect):
            storage.list_runs_page(filters, page=2, page_size=2)
        select = next(sql for sql in statements if "ORDER BY started_at DESC, id DESC LIMIT" in sql)
        with storage._connect(read_only=True) as conn:
            plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + select))
        self.assertIn("project_id=? AND observation_kind=?", plan)
        self.assertNotIn("TEMP B-TREE", plan)

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
