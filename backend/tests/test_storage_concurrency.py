from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend.app import storage


class StorageConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "snapshot # test.db"
        self.backups = Path(self.temp.name) / "backups"
        self.db_patch = patch.object(storage, "DB_PATH", self.db_path)
        self.backup_patch = patch.object(storage, "BACKUPS_DIR", self.backups)
        self.db_patch.start()
        self.backup_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.backup_patch.stop)
        storage.init_db()

    def test_readers_finish_while_writer_transaction_is_still_open(self) -> None:
        writer_ready = threading.Event()
        release_writer = threading.Event()

        def write() -> None:
            with storage._connect() as conn:
                conn.execute("UPDATE settings SET value = '99' WHERE key = 'run_retention_days'")
                writer_ready.set()
                self.assertTrue(release_writer.wait(5))

        with ThreadPoolExecutor(max_workers=3) as pool:
            writer = pool.submit(write)
            try:
                self.assertTrue(writer_ready.wait(5))
                settings = pool.submit(storage.get_settings).result(timeout=2)
                page = pool.submit(storage.list_runs_page).result(timeout=2)
                self.assertNotEqual(settings["run_retention_days"], 99)
                self.assertEqual(page["total"], 0)
                self.assertFalse(writer.done())
            finally:
                release_writer.set()
            writer.result(timeout=5)
        self.assertEqual(storage.get_settings()["run_retention_days"], 99)

    def test_read_transaction_keeps_snapshot_across_concurrent_commit(self) -> None:
        with storage._connect(read_only=True) as conn:
            before = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(storage.create_project, "Concurrent project").result(timeout=2)
            after = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(len(storage.list_projects()), before + 1)

    def test_read_connection_is_uri_read_only_even_if_query_only_is_disabled(self) -> None:
        with storage._connect(read_only=True) as conn:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            conn.execute("PRAGMA query_only=OFF")
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute("DELETE FROM projects")
        missing = Path(self.temp.name) / "not-created.db"
        with patch.object(storage, "DB_PATH", missing):
            with self.assertRaises(sqlite3.OperationalError):
                with storage._connect(read_only=True):
                    self.fail("A read connection must not create a missing database")
        self.assertFalse(missing.exists())

    def test_connection_rolls_back_and_closes_after_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with storage._connect() as conn:
                conn.execute("INSERT INTO projects(name, created_at, updated_at) VALUES ('Rolled back', '', '')")
                raise RuntimeError("abort")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        self.assertNotIn("Rolled back", [project["name"] for project in storage.list_projects()])

    def test_health_file_size_tolerates_missing_wal_but_does_not_hide_access_errors(self) -> None:
        missing = Path(self.temp.name) / "not-present-wal"
        self.assertEqual(storage._database_file_size(missing), 0)
        self.assertEqual(storage._database_file_size(missing, fallback=123), 123)
        with patch.object(Path, "stat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                storage._database_file_size(missing)

    def test_writers_serialize_read_modify_write_cursor_updates(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: storage.next_runner_cursor("concurrent", 100), range(40)))
        self.assertEqual(sorted(results), list(range(40)))

    def test_maintenance_drains_existing_access_and_blocks_new_access(self) -> None:
        gate = storage._DatabaseAccess()
        initial_ready = threading.Event()
        nested_allowed = threading.Event()
        nested_done = threading.Event()
        initial_release = threading.Event()
        maintenance_ready = threading.Event()
        maintenance_release = threading.Event()
        new_access_ready = threading.Event()

        def initial_access() -> None:
            with gate.shared():
                initial_ready.set()
                self.assertTrue(nested_allowed.wait(5))
                with gate.shared():
                    nested_done.set()
                self.assertTrue(initial_release.wait(5))

        def maintenance() -> None:
            with gate.exclusive():
                with gate.exclusive(), gate.shared():
                    maintenance_ready.set()
                    self.assertTrue(maintenance_release.wait(5))

        def new_access() -> None:
            with gate.shared():
                new_access_ready.set()

        with ThreadPoolExecutor(max_workers=3) as pool:
            initial = pool.submit(initial_access)
            try:
                self.assertTrue(initial_ready.wait(5))
                exclusive = pool.submit(maintenance)
                with gate._condition:
                    self.assertTrue(gate._condition.wait_for(lambda: gate._waiting_exclusive == 1, timeout=5))
                newcomer = pool.submit(new_access)
                nested_allowed.set()
                self.assertTrue(nested_done.wait(5), "Nested access must not deadlock behind maintenance")
                self.assertFalse(maintenance_ready.is_set())
                self.assertFalse(new_access_ready.is_set())
                initial_release.set()
                self.assertTrue(maintenance_ready.wait(5))
                self.assertFalse(new_access_ready.is_set())
                maintenance_release.set()
                initial.result(timeout=5)
                exclusive.result(timeout=5)
                newcomer.result(timeout=5)
            finally:
                nested_allowed.set()
                initial_release.set()
                maintenance_release.set()
        self.assertTrue(new_access_ready.is_set())

    def test_maintenance_upgrade_is_rejected_instead_of_deadlocking(self) -> None:
        gate = storage._DatabaseAccess()
        with gate.shared():
            with self.assertRaisesRegex(RuntimeError, "维护"):
                with gate.exclusive():
                    self.fail("Shared access cannot upgrade to exclusive access")

    def test_restore_keeps_safety_backup_copy_and_initialization_exclusive(self) -> None:
        backup = storage.create_database_backup()
        added = storage.create_project("Before restore")
        init_ready = threading.Event()
        init_release = threading.Event()
        real_init = storage.init_db
        real_copy = storage._copy_database
        stages: list[str] = []

        def copy(source: Path, destination: Path) -> None:
            self.assertEqual(storage._DATABASE_ACCESS._exclusive_owner, threading.get_ident())
            stages.append("copy")
            real_copy(source, destination)

        def initialize() -> None:
            self.assertEqual(storage._DATABASE_ACCESS._exclusive_owner, threading.get_ident())
            stages.append("init")
            init_ready.set()
            self.assertTrue(init_release.wait(5))
            real_init()

        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(storage, "_copy_database", side_effect=copy), patch.object(
            storage, "init_db", side_effect=initialize
        ):
            restoring = pool.submit(storage.restore_database_backup, backup["filename"])
            try:
                self.assertTrue(init_ready.wait(5))
                reader = pool.submit(storage.get_project, added["id"])
                self.assertFalse(reader.done())
                init_release.set()
                result = restoring.result(timeout=5)
                self.assertIsNone(reader.result(timeout=5))
            finally:
                init_release.set()
        self.assertEqual(stages, ["copy", "copy", "init"])
        safety = self.backups / result["safety_backup"]["filename"]
        conn = sqlite3.connect(f"{safety.as_uri()}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("SELECT name FROM projects WHERE id = ?", (added["id"],)).fetchone()[0], "Before restore")
        finally:
            conn.close()

    def test_restore_rejects_corrupt_backup_before_changing_live_database(self) -> None:
        self.backups.mkdir(exist_ok=True)
        (self.backups / "pulseguard-corrupt.db").write_bytes(b"not a database")
        before = storage.list_projects()
        with self.assertRaisesRegex(ValueError, "校验失败"):
            storage.restore_database_backup("pulseguard-corrupt.db")
        self.assertEqual(storage.list_projects(), before)

    def test_failed_restore_initialization_restores_original_database_before_readers_resume(self) -> None:
        backup = storage.create_database_backup()
        added = storage.create_project("Keep after failed restore")

        def failed_init() -> None:
            with storage._connect() as conn:
                conn.execute("UPDATE projects SET name = 'Partial migration'")
            raise RuntimeError("migration failed")

        with patch.object(storage, "init_db", side_effect=failed_init):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                storage.restore_database_backup(backup["filename"])
        self.assertEqual(storage.get_project(added["id"])["name"], "Keep after failed restore")
        self.assertNotIn("Partial migration", [project["name"] for project in storage.list_projects()])

    def test_stale_scan_without_candidates_does_not_acquire_writer_lock(self) -> None:
        class ForbiddenWriter:
            def __enter__(self) -> None:
                raise AssertionError("Unchanged stale scan acquired writer lock")

            def __exit__(self, *args: object) -> None:
                pass

        with patch.object(storage, "_LOCK", ForbiddenWriter()):
            self.assertEqual(storage.refresh_stale_statuses(), 0)

    def test_failed_restore_and_failed_recovery_report_explicit_manual_recovery_error(self) -> None:
        backup = storage.create_database_backup()
        added = storage.create_project("Original database marker")
        real_copy = storage._copy_database
        calls = 0

        def fail_recovery(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("recovery disk failure")
            real_copy(source, destination)

        with patch.object(storage, "_copy_database", side_effect=fail_recovery), patch.object(
            storage, "init_db", side_effect=RuntimeError("migration failed")
        ):
            with self.assertRaises(storage.DatabaseRestoreRecoveryError) as result:
                storage.restore_database_backup(backup["filename"])
        self.assertIn("安全备份 pulseguard-", str(result.exception))
        self.assertIn("人工恢复", str(result.exception))
        self.assertNotIn(str(self.backups), str(result.exception))
        self.assertIsInstance(result.exception.__cause__, OSError)
        self.assertIsNone(storage.get_project(added["id"]), "The error must not imply that recovery succeeded")

    def test_stale_update_rechecks_candidate_after_concurrent_success(self) -> None:
        check = storage.list_checks()[0]
        old = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
        with storage._connect() as conn:
            conn.execute(
                """INSERT INTO check_status(check_id, current_status, monitor_status, last_scheduled_at, updated_at)
                   VALUES (?, 'healthy', 'healthy', ?, '')""",
                (check["id"], old),
            )
        read_complete = threading.Event()
        success_complete = threading.Event()
        real_connect = storage._connect

        @contextmanager
        def intercepted_connect(*, read_only: bool = False):
            if not read_only:
                read_complete.set()
                self.assertTrue(success_complete.wait(5))
            with real_connect(read_only=read_only) as conn:
                yield conn

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(storage, "_connect", intercepted_connect):
            refreshing = pool.submit(storage.refresh_stale_statuses, check["id"])
            try:
                self.assertTrue(read_complete.wait(5))
                with real_connect() as conn:
                    conn.execute("UPDATE check_status SET last_scheduled_at = ? WHERE check_id = ?", (storage.now_iso(), check["id"]))
            finally:
                success_complete.set()
            self.assertEqual(refreshing.result(timeout=5), 0)
        self.assertEqual(storage.get_check(check["id"], refresh_stale=False)["monitor_status"], "healthy")

    def test_stale_update_preserves_disabled_invalid_dates_and_project_scope(self) -> None:
        checks = storage.list_checks()
        disabled = checks[0]
        invalid = checks[1]
        other_project = storage.create_project("Other stale project")
        scoped = storage.create_check({**checks[0], "name": "Scoped stale"}, project_id=other_project["id"])
        old = (datetime.now().astimezone() - timedelta(days=2)).isoformat()
        with storage._connect() as conn:
            conn.execute("UPDATE checks SET enabled = 0 WHERE id = ?", (disabled["id"],))
            for check, timestamp in ((disabled, old), (invalid, "invalid"), (scoped, old)):
                conn.execute(
                    """INSERT INTO check_status(check_id, current_status, monitor_status, last_scheduled_at, updated_at)
                       VALUES (?, 'healthy', 'healthy', ?, '')""",
                    (check["id"], timestamp),
                )
        self.assertEqual(storage.refresh_stale_statuses(project_id=disabled["project_id"]), 0)
        self.assertIsNone(storage.get_check(scoped["id"], project_id=disabled["project_id"]))
        self.assertEqual(storage.get_check(scoped["id"], refresh_stale=False)["monitor_status"], "healthy")
        self.assertEqual(storage.refresh_stale_statuses(project_id=other_project["id"]), 1)
        self.assertEqual(storage.get_check(scoped["id"], refresh_stale=False)["monitor_status"], "stale")
        self.assertEqual(storage.refresh_stale_statuses(project_id=other_project["id"]), 0)


if __name__ == "__main__":
    unittest.main()
