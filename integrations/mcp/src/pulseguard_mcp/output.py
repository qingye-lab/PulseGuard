from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


CHECK_FIELDS = (
    "id", "project_id", "name", "type", "enabled", "tags", "monitor_status", "current_status",
    "last_run_id", "last_run_at", "last_error", "entry_url", "method", "interval_seconds", "timeout_ms",
    "runner_id", "runner_ids", "browser_type", "viewport_mode",
)
RUN_FIELDS = (
    "id", "project_id", "check_id", "check_name", "check_type", "status", "failure_kind", "trigger",
    "observation_kind", "affects_health", "started_at", "finished_at", "duration_ms", "error_message",
    "notification_status", "notification_error", "notification_sent_at", "runner_id", "runner_name", "run_group_id", "browser_type",
    "details_pruned_at", "deduplicated_count",
)
SECRET_KEY = re.compile(r"authorization|cookie|password|passwd|secret|token|api[-_]?key|webhook", re.I)
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
SECRET_TEXT = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|password|passwd|secret|[\w-]*token|api[-_]?key)"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\r\n,;}]+)"
)


def redact_url(value: str) -> str:
    try:
        url = urlsplit(value)
        host = url.netloc.rsplit("@", 1)[-1]
        query = urlencode([(key, "[redacted]") for key, _ in parse_qsl(url.query, keep_blank_values=True)])
        return urlunsplit((url.scheme, host, url.path, query, ""))
    except ValueError:
        return "[invalid URL omitted]"


def sanitize(value: Any, *, token: str = "", depth: int = 0) -> Any:
    """Conservative output reduction, not a guarantee against secrets in arbitrary prose/images."""
    if depth > 10:
        return "[nested content omitted]"
    if isinstance(value, dict):
        sensitive_path = SECRET_KEY.search(str(value.get("path") or value.get("key") or ""))
        return {
            key: "[redacted]" if SECRET_KEY.search(str(key)) or (
                sensitive_path and key in {
                    "actual", "expected", "current", "baseline", "current_actual", "baseline_actual",
                    "current_expected", "baseline_expected", "message", "current_message", "baseline_message",
                }
            ) else sanitize(item, token=token, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        result = [sanitize(item, token=token, depth=depth + 1) for item in value[:100]]
        if len(value) > 100:
            result.append({"omitted_items": len(value) - 100})
        return result
    if isinstance(value, str):
        if token:
            value = value.replace(token, "[redacted]")
        value = URL.sub(lambda match: redact_url(match.group()), value)
        value = SECRET_TEXT.sub(lambda match: match[1] + match[2] + "[redacted]", value)
        return value if len(value) <= 4000 else value[:4000] + "\n[truncated; inspect full content in the console]"
    return value


def project_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in fields if key in value}


def run_summary(run: dict[str, Any]) -> dict[str, Any]:
    result = project_fields(run, RUN_FIELDS)
    result["artifacts"] = {
        kind: bool(run.get(f"{kind}_path")) for kind in ("screenshot", "trace", "response")
    }
    return result
