from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.main import app


class FakeScheduler:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_all(self) -> None:
        self.refresh_count += 1

    def sync_check(self, check_id: int) -> None:
        return None


class ProjectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        storage.init_db()

    def test_project_lifecycle_preserves_one_active_project_and_allows_only_empty_delete(self) -> None:
        default_id = storage.default_project_id()
        second = storage.create_project("  Payments  ")

        self.assertEqual(second["name"], "Payments")
        with self.assertRaisesRegex(ValueError, "项目名称已存在"):
            storage.create_project("payments")

        archived = storage.archive_project(int(second["id"]))
        self.assertEqual(archived["status"], "archived")
        with self.assertRaisesRegex(ValueError, "至少需要保留一个活跃项目"):
            storage.archive_project(default_id)

        restored = storage.restore_project(int(second["id"]))
        self.assertEqual(restored["status"], "active")
        self.assertTrue(storage.delete_empty_project(int(second["id"])))

        nonempty = storage.create_project("Nonempty")
        storage.create_check(api_check_payload("Owned"), project_id=int(nonempty["id"]))
        with self.assertRaisesRegex(ValueError, "空项目"):
            storage.delete_empty_project(int(nonempty["id"]))

    def test_checks_runs_and_scheduler_queries_are_project_scoped(self) -> None:
        first_id = storage.default_project_id()
        second_id = int(storage.create_project("Second")["id"])
        first = storage.create_check(api_check_payload("Same name"), project_id=first_id)
        second = storage.create_check(api_check_payload("Same name"), project_id=second_id)
        first_run = storage.create_run(first)
        second_run = storage.create_run(second)

        self.assertIsNone(storage.get_check(int(first["id"]), project_id=second_id))
        self.assertIsNone(storage.get_run(int(first_run["id"]), project_id=second_id))
        self.assertEqual(
            [run["id"] for run in storage.list_runs({"project_id": second_id})],
            [second_run["id"]],
        )

        storage.archive_project(second_id)
        active_checks = storage.list_checks(enabled_only=True, active_projects_only=True)
        self.assertNotIn(int(second["id"]), [int(check["id"]) for check in active_checks])
        self.assertIn(int(first["id"]), [int(check["id"]) for check in active_checks])

    def test_empty_project_with_read_only_token_cannot_be_deleted(self) -> None:
        project = storage.create_project("Token scoped")
        project_id = int(project["id"])
        token = storage.create_read_only_token("Token scoped", [project_id])

        with self.assertRaisesRegex(ValueError, "只读令牌"):
            storage.delete_empty_project(project_id)

        self.assertIsNotNone(storage.get_project(project_id))
        self.assertTrue(storage.validate_read_only_token(str(token["token"]), project_id))
        storage.delete_read_only_token(str(token["id"]))
        self.assertTrue(storage.delete_empty_project(project_id))


class ProjectApiBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        storage.init_db()
        self.scheduler = FakeScheduler()
        self.scheduler_patch = patch.object(app.state, "scheduler", self.scheduler, create=True)
        self.scheduler_patch.start()
        self.client = TestClient(app)
        self.first_id = storage.default_project_id()
        self.second_id = int(storage.create_project("Second")["id"])

    def tearDown(self) -> None:
        self.scheduler_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_required_header_cross_project_and_archived_write_boundary(self) -> None:
        owned = storage.create_check(api_check_payload("Owned"), project_id=self.first_id)

        missing = self.client.get("/api/checks")
        invalid = self.client.get("/api/checks", headers=project_headers(999999))
        cross = self.client.get(
            f"/api/checks/{owned['id']}", headers=project_headers(self.second_id)
        )
        storage.archive_project(self.second_id)
        archived_read = self.client.get("/api/checks", headers=project_headers(self.second_id))
        archived_write = self.client.post(
            "/api/checks", json=api_check_payload("Blocked"), headers=project_headers(self.second_id)
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(invalid.status_code, 404)
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(archived_read.status_code, 200)
        self.assertEqual(archived_write.status_code, 409)

    def test_read_only_token_cannot_cross_project_scope(self) -> None:
        token = storage.create_read_only_token("First only", [self.first_id])["token"]

        allowed = self.client.get(
            "/api/read-only/snapshot",
            headers={**project_headers(self.first_id), "Authorization": f"Bearer {token}"},
        )
        denied = self.client.get(
            "/api/read-only/snapshot",
            headers={**project_headers(self.second_id), "Authorization": f"Bearer {token}"},
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)

    def test_audit_route_includes_current_project_and_global_events_only(self) -> None:
        storage.record_audit_event("updated", "settings", summary="Global")
        storage.record_audit_event("updated", "check", 1, summary="First", project_id=self.first_id)
        storage.record_audit_event("updated", "check", 2, summary="Second", project_id=self.second_id)

        response = self.client.get("/api/audit-events", headers=project_headers(self.first_id))

        self.assertEqual(response.status_code, 200)
        summaries = [event["summary"] for event in response.json()]
        self.assertEqual(summaries, ["First", "Global"])

    def test_project_lifecycle_events_are_written_to_the_project_scope(self) -> None:
        created = self.client.post("/api/projects", json={"name": "Audited"})
        project_id = int(created.json()["id"])
        self.client.put(f"/api/projects/{project_id}", json={"name": "Audited renamed"})
        self.client.post(f"/api/projects/{project_id}/archive")
        self.client.post(f"/api/projects/{project_id}/restore")

        events = storage.list_audit_events(project_id=project_id)

        self.assertEqual(created.status_code, 200)
        self.assertEqual([event["action"] for event in events], ["restored", "archived", "renamed", "created"])


class ProjectMigrationTests(unittest.TestCase):
    def test_migration_is_idempotent_and_assigns_all_rows_alerts_system_channels_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            create_legacy_database(Path(storage.DB_PATH))

            preview = storage.migrate_projects()
            applied = storage.migrate_projects(apply=True)
            repeated = storage.migrate_projects(apply=True)

            self.assertTrue(preview["required"])
            self.assertFalse(preview["applied"])
            self.assertFalse(applied["required"])
            self.assertTrue(applied["applied"])
            self.assertFalse(repeated["applied"])

            with storage._connect() as conn:
                project_id = int(conn.execute("SELECT id FROM projects").fetchone()["id"])
                for table in storage.PROJECT_SCOPED_TABLES:
                    self.assertEqual(
                        conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)
                        ).fetchone()[0],
                        1,
                        table,
                    )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE project_id = ?", (project_id,)
                    ).fetchone()[0],
                    1,
                )
                remaining_legacy_keys = conn.execute(
                    "SELECT key FROM settings WHERE key IN ('alerts_enabled', 'notification_channels', 'alert_tag_policies')"
                ).fetchall()
                self.assertEqual(remaining_legacy_keys, [])

            alerts = storage.get_project_alert_settings(project_id)
            settings = storage.get_settings()
            self.assertTrue(alerts["alerts_enabled"])
            self.assertEqual(alerts["notification_channels"][0]["id"], "shared")
            self.assertEqual(
                [channel["id"] for channel in settings["system_notification_channels"]],
                ["shared", "project-only"],
            )
            self.assertEqual(settings["system_notification_channel_ids"], ["shared"])
            self.assertEqual(settings["system_notification_channels"][0]["webhook_url"], "https://example.test/hook")
            self.assertEqual(
                settings["system_notification_channels"][1]["dingtalk_secret"],
                "SEC-project-only",
            )
            self.assertEqual(settings["read_only_tokens"][0]["project_ids"], [project_id])

    def test_failed_migration_rolls_back_schema_changes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            create_legacy_database(Path(storage.DB_PATH))
            with patch.object(storage, "_validate_project_migration", side_effect=RuntimeError("forced")):
                with self.assertRaisesRegex(RuntimeError, "forced"):
                    storage.migrate_projects(apply=True)

            with sqlite3.connect(storage.DB_PATH) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                }
                self.assertNotIn("projects", tables)
                self.assertNotIn("project_id", {row[1] for row in conn.execute("PRAGMA table_info(checks)")})


def project_headers(project_id: int) -> dict[str, str]:
    return {"X-PulseGuard-Project-Id": str(project_id)}


def api_check_payload(name: str) -> dict[str, object]:
    return {
        "name": name,
        "type": "api",
        "enabled": True,
        "interval_seconds": 120,
        "timeout_ms": 10000,
        "entry_url": "https://example.test/health",
        "viewport_mode": "web",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": "[]",
        "setup_script": "",
        "script": "",
        "tags": "project",
        "alert_policy_json": "{}",
    }


def create_legacy_database(path: Path) -> None:
    timestamp = "2026-09-01T00:00:00+08:00"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE checks (id INTEGER PRIMARY KEY, name TEXT, type TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE runs (id INTEGER PRIMARY KEY, check_id INTEGER, started_at TEXT);
            CREATE TABLE check_versions (id INTEGER PRIMARY KEY, check_id INTEGER, created_at TEXT);
            CREATE TABLE anomaly_cycles (id INTEGER PRIMARY KEY, check_id INTEGER, status TEXT, opened_at TEXT);
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY, action TEXT, entity_type TEXT, entity_id TEXT,
                entity_name TEXT, summary TEXT, payload_json TEXT, created_at TEXT
            );
            CREATE TABLE run_archives (
                id INTEGER PRIMARY KEY, archive_date TEXT, check_type TEXT, status TEXT,
                run_count INTEGER, duration_sum_ms INTEGER, duration_sample_count INTEGER,
                last_run_at TEXT, updated_at TEXT, UNIQUE(archive_date, check_type, status)
            );
            CREATE TABLE trend_rollups (
                id INTEGER PRIMARY KEY, check_id INTEGER, check_type TEXT,
                bucket_granularity TEXT, bucket_start TEXT, success_count INTEGER,
                failure_count INTEGER, duration_sum_ms INTEGER, avg_duration_ms INTEGER,
                p95_duration_ms INTEGER, p99_duration_ms INTEGER,
                latency_histogram_json TEXT, duration_samples_json TEXT, updated_at TEXT,
                UNIQUE(check_id, bucket_granularity, bucket_start)
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            """
        )
        conn.execute("INSERT INTO checks VALUES (1, 'Legacy', 'api', ?, ?)", (timestamp, timestamp))
        conn.execute("INSERT INTO runs VALUES (1, 1, ?)", (timestamp,))
        conn.execute("INSERT INTO check_versions VALUES (1, 1, ?)", (timestamp,))
        conn.execute("INSERT INTO anomaly_cycles VALUES (1, 1, 'open', ?)", (timestamp,))
        conn.execute(
            "INSERT INTO audit_events VALUES (1, 'created', 'check', '1', 'Legacy', '', '{}', ?)",
            (timestamp,),
        )
        conn.execute(
            "INSERT INTO run_archives VALUES (1, '2026-09-01', 'api', 'ok', 1, 100, 1, ?, ?)",
            (timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO trend_rollups VALUES (1, 1, 'api', '1h', ?, 1, 0, 100, 100, 100, 100, '{}', '[100]', ?)",
            (timestamp, timestamp),
        )
        legacy_settings = {
            "alerts_enabled": True,
            "notification_channels": [
                {
                    "id": "shared",
                    "name": "Shared",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://example.test/hook",
                },
                {
                    "id": "project-only",
                    "name": "Project only",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://example.test/project-only",
                    "dingtalk_secret": "SEC-project-only",
                }
            ],
            "execution_notification_channel_ids": ["shared"],
            "alert_tag_policies": [],
            "alert_cooldown_minutes": 15,
            "recovery_notification": True,
            "system_notification_channel_ids": ["shared"],
            "read_only_tokens": [
                {"id": "legacy-token", "name": "Legacy", "token": "secret", "created_at": timestamp}
            ],
        }
        for key, value in legacy_settings.items():
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), timestamp),
            )
        conn.execute("INSERT INTO schema_versions VALUES (1, ?)", (timestamp,))


if __name__ == "__main__":
    unittest.main()
