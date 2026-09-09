from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

import httpx

from .artifacts import ArtifactStore
from .context import RunContext, RunFailure, compile_check_function
from .resource_pool import ProbeResourcePool
from .variables import VARIABLE_PLACEHOLDER_PATTERN, mask_data, mask_text, resolve_data, variable_lookup


ASSERTION_TYPES = {
    "status_code",
    "response_time",
    "json_path_exists",
    "json_path_equals",
    "json_path_not_empty",
    "json_path_contains",
    "json_path_type",
    "json_path_length",
}
JSON_ASSERTION_TYPES = {
    "json_path_exists",
    "json_path_equals",
    "json_path_not_empty",
    "json_path_contains",
    "json_path_type",
    "json_path_length",
}
JSON_VALUE_TYPES = {"string", "number", "boolean", "object", "array", "null"}
LENGTH_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}


class ApiInspectionFailure(RunFailure):
    """Expected API preview failure with attempt-owned diagnostics."""

    def __init__(self, message: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def api_inspection_diagnostics(ctx: Any, *, error: Exception | None = None) -> dict[str, Any]:
    """Return bounded, masked API execution evidence for preview and run details."""
    settings = getattr(ctx, "settings", {}) or {}
    check = getattr(ctx, "check", {}) or {}
    available = variable_lookup(settings)
    template = str(check.get("entry_url") or "")
    timings: dict[str, Any] = {}
    request_ms = getattr(ctx, "last_request_duration_ms", None)
    task_ms = getattr(ctx, "task_duration_ms", None)
    if request_ms is not None:
        timings["request_ms"] = int(request_ms)
    if task_ms is not None:
        timings["task_ms"] = int(task_ms)
    result = {
        "stage": getattr(ctx, "api_stage", "prepare"),
        "status": "failed" if error else "ok",
        "error_type": type(error).__name__ if error else None,
        "message": str(error) if error else None,
        "url_variables": {
            name: "available" if name in available else "missing"
            for name in sorted(set(VARIABLE_PLACEHOLDER_PATTERN.findall(template)))
        },
        "variables": ctx.vars.snapshot() if getattr(ctx, "vars", None) is not None else {},
        "request": getattr(ctx, "request_snapshot", None),
        "response": getattr(ctx, "response_snapshot", None),
        "http_requests": _history_snapshot(ctx),
        "timings": timings,
        "modules": getattr(ctx, "library_metadata", None),
        "compile": list(getattr(ctx, "compile_diagnostics", []) or []),
        "logs": "\n".join(getattr(ctx, "logs", [])[-50:]),
    }
    return _bounded(mask_data(result, settings))


def _history_snapshot(ctx: Any) -> list[dict[str, Any]]:
    history = getattr(ctx, "http_exchanges", [])
    if not isinstance(history, list):
        return []
    try:
        return copy.deepcopy(history[-10:])
    except Exception:
        return []


def normalize_api_assertions(raw: str | None) -> list[dict[str, Any]]:
    if not raw or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("校验项必须是合法 JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("校验项必须是 JSON Array")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个校验项格式不正确")
        assertion_type = str(item.get("type") or "").strip()
        if assertion_type not in ASSERTION_TYPES:
            raise ValueError(f"第 {index} 个校验项类型无效")

        assertion: dict[str, Any] = {
            "id": str(item.get("id") or f"assertion-{index}"),
            "type": assertion_type,
            "enabled": bool(item.get("enabled", True)),
        }
        if assertion_type == "status_code":
            assertion["expected_status"] = _bounded_int(item.get("expected_status", 200), 100, 599, "期望状态码")
        elif assertion_type == "response_time":
            assertion["max_ms"] = _bounded_int(item.get("max_ms", 1000), 1, 300000, "响应耗时")
        elif assertion_type in JSON_ASSERTION_TYPES:
            assertion["path"] = _json_path(item.get("path"))
            if assertion_type in {"json_path_equals", "json_path_contains"}:
                assertion["expected_value"] = str(item.get("expected_value", ""))
            elif assertion_type == "json_path_type":
                assertion["expected_type"] = _enum(item.get("expected_type", "string"), JSON_VALUE_TYPES, "字段类型")
            elif assertion_type == "json_path_length":
                assertion["operator"] = _enum(item.get("operator", "gte"), LENGTH_OPERATORS, "长度操作符")
                assertion["expected_length"] = _bounded_int(item.get("expected_length", 1), 0, 1000000, "期望长度")
        normalized.append(assertion)
    return normalized


def has_enabled_api_assertions(raw: str | None) -> bool:
    return any(assertion.get("enabled", True) for assertion in normalize_api_assertions(raw))


async def run_structured_api_check(
    ctx: RunContext,
    response: httpx.Response | None = None,
    request_ms: int | None = None,
) -> None:
    assertions = [
        resolve_data(item, ctx.settings)
        for item in normalize_api_assertions(ctx.check.get("assertions_json"))
        if item.get("enabled", True)
    ]
    if not assertions:
        raise RunFailure("请至少添加一个启用的接口校验项")

    if response is None:
        started = time.perf_counter()
        response = await ctx.request()
        elapsed_ms = max(0, int((time.perf_counter() - started) * 1000))
    else:
        elapsed_ms = request_ms
        if elapsed_ms is None:
            elapsed_ms = getattr(ctx, "last_request_duration_ms", None)
        elapsed_ms = max(0, int(elapsed_ms or 0))
    ctx.log(f"HTTP {response.status_code} · {elapsed_ms} ms")
    if ctx.response_snapshot is None or ctx.settings.get("success_response_artifacts_enabled"):
        ctx.save_response(response)
    if ctx.response_snapshot is not None:
        ctx.response_snapshot["duration_ms"] = elapsed_ms
        ctx.response_snapshot["timings"] = {"request_ms": elapsed_ms}

    body_json: Any = None
    body_is_json = False
    try:
        body_json = response.json()
        body_is_json = True
    except ValueError:
        body_is_json = False

    errors: list[str] = []
    assertion_results: list[dict[str, Any]] = []
    for assertion in assertions:
        label = assertion_label(assertion)
        detail = _evaluate_assertion_detail(assertion, response, elapsed_ms, body_json, body_is_json)
        error = None if detail["passed"] else str(detail["message"])
        assertion_results.append(_compact_result({"rule": label, "path": assertion.get("path"), **detail}))
        if error:
            errors.append(error)
            ctx.log(f"校验失败：{error}")
        else:
            ctx.log(f"校验通过：{label}")

    if ctx.response_snapshot is not None:
        ctx.response_snapshot["assertions"] = assertion_results

    if errors:
        raise RunFailure("；".join(errors))


async def inspect_api_response(
    config: dict[str, Any],
    settings: dict[str, Any] | None = None,
    resources: ProbeResourcePool | None = None,
    script_loader: Any | None = None,
) -> dict[str, Any]:
    settings = settings or {}
    entry_url = str(config.get("entry_url") or "").strip()
    if not entry_url:
        raise ValueError("目标 URL 不能为空")

    timeout_ms = int(config.get("timeout_ms") or 10000)
    check = {
        "id": 0,
        "project_id": config.get("project_id"),
        "name": "接口预览",
        "type": "api",
        "entry_url": entry_url,
        "method": str(config.get("method") or "GET").upper(),
        "headers_json": config.get("headers_json") or "{}",
        "body": config.get("body") or "",
        "timeout_ms": timeout_ms,
        "assertions_json": "[]",
        "script": config.get("script") or "",
        "_module_bundle": config.get("_module_bundle") or config.get("module_bundle"),
    }
    ctx = RunContext(check, 0, settings, ArtifactStore(), resources=resources)
    runtime_settings = ctx.settings
    script = str(config.get("script") or "")
    max_runtime_seconds = max(0.001, float(settings.get("max_task_runtime_seconds", 60) or 60))
    timeout_seconds = min(timeout_ms / 1000, max_runtime_seconds)
    started = time.perf_counter()
    response: httpx.Response | None = None
    failure: Exception | None = None
    try:
        async def execute() -> httpx.Response:
            nonlocal response
            if script.strip():
                ctx.api_stage = "script"
                loader = script_loader or compile_check_function
                check_func = loader(script, ctx)
                await check_func(ctx)
                response = ctx.http.last_response
                if response is None:
                    try:
                        _ = ctx.entry_url
                    except Exception as exc:
                        raise RunFailure(str(exc)) from exc
                    raise RunFailure("脚本未产生 HTTP 响应，无法预览接口结果")
                return response
            ctx.api_stage = "request"
            response = await ctx.request()
            return response

        response = await asyncio.wait_for(execute(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        try:
            await ctx.close(False)
        except Exception as cleanup_error:
            ctx.log(f"运行清理失败：{cleanup_error}")
        raise
    except asyncio.TimeoutError:
        failure = asyncio.TimeoutError(f"执行超过超时限制：{timeout_ms}ms")
    except Exception as exc:
        failure = exc
    task_ms = max(0, int((time.perf_counter() - started) * 1000))
    ctx.set_task_duration(task_ms)
    if failure is not None:
        diagnostics = api_inspection_diagnostics(ctx, error=failure)
        try:
            await ctx.close(False)
        except Exception as cleanup_error:
            ctx.log(f"运行清理失败：{cleanup_error}")
        safe_message = mask_text(str(failure) or type(failure).__name__, runtime_settings)
        raise ApiInspectionFailure(safe_message, diagnostics) from None
    assert response is not None
    request_ms = int(ctx.last_request_duration_ms or 0)
    body_text = mask_text(response.text, runtime_settings)
    try:
        body_text = json.dumps(mask_data(response.json(), runtime_settings), ensure_ascii=False)
    except ValueError:
        pass
    body_text = mask_text(body_text, runtime_settings)
    body_preview = body_text if len(body_text) <= 50000 else body_text[:50000] + "\n... 响应体已截断"

    json_value: Any = None
    json_valid = False
    try:
        json_value = response.json()
        json_valid = True
    except ValueError:
        json_valid = False

    result = {
        "status_code": response.status_code,
        "duration_ms": request_ms,
        "headers": mask_data(dict(response.headers), runtime_settings),
        "body": mask_text(body_preview, runtime_settings),
        "json_valid": json_valid,
        "json_paths": mask_data(extract_json_paths(json_value), runtime_settings) if json_valid else [],
        "timings": {"request_ms": request_ms, "task_ms": task_ms},
        "diagnostics": api_inspection_diagnostics(ctx),
    }
    try:
        await ctx.close(False)
    except Exception as cleanup_error:
        ctx.log(f"运行清理失败：{cleanup_error}")
    return result


def extract_json_paths(value: Any, max_paths: int = 240) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []

    def visit(current: Any, path: str, depth: int) -> None:
        if len(paths) >= max_paths or depth > 8:
            return
        if path != "$":
            paths.append({"path": path, "type": _value_type(current), "preview": _preview(current), "length": _length(current)})
        if isinstance(current, dict):
            for key, child in current.items():
                child_path = f"{path}.{key}" if _simple_key(str(key)) else f"{path}[{json.dumps(str(key), ensure_ascii=False)}]"
                visit(child, child_path, depth + 1)
                if len(paths) >= max_paths:
                    return
        elif isinstance(current, list):
            for index, child in enumerate(current[:20]):
                visit(child, f"{path}[{index}]", depth + 1)
                if len(paths) >= max_paths:
                    return

    visit(value, "$", 0)
    return paths


def assertion_label(assertion: dict[str, Any]) -> str:
    assertion_type = assertion["type"]
    if assertion_type == "status_code":
        return f"状态码 = {assertion['expected_status']}"
    if assertion_type == "response_time":
        return f"响应耗时 <= {assertion['max_ms']}ms"
    if assertion_type == "json_path_exists":
        return f"字段存在 {assertion['path']}"
    if assertion_type == "json_path_equals":
        return f"字段等于 {assertion['path']}"
    if assertion_type == "json_path_not_empty":
        return f"字段非空 {assertion['path']}"
    if assertion_type == "json_path_contains":
        return f"字段包含 {assertion['path']}"
    if assertion_type == "json_path_type":
        return f"字段类型 {assertion['path']} = {assertion['expected_type']}"
    if assertion_type == "json_path_length":
        return f"字段长度 {assertion['path']} {_operator_label(assertion['operator'])} {assertion['expected_length']}"
    return assertion_type


def _evaluate_assertion(
    assertion: dict[str, Any],
    response: httpx.Response,
    elapsed_ms: int,
    body_json: Any,
    body_is_json: bool,
) -> str | None:
    detail = _evaluate_assertion_detail(assertion, response, elapsed_ms, body_json, body_is_json)
    return None if detail["passed"] else str(detail["message"])


def _evaluate_assertion_detail(
    assertion: dict[str, Any],
    response: httpx.Response,
    elapsed_ms: int,
    body_json: Any,
    body_is_json: bool,
) -> dict[str, Any]:
    assertion_type = assertion["type"]
    if assertion_type == "status_code":
        expected = int(assertion["expected_status"])
        passed = response.status_code == expected
        return _detail(passed, response.status_code, expected, "=", "实际状态码等于预期值" if passed else f"状态码不匹配：期望 {expected}，实际 {response.status_code}")
    if assertion_type == "response_time":
        maximum = int(assertion["max_ms"])
        passed = elapsed_ms <= maximum
        return _detail(passed, f"{elapsed_ms}ms", f"{maximum}ms", "<=", "实际耗时小于等于预期值" if passed else f"响应耗时超过限制：期望 <= {maximum}ms，实际 {elapsed_ms}ms")
    if assertion_type in JSON_ASSERTION_TYPES and not body_is_json:
        return _detail(False, None, "合法 JSON", "", "响应体不是合法 JSON，无法执行字段校验")

    if assertion_type == "json_path_exists":
        exists, actual = find_json_path(body_json, assertion["path"])
        return _detail(exists, _field_state(exists, actual), "存在", "", "字段存在" if exists else f"JSON 字段不存在：{assertion['path']}")
    if assertion_type == "json_path_equals":
        exists, actual = find_json_path(body_json, assertion["path"])
        if not exists:
            return _detail(False, "不存在", _expected_value(assertion.get("expected_value", "")), "=", f"JSON 字段不存在：{assertion['path']}")
        expected = _expected_value(assertion.get("expected_value", ""))
        passed = actual == expected
        return _detail(
            passed,
            actual,
            expected,
            "=",
            f"实际值 {_format_result_value(actual)} 等于预期值 {_format_result_value(expected)}"
            if passed
            else f"JSON 字段值不匹配：{assertion['path']} 期望 {expected!r}，实际 {actual!r}",
        )
    if assertion_type == "json_path_not_empty":
        exists, actual = find_json_path(body_json, assertion["path"])
        if not exists:
            return _detail(False, "不存在", "非空", "", f"JSON 字段不存在：{assertion['path']}")
        passed = _not_empty(actual)
        return _detail(passed, _field_state(True, actual), "非空", "", "字段非空" if passed else f"JSON 字段为空：{assertion['path']}")
    if assertion_type == "json_path_contains":
        exists, actual = find_json_path(body_json, assertion["path"])
        if not exists:
            return _detail(False, "不存在", str(assertion.get("expected_value", "")), "包含", f"JSON 字段不存在：{assertion['path']}")
        expected_text = str(assertion.get("expected_value", ""))
        passed = _contains(actual, expected_text)
        return _detail(passed, actual, expected_text, "包含", "实际值包含预期值" if passed else f"JSON 字段不包含期望内容：{assertion['path']} 期望包含 {expected_text!r}")
    if assertion_type == "json_path_type":
        exists, actual = find_json_path(body_json, assertion["path"])
        if not exists:
            return _detail(False, "不存在", assertion["expected_type"], "=", f"JSON 字段不存在：{assertion['path']}")
        actual_type = _value_type(actual)
        expected_type = assertion["expected_type"]
        passed = actual_type == expected_type
        return _detail(passed, actual_type, expected_type, "=", "字段类型等于预期类型" if passed else f"JSON 字段类型不匹配：{assertion['path']} 期望 {expected_type}，实际 {actual_type}")
    if assertion_type == "json_path_length":
        exists, actual = find_json_path(body_json, assertion["path"])
        if not exists:
            return _detail(False, "不存在", int(assertion["expected_length"]), _operator_label(assertion["operator"]), f"JSON 字段不存在：{assertion['path']}")
        actual_length = _length(actual)
        if actual_length is None:
            return _detail(False, "不支持", int(assertion["expected_length"]), _operator_label(assertion["operator"]), f"JSON 字段不支持长度校验：{assertion['path']}")
        expected_length = int(assertion["expected_length"])
        operator = assertion["operator"]
        passed = _compare_length(actual_length, operator, expected_length)
        return _detail(
            passed,
            actual_length,
            expected_length,
            _operator_label(operator),
            "字段长度满足预期" if passed else f"JSON 字段长度不匹配：{assertion['path']} 实际 {actual_length}，期望 {_operator_label(operator)} {expected_length}",
        )
    return _detail(False, None, None, "", "校验项类型无效")


def find_json_path(value: Any, path: str) -> tuple[bool, Any]:
    current = value
    for segment in _parse_path(path):
        if isinstance(segment, int):
            if not isinstance(current, list) or segment < 0 or segment >= len(current):
                return False, None
            current = current[segment]
        else:
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]
    return True, current


def _parse_path(path: str) -> list[str | int]:
    text = _json_path(path)
    segments: list[str | int] = []
    index = 1
    while index < len(text):
        if text[index] == ".":
            index += 1
            start = index
            while index < len(text) and text[index] not in ".[":
                index += 1
            key = text[start:index]
            if not key:
                raise ValueError("JSON 字段路径格式不正确")
            segments.append(key)
        elif text[index] == "[":
            end = text.find("]", index)
            if end < 0:
                raise ValueError("JSON 字段路径格式不正确")
            raw = text[index + 1 : end]
            if raw.isdigit():
                segments.append(int(raw))
            else:
                try:
                    key = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError("JSON 字段路径格式不正确") from exc
                if not isinstance(key, str):
                    raise ValueError("JSON 字段路径格式不正确")
                segments.append(key)
            index = end + 1
        else:
            raise ValueError("JSON 字段路径格式不正确")
    return segments


def _json_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("$"):
        raise ValueError("JSON 字段路径必须以 $ 开头")
    return path


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return number


def _enum(value: Any, allowed: set[str], label: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"{label}无效")
    return text


def _expected_value(raw: str) -> Any:
    text = str(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _detail(passed: bool, actual: Any, expected: Any, operator: str, message: str) -> dict[str, Any]:
    return {
        "passed": passed,
        "status": "ok" if passed else "failed",
        "actual": _result_value(actual),
        "operator": operator,
        "expected": _result_value(expected),
        "message": message,
    }


def _compact_result(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "passed" and item not in {None, ""}}


def _field_state(exists: bool, value: Any) -> str:
    if not exists:
        return "不存在"
    length = _length(value)
    if length is None:
        return _value_type(value)
    return f"{_value_type(value)}，长度 {length}"


def _result_value(value: Any) -> Any:
    if isinstance(value, dict):
        return f"对象，{len(value)} 个字段"
    if isinstance(value, list):
        return f"数组，{len(value)} 项"
    return value


def _format_result_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return str(_result_value(value))
    return json.dumps(value, ensure_ascii=False)


def _contains(actual: Any, expected_text: str) -> bool:
    if isinstance(actual, str):
        return expected_text in actual
    if isinstance(actual, list):
        return _expected_value(expected_text) in actual
    if isinstance(actual, dict):
        return expected_text in actual
    return expected_text in str(actual)


def _not_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _compare_length(actual: int, operator: str, expected: int) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    if operator == "lt":
        return actual < expected
    if operator == "lte":
        return actual <= expected
    return False


def _operator_label(operator: str) -> str:
    return {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(operator, operator)


def _simple_key(value: str) -> bool:
    return value.isidentifier()


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _length(value: Any) -> int | None:
    if isinstance(value, (str, list, dict)):
        return len(value)
    return None


def _preview(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 80 else text[:77] + "..."


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "… 已截断"
    if isinstance(value, str):
        return value[:8000] + ("\n… 已截断" if len(value) > 8000 else "")
    if isinstance(value, dict):
        result = {str(key): _bounded(item, depth + 1) for key, item in list(value.items())[:200]}
        if len(value) > 200:
            result["…"] = "已截断"
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in value[:50]] + (["… 已截断"] if len(value) > 50 else [])
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:8000]
