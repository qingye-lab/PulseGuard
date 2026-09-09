from __future__ import annotations

import json
from typing import Any


BROWSER_TYPES = ("chromium", "firefox", "webkit")
BROWSER_TYPE_SET = set(BROWSER_TYPES)
DEFAULT_BROWSER_TYPE = "chromium"
DEFAULT_BROWSER_POOL_SIZE = 5
MIN_BROWSER_POOL_SIZE = 1
MAX_BROWSER_POOL_SIZE = 5


def normalize_browser_types(value: Any, *, default: list[str] | None = None, allow_empty: bool = False) -> list[str]:
    if value is None:
        result = list(default or [])
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        result = [str(item or "").strip() for item in parsed] if isinstance(parsed, list) else [value]
    elif isinstance(value, list):
        result = [str(item or "").strip() for item in value]
    else:
        raise ValueError("browser type 列表格式无效")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in result:
        browser_type = str(item or "").strip()
        if not browser_type:
            continue
        if browser_type not in BROWSER_TYPE_SET:
            raise ValueError(f"不支持的 browser type：{browser_type}")
        if browser_type in seen:
            continue
        seen.add(browser_type)
        normalized.append(browser_type)
    if not normalized and not allow_empty:
        return list(default or [DEFAULT_BROWSER_TYPE])
    return normalized


def normalize_browser_selection_mode(value: Any) -> str:
    mode = str(value or "selected_parallel").strip()
    return mode if mode in {"selected_parallel", "round_robin_all"} else "selected_parallel"


def normalize_browser_pool_sizes(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    result: dict[str, int] = {}
    for browser_type in BROWSER_TYPES:
        result[browser_type] = _bounded_pool_size(raw.get(browser_type, DEFAULT_BROWSER_POOL_SIZE))
    return result


def normalized_browser_settings(settings: dict[str, Any]) -> dict[str, Any]:
    result = dict(settings)
    legacy_browser_type = str(result.get("browser_type") or DEFAULT_BROWSER_TYPE).strip()
    if legacy_browser_type not in BROWSER_TYPE_SET:
        legacy_browser_type = DEFAULT_BROWSER_TYPE

    has_enabled_setting = "enabled_browser_types" in result and result.get("enabled_browser_types") is not None
    enabled = normalize_browser_types(result.get("enabled_browser_types"), default=[legacy_browser_type])
    if legacy_browser_type not in enabled and not has_enabled_setting:
        enabled = [legacy_browser_type, *enabled]
    if legacy_browser_type not in enabled:
        legacy_browser_type = enabled[0] if enabled else DEFAULT_BROWSER_TYPE

    prewarmed = normalize_browser_types(result.get("prewarmed_browser_types"), default=[legacy_browser_type], allow_empty=True)
    prewarmed = [browser_type for browser_type in prewarmed if browser_type in enabled]

    raw_pool_sizes = result.get("browser_pool_sizes")
    if raw_pool_sizes is None and "browser_pool_size" in result:
        legacy_size = _bounded_pool_size(result.get("browser_pool_size"))
        pool_sizes = {browser_type: legacy_size for browser_type in BROWSER_TYPES}
    else:
        pool_sizes = normalize_browser_pool_sizes(raw_pool_sizes)

    result["browser_type"] = legacy_browser_type
    result["enabled_browser_types"] = enabled
    result["prewarmed_browser_types"] = prewarmed
    result["browser_pool_sizes"] = pool_sizes
    return result


def enabled_browser_types(settings: dict[str, Any]) -> list[str]:
    return normalized_browser_settings(settings)["enabled_browser_types"]


def prewarmed_browser_types(settings: dict[str, Any]) -> list[str]:
    return normalized_browser_settings(settings)["prewarmed_browser_types"]


def browser_pool_sizes(settings: dict[str, Any]) -> dict[str, int]:
    return normalized_browser_settings(settings)["browser_pool_sizes"]


def browser_pool_size_for(settings: dict[str, Any], browser_type: str) -> int:
    sizes = browser_pool_sizes(settings)
    return sizes.get(browser_type, DEFAULT_BROWSER_POOL_SIZE)


def settings_for_browser_type(settings: dict[str, Any], browser_type: str) -> dict[str, Any]:
    normalized = normalized_browser_settings(settings)
    if browser_type not in BROWSER_TYPE_SET:
        raise ValueError(f"不支持的 browser type：{browser_type}")
    normalized["browser_type"] = browser_type
    return normalized


def browser_type_status(installed: list[str], settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    settings = normalized_browser_settings(settings or {})
    enabled = set(settings.get("enabled_browser_types") or [DEFAULT_BROWSER_TYPE])
    prewarmed = set(settings.get("prewarmed_browser_types") or [])
    installed_set = set(normalize_browser_types(installed, default=[], allow_empty=True))
    sizes = browser_pool_sizes(settings)
    return {
        browser_type: {
            "enabled": browser_type in enabled,
            "installed": browser_type in installed_set,
            "available": browser_type in enabled and browser_type in installed_set,
            "prewarmed": browser_type in prewarmed,
            "pool_size": sizes.get(browser_type, DEFAULT_BROWSER_POOL_SIZE),
        }
        for browser_type in BROWSER_TYPES
    }


def _bounded_pool_size(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("browser pool size 必须是数字")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("browser pool size 必须是数字") from exc
    return max(MIN_BROWSER_POOL_SIZE, min(MAX_BROWSER_POOL_SIZE, number))
