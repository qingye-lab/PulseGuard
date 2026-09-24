from __future__ import annotations

import json
from typing import Any

from .variables import mask_text


FAILURE_STATUSES = {"failed", "timeout", "skipped", "preparation_failed"}
ASSERTION_FAILURE_STATUSES = {"failed", "timeout"}
FAILURE_KIND_LABELS = {
    "target": "目标页面/API",
    "runner": "执行环境",
    "preparation": "前置准备",
    "none": "未记录",
}


def build_failure_summary(
    run: dict[str, Any],
    settings: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = str(run.get("status") or "")
    failure_kind = str(run.get("failure_kind") or "none")
    error_message = _brief(mask_text(str(run.get("error_message") or ""), settings), 1000)
    snapshot = _snapshot(run.get("response_snapshot"))
    assertions = _assertions(snapshot)
    failed_assertions = [item for item in assertions if str(item.get("status") or "") in ASSERTION_FAILURE_STATUSES]
    baseline_snapshot = _snapshot(baseline.get("response_snapshot")) if baseline else {}
    baseline_assertions = _assertions_by_key(_assertions(baseline_snapshot))
    regressed_count = sum(
        1
        for item in failed_assertions
        if str((baseline_assertions.get(_assertion_key(item)) or {}).get("status") or "") == "ok"
    )

    summary = _summary_text(run, status, failure_kind, error_message, assertions, failed_assertions, regressed_count)
    signals = _signals(failure_kind, baseline)
    next_steps = _next_steps(run, status, failure_kind, assertions, failed_assertions, baseline, regressed_count)
    return {
        "run_id": run.get("id"),
        "check_id": run.get("check_id"),
        "check_name": run.get("check_name"),
        "status": status,
        "failure_kind": failure_kind,
        "details_pruned_at": run.get("details_pruned_at"),
        "evidence_available": not bool(run.get("details_pruned_at")),
        "summary": summary,
        "signals": signals,
        "next_steps": next_steps,
    }


def _summary_text(
    run: dict[str, Any],
    status: str,
    failure_kind: str,
    error_message: str,
    assertions: list[dict[str, Any]],
    failed_assertions: list[dict[str, Any]],
    regressed_count: int,
) -> str:
    if status not in FAILURE_STATUSES:
        return "本次运行未记录失败。"
    if status == "preparation_failed":
        return "前置准备失败，目标检查未执行，不用于判定目标故障或恢复。"
    if failure_kind == "runner":
        return "Runner 执行过程失败，目标检查未完成。"
    if status == "timeout":
        return "运行超过超时限制，未完成目标检查。"
    if status == "skipped":
        return "本次运行未执行完成，已被 Runner 跳过或取消。"
    if assertions and failed_assertions:
        check_type = str(run.get("check_type") or "").upper()
        check_type_label = f" {check_type}" if check_type in {"UI", "API"} else ""
        text = f"{len(failed_assertions)}/{len(assertions)} 项{check_type_label} 校验失败"
        if regressed_count:
            text += f"，其中 {regressed_count} 项为相对最近成功运行的新增失败"
        return f"{text}。"
    if error_message:
        return f"目标检查失败：{_brief(error_message, 240)}"
    return "目标检查失败，但本次运行未记录可用的失败详情。"


def _signals(
    failure_kind: str,
    baseline: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = [
        {"label": "失败来源", "value": FAILURE_KIND_LABELS.get(failure_kind, "未记录")},
    ]
    if baseline:
        signals.append({"label": "对比基线", "value": f"成功运行 #{baseline.get('id')}"})
    return signals


def _next_steps(
    run: dict[str, Any],
    status: str,
    failure_kind: str,
    assertions: list[dict[str, Any]],
    failed_assertions: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    regressed_count: int,
) -> list[str]:
    if status not in FAILURE_STATUSES:
        return ["如需复盘，可查看运行日志、请求/响应快照和最近成功对比。"]
    if status == "preparation_failed":
        return ["查看前置脚本错误和变量解析结果，修复后重新运行。", "本次没有有效目标观测，不据此确认故障或恢复。"]
    if failure_kind == "runner":
        return [
            "检查 Runner 是否在线、执行队列是否已满，以及浏览器或运行依赖是否可用。",
            "查看运行日志中的取消、清理、浏览器启动或内部执行错误。",
        ]
    if status == "timeout":
        return [
            "确认目标服务响应时间是否超过任务超时限制。",
            "对比最近成功运行的耗时，并检查页面加载或接口依赖是否变慢。",
        ]
    if assertions and failed_assertions:
        steps: list[str] = []
        if baseline and _snapshot_url(_snapshot(run.get("response_snapshot"))) != _snapshot_url(_snapshot(baseline.get("response_snapshot"))):
            steps.append(f"当前访问地址与成功运行 #{baseline.get('id')} 不同，确认地址变更是否符合预期。")
        if regressed_count:
            steps.append(f"在“对比”中查看 {regressed_count} 项新增失败的变化。")
        if len(steps) < 3:
            steps.append("在“校验”中逐项确认页面内容或校验规则是否需要更新。")
        if len(steps) < 3:
            steps.append("若页面内容正常，检查失败项是否依赖特定数据、登录状态或前置操作。")
        return steps[:3]
    return [
        "查看本次错误详情和运行日志，确认目标服务、页面或接口是否可达。",
        "如最近成功基线可用，优先对比 URL、状态码、响应内容和运行耗时。",
    ]


def _assertions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    value = snapshot.get("assertions")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _assertions_by_key(assertions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_assertion_key(item): item for item in assertions}


def _assertion_key(assertion: dict[str, Any]) -> str:
    stable_id = assertion.get("id") or assertion.get("assertion_id")
    if stable_id:
        return f"id:{stable_id}"
    return "|".join(str(assertion.get(part) or "") for part in ("rule", "path"))


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


def _snapshot_url(snapshot: dict[str, Any]) -> str | None:
    page = snapshot.get("page")
    if isinstance(page, dict) and page.get("url"):
        return str(page.get("url"))
    if snapshot.get("url"):
        return str(snapshot.get("url"))
    return None


def _brief(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"
