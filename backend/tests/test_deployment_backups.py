from __future__ import annotations

from contextlib import closing
import gzip
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import uuid

from scripts import deployment_backups as backups


class DeploymentBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.directory / "backups"
        self.app = self.directory / "app"
        (self.app / "data" / "backups").mkdir(parents=True)
        (self.app / "backend").mkdir()
        (self.app / "reports").mkdir()
        (self.app / "backend" / "main.py").write_text("old source", encoding="utf-8")
        (self.app / "data" / "runner-token.key").write_text("test-key", encoding="utf-8")
        (self.app / "data" / "backups" / "manual.db").write_bytes(b"do not recursively archive")
        (self.app / "reports" / "run.json").write_text("{}", encoding="utf-8")
        with closing(sqlite3.connect(self.app / "data" / "pulseguard.db")) as conn:
            conn.execute("CREATE TABLE settings (key TEXT, value TEXT)")
            conn.execute("INSERT INTO settings VALUES ('database_backup_retention', '2')")
            conn.commit()

    def create(self, **kwargs) -> Path:
        return backups.create(self.root, uuid.uuid4().hex, self.app, **kwargs)

    def test_large_snapshot_is_not_scanned_separately_for_hashing(self) -> None:
        with closing(sqlite3.connect(self.app / 'data' / 'pulseguard.db')) as conn:
            conn.execute('CREATE TABLE blobs (data BLOB)')
            conn.execute('INSERT INTO blobs VALUES (?)', (b'x' * (2 * 1024 * 1024),))
            conn.commit()
        with patch.object(backups, '_digest', wraps=backups._digest) as digest:
            path = self.create()
        self.assertNotIn('database.db', [call.args[0].name for call in digest.call_args_list])
        restored = self.directory / 'large-restored.db'
        with gzip.open(path / 'database.db.gz', 'rb') as source:
            restored.write_bytes(source.read())
        with closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(conn.execute('SELECT length(data) FROM blobs').fetchone()[0], 2 * 1024 * 1024)

    def complete(self, path: Path, retention: int = 2, **kwargs) -> list[str]:
        return backups.complete(self.root, path.name.removeprefix("managed-"), retention, **kwargs)

    def test_snapshot_callback_resumes_writes_without_changing_frozen_recovery_point(self) -> None:
        observed = []

        def resume(snapshot):
            observed.append(snapshot)
            self.assertTrue(snapshot.is_file())
            self.assertTrue((snapshot.parent.parent / 'source.tar.gz').is_file())
            self.assertTrue((snapshot.parent.parent / 'runtime.tar.gz').is_file())
            self.assertFalse((snapshot.parent.parent / 'manifest.json').exists())
            (self.app / 'backend/main.py').write_text('resumed source')
            (self.app / 'data/runner-token.key').write_text('resumed key')
            with closing(sqlite3.connect(self.app / 'data/pulseguard.db')) as conn:
                conn.execute("INSERT INTO settings VALUES ('after_snapshot', 'new')")
                conn.commit()

        path = self.create(on_snapshot=resume)
        self.assertEqual(len(observed), 1)
        with tarfile.open(path / 'source.tar.gz') as archive:
            self.assertEqual(archive.extractfile('backend/main.py').read(), b'old source')
        with tarfile.open(path / 'runtime.tar.gz') as archive:
            self.assertEqual(archive.extractfile('data/runner-token.key').read(), b'test-key')
        restored = self.directory / 'callback-restored.db'
        with gzip.open(path / 'database.db.gz', 'rb') as source:
            restored.write_bytes(source.read())
        with closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM settings').fetchone()[0], 1)
        self.complete(path)

    def test_snapshot_callback_failure_cannot_create_accepted_backup_or_prune_history(self) -> None:
        previous = self.create()
        self.complete(previous)

        def fail(_snapshot):
            raise RuntimeError('resume failed')

        with self.assertRaisesRegex(RuntimeError, 'resume failed'):
            self.create(on_snapshot=fail)
        self.assertTrue(previous.is_dir())
        self.assertEqual(len(list(self.root.glob('managed-*/manifest.json'))), 1)

    def test_database_and_runtime_round_trip_without_recursive_backups(self) -> None:
        (self.app / ".venv").mkdir()
        (self.app / ".venv" / "python").write_bytes(b"exclude")
        path = self.create(include_reports=True)
        self.assertFalse((path / "database.db").exists())
        restored = self.directory / "restored.db"
        with gzip.open(path / "database.db.gz", "rb") as stream:
            restored.write_bytes(stream.read())
        with closing(sqlite3.connect(restored)) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(conn.execute("SELECT value FROM settings").fetchone(), ("2",))
        with tarfile.open(path / "source.tar.gz") as archive:
            self.assertEqual(archive.extractfile("backend/main.py").read(), b"old source")
            self.assertFalse(any(n.startswith(("data", "reports", ".venv")) for n in archive.getnames()))
        with tarfile.open(path / "runtime.tar.gz") as archive:
            self.assertEqual(archive.extractfile("data/runner-token.key").read(), b"test-key")
            self.assertIn("reports/run.json", archive.getnames())
            self.assertNotIn("data/backups", archive.getnames())
            self.assertNotIn("data/pulseguard.db", archive.getnames())

    def test_successful_deployments_keep_exactly_two_complete_recovery_points(self) -> None:
        created = []
        for _ in range(5):
            path = self.create()
            self.complete(path)
            created.append(path)
        self.assertEqual(set(self.root.glob("managed-*")), set(created[-2:]))
        self.assertEqual(self.complete(created[-1]), [])  # safe retry

    def test_pending_and_unknown_backups_are_not_pruned(self) -> None:
        old = self.create()
        self.complete(old)
        pending = self.create()
        legacy = self.root / "source-before-deploy.tar.gz"
        legacy.write_bytes(b"unmanaged")
        unknown = self.root / ("managed-" + uuid.uuid4().hex)
        unknown.mkdir()
        (unknown / "manifest.json").write_text("{}", encoding="utf-8")
        new = self.create()
        self.complete(new, retention=1)
        self.assertFalse(old.exists())
        self.assertTrue(pending.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue(unknown.exists())

    def test_corrupt_new_backup_does_not_remove_previous_recovery_point(self) -> None:
        old = self.create()
        self.complete(old)
        new = self.create()
        (new / "database.db.gz").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            self.complete(new, retention=1)
        self.assertTrue(old.exists())
        self.assertEqual(json.loads((new / "manifest.json").read_text())["state"], "pending")

    def test_failed_create_does_not_prune_and_releases_lock(self) -> None:
        old = self.create()
        self.complete(old)
        with patch.object(backups.gzip, "open", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.create()
        self.assertTrue(old.exists())
        self.assertFalse((self.root / ".retention.lock").exists())

    def test_dry_run_and_invalid_retention_do_not_mutate(self) -> None:
        old = self.create()
        self.complete(old)
        new = self.create()
        self.assertEqual(self.complete(new, retention=1, dry_run=True), [str(old)])
        self.assertTrue(old.exists())
        self.assertEqual(json.loads((new / "manifest.json").read_text())["state"], "pending")
        for value in [0, 31]:
            with self.assertRaises(ValueError):
                self.complete(new, retention=value)
        self.assertTrue(old.exists())

    def test_out_of_order_completion_cannot_delete_newer_recovery_point(self) -> None:
        old = self.create()
        new = self.create()
        self.complete(new, retention=1)
        with self.assertRaisesRegex(ValueError, "newer deployment"):
            self.complete(old, retention=1)
        self.assertTrue(new.exists())

    def test_repeated_or_backwards_clock_cannot_invert_recovery_point_order(self) -> None:
        for values in ((100, 100), (100, 50)):
            with self.subTest(values=values), patch.object(backups.time, "time_ns", side_effect=values):
                old = self.create()
                new = self.create()
            old_stamp = json.loads((old / "manifest.json").read_text())["created_ns"]
            new_stamp = json.loads((new / "manifest.json").read_text())["created_ns"]
            self.assertGreater(new_stamp, old_stamp)
            self.complete(new, retention=1)
            with self.assertRaisesRegex(ValueError, "newer deployment"):
                self.complete(old, retention=1)
            self.assertTrue(new.exists())

    def test_locked_root_and_escaping_paths_fail_closed(self) -> None:
        old = self.create()
        self.complete(old)
        lock = self.root / ".retention.lock"
        lock.touch()
        with self.assertRaises(FileExistsError):
            self.complete(old)
        self.assertTrue(lock.exists())
        lock.unlink()
        with self.assertRaises(ValueError):
            backups.complete(self.root, "../../app", 1)
        with self.assertRaisesRegex(ValueError, "outside"):
            backups.create(self.app / "data" / "backups", uuid.uuid4().hex, self.app)
        self.assertTrue((self.app / "data" / "pulseguard.db").is_file())

    def test_unexpected_file_blocks_pruning_current_backup(self) -> None:
        path = self.create()
        (path / "user-data.txt").write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected files"):
            self.complete(path)
        self.assertTrue(path.exists())

    def test_symlink_target_is_not_followed(self) -> None:
        self.root.mkdir()
        backup_id = uuid.uuid4().hex
        link = self.root / ("managed-" + backup_id)
        try:
            link.symlink_to(self.app, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation not available on this host")
        with self.assertRaisesRegex(ValueError, "escapes"):
            backups.complete(self.root, backup_id, 1)
        self.assertTrue((self.app / "data" / "pulseguard.db").is_file())
