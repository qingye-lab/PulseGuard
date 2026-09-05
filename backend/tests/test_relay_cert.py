from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import relay_cert


class RelayCertificateTests(unittest.TestCase):
    def test_runner_inspection_uses_read_only_connection_and_closes_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "relay # inspection.db"
            created = sqlite3.connect(db_path)
            try:
                created.execute("CREATE TABLE probe_runners (runner_id TEXT, connection_mode TEXT)")
                created.commit()
            finally:
                created.close()
            inspected = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            with patch.object(relay_cert, "DB_PATH", db_path), patch.object(
                relay_cert.sqlite3, "connect", return_value=inspected
            ) as connect:
                self.assertFalse(relay_cert._has_existing_relay_runners())
            connect.assert_called_once_with(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.ProgrammingError):
                inspected.execute("SELECT 1")

    def test_existing_relay_runners_block_silent_certificate_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "pulseguard.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE probe_runners (runner_id TEXT, connection_mode TEXT)")
                conn.execute("INSERT INTO probe_runners (runner_id, connection_mode) VALUES (?, ?)", ("edge-1", "relay"))

            with patch.object(relay_cert, "DB_PATH", db_path), patch.object(
                relay_cert, "RELAY_CERT_FILE", Path(temp_dir) / "relay.crt"
            ), patch.object(relay_cert, "RELAY_KEY_FILE", Path(temp_dir) / "relay.key"):
                with self.assertRaisesRegex(RuntimeError, "restore data/relay certificate files"):
                    relay_cert.ensure_relay_certificate()

    def test_existing_certificate_pair_is_reused_even_when_relay_runners_exist(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "pulseguard.db"
            cert_path = Path(temp_dir) / "relay.crt"
            key_path = Path(temp_dir) / "relay.key"
            cert_path.write_text("cert", encoding="utf-8")
            key_path.write_text("key", encoding="utf-8")
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE probe_runners (runner_id TEXT, connection_mode TEXT)")
                conn.execute("INSERT INTO probe_runners (runner_id, connection_mode) VALUES (?, ?)", ("edge-1", "relay"))

            with patch.object(relay_cert, "DB_PATH", db_path), patch.object(
                relay_cert, "RELAY_CERT_FILE", cert_path
            ), patch.object(relay_cert, "RELAY_KEY_FILE", key_path):
                self.assertEqual(relay_cert.ensure_relay_certificate(), (cert_path, key_path))
