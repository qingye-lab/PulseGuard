from __future__ import annotations

import json
import math
import os
import re
import secrets
import hashlib
import sqlite3
import threading
import time as time_module
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .artifacts import cleanup_old_artifacts, delete_artifact_paths
from .browser_types import (
    DEFAULT_BROWSER_TYPE,
    browser_type_status,
    normalize_browser_selection_mode,
    normalize_browser_types,
    normalized_browser_settings,
)
from .config import (
    BACKUPS_DIR,
    DB_PATH,
    RELAY_DEPLOY_COMMAND_TTL_HOURS,
    RELAY_INTERNAL_HOST,
    RELAY_INTERNAL_PORT_END,
    RELAY_INTERNAL_PORT_START,
    RELAY_PORT_QUARANTINE_SECONDS,
    TREND_BACKFILL_ON_STARTUP,
    ensure_runtime_dirs,
)
from .defaults import DEFAULT_SETTINGS, DEMO_CHECKS
from .monitoring import HEALTH_STATES, next_health_state, run_metadata
from .schemas import normalize_settings_values
from .variables import is_sensitive_variable_name


_LOCK = threading.RLock()
_TAG_SPLIT_PATTERN = re.compile(r"[,\s]+")
LOCAL_RUNNER_ID = "local"
RUNNER_HEARTBEAT_TIMEOUT_SECONDS = 120
RUNNER_ROLES = {"local", "child"}
RUNNER_SELECTION_MODES = {"selected_parallel", "round_robin_all"}
DEPLOYMENT_STATE_KEY = "deployment_window"
RUNNER_CONNECTION_MODES = {"manual", "relay"}
RELAY_PRE_ACTIVATION_STATUSES = {"pending_deployment", "expired", "connecting"}
RELAY_HEARTBEAT_TIMEOUT_SECONDS = 60
ENCRYPTED_RUNNER_TOKEN_PREFIX = "fernet:"
RUN_SUMMARY_COLUMNS = (
    "id",
    "check_id",
    "check_name",
    "check_type",
    "status",
    "started_at",
    "finished_at",
    "duration_ms",
    "error_message",
    "screenshot_path",
    "trace_path",
    "response_path",
    "failure_fingerprint",
    "deduplicated_count",
    "runner_id",
    "runner_name",
    "runner_address",
    "runner_region",
    "runner_browser_version",
    "browser_type",
    "failure_kind",
    "notification_status",
    "notification_channel",
    "notification_error",
    "notification_sent_at",
    "trigger",
    "observation_kind",
    "affects_health",
    "run_group_id",
    "created_at",
)
TREND_GRANULARITIES: tuple[tuple[str, int], ...] = (("5m", 300), ("1h", 3600), ("1d", 86400))
TREND_GRANULARITY_SECONDS = dict(TREND_GRANULARITIES)
TREND_LATENCY_BUCKETS_MS = (
    50,
    100,
    200,
    300,
    500,
    750,
    1000,
    1500,
    2000,
    3000,
    5000,
    7500,
    10000,
    12000,
    15000,
    18000,
    20000,
    25000,
    30000,
    60000,
    120000,
    300000,
)
TREND_PERIOD_SECONDS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
TREND_DISPLAY_BUCKET_SECONDS = (300, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000)
TREND_SUMMARY_CHECK_IDS = {"ui": -1, "api": -2}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_db() -> None:
    ensure_runtime_dirs()
    with _connect() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA busy_timeout=30000;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS checks (
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
                alert_policy_json TEXT NOT NULL DEFAULT '{}',
                runner_selection_mode TEXT NOT NULL DEFAULT 'selected_parallel',
                runner_ids_json TEXT NOT NULL DEFAULT '["local"]',
                browser_selection_mode TEXT NOT NULL DEFAULT 'selected_parallel',
                browser_types_json TEXT NOT NULL DEFAULT '["chromium"]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id INTEGER NOT NULL,
                check_name TEXT NOT NULL,
                check_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_ms INTEGER,
                error_message TEXT,
                error_stack TEXT,
                logs TEXT,
                screenshot_path TEXT,
                trace_path TEXT,
                response_path TEXT,
                failure_fingerprint TEXT,
                deduplicated_count INTEGER NOT NULL DEFAULT 0,
                request_snapshot TEXT,
                response_snapshot TEXT,
                runner_id TEXT,
                runner_name TEXT,
                runner_address TEXT,
                runner_region TEXT,
                runner_browser_version TEXT,
                browser_type TEXT,
                failure_kind TEXT,
                notification_status TEXT,
                notification_channel TEXT,
                notification_error TEXT,
                notification_sent_at TEXT,
                trigger TEXT NOT NULL DEFAULT 'legacy',
                observation_kind TEXT NOT NULL DEFAULT 'observation',
                affects_health INTEGER NOT NULL DEFAULT 1,
                run_group_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS check_status (
                check_id INTEGER PRIMARY KEY,
                current_status TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT,
                last_failed_at TEXT,
                last_run_at TEXT,
                last_run_id INTEGER,
                last_error TEXT,
                last_notified_at TEXT,
                monitor_status TEXT NOT NULL DEFAULT 'unknown',
                consecutive_successes INTEGER NOT NULL DEFAULT 0,
                last_scheduled_at TEXT,
                last_scheduled_run_id INTEGER,
                last_state_changed_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS heartbeats (
                key TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'ok',
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                entity_name TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS check_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                archive_date TEXT NOT NULL,
                check_type TEXT NOT NULL,
                status TEXT NOT NULL,
                run_count INTEGER NOT NULL DEFAULT 0,
                duration_sum_ms INTEGER NOT NULL DEFAULT 0,
                duration_sample_count INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(archive_date, check_type, status)
            );

            CREATE TABLE IF NOT EXISTS trend_rollups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id INTEGER NOT NULL,
                check_type TEXT NOT NULL,
                bucket_granularity TEXT NOT NULL,
                bucket_start TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                duration_sum_ms INTEGER NOT NULL DEFAULT 0,
                avg_duration_ms INTEGER,
                p95_duration_ms INTEGER,
                p99_duration_ms INTEGER,
                latency_histogram_json TEXT NOT NULL DEFAULT '{}',
                duration_samples_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                UNIQUE(check_id, bucket_granularity, bucket_start)
            );

            CREATE TABLE IF NOT EXISTS probe_runners (
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
                connection_mode TEXT NOT NULL DEFAULT 'manual',
                allow_insecure_http INTEGER NOT NULL DEFAULT 0,
                relay_token_hash TEXT NOT NULL DEFAULT '',
                relay_token_hint TEXT NOT NULL DEFAULT '',
                relay_token_version INTEGER NOT NULL DEFAULT 0,
                relay_previous_token_hash TEXT NOT NULL DEFAULT '',
                relay_previous_token_hint TEXT NOT NULL DEFAULT '',
                relay_previous_token_version INTEGER NOT NULL DEFAULT 0,
                relay_previous_token_expires_at TEXT,
                allocated_internal_port INTEGER,
                deploy_command_expires_at TEXT,
                relay_last_seen_at TEXT,
                worker_health_last_success_at TEXT,
                last_disconnect_reason TEXT NOT NULL DEFAULT '',
                was_available INTEGER NOT NULL DEFAULT 0,
                unavailable_since TEXT,
                unavailable_notified_at TEXT,
                created_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS relay_port_quarantine (
                port INTEGER PRIMARY KEY,
                released_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runner_cursors (
                scope TEXT PRIMARY KEY,
                cursor INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS anomaly_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at TEXT NOT NULL,
                resolved_at TEXT,
                first_run_id INTEGER NOT NULL,
                last_run_id INTEGER NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 1,
                failure_kind TEXT NOT NULL DEFAULT 'target',
                summary TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS schema_versions (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_checks_type ON checks(type);
            CREATE INDEX IF NOT EXISTS idx_runs_check_id ON runs(check_id);
            CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_type_status ON runs(check_type, status);
            CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_check_versions_check_id ON check_versions(check_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_run_archives_date ON run_archives(archive_date DESC);
            CREATE INDEX IF NOT EXISTS idx_trend_rollups_bucket ON trend_rollups(bucket_granularity, bucket_start);
            CREATE INDEX IF NOT EXISTS idx_trend_rollups_type_bucket ON trend_rollups(check_type, bucket_granularity, bucket_start);
            CREATE INDEX IF NOT EXISTS idx_probe_runners_region ON probe_runners(network_region, status);
            CREATE INDEX IF NOT EXISTS idx_anomaly_cycles_check_status ON anomaly_cycles(check_id, status, opened_at DESC);
            """
        )
        _ensure_check_columns(conn)
        _ensure_run_columns(conn)
        _ensure_trend_rollup_columns(conn)
        _ensure_check_status_columns(conn)
        _ensure_probe_runner_columns(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_probe_runners_relay_port ON probe_runners(allocated_internal_port)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_observation ON runs(affects_health, started_at DESC)")
        conn.execute("INSERT OR IGNORE INTO schema_versions(version, applied_at) VALUES (1, ?)", (now_iso(),))
        _ensure_default_settings(conn)
        _migrate_read_only_tokens(conn)
        _ensure_local_probe_runner(conn)
        _mark_interrupted_runs(conn)
        _seed_demo_checks(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_health_started_check ON runs(affects_health, started_at DESC, check_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_trend_cover ON runs(affects_health, check_type, status, started_at DESC, check_id, failure_kind, duration_ms)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runs_business_failures_started
            ON runs(started_at DESC, id DESC)
            WHERE check_id > 0
              AND affects_health = 1
              AND status IN ('failed', 'timeout')
              AND (failure_kind IS NULL OR failure_kind = 'target')
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_runner_id ON runs(runner_id, started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_group_id ON runs(run_group_id, started_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_check_id_desc ON runs(check_id, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_failure_fingerprint ON runs(check_id, failure_fingerprint, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_observation_kind_started ON runs(observation_kind, started_at DESC, id DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_observation_kind_check_started ON runs(observation_kind, check_id, started_at DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_page_filter_started ON runs(observation_kind, check_type, status, started_at DESC, id DESC)"
        )
        if TREND_BACKFILL_ON_STARTUP:
            _backfill_trend_rollups(conn)


def list_checks(check_type: str | None = None, enabled_only: bool = False, refresh_stale: bool = True) -> list[dict[str, Any]]:
    if refresh_stale:
        refresh_stale_statuses()
    sql = """
        SELECT c.*, s.current_status, s.monitor_status, s.consecutive_failures,
               s.consecutive_successes, s.last_success_at, s.last_failed_at,
               s.last_run_at, s.last_run_id, s.last_error, s.last_scheduled_at,
               s.last_scheduled_run_id, s.last_state_changed_at,
               r.duration_ms AS last_duration_ms
        FROM checks c
        LEFT JOIN check_status s ON s.check_id = c.id
        LEFT JOIN runs r ON r.id = s.last_run_id
    """
    clauses: list[str] = []
    params: list[Any] = []
    if check_type:
        clauses.append("c.type = ?")
        params.append(check_type)
    if enabled_only:
        clauses.append("c.enabled = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY c.updated_at DESC, c.id DESC"
    with _LOCK, _connect() as conn:
        return [_normalize_check(row) for row in conn.execute(sql, params).fetchall()]


def check_tag_set(tags: str | None) -> set[str]:
    return {item.strip().lower() for item in _TAG_SPLIT_PATTERN.split(tags or "") if item.strip()}


def select_checks_for_batch(check_type: str | None = None, tag: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    checks = list_checks(check_type, enabled_only=enabled_only)
    normalized_tag = (tag or "").strip().lower()
    if not normalized_tag:
        return checks
    return [check for check in checks if normalized_tag in check_tag_set(check.get("tags"))]


def get_check(check_id: int, refresh_stale: bool = True) -> dict[str, Any] | None:
    if refresh_stale:
        refresh_stale_statuses(check_id)
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, s.current_status, s.monitor_status, s.consecutive_failures,
                   s.consecutive_successes, s.last_success_at, s.last_failed_at,
                   s.last_run_at, s.last_run_id, s.last_error, s.last_scheduled_at,
                   s.last_scheduled_run_id, s.last_state_changed_at,
                   r.duration_ms AS last_duration_ms
            FROM checks c
            LEFT JOIN check_status s ON s.check_id = c.id
            LEFT JOIN runs r ON r.id = s.last_run_id
            WHERE c.id = ?
            """,
            (check_id,),
        ).fetchone()
    return _normalize_check(row) if row else None


def create_check(data: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO checks (
                name, type, enabled, interval_seconds, timeout_ms, entry_url, viewport_mode, method,
                headers_json, body, assertions_json, setup_script, script, tags, alert_policy_json,
                runner_selection_mode, runner_ids_json, browser_selection_mode, browser_types_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"].strip(),
                data["type"],
                1 if data.get("enabled", True) else 0,
                int(data.get("interval_seconds") or 300),
                int(data.get("timeout_ms") or 15000),
                data.get("entry_url", "").strip(),
                data.get("viewport_mode") or "web",
                (data.get("method") or "").upper(),
                data.get("headers_json") or "{}",
                data.get("body") or "",
                data.get("assertions_json") or "[]",
                data.get("setup_script") or "",
                data.get("script") or "",
                data.get("tags") or "",
                data.get("alert_policy_json") or "{}",
                _normalize_runner_selection_mode(data.get("runner_selection_mode")),
                json.dumps(_normalize_runner_ids(data.get("runner_ids") or data.get("runner_ids_json")), ensure_ascii=False),
                _normalize_browser_selection_mode(data.get("browser_selection_mode")),
                json.dumps(_normalize_check_browser_types(data), ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        check_id = int(cursor.lastrowid)
    return get_check(check_id)  # type: ignore[return-value]


def update_check(check_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE checks
            SET name = ?, type = ?, enabled = ?, interval_seconds = ?, timeout_ms = ?,
                entry_url = ?, viewport_mode = ?, method = ?, headers_json = ?, body = ?, assertions_json = ?, setup_script = ?,
                script = ?, tags = ?, alert_policy_json = ?, runner_selection_mode = ?, runner_ids_json = ?,
                browser_selection_mode = ?, browser_types_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                data["name"].strip(),
                data["type"],
                1 if data.get("enabled", True) else 0,
                int(data.get("interval_seconds") or 300),
                int(data.get("timeout_ms") or 15000),
                data.get("entry_url", "").strip(),
                data.get("viewport_mode") or "web",
                (data.get("method") or "").upper(),
                data.get("headers_json") or "{}",
                data.get("body") or "",
                data.get("assertions_json") or "[]",
                data.get("setup_script") or "",
                data.get("script") or "",
                data.get("tags") or "",
                data.get("alert_policy_json") or "{}",
                _normalize_runner_selection_mode(data.get("runner_selection_mode")),
                json.dumps(_normalize_runner_ids(data.get("runner_ids") or data.get("runner_ids_json")), ensure_ascii=False),
                _normalize_browser_selection_mode(data.get("browser_selection_mode")),
                json.dumps(_normalize_check_browser_types(data), ensure_ascii=False),
                timestamp,
                check_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
    return get_check(check_id)


def delete_check(check_id: int) -> bool:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM check_status WHERE check_id = ?", (check_id,))
        cursor = conn.execute("DELETE FROM checks WHERE id = ?", (check_id,))
        return cursor.rowcount > 0


def set_check_enabled(check_id: int, enabled: bool) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            "UPDATE checks SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now_iso(), check_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_check(check_id)


def batch_set_check_enabled(check_ids: list[int], enabled: bool) -> int:
    ids = _clean_check_ids(check_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            f"UPDATE checks SET enabled = ?, updated_at = ? WHERE id IN ({placeholders})",
            [1 if enabled else 0, now_iso(), *ids],
        )
        return int(cursor.rowcount)


def batch_update_check_interval(check_ids: list[int], interval_seconds: int) -> int:
    ids = _clean_check_ids(check_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            f"UPDATE checks SET interval_seconds = ?, updated_at = ? WHERE id IN ({placeholders})",
            [int(interval_seconds), now_iso(), *ids],
        )
        return int(cursor.rowcount)


def _clean_check_ids(check_ids: list[int]) -> list[int]:
    cleaned: list[int] = []
    seen: set[int] = set()
    for raw in check_ids:
        check_id = int(raw)
        if check_id <= 0 or check_id in seen:
            continue
        seen.add(check_id)
        cleaned.append(check_id)
    return cleaned


def record_heartbeat(key: str, status: str = "ok", message: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    heartbeat_key = key.strip()
    timestamp = now_iso()
    payload_json = json.dumps(payload or {}, ensure_ascii=False)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO heartbeats(key, status, message, payload_json, received_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                status = excluded.status,
                message = excluded.message,
                payload_json = excluded.payload_json,
                received_at = excluded.received_at,
                updated_at = excluded.updated_at
            """,
            (heartbeat_key, status, message, payload_json, timestamp, timestamp),
        )
    return get_heartbeat(heartbeat_key)  # type: ignore[return-value]


def get_heartbeat(key: str) -> dict[str, Any] | None:
    heartbeat_key = key.strip()
    if not heartbeat_key:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM heartbeats WHERE key = ?", (heartbeat_key,)).fetchone()
    return _normalize_heartbeat(row) if row else None


def get_deployment_state() -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        return _read_deployment_state(conn)


def start_deployment_window(reason: str = "docker-deploy") -> dict[str, Any]:
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        previous = _read_deployment_state(conn)
        started_at = previous.get("started_at") if previous.get("active") else timestamp
        state = {
            "active": True,
            "reason": str(reason or "docker-deploy").strip() or "docker-deploy",
            "started_at": started_at,
            "finished_at": None,
            "updated_at": timestamp,
        }
        _write_runtime_state(conn, DEPLOYMENT_STATE_KEY, state, timestamp)
        return state


def finish_deployment_window() -> dict[str, Any]:
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        previous = _read_deployment_state(conn)
        state = {
            "active": False,
            "reason": previous.get("reason") or "",
            "started_at": previous.get("started_at"),
            "finished_at": timestamp,
            "updated_at": timestamp,
        }
        _write_runtime_state(conn, DEPLOYMENT_STATE_KEY, state, timestamp)
        return state


def is_deployment_window_active() -> bool:
    return bool(get_deployment_state().get("active"))


def discard_incomplete_runs() -> int:
    with _LOCK, _connect() as conn:
        return _discard_incomplete_runs(conn)


def discard_incomplete_run(run_id: int) -> bool:
    with _LOCK, _connect() as conn:
        conn.execute("UPDATE check_status SET last_run_id = NULL WHERE last_run_id = ?", (run_id,))
        conn.execute("UPDATE check_status SET last_scheduled_run_id = NULL WHERE last_scheduled_run_id = ?", (run_id,))
        cursor = conn.execute(
            "DELETE FROM runs WHERE id = ? AND status IN ('pending', 'running')",
            (run_id,),
        )
        return cursor.rowcount > 0


def record_audit_event(
    action: str,
    entity_type: str,
    entity_id: int | str | None = None,
    entity_name: str = "",
    summary: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO audit_events(action, entity_type, entity_id, entity_name, summary, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                entity_name,
                summary,
                json.dumps(payload or {}, ensure_ascii=False),
                timestamp,
            ),
        )
        event_id = int(cursor.lastrowid)
    return get_audit_event(event_id)  # type: ignore[return-value]


def get_audit_event(event_id: int) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
    return _normalize_audit_event(row) if row else None


def list_audit_events(limit: int = 100) -> list[dict[str, Any]]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events ORDER BY created_at DESC, id DESC LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()
    return [_normalize_audit_event(row) for row in rows]


def record_check_version(check: dict[str, Any], action: str) -> dict[str, Any]:
    timestamp = now_iso()
    check_id = int(check["id"])
    snapshot = _check_snapshot(check)
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO check_versions(check_id, action, snapshot_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (check_id, action, json.dumps(snapshot, ensure_ascii=False), timestamp),
        )
        version_id = int(cursor.lastrowid)
    return get_check_version(version_id)  # type: ignore[return-value]


def get_check_version(version_id: int) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM check_versions WHERE id = ?", (version_id,)).fetchone()
    return _normalize_check_version(row) if row else None


def list_check_versions(check_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM check_versions
            WHERE check_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (check_id, max(1, min(200, int(limit)))),
        ).fetchall()
    return [_normalize_check_version(row) for row in rows]


def list_run_archives(limit: int = 90) -> list[dict[str, Any]]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM run_archives
            ORDER BY archive_date DESC, check_type ASC, status ASC
            LIMIT ?
            """,
            (max(1, min(365, int(limit))),),
        ).fetchall()
    return [_normalize_run_archive(row) for row in rows]


def _run_browser_type(check: dict[str, Any], runner: dict[str, Any]) -> str:
    if str(check.get("type") or check.get("check_type") or "") != "ui":
        return ""
    return str(runner.get("browser_type") or check.get("_browser_type") or check.get("browser_type") or "")


def list_monitoring_trends(
    period: str = "24h",
    start: str | None = None,
    end: str | None = None,
    check_type: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 12,
    hour_start: str | None = None,
    hour_end: str | None = None,
) -> dict[str, Any]:
    range_start, range_end = _trend_window(period, start, end)
    hour_window = _parse_hour_window(hour_start, hour_end)
    page = max(1, int(page))
    page_size = max(1, min(50, int(page_size)))
    normalized_type = _normalize_trend_check_type(check_type)
    query = (q or "").strip()
    refresh_stale_statuses()
    with _LOCK, _connect() as conn:
        total, checks = _monitoring_checks_page(conn, normalized_type, query, page, page_size)
        summary_types = [normalized_type] if normalized_type else ["ui", "api"]
        summary_ids = [TREND_SUMMARY_CHECK_IDS[item] for item in summary_types]
        summaries_by_id = _trend_series_by_check(
            conn, summary_ids, range_start, range_end, 100, hour_window=hour_window
        )
        task_ids = [int(check["id"]) for check in checks]
        tasks_by_id = _trend_series_by_check(
            conn, task_ids, range_start, range_end, 20, hour_window=hour_window
        )

    return {
        "period": period,
        "start": range_start.isoformat(timespec="seconds"),
        "end": range_end.isoformat(timespec="seconds"),
        "hour_start": hour_start if hour_window else None,
        "hour_end": hour_end if hour_window else None,
        "summaries": [
            {
                "check_type": item,
                "label": "UI" if item == "ui" else "API",
                **summaries_by_id.get(TREND_SUMMARY_CHECK_IDS[item], _empty_trend_series()),
            }
            for item in summary_types
        ],
        "tasks": {
            "items": [
                {
                    "check_id": int(check["id"]),
                    "name": check["name"],
                    "check_type": check["type"],
                    "enabled": bool(check["enabled"]),
                    "monitor_status": check.get("monitor_status") or "unknown",
                    "last_run_at": check.get("last_run_at"),
                    "last_duration_ms": check.get("last_duration_ms"),
                    **tasks_by_id.get(int(check["id"]), _empty_trend_series()),
                }
                for check in checks
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


def get_check_trend(
    check_id: int,
    period: str = "24h",
    start: str | None = None,
    end: str | None = None,
    hour_start: str | None = None,
    hour_end: str | None = None,
) -> dict[str, Any] | None:
    check = get_check(check_id)
    if not check:
        return None
    range_start, range_end = _trend_window(period, start, end)
    hour_window = _parse_hour_window(hour_start, hour_end)
    with _LOCK, _connect() as conn:
        series = _trend_series_by_check(
            conn, [check_id], range_start, range_end, 100, hour_window=hour_window
        ).get(check_id, _empty_trend_series())
    return {
        "period": period,
        "start": range_start.isoformat(timespec="seconds"),
        "end": range_end.isoformat(timespec="seconds"),
        "hour_start": hour_start if hour_window else None,
        "hour_end": hour_end if hour_window else None,
        "check": {
            "id": int(check["id"]),
            "name": check["name"],
            "type": check["type"],
            "enabled": bool(check["enabled"]),
            "monitor_status": check.get("monitor_status") or "unknown",
            "last_run_at": check.get("last_run_at"),
            "last_duration_ms": check.get("last_duration_ms"),
        },
        **series,
    }


def create_run(check: dict[str, Any], status: str = "running", error_message: str | None = None) -> dict[str, Any]:
    timestamp = now_iso()
    finished_at = timestamp if status == "skipped" else None
    duration_ms = 0 if status == "skipped" else None
    notification_status = "not_required" if status == "skipped" else None
    runner = check.get("_runner") if isinstance(check.get("_runner"), dict) else {}
    browser_type = _run_browser_type(check, runner)
    metadata = check.get("_run") if isinstance(check.get("_run"), dict) else run_metadata(
        "scheduled" if int(check.get("id") or 0) > 0 else "draft"
    )
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs (
                check_id, check_name, check_type, status, started_at, finished_at,
                duration_ms, error_message, error_stack, logs, screenshot_path,
                trace_path, response_path, request_snapshot, response_snapshot,
                runner_id, runner_name, runner_address, runner_region, runner_browser_version, browser_type, failure_kind,
                notification_status, notification_channel, notification_error,
                notification_sent_at, trigger, observation_kind, affects_health, run_group_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(check["id"]),
                check["name"],
                check["type"],
                status,
                timestamp,
                finished_at,
                duration_ms,
                error_message,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                runner.get("runner_id"),
                runner.get("runner_name"),
                runner.get("runner_address"),
                runner.get("runner_region"),
                runner.get("runner_browser_version"),
                browser_type,
                runner.get("failure_kind"),
                notification_status,
                None,
                None,
                None,
                metadata["trigger"],
                metadata["observation_kind"],
                1 if metadata["affects_health"] else 0,
                metadata.get("run_group_id"),
                timestamp,
            ),
        )
        run_id = int(cursor.lastrowid)
    return get_run(run_id)  # type: ignore[return-value]


def start_run(run_id: int) -> dict[str, Any] | None:
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runs
            SET status = 'running',
                started_at = ?,
                finished_at = NULL,
                duration_ms = NULL,
                error_message = NULL
            WHERE id = ? AND status = 'pending'
            """,
            (timestamp, run_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_run(run_id)


def finish_run(run_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    affects_health = data.get("affects_health")
    affects_health_value = None if affects_health is None else (1 if affects_health else 0)
    failure_fingerprint = _failure_fingerprint_for_values(
        str(data.get("status") or ""),
        data.get("failure_kind"),
        data.get("error_message"),
    )
    removed_artifact_paths: list[str] = []
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runs
            SET status = ?, finished_at = ?, duration_ms = ?, error_message = ?,
                error_stack = ?, logs = ?, screenshot_path = ?, trace_path = ?,
                response_path = ?, request_snapshot = ?, response_snapshot = ?,
                runner_id = ?, runner_name = ?, runner_address = ?, runner_region = ?,
                runner_browser_version = ?, browser_type = CASE WHEN check_type = 'ui' THEN ? ELSE '' END,
                failure_kind = ?, failure_fingerprint = ?, affects_health = COALESCE(?, affects_health)
            WHERE id = ?
            """,
            (
                data["status"],
                data["finished_at"],
                data["duration_ms"],
                data.get("error_message"),
                data.get("error_stack"),
                data.get("logs"),
                data.get("screenshot_path"),
                data.get("trace_path"),
                data.get("response_path"),
                data.get("request_snapshot"),
                data.get("response_snapshot"),
                data.get("runner_id"),
                data.get("runner_name"),
                data.get("runner_address"),
                data.get("runner_region"),
                data.get("runner_browser_version"),
                data.get("browser_type"),
                data.get("failure_kind"),
                failure_fingerprint,
                affects_health_value,
                run_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is not None and not row["trend_recorded_at"]:
            recorded = _record_trend_rollups(conn, row)
            if recorded:
                conn.execute("UPDATE runs SET trend_recorded_at = ? WHERE id = ?", (now_iso(), run_id))
            removed_artifact_paths = _compact_repeated_failure_runs(conn, row)
    if removed_artifact_paths:
        delete_artifact_paths(removed_artifact_paths)
    return get_run(run_id)


def _compact_repeated_failure_runs(conn: sqlite3.Connection, current: sqlite3.Row) -> list[str]:
    current_id = int(current["id"] or 0)
    check_id = int(current["check_id"] or 0)
    status = str(current["status"] or "")
    if current_id <= 0 or check_id <= 0 or status not in {"failed", "timeout"}:
        return []
    if str(current["observation_kind"] or "observation") != "observation" or not bool(current["affects_health"]):
        return []

    fingerprint = _failure_fingerprint_for_row(current)
    if not fingerprint:
        return []

    retention = _similar_failure_retention_count(conn)
    rows = conn.execute(
        """
        SELECT id, status, error_message, failure_kind, failure_fingerprint,
               deduplicated_count, screenshot_path, trace_path, response_path
        FROM runs INDEXED BY idx_runs_check_id_desc
        WHERE check_id = ?
          AND id <= ?
          AND observation_kind = 'observation'
          AND affects_health = 1
        ORDER BY id DESC
        """,
        (check_id, current_id),
    ).fetchall()

    matching_tail: list[sqlite3.Row] = []
    for row in rows:
        if _failure_fingerprint_for_row(row) != fingerprint:
            break
        matching_tail.append(row)
    if len(matching_tail) <= retention:
        return []

    victims = matching_tail[retention:]
    victim_ids = [int(row["id"]) for row in victims]
    rolled_deduplicated_count = sum(int(row["deduplicated_count"] or 0) for row in matching_tail[1:])
    removed_count = len(victims) + rolled_deduplicated_count
    artifact_paths = [
        path
        for row in victims
        for path in (row["screenshot_path"], row["trace_path"], row["response_path"])
        if path
    ]
    placeholders = ", ".join("?" for _ in victim_ids)
    conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", victim_ids)
    conn.execute(
        f"""
        UPDATE check_status
        SET last_run_id = CASE WHEN last_run_id IN ({placeholders}) THEN NULL ELSE last_run_id END,
            last_scheduled_run_id = CASE WHEN last_scheduled_run_id IN ({placeholders}) THEN NULL ELSE last_scheduled_run_id END
        WHERE last_run_id IN ({placeholders}) OR last_scheduled_run_id IN ({placeholders})
        """,
        [*victim_ids, *victim_ids, *victim_ids, *victim_ids],
    )
    kept_ids = [int(row["id"]) for row in matching_tail[:retention]]
    if kept_ids:
        oldest_kept_id = kept_ids[-1]
        newest_kept_id = kept_ids[0]
        conn.execute(
            f"""
            UPDATE anomaly_cycles
            SET first_run_id = CASE WHEN first_run_id IN ({placeholders}) THEN ? ELSE first_run_id END,
                last_run_id = CASE WHEN last_run_id IN ({placeholders}) THEN ? ELSE last_run_id END
            WHERE first_run_id IN ({placeholders}) OR last_run_id IN ({placeholders})
            """,
            [*victim_ids, oldest_kept_id, *victim_ids, newest_kept_id, *victim_ids, *victim_ids],
        )
    previous_deduplicated_ids = [
        int(row["id"])
        for row in matching_tail[1:retention]
        if int(row["deduplicated_count"] or 0) > 0
    ]
    if previous_deduplicated_ids:
        previous_placeholders = ", ".join("?" for _ in previous_deduplicated_ids)
        conn.execute(
            f"UPDATE runs SET deduplicated_count = 0 WHERE id IN ({previous_placeholders})",
            previous_deduplicated_ids,
        )
    if removed_count:
        conn.execute(
            "UPDATE runs SET deduplicated_count = COALESCE(deduplicated_count, 0) + ? WHERE id = ?",
            (removed_count, current_id),
        )
    return artifact_paths


def _similar_failure_retention_count(conn: sqlite3.Connection) -> int:
    raw_value = _read_setting_value(conn, "similar_failure_retention_count")
    try:
        value = int(raw_value if raw_value is not None else DEFAULT_SETTINGS["similar_failure_retention_count"])
    except (TypeError, ValueError):
        value = int(DEFAULT_SETTINGS["similar_failure_retention_count"])
    return max(1, min(100, value))


def _failure_fingerprint_for_row(row: sqlite3.Row) -> str:
    existing = ""
    try:
        existing = str(row["failure_fingerprint"] or "")
    except (IndexError, KeyError):
        existing = ""
    if existing:
        return existing
    return _failure_fingerprint_for_values(str(row["status"] or ""), row["failure_kind"], row["error_message"])


def _failure_fingerprint_for_values(status: str, failure_kind: Any, error_message: Any) -> str:
    normalized_status = str(status or "")
    if normalized_status not in {"failed", "timeout"}:
        return ""
    payload = {
        "status": normalized_status,
        "failure_kind": _normalize_failure_kind(failure_kind, normalized_status),
        "message": _normalize_failure_message(error_message),
    }
    if not payload["message"]:
        return ""
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _normalize_failure_message(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:1000]


def upsert_probe_runner(data: dict[str, Any]) -> dict[str, Any]:
    timestamp = now_iso()
    runner_id = str(data.get("runner_id") or "").strip()
    if not runner_id:
        raise ValueError("Runner ID 不能为空")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO probe_runners (
                runner_id, name, address, network_region, browser_version, installed_browser_types_json, available_browser_types_json,
                status, metadata_json, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(runner_id) DO UPDATE SET
                name = excluded.name,
                address = excluded.address,
                network_region = excluded.network_region,
                browser_version = excluded.browser_version,
                installed_browser_types_json = excluded.installed_browser_types_json,
                available_browser_types_json = excluded.available_browser_types_json,
                status = excluded.status,
                metadata_json = excluded.metadata_json,
                last_seen_at = excluded.last_seen_at,
                updated_at = excluded.updated_at
            """,
            (
                runner_id,
                str(data.get("name") or runner_id).strip(),
                str(data.get("address") or "").strip(),
                str(data.get("network_region") or "local").strip(),
                str(data.get("browser_version") or "").strip(),
                json.dumps(_normalize_runner_browser_types(data.get("installed_browser_types")), ensure_ascii=False),
                json.dumps(_normalize_runner_browser_types(data.get("available_browser_types")), ensure_ascii=False),
                str(data.get("status") or "ok").strip(),
                json.dumps(metadata, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        if str(data.get("status") or "ok").strip() in {"ok", "warning"}:
            conn.execute(
                """
                UPDATE probe_runners
                SET unavailable_since = NULL,
                    unavailable_notified_at = NULL
                WHERE runner_id = ?
                """,
                (runner_id,),
            )
    return get_probe_runner(runner_id)  # type: ignore[return-value]


def create_probe_runner(data: dict[str, Any], *, generate_token: bool = True) -> dict[str, Any]:
    raw_token = str(data.get("token") or "").strip()
    generated_token = bool(generate_token and not raw_token)
    token = raw_token or (create_runner_token() if generate_token else "")
    runner_id = _normalize_runner_id(data.get("runner_id") or f"runner-{uuid.uuid4().hex[:8]}")
    timestamp = now_iso()
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    role = _normalize_runner_role(data.get("role") or "child")
    address = str(data.get("address") or "").strip()
    allow_insecure_http = bool(data.get("allow_insecure_http", False))
    ensure_probe_runner_transport_allowed(
        {
            "role": role,
            "connection_mode": "manual",
            "address": address,
            "allow_insecure_http": allow_insecure_http,
        }
    )
    with _LOCK, _connect() as conn:
        if conn.execute("SELECT 1 FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone():
            raise ValueError("Runner ID already exists")
        conn.execute(
            """
            INSERT INTO probe_runners (
                runner_id, name, address, network_region, browser_version, installed_browser_types_json, available_browser_types_json, status,
                metadata_json, enabled, role, token_value, token_hash, token_hint, created_at,
                connection_mode, allow_insecure_http, last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                runner_id,
                str(data.get("name") or runner_id).strip(),
                address,
                str(data.get("network_region") or "local").strip(),
                str(data.get("browser_version") or "").strip(),
                json.dumps(_normalize_runner_browser_types(data.get("installed_browser_types")), ensure_ascii=False),
                json.dumps(_normalize_runner_browser_types(data.get("available_browser_types")), ensure_ascii=False),
                "offline",
                json.dumps(metadata, ensure_ascii=False),
                1 if data.get("enabled", True) else 0,
                role,
                _store_runner_token(token),
                runner_token_hash(token) if token else "",
                _token_hint(token),
                timestamp,
                "manual",
                1 if allow_insecure_http else 0,
                timestamp,
                timestamp,
            ),
        )
    runner = get_probe_runner(runner_id) or {}
    if generated_token:
        runner["token"] = token
    return runner


def provision_probe_runner(data: dict[str, Any]) -> dict[str, Any]:
    runner_id = _normalize_runner_id(data.get("runner_id") or f"runner-{uuid.uuid4().hex[:8]}")
    relay_token = create_relay_token()
    worker_token = create_runner_token()
    timestamp = now_iso()
    expires_at = (datetime.now().astimezone() + timedelta(hours=RELAY_DEPLOY_COMMAND_TTL_HOURS)).isoformat(timespec="seconds")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        if conn.execute("SELECT 1 FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone():
            raise ValueError("Runner ID already exists")
        port = _allocate_relay_internal_port(conn)
        conn.execute(
            """
            INSERT INTO probe_runners (
                runner_id, name, address, network_region, browser_version, status,
                metadata_json, enabled, role, token_value, token_hash, token_hint,
                connection_mode, relay_token_hash, relay_token_hint, relay_token_version,
                allocated_internal_port, deploy_command_expires_at, created_at,
                last_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                runner_id,
                str(data.get("name") or runner_id).strip(),
                f"http://{RELAY_INTERNAL_HOST}:{port}",
                str(data.get("network_region") or "local").strip() or "local",
                "",
                "pending_deployment",
                json.dumps(metadata, ensure_ascii=False),
                1 if data.get("enabled", True) else 0,
                "child",
                _store_runner_token(worker_token),
                runner_token_hash(worker_token),
                _token_hint(worker_token),
                "relay",
                relay_token_hash(relay_token),
                _token_hint(relay_token),
                1,
                port,
                expires_at,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    runner = get_probe_runner(runner_id) or {}
    runner["relay_token"] = relay_token
    runner["worker_token"] = worker_token
    return runner


def regenerate_probe_runner_provision(runner_id: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    relay_token = create_relay_token()
    timestamp = now_iso()
    expires_at = (datetime.now().astimezone() + timedelta(hours=RELAY_DEPLOY_COMMAND_TTL_HOURS)).isoformat(timespec="seconds")
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        row = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        if not row:
            return None
        current = _normalize_probe_runner(row)
        if current.get("connection_mode") != "relay":
            raise ValueError("手动添加的 Runner 不能重新生成 relay 部署命令")
        current_hash = str(row["relay_token_hash"] or "")
        current_hint = str(row["relay_token_hint"] or "")
        current_version = int(row["relay_token_version"] or 0)
        keep_previous = bool(current.get("available") and current_hash and current_version > 0)
        next_version = current_version + 1
        next_status = "available" if keep_previous else "pending_deployment"
        next_relay_seen_at = row["relay_last_seen_at"] if keep_previous else None
        next_worker_health_at = row["worker_health_last_success_at"] if keep_previous else None
        next_disconnect_reason = str(row["last_disconnect_reason"] or "") if keep_previous else "deployment command regenerated"
        conn.execute(
            """
            UPDATE probe_runners
            SET relay_previous_token_hash = ?,
                relay_previous_token_hint = ?,
                relay_previous_token_version = ?,
                relay_previous_token_expires_at = ?,
                relay_token_hash = ?,
                relay_token_hint = ?,
                relay_token_version = ?,
                deploy_command_expires_at = ?,
                status = ?,
                relay_last_seen_at = ?,
                worker_health_last_success_at = ?,
                last_disconnect_reason = ?,
                updated_at = ?
            WHERE runner_id = ?
            """,
            (
                current_hash if keep_previous else "",
                current_hint if keep_previous else "",
                current_version if keep_previous else 0,
                expires_at if keep_previous else None,
                relay_token_hash(relay_token),
                _token_hint(relay_token),
                next_version,
                expires_at,
                next_status,
                next_relay_seen_at,
                next_worker_health_at,
                next_disconnect_reason,
                timestamp,
                runner_id,
            ),
        )
    runner = get_probe_runner(runner_id) or {}
    runner["relay_token"] = relay_token
    runner["worker_token"] = get_probe_runner_token(runner_id)
    return runner


def update_probe_runner(runner_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        current = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        if not current:
            return None
        name = str(data["name"] if data.get("name") is not None else current["name"] or runner_id).strip() or runner_id
        address = str(data["address"] if data.get("address") is not None else current["address"] or "").strip()
        network_region = str(data["network_region"] if data.get("network_region") is not None else current["network_region"] or "local").strip() or "local"
        enabled = bool(data["enabled"] if data.get("enabled") is not None else current["enabled"])
        allow_insecure_http = bool(
            data["allow_insecure_http"] if data.get("allow_insecure_http") is not None else current["allow_insecure_http"]
        )
        ensure_probe_runner_transport_allowed(
            {
                "role": current["role"],
                "connection_mode": current["connection_mode"],
                "address": address,
                "allow_insecure_http": allow_insecure_http,
            }
        )
        assignments = [
            "name = ?",
            "address = ?",
            "network_region = ?",
            "enabled = ?",
            "allow_insecure_http = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [name, address, network_region, 1 if enabled else 0, 1 if allow_insecure_http else 0, timestamp]
        token = str(data.get("token") or "").strip()
        if token:
            assignments.extend(["token_value = ?", "token_hash = ?", "token_hint = ?"])
            params.extend([_store_runner_token(token), runner_token_hash(token), _token_hint(token)])
        params.append(runner_id)
        cursor = conn.execute(
            f"""
            UPDATE probe_runners
            SET {", ".join(assignments)}
            WHERE runner_id = ?
            """,
            params,
        )
        if cursor.rowcount == 0:
            return None
        if runner_id == LOCAL_RUNNER_ID:
            for key, value in {
                "local_runner_name": name,
                "local_runner_address": address,
                "local_runner_region": network_region,
            }.items():
                conn.execute(
                    """
                    INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, json.dumps(value, ensure_ascii=False), timestamp),
                )
    return get_probe_runner(runner_id)


def mark_probe_runner_available(runner_id: str, health: dict[str, Any] | None = None) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    health = health if isinstance(health, dict) else {}
    status = str(health.get("status") or "ok").strip().lower()
    if status not in {"ok", "warning"}:
        status = "ok"
    browser_version = str(health.get("browser_version") or "").strip()
    installed_browser_types = _normalize_runner_browser_types(health.get("installed_browser_types"))
    available_browser_types = _normalize_runner_browser_types(health.get("available_browser_types"))
    metadata = health.get("metadata") if isinstance(health.get("metadata"), dict) else {}
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        current = conn.execute("SELECT connection_mode FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        next_status = "available" if current and str(current["connection_mode"] or "") == "relay" else status
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = ?,
                browser_version = ?,
                installed_browser_types_json = ?,
                available_browser_types_json = ?,
                metadata_json = ?,
                deploy_command_expires_at = CASE WHEN ? = 'relay' THEN NULL ELSE deploy_command_expires_at END,
                unavailable_since = NULL,
                unavailable_notified_at = NULL,
                worker_health_last_success_at = ?,
                was_available = 1,
                last_seen_at = ?,
                updated_at = ?
            WHERE runner_id = ?
            """,
            (
                next_status,
                browser_version,
                json.dumps(installed_browser_types, ensure_ascii=False),
                json.dumps(available_browser_types, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                str(current["connection_mode"] or "") if current else "",
                timestamp,
                timestamp,
                timestamp,
                runner_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def delete_probe_runner(runner_id: str) -> bool:
    runner_id = _normalize_runner_id(runner_id)
    if runner_id == LOCAL_RUNNER_ID:
        raise ValueError("local Runner cannot be deleted")
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT allocated_internal_port FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        cursor = conn.execute("DELETE FROM probe_runners WHERE runner_id = ?", (runner_id,))
        if cursor.rowcount > 0 and row and row["allocated_internal_port"]:
            conn.execute(
                "INSERT OR REPLACE INTO relay_port_quarantine(port, released_at) VALUES (?, ?)",
                (int(row["allocated_internal_port"]), now_iso()),
            )
        return cursor.rowcount > 0


def rotate_probe_runner_token(runner_id: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    token = create_runner_token()
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET token_value = ?, token_hash = ?, token_hint = ?, updated_at = ?
            WHERE runner_id = ?
            """,
            (_store_runner_token(token), runner_token_hash(token), _token_hint(token), timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    runner = get_probe_runner(runner_id) or {}
    runner["token"] = token
    return runner


def get_probe_runner(runner_id: str) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        row = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
    return _normalize_probe_runner(row) if row else None


def list_probe_runners(limit: int = 100) -> list[dict[str, Any]]:
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM probe_runners
            ORDER BY last_seen_at DESC, runner_id ASC
            LIMIT ?
            """,
            (max(1, min(500, int(limit))),),
        ).fetchall()
    return [_normalize_probe_runner(row) for row in rows]


def list_enabled_probe_runners() -> list[dict[str, Any]]:
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM probe_runners
            WHERE enabled = 1
            ORDER BY role = 'local' DESC, runner_id ASC
            """
        ).fetchall()
    return [_normalize_probe_runner(row) for row in rows]


def list_schedulable_probe_runners() -> list[dict[str, Any]]:
    return [runner for runner in list_enabled_probe_runners() if can_schedule_runner(runner)]


def list_probe_runners_by_ids(runner_ids: list[str]) -> list[dict[str, Any]]:
    ids = _normalize_runner_ids(runner_ids)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        rows = conn.execute(f"SELECT * FROM probe_runners WHERE runner_id IN ({placeholders})", ids).fetchall()
    by_id = {_normalize_probe_runner(row)["runner_id"]: _normalize_probe_runner(row) for row in rows}
    return [by_id[runner_id] for runner_id in ids if runner_id in by_id]


def runner_metadata(
    runner: dict[str, Any],
    failure_kind: str = "none",
    browser_version: str | None = None,
    browser_type: str | None = None,
) -> dict[str, str]:
    return {
        "runner_id": str(runner.get("runner_id") or LOCAL_RUNNER_ID),
        "runner_name": str(runner.get("name") or runner.get("runner_id") or LOCAL_RUNNER_ID),
        "runner_address": str(runner.get("address") or ""),
        "runner_region": str(runner.get("network_region") or "local"),
        "runner_browser_version": str(browser_version if browser_version is not None else runner.get("browser_version") or ""),
        "browser_type": str(browser_type if browser_type is not None else runner.get("browser_type") or ""),
        "failure_kind": failure_kind if failure_kind in {"none", "target", "runner"} else "runner",
    }


def create_runner_token() -> str:
    return f"pgrn_{secrets.token_urlsafe(32)}"


def create_relay_token() -> str:
    return f"pgrl_{secrets.token_urlsafe(32)}"


def runner_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def relay_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _runner_token_key_file() -> Path:
    return Path(os.getenv("PULSEGUARD_RUNNER_TOKEN_KEY_FILE", Path(DB_PATH).parent / "runner-token.key")).resolve()


def _runner_token_key() -> bytes:
    env_key = os.getenv("PULSEGUARD_RUNNER_TOKEN_ENCRYPTION_KEY", "").strip()
    if env_key:
        key = env_key.encode("utf-8")
        Fernet(key)
        return key

    key_file = _runner_token_key_file()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip().encode("utf-8")
        Fernet(key)
        return key

    key = Fernet.generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(key_file, flags, 0o600)
    except FileExistsError:
        for _ in range(50):
            stored = key_file.read_text(encoding="utf-8").strip()
            if stored:
                key = stored.encode("utf-8")
                Fernet(key)
                return key
            time_module.sleep(0.02)
        raise RuntimeError("runner token encryption key file is empty")
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(key.decode("utf-8") + "\n")
    return key


def _runner_token_cipher() -> Fernet:
    return Fernet(_runner_token_key())


def _store_runner_token(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return ""
    return ENCRYPTED_RUNNER_TOKEN_PREFIX + _runner_token_cipher().encrypt(token.encode("utf-8")).decode("utf-8")


def _load_runner_token(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not value.startswith(ENCRYPTED_RUNNER_TOKEN_PREFIX):
        return value
    encrypted = value[len(ENCRYPTED_RUNNER_TOKEN_PREFIX) :].encode("utf-8")
    try:
        return _runner_token_cipher().decrypt(encrypted).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("runner token encryption key cannot decrypt stored token") from exc


def verify_probe_runner_token(runner_id: str, token: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    token = str(token or "").strip()
    if not runner_id or not token:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
    if not row:
        return None
    expected_hash = str(row["token_hash"] or "")
    expected_value = str(row["token_value"] or "") if "token_value" in row.keys() else ""
    valid = bool(expected_value and secrets.compare_digest(expected_value, token))
    valid = valid or bool(expected_hash and secrets.compare_digest(expected_hash, runner_token_hash(token)))
    if not valid:
        return None
    return _normalize_probe_runner(row)


def get_probe_runner_token(runner_id: str) -> str:
    runner_id = _normalize_runner_id(runner_id)
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT token_value FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
    return _load_runner_token(str(row["token_value"] or "")) if row else ""


def verify_probe_runner_relay_token(runner_id: str, token: str, version: int) -> dict[str, Any] | None:
    try:
        runner_id = _normalize_runner_id(runner_id)
    except ValueError:
        return None
    token = str(token or "").strip()
    if not token:
        return None
    with _LOCK, _connect() as conn:
        _expire_probe_runner_provisions(conn)
        row = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
    if not row:
        return None
    token_digest = relay_token_hash(token)
    expected_hash = str(row["relay_token_hash"] or "")
    expected_version = int(row["relay_token_version"] or 0)
    if str(row["connection_mode"] or "") != "relay":
        return None
    if not int(row["enabled"] or 0):
        return None
    matched_current = int(version or 0) == expected_version and secrets.compare_digest(expected_hash, token_digest)
    matched_previous = _previous_relay_token_matches(row, token_digest, int(version or 0))
    if not matched_current and not matched_previous:
        return None
    expires_at = str(row["deploy_command_expires_at"] or "")
    try:
        expired = bool(expires_at and datetime.fromisoformat(expires_at) < datetime.now().astimezone())
    except ValueError:
        expired = False
    status = str(row["status"] or "")
    was_available = bool(int(row["was_available"] or 0))
    if matched_current and expired and status in RELAY_PRE_ACTIVATION_STATUSES and not was_available:
        return None
    return _normalize_probe_runner(row)


def mark_probe_runner_relay_connected(runner_id: str, token_version: int | None = None) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    connected_version = int(token_version or 0)
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = 'connecting',
                relay_last_seen_at = ?,
                last_disconnect_reason = '',
                last_seen_at = ?,
                relay_previous_token_hash = CASE WHEN ? > 0 AND relay_token_version = ? THEN '' ELSE relay_previous_token_hash END,
                relay_previous_token_hint = CASE WHEN ? > 0 AND relay_token_version = ? THEN '' ELSE relay_previous_token_hint END,
                relay_previous_token_version = CASE WHEN ? > 0 AND relay_token_version = ? THEN 0 ELSE relay_previous_token_version END,
                relay_previous_token_expires_at = CASE WHEN ? > 0 AND relay_token_version = ? THEN NULL ELSE relay_previous_token_expires_at END,
                updated_at = ?
            WHERE runner_id = ? AND connection_mode = 'relay'
            """,
            (
                timestamp,
                timestamp,
                connected_version,
                connected_version,
                connected_version,
                connected_version,
                connected_version,
                connected_version,
                connected_version,
                connected_version,
                timestamp,
                runner_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def mark_probe_runner_relay_seen(runner_id: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET relay_last_seen_at = ?,
                last_seen_at = ?,
                updated_at = ?
            WHERE runner_id = ? AND connection_mode = 'relay'
            """,
            (timestamp, timestamp, timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def mark_probe_runner_relay_disconnected(runner_id: str, reason: str = "") -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        current_status = str(row["status"] or "") if row else ""
        if row and _relay_token_rotation_pending(row):
            status = "connecting"
        elif current_status in RELAY_PRE_ACTIVATION_STATUSES:
            status = current_status
        else:
            status = "unavailable" if row and int(row["was_available"] or 0) else "connecting"
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = ?,
                last_disconnect_reason = ?,
                unavailable_since = CASE WHEN ? = 'unavailable' THEN COALESCE(unavailable_since, ?) ELSE unavailable_since END,
                updated_at = ?
            WHERE runner_id = ? AND connection_mode = 'relay'
            """,
            (status, str(reason or "")[:500], status, timestamp, timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def mark_relay_runners_restarted(reason: str = "relay server restarted") -> int:
    timestamp = now_iso()
    pre_activation = tuple(RELAY_PRE_ACTIVATION_STATUSES)
    placeholders = ", ".join("?" for _ in pre_activation)
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            f"""
            UPDATE probe_runners
            SET status = CASE
                    WHEN status IN ({placeholders}) THEN status
                    WHEN was_available = 1 THEN 'unavailable'
                    ELSE 'connecting'
                END,
                relay_last_seen_at = NULL,
                last_disconnect_reason = ?,
                unavailable_since = CASE
                    WHEN status NOT IN ({placeholders}) AND was_available = 1 THEN COALESCE(unavailable_since, ?)
                    ELSE unavailable_since
                END,
                updated_at = ?
            WHERE connection_mode = 'relay'
            """,
            (*pre_activation, str(reason or "")[:500], *pre_activation, timestamp, timestamp),
        )
        return cursor.rowcount


def mark_probe_runner_relay_auth_failed(runner_id: str) -> dict[str, Any] | None:
    try:
        runner_id = _normalize_runner_id(runner_id)
    except ValueError:
        return None
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        current_status = str(row["status"] or "") if row else ""
        if row and _relay_token_rotation_pending(row):
            status = "connecting"
        elif current_status in RELAY_PRE_ACTIVATION_STATUSES:
            status = current_status
        else:
            status = "auth_failed" if row and int(row["was_available"] or 0) else "pending_deployment"
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = ?,
                last_disconnect_reason = 'relay authentication failed',
                unavailable_since = CASE WHEN ? = 'auth_failed' THEN COALESCE(unavailable_since, ?) ELSE unavailable_since END,
                updated_at = ?
            WHERE runner_id = ? AND connection_mode = 'relay'
            """,
            (status, status, timestamp, timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def can_schedule_runner(runner_or_id: dict[str, Any] | str) -> bool:
    runner = get_probe_runner(runner_or_id) if isinstance(runner_or_id, str) else runner_or_id
    if not runner or not runner.get("enabled"):
        return False
    if probe_runner_transport_error(runner):
        return False
    if str(runner.get("role") or "") == "local":
        return runner.get("available", False)
    if str(runner.get("connection_mode") or "manual") != "relay":
        return runner.get("available", False)
    if str(runner.get("status") or "") != "available":
        return False
    return _timestamp_is_fresh(runner.get("relay_last_seen_at"), RELAY_HEARTBEAT_TIMEOUT_SECONDS) and _timestamp_is_fresh(
        runner.get("worker_health_last_success_at"),
        RUNNER_HEARTBEAT_TIMEOUT_SECONDS,
    )


def probe_runner_transport_error(runner: dict[str, Any]) -> str:
    if str(runner.get("role") or "") != "child":
        return ""
    if str(runner.get("connection_mode") or "manual") != "manual":
        return ""
    address = str(runner.get("address") or "").strip()
    if not address:
        return ""
    parsed = urlsplit(address if "://" in address else f"http://{address}")
    if parsed.scheme == "http" and not bool(runner.get("allow_insecure_http")):
        return "手动 HTTP 节点未确认明文传输风险，请改用 HTTPS、Relay，或显式允许不安全 HTTP"
    return ""


def ensure_probe_runner_transport_allowed(runner: dict[str, Any]) -> None:
    error = probe_runner_transport_error(runner)
    if error:
        raise ValueError(error)


def mark_probe_runner_updating(runner_id: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = 'updating',
                last_disconnect_reason = '',
                updated_at = ?
            WHERE runner_id = ?
            """,
            (timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def restore_probe_runner_status(runner_id: str, status: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    status = str(status or "").strip().lower()
    if not status:
        return get_probe_runner(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = ?,
                updated_at = ?
            WHERE runner_id = ? AND status = 'updating'
            """,
            (status, timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return get_probe_runner(runner_id)
    return get_probe_runner(runner_id)


def mark_probe_runner_unavailable(runner_id: str, status: str = "offline") -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT connection_mode, was_available, status FROM probe_runners WHERE runner_id = ?", (runner_id,)).fetchone()
        next_status = status
        if row and str(row["connection_mode"] or "") == "relay":
            current_status = str(row["status"] or "")
            if current_status == "updating":
                next_status = current_status
            elif current_status in RELAY_PRE_ACTIVATION_STATUSES:
                next_status = current_status
            else:
                next_status = "unavailable" if int(row["was_available"] or 0) else "connecting"
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET status = ?,
                unavailable_since = CASE WHEN ? IN ('offline', 'unavailable', 'unhealthy', 'auth_failed') THEN COALESCE(unavailable_since, ?) ELSE unavailable_since END,
                updated_at = ?
            WHERE runner_id = ?
            """,
            (next_status, next_status, timestamp, timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def mark_probe_runner_unavailable_notified(runner_id: str) -> dict[str, Any] | None:
    runner_id = _normalize_runner_id(runner_id)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE probe_runners
            SET unavailable_notified_at = ?, updated_at = ?
            WHERE runner_id = ?
            """,
            (timestamp, timestamp, runner_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_probe_runner(runner_id)


def should_notify_probe_runner_unavailable(runner: dict[str, Any]) -> bool:
    if not bool(runner.get("enabled")) or str(runner.get("role") or "") == "local" or runner.get("available") or runner.get("unavailable_notified_at"):
        return False
    if str(runner.get("connection_mode") or "manual") == "relay":
        return bool(runner.get("was_available")) and str(runner.get("status") or "") in {"unavailable", "unhealthy", "auth_failed"}
    return True


def next_runner_cursor(scope: str, count: int) -> int:
    count = max(1, int(count))
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT cursor FROM runner_cursors WHERE scope = ?", (scope,)).fetchone()
        current = int(row["cursor"] or 0) if row else 0
        next_cursor = (current + 1) % count
        conn.execute(
            """
            INSERT INTO runner_cursors(scope, cursor, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(scope) DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at
            """,
            (scope, next_cursor, timestamp),
        )
    return current % count


def update_run_notification(
    run_id: int,
    status: str,
    channel: str | None = None,
    error: str | None = None,
    sent_at: str | None = None,
) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE runs
            SET notification_status = ?,
                notification_channel = ?,
                notification_error = ?,
                notification_sent_at = ?
            WHERE id = ?
            """,
            (status, channel, error, sent_at, run_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_run(run_id)


def update_check_status(check_id: int, run: dict[str, Any]) -> dict[str, Any]:
    timestamp = run.get("finished_at") or now_iso()
    settings = get_settings()

    with _LOCK, _connect() as conn:
        previous = conn.execute(
            "SELECT * FROM check_status WHERE check_id = ?",
            (check_id,),
        ).fetchone()
        previous_dict = dict(previous) if previous else None
        transition = next_health_state(previous_dict, run, settings)
        current_status = transition["current_status"]
        previous_status = transition["previous_status"]
        last_success_at = timestamp if run["status"] == "ok" else (previous_dict or {}).get("last_success_at")
        last_failed_at = (
            timestamp
            if run["status"] in {"failed", "timeout"} and run.get("failure_kind") != "runner"
            else (previous_dict or {}).get("last_failed_at")
        )
        state_changed_at = timestamp if current_status != previous_status else (previous_dict or {}).get("last_state_changed_at")

        conn.execute(
            """
            INSERT INTO check_status (
                check_id, current_status, consecutive_failures, last_success_at,
                last_failed_at, last_run_at, last_run_id, last_error, last_notified_at,
                monitor_status, consecutive_successes, last_scheduled_at,
                last_scheduled_run_id, last_state_changed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(check_id) DO UPDATE SET
                current_status = excluded.current_status,
                consecutive_failures = excluded.consecutive_failures,
                last_success_at = excluded.last_success_at,
                last_failed_at = excluded.last_failed_at,
                last_run_at = excluded.last_run_at,
                last_run_id = excluded.last_run_id,
                last_error = excluded.last_error,
                monitor_status = excluded.monitor_status,
                consecutive_successes = excluded.consecutive_successes,
                last_scheduled_at = excluded.last_scheduled_at,
                last_scheduled_run_id = excluded.last_scheduled_run_id,
                last_state_changed_at = excluded.last_state_changed_at,
                updated_at = excluded.updated_at
            """,
            (
                check_id,
                current_status,
                transition["consecutive_failures"],
                last_success_at,
                last_failed_at,
                timestamp,
                run["id"],
                run.get("error_message"),
                (previous_dict or {}).get("last_notified_at"),
                current_status,
                transition["consecutive_successes"],
                timestamp,
                run["id"],
                state_changed_at,
                timestamp,
            ),
        )
        _update_anomaly_cycle(conn, check_id, run, transition, timestamp)

    return transition


def refresh_stale_statuses(check_id: int | None = None) -> int:
    settings = get_settings()
    stale_after = max(1, int(settings.get("stale_after_intervals", 2)))
    clauses = ["c.enabled = 1", "s.last_scheduled_at IS NOT NULL", "s.monitor_status != 'stale'"]
    params: list[Any] = []
    if check_id is not None:
        clauses.append("c.id = ?")
        params.append(check_id)
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.id, c.interval_seconds, s.last_scheduled_at
            FROM checks c
            JOIN check_status s ON s.check_id = c.id
            WHERE {" AND ".join(clauses)}
            """,
            params,
        ).fetchall()
        stale_ids: list[int] = []
        now = datetime.now().astimezone()
        for row in rows:
            try:
                last_scheduled = datetime.fromisoformat(str(row["last_scheduled_at"]))
            except ValueError:
                continue
            if now - last_scheduled > timedelta(seconds=max(5, int(row["interval_seconds"])) * stale_after):
                stale_ids.append(int(row["id"]))
        if not stale_ids:
            return 0
        placeholders = ",".join("?" for _ in stale_ids)
        timestamp = now_iso()
        conn.execute(
            f"""
            UPDATE check_status
            SET current_status = 'stale', monitor_status = 'stale',
                last_state_changed_at = ?, updated_at = ?
            WHERE check_id IN ({placeholders})
            """,
            [timestamp, timestamp, *stale_ids],
        )
        return len(stale_ids)


def _update_anomaly_cycle(
    conn: sqlite3.Connection,
    check_id: int,
    run: dict[str, Any],
    transition: dict[str, Any],
    timestamp: str,
) -> None:
    current = transition["current_status"]
    previous = transition["previous_status"]
    if current == "failing":
        open_cycle = conn.execute(
            "SELECT id FROM anomaly_cycles WHERE check_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (check_id,),
        ).fetchone()
        if open_cycle:
            conn.execute(
                """
                UPDATE anomaly_cycles
                SET last_run_id = ?, failure_count = ?, failure_kind = ?, summary = ?
                WHERE id = ?
                """,
                (
                    run["id"],
                    transition["consecutive_failures"],
                    run.get("failure_kind") or "target",
                    run.get("error_message") or "",
                    open_cycle["id"],
                ),
            )
        elif previous != "failing":
            conn.execute(
                """
                INSERT INTO anomaly_cycles (
                    check_id, status, opened_at, first_run_id, last_run_id,
                    failure_count, failure_kind, summary
                ) VALUES (?, 'open', ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    timestamp,
                    run["id"],
                    run["id"],
                    transition["consecutive_failures"],
                    run.get("failure_kind") or "target",
                    run.get("error_message") or "",
                ),
            )
    elif current == "healthy":
        conn.execute(
            """
            UPDATE anomaly_cycles
            SET status = 'resolved', resolved_at = ?, last_run_id = ?
            WHERE check_id = ? AND status = 'open'
            """,
            (timestamp, run["id"], check_id),
        )


def get_status_transition(check_id: int, current_status: str) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM check_status WHERE check_id = ?", (check_id,)).fetchone()
    previous = dict(row) if row else {}
    return {
        "previous_status": previous.get("current_status"),
        "current_status": current_status,
        "previous_consecutive_failures": previous.get("consecutive_failures", 0),
        "consecutive_failures": previous.get("consecutive_failures", 0),
        "last_notified_at": previous.get("last_notified_at"),
    }


def update_last_notified(check_id: int, notified_at: str | None = None) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE check_status SET last_notified_at = ?, updated_at = ? WHERE check_id = ?",
            (notified_at or now_iso(), now_iso(), check_id),
        )


def get_run(run_id: int) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _normalize_run(row) if row else None


def get_previous_successful_run(run: dict[str, Any]) -> dict[str, Any] | None:
    check_id = int(run.get("check_id") or 0)
    run_id = int(run.get("id") or 0)
    if check_id <= 0 or run_id <= 0:
        return None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM runs
            WHERE check_id = ?
              AND status = 'ok'
              AND id < ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (check_id, run_id),
        ).fetchone()
    return _normalize_run(row) if row else None


def list_recent_business_incidents(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(100, int(limit)))
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_run_summary_select()}
            FROM runs INDEXED BY idx_runs_business_failures_started
            WHERE check_id > 0
              AND affects_health = 1
              AND status IN ('failed', 'timeout')
              AND (failure_kind IS NULL OR failure_kind = 'target')
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [_normalize_run(row) for row in rows]


def list_runs(filters: dict[str, Any] | None = None, limit: int = 100, summary_only: bool = False) -> list[dict[str, Any]]:
    filters = filters or {}
    clauses: list[str] = []
    params: list[Any] = []

    if filters.get("check_type"):
        clauses.append("check_type = ?")
        params.append(filters["check_type"])
    if filters.get("status"):
        if filters["status"] == "failed":
            clauses.append("status IN ('failed', 'timeout')")
        else:
            clauses.append("status = ?")
            params.append(filters["status"])
    if filters.get("notification_status"):
        clauses.append("notification_status = ?")
        params.append(filters["notification_status"])
    if filters.get("runner_id"):
        clauses.append("runner_id = ?")
        params.append(filters["runner_id"])
    if filters.get("run_group_id"):
        clauses.append("run_group_id = ?")
        params.append(filters["run_group_id"])
    if filters.get("trigger"):
        clauses.append("trigger = ?")
        params.append(filters["trigger"])
    if filters.get("observation_kind"):
        clauses.append("observation_kind = ?")
        params.append(filters["observation_kind"])
    if filters.get("q"):
        clauses.append("check_name LIKE ?")
        params.append(f"%{filters['q']}%")
    if filters.get("check_id"):
        clauses.append("check_id = ?")
        params.append(int(filters["check_id"]))
    if filters.get("start"):
        clauses.append("started_at >= ?")
        params.append(filters["start"])
    if filters.get("end"):
        clauses.append("started_at <= ?")
        params.append(filters["end"])

    select_columns = _run_summary_select() if summary_only else "*"
    sql = f"SELECT {select_columns} FROM runs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with _LOCK, _connect() as conn:
        return [_normalize_run(row) for row in conn.execute(sql, params).fetchall()]


def list_runs_page(
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    filters = filters or {}
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    clauses, params = _run_filter_clauses(filters)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _LOCK, _connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS count FROM runs{where}", params).fetchone()["count"])
        rows = conn.execute(
            f"SELECT {_run_summary_select()} FROM runs{where} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [_normalize_run(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _run_filter_clauses(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    mappings = (
        ("check_type", "check_type"),
        ("notification_status", "notification_status"),
        ("runner_id", "runner_id"),
        ("run_group_id", "run_group_id"),
        ("trigger", "trigger"),
        ("observation_kind", "observation_kind"),
    )
    for key, column in mappings:
        if filters.get(key):
            clauses.append(f"{column} = ?")
            params.append(filters[key])
    if filters.get("status"):
        if filters["status"] == "failed":
            clauses.append("status IN ('failed', 'timeout')")
        else:
            clauses.append("status = ?")
            params.append(filters["status"])
    if filters.get("q"):
        clauses.append("check_name LIKE ?")
        params.append(f"%{filters['q']}%")
    if filters.get("check_id"):
        clauses.append("check_id = ?")
        params.append(int(filters["check_id"]))
    if filters.get("start"):
        clauses.append("started_at >= ?")
        params.append(filters["start"])
    if filters.get("end"):
        clauses.append("started_at <= ?")
        params.append(filters["end"])
    return clauses, params


def list_anomaly_cycles(limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE a.status = ?"
        params.append(status)
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.*, c.name AS check_name, c.type AS check_type
            FROM anomaly_cycles a
            JOIN checks c ON c.id = a.check_id
            {where}
            ORDER BY a.opened_at DESC, a.id DESC
            LIMIT ?
            """,
            [*params, max(1, min(500, int(limit)))],
        ).fetchall()
    return [dict(row) for row in rows]


def create_database_backup(preserve_filename: str | None = None) -> dict[str, Any]:
    ensure_runtime_dirs()
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"pulseguard-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')}.db"
    target = BACKUPS_DIR / filename
    with _LOCK:
        source = sqlite3.connect(DB_PATH, timeout=30)
        destination = sqlite3.connect(target, timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    preserve_filenames = {target.name}
    if preserve_filename:
        preserve_filenames.add(preserve_filename)
    _cleanup_database_backups(preserve_filenames)
    return _database_backup_info(target)


def list_database_backups() -> list[dict[str, Any]]:
    ensure_runtime_dirs()
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    return [_database_backup_info(path) for path in sorted(BACKUPS_DIR.glob("pulseguard-*.db"), reverse=True)]


def restore_database_backup(filename: str) -> dict[str, Any]:
    source_path = (BACKUPS_DIR / filename).resolve()
    if source_path.parent != BACKUPS_DIR.resolve() or not source_path.is_file():
        raise ValueError("数据库备份不存在")
    safety_backup = create_database_backup(preserve_filename=source_path.name)
    with _LOCK:
        source = sqlite3.connect(source_path, timeout=30)
        destination = sqlite3.connect(DB_PATH, timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    init_db()
    return {"restored": _database_backup_info(source_path), "safety_backup": safety_backup}


def _database_backup_info(path: Any) -> dict[str, Any]:
    stat = path.stat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def _cleanup_database_backups(preserve_filenames: set[str] | None = None) -> None:
    retention = max(1, int(get_settings().get("database_backup_retention", 7)))
    preserved = preserve_filenames or set()
    removable = [
        path
        for path in sorted(BACKUPS_DIR.glob("pulseguard-*.db"), reverse=True)
        if path.name not in preserved
    ]
    for path in removable[retention:]:
        path.unlink(missing_ok=True)


def get_settings() -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    for row in rows:
        if row["key"] not in DEFAULT_SETTINGS:
            continue
        try:
            settings[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            settings[row["key"]] = row["value"]
    return normalized_browser_settings(settings)


def get_public_settings() -> dict[str, Any]:
    settings = get_settings()
    settings["read_only_tokens"] = [
        _public_read_only_token(token)
        for token in settings.get("read_only_tokens", [])
        if isinstance(token, dict)
    ]
    settings["read_only_token_set"] = bool(settings.get("read_only_token") or settings["read_only_tokens"])
    settings["read_only_token"] = ""
    settings["notification_channels"] = [
        _public_notification_channel(channel)
        for channel in settings.get("notification_channels", [])
        if isinstance(channel, dict)
    ]
    settings["environment_variables"] = [
        _public_environment_variable(variable)
        for variable in settings.get("environment_variables", [])
        if isinstance(variable, dict)
    ]
    return settings


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULT_SETTINGS)
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"不支持的设置项：{', '.join(unknown)}")
    normalized_values = normalize_settings_update_values(values)
    timestamp = now_iso()
    with _LOCK, _connect() as conn:
        for key, value in normalized_values.items():
            if key not in allowed:
                continue
            conn.execute(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), timestamp),
            )
        if "members" in normalized_values:
            member_ids = {
                str(member.get("id") or "")
                for member in normalized_values["members"]
                if isinstance(member, dict) and member.get("id")
            }
            _prune_unknown_member_references(conn, member_ids, timestamp)
        if "notification_channels" in normalized_values:
            channel_ids = {
                str(channel.get("id") or "")
                for channel in normalized_values["notification_channels"]
                if isinstance(channel, dict) and channel.get("id")
            }
            _prune_unknown_channel_references(conn, channel_ids, timestamp)
    return get_settings()


def normalize_settings_update_values(values: dict[str, Any]) -> dict[str, Any]:
    values = _preserve_notification_channel_secrets(values)
    values = _preserve_environment_variable_secrets(values)
    normalized = normalize_settings_values(values)
    if any(key in normalized for key in {"browser_type", "enabled_browser_types", "prewarmed_browser_types", "browser_pool_sizes", "browser_pool_size"}):
        merged = normalized_browser_settings({**get_settings(), **normalized})
        normalized["enabled_browser_types"] = merged["enabled_browser_types"]
        normalized["prewarmed_browser_types"] = merged["prewarmed_browser_types"]
        normalized["browser_pool_sizes"] = merged["browser_pool_sizes"]
        normalized["browser_type"] = merged["browser_type"]
    return normalized


def create_read_only_token(name: str) -> dict[str, Any]:
    token = f"pgro_{secrets.token_urlsafe(32)}"
    item = {
        "id": f"rot_{uuid.uuid4().hex}",
        "name": str(name or "").strip() or "未命名令牌",
        "token": token,
        "created_at": now_iso(),
    }
    settings = get_settings()
    tokens = [
        token_item
        for token_item in settings.get("read_only_tokens", [])
        if isinstance(token_item, dict)
    ]
    tokens.append(item)
    update_settings({"read_only_tokens": tokens})
    public = _public_read_only_token(item)
    public["token"] = token
    return public


def delete_read_only_token(token_id: str) -> dict[str, Any]:
    target = str(token_id or "").strip()
    settings = get_settings()
    tokens = [
        token_item
        for token_item in settings.get("read_only_tokens", [])
        if isinstance(token_item, dict)
    ]
    next_tokens = [token_item for token_item in tokens if str(token_item.get("id") or "") != target]
    if len(next_tokens) == len(tokens):
        raise ValueError("只读访问令牌不存在")
    update_settings({"read_only_tokens": next_tokens})
    return get_public_settings()


def _public_read_only_token(token: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(token.get("id") or ""),
        "name": str(token.get("name") or "未命名令牌"),
        "created_at": str(token.get("created_at") or ""),
    }


def _prune_unknown_member_references(conn: sqlite3.Connection, member_ids: set[str], timestamp: str) -> None:
    for row in conn.execute("SELECT id, alert_policy_json FROM checks").fetchall():
        try:
            policy = json.loads(row["alert_policy_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(policy, dict) or not isinstance(policy.get("member_ids"), list):
            continue
        selected = [str(member_id) for member_id in policy["member_ids"] if str(member_id) in member_ids]
        if selected == policy["member_ids"]:
            continue
        if selected:
            policy["member_ids"] = selected
        else:
            policy.pop("member_ids", None)
        conn.execute(
            "UPDATE checks SET alert_policy_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(policy, ensure_ascii=False), timestamp, int(row["id"])),
        )


def _prune_unknown_channel_references(conn: sqlite3.Connection, channel_ids: set[str], timestamp: str) -> None:
    for key in ("execution_notification_channel_ids", "system_notification_channel_ids"):
        raw_value = _read_setting_value(conn, key)
        selected = raw_value if isinstance(raw_value, list) else []
        if not isinstance(selected, list):
            continue
        next_selected = [str(channel_id) for channel_id in selected if str(channel_id) in channel_ids]
        if next_selected == selected:
            continue
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(next_selected, ensure_ascii=False), timestamp),
        )

    for row in conn.execute("SELECT id, alert_policy_json FROM checks").fetchall():
        try:
            policy = json.loads(row["alert_policy_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(policy, dict) or not isinstance(policy.get("notification_channel_ids"), list):
            continue
        selected = [str(channel_id) for channel_id in policy["notification_channel_ids"] if str(channel_id) in channel_ids]
        if selected == policy["notification_channel_ids"]:
            continue
        if selected:
            policy["notification_channel_ids"] = selected
        else:
            policy.pop("notification_channel_ids", None)
        conn.execute(
            "UPDATE checks SET alert_policy_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(policy, ensure_ascii=False), timestamp, int(row["id"])),
        )

    raw_policies = _read_setting_value(conn, "alert_tag_policies")
    policies = raw_policies if isinstance(raw_policies, list) else []
    if not isinstance(policies, list):
        return
    changed = False
    for policy in policies:
        if not isinstance(policy, dict) or not isinstance(policy.get("notification_channel_ids"), list):
            continue
        selected = [str(channel_id) for channel_id in policy["notification_channel_ids"] if str(channel_id) in channel_ids]
        if selected == policy["notification_channel_ids"]:
            continue
        changed = True
        if selected:
            policy["notification_channel_ids"] = selected
        else:
            policy.pop("notification_channel_ids", None)
    if changed:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES ('alert_tag_policies', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (json.dumps(policies, ensure_ascii=False), timestamp),
        )


def _public_notification_channel(channel: dict[str, Any]) -> dict[str, Any]:
    public = dict(channel)
    public["dingtalk_secret_set"] = bool(public.get("dingtalk_secret"))
    public["dingtalk_secret"] = ""
    return public


def _public_environment_variable(variable: dict[str, Any]) -> dict[str, Any]:
    public = dict(variable)
    public["value_set"] = bool(public.get("value"))
    if public.get("secret") or is_sensitive_variable_name(str(public.get("name") or "")):
        public["secret"] = True
        public["value"] = ""
    return public


def _preserve_notification_channel_secrets(values: dict[str, Any]) -> dict[str, Any]:
    channels = values.get("notification_channels")
    if not isinstance(channels, list):
        return values

    current_channels = {
        str(channel.get("id")): channel
        for channel in get_settings().get("notification_channels", [])
        if isinstance(channel, dict) and channel.get("id")
    }
    merged_channels: list[Any] = []
    for raw_channel in channels:
        if not isinstance(raw_channel, dict):
            merged_channels.append(raw_channel)
            continue

        channel = dict(raw_channel)
        channel_id = str(channel.get("id") or "")
        has_new_secret = bool(str(channel.get("dingtalk_secret") or "").strip())
        should_clear_secret = bool(channel.pop("dingtalk_secret_clear", False))
        if not has_new_secret and not should_clear_secret and channel_id in current_channels:
            channel["dingtalk_secret"] = current_channels[channel_id].get("dingtalk_secret") or ""
        merged_channels.append(channel)

    next_values = dict(values)
    next_values["notification_channels"] = merged_channels
    return next_values


def _preserve_environment_variable_secrets(values: dict[str, Any]) -> dict[str, Any]:
    variables = values.get("environment_variables")
    if not isinstance(variables, list):
        return values

    current_variables = {
        str(variable.get("id")): variable
        for variable in get_settings().get("environment_variables", [])
        if isinstance(variable, dict) and variable.get("id")
    }
    merged_variables: list[Any] = []
    for raw_variable in variables:
        if not isinstance(raw_variable, dict):
            merged_variables.append(raw_variable)
            continue

        variable = dict(raw_variable)
        variable_id = str(variable.get("id") or "")
        has_new_value = bool(str(variable.get("value") or ""))
        should_clear_value = bool(variable.pop("value_clear", False))
        is_secret = bool(variable.get("secret", False)) or is_sensitive_variable_name(str(variable.get("name") or ""))
        if is_secret and not has_new_value and not should_clear_value and variable_id in current_variables:
            variable["value"] = current_variables[variable_id].get("value") or ""
        merged_variables.append(variable)

    next_values = dict(values)
    next_values["environment_variables"] = merged_variables
    return next_values


def get_overview() -> dict[str, Any]:
    refresh_stale_statuses()
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with _LOCK, _connect() as conn:
        ui_count = conn.execute("SELECT COUNT(*) AS count FROM checks WHERE type = 'ui'").fetchone()["count"]
        api_count = conn.execute("SELECT COUNT(*) AS count FROM checks WHERE type = 'api'").fetchone()["count"]
        state_counts = {
            str(row["monitor_status"] or "unknown"): int(row["count"])
            for row in conn.execute(
                """
                SELECT COALESCE(s.monitor_status, 'unknown') AS monitor_status, COUNT(*) AS count
                FROM checks c
                LEFT JOIN check_status s ON s.check_id = c.id
                WHERE c.enabled = 1
                GROUP BY COALESCE(s.monitor_status, 'unknown')
                """
            ).fetchall()
        }
        failing_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM check_status s
            JOIN checks c ON c.id = s.check_id
            WHERE c.enabled = 1 AND s.monitor_status = 'failing'
            """
        ).fetchone()["count"]
        today_runs = conn.execute(
            "SELECT COUNT(*) AS count FROM runs WHERE check_id > 0 AND affects_health = 1 AND started_at >= ?",
            (today,),
        ).fetchone()["count"]
        latest_run = conn.execute(
            f"SELECT {_run_summary_select()} FROM runs WHERE check_id > 0 AND affects_health = 1 ORDER BY started_at DESC, id DESC LIMIT 1"
        ).fetchone()
        recovered = conn.execute(
            """
            SELECT c.name, c.type, s.last_success_at
            FROM check_status s
            JOIN checks c ON c.id = s.check_id
            WHERE s.last_failed_at IS NOT NULL
              AND s.last_success_at IS NOT NULL
              AND s.last_success_at > s.last_failed_at
            ORDER BY s.last_success_at DESC
            LIMIT 1
            """
        ).fetchone()
        failures = conn.execute(
            f"""
            SELECT {_run_summary_select("r")}, s.consecutive_failures
            FROM check_status s
            JOIN runs r ON r.id = s.last_scheduled_run_id
            JOIN checks c ON c.id = s.check_id
            WHERE c.enabled = 1
              AND s.monitor_status = 'failing'
              AND r.status IN ('failed', 'timeout')
              AND (r.failure_kind IS NULL OR r.failure_kind = 'target')
            ORDER BY r.started_at DESC, r.id DESC
            LIMIT 8
            """
        ).fetchall()
        trends = _overview_trends(conn)

    return {
        "ui_count": ui_count,
        "api_count": api_count,
        "failing_count": failing_count,
        "suspected_failing_count": state_counts.get("suspected_failing", 0),
        "suspected_recovery_count": state_counts.get("suspected_recovery", 0),
        "suspected_count": state_counts.get("suspected_failing", 0) + state_counts.get("suspected_recovery", 0),
        "unknown_count": state_counts.get("unknown", 0),
        "stale_count": state_counts.get("stale", 0),
        "healthy_count": state_counts.get("healthy", 0),
        "today_runs": today_runs,
        "latest_run": _normalize_run(latest_run) if latest_run else None,
        "latest_recovered": dict(recovered) if recovered else None,
        "recent_failures": [_normalize_run(row) for row in failures],
        "trends": trends,
    }


def _overview_trends(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now().astimezone()
    periods = [
        ("24h", "近 24 小时", now - timedelta(hours=24)),
        ("7d", "近 7 天", now - timedelta(days=7)),
    ]
    trend_types = [("ui", "UI"), ("api", "API")]
    trends: list[dict[str, Any]] = []
    for key, label, cutoff in periods:
        rows = conn.execute(
            """
            SELECT check_type, status, duration_ms
            FROM runs
            WHERE check_id > 0
              AND affects_health = 1
              AND started_at >= ?
              AND started_at <= ?
              AND check_type IN ('ui', 'api')
              AND status IN ('ok', 'failed', 'timeout')
              AND (status = 'ok' OR failure_kind IS NULL OR failure_kind = 'target')
            """,
            (cutoff.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
        ).fetchall()
        rows_by_type: dict[str, list[sqlite3.Row]] = {check_type: [] for check_type, _ in trend_types}
        for row in rows:
            rows_by_type[str(row["check_type"])].append(row)
        trends.append(
            {
                "key": key,
                "label": label,
                "series": [
                    {
                        "check_type": check_type,
                        "label": type_label,
                        **_overview_trend_metrics(rows_by_type[check_type]),
                    }
                    for check_type, type_label in trend_types
                ],
            }
        )
    return trends


def _overview_trend_metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
    total = len(rows)
    successes = sum(1 for row in rows if row["status"] == "ok")
    failures = sum(1 for row in rows if row["status"] in {"failed", "timeout"})
    durations = sorted(int(row["duration_ms"]) for row in rows if row["status"] == "ok" and row["duration_ms"] is not None)
    return {
        "runs": total,
        "success_count": successes,
        "success_rate": round((successes / total) * 100, 1) if total else None,
        "failure_count": failures,
        "duration_p50_ms": _percentile(durations, 0.5),
        "duration_p95_ms": _percentile(durations, 0.95),
    }


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(len(values) * percentile + 0.999999) - 1))
    return values[index]


def _trend_window(period: str, start: str | None, end: str | None) -> tuple[datetime, datetime]:
    normalized = (period or "24h").strip()
    if normalized not in {"24h", "7d", "30d", "custom"}:
        raise ValueError("趋势周期无效")
    now = datetime.now().astimezone().replace(microsecond=0)
    range_end = _parse_trend_datetime(end) if end else now
    if start:
        range_start = _parse_trend_datetime(start)
    elif normalized == "custom":
        range_start = range_end - timedelta(days=1)
    else:
        range_start = range_end - timedelta(seconds=TREND_PERIOD_SECONDS[normalized])
    if range_start >= range_end:
        raise ValueError("趋势开始时间必须早于结束时间")
    return range_start, range_end


def _parse_trend_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("趋势时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone().replace(microsecond=0)


def _normalize_trend_check_type(check_type: str | None) -> str | None:
    if not check_type:
        return None
    if check_type not in {"ui", "api"}:
        raise ValueError("任务类型无效")
    return check_type


def _monitoring_checks_page(
    conn: sqlite3.Connection,
    check_type: str | None,
    q: str,
    page: int,
    page_size: int,
) -> tuple[int, list[dict[str, Any]]]:
    clauses: list[str] = []
    params: list[Any] = []
    if check_type:
        clauses.append("c.type = ?")
        params.append(check_type)
    if q:
        clauses.append("c.name LIKE ?")
        params.append(f"%{q}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    total = int(conn.execute(f"SELECT COUNT(*) AS count FROM checks c{where}", params).fetchone()["count"])
    rows = conn.execute(
        f"""
        SELECT c.*, s.current_status, s.monitor_status, s.consecutive_failures,
               s.consecutive_successes, s.last_success_at, s.last_failed_at,
               s.last_run_at, s.last_run_id, s.last_error, s.last_scheduled_at,
               s.last_scheduled_run_id, s.last_state_changed_at,
               r.duration_ms AS last_duration_ms
        FROM checks c
        LEFT JOIN check_status s ON s.check_id = c.id
        LEFT JOIN runs r ON r.id = s.last_run_id
        {where}
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    return total, [_normalize_check(row) for row in rows]


def _trend_series_by_check(
    conn: sqlite3.Connection,
    check_ids: list[int],
    range_start: datetime,
    range_end: datetime,
    max_points: int,
    hour_window: tuple[time, time] | None = None,
) -> dict[int, dict[str, Any]]:
    if not check_ids:
        return {}
    display_seconds = _trend_display_bucket_seconds(range_start, range_end, max_points)
    if hour_window is not None:
        display_seconds = max(display_seconds, 3600)
        source_granularity = _trend_hour_window_source_granularity(hour_window)
    else:
        source_granularity = _trend_source_granularity(display_seconds)
    query_start = _trend_rollup_bucket_start(range_start, TREND_GRANULARITY_SECONDS[source_granularity])
    placeholders = ", ".join("?" for _ in check_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM trend_rollups
        WHERE check_id IN ({placeholders})
          AND bucket_granularity = ?
          AND bucket_start >= ?
          AND bucket_start <= ?
        ORDER BY bucket_start ASC
        """,
        [
            *check_ids,
            source_granularity,
            query_start.isoformat(timespec="seconds"),
            range_end.isoformat(timespec="seconds"),
        ],
    ).fetchall()
    totals = {check_id: _empty_trend_accumulator() for check_id in check_ids}
    buckets: dict[int, dict[str, dict[str, Any]]] = {check_id: {} for check_id in check_ids}
    for row in rows:
        check_id = int(row["check_id"])
        if check_id not in buckets:
            continue
        bucket_dt = _parse_trend_datetime(row["bucket_start"])
        if hour_window is not None and not _hour_in_window(bucket_dt.time(), hour_window):
            continue
        _merge_rollup_into_accumulator(totals[check_id], row)
        bucket_start = _display_bucket_start(bucket_dt, range_start, display_seconds)
        bucket_key = bucket_start.isoformat(timespec="seconds")
        bucket = buckets[check_id].setdefault(bucket_key, _empty_trend_accumulator())
        _merge_rollup_into_accumulator(bucket, row)
    result: dict[int, dict[str, Any]] = {}
    for check_id in check_ids:
        points = [
            {"bucket_start": bucket_start, **_trend_metrics(accumulator)}
            for bucket_start, accumulator in sorted(buckets[check_id].items())
        ]
        result[check_id] = {**_trend_metrics(totals[check_id]), "points": points}
    return result


def _parse_hour_window(hour_start: str | None, hour_end: str | None) -> tuple[time, time] | None:
    if not hour_start and not hour_end:
        return None
    if not hour_start or not hour_end:
        raise ValueError("小时段必须同时提供开始与结束")
    start_t = _parse_clock_time(hour_start)
    end_t = _parse_clock_time(hour_end)
    if start_t == end_t:
        raise ValueError("小时段开始与结束不能相同")
    return start_t, end_t


def _parse_clock_time(value: str) -> time:
    text = str(value or "").strip()
    if not text:
        raise ValueError("小时段格式无效")
    parts = text.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError("小时段格式无效")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as exc:
        raise ValueError("小时段格式无效") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("小时段必须在 00:00 - 23:59 之间")
    return time(hour=hour, minute=minute)


def _hour_in_window(value: time, window: tuple[time, time]) -> bool:
    start_t, end_t = window
    if start_t < end_t:
        return start_t <= value < end_t
    # crosses midnight (e.g. 22:00 -> 02:00)
    return value >= start_t or value < end_t


def _trend_hour_window_source_granularity(hour_window: tuple[time, time]) -> str:
    start_t, end_t = hour_window
    if start_t.minute or end_t.minute:
        return "5m"
    return "1h"


def _trend_display_bucket_seconds(range_start: datetime, range_end: datetime, max_points: int) -> int:
    seconds = max(1, int((range_end - range_start).total_seconds()))
    raw = max(1, math.ceil(seconds / max(1, max_points)))
    for bucket in TREND_DISPLAY_BUCKET_SECONDS:
        if bucket >= raw:
            return bucket
    return math.ceil(raw / 86400) * 86400


def _trend_source_granularity(display_seconds: int) -> str:
    if display_seconds <= 1800:
        return "5m"
    if display_seconds <= 43200:
        return "1h"
    return "1d"


def _display_bucket_start(value: datetime, range_start: datetime, bucket_seconds: int) -> datetime:
    offset = max(0, int((value - range_start).total_seconds()))
    return range_start + timedelta(seconds=(offset // bucket_seconds) * bucket_seconds)


def _empty_trend_series() -> dict[str, Any]:
    return {**_trend_metrics(_empty_trend_accumulator()), "points": []}


def _empty_trend_accumulator() -> dict[str, Any]:
    return {
        "success_count": 0,
        "failure_count": 0,
        "duration_sum_ms": 0,
        "histogram": {},
        "durations": [],
        "duration_sample_count": 0,
    }


def _merge_rollup_into_accumulator(accumulator: dict[str, Any], row: sqlite3.Row) -> None:
    accumulator["success_count"] += int(row["success_count"] or 0)
    accumulator["failure_count"] += int(row["failure_count"] or 0)
    accumulator["duration_sum_ms"] += int(row["duration_sum_ms"] or 0)
    histogram = accumulator["histogram"]
    for bucket, count in _load_histogram(row["latency_histogram_json"]).items():
        histogram[bucket] = int(histogram.get(bucket, 0)) + int(count)
    samples = _load_duration_samples(row["duration_samples_json"])
    if samples:
        accumulator.setdefault("durations", []).extend(samples)
        accumulator["duration_sample_count"] = int(accumulator.get("duration_sample_count") or 0) + len(samples)


def _trend_metrics(accumulator: dict[str, Any]) -> dict[str, Any]:
    success_count = int(accumulator["success_count"] or 0)
    failure_count = int(accumulator["failure_count"] or 0)
    duration_sum = int(accumulator["duration_sum_ms"] or 0)
    durations = accumulator.get("durations")
    duration_sample_count = int(accumulator.get("duration_sample_count") or 0)
    histogram = accumulator.get("histogram") if isinstance(accumulator.get("histogram"), dict) else {}
    if isinstance(durations, list) and duration_sample_count == success_count:
        sorted_durations = sorted(int(value) for value in durations)
        p95_duration_ms = _percentile(sorted_durations, 0.95) if len(sorted_durations) >= 20 else None
        p99_duration_ms = _percentile(sorted_durations, 0.99) if len(sorted_durations) >= 100 else None
    else:
        p95_duration_ms = _histogram_percentile(histogram, 0.95, min_samples=20)
        p99_duration_ms = _histogram_percentile(histogram, 0.99, min_samples=100)
    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "duration_sum_ms": duration_sum,
        "avg_duration_ms": round(duration_sum / success_count) if success_count else None,
        "p95_duration_ms": p95_duration_ms,
        "p99_duration_ms": p99_duration_ms,
    }


def _record_trend_rollups(conn: sqlite3.Connection, run: sqlite3.Row) -> bool:
    if not _run_should_record_trend(run):
        return False
    status = str(run["status"] or "")
    check_type = str(run["check_type"] or "")
    started_at = _parse_trend_datetime(str(run["started_at"]))
    duration_ms = int(run["duration_ms"]) if status == "ok" and run["duration_ms"] is not None else None
    failure = status in {"failed", "timeout"}
    check_ids = [int(run["check_id"]), TREND_SUMMARY_CHECK_IDS[check_type]]
    for check_id in check_ids:
        for granularity, seconds in TREND_GRANULARITIES:
            _upsert_trend_rollup(
                conn,
                check_id=check_id,
                check_type=check_type,
                granularity=granularity,
                bucket_start=_trend_rollup_bucket_start(started_at, seconds),
                duration_ms=duration_ms,
                failure=failure,
            )
    return True


def _run_should_record_trend(run: sqlite3.Row) -> bool:
    status = str(run["status"] or "")
    check_type = str(run["check_type"] or "")
    if int(run["check_id"] or 0) <= 0 or check_type not in {"ui", "api"}:
        return False
    if not bool(run["affects_health"]):
        return False
    if str(run["observation_kind"] or "observation") != "observation":
        return False
    if _normalize_failure_kind(run["failure_kind"], status) == "runner":
        return False
    if status == "ok":
        return run["duration_ms"] is not None
    return status in {"failed", "timeout"}


def _upsert_trend_rollup(
    conn: sqlite3.Connection,
    check_id: int,
    check_type: str,
    granularity: str,
    bucket_start: datetime,
    duration_ms: int | None,
    failure: bool,
) -> None:
    bucket_iso = bucket_start.isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT *
        FROM trend_rollups
        WHERE check_id = ? AND bucket_granularity = ? AND bucket_start = ?
        """,
        (check_id, granularity, bucket_iso),
    ).fetchone()
    success_count = int(row["success_count"] or 0) if row else 0
    failure_count = int(row["failure_count"] or 0) if row else 0
    duration_sum = int(row["duration_sum_ms"] or 0) if row else 0
    histogram = _load_histogram(row["latency_histogram_json"]) if row else {}
    duration_samples = _load_duration_samples(row["duration_samples_json"]) if row else []
    if duration_ms is not None:
        normalized_duration = max(0, int(duration_ms))
        success_count += 1
        duration_sum += normalized_duration
        duration_samples.append(normalized_duration)
        bucket = str(_latency_bucket(normalized_duration))
        histogram[bucket] = int(histogram.get(bucket, 0)) + 1
    if failure:
        failure_count += 1
    metrics = _trend_metrics(
        {
            "success_count": success_count,
            "failure_count": failure_count,
            "duration_sum_ms": duration_sum,
            "histogram": histogram,
            "durations": duration_samples,
            "duration_sample_count": len(duration_samples),
        }
    )
    payload = (
        check_id,
        check_type,
        granularity,
        bucket_iso,
        success_count,
        failure_count,
        duration_sum,
        metrics["avg_duration_ms"],
        metrics["p95_duration_ms"],
        metrics["p99_duration_ms"],
        json.dumps(histogram, ensure_ascii=False, sort_keys=True),
        json.dumps(duration_samples, ensure_ascii=False),
        now_iso(),
    )
    conn.execute(
        """
        INSERT INTO trend_rollups (
            check_id, check_type, bucket_granularity, bucket_start, success_count,
            failure_count, duration_sum_ms, avg_duration_ms, p95_duration_ms,
            p99_duration_ms, latency_histogram_json, duration_samples_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(check_id, bucket_granularity, bucket_start) DO UPDATE SET
            check_type = excluded.check_type,
            success_count = excluded.success_count,
            failure_count = excluded.failure_count,
            duration_sum_ms = excluded.duration_sum_ms,
            avg_duration_ms = excluded.avg_duration_ms,
            p95_duration_ms = excluded.p95_duration_ms,
            p99_duration_ms = excluded.p99_duration_ms,
            latency_histogram_json = excluded.latency_histogram_json,
            duration_samples_json = excluded.duration_samples_json,
            updated_at = excluded.updated_at
        """,
        payload,
    )


def _trend_rollup_bucket_start(value: datetime, seconds: int) -> datetime:
    if seconds == 86400:
        return value.replace(hour=0, minute=0, second=0, microsecond=0)
    if seconds == 3600:
        return value.replace(minute=0, second=0, microsecond=0)
    minute = (value.minute // 5) * 5
    return value.replace(minute=minute, second=0, microsecond=0)


def _latency_bucket(duration_ms: int) -> int:
    value = max(0, int(duration_ms))
    for bucket in TREND_LATENCY_BUCKETS_MS:
        if value <= bucket:
            return bucket
    return TREND_LATENCY_BUCKETS_MS[-1]


def _load_histogram(value: Any) -> dict[str, int]:
    try:
        raw = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): int(count) for key, count in raw.items() if int(count or 0) > 0}


def _load_duration_samples(value: Any) -> list[int]:
    try:
        raw = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    samples: list[int] = []
    for item in raw:
        try:
            samples.append(max(0, int(item)))
        except (TypeError, ValueError):
            continue
    return samples


def _histogram_percentile(histogram: dict[str, int], percentile: float, min_samples: int) -> int | None:
    total = sum(int(count) for count in histogram.values())
    if total < min_samples:
        return None
    target = max(1, math.ceil(total * percentile))
    seen = 0
    for bucket in sorted((int(key), int(count)) for key, count in histogram.items()):
        seen += bucket[1]
        if seen >= target:
            return bucket[0]
    return None


def _backfill_trend_rollups(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT *
        FROM runs
        WHERE trend_recorded_at IS NULL
          AND check_id > 0
          AND affects_health = 1
          AND status IN ('ok', 'failed', 'timeout')
        ORDER BY started_at ASC, id ASC
        """
    ).fetchall()
    if not rows:
        return
    timestamp = now_iso()
    for row in rows:
        _record_trend_rollups(conn, row)
        conn.execute("UPDATE runs SET trend_recorded_at = ? WHERE id = ?", (timestamp, int(row["id"])))


def cleanup_old_data(settings: dict[str, Any] | None = None) -> int:
    settings = settings or get_settings()
    retention_days = int(settings.get("run_retention_days", 30))
    cutoff = (datetime.now().astimezone() - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with _LOCK, _connect() as conn:
        _archive_runs_before(conn, cutoff)
        expired_run_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM runs WHERE created_at < ?", (cutoff,)).fetchall()
        ]
        cursor = conn.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,))
        removed_runs = cursor.rowcount
        if expired_run_ids:
            placeholders = ", ".join("?" for _ in expired_run_ids)
            conn.execute(
                f"UPDATE check_status SET last_run_id = NULL WHERE last_run_id IN ({placeholders})",
                expired_run_ids,
            )
    removed_artifacts = cleanup_old_artifacts(settings)
    removed_artifact_count = sum(len(paths) for paths in removed_artifacts.values())
    if removed_artifact_count:
        with _LOCK, _connect() as conn:
            for column, paths in removed_artifacts.items():
                _clear_artifact_paths(conn, column, paths)
    return removed_runs + removed_artifact_count


def _archive_runs_before(conn: sqlite3.Connection, cutoff: str) -> None:
    rows = conn.execute(
        """
        SELECT
            substr(started_at, 1, 10) AS archive_date,
            check_type,
            status,
            COUNT(*) AS run_count,
            COALESCE(SUM(COALESCE(duration_ms, 0)), 0) AS duration_sum_ms,
            SUM(CASE WHEN duration_ms IS NULL THEN 0 ELSE 1 END) AS duration_sample_count,
            MAX(started_at) AS last_run_at
        FROM runs
        WHERE created_at < ?
          AND check_id > 0
          AND status IN ('ok', 'failed', 'timeout', 'skipped')
        GROUP BY archive_date, check_type, status
        """,
        (cutoff,),
    ).fetchall()
    timestamp = now_iso()
    for row in rows:
        conn.execute(
            """
            INSERT INTO run_archives (
                archive_date, check_type, status, run_count, duration_sum_ms,
                duration_sample_count, last_run_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_date, check_type, status) DO UPDATE SET
                run_count = run_archives.run_count + excluded.run_count,
                duration_sum_ms = run_archives.duration_sum_ms + excluded.duration_sum_ms,
                duration_sample_count = run_archives.duration_sample_count + excluded.duration_sample_count,
                last_run_at = CASE
                    WHEN excluded.last_run_at > COALESCE(run_archives.last_run_at, '') THEN excluded.last_run_at
                    ELSE run_archives.last_run_at
                END,
                updated_at = excluded.updated_at
            """,
            (
                row["archive_date"] or "",
                row["check_type"] or "",
                row["status"] or "",
                int(row["run_count"] or 0),
                int(row["duration_sum_ms"] or 0),
                int(row["duration_sample_count"] or 0),
                row["last_run_at"],
                timestamp,
            ),
        )


def _clear_artifact_paths(conn: sqlite3.Connection, column: str, paths: list[str]) -> None:
    if not paths:
        return
    if column not in {"screenshot_path", "trace_path", "response_path"}:
        raise ValueError(f"不支持的产物字段：{column}")
    placeholders = ", ".join("?" for _ in paths)
    conn.execute(f"UPDATE runs SET {column} = NULL WHERE {column} IN ({placeholders})", paths)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_check_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(checks)").fetchall()}
    columns = {
        "assertions_json": "TEXT",
        "viewport_mode": "TEXT NOT NULL DEFAULT 'web'",
        "setup_script": "TEXT NOT NULL DEFAULT ''",
        "alert_policy_json": "TEXT NOT NULL DEFAULT '{}'",
        "runner_selection_mode": "TEXT NOT NULL DEFAULT 'selected_parallel'",
        "runner_ids_json": "TEXT NOT NULL DEFAULT '[\"local\"]'",
        "browser_selection_mode": "TEXT NOT NULL DEFAULT 'selected_parallel'",
        "browser_types_json": "TEXT NOT NULL DEFAULT '[\"chromium\"]'",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE checks ADD COLUMN {name} {column_type}")


def _ensure_run_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    columns = {
        "request_snapshot": "TEXT",
        "response_snapshot": "TEXT",
        "runner_id": "TEXT",
        "runner_name": "TEXT",
        "runner_address": "TEXT",
        "runner_region": "TEXT",
        "runner_browser_version": "TEXT",
        "browser_type": "TEXT",
        "failure_kind": "TEXT",
        "failure_fingerprint": "TEXT",
        "deduplicated_count": "INTEGER NOT NULL DEFAULT 0",
        "notification_status": "TEXT",
        "notification_channel": "TEXT",
        "notification_error": "TEXT",
        "notification_sent_at": "TEXT",
        "trigger": "TEXT NOT NULL DEFAULT 'legacy'",
        "observation_kind": "TEXT NOT NULL DEFAULT 'observation'",
        "affects_health": "INTEGER NOT NULL DEFAULT 1",
        "run_group_id": "TEXT",
        "trend_recorded_at": "TEXT",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {column_type}")


def _ensure_trend_rollup_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trend_rollups)").fetchall()}
    columns = {
        "duration_samples_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE trend_rollups ADD COLUMN {name} {column_type}")


def _ensure_check_status_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(check_status)").fetchall()}
    columns = {
        "monitor_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "consecutive_successes": "INTEGER NOT NULL DEFAULT 0",
        "last_scheduled_at": "TEXT",
        "last_scheduled_run_id": "INTEGER",
        "last_state_changed_at": "TEXT",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE check_status ADD COLUMN {name} {column_type}")
    conn.execute(
        """
        UPDATE check_status
        SET monitor_status = CASE current_status
            WHEN 'ok' THEN 'healthy'
            WHEN 'failed' THEN 'failing'
            ELSE COALESCE(NULLIF(monitor_status, ''), 'unknown')
        END
        WHERE monitor_status = 'unknown' AND current_status IN ('ok', 'failed')
        """
    )
    conn.execute(
        """
        UPDATE check_status
        SET current_status = 'unknown',
            monitor_status = 'unknown',
            consecutive_failures = 0,
            consecutive_successes = 0
        WHERE current_status = 'suspected' OR monitor_status = 'suspected'
        """
    )


def _ensure_probe_runner_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(probe_runners)").fetchall()}
    migrate_legacy_insecure_http = "allow_insecure_http" not in existing
    columns = {
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "role": "TEXT NOT NULL DEFAULT 'child'",
        "token_value": "TEXT NOT NULL DEFAULT ''",
        "token_hash": "TEXT NOT NULL DEFAULT ''",
        "token_hint": "TEXT NOT NULL DEFAULT ''",
        "installed_browser_types_json": "TEXT NOT NULL DEFAULT '[]'",
        "available_browser_types_json": "TEXT NOT NULL DEFAULT '[]'",
        "connection_mode": "TEXT NOT NULL DEFAULT 'manual'",
        "allow_insecure_http": "INTEGER NOT NULL DEFAULT 0",
        "relay_token_hash": "TEXT NOT NULL DEFAULT ''",
        "relay_token_hint": "TEXT NOT NULL DEFAULT ''",
        "relay_token_version": "INTEGER NOT NULL DEFAULT 0",
        "relay_previous_token_hash": "TEXT NOT NULL DEFAULT ''",
        "relay_previous_token_hint": "TEXT NOT NULL DEFAULT ''",
        "relay_previous_token_version": "INTEGER NOT NULL DEFAULT 0",
        "relay_previous_token_expires_at": "TEXT",
        "allocated_internal_port": "INTEGER",
        "deploy_command_expires_at": "TEXT",
        "relay_last_seen_at": "TEXT",
        "worker_health_last_success_at": "TEXT",
        "last_disconnect_reason": "TEXT NOT NULL DEFAULT ''",
        "was_available": "INTEGER NOT NULL DEFAULT 0",
        "unavailable_since": "TEXT",
        "unavailable_notified_at": "TEXT",
        "created_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE probe_runners ADD COLUMN {name} {column_type}")
    if migrate_legacy_insecure_http:
        conn.execute(
            """
            UPDATE probe_runners
            SET allow_insecure_http = 1
            WHERE role = 'child'
              AND connection_mode = 'manual'
              AND TRIM(address) <> ''
              AND (LOWER(address) LIKE 'http://%' OR INSTR(address, '://') = 0)
            """
        )
    timestamp = now_iso()
    conn.execute(
        "UPDATE probe_runners SET created_at = COALESCE(NULLIF(created_at, ''), updated_at, last_seen_at, ?) WHERE created_at = ''",
        (timestamp,),
    )
    _migrate_plaintext_runner_tokens(conn)


def _ensure_local_probe_runner(conn: sqlite3.Connection) -> None:
    timestamp = now_iso()
    name = str(_read_setting_value(conn, "local_runner_name") or "local").strip() or "local"
    address = str(_read_setting_value(conn, "local_runner_address") or "127.0.0.1").strip()
    region = str(_read_setting_value(conn, "local_runner_region") or "local").strip() or "local"
    conn.execute(
        """
        INSERT INTO probe_runners (
            runner_id, name, address, network_region, browser_version, installed_browser_types_json, available_browser_types_json, status,
            metadata_json, enabled, role, token_hash, token_hint, created_at,
            connection_mode, was_available, last_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(runner_id) DO UPDATE SET
            name = excluded.name,
            address = excluded.address,
            network_region = excluded.network_region,
            status = 'ok',
            role = 'local',
            connection_mode = 'manual',
            was_available = 1,
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        (
            LOCAL_RUNNER_ID,
            name,
            address,
            region,
            "",
            json.dumps([DEFAULT_BROWSER_TYPE], ensure_ascii=False),
            json.dumps([DEFAULT_BROWSER_TYPE], ensure_ascii=False),
            "ok",
            "{}",
            1,
            "local",
            "",
            "",
            timestamp,
            "manual",
            1,
            timestamp,
            timestamp,
        ),
    )


def _ensure_default_settings(conn: sqlite3.Connection) -> None:
    timestamp = now_iso()
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), timestamp),
        )


def _migrate_read_only_tokens(conn: sqlite3.Connection) -> None:
    legacy_token = str(_read_setting_value(conn, "read_only_token") or "").strip()
    legacy_name = str(_read_setting_value(conn, "read_only_token_name") or "").strip() or "旧版只读令牌"
    tokens_value = _read_setting_value(conn, "read_only_tokens")
    tokens = tokens_value if isinstance(tokens_value, list) else []
    timestamp = now_iso()

    if legacy_token:
        normalized_tokens = [token for token in tokens if isinstance(token, dict)]
        if not any(str(token.get("token") or "") == legacy_token for token in normalized_tokens):
            normalized_tokens.append(
                {
                    "id": f"rot_{uuid.uuid4().hex}",
                    "name": legacy_name,
                    "token": legacy_token,
                    "created_at": timestamp,
                }
            )
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("read_only_tokens", json.dumps(normalized_tokens, ensure_ascii=False), timestamp),
        )
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            ("read_only_token", json.dumps("", ensure_ascii=False), timestamp),
        )
    conn.execute("DELETE FROM settings WHERE key = 'read_only_token_name'")


def _migrate_plaintext_runner_tokens(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT runner_id, token_value, token_hash
        FROM probe_runners
        WHERE token_value != ''
        """
    ).fetchall()
    for row in rows:
        token = str(row["token_value"] or "").strip()
        if not token or token.startswith(ENCRYPTED_RUNNER_TOKEN_PREFIX):
            continue
        token_hash = str(row["token_hash"] or "").strip() or runner_token_hash(token)
        conn.execute(
            """
            UPDATE probe_runners
            SET token_value = ?,
                token_hash = ?,
                token_hint = ?
            WHERE runner_id = ?
            """,
            (_store_runner_token(token), token_hash, _token_hint(token), row["runner_id"]),
        )


def _read_setting_value(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def _read_runtime_state(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT value, updated_at FROM runtime_state WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table: runtime_state" in str(exc):
            return None
        raise
    if not row:
        return None
    try:
        value = json.loads(row["value"])
    except json.JSONDecodeError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    value["updated_at"] = value.get("updated_at") or row["updated_at"]
    return value


def _write_runtime_state(conn: sqlite3.Connection, key: str, value: dict[str, Any], timestamp: str) -> None:
    conn.execute(
        """
        INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), timestamp),
    )


def _read_deployment_state(conn: sqlite3.Connection) -> dict[str, Any]:
    state = _read_runtime_state(conn, DEPLOYMENT_STATE_KEY) or {}
    return {
        "active": bool(state.get("active")),
        "reason": str(state.get("reason") or ""),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "updated_at": state.get("updated_at"),
    }


def _discard_incomplete_runs(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id FROM runs WHERE status IN ('pending', 'running')").fetchall()
    run_ids = [int(row["id"]) for row in rows]
    if not run_ids:
        return 0
    placeholders = ", ".join("?" for _ in run_ids)
    conn.execute(f"UPDATE check_status SET last_run_id = NULL WHERE last_run_id IN ({placeholders})", run_ids)
    conn.execute(f"UPDATE check_status SET last_scheduled_run_id = NULL WHERE last_scheduled_run_id IN ({placeholders})", run_ids)
    cursor = conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)
    return cursor.rowcount


def _mark_interrupted_runs(conn: sqlite3.Connection) -> None:
    if _read_deployment_state(conn).get("active"):
        _discard_incomplete_runs(conn)
        return
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE runs
        SET status = 'skipped',
            finished_at = COALESCE(finished_at, ?),
            duration_ms = COALESCE(duration_ms, 0),
            error_message = COALESCE(error_message, '服务启动时发现任务未完成，已标记为中断'),
            failure_kind = COALESCE(failure_kind, 'runner'),
            notification_status = COALESCE(notification_status, 'not_required')
        WHERE status IN ('pending', 'running')
        """,
        (timestamp,),
    )


def _seed_demo_checks(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS count FROM checks").fetchone()["count"]
    if count:
        return
    timestamp = now_iso()
    for item in DEMO_CHECKS:
        conn.execute(
            """
            INSERT INTO checks (
                name, type, enabled, interval_seconds, timeout_ms, entry_url, viewport_mode, method,
                headers_json, body, assertions_json, setup_script, script, tags, alert_policy_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["name"],
                item["type"],
                1 if item["enabled"] else 0,
                item["interval_seconds"],
                item["timeout_ms"],
                item["entry_url"],
                item.get("viewport_mode") or "web",
                item["method"],
                item["headers_json"],
                item["body"],
                item.get("assertions_json") or "[]",
                item.get("setup_script") or "",
                item["script"],
                item["tags"],
                item.get("alert_policy_json") or "{}",
                timestamp,
                timestamp,
            ),
        )


def _normalize_check(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["enabled"] = bool(data["enabled"])
    data.pop("setup_url", None)
    data["viewport_mode"] = data.get("viewport_mode") or "web"
    data["headers_json"] = data.get("headers_json") or "{}"
    data["body"] = data.get("body") or ""
    data["assertions_json"] = data.get("assertions_json") or "[]"
    data["setup_script"] = data.get("setup_script") or ""
    data["script"] = data.get("script") or ""
    data["tags"] = data.get("tags") or ""
    data["alert_policy_json"] = data.get("alert_policy_json") or "{}"
    data["runner_selection_mode"] = _normalize_runner_selection_mode(data.get("runner_selection_mode"))
    data["runner_ids"] = _normalize_runner_ids(data.get("runner_ids_json"))
    data["browser_selection_mode"] = _normalize_browser_selection_mode(data.get("browser_selection_mode"))
    data["browser_types"] = _normalize_check_browser_types(data)
    if not data["enabled"]:
        data["monitor_status"] = "disabled"
    else:
        data["monitor_status"] = data.get("monitor_status") or "unknown"
        if data["monitor_status"] not in HEALTH_STATES:
            data["monitor_status"] = "unknown"
    data["current_status"] = data["monitor_status"]
    data["consecutive_failures"] = int(data.get("consecutive_failures") or 0)
    data["consecutive_successes"] = int(data.get("consecutive_successes") or 0)
    data["last_duration_ms"] = data.get("last_duration_ms")
    return data


def _normalize_run(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["duration_ms"] = data.get("duration_ms")
    data["consecutive_failures"] = int(data.get("consecutive_failures") or 0)
    data["deduplicated_count"] = int(data.get("deduplicated_count") or 0)
    data["failure_kind"] = _normalize_failure_kind(data.get("failure_kind"), str(data.get("status") or ""))
    data["runner_id"] = data.get("runner_id") or (LOCAL_RUNNER_ID if data.get("runner_name") else "")
    data["browser_type"] = (data.get("browser_type") or DEFAULT_BROWSER_TYPE) if data.get("check_type") == "ui" else ""
    data["trigger"] = data.get("trigger") or "legacy"
    data["observation_kind"] = data.get("observation_kind") or "observation"
    data["affects_health"] = bool(data.get("affects_health"))
    data["run_group_id"] = data.get("run_group_id") or ""
    return data


def _run_summary_select(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{column}" for column in RUN_SUMMARY_COLUMNS)


def _normalize_failure_kind(value: Any, status: str) -> str:
    failure_kind = str(value or "").strip()
    if failure_kind in {"none", "target", "runner"}:
        return failure_kind
    if status in {"failed", "timeout"}:
        return "target"
    if status == "skipped":
        return "runner"
    return "none"


def _normalize_heartbeat(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload_json") or "{}")
    except json.JSONDecodeError:
        data["payload"] = {}
    data.pop("payload_json", None)
    return data


def _normalize_audit_event(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload_json") or "{}")
    except json.JSONDecodeError:
        data["payload"] = {}
    data.pop("payload_json", None)
    return data


def _normalize_check_version(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["snapshot"] = json.loads(data.get("snapshot_json") or "{}")
    except json.JSONDecodeError:
        data["snapshot"] = {}
    data.pop("snapshot_json", None)
    return data


def _normalize_run_archive(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("id", "run_count", "duration_sum_ms", "duration_sample_count"):
        data[key] = int(data.get(key) or 0)
    return data


def _normalize_probe_runner(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata"] = json.loads(data.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        data["metadata"] = {}
    data.pop("metadata_json", None)
    data["enabled"] = bool(data.get("enabled", True))
    data["role"] = _normalize_runner_role(data.get("role"))
    data["installed_browser_types"] = _normalize_runner_browser_types(data.get("installed_browser_types_json"))
    data["available_browser_types"] = _normalize_runner_browser_types(data.get("available_browser_types_json"))
    data["browser_type_status"] = browser_type_status(data["installed_browser_types"], get_settings())
    data["connection_mode"] = _normalize_runner_connection_mode(data.get("connection_mode"))
    data["allow_insecure_http"] = bool(data.get("allow_insecure_http"))
    data["relay_token_version"] = int(data.get("relay_token_version") or 0)
    data["relay_previous_token_version"] = int(data.get("relay_previous_token_version") or 0)
    data["allocated_internal_port"] = data.get("allocated_internal_port")
    data["was_available"] = bool(data.get("was_available"))
    data["token_set"] = bool(data.get("token_hash"))
    data["relay_token_set"] = bool(data.get("relay_token_hash"))
    data["relay_token_rotation_pending"] = bool(data.get("relay_previous_token_hash")) and not _timestamp_is_past(
        data.get("relay_previous_token_expires_at")
    )
    if not data["enabled"]:
        data["status"] = "disabled"
    elif data["connection_mode"] == "relay":
        data["status"] = _derive_relay_runner_status(data)
    data["available"] = _runner_available(data)
    data.pop("token_value", None)
    data.pop("token_hash", None)
    data.pop("installed_browser_types_json", None)
    data.pop("available_browser_types_json", None)
    data.pop("relay_token_hash", None)
    data.pop("relay_previous_token_hash", None)
    data.pop("relay_previous_token_hint", None)
    data.pop("relay_previous_token_version", None)
    data.pop("relay_previous_token_expires_at", None)
    return data


def _derive_relay_runner_status(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "")
    if status in RELAY_PRE_ACTIVATION_STATUSES:
        return status
    if status == "available":
        relay_fresh = _timestamp_is_fresh(data.get("relay_last_seen_at"), RELAY_HEARTBEAT_TIMEOUT_SECONDS)
        health_fresh = _timestamp_is_fresh(data.get("worker_health_last_success_at"), RUNNER_HEARTBEAT_TIMEOUT_SECONDS)
        if not relay_fresh:
            return "unavailable" if data.get("was_available") else "connecting"
        if not health_fresh:
            return "unhealthy" if data.get("was_available") else "connecting"
    return status


def _normalize_runner_id(value: Any) -> str:
    runner_id = str(value or "").strip()
    if not runner_id:
        raise ValueError("Runner ID cannot be empty")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", runner_id):
        raise ValueError("Runner ID format is invalid")
    return runner_id


def _normalize_runner_role(value: Any) -> str:
    role = str(value or "child").strip().lower()
    return role if role in RUNNER_ROLES else "child"


def _normalize_runner_connection_mode(value: Any) -> str:
    mode = str(value or "manual").strip().lower()
    return mode if mode in RUNNER_CONNECTION_MODES else "manual"


def _normalize_runner_selection_mode(value: Any) -> str:
    mode = str(value or "selected_parallel").strip()
    return mode if mode in RUNNER_SELECTION_MODES else "selected_parallel"


def _normalize_browser_selection_mode(value: Any) -> str:
    return normalize_browser_selection_mode(value)


def _normalize_runner_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [value]
    else:
        parsed = value
    if not isinstance(parsed, list):
        parsed = []
    result: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        runner_id = str(item or "").strip()
        if not runner_id or runner_id in seen:
            continue
        seen.add(runner_id)
        result.append(runner_id)
    return result or [LOCAL_RUNNER_ID]


def _normalize_check_browser_types(data: dict[str, Any]) -> list[str]:
    if str(data.get("type") or "ui") != "ui":
        return []
    value = data.get("browser_types")
    if value is None:
        value = data.get("browser_types_json")
    return normalize_browser_types(value, default=[DEFAULT_BROWSER_TYPE])


def _normalize_runner_browser_types(value: Any) -> list[str]:
    return normalize_browser_types(value, default=[], allow_empty=True)


def _token_hint(token: str) -> str:
    token = str(token or "")
    return token[-6:] if len(token) >= 6 else token


def _runner_available(data: dict[str, Any]) -> bool:
    if not data.get("enabled"):
        return False
    if str(data.get("status") or "") == "updating":
        return False
    if str(data.get("role") or "") == "local":
        return str(data.get("status") or "ok") != "offline"
    if str(data.get("connection_mode") or "manual") == "relay":
        return (
            str(data.get("status") or "") == "available"
            and _timestamp_is_fresh(data.get("relay_last_seen_at"), RELAY_HEARTBEAT_TIMEOUT_SECONDS)
            and _timestamp_is_fresh(data.get("worker_health_last_success_at"), RUNNER_HEARTBEAT_TIMEOUT_SECONDS)
        )
    if str(data.get("status") or "ok") == "offline":
        return False
    return _timestamp_is_fresh(data.get("last_seen_at"), RUNNER_HEARTBEAT_TIMEOUT_SECONDS)


def _timestamp_is_fresh(value: Any, timeout_seconds: int) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return False
    age = datetime.now(parsed.tzinfo) - parsed
    return age.total_seconds() <= timeout_seconds


def _timestamp_is_past(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return False
    return parsed < datetime.now(parsed.tzinfo)


def _previous_relay_token_matches(row: sqlite3.Row, token_hash_value: str, version: int) -> bool:
    previous_hash = str(row["relay_previous_token_hash"] or "")
    previous_version = int(row["relay_previous_token_version"] or 0)
    if not previous_hash or previous_version <= 0:
        return False
    if _timestamp_is_past(row["relay_previous_token_expires_at"]):
        return False
    return int(version or 0) == previous_version and secrets.compare_digest(previous_hash, token_hash_value)


def _relay_token_rotation_pending(row: sqlite3.Row) -> bool:
    return bool(str(row["relay_previous_token_hash"] or "")) and not _timestamp_is_past(row["relay_previous_token_expires_at"])


def _allocate_relay_internal_port(conn: sqlite3.Connection) -> int:
    now = datetime.now().astimezone()
    cutoff = (now - timedelta(seconds=RELAY_PORT_QUARANTINE_SECONDS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM relay_port_quarantine WHERE released_at < ?", (cutoff,))
    used = {
        int(row["allocated_internal_port"])
        for row in conn.execute("SELECT allocated_internal_port FROM probe_runners WHERE allocated_internal_port IS NOT NULL").fetchall()
    }
    quarantined = {int(row["port"]) for row in conn.execute("SELECT port FROM relay_port_quarantine").fetchall()}
    for port in range(RELAY_INTERNAL_PORT_START, RELAY_INTERNAL_PORT_END + 1):
        if port not in used and port not in quarantined:
            return port
    raise ValueError("relay 内部端口池已用尽")


def _expire_probe_runner_provisions(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(probe_runners)").fetchall()}
    if not columns:
        return
    timestamp = now_iso()
    deploy_expiry_columns = {"connection_mode", "status", "was_available", "deploy_command_expires_at", "updated_at"}
    if deploy_expiry_columns <= columns:
        conn.execute(
            """
            UPDATE probe_runners
            SET status = 'expired',
                updated_at = ?
            WHERE connection_mode = 'relay'
              AND status IN ('pending_deployment', 'connecting')
              AND COALESCE(was_available, 0) = 0
              AND deploy_command_expires_at IS NOT NULL
              AND deploy_command_expires_at < ?
            """,
            (timestamp, timestamp),
        )
    rotation_expiry_columns = {
        "relay_token_hash",
        "relay_token_hint",
        "relay_token_version",
        "relay_previous_token_hash",
        "relay_previous_token_hint",
        "relay_previous_token_version",
        "relay_previous_token_expires_at",
        "status",
        "last_disconnect_reason",
        "updated_at",
    }
    if rotation_expiry_columns <= columns:
        conn.execute(
            """
            UPDATE probe_runners
            SET relay_token_hash = CASE
                    WHEN relay_previous_token_hash <> '' THEN ''
                    ELSE relay_token_hash
                END,
                relay_token_hint = CASE
                    WHEN relay_previous_token_hash <> '' THEN ''
                    ELSE relay_token_hint
                END,
                relay_token_version = CASE
                    WHEN relay_previous_token_hash <> '' THEN 0
                    ELSE relay_token_version
                END,
                status = CASE
                    WHEN relay_previous_token_hash <> '' THEN 'expired'
                    ELSE status
                END,
                last_disconnect_reason = CASE
                    WHEN relay_previous_token_hash <> '' THEN 'relay token rotation expired'
                    ELSE last_disconnect_reason
                END,
                relay_previous_token_hash = '',
                relay_previous_token_hint = '',
                relay_previous_token_version = 0,
                relay_previous_token_expires_at = NULL,
                updated_at = ?
            WHERE relay_previous_token_expires_at IS NOT NULL
              AND relay_previous_token_expires_at < ?
            """,
            (timestamp, timestamp),
        )


def _check_snapshot(check: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "name",
        "type",
        "enabled",
        "interval_seconds",
        "timeout_ms",
        "entry_url",
        "viewport_mode",
        "method",
        "headers_json",
        "body",
        "assertions_json",
        "setup_script",
        "script",
        "tags",
        "alert_policy_json",
        "runner_selection_mode",
        "runner_ids",
        "browser_selection_mode",
        "browser_types",
    )
    return {field: check.get(field) for field in fields}
