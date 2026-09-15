from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.app import storage


class BackupRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(storage, "DB_PATH", root / "instance.db"))
        self.enterContext(patch.object(storage, "BACKUPS_DIR", root / "backups"))
        storage.init_db()

    def test_new_backup_counts_toward_limit(self) -> None:
        storage.update_settings({"database_backup_retention": 2})
        created = [storage.create_database_backup()["filename"] for _ in range(5)]
        self.assertEqual({b["filename"] for b in storage.list_database_backups()}, set(created[-2:]))

    def test_one_keeps_only_new_backup_even_with_manual_names(self) -> None:
        storage.update_settings({"database_backup_retention": 1})
        storage.BACKUPS_DIR.mkdir()
        (storage.BACKUPS_DIR / "pulseguard-z-manual.db").write_bytes(b"manual")
        newest = storage.create_database_backup()["filename"]
        self.assertEqual([b["filename"] for b in storage.list_database_backups()], [newest])

    def test_keeps_recent_files_by_time_and_leaves_unmanaged_files(self) -> None:
        storage.update_settings({"database_backup_retention": 2})
        storage.BACKUPS_DIR.mkdir()
        old = storage.BACKUPS_DIR / "pulseguard-z-old.db"
        recent = storage.BACKUPS_DIR / "pulseguard-a-recent.db"
        for p, timestamp in [(old, 100), (recent, 200)]:
            p.write_bytes(b"backup")
            os.utime(p, (timestamp, timestamp))
        unmanaged = storage.BACKUPS_DIR / "migration-safety.db"
        unmanaged.write_bytes(b"keep")
        storage.create_database_backup()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unmanaged.exists())

    def test_restore_preserves_source_and_safety_at_limit_one(self) -> None:
        storage.update_settings({"database_backup_retention": 1})
        source = storage.create_database_backup()["filename"]
        result = storage.restore_database_backup(source)
        self.assertEqual(
            {b["filename"] for b in storage.list_database_backups()},
            {source, result["safety_backup"]["filename"]},
        )
        newest = storage.create_database_backup()["filename"]
        self.assertEqual([b["filename"] for b in storage.list_database_backups()], [newest])

    def test_failed_restore_keeps_both_recovery_files(self) -> None:
        storage.update_settings({"database_backup_retention": 1})
        source = storage.create_database_backup()["filename"]
        with patch.object(storage, "init_db", side_effect=RuntimeError("migration failed")):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                storage.restore_database_backup(source)
        self.assertEqual(len(storage.list_database_backups()), 2)
