from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app import storage
from backend.app.monitoring import next_health_state, run_metadata


class MonitoringStateTests(unittest.TestCase):
    def test_only_draft_runs_do_not_affect_health(self) -> None:
        for trigger in ("scheduled", "confirm-recovery", "manual", "manual-batch", "rerun:12", "cli"):
            with self.subTest(trigger=trigger):
                metadata = run_metadata(trigger)
                self.assertTrue(metadata["affects_health"])
                self.assertEqual(metadata["observation_kind"], "observation")

        draft = run_metadata("draft-debug")
        self.assertFalse(draft["affects_health"])
        self.assertEqual(draft["observation_kind"], "draft")

    def test_target_failures_require_confirmation(self) -> None:
        settings = {"api_failure_confirmation_count": 2, "recovery_confirmation_count": 2}
        first = next_health_state(None, {"status": "failed", "failure_kind": "target", "check_type": "api"}, settings)
        second = next_health_state(
            {"monitor_status": first["current_status"], "consecutive_failures": first["consecutive_failures"]},
            {"status": "failed", "failure_kind": "target", "check_type": "api"},
            settings,
        )
        self.assertEqual(first["current_status"], "suspected_failing")
        self.assertEqual(second["current_status"], "failing")

    def test_ui_failures_use_ui_confirmation_threshold(self) -> None:
        settings = {"ui_failure_confirmation_count": 3, "recovery_confirmation_count": 2}
        first = next_health_state(None, {"status": "failed", "failure_kind": "target", "check_type": "ui"}, settings)
        second = next_health_state(
            {"monitor_status": first["current_status"], "consecutive_failures": first["consecutive_failures"]},
            {"status": "failed", "failure_kind": "target", "check_type": "ui"},
            settings,
        )
        third = next_health_state(
            {"monitor_status": second["current_status"], "consecutive_failures": second["consecutive_failures"]},
            {"status": "failed", "failure_kind": "target", "check_type": "ui"},
            settings,
        )
        self.assertEqual(first["current_status"], "suspected_failing")
        self.assertEqual(second["current_status"], "suspected_failing")
        self.assertEqual(third["current_status"], "failing")

    def test_runner_failure_becomes_unknown(self) -> None:
        state = next_health_state(
            {"monitor_status": "healthy", "consecutive_failures": 0},
            {"status": "failed", "failure_kind": "runner"},
            {"api_failure_confirmation_count": 1},
        )
        self.assertEqual(state["current_status"], "unknown")
        self.assertEqual(state["consecutive_failures"], 0)

    def test_first_success_becomes_healthy_without_recovery_confirmation(self) -> None:
        state = next_health_state(
            {"monitor_status": "unknown", "consecutive_successes": 0},
            {"status": "ok", "failure_kind": "none", "check_type": "api", "trigger": "scheduled"},
            {"recovery_confirmation_count": 2},
        )
        self.assertEqual(state["current_status"], "healthy")

    def test_confirm_recovery_is_explicit_and_immediate(self) -> None:
        state = next_health_state(
            {"monitor_status": "failing", "consecutive_failures": 3},
            {"status": "ok", "failure_kind": "none", "trigger": "confirm-recovery"},
            {"recovery_confirmation_count": 3},
        )
        self.assertEqual(state["current_status"], "healthy")

    def test_recovery_stays_failing_until_confirmation_completes(self) -> None:
        settings = {"recovery_confirmation_count": 2}
        first = next_health_state(
            {"monitor_status": "failing", "consecutive_failures": 3},
            {"status": "ok", "failure_kind": "none", "trigger": "scheduled"},
            settings,
        )
        second = next_health_state(
            {"monitor_status": first["current_status"], "consecutive_successes": first["consecutive_successes"]},
            {"status": "ok", "failure_kind": "none", "trigger": "scheduled"},
            settings,
        )
        self.assertEqual(first["current_status"], "suspected_recovery")
        self.assertEqual(second["previous_status"], "suspected_recovery")
        self.assertEqual(second["current_status"], "healthy")


class MonitoringStorageTests(unittest.TestCase):
    def test_backup_and_restore_sources_remain_distinct_when_clock_repeats(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, 'DB_PATH', Path(temp_dir) / 'pulseguard.db'
        ), patch.object(storage, 'BACKUPS_DIR', Path(temp_dir) / 'backups'):
            storage.init_db()
            frozen = datetime.now().astimezone()
            with patch.object(storage, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = frozen
                first = storage.create_database_backup()
                added = storage.create_check({
                    'name': 'Distinct restore marker', 'type': 'api', 'enabled': True,
                    'interval_seconds': 300, 'timeout_ms': 10000,
                    'entry_url': 'https://example.com/restore-marker', 'method': 'GET',
                    'headers_json': '{}', 'body': '', 'assertions_json': '[]',
                    'setup_script': '', 'script': 'async def check(ctx):\n    pass\n', 'tags': '',
                })
                second = storage.create_database_backup()
                self.assertNotEqual(first['filename'], second['filename'])
                restored = storage.restore_database_backup(first['filename'])
            self.assertNotEqual(first['filename'], restored['safety_backup']['filename'])
            self.assertIsNone(storage.get_check(added['id']))

    def test_run_pagination_filters_observation_kind_and_backup_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "BACKUPS_DIR", Path(temp_dir) / "backups"):
            storage.init_db()
            check = storage.list_checks("api")[0]
            observation = storage.create_run({**check, "_run": run_metadata("scheduled")}, "skipped")
            draft = storage.create_run({**check, "id": 0, "_run": run_metadata("draft-debug")}, "skipped")

            page = storage.list_runs_page({"observation_kind": "observation"}, page=1, page_size=10)
            backup = storage.create_database_backup()
            added = storage.create_check(
                {
                    "name": "Restore marker",
                    "type": "api",
                    "enabled": True,
                    "interval_seconds": 300,
                    "timeout_ms": 10000,
                    "entry_url": "https://example.com/restore-marker",
                    "method": "GET",
                    "headers_json": "{}",
                    "body": "",
                    "assertions_json": "[]",
                    "setup_script": "",
                    "script": "async def check(ctx):\n    pass\n",
                    "tags": "",
                }
            )
            storage.restore_database_backup(backup["filename"])
            restored_marker = storage.get_check(int(added["id"]))
            backups = storage.list_database_backups()

        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["id"], observation["id"])
        self.assertNotEqual(page["items"][0]["id"], draft["id"])
        self.assertIsNone(restored_marker)
        self.assertGreaterEqual(len(backups), 2)

    def test_database_backup_retention_keeps_new_backup_when_manual_names_sort_first(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "BACKUPS_DIR", Path(temp_dir) / "backups"):
            storage.init_db()
            storage.update_settings({"database_backup_retention": 1})
            storage.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            (storage.BACKUPS_DIR / "pulseguard-manual-predeploy-99999999.db").write_bytes(b"older manual backup")

            backup = storage.create_database_backup()

            self.assertTrue((Path(temp_dir) / "backups" / backup["filename"]).is_file())
            self.assertGreater(backup["size_bytes"], 0)


def _seed_trend_run(check_id: int, check_type: str, started_at: datetime, duration_ms: int = 300) -> None:
    timestamp = started_at.isoformat(timespec="seconds")
    with storage._connect() as conn:
        project_id = int(conn.execute("SELECT project_id FROM checks WHERE id = ?", (check_id,)).fetchone()[0])
        cursor = conn.execute(
            """
            INSERT INTO runs (
                project_id, check_id, check_name, check_type, status, started_at, finished_at,
                duration_ms, failure_kind, observation_kind, affects_health, created_at
            ) VALUES (?, ?, ?, ?, 'ok', ?, ?, ?, 'none', 'observation', 1, ?)
            """,
            (
                project_id,
                check_id,
                "Trend UI" if check_type == "ui" else "Trend API",
                check_type,
                timestamp,
                timestamp,
                duration_ms,
                timestamp,
            ),
        )
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        assert row is not None
        storage._record_trend_rollups(conn, row)
        conn.execute("UPDATE runs SET trend_recorded_at = ? WHERE id = ?", (storage.now_iso(), int(cursor.lastrowid)))


def _seed_hour_runs(check_id: int, check_type: str, anchor: datetime, hours: list[int]) -> None:
    for hour in hours:
        started_at = anchor.replace(hour=hour, minute=0, second=0, microsecond=0)
        _seed_trend_run(check_id, check_type, started_at)


class MonitoringHourWindowTests(unittest.TestCase):
    def test_parse_hour_window_validates_inputs(self) -> None:
        self.assertIsNone(storage._parse_hour_window(None, None))
        self.assertIsNone(storage._parse_hour_window("", ""))
        with self.assertRaises(ValueError):
            storage._parse_hour_window("02:00", None)
        with self.assertRaises(ValueError):
            storage._parse_hour_window(None, "04:00")
        with self.assertRaises(ValueError):
            storage._parse_hour_window("invalid", "04:00")
        with self.assertRaises(ValueError):
            storage._parse_hour_window("02:00", "02:00")
        window = storage._parse_hour_window("02:00", "04:00")
        self.assertEqual(window, (time(2, 0), time(4, 0)))

    def test_hour_in_window_handles_cross_midnight(self) -> None:
        normal = (time(2, 0), time(4, 0))
        self.assertTrue(storage._hour_in_window(time(2, 0), normal))
        self.assertTrue(storage._hour_in_window(time(3, 30), normal))
        self.assertFalse(storage._hour_in_window(time(4, 0), normal))
        self.assertFalse(storage._hour_in_window(time(1, 30), normal))

        cross = (time(22, 0), time(2, 0))
        self.assertTrue(storage._hour_in_window(time(22, 0), cross))
        self.assertTrue(storage._hour_in_window(time(23, 30), cross))
        self.assertTrue(storage._hour_in_window(time(0, 30), cross))
        self.assertTrue(storage._hour_in_window(time(1, 0), cross))
        self.assertFalse(storage._hour_in_window(time(2, 0), cross))
        self.assertFalse(storage._hour_in_window(time(12, 0), cross))

    def test_monitoring_trends_filters_to_hour_window(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "BACKUPS_DIR", Path(temp_dir) / "backups"):
            storage.init_db()
            check = storage.list_checks("api")[0]
            check_id = int(check["id"])
            anchor = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
            _seed_hour_runs(check_id, "api", anchor, [1, 2, 3, 4, 5])

            full = storage.list_monitoring_trends(period="7d", check_type="api")
            target_full = next(item for item in full["tasks"]["items"] if item["check_id"] == check_id)
            self.assertEqual(target_full["success_count"], 5)

            scoped = storage.list_monitoring_trends(
                period="7d", check_type="api", hour_start="02:00", hour_end="04:00"
            )
            target_scoped = next(item for item in scoped["tasks"]["items"] if item["check_id"] == check_id)
            self.assertEqual(target_scoped["success_count"], 2)
            self.assertEqual(scoped["hour_start"], "02:00")
            self.assertEqual(scoped["hour_end"], "04:00")
            summary = scoped["summaries"][0]
            self.assertEqual(summary["success_count"], 2)

    def test_monitoring_trends_minute_hour_window_uses_five_minute_rollups(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "BACKUPS_DIR", Path(temp_dir) / "backups"):
            storage.init_db()
            check = storage.list_checks("api")[0]
            check_id = int(check["id"])
            anchor = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
            for hour, minute in [(2, 0), (2, 15), (2, 30), (3, 30), (3, 45), (4, 0)]:
                started_at = anchor.replace(hour=hour, minute=minute)
                _seed_trend_run(check_id, "api", started_at)

            scoped = storage.list_monitoring_trends(
                period="7d", check_type="api", hour_start="02:15", hour_end="03:45"
            )
            target_scoped = next(item for item in scoped["tasks"]["items"] if item["check_id"] == check_id)

        self.assertEqual(target_scoped["success_count"], 3)
        self.assertEqual(scoped["summaries"][0]["success_count"], 3)

    def test_monitoring_trends_hour_window_cross_midnight(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "BACKUPS_DIR", Path(temp_dir) / "backups"):
            storage.init_db()
            check = storage.list_checks("api")[0]
            check_id = int(check["id"])
            anchor = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
            _seed_hour_runs(check_id, "api", anchor, [21, 22, 23, 0, 1, 2, 3])

            scoped = storage.list_monitoring_trends(
                period="7d", check_type="api", hour_start="22:00", hour_end="02:00"
            )
            target_scoped = next(item for item in scoped["tasks"]["items"] if item["check_id"] == check_id)
            # 22, 23, 0, 1 hit the window (4 buckets); 21, 2, 3 are excluded
            self.assertEqual(target_scoped["success_count"], 4)

    def test_monitoring_trends_invalid_hour_window(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ), patch.object(storage, "BACKUPS_DIR", Path(temp_dir) / "backups"):
            storage.init_db()
            with self.assertRaises(ValueError):
                storage.list_monitoring_trends(period="24h", hour_start="02:00")
