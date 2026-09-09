"""Verify a deployed used-car candidate with bounded live observations.

The first phase runs each candidate through its normal manual endpoint and
records request/assertion counts.  The reverse phase then sends each candidate
through ``POST /api/checks/debug``.
That endpoint creates a draft observation and the verifier rejects any result
that is not marked ``observation_kind=draft`` and ``affects_health=false``.  The
reverse phase never calls a check PUT, publish, enable, disable, or normal run
endpoint.  The scheduled phase is read-only: it polls checks and runs until
every target has a new scheduled observation after the interval lower bound.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.script_modules import bundle_content_hash, source_hash  # noqa: E402
from scripts.migrate_usedcar_modules import (  # noqa: E402
    REQUEST_COUNTS,
    TARGET_IDS,
    _api_json,
    _config_fingerprint,
    _read_json,
    build_verification_matrix,
)


class RemoteVerificationError(RuntimeError):
    """The deployed state does not satisfy the reviewed migration contract."""


HEALTH_FIELDS = (
    "current_status",
    "monitor_status",
    "consecutive_failures",
    "consecutive_successes",
    "last_run_id",
    "last_scheduled_run_id",
)
_MISSING = object()


def _plan_checks(plan: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    checks = plan.get("checks")
    if not isinstance(checks, list):
        raise RemoteVerificationError("迁移计划缺少 checks")
    result: dict[int, dict[str, Any]] = {}
    for item in checks:
        if not isinstance(item, Mapping) or not isinstance(item.get("payload"), Mapping):
            raise RemoteVerificationError("迁移计划 candidate payload 无效")
        result[int(item.get("id") or 0)] = dict(item["payload"])
    if set(result) != set(TARGET_IDS):
        raise RemoteVerificationError(f"迁移计划任务集合不是34–48：{sorted(result)}")
    return result


def _health_snapshot(check: Mapping[str, Any]) -> dict[str, Any]:
    return {field: check.get(field) for field in HEALTH_FIELDS}


def _decode_snapshot(result: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = result.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, Mapping) else None


def _run_diagnostics(result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    request_snapshot = _decode_snapshot(result, "request_snapshot")
    diagnostics = request_snapshot.get("diagnostics") if isinstance(request_snapshot, Mapping) else None
    return diagnostics if isinstance(diagnostics, Mapping) else None


def _snapshot_counts(result: Mapping[str, Any]) -> tuple[int, int, bool]:
    diagnostics = _run_diagnostics(result)
    requests = diagnostics.get("http_requests") if isinstance(diagnostics, Mapping) else None
    response_snapshot = _decode_snapshot(result, "response_snapshot")
    assertions = response_snapshot.get("assertions") if isinstance(response_snapshot, Mapping) else None
    intended_negative = any(
        isinstance(item, Mapping)
        and item.get("status") == "failed"
        and item.get("expected") == 599
        for item in (assertions if isinstance(assertions, list) else [])
    )
    return (
        len(requests) if isinstance(requests, list) else 0,
        len(assertions) if isinstance(assertions, list) else 0,
        intended_negative,
    )


def _expected_assertions(plan: Mapping[str, Any], check_id: int) -> int:
    matrix = plan.get("verification_matrix")
    if not isinstance(matrix, Mapping):
        matrix = build_verification_matrix(plan)
    checks = matrix.get("checks") if isinstance(matrix, Mapping) else None
    item = checks.get(str(check_id)) if isinstance(checks, Mapping) else None
    return int(item.get("assertion_count") or 0) if isinstance(item, Mapping) else 0


def _run_module_evidence(
    check_id: int,
    result: Any,
    plan: Mapping[str, Any],
    module_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the frozen module metadata embedded in one run snapshot."""

    matrix = plan.get("verification_matrix")
    matrix_checks = matrix.get("checks") if isinstance(matrix, Mapping) else None
    matrix_item = matrix_checks.get(str(check_id)) if isinstance(matrix_checks, Mapping) else None
    dynamic = bool(isinstance(matrix_item, Mapping) and matrix_item.get("module_refs"))
    expected = {
        "project_id": int(module_identity.get("project_id") or 0),
        "release_id": module_identity.get("release_id") if dynamic else None,
        "release_hash": module_identity.get("release_hash") if dynamic else None,
        "content_hash": module_identity.get("content_hash") if dynamic else bundle_content_hash([]),
        "module_hashes": dict(module_identity.get("module_hashes") or {}) if dynamic else {},
    }
    diagnostics = _run_diagnostics(result) if isinstance(result, Mapping) else None
    actual: Any = _MISSING
    if isinstance(diagnostics, Mapping):
        actual = diagnostics.get("modules", _MISSING)
    if diagnostics is None:
        ok = False
        error = "request_snapshot 缺少 diagnostics"
    elif dynamic:
        ok = actual == expected
        error = "动态任务的 diagnostics.modules 与已发布模块版本不一致" if not ok else ""
    else:
        ok = actual == expected
        error = "静态任务 diagnostics.modules 必须是空公共方法 bundle" if not ok else ""
    return {
        "dynamic": dynamic,
        "expected": expected,
        "actual": None if actual is _MISSING else actual,
        "ok": ok,
        "error": error,
    }


def _negative_assertions(raw: Any) -> str:
    try:
        assertions = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        assertions = []
    if not isinstance(assertions, list):
        assertions = []
    for assertion in assertions:
        if isinstance(assertion, dict) and assertion.get("type") == "status_code":
            assertion["expected_status"] = 599
            break
    else:
        assertions.append(
            {
                "id": "migration-negative-status",
                "type": "status_code",
                "enabled": True,
                "expected_status": 599,
            }
        )
    return json.dumps(assertions, ensure_ascii=False)


def _module_identity(library: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    published = library.get("published")
    if not isinstance(published, Mapping):
        raise RemoteVerificationError("线上尚无已发布公共方法版本")
    modules = published.get("modules")
    if not isinstance(modules, list):
        raise RemoteVerificationError("线上已发布版本缺少 modules")
    hashes: dict[str, str] = {}
    usedcar_source = ""
    for item in modules:
        if not isinstance(item, Mapping):
            raise RemoteVerificationError("线上模块元数据格式无效")
        name = str(item.get("name") or "").strip()
        source = str(item.get("source") or "")
        if not name or not source:
            raise RemoteVerificationError("线上模块缺少名称或源码")
        actual_hash = source_hash(source)
        supplied_hash = str(item.get("hash") or "").strip()
        if supplied_hash and supplied_hash != actual_hash:
            raise RemoteVerificationError(f"线上模块 {name} 源码哈希不一致")
        hashes[name] = actual_hash
        if name == "usedcar":
            usedcar_source = source
    expected_bundle = plan.get("module_bundle")
    expected_modules = expected_bundle.get("modules") if isinstance(expected_bundle, Mapping) else None
    expected = expected_modules[0] if isinstance(expected_modules, list) and expected_modules else None
    expected_hash = str(expected.get("hash") or "") if isinstance(expected, Mapping) else ""
    if not usedcar_source or hashes.get("usedcar") != expected_hash:
        raise RemoteVerificationError("线上 usedcar 模块源码与候选 hash 不一致")
    source = plan.get("source")
    project_id = int(source.get("project_id") or 0) if isinstance(source, Mapping) else 0
    if project_id <= 0:
        raise RemoteVerificationError("迁移计划缺少 source.project_id")
    return {
        "project_id": project_id,
        "release_id": published.get("release_id"),
        "release_hash": published.get("release_hash"),
        "content_hash": published.get("content_hash"),
        "module_hashes": hashes,
    }


def _validate_live_checks(
    checks_value: Any,
    plan: Mapping[str, Any],
    candidate_checks: Mapping[int, Mapping[str, Any]],
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    if not isinstance(checks_value, list):
        raise RemoteVerificationError("线上 checks 不是数组")
    by_id = {int(item.get("id") or 0): dict(item) for item in checks_value if isinstance(item, Mapping)}
    if not set(TARGET_IDS).issubset(by_id):
        raise RemoteVerificationError(f"线上缺少目标任务：{sorted(set(TARGET_IDS) - set(by_id))}")
    review_index = plan.get("review_index")
    if not isinstance(review_index, Mapping):
        raise RemoteVerificationError("迁移计划缺少 review_index")
    config_before: dict[int, dict[str, Any]] = {}
    for check_id in TARGET_IDS:
        current = by_id[check_id]
        candidate = candidate_checks[check_id]
        reviewed = review_index.get(str(check_id))
        if not isinstance(reviewed, Mapping):
            raise RemoteVerificationError(f"任务 {check_id} 缺少 reviewed 配置")
        if _config_fingerprint(current) != reviewed.get("config_fingerprint"):
            raise RemoteVerificationError(f"任务 {check_id} 配置指纹与迁移前不一致")
        if source_hash(current.get("script") or "") != source_hash(candidate.get("script") or ""):
            raise RemoteVerificationError(f"任务 {check_id} live 脚本不是候选脚本")
        if int(current.get("interval_seconds") or 0) != 60:
            raise RemoteVerificationError(f"任务 {check_id} 间隔不是60秒")
        if list(current.get("runner_ids") or []) != ["local"]:
            raise RemoteVerificationError(f"任务 {check_id} 执行节点不是local")
        if not bool(current.get("enabled")):
            raise RemoteVerificationError(f"任务 {check_id} 未启用，无法验证下一轮定时执行")
        config_before[check_id] = _health_snapshot(current)
    return by_id, config_before


def _draft_row(
    check_id: int,
    result: Any,
    *,
    expected_requests: int,
    expected_assertions: int,
    module_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {
            "original_check_id": check_id,
            "status": "invalid",
            "requests": 0,
            "expected_requests": expected_requests,
            "assertions": 0,
            "expected_assertions": expected_assertions,
            "module_metadata_ok": False,
            "draft_safe": False,
        }
    observation_kind = str(result.get("observation_kind") or "")
    affects_health = result.get("affects_health")
    # The run is persisted with check_id=0 for a draft; retain the originating
    # task association from the request in the evidence matrix.
    requests, assertions, intended_negative = _snapshot_counts(result)
    counts_match = requests == expected_requests and assertions == expected_assertions
    module_metadata_ok = bool(module_evidence.get("ok"))
    safe = (
        str(result.get("status") or "") == "failed"
        and observation_kind == "draft"
        and affects_health is False
        and str(result.get("notification_status") or "") == "not_required"
        and intended_negative
        and counts_match
        and module_metadata_ok
    )
    return {
        "original_check_id": check_id,
        "run_id": result.get("id"),
        "returned_check_id": result.get("check_id"),
        "status": str(result.get("status") or ""),
        "observation_kind": observation_kind,
        "affects_health": affects_health,
        "notification_status": result.get("notification_status"),
        "intended_negative": intended_negative,
        "requests": requests,
        "expected_requests": expected_requests,
        "assertions": assertions,
        "expected_assertions": expected_assertions,
        "counts_match": counts_match,
        "module_metadata_ok": module_metadata_ok,
        "module_metadata": module_evidence.get("actual"),
        "expected_module_metadata": module_evidence.get("expected"),
        "module_metadata_error": module_evidence.get("error"),
        "draft_safe": safe,
        "error": str(result.get("error_message") or "")[:240],
    }


def _positive_row(
    check_id: int,
    result: Any,
    *,
    expected_requests: int,
    expected_assertions: int,
    module_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep the positive manual result tied to its originating task."""

    if not isinstance(result, Mapping):
        return {
            "original_check_id": check_id,
            "status": "invalid",
            "requests": 0,
            "expected_requests": expected_requests,
            "assertions": 0,
            "expected_assertions": expected_assertions,
            "module_metadata_ok": False,
            "positive_ok": False,
            "error": "响应不是对象",
        }
    requests, assertions, intended_negative = _snapshot_counts(result)
    status = str(result.get("status") or "")
    positive_ok = (
        status == "ok"
        and requests == expected_requests
        and assertions == expected_assertions
        and not intended_negative
        and bool(module_evidence.get("ok"))
    )
    return {
        "original_check_id": check_id,
        "run_id": result.get("id"),
        "returned_check_id": result.get("check_id"),
        "status": status,
        "requests": requests,
        "expected_requests": expected_requests,
        "assertions": assertions,
        "expected_assertions": expected_assertions,
        "module_metadata_ok": bool(module_evidence.get("ok")),
        "module_metadata": module_evidence.get("actual"),
        "expected_module_metadata": module_evidence.get("expected"),
        "module_metadata_error": module_evidence.get("error"),
        "positive_ok": positive_ok,
        "error": str(result.get("error_message") or "")[:240],
    }


def run_positive_manual(
    base_url: str,
    project_id: int,
    plan: Mapping[str, Any],
    module_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Run every reviewed task once through its normal manual endpoint."""

    _plan_checks(plan)
    results: dict[str, dict[str, Any]] = {}
    for check_id in TARGET_IDS:
        try:
            result = _api_json(
                base_url,
                f"/api/checks/{check_id}/run",
                project_id,
                method="POST",
            )
            results[str(check_id)] = _positive_row(
                check_id,
                result,
                expected_requests=REQUEST_COUNTS[check_id],
                expected_assertions=_expected_assertions(plan, check_id),
                module_evidence=_run_module_evidence(check_id, result, plan, module_identity),
            )
        except Exception as exc:
            results[str(check_id)] = {
                "original_check_id": check_id,
                "status": "transport_error",
                "requests": 0,
                "expected_requests": REQUEST_COUNTS[check_id],
                "assertions": 0,
                "expected_assertions": _expected_assertions(plan, check_id),
                "module_metadata_ok": False,
                "positive_ok": False,
                "error": str(exc)[:240],
            }
    all_positive_ok = bool(results) and all(item.get("positive_ok") is True for item in results.values())
    return {
        "endpoint": "POST /api/checks/{id}/run",
        "scenario": "15 reviewed candidates through normal manual execution",
        "count": len(results),
        "all_positive_ok": all_positive_ok,
        "request_counts_match": all(
            item.get("requests") == item.get("expected_requests") for item in results.values()
        ),
        "assertion_counts_match": all(
            item.get("assertions") == item.get("expected_assertions") for item in results.values()
        ),
        "checks": results,
    }


def run_reverse_draft_debug(
    base_url: str,
    project_id: int,
    plan: Mapping[str, Any],
    module_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Run all 15 negative probes as draft debug and prove no live mutation."""

    candidate_checks = _plan_checks(plan)
    before_at = datetime.now(timezone.utc)
    # Take the timestamp before the health snapshot.  A scheduler run that
    # starts in this window must be treated as concurrent evidence even if it
    # completes before the draft loop finishes.
    before_value = _api_json(base_url, "/api/checks?type=api", project_id)
    _, before_health = _validate_live_checks(before_value, plan, candidate_checks)
    baseline_runs_value = _api_json(base_url, "/api/runs?type=api&limit=500", project_id)
    baseline_run_ids: dict[int, set[int]] = {check_id: set() for check_id in TARGET_IDS}
    for raw in baseline_runs_value if isinstance(baseline_runs_value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        check_id = int(raw.get("check_id") or 0)
        started_at = _timestamp(raw.get("started_at"))
        if check_id in baseline_run_ids and (started_at is None or started_at < before_at):
            baseline_run_ids[check_id].add(int(raw.get("id") or 0))
    results: dict[str, dict[str, Any]] = {}
    for check_id in TARGET_IDS:
        negative_payload = dict(candidate_checks[check_id])
        negative_payload["assertions_json"] = _negative_assertions(negative_payload.get("assertions_json"))
        try:
            result = _api_json(
                base_url,
                "/api/checks/debug",
                project_id,
                method="POST",
                payload=negative_payload,
            )
            results[str(check_id)] = _draft_row(
                check_id,
                result,
                expected_requests=REQUEST_COUNTS[check_id],
                expected_assertions=_expected_assertions(plan, check_id),
                module_evidence=_run_module_evidence(check_id, result, plan, module_identity),
            )
        except Exception as exc:
            results[str(check_id)] = {
                "original_check_id": check_id,
                "status": "transport_error",
                "draft_safe": False,
                "intended_negative": False,
                "counts_match": False,
                "module_metadata_ok": False,
                "error": str(exc)[:240],
            }
    after_value = _api_json(base_url, "/api/checks?type=api", project_id)
    _after_checks, after_health = _validate_live_checks(after_value, plan, candidate_checks)
    after_runs_value = _api_json(base_url, "/api/runs?type=api&limit=500", project_id)
    concurrent_scheduled = _scheduled_evidence(
        after_runs_value,
        baseline_run_ids,
        before_at,
        plan=plan,
        module_identity=module_identity,
    )
    changed = [check_id for check_id in TARGET_IDS if after_health[check_id] != before_health[check_id]]
    unexpected_changes = [check_id for check_id in changed if check_id not in concurrent_scheduled]
    if unexpected_changes:
        raise RemoteVerificationError(f"draft debug 改变了 live health：{unexpected_changes}")
    if not all(item["draft_safe"] for item in results.values()):
        raise RemoteVerificationError("存在不是 failed/draft/expected=599/affects_health=false 的反向验证结果")
    return {
        "endpoint": "POST /api/checks/debug",
        "scenario": "15 negative candidates with status assertion expected=599",
        "count": len(results),
        "all_draft_safe": True,
        "live_health_unchanged": not changed,
        "live_health_unchanged_by_draft": not unexpected_changes,
        "live_health_changed_by_concurrent_scheduled": bool(changed),
        "concurrent_scheduled_ids": sorted(concurrent_scheduled),
        "health_changes": {
            str(check_id): {
                "before": before_health[check_id],
                "after": after_health[check_id],
                "scheduled_run": concurrent_scheduled.get(check_id),
            }
            for check_id in changed
        },
        "live_config_rechecked": True,
        "checks": results,
        "write_endpoints_called": [],
    }


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _scheduled_evidence(
    runs_value: Any,
    baseline_run_ids: Mapping[int, set[int]],
    lower_bound_at: datetime,
    *,
    plan: Mapping[str, Any] | None = None,
    module_identity: Mapping[str, Any] | None = None,
) -> dict[int, dict[str, Any]]:
    runs = runs_value if isinstance(runs_value, list) else []
    result: dict[int, dict[str, Any]] = {}
    for raw in runs:
        if not isinstance(raw, Mapping):
            continue
        check_id = int(raw.get("check_id") or 0)
        run_id = int(raw.get("id") or 0)
        started_at = _timestamp(raw.get("started_at"))
        if (
            check_id not in TARGET_IDS
            or run_id in baseline_run_ids.get(check_id, set())
            or str(raw.get("trigger") or "") != "scheduled"
            or str(raw.get("observation_kind") or "") != "observation"
            or raw.get("affects_health") is not True
            or str(raw.get("status") or "") != "ok"
            or started_at is None
            or started_at < lower_bound_at
            or _timestamp(raw.get("finished_at")) is None
        ):
            continue
        if str(raw.get("runner_id") or "") != "local":
            continue
        request_count, assertion_count, intended_negative = _snapshot_counts(raw)
        expected_requests = REQUEST_COUNTS[check_id]
        expected_assertions = _expected_assertions(plan, check_id) if plan is not None else None
        if request_count != expected_requests or (
            expected_assertions is not None and assertion_count != expected_assertions
        ) or intended_negative:
            continue
        module_evidence = (
            _run_module_evidence(check_id, raw, plan, module_identity)
            if plan is not None and module_identity is not None
            else {"ok": True, "actual": None, "expected": None, "error": ""}
        )
        if not module_evidence.get("ok"):
            continue
        current = result.get(check_id)
        if current is None or run_id > int(current.get("run_id") or 0):
            result[check_id] = {
                "run_id": run_id,
                "status": str(raw.get("status") or ""),
                "trigger": "scheduled",
                "observation_kind": "observation",
                "affects_health": True,
                "runner_id": "local",
                "started_at": raw.get("started_at"),
                "finished_at": raw.get("finished_at"),
                "requests": request_count,
                "expected_requests": expected_requests,
                "assertions": assertion_count,
                "expected_assertions": expected_assertions,
                "counts_match": True,
                "module_metadata_ok": True,
                "module_metadata": module_evidence.get("actual"),
                "expected_module_metadata": module_evidence.get("expected"),
            }
    return result


def poll_next_scheduled_runs(
    base_url: str,
    project_id: int,
    checks: Mapping[int, Mapping[str, Any]],
    plan: Mapping[str, Any],
    module_identity: Mapping[str, Any],
    *,
    baseline_runs: Any,
    baseline_at: datetime,
    poll_interval_seconds: float = 5.0,
    timeout_after_lower_bound_seconds: float = 120.0,
    sleep=time.sleep,
    monotonic=time.monotonic,
    now=lambda: datetime.now().astimezone(),
) -> dict[str, Any]:
    """Wait out the scheduler interval, then collect all 15 run evidences."""

    intervals = {check_id: max(5, int(checks[check_id].get("interval_seconds") or 0)) for check_id in TARGET_IDS}
    if any(value != 60 for value in intervals.values()):
        raise RemoteVerificationError(f"定时验证要求全部60秒，实际={intervals}")
    lower_bound_seconds = max(intervals.values())
    lower_bound_at = baseline_at + timedelta(seconds=lower_bound_seconds)
    baseline_run_ids: dict[int, set[int]] = {check_id: set() for check_id in TARGET_IDS}
    for raw in baseline_runs if isinstance(baseline_runs, list) else []:
        if isinstance(raw, Mapping) and int(raw.get("check_id") or 0) in baseline_run_ids:
            baseline_run_ids[int(raw["check_id"])].add(int(raw.get("id") or 0))
    started = monotonic()
    while now() < lower_bound_at:
        remaining = (lower_bound_at - now()).total_seconds()
        sleep(max(0.01, min(float(poll_interval_seconds), remaining)))
    deadline = monotonic() + max(0.0, float(timeout_after_lower_bound_seconds))
    latest: dict[int, dict[str, Any]] = {}
    polls = 0
    while True:
        runs = _api_json(base_url, "/api/runs?type=api&limit=500", project_id)
        latest = _scheduled_evidence(
            runs,
            baseline_run_ids,
            lower_bound_at,
            plan=plan,
            module_identity=module_identity,
        )
        polls += 1
        if set(latest) == set(TARGET_IDS):
            break
        if monotonic() >= deadline:
            missing = sorted(set(TARGET_IDS) - set(latest))
            raise RemoteVerificationError(f"下一轮定时验证超时，缺少任务：{missing}")
        sleep(max(0.01, min(float(poll_interval_seconds), max(0.01, deadline - monotonic()))))
    return {
        "endpoint": "GET /api/checks?type=api + GET /api/runs?type=api&limit=500",
        "baseline_at": baseline_at.isoformat(timespec="seconds"),
        "lower_bound_seconds": lower_bound_seconds,
        "lower_bound_at": lower_bound_at.isoformat(timespec="seconds"),
        "poll_interval_seconds": poll_interval_seconds,
        "timeout_after_lower_bound_seconds": timeout_after_lower_bound_seconds,
        "polls_after_lower_bound": polls,
        "all_15_observed": True,
        "checks": {str(check_id): latest[check_id] for check_id in TARGET_IDS},
        "rule_evidence": {
            "required_trigger": "scheduled",
            "required_observation_kind": "observation",
            "required_affects_health": True,
            "required_runner_id": "local",
        },
        "elapsed_seconds": max(0.0, monotonic() - started),
    }


def _matrix_output(plan: Mapping[str, Any]) -> dict[str, Any]:
    matrix = plan.get("verification_matrix")
    if not isinstance(matrix, Mapping):
        matrix = build_verification_matrix(plan)
    checks = {
        str(check_id): {
            "module_refs": item.get("module_refs") or [],
            "module_hashes": item.get("module_hashes") or {},
            "module_metadata": item.get("module_metadata"),
            "assertion_count": item.get("assertion_count"),
            "config_fingerprint_present": bool(item.get("config_fingerprint")),
            "script_hash": item.get("script_hash"),
        }
        for check_id, item in (matrix.get("checks") or {}).items()
        if isinstance(item, Mapping)
    }
    return {
        "target_ids": matrix["target_ids"],
        "dynamic_ids": matrix["dynamic_ids"],
        "static_ids": matrix["static_ids"],
        "assertion_rule_count": matrix["assertion_rule_count"],
        "checks": checks,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--project-id", required=True, type=int)
    parser.add_argument("--plan", type=Path, default=Path("pg-usedcar-rules-migration-candidate.json"))
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--timeout-after-lower-bound-seconds", type=float, default=120.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    try:
        plan = _read_json(args.plan)
    except Exception as exc:
        raise RemoteVerificationError(f"迁移计划不可读：{args.plan}") from exc
    if not isinstance(plan, Mapping):
        raise RemoteVerificationError("迁移计划不是对象")
    candidate_checks = _plan_checks(plan)
    library = _api_json(args.base_url, f"/api/projects/{args.project_id}/library", args.project_id)
    module = _module_identity(library, plan)
    checks_value = _api_json(args.base_url, "/api/checks?type=api", args.project_id)
    live_checks, _ = _validate_live_checks(checks_value, plan, candidate_checks)
    positive = run_positive_manual(args.base_url, args.project_id, plan, module)
    if not positive["all_positive_ok"]:
        raise RemoteVerificationError("15 项正向 manual 未全部通过，已停止后续反向/定时验收")
    # Manual runs are health observations by design.  Refresh the live snapshot
    # after that stage so the reverse draft comparison starts from the correct
    # health baseline and only attributes later changes to a concurrent scheduler.
    checks_after_positive = _api_json(args.base_url, "/api/checks?type=api", args.project_id)
    live_checks, _ = _validate_live_checks(checks_after_positive, plan, candidate_checks)
    reverse = run_reverse_draft_debug(args.base_url, args.project_id, plan, module)
    baseline_at = datetime.now().astimezone()
    baseline_runs = _api_json(args.base_url, "/api/runs?type=api&limit=500", args.project_id)
    scheduled = poll_next_scheduled_runs(
        args.base_url,
        args.project_id,
        live_checks,
        plan,
        module,
        baseline_runs=baseline_runs,
        baseline_at=baseline_at,
        poll_interval_seconds=args.poll_interval_seconds,
        timeout_after_lower_bound_seconds=args.timeout_after_lower_bound_seconds,
    )
    result = {
        "mode": "post-deploy-remote-verification",
        "project_id": args.project_id,
        "module": module,
        "matrix": _matrix_output(plan),
        "configuration_preserved": True,
        "positive_manual": positive,
        "reverse_draft_debug": reverse,
        "scheduled_poll": scheduled,
        "request_count_baseline": {str(check_id): REQUEST_COUNTS[check_id] for check_id in TARGET_IDS},
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RemoteVerificationError as exc:
        print(f"remote verification aborted: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
