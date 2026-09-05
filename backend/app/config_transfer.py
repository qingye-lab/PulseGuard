from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from . import storage
from .defaults import DEFAULT_PROJECT_ALERT_SETTINGS, DEFAULT_SETTINGS
from .schemas import CheckCreate, normalize_project_alert_settings_values, normalize_settings_values
from .variables import is_sensitive_variable_name, mask_data


EXPORT_SCHEMA_VERSION = 4
PROJECT_EXPORT_SCHEMA_VERSION = 1
CHECK_EXPORT_FIELDS = (
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


def export_config(redact: bool = True) -> dict[str, Any]:
    settings = storage.get_settings()
    export_settings = _export_settings(settings, redact=redact)
    export_runners = [_export_runner(runner, redact=redact) for runner in storage.list_probe_runners()]
    return {
        "schema": "pulseguard.config",
        "version": EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "redacted": redact,
        "settings": export_settings,
        "runners": export_runners,
    }


def preview_import(bundle: dict[str, Any]) -> dict[str, Any]:
    parsed = _parse_bundle(bundle)
    issues = parsed["issues"]
    warnings = parsed["warnings"]

    return {
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "runners": len(parsed.get("runners", [])),
            "settings": len(parsed["settings"]),
        },
        "summary": {
            "schema": parsed["schema"],
            "version": parsed["version"],
            "redacted": bool(bundle.get("redacted", False)) if isinstance(bundle, dict) else False,
        },
    }


def apply_import(bundle: dict[str, Any], *, replace_existing: bool = False) -> dict[str, Any]:
    parsed = _parse_bundle(bundle)
    if parsed["issues"]:
        raise ValueError("导入预检失败：" + "；".join(parsed["issues"]))

    updated_settings = 0
    if parsed["settings"]:
        storage.update_settings(parsed["settings"])
        updated_settings = len(parsed["settings"])

    imported_runners = 0
    for runner in parsed.get("runners", []):
        if runner.get("runner_id") == storage.LOCAL_RUNNER_ID:
            storage.update_probe_runner(storage.LOCAL_RUNNER_ID, runner)
            continue
        existing_runner = storage.get_probe_runner(str(runner.get("runner_id") or ""))
        runner_payload = _runner_import_payload(runner, existing_runner)
        if existing_runner:
            storage.update_probe_runner(str(runner["runner_id"]), runner_payload)
        else:
            storage.create_probe_runner(runner_payload, generate_token=not bool(runner.get("_missing_token")))
        imported_runners += 1

    return {
        "ok": True,
        "settings_updated": updated_settings,
        "runners_imported": imported_runners,
    }


def export_project(project_id: int, redact: bool = True) -> dict[str, Any]:
    project = storage.get_project(project_id)
    if not project:
        raise ValueError("项目不存在")
    settings = storage.settings_for_project(storage.get_settings(), project_id)
    alerts = storage.get_project_alert_settings(project_id)
    export_alerts = deepcopy(
        {key: alerts.get(key, default) for key, default in DEFAULT_PROJECT_ALERT_SETTINGS.items()}
    )
    if redact:
        export_alerts["notification_channels"] = [
            _redact_channel(channel) for channel in export_alerts.get("notification_channels") or []
        ]
        export_alerts["alert_tag_policies"] = [
            {key: value for key, value in policy.items() if key != "member_ids"}
            for policy in export_alerts.get("alert_tag_policies") or []
            if isinstance(policy, dict)
        ]
    return {
        "schema": "pulseguard.project",
        "version": PROJECT_EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "redacted": redact,
        "project": {"name": project["name"]},
        "alerts": export_alerts,
        "variables": [
            _redact_variable(item) if redact else item
            for item in storage.get_project_variables(project_id)
        ],
        "checks": [
            _export_check(check, settings, redact=redact)
            for check in storage.list_checks(project_id=project_id)
        ],
    }


def preview_project_import(bundle: dict[str, Any], project_id: int) -> dict[str, Any]:
    parsed = _parse_project_bundle(bundle)
    issues = parsed["issues"]
    warnings = parsed["warnings"]
    existing_names = {
        str(check.get("name") or "") for check in storage.list_checks(project_id=project_id)
    }
    conflicts = [check["name"] for check in parsed["checks"] if check["name"] in existing_names]
    for name in conflicts:
        warnings.append(f"当前项目已存在同名任务，导入时将自动改名：{name}")
    _validate_project_references(parsed, issues)
    return {
        "valid": not issues,
        "issues": issues,
        "warnings": warnings,
        "counts": {
            "checks": len(parsed["checks"]),
            "settings": len(parsed["alerts"]),
            "conflicts": len(conflicts),
        },
        "summary": {
            "schema": parsed["schema"],
            "version": parsed["version"],
            "redacted": bool(bundle.get("redacted", False)) if isinstance(bundle, dict) else False,
        },
    }


def apply_project_import(
    bundle: dict[str, Any], project_id: int, *, replace_existing: bool = False
) -> dict[str, Any]:
    with storage.write_transaction():
        return _apply_project_import(bundle, project_id, replace_existing=replace_existing)


def _apply_project_import(
    bundle: dict[str, Any], project_id: int, *, replace_existing: bool
) -> dict[str, Any]:
    preview = preview_project_import(bundle, project_id)
    if not preview["valid"]:
        raise ValueError("导入预检失败：" + "；".join(preview["issues"]))
    parsed = _parse_project_bundle(bundle)
    existing = {
        str(check.get("name") or ""): check for check in storage.list_checks(project_id=project_id)
    }
    used_names = set(existing)
    created = updated = renamed = 0
    imported_checks: list[dict[str, Any]] = []
    for check in parsed["checks"]:
        payload = dict(check)
        existing_check = existing.get(payload["name"])
        if replace_existing and existing_check is not None:
            saved = storage.update_check(int(existing_check["id"]), payload, project_id=project_id)
            updated += 1
        else:
            if payload["name"] in used_names:
                payload["name"] = _unique_import_name(payload["name"], used_names)
                renamed += 1
            saved = storage.create_check(payload, project_id=project_id)
            created += 1
        used_names.add(payload["name"])
        if saved:
            storage.record_check_version(saved, "imported")
            imported_checks.append({"id": saved["id"], "name": saved["name"], "type": saved["type"]})
    # Validate channels against the final task set, including untouched tasks.
    # Any failure rolls tasks, versions, variables and alerts back together.
    if "variables" in bundle:
        storage.update_project_variables(project_id, parsed["variables"])
    if parsed["alerts"]:
        storage.update_project_alert_settings(project_id, parsed["alerts"])
    return {
        "ok": True,
        "project_id": project_id,
        "alerts_updated": bool(parsed["alerts"]),
        "checks_created": created,
        "checks_updated": updated,
        "checks_renamed": renamed,
        "checks": imported_checks,
    }


def _export_settings(settings: dict[str, Any], *, redact: bool) -> dict[str, Any]:
    exported = deepcopy({key: settings.get(key, default) for key, default in DEFAULT_SETTINGS.items()})
    if not redact:
        return exported

    exported["system_notification_channels"] = [
        _redact_channel(channel) for channel in exported.get("system_notification_channels") or []
    ]
    exported["members"] = []
    exported["environment_variables"] = [_redact_variable(variable) for variable in exported.get("environment_variables") or []]
    exported["read_only_token"] = ""
    exported["read_only_tokens"] = []
    if _proxy_has_credentials(str(exported.get("browser_proxy") or "")):
        exported["browser_proxy"] = ""
    return exported


def _export_check(check: dict[str, Any], settings: dict[str, Any], *, redact: bool) -> dict[str, Any]:
    exported = {field: check.get(field) for field in CHECK_EXPORT_FIELDS}
    if not redact:
        return exported
    exported["alert_policy_json"] = _redact_member_references(exported.get("alert_policy_json"))
    return mask_data(exported, settings)


def _export_runner(runner: dict[str, Any], *, redact: bool) -> dict[str, Any]:
    exported = {
        "runner_id": runner.get("runner_id"),
        "name": runner.get("name"),
        "address": runner.get("address"),
        "network_region": runner.get("network_region"),
        "enabled": bool(runner.get("enabled", True)),
        "role": runner.get("role") or "child",
        "allow_insecure_http": bool(runner.get("allow_insecure_http")),
    }
    if not redact:
        exported["token_set"] = bool(runner.get("token_set"))
    return exported


def _redact_member_references(value: Any) -> str:
    try:
        policy = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return "{}"
    if not isinstance(policy, dict):
        return "{}"
    policy.pop("member_ids", None)
    return json.dumps(policy, ensure_ascii=False)


def _redact_channel(channel: Any) -> Any:
    if not isinstance(channel, dict):
        return channel
    redacted = dict(channel)
    webhook_url = str(redacted.get("webhook_url") or "")
    if webhook_url and "${" not in webhook_url:
        redacted["webhook_url"] = ""
    if redacted.get("dingtalk_secret"):
        redacted["dingtalk_secret"] = ""
    return redacted


def _redact_variable(variable: Any) -> Any:
    if not isinstance(variable, dict):
        return variable
    redacted = dict(variable)
    name = str(redacted.get("name") or "")
    if redacted.get("secret") or is_sensitive_variable_name(name):
        redacted["secret"] = True
        redacted["value"] = ""
    return redacted


def _parse_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    if not isinstance(bundle, dict):
        return {"schema": "", "version": None, "settings": {}, "runners": [], "issues": ["导入内容必须是 JSON Object"], "warnings": warnings}

    schema = str(bundle.get("schema") or "")
    if schema != "pulseguard.config":
        issues.append("系统配置包 schema 必须是 pulseguard.config")
    version = bundle.get("version")
    if version != EXPORT_SCHEMA_VERSION:
        issues.append(
            f"系统配置包版本必须是 v{EXPORT_SCHEMA_VERSION}；旧 v3 总包请在迁移后重新导出系统包和项目包"
        )

    raw_settings = bundle.get("settings") or {}
    settings: dict[str, Any] = {}
    if raw_settings:
        if not isinstance(raw_settings, dict):
            issues.append("settings 必须是 JSON Object")
        else:
            unknown = sorted(set(raw_settings) - set(DEFAULT_SETTINGS))
            if unknown:
                issues.append(f"settings 包含不支持的设置项：{', '.join(unknown)}")
            settings = {key: value for key, value in raw_settings.items() if key in DEFAULT_SETTINGS}
            try:
                settings = storage.normalize_settings_update_values(settings)
            except ValueError as exc:
                issues.append(str(exc))

    if "checks" in bundle or "alerts" in bundle or "project" in bundle:
        issues.append("系统配置包不能包含项目任务或项目告警")

    return {
        "schema": schema or "pulseguard.config",
        "version": version or EXPORT_SCHEMA_VERSION,
        "settings": settings,
        "runners": _parse_runners(
            bundle.get("runners") or [],
            issues,
            warnings,
            allow_legacy_insecure_http=False,
        ),
        "issues": issues,
        "warnings": warnings,
    }


def _parse_project_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    if not isinstance(bundle, dict):
        return {
            "schema": "",
            "version": None,
            "alerts": {},
            "checks": [],
            "issues": ["导入内容必须是 JSON Object"],
            "warnings": warnings,
        }
    schema = str(bundle.get("schema") or "")
    version = bundle.get("version")
    if schema != "pulseguard.project":
        issues.append("项目配置包 schema 必须是 pulseguard.project")
    if version != PROJECT_EXPORT_SCHEMA_VERSION:
        issues.append(f"项目配置包版本必须是 v{PROJECT_EXPORT_SCHEMA_VERSION}")
    if "settings" in bundle or "runners" in bundle:
        issues.append("项目配置包不能包含全局设置或 Runner")

    raw_alerts = bundle.get("alerts") or {}
    alerts: dict[str, Any] = {}
    if not isinstance(raw_alerts, dict):
        issues.append("alerts 必须是 JSON Object")
    else:
        try:
            alerts = normalize_project_alert_settings_values(raw_alerts)
        except ValueError as exc:
            issues.append(str(exc))

    variables: list[dict[str, Any]] = []
    try:
        variables = normalize_settings_values({"environment_variables": bundle.get("variables", [])})["environment_variables"]
    except ValueError as exc:
        issues.append(str(exc))
    checks: list[dict[str, Any]] = []
    raw_checks = bundle.get("checks") or []
    if not isinstance(raw_checks, list):
        issues.append("checks 必须是 JSON Array")
    else:
        seen_names: set[str] = set()
        for index, raw_check in enumerate(raw_checks, start=1):
            if not isinstance(raw_check, dict):
                issues.append(f"第 {index} 个任务格式不正确")
                continue
            payload = _check_payload(raw_check)
            try:
                normalized = CheckCreate.model_validate(payload).model_dump()
            except ValidationError as exc:
                detail = "; ".join(str(item.get("msg") or "") for item in exc.errors())
                issues.append(f"第 {index} 个任务无效：{detail}")
                continue
            if normalized["name"] in seen_names:
                warnings.append(f"导入文件内任务名称重复，导入时将自动改名：{normalized['name']}")
            seen_names.add(normalized["name"])
            checks.append(normalized)
    return {
        "schema": schema,
        "version": version,
        "alerts": alerts,
        "variables": variables,
        "checks": checks,
        "issues": issues,
        "warnings": warnings,
    }


def _validate_project_references(parsed: dict[str, Any], issues: list[str]) -> None:
    member_ids = {
        str(member.get("id") or "")
        for member in storage.get_settings().get("members", [])
        if isinstance(member, dict) and member.get("id")
    }
    channel_ids = {
        str(channel.get("id") or "")
        for channel in parsed.get("alerts", {}).get("notification_channels", [])
        if isinstance(channel, dict) and channel.get("id")
    }
    missing_members: set[str] = set()
    missing_channels: set[str] = set()
    for check in parsed.get("checks", []):
        try:
            policy = json.loads(str(check.get("alert_policy_json") or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(policy, dict):
            continue
        missing_members.update(str(item) for item in policy.get("member_ids", []) if str(item) not in member_ids)
        missing_channels.update(
            str(item) for item in policy.get("notification_channel_ids", []) if str(item) not in channel_ids
        )
    if missing_members:
        issues.append("项目包引用了全局通讯录中不存在的成员：" + "、".join(sorted(missing_members)))
    if missing_channels:
        issues.append("项目包引用了不存在的项目通知渠道：" + "、".join(sorted(missing_channels)))


def _parse_runners(
    raw_runners: Any,
    issues: list[str],
    warnings: list[str],
    *,
    allow_legacy_insecure_http: bool,
) -> list[dict[str, Any]]:
    if not raw_runners:
        return []
    if not isinstance(raw_runners, list):
        issues.append("runners 必须是 JSON Array")
        return []
    runners: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_runners, start=1):
        if not isinstance(raw, dict):
            issues.append(f"第 {index} 个 Runner 格式不正确")
            continue
        runner_id = str(raw.get("runner_id") or "").strip()
        if not runner_id:
            issues.append(f"第 {index} 个 Runner ID 不能为空")
            continue
        if runner_id in seen:
            issues.append(f"Runner ID 重复：{runner_id}")
            continue
        seen.add(runner_id)
        is_child = runner_id != storage.LOCAL_RUNNER_ID
        token = str(raw.get("token") or raw.get("token_value") or "").strip()
        enabled = bool(raw.get("enabled", True))
        address = str(raw.get("address") or "").strip()
        raw_allow_insecure_http = raw.get("allow_insecure_http")
        if raw_allow_insecure_http is not None and not isinstance(raw_allow_insecure_http, bool):
            issues.append(f"Runner {runner_id} 的 allow_insecure_http 必须是布尔值")
            continue
        allow_insecure_http = bool(raw_allow_insecure_http)
        uses_manual_http = is_child and address and (
            address.lower().startswith("http://") or "://" not in address
        )
        if uses_manual_http:
            if raw_allow_insecure_http is None and allow_legacy_insecure_http:
                allow_insecure_http = True
                warnings.append(f"Runner {runner_id} 使用旧版手工 HTTP 配置，已保留为显式允许不安全 HTTP")
            elif not allow_insecure_http:
                issues.append(f"Runner {runner_id} 使用手工 HTTP，必须显式设置 allow_insecure_http=true")
                continue
        if is_child and not token and enabled:
            warnings.append(f"Runner {runner_id} 未包含认证信息，导入后将停用；请更新认证后再启用")
            enabled = False
        runner = {
            "runner_id": runner_id,
            "name": str(raw.get("name") or runner_id).strip(),
            "address": address,
            "network_region": str(raw.get("network_region") or "local").strip(),
            "enabled": enabled,
            "role": "local" if runner_id == storage.LOCAL_RUNNER_ID else "child",
            "allow_insecure_http": allow_insecure_http,
        }
        if token:
            runner["token"] = token
        elif is_child:
            runner["_missing_token"] = True
        runners.append(runner)
    return runners


def _runner_import_payload(runner: dict[str, Any], existing_runner: dict[str, Any] | None) -> dict[str, Any]:
    payload = {key: value for key, value in runner.items() if not key.startswith("_")}
    if runner.get("_missing_token") and not (existing_runner or {}).get("token_set"):
        payload["enabled"] = False
    return payload


def _check_payload(raw_check: dict[str, Any]) -> dict[str, Any]:
    payload = {field: raw_check.get(field) for field in CHECK_EXPORT_FIELDS if field in raw_check}
    check_type = payload.get("type") or "api"
    payload.setdefault("enabled", True)
    payload.setdefault("interval_seconds", int(DEFAULT_SETTINGS["default_interval_seconds"]))
    payload.setdefault(
        "timeout_ms",
        int(DEFAULT_SETTINGS["default_ui_timeout_ms"] if check_type == "ui" else DEFAULT_SETTINGS["default_api_timeout_ms"]),
    )
    payload.setdefault("entry_url", "")
    payload.setdefault("viewport_mode", "web")
    payload.setdefault("method", "GET" if check_type == "api" else "")
    payload.setdefault("headers_json", "{}")
    payload.setdefault("body", "")
    payload.setdefault("assertions_json", "[]")
    payload.setdefault("setup_script", "")
    payload.setdefault("script", "")
    payload.setdefault("tags", "")
    payload.setdefault("alert_policy_json", "{}")
    payload.setdefault("runner_selection_mode", "selected_parallel")
    payload.setdefault("runner_ids", ["local"])
    payload.setdefault("browser_selection_mode", "selected_parallel")
    payload.setdefault("browser_types", ["chromium"] if check_type == "ui" else [])
    return payload


def _unique_import_name(name: str, used_names: set[str]) -> str:
    base = f"{name} (导入)"
    candidate = base
    index = 2
    while candidate in used_names:
        candidate = f"{base} {index}"
        index += 1
    return candidate


def _proxy_has_credentials(proxy_url: str) -> bool:
    if not proxy_url or "${" in proxy_url:
        return False
    try:
        parts = urlsplit(proxy_url)
    except ValueError:
        return False
    return bool(parts.username or parts.password)
