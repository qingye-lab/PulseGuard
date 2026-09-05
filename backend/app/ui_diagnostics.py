"""Attempt-owned evidence shared by UI preview and execution; no site-specific rules."""
from __future__ import annotations

from typing import Any

from .context import RunFailure
from .variables import VARIABLE_PLACEHOLDER_PATTERN, mask_data, variable_lookup


class UiInspectionFailure(RunFailure):
    def __init__(self, message: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def inspection_diagnostics(ctx: Any, *, error: Exception | None = None) -> dict[str, Any]:
    # Never dump settings/process environment. Only show values explicitly generated
    # in this attempt; URL dependencies expose availability, not secret values.
    available = variable_lookup(ctx.settings)
    template = str(ctx.check.get("entry_url") or "")
    stage = getattr(ctx, "ui_stage", "prepare")
    result = {
        "stage": stage,
        "status": "failed" if error else "ok",
        "error_type": type(error).__name__ if error else None,
        "message": (str(error) or ("执行超时" if isinstance(error, TimeoutError) else type(error).__name__)) if error else None,
        "url_variables": {name: "available" if name in available else "missing"
                          for name in sorted(set(VARIABLE_PLACEHOLDER_PATTERN.findall(template)))},
        "variables": ctx.vars.snapshot(),
        "request": ctx.request_snapshot,
        "http_requests": getattr(ctx, "http_exchanges", []),
        "logs": "\n".join(ctx.logs[-50:]),
    }
    # Mask BEFORE truncation, so truncating a secret never leaves a visible prefix.
    return _bounded(mask_data(result, ctx.settings))


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
