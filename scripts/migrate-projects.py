from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app import storage  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database(source_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"pulseguard-pre-project-migration-{timestamp}.db"
    source = sqlite3.connect(source_path, timeout=30)
    destination = sqlite3.connect(target, timeout=30)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preflight or apply the one-time PulseGuard project-isolation migration."
    )
    parser.add_argument("--apply", action="store_true", help="Apply the migration; default is read-only preflight.")
    parser.add_argument("--db", type=Path, default=Path(storage.DB_PATH), help="Database path to inspect or migrate.")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Directory for the pre-migration SQLite backup.")
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve()
    storage.DB_PATH = db_path
    before = storage.inspect_project_migration()
    result: dict[str, object] = {
        "database": str(db_path),
        "mode": "apply" if args.apply else "preflight",
        "before": before,
    }
    if not db_path.exists() and args.apply:
        result["error"] = "数据库不存在；新数据库无需迁移，直接启动新版应用即可"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not before.get("required"):
        result["after"] = before
        result["applied"] = False
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    before_sha256 = _sha256(db_path)
    backup_dir = (args.backup_dir or (db_path.parent / "backups")).expanduser().resolve()
    backup_path = _backup_database(db_path, backup_dir)
    backup_sha256 = _sha256(backup_path)
    backup_conn = sqlite3.connect(backup_path, timeout=30)
    try:
        integrity = str(backup_conn.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        backup_conn.close()
    if integrity.lower() != "ok":
        result["error"] = f"迁移前备份完整性检查失败：{integrity}"
        result["backup"] = str(backup_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3

    try:
        migration = storage.migrate_projects(apply=True)
    except Exception as exc:
        result.update(
            {
                "error": str(exc),
                "backup": str(backup_path),
                "before_sha256": before_sha256,
                "backup_sha256": backup_sha256,
            }
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 4

    result.update(
        {
            "applied": True,
            "backup": str(backup_path),
            "before_sha256": before_sha256,
            "backup_sha256": backup_sha256,
            "after_sha256": _sha256(db_path),
            "after": migration,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
