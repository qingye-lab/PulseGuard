from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app import artifacts, storage


class OverviewStorageTests(unittest.TestCase):
    def test_database_health_reports_reclaimable_space_without_mutating_database(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            before = Path(storage.DB_PATH).read_bytes()

            health = storage.get_database_health()

            after = Path(storage.DB_PATH).read_bytes()

        self.assertEqual(health["status"], "normal")
        self.assertEqual(health["journal_mode"], "wal")
        self.assertGreater(health["page_size_bytes"], 0)
        self.assertGreater(health["page_count"], 0)
        self.assertGreaterEqual(health["freelist_pages"], 0)
        self.assertGreaterEqual(health["estimated_used_bytes"], 0)
        self.assertGreaterEqual(health["reclaimable_bytes"], 0)
        self.assertFalse(health["maintenance_recommended"])
        self.assertEqual(after, before)

    def test_database_health_recommends_maintenance_only_when_both_thresholds_are_met(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            with storage._connect() as conn:
                conn.execute("CREATE TABLE maintenance_pressure(payload BLOB)")
                conn.executemany(
                    "INSERT INTO maintenance_pressure(payload) VALUES (zeroblob(8192))",
                    [() for _ in range(32)],
                )
                conn.execute("DROP TABLE maintenance_pressure")

            with patch.object(storage, "DATABASE_MAINTENANCE_MIN_RECLAIMABLE_BYTES", 1), patch.object(
                storage, "DATABASE_MAINTENANCE_MIN_RECLAIMABLE_RATIO", 0.0
            ):
                health = storage.get_database_health()

        self.assertGreater(health["reclaimable_bytes"], 0)
        self.assertGreater(health["reclaimable_ratio"], 0)
        self.assertTrue(health["maintenance_recommended"])
        self.assertEqual(health["status"], "attention")

    def test_overview_excludes_draft_runs_from_task_level_metrics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(
                {
                    "name": "正式任务",
                    "type": "api",
                    "enabled": True,
                    "interval_seconds": 300,
                    "timeout_ms": 10000,
                    "entry_url": "https://example.com",
                    "method": "GET",
                    "headers_json": "{}",
                    "body": "",
                    "script": "async def check(ctx):\n    pass\n",
                    "tags": "",
                }
            )
            saved_run = storage.create_run(check)
            storage.finish_run(int(saved_run["id"]), run_payload("ok"))

            draft_run = storage.create_run({"id": 0, "name": "草稿调试", "type": "api"})
            storage.finish_run(int(draft_run["id"]), run_payload("failed", "草稿失败"))

            overview = storage.get_overview()

        self.assertEqual(overview["today_runs"], 1)
        self.assertEqual(overview["latest_run"]["id"], saved_run["id"])
        self.assertNotIn(draft_run["id"], [run["id"] for run in overview["recent_failures"]])

    def test_overview_excludes_runner_failures_from_business_failure_metrics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            storage.update_settings({"api_failure_confirmation_count": 1})
            target_check = storage.create_check(api_check_data("Target API"))
            runner_check = storage.create_check(api_check_data("Runner API"))

            target_run = storage.create_run(target_check)
            target_finished = storage.finish_run(int(target_run["id"]), run_payload("failed", "target failed"))
            assert target_finished is not None
            storage.update_check_status(int(target_check["id"]), target_finished)

            runner_run = storage.create_run(
                {
                    **runner_check,
                    "_runner": storage.runner_metadata(
                        {"runner_id": "edge-1", "name": "Edge 1"},
                        failure_kind="runner",
                    ),
                }
            )
            runner_finished = storage.finish_run(
                int(runner_run["id"]),
                {
                    **run_payload("failed", "runner failed"),
                    **storage.runner_metadata({"runner_id": "edge-1", "name": "Edge 1"}, failure_kind="runner"),
                    "affects_health": False,
                },
            )
            assert runner_finished is not None

            overview = storage.get_overview()
            retained_runner_run = storage.get_run(int(runner_run["id"]))
            listed_runner_runs = storage.list_runs({"runner_id": "edge-1"}, limit=10)

        trend_24h = {item["check_type"]: item for item in overview["trends"][0]["series"]}
        self.assertIsNotNone(retained_runner_run)
        self.assertEqual(retained_runner_run["failure_kind"], "runner")
        self.assertFalse(retained_runner_run["affects_health"])
        self.assertIn(runner_run["id"], [run["id"] for run in listed_runner_runs])
        self.assertEqual(overview["failing_count"], 1)
        self.assertEqual([run["id"] for run in overview["recent_failures"]], [target_run["id"]])
        self.assertEqual(trend_24h["api"]["runs"], 1)
        self.assertEqual(trend_24h["api"]["failure_count"], 1)

    def test_recent_business_incidents_exclude_runner_and_non_health_failures(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(api_check_data("Incident API"))
            target_run = storage.create_run(check)
            storage.finish_run(int(target_run["id"]), run_payload("failed", "legacy target failure"))

            runner_run = storage.create_run(
                {
                    **check,
                    "_runner": storage.runner_metadata(
                        {"runner_id": "edge-1", "name": "Edge 1"},
                        failure_kind="runner",
                    ),
                }
            )
            storage.finish_run(
                int(runner_run["id"]),
                {
                    **run_payload("failed", "runner failed"),
                    **storage.runner_metadata({"runner_id": "edge-1", "name": "Edge 1"}, failure_kind="runner"),
                },
            )

            draft_run = storage.create_run({"id": 0, "name": "Draft API", "type": "api"})
            storage.finish_run(int(draft_run["id"]), run_payload("failed", "draft failure"))

            incidents = storage.list_recent_business_incidents(limit=10)

        self.assertEqual([run["id"] for run in incidents], [target_run["id"]])

    def test_overview_trends_split_ui_and_api_windows_and_ignore_invalid_runs(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            api_check = storage.create_check(api_check_data("Trend API"))
            ui_check = storage.create_check(ui_check_data("Trend UI"))
            api_check_id = int(api_check["id"])
            ui_check_id = int(ui_check["id"])
            now = datetime.now().astimezone().replace(microsecond=0)

            insert_trend_run(api_check_id, "api", "ok", now - timedelta(hours=1), 100)
            insert_trend_run(api_check_id, "api", "failed", now - timedelta(hours=2), 300)
            insert_trend_run(api_check_id, "api", "timeout", now - timedelta(hours=3), 500)
            insert_trend_run(api_check_id, "api", "failed", now - timedelta(hours=3, minutes=30), 900, failure_kind="runner")
            insert_trend_run(api_check_id, "api", "ok", now - timedelta(hours=4), 700)
            insert_trend_run(api_check_id, "api", "ok", now - timedelta(days=2), 200)
            insert_trend_run(api_check_id, "api", "failed", now - timedelta(days=3), 400)
            insert_trend_run(api_check_id, "api", "ok", now - timedelta(days=4), 600)

            insert_trend_run(ui_check_id, "ui", "ok", now - timedelta(hours=1), 120)
            insert_trend_run(ui_check_id, "ui", "failed", now - timedelta(hours=2), 320)
            insert_trend_run(ui_check_id, "ui", "ok", now - timedelta(days=2), 220)
            insert_trend_run(ui_check_id, "ui", "timeout", now - timedelta(days=3), 420)
            insert_trend_run(ui_check_id, "ui", "ok", now - timedelta(days=4), 620)

            insert_trend_run(api_check_id, "api", "ok", now - timedelta(days=8), 999)
            insert_trend_run(ui_check_id, "ui", "ok", now - timedelta(days=8), 998)
            insert_trend_run(0, "api", "failed", now - timedelta(hours=5), 1)
            insert_trend_run(-10, "ui", "ok", now - timedelta(hours=6), 2)
            insert_trend_run(api_check_id, "api", "skipped", now - timedelta(hours=7), 3)
            insert_trend_run(api_check_id, "api", "running", now - timedelta(hours=8), 4)
            insert_trend_run(ui_check_id, "ui", "pending", now - timedelta(hours=9), 5)
            insert_trend_run(api_check_id, "api", "failed", now + timedelta(hours=1), 800)

            overview = storage.get_overview()

        trends = {item["key"]: item for item in overview["trends"]}
        trend_24h = {item["check_type"]: item for item in trends["24h"]["series"]}
        trend_7d = {item["check_type"]: item for item in trends["7d"]["series"]}

        self.assertEqual(set(trends), {"24h", "7d"})
        self.assertEqual(set(trend_24h), {"ui", "api"})
        self.assertEqual(trend_24h["ui"]["label"], "UI")
        self.assertEqual(trend_24h["api"]["label"], "API")
        self.assertNotIn("runs", trends["24h"])
        self.assertEqual(trend_24h["api"]["runs"], 4)
        self.assertEqual(trend_24h["api"]["success_count"], 2)
        self.assertEqual(trend_24h["api"]["success_rate"], 50.0)
        self.assertEqual(trend_24h["api"]["failure_count"], 2)
        self.assertEqual(trend_24h["api"]["duration_p50_ms"], 100)
        self.assertEqual(trend_24h["api"]["duration_p95_ms"], 700)
        self.assertEqual(trend_24h["ui"]["runs"], 2)
        self.assertEqual(trend_24h["ui"]["success_count"], 1)
        self.assertEqual(trend_24h["ui"]["success_rate"], 50.0)
        self.assertEqual(trend_24h["ui"]["failure_count"], 1)
        self.assertEqual(trend_24h["ui"]["duration_p50_ms"], 120)
        self.assertEqual(trend_24h["ui"]["duration_p95_ms"], 120)
        self.assertEqual(trend_7d["api"]["runs"], 7)
        self.assertEqual(trend_7d["api"]["success_rate"], 57.1)
        self.assertEqual(trend_7d["api"]["failure_count"], 3)
        self.assertEqual(trend_7d["api"]["duration_p50_ms"], 200)
        self.assertEqual(trend_7d["api"]["duration_p95_ms"], 700)
        self.assertEqual(trend_7d["ui"]["runs"], 5)
        self.assertEqual(trend_7d["ui"]["success_rate"], 60.0)
        self.assertEqual(trend_7d["ui"]["failure_count"], 2)
        self.assertEqual(trend_7d["ui"]["duration_p50_ms"], 220)
        self.assertEqual(trend_7d["ui"]["duration_p95_ms"], 620)

    def test_overview_trends_use_one_seven_day_scan_and_preserve_project_boundaries(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            project_id = storage.default_project_id()
            check = storage.create_check(api_check_data("Scoped trend API"), project_id=project_id)
            other_project = storage.create_project("Other trend project")
            other_check = storage.create_check(api_check_data("Other trend API"), project_id=other_project["id"])
            now = datetime.now().astimezone().replace(microsecond=0)

            insert_trend_run(int(check["id"]), "api", "ok", now - timedelta(days=7), 10)
            insert_trend_run(int(check["id"]), "api", "ok", now - timedelta(hours=24), 20)
            insert_trend_run(int(check["id"]), "api", "ok", now - timedelta(hours=1), 30)
            insert_trend_run(int(check["id"]), "api", "failed", now - timedelta(hours=2), None)
            insert_trend_run(int(check["id"]), "api", "ok", now - timedelta(days=7, seconds=1), 999)
            insert_trend_run(int(other_check["id"]), "api", "ok", now - timedelta(hours=1), 1000)

            statements: list[str] = []
            with storage._connect(read_only=True) as conn:
                conn.set_trace_callback(statements.append)
                trends = storage._overview_trends(conn, now=now, project_id=project_id)
                conn.set_trace_callback(None)

        trend_queries = [statement for statement in statements if "FROM runs" in statement]
        self.assertEqual(len(trend_queries), 1)
        by_period = {item["key"]: item for item in trends}
        recent = {item["check_type"]: item for item in by_period["24h"]["series"]}["api"]
        weekly = {item["check_type"]: item for item in by_period["7d"]["series"]}["api"]
        self.assertEqual(recent["runs"], 3)
        self.assertEqual(recent["success_count"], 2)
        self.assertEqual(recent["failure_count"], 1)
        self.assertEqual(recent["duration_p50_ms"], 20)
        self.assertEqual(recent["duration_p95_ms"], 30)
        self.assertEqual(weekly["runs"], 4)
        self.assertEqual(weekly["success_count"], 3)
        self.assertEqual(weekly["failure_count"], 1)
        self.assertEqual(weekly["duration_p50_ms"], 20)
        self.assertEqual(weekly["duration_p95_ms"], 30)

    def test_overview_trends_return_empty_metrics_without_duration_samples(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(api_check_data("Sparse Trend API"))
            now = datetime.now().astimezone().replace(microsecond=0)
            insert_trend_run(int(check["id"]), "api", "ok", now - timedelta(hours=1), None)

            overview = storage.get_overview()

        trends = {item["key"]: item for item in overview["trends"]}
        series = {item["check_type"]: item for item in trends["24h"]["series"]}
        self.assertEqual(series["api"]["runs"], 1)
        self.assertEqual(series["api"]["success_rate"], 100.0)
        self.assertEqual(series["api"]["failure_count"], 0)
        self.assertIsNone(series["api"]["duration_p50_ms"])
        self.assertIsNone(series["api"]["duration_p95_ms"])
        self.assertEqual(series["ui"]["runs"], 0)
        self.assertIsNone(series["ui"]["success_rate"])
        self.assertEqual(series["ui"]["failure_count"], 0)
        self.assertIsNone(series["ui"]["duration_p50_ms"])
        self.assertIsNone(series["ui"]["duration_p95_ms"])

    def test_ui_setup_fields_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(
                {
                    "name": "登录后页面",
                    "type": "ui",
                    "enabled": True,
                    "interval_seconds": 300,
                    "timeout_ms": 15000,
                    "entry_url": "https://example.com/dashboard",
                    "viewport_mode": "h5",
                    "method": "",
                    "headers_json": "{}",
                    "body": "",
                    "assertions_json": '[{"type":"title_contains","expected_text":"Dashboard"}]',
                    "setup_script": "async def setup(ctx, page):\n    pass\n",
                    "script": "",
                    "tags": "",
                }
            )

            loaded = storage.get_check(int(check["id"]))

        self.assertEqual(loaded["viewport_mode"], "h5")
        self.assertIn("async def setup", loaded["setup_script"])

    def test_pending_run_can_transition_to_running(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(
                {
                    "name": "排队任务",
                    "type": "api",
                    "enabled": True,
                    "interval_seconds": 300,
                    "timeout_ms": 10000,
                    "entry_url": "https://example.com",
                    "method": "GET",
                    "headers_json": "{}",
                    "body": "",
                    "script": "async def check(ctx):\n    pass\n",
                    "tags": "",
                }
            )

            queued = storage.create_run(check, "pending")
            started = storage.start_run(int(queued["id"]))

        self.assertIsNotNone(started)
        self.assertEqual(started["status"], "running")
        self.assertIsNone(started["finished_at"])


class CheckAlertPolicyStorageTests(unittest.TestCase):
    def test_create_get_and_update_check_persist_alert_policy_json(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            defaulted = storage.create_check(api_check_data("Default policy API"))

            policy = {
                "alert_cooldown_minutes": 15,
                "recovery_notification": False,
                "notification_channel_ids": ["ops"],
            }
            created = storage.create_check(
                api_check_data("Policy API", alert_policy_json=json_dumps(policy))
            )
            loaded = storage.get_check(int(created["id"]))

            updated_policy = {
                "alert_cooldown_minutes": 45,
                "recovery_notification": True,
                "notification_channel_ids": ["ops", "backup"],
            }
            update_payload = dict(created)
            update_payload["name"] = "Updated policy API"
            update_payload["alert_policy_json"] = json_dumps(updated_policy)
            updated = storage.update_check(int(created["id"]), update_payload)
            reloaded = storage.get_check(int(created["id"]))

        self.assertEqual(defaulted["alert_policy_json"], "{}")
        self.assertIsNotNone(loaded)
        self.assertIsNotNone(updated)
        self.assertIsNotNone(reloaded)
        self.assertEqual(loaded["alert_policy_json"], json_dumps(policy))
        self.assertEqual(updated["alert_policy_json"], json_dumps(updated_policy))
        self.assertEqual(reloaded["alert_policy_json"], json_dumps(updated_policy))

    def test_ui_check_browser_selection_fields_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(
                {
                    **ui_check_data("Browser matrix UI"),
                    "browser_selection_mode": "round_robin_all",
                    "browser_types": ["firefox", "chromium"],
                }
            )
            loaded = storage.get_check(int(check["id"]))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["browser_selection_mode"], "round_robin_all")
        self.assertEqual(loaded["browser_types"], ["firefox", "chromium"])

    def test_legacy_check_rows_require_explicit_project_migration(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            db_path = Path(temp_dir) / "pulseguard.db"
            timestamp = storage.now_iso()
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute(
                    """
                    CREATE TABLE checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        interval_seconds INTEGER NOT NULL DEFAULT 300,
                        timeout_ms INTEGER NOT NULL DEFAULT 15000,
                        entry_url TEXT,
                        viewport_mode TEXT NOT NULL DEFAULT 'web',
                        method TEXT,
                        headers_json TEXT,
                        body TEXT,
                        assertions_json TEXT,
                        setup_script TEXT NOT NULL DEFAULT '',
                        script TEXT NOT NULL,
                        tags TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO checks (
                        name, type, enabled, interval_seconds, timeout_ms, entry_url, viewport_mode,
                        method, headers_json, body, assertions_json, setup_script, script, tags, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "Legacy policy API",
                        "api",
                        1,
                        300,
                        10000,
                        "https://example.com/legacy",
                        "web",
                        "GET",
                        "{}",
                        "",
                        "[]",
                        "",
                        "",
                        "legacy",
                        timestamp,
                        timestamp,
                    ),
                )

            with self.assertRaisesRegex(RuntimeError, "migrate-projects.py --apply"):
                storage.init_db()

            self.assertTrue(db_path.exists())
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(conn.execute("SELECT name FROM checks").fetchall(), [("Legacy policy API",)])

    def test_member_settings_support_multiple_channels_and_prune_deleted_task_references(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            members = [
                {
                    "id": "alice",
                    "name": "Alice",
                    "feishu_open_id": "ou_alice",
                    "wecom_user_id": "alice.wx",
                    "wecom_mobile": "13800000001",
                    "dingtalk_user_id": "alice.ding",
                    "dingtalk_mobile": "13900000001",
                },
                {
                    "id": "bob",
                    "name": "Bob",
                    "feishu_open_id": "ou_bob",
                    "wecom_user_id": "",
                    "wecom_mobile": "",
                    "dingtalk_user_id": "",
                    "dingtalk_mobile": "",
                },
            ]
            storage.update_settings({"members": members})
            check = storage.create_check(
                api_check_data(
                    "Member policy API",
                    alert_policy_json=json_dumps({"member_ids": ["alice", "bob"]}),
                )
            )

            storage.update_settings({"members": [members[1]]})
            loaded = storage.get_check(int(check["id"]))
            saved_members = storage.get_settings()["members"]

        self.assertEqual(saved_members, [members[1]])
        self.assertEqual(json.loads(loaded["alert_policy_json"])["member_ids"], ["bob"])


class CheckBatchStorageTests(unittest.TestCase):
    def test_check_tag_set_normalizes_comma_and_whitespace_tags(self) -> None:
        self.assertEqual(storage.check_tag_set(" smoke,Prod api  "), {"smoke", "prod", "api"})

    def test_select_checks_for_batch_matches_type_and_exact_tag_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            smoke_api = storage.create_check(api_check_data("Smoke API", tags="smoke, prod"))
            storage.create_check(api_check_data("Smoketest API", tags="smoketest"))
            storage.create_check(ui_check_data("Smoke UI", tags="smoke"))

            selected = storage.select_checks_for_batch("api", tag="SMOKE")

        self.assertEqual([item["id"] for item in selected], [smoke_api["id"]])

    def test_select_checks_for_batch_can_limit_to_enabled_checks(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            enabled = storage.create_check(api_check_data("Enabled API", tags="batch"))
            disabled = storage.create_check(api_check_data("Disabled API", tags="batch", enabled=False))

            selected_enabled = storage.select_checks_for_batch("api", tag="batch", enabled_only=True)
            selected_all = storage.select_checks_for_batch("api", tag="batch", enabled_only=False)

        self.assertEqual([item["id"] for item in selected_enabled], [enabled["id"]])
        self.assertEqual({item["id"] for item in selected_all}, {enabled["id"], disabled["id"]})

    def test_batch_enabled_and_interval_updates_clean_selected_ids(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            first = storage.create_check(api_check_data("First API"))
            second = storage.create_check(api_check_data("Second API"))
            third = storage.create_check(api_check_data("Third API"))

            disabled_count = storage.batch_set_check_enabled([int(first["id"]), int(second["id"]), int(first["id"]), 0, -1], False)
            interval_count = storage.batch_update_check_interval([int(second["id"]), int(third["id"])], 120)
            reloaded = {item["id"]: item for item in storage.list_checks("api")}

        self.assertEqual(disabled_count, 2)
        self.assertEqual(interval_count, 2)
        self.assertFalse(reloaded[first["id"]]["enabled"])
        self.assertFalse(reloaded[second["id"]]["enabled"])
        self.assertTrue(reloaded[third["id"]]["enabled"])
        self.assertEqual(reloaded[second["id"]]["interval_seconds"], 120)
        self.assertEqual(reloaded[third["id"]]["interval_seconds"], 120)


class HeartbeatStorageTests(unittest.TestCase):
    def test_record_heartbeat_upserts_latest_payload(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            first = storage.record_heartbeat("nightly-job", status="ok", message="started", payload={"build": 1})
            second = storage.record_heartbeat("nightly-job", status="failed", message="failed", payload={"build": 2})
            loaded = storage.get_heartbeat("nightly-job")

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["payload"], {"build": 2})
        self.assertEqual(loaded["message"], "failed")
        self.assertEqual(loaded["payload"], {"build": 2})


class RunArchiveStorageTests(unittest.TestCase):
    def test_cleanup_old_data_archives_expired_run_summaries_before_delete(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch("backend.app.storage.cleanup_old_artifacts", return_value={}):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Archive API"))
            check_id = int(check["id"])
            old_day = datetime.now().astimezone().replace(microsecond=0) - timedelta(days=3)
            recent = datetime.now().astimezone().replace(microsecond=0)
            insert_trend_run(check_id, "api", "ok", old_day, 100)
            insert_trend_run(check_id, "api", "failed", old_day + timedelta(hours=1), 300)
            insert_trend_run(check_id, "api", "ok", recent, 500)

            removed = storage.cleanup_old_data({"run_retention_days": 1})
            archives = storage.list_run_archives()
            remaining = storage.list_runs(limit=10)

        archive_by_status = {item["status"]: item for item in archives}
        self.assertEqual(removed, 2)
        self.assertEqual(set(archive_by_status), {"failed", "ok"})
        self.assertEqual(archive_by_status["ok"]["run_count"], 1)
        self.assertEqual(archive_by_status["ok"]["duration_sum_ms"], 100)
        self.assertEqual(archive_by_status["failed"]["duration_sample_count"], 1)
        self.assertEqual([run["status"] for run in remaining], ["ok"])


class RepeatedFailureCompactionTests(unittest.TestCase):
    def test_delete_artifact_paths_ignores_locked_files(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            artifacts, "SCREENSHOTS_DIR", Path(temp_dir) / "screenshots"
        ):
            screenshots_dir = Path(temp_dir) / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            locked_file = screenshots_dir / "locked.png"
            locked_file.write_text("artifact", encoding="utf-8")

            with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
                removed = artifacts.delete_artifact_paths(["screenshots/locked.png"])

            self.assertEqual(removed, [])
            self.assertTrue(locked_file.exists())

    def test_finish_run_compacts_repeated_failures_and_deletes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch("backend.app.artifacts.SCREENSHOTS_DIR", Path(temp_dir) / "screenshots"), patch(
            "backend.app.artifacts.TRACES_DIR", Path(temp_dir) / "traces"
        ), patch("backend.app.artifacts.RESPONSES_DIR", Path(temp_dir) / "responses"):
            screenshots_dir = Path(temp_dir) / "screenshots"
            traces_dir = Path(temp_dir) / "traces"
            responses_dir = Path(temp_dir) / "responses"
            for directory in (screenshots_dir, traces_dir, responses_dir):
                directory.mkdir(parents=True, exist_ok=True)

            storage.init_db()
            storage.update_settings({"similar_failure_retention_count": 3})
            check = storage.create_check(api_check_data("Repeated Failure API"))
            created_ids: list[int] = []
            artifact_paths: dict[int, list[Path]] = {}

            for index in range(5):
                run = storage.create_run(check)
                run_id = int(run["id"])
                created_ids.append(run_id)
                screenshot = screenshots_dir / f"failure-{index}.png"
                trace = traces_dir / f"trace-{index}.zip"
                response = responses_dir / f"response-{index}.json"
                for path in (screenshot, trace, response):
                    path.write_text("artifact", encoding="utf-8")
                artifact_paths[run_id] = [screenshot, trace, response]
                storage.finish_run(
                    run_id,
                    {
                        **run_payload("failed", "same target failure"),
                        "screenshot_path": f"screenshots/{screenshot.name}",
                        "trace_path": f"traces/{trace.name}",
                        "response_path": f"responses/{response.name}",
                    },
                )

            runs = storage.list_runs({"check_id": int(check["id"])}, limit=10)
            latest = storage.get_run(created_ids[-1])

            self.assertEqual([run["id"] for run in runs], list(reversed(created_ids[-3:])))
            assert latest is not None
            self.assertEqual(latest["deduplicated_count"], 2)
            for run_id in created_ids[:2]:
                self.assertTrue(all(not path.exists() for path in artifact_paths[run_id]))
            for run_id in created_ids[-3:]:
                self.assertTrue(all(path.exists() for path in artifact_paths[run_id]))


class MonitoringTrendStorageTests(unittest.TestCase):
    def test_init_db_skips_historical_trend_backfill_by_default(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "TREND_BACKFILL_ON_STARTUP", False):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("No Startup Backfill API"))
            check_id = int(check["id"])
            started_at = datetime.now().astimezone().replace(microsecond=0) - timedelta(hours=1)
            insert_trend_run(check_id, "api", "ok", started_at, 180)

            storage.init_db()
            trend = storage.get_check_trend(
                check_id,
                period="custom",
                start=(started_at - timedelta(minutes=5)).isoformat(timespec="seconds"),
                end=(started_at + timedelta(minutes=5)).isoformat(timespec="seconds"),
            )
            with storage._connect() as conn:
                rollup_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM trend_rollups WHERE check_id = ?",
                    (check_id,),
                ).fetchone()["count"]
                recorded_at = conn.execute(
                    "SELECT trend_recorded_at FROM runs WHERE check_id = ?",
                    (check_id,),
                ).fetchone()["trend_recorded_at"]

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 0)
        self.assertEqual(rollup_count, 0)
        self.assertIsNone(recorded_at)

    def test_init_db_backfills_historical_trends_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "TREND_BACKFILL_ON_STARTUP", False):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Startup Backfill API"))
            check_id = int(check["id"])
            started_at = datetime.now().astimezone().replace(microsecond=0) - timedelta(hours=1)
            insert_trend_run(check_id, "api", "ok", started_at, 180)

            with patch.object(storage, "TREND_BACKFILL_ON_STARTUP", True):
                storage.init_db()
            trend = storage.get_check_trend(
                check_id,
                period="custom",
                start=(started_at - timedelta(minutes=5)).isoformat(timespec="seconds"),
                end=(started_at + timedelta(minutes=5)).isoformat(timespec="seconds"),
            )
            with storage._connect() as conn:
                rollup_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM trend_rollups WHERE check_id = ?",
                    (check_id,),
                ).fetchone()["count"]
                recorded_at = conn.execute(
                    "SELECT trend_recorded_at FROM runs WHERE check_id = ?",
                    (check_id,),
                ).fetchone()["trend_recorded_at"]

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 1)
        self.assertGreater(rollup_count, 0)
        self.assertIsNotNone(recorded_at)

    def test_custom_window_includes_overlapping_first_source_bucket(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Partial Bucket API"))
            base = datetime.now().astimezone().replace(second=0, microsecond=0)
            base = base - timedelta(minutes=base.minute % 5)
            run_at = base + timedelta(minutes=2)
            run = storage.create_run(check)
            set_run_started_at(int(run["id"]), run_at)
            storage.finish_run(int(run["id"]), run_payload("ok", duration_ms=180))

            trend = storage.get_check_trend(
                int(check["id"]),
                period="custom",
                start=(base + timedelta(minutes=1)).isoformat(timespec="seconds"),
                end=(base + timedelta(minutes=4)).isoformat(timespec="seconds"),
            )

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 1)

    def test_long_custom_window_keeps_task_points_under_limit(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Long Range API"))
            start = datetime.now().astimezone().replace(microsecond=0) - timedelta(days=3650)
            for index in range(30):
                run = storage.create_run(check)
                set_run_started_at(int(run["id"]), start + timedelta(days=index * 100))
                storage.finish_run(int(run["id"]), run_payload("ok", duration_ms=120 + index))

            page = storage.list_monitoring_trends(
                period="custom",
                start=start.isoformat(timespec="seconds"),
                end=(start + timedelta(days=3650)).isoformat(timespec="seconds"),
                page=1,
                page_size=12,
            )

        self.assertLessEqual(len(page["tasks"]["items"][0]["points"]), 20)

    def test_finish_run_records_rollups_and_merges_percentiles_from_histograms(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Latency API"))
            now = datetime.now().astimezone().replace(microsecond=0)

            for index in range(20):
                run = storage.create_run(check)
                set_run_started_at(int(run["id"]), now - timedelta(hours=2, minutes=index))
                storage.finish_run(int(run["id"]), run_payload("ok", duration_ms=100))
            for index in range(20):
                run = storage.create_run(check)
                set_run_started_at(int(run["id"]), now - timedelta(hours=1, minutes=index))
                storage.finish_run(int(run["id"]), run_payload("ok", duration_ms=3000))

            trend = storage.get_check_trend(
                int(check["id"]),
                period="custom",
                start=(now - timedelta(hours=3)).isoformat(timespec="seconds"),
                end=(now + timedelta(minutes=1)).isoformat(timespec="seconds"),
            )
            page = storage.list_monitoring_trends(
                period="custom",
                start=(now - timedelta(hours=3)).isoformat(timespec="seconds"),
                end=(now + timedelta(minutes=1)).isoformat(timespec="seconds"),
                page=1,
                page_size=12,
            )

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 40)
        self.assertEqual(trend["avg_duration_ms"], 1550)
        self.assertEqual(trend["p95_duration_ms"], 3000)
        self.assertIsNone(trend["p99_duration_ms"])
        summary = next(item for item in page["summaries"] if item["check_type"] == "api")
        self.assertEqual(summary["success_count"], 40)
        self.assertEqual(summary["p95_duration_ms"], 3000)

    def test_trend_tail_latency_percentile_uses_exact_success_durations(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Tail Bucket API"))
            now = datetime.now().astimezone().replace(microsecond=0)
            sample_at = now - timedelta(minutes=1)

            for index in range(98):
                run = storage.create_run(check)
                set_run_started_at(int(run["id"]), sample_at)
                storage.finish_run(int(run["id"]), run_payload("ok", duration_ms=1500))
            for index in range(2):
                run = storage.create_run(check)
                set_run_started_at(int(run["id"]), sample_at)
                storage.finish_run(int(run["id"]), run_payload("ok", duration_ms=16266))

            trend = storage.get_check_trend(
                int(check["id"]),
                period="custom",
                start=(now - timedelta(hours=3)).isoformat(timespec="seconds"),
                end=(now + timedelta(minutes=1)).isoformat(timespec="seconds"),
            )

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 100)
        self.assertEqual(trend["p99_duration_ms"], 16266)
        self.assertLess(trend["p99_duration_ms"], 30000)
        self.assertEqual(len(trend["points"]), 1)
        self.assertEqual(trend["points"][0]["p99_duration_ms"], 16266)

    def test_failed_runs_count_failures_without_latency_and_runner_failures_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Failures API"))
            target = storage.create_run(check)
            storage.finish_run(int(target["id"]), {**run_payload("failed", "target down", duration_ms=900), "failure_kind": "target"})
            runner = storage.create_run(check)
            storage.finish_run(int(runner["id"]), {**run_payload("failed", "runner down", duration_ms=1200), "failure_kind": "runner"})

            trend = storage.get_check_trend(int(check["id"]))

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 0)
        self.assertEqual(trend["failure_count"], 1)
        self.assertIsNone(trend["avg_duration_ms"])

    def test_draft_and_non_health_runs_do_not_enter_trends(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Formal API"))
            draft = storage.create_run({"id": 0, "name": "Draft", "type": "api"})
            storage.finish_run(int(draft["id"]), run_payload("ok", duration_ms=100))
            verification = storage.create_run(
                {
                    **check,
                    "_run": {"trigger": "manual-verify", "observation_kind": "verification", "affects_health": False},
                }
            )
            storage.finish_run(int(verification["id"]), run_payload("ok", duration_ms=100))

            trend = storage.get_check_trend(int(check["id"]))

        self.assertIsNotNone(trend)
        assert trend is not None
        self.assertEqual(trend["success_count"], 0)
        self.assertEqual(trend["failure_count"], 0)
        self.assertEqual(trend["points"], [])

    def test_monitoring_trends_paginates_tasks(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            for index in range(13):
                storage.create_check(api_check_data(f"Paged API {index:02d}"))

            page = storage.list_monitoring_trends(page=2, page_size=12, check_type="api")

        self.assertEqual(page["tasks"]["total"], 13)
        self.assertEqual(page["tasks"]["page"], 2)
        self.assertEqual(len(page["tasks"]["items"]), 1)
        self.assertEqual(page["tasks"]["items"][0]["check_type"], "api")


class AuditAndVersionStorageTests(unittest.TestCase):
    def test_record_audit_event_lists_payload(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            event = storage.record_audit_event("updated", "check", 7, "API", "更新任务", {"field": "name"})
            events = storage.list_audit_events()

        self.assertEqual(event["payload"], {"field": "name"})
        self.assertEqual(events[0]["id"], event["id"])
        self.assertEqual(events[0]["summary"], "更新任务")

    def test_record_check_version_stores_definition_snapshot_only(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Versioned API", tags="audit"))
            check["current_status"] = "failed"
            version = storage.record_check_version(check, "updated")
            versions = storage.list_check_versions(int(check["id"]))

        self.assertEqual(version["snapshot"]["name"], "Versioned API")
        self.assertEqual(version["snapshot"]["tags"], "audit")
        self.assertNotIn("current_status", version["snapshot"])
        self.assertEqual(versions[0]["id"], version["id"])


class RunnerStorageTests(unittest.TestCase):
    def test_init_db_adds_relay_port_column_before_creating_its_index(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            db_path = Path(temp_dir) / "pulseguard.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE probe_runners (
                        runner_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        address TEXT NOT NULL DEFAULT '',
                        network_region TEXT NOT NULL DEFAULT 'local',
                        browser_version TEXT NOT NULL DEFAULT '',
                        installed_browser_types_json TEXT NOT NULL DEFAULT '[]',
                        available_browser_types_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'ok',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        role TEXT NOT NULL DEFAULT 'child',
                        token_value TEXT NOT NULL DEFAULT '',
                        token_hash TEXT NOT NULL DEFAULT '',
                        token_hint TEXT NOT NULL DEFAULT '',
                        unavailable_since TEXT,
                        unavailable_notified_at TEXT,
                        created_at TEXT NOT NULL DEFAULT '',
                        last_seen_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )

            storage.init_db()
            with sqlite3.connect(db_path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(probe_runners)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(probe_runners)")}

        self.assertIn("allocated_internal_port", columns)
        self.assertIn("idx_probe_runners_relay_port", indexes)

    def test_run_metadata_is_persisted_from_create_and_finish(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(api_check_data("Runner API"))

            run = storage.create_run(
                {
                    **check,
                    "_runner": {
                        "runner_name": "office-runner",
                        "runner_address": "10.0.0.8",
                        "runner_region": "office-lan",
                        "runner_browser_version": "",
                        "failure_kind": "none",
                    },
                },
                "pending",
            )

            payload = run_payload("failed", "target failed")
            payload.update(
                {
                    "runner_name": "office-runner",
                    "runner_address": "10.0.0.8",
                    "runner_region": "office-lan",
                    "runner_browser_version": "chromium 120.0",
                    "failure_kind": "target",
                }
            )
            finished = storage.finish_run(int(run["id"]), payload)

        self.assertEqual(run["runner_name"], "office-runner")
        self.assertEqual(run["runner_region"], "office-lan")
        self.assertEqual(run["failure_kind"], "none")
        self.assertIsNotNone(finished)
        self.assertEqual(finished["runner_name"], "office-runner")
        self.assertEqual(finished["runner_address"], "10.0.0.8")
        self.assertEqual(finished["runner_region"], "office-lan")
        self.assertEqual(finished["runner_browser_version"], "chromium 120.0")
        self.assertEqual(finished["failure_kind"], "target")

    def test_api_run_does_not_persist_browser_type(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check({**api_check_data("API Browser Type"), "browser_type": "chromium"})
            run = storage.create_run(
                {
                    **check,
                    "browser_type": "chromium",
                    "_runner": {**storage.runner_metadata({"runner_id": "local", "name": "local"}), "browser_type": "chromium"},
                }
            )
            finished = storage.finish_run(int(run["id"]), {**run_payload("ok"), "browser_type": "chromium"})
            loaded = storage.get_run(int(run["id"]))

        self.assertEqual(run["browser_type"], "")
        self.assertIsNotNone(finished)
        self.assertEqual(finished["browser_type"], "")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["browser_type"], "")

    def test_runs_can_be_filtered_by_run_group_id_for_multi_runner_results(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            check = storage.create_check(api_check_data("Grouped Runner API"))
            first = storage.create_run(
                {
                    **check,
                    "_runner": storage.runner_metadata({"runner_id": "edge-1", "name": "Edge 1"}),
                    "_run": {**storage.run_metadata("manual"), "run_group_id": "rg-test-1"},
                },
                "pending",
            )
            second = storage.create_run(
                {
                    **check,
                    "_runner": storage.runner_metadata({"runner_id": "edge-2", "name": "Edge 2"}),
                    "_run": {**storage.run_metadata("manual"), "run_group_id": "rg-test-1"},
                },
                "pending",
            )
            other = storage.create_run(
                {
                    **check,
                    "_runner": storage.runner_metadata({"runner_id": "edge-3", "name": "Edge 3"}),
                    "_run": {**storage.run_metadata("manual"), "run_group_id": "rg-test-2"},
                },
                "pending",
            )
            storage.finish_run(int(first["id"]), {**run_payload("ok"), **storage.runner_metadata({"runner_id": "edge-1", "name": "Edge 1"})})
            storage.finish_run(int(second["id"]), {**run_payload("failed", "target failed"), **storage.runner_metadata({"runner_id": "edge-2", "name": "Edge 2"}, failure_kind="target")})
            storage.finish_run(int(other["id"]), {**run_payload("ok"), **storage.runner_metadata({"runner_id": "edge-3", "name": "Edge 3"})})

            grouped = storage.list_runs({"run_group_id": "rg-test-1"}, limit=10)
            grouped_page = storage.list_runs_page({"run_group_id": "rg-test-1"}, page=1, page_size=10)

        self.assertEqual({run["runner_id"] for run in grouped}, {"edge-1", "edge-2"})
        self.assertEqual(grouped_page["total"], 2)
        self.assertEqual({run["runner_id"] for run in grouped_page["items"]}, {"edge-1", "edge-2"})
        self.assertNotIn("logs", grouped_page["items"][0])
        self.assertNotIn("error_stack", grouped_page["items"][0])
        self.assertNotIn("request_snapshot", grouped_page["items"][0])
        self.assertNotIn("response_snapshot", grouped_page["items"][0])

    def test_failed_run_without_failure_kind_defaults_to_target(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            run = storage.create_run(
                {
                    "id": 3,
                    "name": "Legacy failure",
                    "type": "api",
                },
                "failed",
                "legacy failure",
            )
            loaded = storage.get_run(int(run["id"]))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["failure_kind"], "target")

    def test_probe_runner_heartbeat_upserts_and_lists_metadata(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            first = storage.upsert_probe_runner(
                {
                    "runner_id": "office-1",
                    "name": "Office Runner",
                    "address": "http://10.0.0.8:8787",
                    "network_region": "office-lan",
                    "browser_version": "chromium 120.0",
                    "status": "ok",
                    "metadata": {"capability": "ui"},
                }
            )
            updated = storage.upsert_probe_runner(
                {
                    "runner_id": "office-1",
                    "name": "Office Runner 2",
                    "address": "http://10.0.0.9:8787",
                    "network_region": "office-lan",
                    "browser_version": "chromium 121.0",
                    "status": "warning",
                    "metadata": {"capability": "api"},
                }
            )
            runners = storage.list_probe_runners()

        self.assertEqual(first["runner_id"], "office-1")
        self.assertEqual(updated["name"], "Office Runner 2")
        self.assertEqual(updated["address"], "http://10.0.0.9:8787")
        self.assertEqual(updated["browser_version"], "chromium 121.0")
        self.assertEqual(updated["status"], "warning")
        self.assertEqual(updated["metadata"], {"capability": "api"})
        self.assertEqual({runner["runner_id"] for runner in runners}, {"local", "office-1"})

    def test_probe_runner_browser_types_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            runner = storage.upsert_probe_runner(
                {
                    "runner_id": "office-1",
                    "name": "Office Runner",
                    "address": "http://10.0.0.8:8787",
                    "network_region": "office-lan",
                    "browser_version": "chromium 120.0",
                    "installed_browser_types": ["chromium", "firefox"],
                    "available_browser_types": ["chromium"],
                    "status": "ok",
                    "metadata": {},
                }
            )

        self.assertEqual(runner["installed_browser_types"], ["chromium", "firefox"])
        self.assertEqual(runner["available_browser_types"], ["chromium"])
        self.assertTrue(runner["browser_type_status"]["chromium"]["installed"])

    def test_local_runner_update_persists_to_settings(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            updated = storage.update_probe_runner(
                "local",
                {
                    "name": "Main Node",
                    "address": "http://127.0.0.1:8787",
                    "network_region": "office",
                    "enabled": True,
                },
            )
            settings = storage.get_settings()
            storage.init_db()
            reloaded = storage.get_probe_runner("local")

        self.assertIsNotNone(updated)
        self.assertEqual(settings["local_runner_name"], "Main Node")
        self.assertEqual(settings["local_runner_address"], "http://127.0.0.1:8787")
        self.assertEqual(settings["local_runner_region"], "office")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["name"], "Main Node")
        self.assertEqual(reloaded["address"], "http://127.0.0.1:8787")
        self.assertEqual(reloaded["network_region"], "office")

    def test_managed_runner_supplied_token_is_hidden_and_rotation_invalidates_old_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            created = storage.create_probe_runner(
                {
                    "runner_id": "edge-1",
                    "name": "Edge Runner",
                    "address": "http://10.0.0.8:8787",
                    "token": "runner-secret-1",
                    "allow_insecure_http": True,
                }
            )
            public = storage.get_probe_runner("edge-1")
            listed = next(runner for runner in storage.list_probe_runners() if runner["runner_id"] == "edge-1")
            with storage._connect() as conn:
                raw_token_value = conn.execute(
                    "SELECT token_value FROM probe_runners WHERE runner_id = ?",
                    ("edge-1",),
                ).fetchone()["token_value"]
            verified = storage.verify_probe_runner_token("edge-1", "runner-secret-1")
            rotated = storage.rotate_probe_runner_token("edge-1")
            with storage._connect() as conn:
                rotated_token_value = conn.execute(
                    "SELECT token_value FROM probe_runners WHERE runner_id = ?",
                    ("edge-1",),
                ).fetchone()["token_value"]
            old_verified = storage.verify_probe_runner_token("edge-1", "runner-secret-1")
            new_verified = storage.verify_probe_runner_token("edge-1", str(rotated["token"]))

        self.assertNotIn("token", created)
        self.assertIsNotNone(public)
        self.assertNotIn("token", public)
        self.assertNotIn("token_value", public)
        self.assertTrue(public["token_set"])
        self.assertEqual(public["token_hint"], "cret-1")
        self.assertTrue(str(raw_token_value).startswith(storage.ENCRYPTED_RUNNER_TOKEN_PREFIX))
        self.assertNotIn("runner-secret-1", str(raw_token_value))
        self.assertTrue(str(rotated_token_value).startswith(storage.ENCRYPTED_RUNNER_TOKEN_PREFIX))
        self.assertNotIn(str(rotated["token"]), str(rotated_token_value))
        self.assertNotIn("token", listed)
        self.assertIsNotNone(verified)
        self.assertIsNone(old_verified)
        self.assertIsNotNone(new_verified)
        self.assertNotEqual(rotated["token"], "runner-secret-1")

    def test_init_db_migrates_legacy_plaintext_runner_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            with storage._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO probe_runners (
                        runner_id, name, address, network_region, status,
                        metadata_json, enabled, role, token_value, token_hash, token_hint,
                        connection_mode, created_at, last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-1",
                        "Legacy Runner",
                        "http://10.0.0.10:8788",
                        "edge",
                        "ok",
                        "{}",
                        1,
                        "child",
                        "pgrn_legacy-token-1234567890",
                        "",
                        "",
                        "manual",
                        storage.now_iso(),
                        storage.now_iso(),
                        storage.now_iso(),
                    ),
                )

            storage.init_db()
            with storage._connect() as conn:
                raw_token_value = conn.execute(
                    "SELECT token_value, token_hash, token_hint FROM probe_runners WHERE runner_id = ?",
                    ("legacy-1",),
                ).fetchone()
            token = storage.get_probe_runner_token("legacy-1")
            verified = storage.verify_probe_runner_token("legacy-1", "pgrn_legacy-token-1234567890")

        self.assertEqual(token, "pgrn_legacy-token-1234567890")
        self.assertTrue(str(raw_token_value["token_value"]).startswith(storage.ENCRYPTED_RUNNER_TOKEN_PREFIX))
        self.assertNotIn("pgrn_legacy-token-1234567890", str(raw_token_value["token_value"]))
        self.assertEqual(raw_token_value["token_hash"], storage.runner_token_hash("pgrn_legacy-token-1234567890"))
        self.assertEqual(raw_token_value["token_hint"], "567890")
        self.assertIsNotNone(verified)

    def test_init_db_preserves_legacy_manual_http_runner_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            storage.create_probe_runner(
                {
                    "runner_id": "legacy-http",
                    "name": "Legacy HTTP",
                    "address": "http://10.0.0.10:8788",
                    "token": "pgrn_legacy-http-token-1234567890",
                    "allow_insecure_http": True,
                }
            )
            storage.create_probe_runner(
                {
                    "runner_id": "legacy-https",
                    "name": "Legacy HTTPS",
                    "address": "https://worker.example.com:8788",
                    "token": "pgrn_legacy-https-token-1234567890",
                }
            )
            with storage._connect() as conn:
                conn.execute("ALTER TABLE probe_runners DROP COLUMN allow_insecure_http")

            storage.init_db()
            migrated_http = storage.get_probe_runner("legacy-http")
            migrated_https = storage.get_probe_runner("legacy-https")

        self.assertTrue(migrated_http["allow_insecure_http"])
        self.assertFalse(migrated_https["allow_insecure_http"])
        self.assertTrue(storage.can_schedule_runner({**migrated_http, "available": True}))

    def test_manual_http_runner_requires_explicit_transport_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            with self.assertRaisesRegex(ValueError, "明文传输"):
                storage.create_probe_runner(
                    {
                        "runner_id": "unsafe-http",
                        "name": "Unsafe HTTP",
                        "address": "http://10.0.0.11:8788",
                        "token": "pgrn_unsafe-http-token-1234567890",
                    }
                )
            secure = storage.create_probe_runner(
                {
                    "runner_id": "secure-https",
                    "name": "Secure HTTPS",
                    "address": "https://worker.example.com:8788",
                    "token": "pgrn_secure-https-token-1234567890",
                }
            )

        self.assertFalse(secure["allow_insecure_http"])

    def test_relay_provisioning_requires_session_and_worker_health_before_scheduling(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "RELAY_INTERNAL_PORT_START", 19001), patch.object(storage, "RELAY_INTERNAL_PORT_END", 19002):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            relay_token = provisioned["relay_token"]
            version = int(provisioned["relay_token_version"])
            pending = storage.get_probe_runner(runner_id)
            verified = storage.verify_probe_runner_relay_token(runner_id, relay_token, version)
            connecting = storage.mark_probe_runner_relay_connected(runner_id)
            healthy = storage.mark_probe_runner_available(runner_id, {"status": "ok", "metadata": {"source": "relay"}})

        self.assertEqual(pending["connection_mode"], "relay")
        self.assertEqual(pending["status"], "pending_deployment")
        self.assertIsNotNone(pending["deploy_command_expires_at"])
        self.assertFalse(storage.can_schedule_runner(pending))
        self.assertIsNotNone(verified)
        self.assertEqual(connecting["status"], "connecting")
        self.assertFalse(storage.can_schedule_runner(connecting))
        self.assertEqual(healthy["status"], "available")
        self.assertIsNone(healthy["deploy_command_expires_at"])
        self.assertTrue(storage.can_schedule_runner(healthy))
        self.assertEqual(healthy["address"], "http://pulseguard-relay:19001")

    def test_updating_relay_runner_is_not_schedulable_until_health_recovers(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "RELAY_INTERNAL_PORT_START", 19001), patch.object(storage, "RELAY_INTERNAL_PORT_END", 19002):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            storage.mark_probe_runner_relay_connected(runner_id, token_version=int(provisioned["relay_token_version"]))
            healthy = storage.mark_probe_runner_available(runner_id, {"status": "ok"})
            updating = storage.mark_probe_runner_updating(runner_id)
            still_updating = storage.mark_probe_runner_unavailable(runner_id)
            recovered = storage.mark_probe_runner_available(runner_id, {"status": "ok"})

        self.assertIsNotNone(healthy)
        self.assertTrue(storage.can_schedule_runner(healthy))
        self.assertEqual(updating["status"], "updating")
        self.assertFalse(updating["available"])
        self.assertFalse(storage.can_schedule_runner(updating))
        self.assertEqual(still_updating["status"], "updating")
        self.assertFalse(storage.can_schedule_runner(still_updating))
        self.assertEqual(recovered["status"], "available")
        self.assertTrue(storage.can_schedule_runner(recovered))

    def test_active_relay_runner_can_reconnect_after_deploy_command_expiration(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            relay_token = str(provisioned["relay_token"])
            version = int(provisioned["relay_token_version"])
            storage.mark_probe_runner_relay_connected(runner_id, token_version=version)
            storage.mark_probe_runner_available(runner_id, {"status": "ok"})
            expired_at = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat(timespec="seconds")
            with storage._connect() as conn:
                conn.execute(
                    """
                    UPDATE probe_runners
                    SET status = 'connecting',
                        deploy_command_expires_at = ?
                    WHERE runner_id = ?
                    """,
                    (expired_at, runner_id),
                )

            verified = storage.verify_probe_runner_relay_token(runner_id, relay_token, version)
            runner = storage.get_probe_runner(runner_id)

        self.assertIsNotNone(verified)
        self.assertIsNotNone(runner)
        assert runner is not None
        self.assertEqual(runner["status"], "connecting")
        self.assertTrue(runner["was_available"])
        self.assertTrue(runner["relay_token_set"])

    def test_relay_available_status_is_derived_from_fresh_heartbeat_and_health(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            first = storage.provision_probe_runner({"name": "Relay One", "network_region": "edge"})
            first_id = str(first["runner_id"])
            storage.mark_probe_runner_relay_connected(first_id)
            storage.mark_probe_runner_available(first_id, {"status": "ok"})
            stale_relay_seen_at = (
                datetime.now().astimezone() - timedelta(seconds=storage.RELAY_HEARTBEAT_TIMEOUT_SECONDS + 1)
            ).isoformat(timespec="seconds")
            with storage._connect() as conn:
                conn.execute(
                    "UPDATE probe_runners SET relay_last_seen_at = ? WHERE runner_id = ?",
                    (stale_relay_seen_at, first_id),
                )
            relay_stale = storage.get_probe_runner(first_id)

            second = storage.provision_probe_runner({"name": "Relay Two", "network_region": "edge"})
            second_id = str(second["runner_id"])
            storage.mark_probe_runner_relay_connected(second_id)
            storage.mark_probe_runner_available(second_id, {"status": "ok"})
            stale_worker_health_at = (
                datetime.now().astimezone() - timedelta(seconds=storage.RUNNER_HEARTBEAT_TIMEOUT_SECONDS + 1)
            ).isoformat(timespec="seconds")
            with storage._connect() as conn:
                conn.execute(
                    "UPDATE probe_runners SET worker_health_last_success_at = ? WHERE runner_id = ?",
                    (stale_worker_health_at, second_id),
                )
            health_stale = storage.get_probe_runner(second_id)

        self.assertEqual(relay_stale["status"], "unavailable")
        self.assertFalse(relay_stale["available"])
        self.assertFalse(storage.can_schedule_runner(relay_stale))
        self.assertTrue(storage.should_notify_probe_runner_unavailable(relay_stale))
        self.assertEqual(health_stale["status"], "unhealthy")
        self.assertFalse(health_stale["available"])
        self.assertFalse(storage.can_schedule_runner(health_stale))
        self.assertTrue(storage.should_notify_probe_runner_unavailable(health_stale))

    def test_relay_regenerate_keeps_pending_old_relay_token_invalid_without_rotating_worker_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            old_relay_token = provisioned["relay_token"]
            old_worker_token = provisioned["worker_token"]
            old_version = int(provisioned["relay_token_version"])
            with storage._connect() as conn:
                raw_worker_token_value = conn.execute(
                    "SELECT token_value FROM probe_runners WHERE runner_id = ?",
                    (runner_id,),
                ).fetchone()["token_value"]
            regenerated = storage.regenerate_probe_runner_provision(runner_id)
            old_verified = storage.verify_probe_runner_relay_token(runner_id, old_relay_token, old_version)
            new_verified = storage.verify_probe_runner_relay_token(
                runner_id,
                str(regenerated["relay_token"]),
                int(regenerated["relay_token_version"]),
            )

        self.assertIsNone(old_verified)
        self.assertIsNotNone(new_verified)
        self.assertTrue(str(raw_worker_token_value).startswith(storage.ENCRYPTED_RUNNER_TOKEN_PREFIX))
        self.assertNotIn(old_worker_token, str(raw_worker_token_value))
        self.assertEqual(regenerated["worker_token"], old_worker_token)
        self.assertEqual(int(regenerated["relay_token_version"]), old_version + 1)

    def test_relay_regenerate_keeps_available_runner_on_previous_token_until_new_token_connects(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            old_relay_token = str(provisioned["relay_token"])
            old_worker_token = str(provisioned["worker_token"])
            old_version = int(provisioned["relay_token_version"])
            storage.mark_probe_runner_relay_connected(runner_id, token_version=old_version)
            storage.mark_probe_runner_available(runner_id, {"status": "ok"})

            regenerated = storage.regenerate_probe_runner_provision(runner_id)
            old_verified = storage.verify_probe_runner_relay_token(runner_id, old_relay_token, old_version)
            new_verified = storage.verify_probe_runner_relay_token(
                runner_id,
                str(regenerated["relay_token"]),
                int(regenerated["relay_token_version"]),
            )
            storage.mark_probe_runner_relay_connected(runner_id, token_version=int(regenerated["relay_token_version"]))
            old_after_new_connection = storage.verify_probe_runner_relay_token(runner_id, old_relay_token, old_version)

        self.assertEqual(regenerated["status"], "available")
        self.assertTrue(regenerated["available"])
        self.assertTrue(regenerated["relay_token_rotation_pending"])
        self.assertIsNotNone(old_verified)
        self.assertIsNotNone(new_verified)
        self.assertIsNone(old_after_new_connection)
        self.assertEqual(regenerated["worker_token"], old_worker_token)
        self.assertEqual(int(regenerated["relay_token_version"]), old_version + 1)

    def test_expired_relay_rotation_invalidates_unused_current_token(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            old_relay_token = str(provisioned["relay_token"])
            old_version = int(provisioned["relay_token_version"])
            storage.mark_probe_runner_relay_connected(runner_id, token_version=old_version)
            storage.mark_probe_runner_available(runner_id, {"status": "ok"})

            regenerated = storage.regenerate_probe_runner_provision(runner_id)
            expired_at = (datetime.now().astimezone() - timedelta(seconds=1)).isoformat(timespec="seconds")
            with storage._connect() as conn:
                conn.execute(
                    """
                    UPDATE probe_runners
                    SET relay_previous_token_expires_at = ?,
                        deploy_command_expires_at = ?
                    WHERE runner_id = ?
                    """,
                    (expired_at, expired_at, runner_id),
                )
            old_verified = storage.verify_probe_runner_relay_token(runner_id, old_relay_token, old_version)
            new_verified = storage.verify_probe_runner_relay_token(
                runner_id,
                str(regenerated["relay_token"]),
                int(regenerated["relay_token_version"]),
            )
            expired_runner = storage.get_probe_runner(runner_id)

        self.assertIsNone(old_verified)
        self.assertIsNone(new_verified)
        self.assertIsNotNone(expired_runner)
        assert expired_runner is not None
        self.assertEqual(expired_runner["status"], "expired")
        self.assertFalse(expired_runner["relay_token_set"])
        self.assertFalse(expired_runner["relay_token_rotation_pending"])

    def test_deleted_relay_runner_port_is_quarantined_until_cutoff(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "RELAY_INTERNAL_PORT_START", 19001), patch.object(
            storage, "RELAY_INTERNAL_PORT_END", 19002
        ), patch.object(storage, "RELAY_PORT_QUARANTINE_SECONDS", 960):
            storage.init_db()
            first = storage.provision_probe_runner({"name": "Relay One", "network_region": "edge"})
            self.assertTrue(storage.delete_probe_runner(str(first["runner_id"])))
            second = storage.provision_probe_runner({"name": "Relay Two", "network_region": "edge"})

            with storage._connect() as conn:
                quarantined = conn.execute("SELECT port FROM relay_port_quarantine ORDER BY port").fetchall()
                old_released_at = (datetime.now().astimezone() - timedelta(seconds=961)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE relay_port_quarantine SET released_at = ? WHERE port = ?",
                    (old_released_at, int(first["allocated_internal_port"])),
                )

            third = storage.provision_probe_runner({"name": "Relay Three", "network_region": "edge"})

        self.assertEqual(int(first["allocated_internal_port"]), 19001)
        self.assertEqual([int(row["port"]) for row in quarantined], [19001])
        self.assertEqual(int(second["allocated_internal_port"]), 19002)
        self.assertEqual(int(third["allocated_internal_port"]), 19001)

    def test_disabled_relay_runner_cannot_authenticate_relay_session(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            relay_token = str(provisioned["relay_token"])
            version = int(provisioned["relay_token_version"])
            before_disable = storage.verify_probe_runner_relay_token(runner_id, relay_token, version)
            storage.update_probe_runner(runner_id, {"enabled": False})
            after_disable = storage.verify_probe_runner_relay_token(runner_id, relay_token, version)

        self.assertIsNotNone(before_disable)
        self.assertIsNone(after_disable)

    def test_relay_restart_invalidates_available_runner_for_scheduling(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            storage.mark_probe_runner_relay_connected(runner_id)
            healthy = storage.mark_probe_runner_available(runner_id, {"status": "ok"})
            changed = storage.mark_relay_runners_restarted()
            restarted = storage.get_probe_runner(runner_id)

        self.assertTrue(storage.can_schedule_runner(healthy))
        self.assertGreaterEqual(changed, 1)
        self.assertEqual(restarted["status"], "unavailable")
        self.assertIsNone(restarted["relay_last_seen_at"])
        self.assertEqual(restarted["last_disconnect_reason"], "relay server restarted")
        self.assertFalse(storage.can_schedule_runner(restarted))

    def test_relay_regeneration_suppresses_old_session_failures_during_rotation(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = provisioned["runner_id"]
            storage.mark_probe_runner_relay_connected(runner_id, token_version=int(provisioned["relay_token_version"]))
            storage.mark_probe_runner_available(runner_id, {"status": "ok"})
            regenerated = storage.regenerate_probe_runner_provision(runner_id)
            disconnected = storage.mark_probe_runner_relay_disconnected(runner_id, "old session closed")
            auth_failed = storage.mark_probe_runner_relay_auth_failed(runner_id)

        self.assertEqual(regenerated["status"], "available")
        self.assertTrue(regenerated["relay_token_rotation_pending"])
        self.assertEqual(disconnected["status"], "connecting")
        self.assertEqual(auth_failed["status"], "connecting")
        self.assertFalse(storage.should_notify_probe_runner_unavailable(disconnected or {}))
        self.assertFalse(storage.should_notify_probe_runner_unavailable(auth_failed or {}))

    def test_relay_auth_failed_after_availability_is_alertable(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            provisioned = storage.provision_probe_runner({"name": "Relay Runner", "network_region": "edge"})
            runner_id = str(provisioned["runner_id"])
            storage.mark_probe_runner_relay_connected(runner_id)
            storage.mark_probe_runner_available(runner_id, {"status": "ok"})
            auth_failed = storage.mark_probe_runner_relay_auth_failed(runner_id)

        self.assertEqual(auth_failed["status"], "auth_failed")
        self.assertFalse(auth_failed["available"])
        self.assertTrue(storage.should_notify_probe_runner_unavailable(auth_failed or {}))

    def test_relay_deploy_command_expiry_only_blocks_pre_activation_sessions(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            active = storage.provision_probe_runner({"name": "Active Relay", "network_region": "edge"})
            active_id = str(active["runner_id"])
            storage.mark_probe_runner_relay_connected(active_id)
            storage.mark_probe_runner_available(active_id, {"status": "ok"})
            pending = storage.provision_probe_runner({"name": "Pending Relay", "network_region": "edge"})
            pending_id = str(pending["runner_id"])
            expired_at = (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")
            with storage._connect() as conn:
                conn.execute(
                    "UPDATE probe_runners SET deploy_command_expires_at = ? WHERE runner_id IN (?, ?)",
                    (expired_at, active_id, pending_id),
                )

            active_verified = storage.verify_probe_runner_relay_token(
                active_id,
                str(active["relay_token"]),
                int(active["relay_token_version"]),
            )
            pending_verified = storage.verify_probe_runner_relay_token(
                pending_id,
                str(pending["relay_token"]),
                int(pending["relay_token_version"]),
            )
            pending_after_expiry = storage.get_probe_runner(pending_id)

        self.assertIsNotNone(active_verified)
        self.assertEqual(active_verified["status"], "available")
        self.assertIsNone(pending_verified)
        self.assertEqual(pending_after_expiry["status"], "expired")

    def test_relay_token_verification_rejects_invalid_runner_id(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            verified = storage.verify_probe_runner_relay_token("../bad", "pgrl_secret", 1)

        self.assertIsNone(verified)

    def test_runner_unavailable_notification_is_deduped_until_recovery(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            storage.create_probe_runner(
                {
                    "runner_id": "edge-2",
                    "name": "Edge Runner",
                    "address": "http://10.0.0.9:8787",
                    "token": "runner-secret-2",
                    "allow_insecure_http": True,
                }
            )
            unavailable = storage.mark_probe_runner_unavailable("edge-2")
            should_notify_first = storage.should_notify_probe_runner_unavailable(unavailable or {})
            notified = storage.mark_probe_runner_unavailable_notified("edge-2")
            should_notify_second = storage.should_notify_probe_runner_unavailable(notified or {})
            recovered = storage.mark_probe_runner_available(
                "edge-2",
                {"browser_version": "chromium 120", "status": "ok", "metadata": {"source": "poll"}},
            )
            unavailable_again = storage.mark_probe_runner_unavailable("edge-2")
            should_notify_after_recovery = storage.should_notify_probe_runner_unavailable(unavailable_again or {})

        self.assertTrue(should_notify_first)
        self.assertFalse(should_notify_second)
        self.assertTrue(recovered["available"])
        self.assertEqual(recovered["metadata"], {"source": "poll"})
        self.assertIsNone(recovered["unavailable_notified_at"])
        self.assertTrue(should_notify_after_recovery)

    def test_runner_cursor_round_robins_per_scope(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            first_scope = [storage.next_runner_cursor("check:1", 3) for _ in range(5)]
            second_scope = [storage.next_runner_cursor("check:2", 2) for _ in range(3)]

        self.assertEqual(first_scope, [0, 1, 2, 0, 1])
        self.assertEqual(second_scope, [0, 1, 0])


class DeploymentStorageTests(unittest.TestCase):
    def test_deployment_window_discards_interrupted_runs_on_startup(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            clear_checks()
            check = storage.create_check(api_check_data("Deploy API"))
            completed = storage.create_run(check)
            storage.finish_run(int(completed["id"]), run_payload("ok"))
            pending = storage.create_run(check, "pending")
            running = storage.create_run(check, "pending")
            storage.start_run(int(running["id"]))

            state = storage.start_deployment_window("unit-test")
            storage.init_db()
            remaining_runs = storage.list_runs(limit=10)
            deployment = storage.get_deployment_state()

        self.assertTrue(state["active"])
        self.assertTrue(deployment["active"])
        self.assertEqual({run["id"] for run in remaining_runs}, {completed["id"]})
        self.assertNotIn(pending["id"], {run["id"] for run in remaining_runs})
        self.assertNotIn(running["id"], {run["id"] for run in remaining_runs})

    def test_deployment_window_state_round_trips(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            started = storage.start_deployment_window("unit-test")
            active = storage.get_deployment_state()
            finished = storage.finish_deployment_window()
            inactive = storage.get_deployment_state()

        self.assertTrue(started["active"])
        self.assertTrue(active["active"])
        self.assertEqual(active["reason"], "unit-test")
        self.assertFalse(finished["active"])
        self.assertFalse(inactive["active"])
        self.assertIsNotNone(inactive["finished_at"])


class SettingsVariableStorageTests(unittest.TestCase):
    def test_public_settings_masks_secret_environment_variable_value_but_reports_value_set(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            storage.update_settings(
                {
                    "read_only_token": "read-token-secret-123",
                    "environment_variables": [
                        {"id": "host", "name": "API_HOST", "value": "https://api.example.com", "secret": False},
                        {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True},
                    ]
                }
            )

            raw = storage.get_settings()
            public = storage.get_public_settings()

        raw_variables = {item["id"]: item for item in raw["environment_variables"]}
        public_variables = {item["id"]: item for item in public["environment_variables"]}

        self.assertEqual(raw_variables["token"]["value"], "token-secret-123")
        self.assertEqual(raw["read_only_token"], "read-token-secret-123")
        self.assertEqual(public["read_only_token"], "")
        self.assertTrue(public["read_only_token_set"])
        self.assertEqual(public_variables["host"]["value"], "https://api.example.com")
        self.assertEqual(public_variables["token"]["value"], "")
        self.assertTrue(public_variables["token"]["value_set"])

    def test_secret_environment_variable_empty_update_preserves_existing_value_and_value_clear_removes_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            storage.update_settings(
                {
                    "environment_variables": [
                        {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True},
                    ]
                }
            )
            storage.update_settings(
                {
                    "environment_variables": [
                        {"id": "token", "name": "SERVICE_TOKEN", "value": "", "secret": True},
                    ]
                }
            )
            preserved = storage.get_settings()["environment_variables"][0]

            storage.update_settings(
                {
                    "environment_variables": [
                        {"id": "token", "name": "SERVICE_TOKEN", "value": "", "value_clear": True, "secret": True},
                    ]
                }
            )
            cleared = storage.get_settings()["environment_variables"][0]
            public = storage.get_public_settings()["environment_variables"][0]

        self.assertEqual(preserved["value"], "token-secret-123")
        self.assertEqual(cleared["value"], "")
        self.assertFalse(public["value_set"])


def run_payload(status: str, error_message: str | None = None, duration_ms: int = 10) -> dict[str, object]:
    return {
        "status": status,
        "finished_at": storage.now_iso(),
        "duration_ms": duration_ms,
        "error_message": error_message,
        "error_stack": None,
        "logs": "",
        "screenshot_path": None,
        "trace_path": None,
        "response_path": None,
        "request_snapshot": None,
        "response_snapshot": None,
    }


def set_run_started_at(run_id: int, started_at: datetime) -> None:
    with storage._connect() as conn:
        conn.execute(
            "UPDATE runs SET started_at = ?, created_at = ? WHERE id = ?",
            (started_at.isoformat(timespec="seconds"), started_at.isoformat(timespec="seconds"), run_id),
        )


def insert_trend_run(
    check_id: int,
    check_type: str,
    status: str,
    started_at: datetime,
    duration_ms: int | None,
    failure_kind: str | None = None,
) -> None:
    timestamp = started_at.isoformat(timespec="seconds")
    finished_at = None if status in {"pending", "running"} else timestamp
    with storage._connect() as conn:
        project_row = conn.execute("SELECT project_id FROM checks WHERE id = ?", (check_id,)).fetchone()
        project_id = int(project_row["project_id"]) if project_row else storage.default_project_id()
        conn.execute(
            """
            INSERT INTO runs (
                project_id, check_id, check_name, check_type, status, started_at, finished_at,
                duration_ms, failure_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                check_id,
                "Trend UI" if check_type == "ui" else "Trend API",
                check_type,
                status,
                timestamp,
                finished_at,
                duration_ms,
                failure_kind,
                timestamp,
            ),
        )


def clear_checks() -> None:
    with storage._connect() as conn:
        conn.execute("DELETE FROM check_status")
        conn.execute("DELETE FROM runs")
        conn.execute("DELETE FROM checks")


def api_check_data(name: str, alert_policy_json: str | None = None, tags: str = "", enabled: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": name,
        "type": "api",
        "enabled": enabled,
        "interval_seconds": 300,
        "timeout_ms": 10000,
        "entry_url": "https://example.com",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "script": "",
        "tags": tags,
    }
    if alert_policy_json is not None:
        payload["alert_policy_json"] = alert_policy_json
    return payload


def ui_check_data(name: str, tags: str = "") -> dict[str, object]:
    return {
        "name": name,
        "type": "ui",
        "enabled": True,
        "interval_seconds": 300,
        "timeout_ms": 15000,
        "entry_url": "https://example.com",
        "viewport_mode": "web",
        "method": "",
        "headers_json": "{}",
        "body": "",
        "assertions_json": "[]",
        "setup_script": "",
        "script": "",
        "tags": tags,
    }


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
