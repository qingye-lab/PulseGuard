from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from .config import DB_PATH, RELAY_CERT_FILE, RELAY_KEY_FILE


def ensure_relay_certificate() -> tuple[Path, Path]:
    cert = RELAY_CERT_FILE
    key = RELAY_KEY_FILE
    if cert.exists() and key.exists():
        return cert, key
    if _has_existing_relay_runners():
        raise RuntimeError("relay certificate/key is missing while relay runners exist; restore data/relay certificate files from backup")
    cert.parent.mkdir(parents=True, exist_ok=True)
    key.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "3650",
            "-subj",
            "/CN=PulseGuard Relay",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        key.chmod(0o600)
    except OSError:
        pass
    return cert, key


def _has_existing_relay_runners() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        with closing(sqlite3.connect(f"{DB_PATH.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'probe_runners'"
            ).fetchone()
            if not table:
                return False
            row = conn.execute("SELECT 1 FROM probe_runners WHERE connection_mode = 'relay' LIMIT 1").fetchone()
            return row is not None
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot inspect relay runners before creating relay certificate: {exc}") from exc


def relay_fingerprint() -> str:
    cert, _ = ensure_relay_certificate()
    data = cert.read_text(encoding="utf-8")
    body = "".join(line.strip() for line in data.splitlines() if "CERTIFICATE" not in line)
    import base64

    der = base64.b64decode(body)
    return hashlib.sha256(der).hexdigest()
