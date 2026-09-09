"""Run the reviewed used-car candidate against a fresh local SQLite database.

This is an evidence script for the migration plan.  It publishes the candidate
only inside a temporary database, executes IDs 34–48 through the real
``CheckRunner`` and HTTP client, and then repeats the same 15 checks with an
impossible status assertion.  It never reads ``data/pulseguard.db`` and does
not call a PulseGuard HTTP endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.script_modules import capture_module_bundle, source_hash  # noqa: E402
from scripts.migrate_usedcar_modules import (  # noqa: E402
    DYNAMIC_IDS,
    REQUEST_COUNTS,
    TARGET_IDS,
    _candidate_scripts,
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _payloads(path: Path) -> dict[int, dict[str, Any]]:
    value = _read_json(path)
    items = value.get("checks") if isinstance(value, Mapping) else None
    if not isinstance(items, list):
        raise ValueError("更新候选缺少 checks")
    result: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("payload"), Mapping):
            raise ValueError("更新候选项缺少 payload")
        result[int(item.get("id") or 0)] = dict(item["payload"])
    if set(result) != set(TARGET_IDS):
        raise ValueError(f"候选任务集合不是34–48：{sorted(result)}")
    return result


def _negative_assertions(raw: str | None) -> str:
    try:
        assertions = json.loads(raw or "[]")
    except json.JSONDecodeError:
        assertions = []
    if not isinstance(assertions, list):
        assertions = []
    changed = False
    for assertion in assertions:
        if isinstance(assertion, dict) and assertion.get("type") == "status_code":
            assertion["expected_status"] = 599
            changed = True
            break
    if not changed:
        assertions.append({"id": "migration-negative-status", "type": "status_code", "enabled": True, "expected_status": 599})
    return json.dumps(assertions, ensure_ascii=False)


def _snapshot_details(result: Mapping[str, Any]) -> tuple[int, int, bool]:
    raw = result.get("request_snapshot")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return 0, 0, False
    if not isinstance(raw, Mapping):
        return 0, 0, False
    diagnostics = raw.get("diagnostics")
    requests = diagnostics.get("http_requests") if isinstance(diagnostics, Mapping) else None
    response = result.get("response_snapshot")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            response = None
    assertions = response.get("assertions") if isinstance(response, Mapping) else None
    status_599_failed = any(
        isinstance(item, Mapping)
        and item.get("status") == "failed"
        and item.get("expected") == 599
        for item in (assertions if isinstance(assertions, list) else [])
    )
    return (
        len(requests) if isinstance(requests, list) else 0,
        len(assertions) if isinstance(assertions, list) else 0,
        status_599_failed,
    )


def _result_row(result: Mapping[str, Any], expected_requests: int, expected_assertions: int) -> dict[str, Any]:
    error = str(result.get("error_message") or "").replace("\r", " ").replace("\n", " ")
    request_count, assertion_count, status_599_failed = _snapshot_details(result)
    return {
        "status": str(result.get("status") or ""),
        "requests": request_count,
        "expected_requests": expected_requests,
        "assertions": assertion_count,
        "expected_assertions": expected_assertions,
        "intended_status_assertion_failed": status_599_failed,
        "error": error[:240],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    update = _payloads(args.update_payload)
    expected_assertions = {
        target_id: len(json.loads(update[target_id].get("assertions_json") or "[]"))
        for target_id in TARGET_IDS
    }
    module_source = args.module_source.read_text(encoding="utf-8")
    scripts = _candidate_scripts()
    with tempfile.TemporaryDirectory(prefix="pulseguard-usedcar-validation-") as temporary:
        root = Path(temporary)
        os.environ.update(
            {
                "PULSEGUARD_DATA_DIR": str(root / "data"),
                "PULSEGUARD_DB_PATH": str(root / "data" / "pulseguard.db"),
                "PULSEGUARD_REPORTS_DIR": str(root / "reports"),
                "PULSEGUARD_BACKUPS_DIR": str(root / "backups"),
            }
        )
        from backend.app import storage
        from backend.app.runner import CheckRunner

        storage.DB_PATH = root / "data" / "pulseguard.db"
        storage.BACKUPS_DIR = root / "backups"
        storage.init_db()
        project = storage.default_project_id()
        bundle = capture_module_bundle(
            project,
            None,
            [{"name": "usedcar", "source": module_source, "description": "二手车探活公共方法"}],
            release_hash=None,
        )
        draft = storage.update_project_library_draft(project, 0, [{"name": "usedcar", "source": module_source, "description": "二手车探活公共方法"}])
        published = storage.publish_project_library(project, int(draft["draft_revision"]))
        local_ids: dict[int, int] = {}
        for target_id in TARGET_IDS:
            payload = copy.deepcopy(update[target_id])
            payload["script"] = scripts.get(target_id, "")
            payload["project_id"] = project
            local = storage.create_check(payload, project_id=project)
            local_ids[target_id] = int(local["id"])

        runner = CheckRunner()
        await runner.start()
        positive: dict[str, Any] = {}
        negative: dict[str, Any] = {}
        try:
            with patch("backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock):
                for target_id in TARGET_IDS:
                    result = await runner.run_check(local_ids[target_id], trigger="manual", project_id=project)
                    positive[str(target_id)] = _result_row(
                        result, REQUEST_COUNTS[target_id], expected_assertions[target_id]
                    )
                for target_id in TARGET_IDS:
                    current = storage.get_check(local_ids[target_id], project_id=project)
                    if not current:
                        raise RuntimeError(f"本地任务不存在：{target_id}")
                    negative_payload = copy.deepcopy(current)
                    negative_payload["assertions_json"] = _negative_assertions(current.get("assertions_json"))
                    storage.update_check(local_ids[target_id], negative_payload, project_id=project)
                    result = await runner.run_check(local_ids[target_id], trigger="scheduled", project_id=project)
                    negative[str(target_id)] = _result_row(
                        result, REQUEST_COUNTS[target_id], expected_assertions[target_id]
                    )
        finally:
            await runner.shutdown(record_cancelled=False)

        return {
            "database": "temporary isolated database",
            "project_id": project,
            "module_hash": source_hash(module_source),
            "content_hash": bundle["content_hash"],
            "release_id": published.get("release_id"),
            "target_ids": list(TARGET_IDS),
            "dynamic_ids": list(DYNAMIC_IDS),
            "assertion_rule_count": sum(expected_assertions.values()),
            "positive_manual": positive,
            "negative_scheduled": negative,
            "all_positive_ok": all(item["status"] == "ok" for item in positive.values()),
            "all_negative_failed": all(item["status"] == "failed" for item in negative.values()),
            "request_counts_match_positive": all(
                item["requests"] == item["expected_requests"] for item in positive.values()
            ),
            "request_counts_match_negative": all(
                item["requests"] == item["expected_requests"] for item in negative.values()
            ),
            "assertion_counts_match_positive": all(
                item["assertions"] == item["expected_assertions"] for item in positive.values()
            ),
            "assertion_counts_match_negative": all(
                item["assertions"] == item["expected_assertions"] for item in negative.values()
            ),
            "negative_assertions_failed_as_intended": all(
                item["intended_status_assertion_failed"] for item in negative.values()
            ),
        }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update-payload", type=Path, default=Path(tempfile.gettempdir()) / "pg-usedcar-rules-update.json")
    parser.add_argument("--module-source", type=Path, default=ROOT / "scripts" / "examples" / "usedcar_module.py")
    parser.add_argument("--output", type=Path, help="write sanitized JSON evidence")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    result = asyncio.run(_run(args))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(
        result[key]
        for key in (
            "all_positive_ok",
            "all_negative_failed",
            "request_counts_match_positive",
            "request_counts_match_negative",
            "assertion_counts_match_positive",
            "assertion_counts_match_negative",
            "negative_assertions_failed_as_intended",
        )
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
