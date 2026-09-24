from __future__ import annotations

import json
from collections import Counter
from typing import Any

from . import storage


COMPARISON_STATUSES = {"failed", "timeout"}


def compare_with_previous_success(run_id: int) -> dict[str, Any]:
    current = storage.get_run(run_id)
    if current is None:
        raise ValueError("运行记录不存在")
    if current.get("status") not in COMPARISON_STATUSES:
        return {
            "available": False,
            "message": "仅失败或超时运行需要对比最近一次成功运行",
            "current_run": _run_summary(current),
            "baseline_run": None,
            "fields": [],
            "assertions": [],
        }

    baseline = storage.get_previous_successful_run(current)
    if baseline is None:
        return {
            "available": False,
            "message": "同一任务暂无可对比的成功运行记录",
            "current_run": _run_summary(current),
            "baseline_run": None,
            "fields": [],
            "assertions": [],
        }

    if current.get("details_pruned_at") or baseline.get("details_pruned_at"):
        return {
            "available": False, "message": "运行详情已按保留策略清理，无法比较请求和断言",
            "current_run": _run_summary(current), "baseline_run": _run_summary(baseline),
            "fields": [], "assertions": [],
        }
    current_snapshot = _snapshot(current.get("response_snapshot"))
    baseline_snapshot = _snapshot(baseline.get("response_snapshot"))
    current_request = _snapshot(current.get("request_snapshot"))
    baseline_request = _snapshot(baseline.get("request_snapshot"))

    fields = _field_differences(current, baseline, current_snapshot, baseline_snapshot, current_request, baseline_request)
    assertions = _assertion_differences(current_snapshot, baseline_snapshot)
    return {
        "available": True,
        "message": "",
        "current_run": _run_summary(current),
        "baseline_run": _run_summary(baseline),
        "fields": fields,
        "assertions": assertions,
    }


def _field_differences(
    current: dict[str, Any],
    baseline: dict[str, Any],
    current_snapshot: dict[str, Any],
    baseline_snapshot: dict[str, Any],
    current_request: dict[str, Any],
    baseline_request: dict[str, Any],
) -> list[dict[str, Any]]:
    check_type = str(current.get("check_type") or baseline.get("check_type") or "")
    specs = [
        ("url", "URL", _snapshot_url(current_snapshot, current_request), _snapshot_url(baseline_snapshot, baseline_request)),
        ("title", "标题", _snapshot_title(current_snapshot), _snapshot_title(baseline_snapshot)),
        ("status_code", "状态码", _snapshot_status_code(current_snapshot), _snapshot_status_code(baseline_snapshot)),
    ]
    fields: list[dict[str, Any]] = []
    for key, label, current_value, baseline_value in specs:
        if key == "title" and check_type != "ui" and current_value is None and baseline_value is None:
            continue
        if key == "status_code" and check_type == "ui" and current_value is None and baseline_value is None:
            continue
        fields.append(
            {
                "key": key,
                "label": label,
                "current": current_value,
                "baseline": baseline_value,
                "changed": current_value != baseline_value,
            }
        )
    return fields


def _assertion_differences(current_snapshot: dict[str, Any], baseline_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    current_entries = _assertion_entries(current_snapshot)
    baseline_entries = _assertion_entries(baseline_snapshot)
    max_occurrences = _max_occurrences(current_entries, baseline_entries)
    current_assertions = _assertions_by_key(current_entries, max_occurrences)
    baseline_assertions = _assertions_by_key(baseline_entries, max_occurrences)
    keys = list(dict.fromkeys([*current_assertions.keys(), *baseline_assertions.keys()]))
    differences: list[dict[str, Any]] = []
    for key in keys:
        current = current_assertions.get(key)
        baseline = baseline_assertions.get(key)
        changed = _assertion_changed(current, baseline)
        if not changed and current and current.get("status") == "ok":
            continue
        differences.append(
            {
                "key": key,
                "rule": _value(current, baseline, "rule") or key,
                "path": _value(current, baseline, "path"),
                "current_status": _value(current, None, "status"),
                "baseline_status": _value(baseline, None, "status"),
                "current_actual": _value(current, None, "actual"),
                "baseline_actual": _value(baseline, None, "actual"),
                "current_operator": _value(current, None, "operator"),
                "baseline_operator": _value(baseline, None, "operator"),
                "current_expected": _value(current, None, "expected"),
                "baseline_expected": _value(baseline, None, "expected"),
                "current_message": _value(current, None, "message"),
                "baseline_message": _value(baseline, None, "message"),
                "changed": changed,
            }
        )
    return differences[:24]


def _assertion_entries(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    assertions = snapshot.get("assertions")
    if not isinstance(assertions, list):
        return []
    counters: Counter[str] = Counter()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(assertions):
        if not isinstance(item, dict):
            continue
        base_key = _assertion_base_key(item, index)
        counters[base_key] += 1
        result.append({"base_key": base_key, "occurrence": counters[base_key], "value": item})
    return result


def _max_occurrences(*entry_sets: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for entries in entry_sets:
        for entry in entries:
            base_key = str(entry["base_key"])
            result[base_key] = max(result.get(base_key, 0), int(entry["occurrence"]))
    return result


def _assertions_by_key(entries: list[dict[str, Any]], max_occurrences: dict[str, int]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        base_key = str(entry["base_key"])
        occurrence = int(entry["occurrence"])
        key = base_key if max_occurrences.get(base_key, 0) <= 1 else f"{base_key}#{occurrence}"
        result[key] = entry["value"]
    return result


def _assertion_base_key(item: dict[str, Any], index: int) -> str:
    stable_id = item.get("id") or item.get("assertion_id")
    if stable_id:
        return f"id:{stable_id}"
    return "|".join(str(item.get(part) or "") for part in ("rule", "path")) or f"assertion-{index + 1}"


def _assertion_changed(current: dict[str, Any] | None, baseline: dict[str, Any] | None) -> bool:
    if current is None or baseline is None:
        return True
    fields = ("status", "actual", "operator", "expected", "message")
    return any(current.get(field) != baseline.get(field) for field in fields)


def _snapshot(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _snapshot_url(response_snapshot: dict[str, Any], request_snapshot: dict[str, Any]) -> str | None:
    page = response_snapshot.get("page")
    if isinstance(page, dict) and page.get("url"):
        return str(page.get("url"))
    if response_snapshot.get("url"):
        return str(response_snapshot.get("url"))
    if request_snapshot.get("url"):
        return str(request_snapshot.get("url"))
    return None


def _snapshot_title(snapshot: dict[str, Any]) -> str | None:
    page = snapshot.get("page")
    if isinstance(page, dict) and page.get("title"):
        return str(page.get("title"))
    return None


def _snapshot_status_code(snapshot: dict[str, Any]) -> int | str | None:
    value = snapshot.get("status_code")
    if value is None:
        return None
    if isinstance(value, (int, str)):
        return value
    return str(value)


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("id"),
        "check_id": run.get("check_id"),
        "check_name": run.get("check_name"),
        "check_type": run.get("check_type"),
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_ms": run.get("duration_ms"),
    }


def _value(primary: dict[str, Any] | None, fallback: dict[str, Any] | None, key: str) -> Any:
    if primary is not None and key in primary:
        return primary.get(key)
    if fallback is not None:
        return fallback.get(key)
    return None
