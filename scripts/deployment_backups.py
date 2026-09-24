"""Create self-contained deployment recovery points; prune only after acceptance.

Call create while instance writers are stopped. Call complete only after the
deployment's health and data checks pass, with database_backup_retention.
This module uses only the standard library so it also works on Docker hosts.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time


FORMAT = "pulseguard-deployment-backup-v1"
ID_PATTERN = re.compile(r"[a-f0-9]{32}")


def _target(root: Path, backup_id: str) -> Path:
    if not ID_PATTERN.fullmatch(backup_id):
        raise ValueError("backup ID must be 32 lowercase hexadecimal characters")
    if root.is_symlink() or root.resolve() != root or root == Path(root.anchor):
        raise ValueError("backup root must be an absolute, non-symlink directory")
    target = root / ("managed-" + backup_id)
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError("backup target escapes the backup root")
    return target


@contextmanager
def _locked(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".retention.lock"
    # Fail closed on concurrent work or a lock left by a killed process.
    lock.open("x").close()
    try:
        yield
    finally:
        lock.unlink()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_manifest(target: Path, manifest: dict) -> None:
    temporary = target / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(target / "manifest.json")


def create(root: Path, backup_id: str, app_root: Path, *, include_reports: bool = False) -> Path:
    target = _target(root, backup_id)
    app_root = app_root.resolve(strict=True)
    if root.is_relative_to(app_root) or app_root.is_relative_to(root):
        raise ValueError("deployment backups must be outside the application tree")
    with _locked(root):
        target.mkdir(mode=0o700)
        database = app_root / "data" / "pulseguard.db"
        if database.exists():
            with tempfile.TemporaryDirectory(prefix="snapshot-", dir=target) as temporary:
                snapshot = Path(temporary) / "database.db"
                with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
                    with closing(sqlite3.connect(snapshot)) as destination:
                        source.backup(destination)
                        if destination.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise ValueError("deployment database backup failed integrity check")
                expected = _digest(snapshot)
                compressed = target / "database.db.gz"
                with snapshot.open("rb") as source, gzip.open(compressed, "wb", compresslevel=1) as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                with gzip.open(compressed, "rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                        raise ValueError("compressed database backup failed verification")

        def source_filter(info: tarfile.TarInfo):
            parts = Path(info.name).parts
            if parts[0] in {"data", "reports", ".venv", ".git", ".tmp"}:
                return None
            if any(part in {"node_modules", "__pycache__", ".pytest_cache"} for part in parts):
                return None
            return info

        with tarfile.open(target / "source.tar.gz", "w:gz") as archive:
            for path in sorted(app_root.iterdir()):
                archive.add(path, arcname=path.name, filter=source_filter)

        def runtime_filter(info: tarfile.TarInfo):
            parts = Path(info.name).parts
            if len(parts) > 1 and parts[0] == "data" and parts[1] in {
                "backups", "pulseguard.db", "pulseguard.db-wal", "pulseguard.db-shm",
            }:
                return None
            return info

        # Preserve runner keys and Relay state with the matching database.
        with tarfile.open(target / "runtime.tar.gz", "w:gz") as archive:
            for name in (["data", "reports"] if include_reports else ["data"]):
                path = app_root / name
                if path.exists():
                    archive.add(path, arcname=name, filter=runtime_filter)
        files = {p.name: {"sha256": _digest(p), "bytes": p.stat().st_size} for p in target.iterdir()}
        # Wall clocks can repeat (notably on Windows) or move backwards. Creation
        # is serialized by the root lock, so preserve a strict order in manifests.
        created_ns = time.time_ns()
        for previous in root.glob("managed-*"):
            if previous == target:
                continue
            try:
                created_ns = max(created_ns, _manifest(previous)["created_ns"] + 1)
            except (OSError, ValueError, TypeError):
                continue
        _write_manifest(target, {
            "format": FORMAT, "id": backup_id, "created_ns": created_ns,
            "state": "pending", "files": files,
        })
    return target


def _manifest(target: Path) -> dict:
    if target.is_symlink() or any(p.is_symlink() for p in target.rglob("*")):
        raise ValueError("refusing a backup containing symlinks")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("id") != target.name.removeprefix("managed-")
            or type(manifest.get("created_ns")) is not int
            or manifest.get("state") not in {"pending", "complete"}):
        raise ValueError("invalid deployment backup manifest")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not {"source.tar.gz", "runtime.tar.gz"}.issubset(files) or set(files) - {
        "database.db.gz", "source.tar.gz", "runtime.tar.gz",
    }:
        raise ValueError("invalid deployment backup files")
    if {p.name for p in target.iterdir()} != set(files) | {"manifest.json"}:
        raise ValueError("unexpected files in deployment backup")
    return manifest


def complete(root: Path, backup_id: str, retention: int, *, dry_run: bool = False) -> list[str]:
    if not 1 <= retention <= 30:
        raise ValueError("retention must be between 1 and 30")
    current = _target(root, backup_id)
    with _locked(root):
        manifest = _manifest(current)
        # No historical backup is removed unless the accepted recovery point is intact.
        for name, expected in manifest["files"].items():
            path = current / name
            if path.stat().st_size != expected["bytes"] or _digest(path) != expected["sha256"]:
                raise ValueError("deployment backup verification failed: " + name)
        candidates = []
        for path in root.glob("managed-*"):
            if path == current:
                continue
            if not ID_PATTERN.fullmatch(path.name.removeprefix("managed-")):
                continue
            try:
                other = _manifest(path)
            except (OSError, ValueError, TypeError):
                # Unknown and failed/partial operations need explicit operator review.
                continue
            if other["state"] == "complete":
                candidates.append((other["created_ns"], path))
        candidates.sort(reverse=True)
        if any(created > manifest["created_ns"] for created, _ in candidates):
            raise ValueError("a newer deployment has completed; review the older recovery point explicitly")
        removed = [path for _, path in candidates[retention - 1:]]
        if not dry_run:
            manifest["state"] = "complete"
            _write_manifest(current, manifest)
            for path in removed:
                _target(root, path.name.removeprefix("managed-"))
                _manifest(path)
                shutil.rmtree(path)
        return [str(path) for path in removed]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "complete"])
    parser.add_argument("--root", type=Path, default=Path("/opt/pulseguard-backups"))
    parser.add_argument("--id", required=True)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--include-reports", action="store_true")
    parser.add_argument("--retention", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.action == "create":
        if args.app_root is None or args.dry_run:
            parser.error("create requires --app-root and does not support --dry-run")
        print(create(args.root, args.id, args.app_root, include_reports=args.include_reports))
    else:
        if args.retention is None:
            parser.error("complete requires --retention from database_backup_retention")
        print(json.dumps({"removed": complete(args.root, args.id, args.retention, dry_run=args.dry_run)}))


if __name__ == "__main__":
    main()
