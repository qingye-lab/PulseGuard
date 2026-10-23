from __future__ import annotations

import base64
import asyncio
import html
import hmac
import json
import time
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import storage
from .async_storage import call as storage_call
from .schemas import NOTIFICATION_STATUSES
from .variables import mask_text, resolve_text


class AlertDeliveryError(RuntimeError):
    pass


ALERTABLE_RUN_STATUSES = {"ok", "failed", "timeout"}
WEBHOOK_TYPES = {"feishu", "wecom", "dingtalk"}
_RUNNER_UNAVAILABLE_IN_FLIGHT: set[str] = set()


async def maybe_notify(
    check: dict[str, Any],
    run: dict[str, Any],
    transition: dict[str, Any],
    group_runs: list[dict[str, Any]] | None = None,
) -> None:
    global_settings = (await storage_call(storage.get_settings))
    project_id = int(check.get("project_id") or run.get("project_id") or 0)
    if project_id <= 0:
        settings = global_settings
    else:
        project = (await storage_call(storage.get_project, project_id))
        if not project or project.get("status") != "active":
            (await storage_call(_record_notification, run, "disabled"))
            return
        project_settings = (await storage_call(storage.get_project_alert_settings, project_id))
        settings = {**global_settings, **project_settings, "members": global_settings.get("members") or []}
    if not settings.get("alerts_enabled"):
        (await storage_call(_record_notification, run, "disabled"))
        return

    alert_settings = _resolve_alert_settings(check, settings)
    channels = _notification_channels(alert_settings, enabled_only=True, require_webhook=True)
    members = _selected_members(alert_settings)
    channels = [_with_channel_mentions(channel, members) for channel in channels]
    if not channels:
        (await storage_call(_record_notification, run, "disabled", error=alert_settings.get("_alert_policy_error")))
        return

    if run["status"] not in ALERTABLE_RUN_STATUSES:
        (await storage_call(_record_notification, run, "not_required"))
        return

    should_send = False
    is_recovery = False
    current_status = transition.get("current_status")
    previous_status = transition.get("previous_status")

    if current_status == "failing":
        if previous_status != "failing":
            should_send = True
        else:
            should_send = _cooldown_elapsed(
                transition.get("last_notified_at"),
                int(alert_settings.get("alert_cooldown_minutes", 30)),
            )
    elif current_status == "healthy" and previous_status in {"failing", "suspected_recovery"} and alert_settings.get("recovery_notification", True):
        should_send = True
        is_recovery = True

    channel_summary = _channels_summary(channels)
    if not should_send:
        (await storage_call(_record_notification, run, "suppressed", channel=channel_summary, error=_suppression_reason(transition, alert_settings)))
        return

    title, text = _format_message(check, run, transition, is_recovery, alert_settings, group_runs=group_runs)
    failures: list[tuple[str, Exception]] = []
    sent_channels: list[dict[str, Any]] = []

    for channel in channels:
        try:
            await _send_with_retry(channel, title, text, int(alert_settings.get("alert_delivery_attempts", 1)))
        except Exception as exc:
            failures.append((_channel_display_name(channel), exc))
        else:
            sent_channels.append(channel)

    sent_at = storage.now_iso() if sent_channels else None
    if failures:
        (await storage_call(_record_notification,
            run,
            "failed",
            channel=channel_summary,
            error=_delivery_error_summary(failures, settings),
            sent_at=sent_at,
        ))
        if sent_at:
            (await storage_call(storage.update_last_notified, int(check["id"]), sent_at))
        return

    (await storage_call(_record_notification, run, "sent", channel=channel_summary, sent_at=sent_at))
    (await storage_call(storage.update_last_notified, int(check["id"]), sent_at))


async def notify_runner_unavailable_if_needed(
    runner: dict[str, Any] | str,
    checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Single entry point for "this node just became unreachable".

    Both the background health poll and the dispatch path funnel through here,
    so a node discovered offline by either one produces exactly one alert.
    Manual deactivation and already-reported nodes are filtered by
    `should_notify_probe_runner_unavailable`, which also rate-limits retries
    after a failed delivery.
    """
    runner_id = str(runner if isinstance(runner, str) else runner.get("runner_id") or "")
    if not runner_id:
        return {"status": "skipped", "reason": "unknown_runner"}
    # Polling and dispatch share one event loop. Claim before any await so an
    # in-flight delivery cannot be duplicated by the other discovery path.
    if runner_id in _RUNNER_UNAVAILABLE_IN_FLIGHT:
        return {"status": "skipped", "reason": "in_progress"}
    _RUNNER_UNAVAILABLE_IN_FLIGHT.add(runner_id)
    try:
        try:
            latest = (await storage_call(storage.get_probe_runner, runner_id))
        except Exception:
            latest = None
        if latest is None:
            latest = runner if isinstance(runner, dict) else None
        if not isinstance(latest, dict):
            return {"status": "skipped", "reason": "unknown_runner"}
        if not (await storage_call(storage.should_notify_probe_runner_unavailable, latest)):
            return {"status": "skipped", "reason": "not_required"}
        return await notify_runner_unavailable(latest, checks or [])
    finally:
        _RUNNER_UNAVAILABLE_IN_FLIGHT.discard(runner_id)


async def notify_runner_unavailable(runner: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    runner_id = str(runner.get("runner_id") or "")
    settings = (await storage_call(storage.get_settings))
    if not settings.get("system_alerts_enabled"):
        (await storage_call(storage.mark_probe_runner_unavailable_notified, runner_id))
        return {"status": "disabled", "sent": False}
    channels = _system_notification_channels(settings)
    if not channels:
        (await storage_call(storage.mark_probe_runner_unavailable_notified, runner_id))
        return {"status": "no_channels", "sent": False}
    title = "PulseGuard 执行节点不可用"
    text = _format_runner_unavailable_message(runner, checks)
    failures: list[tuple[str, Exception]] = []
    sent_channels: list[dict[str, Any]] = []
    for channel in channels:
        try:
            await _send_with_retry(channel, title, text, int(settings.get("alert_delivery_attempts", 1)))
        except Exception as exc:
            failures.append((_channel_display_name(channel), exc))
        else:
            sent_channels.append(channel)
    if not sent_channels:
        # Nobody was told.  Record the failed attempt rather than the delivery,
        # so the node stays eligible for a retry once the cooldown expires.
        (await storage_call(storage.mark_probe_runner_unavailable_notify_failed, runner_id))
        return {
            "status": "failed",
            "sent": False,
            "channel": _channels_summary(channels),
            "error": _delivery_error_summary(failures, settings),
        }
    (await storage_call(storage.mark_probe_runner_unavailable_notified, runner_id))
    return {
        "status": "sent",
        "sent": True,
        "channel": _channels_summary(sent_channels),
        "error": _delivery_error_summary(failures, settings) if failures else None,
    }


async def notify_system_error(event: dict[str, Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        effective_settings = dict(settings or (await storage_call(storage.get_settings)))
    except Exception:
        return {"sent": False, "reason": "settings_unavailable"}
    if not effective_settings.get("system_alerts_enabled"):
        return {"sent": False, "reason": "disabled"}

    channels = _system_notification_channels(effective_settings)
    if not channels:
        return {"sent": False, "reason": "no_channels"}

    title, text = _format_system_error_message(event, effective_settings)
    failures: list[tuple[str, Exception]] = []
    sent_channels: list[dict[str, Any]] = []
    for channel in channels:
        try:
            await _send_with_retry(channel, title, text, int(effective_settings.get("alert_delivery_attempts", 1)))
        except Exception as exc:
            failures.append((_channel_display_name(channel), exc))
        else:
            sent_channels.append(channel)

    if failures:
        return {
            "sent": bool(sent_channels),
            "status": "failed",
            "channel": _channels_summary(channels),
            "error": _delivery_error_summary(failures, effective_settings),
        }
    return {"sent": bool(sent_channels), "status": "sent", "channel": _channels_summary(sent_channels)}


def _record_notification(
    run: dict[str, Any],
    status: str,
    channel: str | None = None,
    error: str | None = None,
    sent_at: str | None = None,
) -> None:
    if status not in NOTIFICATION_STATUSES:
        raise ValueError(f"unsupported notification status: {status}")
    run_id = run.get("id")
    if run_id is None:
        return
    storage.update_run_notification(int(run_id), status, channel=channel, error=error, sent_at=sent_at)


async def send_test_alert(settings: dict[str, Any]) -> None:
    channels = _notification_channels(settings, enabled_only=True, require_webhook=True)
    if not channels:
        raise AlertDeliveryError("请至少配置一个启用且填写 Webhook URL 的通知渠道")

    title, text = _test_alert_message()
    failures: list[tuple[str, Exception]] = []
    for channel in channels:
        try:
            await _send_with_retry(channel, title, text, int(settings.get("alert_delivery_attempts", 1)))
        except Exception as exc:
            failures.append((_channel_display_name(channel), exc))
    if failures:
        raise AlertDeliveryError(_delivery_error_summary(failures, settings))


def build_test_alert_preview(settings: dict[str, Any]) -> dict[str, Any]:
    title, text = _test_alert_message()
    channels = _notification_channels(settings, enabled_only=False, require_webhook=False)
    return {
        "channels": [_channel_preview(channel, title, text) for channel in channels],
        "message_text": text,
    }


async def _send_with_retry(channel: dict[str, Any], title: str, text: str, attempts: int) -> None:
    attempts = max(1, min(5, int(attempts)))
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            await send_webhook_alert(channel, title, text)
            return
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
    if last_error is not None:
        raise last_error


def _resolve_alert_settings(check: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    effective = dict(settings)
    execution_channel_ids = settings.get("execution_notification_channel_ids")
    if isinstance(execution_channel_ids, list) and execution_channel_ids:
        effective["notification_channel_ids"] = execution_channel_ids
    tag_policy = _matching_tag_policy(check, settings)
    if tag_policy:
        _apply_alert_policy(effective, tag_policy)
    _apply_alert_policy(effective, _check_alert_policy(check))
    return effective


def _matching_tag_policy(check: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    check_tags = _check_tags(check.get("tags"))
    if not check_tags:
        return {}
    for policy in settings.get("alert_tag_policies") or []:
        if not isinstance(policy, dict) or not policy.get("enabled", True):
            continue
        if str(policy.get("tag") or "").strip().lower() in check_tags:
            return policy
    return {}


def _check_alert_policy(check: dict[str, Any]) -> dict[str, Any]:
    value = check.get("alert_policy_json")
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _apply_alert_policy(settings: dict[str, Any], policy: dict[str, Any]) -> None:
    if "alert_cooldown_minutes" in policy:
        settings["alert_cooldown_minutes"] = policy.get("alert_cooldown_minutes")
    if "recovery_notification" in policy:
        settings["recovery_notification"] = policy.get("recovery_notification")
    if "notification_channel_ids" in policy:
        settings["notification_channel_ids"] = policy.get("notification_channel_ids")
    if "member_ids" in policy:
        settings["member_ids"] = policy.get("member_ids")


def _check_tags(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {tag.strip().lower() for tag in value.split(",") if tag.strip()}


async def send_webhook_alert(channel: dict[str, Any], title: str, text: str) -> None:
    webhook_type = _channel_type(channel)
    webhook_url = str(channel.get("webhook_url") or "").strip()
    if not webhook_url:
        raise AlertDeliveryError("请先填写 Webhook URL")

    request_url = _request_url(webhook_type, webhook_url, channel)
    payload = _webhook_payload(webhook_type, title, text, channel.get("mentions"))

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(request_url, json=payload)

    if response.status_code >= 400:
        raise AlertDeliveryError(f"Webhook 返回 HTTP {response.status_code}")

    error_message = _robot_error(webhook_type, response)
    if error_message:
        raise AlertDeliveryError(error_message)


def _notification_channels(
    settings: dict[str, Any],
    *,
    enabled_only: bool,
    require_webhook: bool,
) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    channel_ids = settings.get("notification_channel_ids")
    selected_ids = {str(item) for item in channel_ids} if isinstance(channel_ids, list) else None
    missing_ids = set(selected_ids or set())
    for raw_channel in settings.get("notification_channels") or []:
        if not isinstance(raw_channel, dict):
            continue

        channel_id = str(raw_channel.get("id") or "")
        if selected_ids is not None:
            if channel_id not in selected_ids:
                continue

        channel_type = _channel_type(raw_channel)
        webhook_url = resolve_text(raw_channel.get("webhook_url") or "", settings).strip()
        enabled = bool(raw_channel.get("enabled", True))
        if enabled_only and not enabled:
            continue
        if require_webhook and not webhook_url:
            continue
        if selected_ids is not None:
            missing_ids.discard(channel_id)

        channels.append(
            {
                "id": channel_id,
                "name": str(raw_channel.get("name") or _channel_type_label(channel_type)).strip()
                or _channel_type_label(channel_type),
                "type": channel_type,
                "enabled": enabled,
                "webhook_url": webhook_url,
                "dingtalk_secret": resolve_text(raw_channel.get("dingtalk_secret") or "", settings).strip(),
            }
        )
    if selected_ids is not None and missing_ids:
        settings["_alert_policy_error"] = f"告警策略引用的通知渠道不存在或不可用：{'、'.join(sorted(missing_ids))}"
    return channels


def _system_notification_channels(settings: dict[str, Any]) -> list[dict[str, Any]]:
    system_channel_ids = settings.get("system_notification_channel_ids")
    if not isinstance(system_channel_ids, list) or not system_channel_ids:
        return []
    effective = dict(settings)
    effective["notification_channels"] = settings.get("system_notification_channels") or []
    effective["notification_channel_ids"] = system_channel_ids
    return _notification_channels(effective, enabled_only=True, require_webhook=True)


def _selected_members(settings: dict[str, Any]) -> list[dict[str, Any]]:
    selected = settings.get("member_ids")
    if not isinstance(selected, list):
        return []
    selected_ids = {str(member_id) for member_id in selected}
    return [
        member
        for member in settings.get("members") or []
        if isinstance(member, dict) and str(member.get("id") or "") in selected_ids
    ]


def _with_channel_mentions(channel: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
    mentions = _channel_mentions(_channel_type(channel), members)
    if not mentions:
        return channel
    return {**channel, "mentions": mentions}


def _channel_mentions(webhook_type: str, members: list[dict[str, Any]]) -> list[dict[str, str]]:
    mentions: list[dict[str, str]] = []
    for member in members:
        name = str(member.get("name") or "").strip()
        if webhook_type == "feishu":
            user_id = str(member.get("feishu_open_id") or "").strip()
            if user_id:
                mentions.append({"name": name, "user_id": user_id})
        elif webhook_type == "wecom":
            user_id = str(member.get("wecom_user_id") or "").strip()
            mobile = str(member.get("wecom_mobile") or "").strip()
            if user_id or mobile:
                mentions.append({"name": name, "user_id": user_id, "mobile": mobile})
        elif webhook_type == "dingtalk":
            user_id = str(member.get("dingtalk_user_id") or "").strip()
            mobile = str(member.get("dingtalk_mobile") or "").strip()
            if user_id or mobile:
                mentions.append({"name": name, "user_id": user_id, "mobile": mobile})
    return mentions


def _channel_preview(channel: dict[str, Any], title: str, text: str) -> dict[str, Any]:
    webhook_type = _channel_type(channel)
    webhook_url = str(channel.get("webhook_url") or "").strip()
    signing_enabled = webhook_type == "dingtalk" and bool(str(channel.get("dingtalk_secret") or "").strip())
    return {
        "id": channel.get("id") or "",
        "name": _channel_display_name(channel),
        "type": webhook_type,
        "enabled": bool(channel.get("enabled", True)),
        "target": _safe_target(webhook_url, webhook_type, signing_enabled),
        "signing_enabled": signing_enabled,
        "payload": _webhook_payload(webhook_type, title, text),
    }


def _channel_type(channel: dict[str, Any]) -> str:
    channel_type = str(channel.get("type") or "feishu")
    return channel_type if channel_type in WEBHOOK_TYPES else "feishu"


def _channel_display_name(channel: dict[str, Any]) -> str:
    channel_type = _channel_type(channel)
    return str(channel.get("name") or _channel_type_label(channel_type)).strip() or _channel_type_label(channel_type)


def _channel_type_label(channel_type: str) -> str:
    return {"feishu": "飞书", "wecom": "企业微信", "dingtalk": "钉钉"}.get(channel_type, channel_type)


def _channels_summary(channels: list[dict[str, Any]]) -> str | None:
    if not channels:
        return None
    return "、".join(_channel_display_name(channel) for channel in channels)


def _delivery_error_summary(failures: list[tuple[str, Exception]], settings: dict[str, Any]) -> str:
    messages = [f"{name}：{_safe_error(exc, settings)}" for name, exc in failures]
    return "；".join(messages)[:500]


def _safe_error(exc: Exception, settings: dict[str, Any]) -> str:
    message = str(exc) or exc.__class__.__name__
    return mask_text(message, settings)[:500]


def _test_alert_message() -> tuple[str, str]:
    title = "PulseGuard 测试告警"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    text = "\n".join(
        [
            "#### PulseGuard 测试告警",
            "",
            "- 类型：通知渠道",
            "- 任务：配置校验",
            f"- 时间：{now}",
            "- 结果：告警链路已连接",
        ]
    )
    return title, text


def _request_url(webhook_type: str, webhook_url: str, settings: dict[str, Any]) -> str:
    if webhook_type != "dingtalk":
        return webhook_url

    secret = str(settings.get("dingtalk_secret") or "").strip()
    if not secret:
        return webhook_url

    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=sha256).digest()
    sign = base64.b64encode(digest).decode("utf-8")

    parts = urlsplit(webhook_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in {"timestamp", "sign"}]
    query.extend([("timestamp", timestamp), ("sign", sign)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _safe_target(webhook_url: str, webhook_type: str, signing_enabled: bool) -> dict[str, Any]:
    if not webhook_url:
        return {"valid": False, "origin": "", "path": "", "query_keys": [], "issues": ["Webhook URL 为空"]}

    parts = urlsplit(webhook_url)
    if not parts.scheme or not parts.netloc:
        return {"valid": False, "origin": "", "path": "", "query_keys": [], "issues": ["Webhook URL 无法解析"]}

    query_keys = [key for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
    if webhook_type == "dingtalk" and signing_enabled:
        for key in ("timestamp", "sign"):
            if key not in query_keys:
                query_keys.append(key)

    return {
        "valid": True,
        "origin": f"{parts.scheme}://{parts.netloc}",
        "path": _masked_path(parts.path),
        "query_keys": query_keys,
        "issues": [],
    }


def _masked_path(path: str) -> str:
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return "/"

    masked: list[str] = []
    for index, segment in enumerate(segments):
        if _looks_sensitive_segment(segment):
            masked.append(_mask_token(segment))
        elif len(segments) > 3 and index >= 3:
            masked.append("...")
            break
        else:
            masked.append(segment)
    return "/" + "/".join(masked)


def _looks_sensitive_segment(segment: str) -> bool:
    return len(segment) >= 16 and any(char.isdigit() for char in segment) and any(char.isalpha() for char in segment)


def _mask_token(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _robot_error(webhook_type: str, response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    if webhook_type == "dingtalk":
        errcode = _robot_failure_code(data, "errcode")
        if errcode:
            return f"钉钉返回 {errcode}：{data.get('errmsg') or '发送失败'}"
    elif webhook_type == "wecom":
        errcode = _robot_failure_code(data, "errcode")
        if errcode:
            return f"企业微信返回 {errcode}：{data.get('errmsg') or '发送失败'}"
    elif webhook_type == "feishu":
        code = _robot_failure_code(data, "code", "StatusCode")
        if code:
            return f"飞书返回 {code}：{data.get('msg') or data.get('StatusMessage') or '发送失败'}"
    return None


def _robot_failure_code(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        code = value.strip() if isinstance(value, str) else str(value)
        if code and code != "0":
            return code
        return None
    return None


def _cooldown_elapsed(last_notified_at: str | None, cooldown_minutes: int) -> bool:
    if not last_notified_at:
        return True
    try:
        last = datetime.fromisoformat(last_notified_at)
    except ValueError:
        return True
    return datetime.now().astimezone() - last >= timedelta(minutes=cooldown_minutes)


def _suppression_reason(transition: dict[str, Any], settings: dict[str, Any]) -> str:
    current_status = transition.get("current_status")
    previous_status = transition.get("previous_status")
    if current_status == "failing" and previous_status == "failing":
        cooldown = int(settings.get("alert_cooldown_minutes", 30))
        last_notified_at = transition.get("last_notified_at")
        if last_notified_at:
            return f"连续失败仍在 {cooldown} 分钟告警冷却窗口内，上次告警时间：{last_notified_at}"
        return f"连续失败仍在 {cooldown} 分钟告警冷却窗口内"
    if current_status == "suspected_failing":
        return "本轮失败仍在故障确认中，未达到故障告警阈值"
    if current_status == "suspected_recovery":
        return "本轮成功仍在恢复确认中，未达到恢复通知阈值"
    if current_status == "healthy":
        return "当前运行正常，且未满足恢复通知条件"
    return "当前运行结果未满足告警发送条件"


def _run_detail_url(check: dict[str, Any], run: dict[str, Any], settings: dict[str, Any]) -> str:
    base_url = str(settings.get("alert_detail_base_url") or "").strip().rstrip("/")
    run_id = run.get("id")
    check_id = check.get("id")
    if not base_url or run_id is None or check_id is None:
        return ""
    project_id = run.get("project_id") or check.get("project_id")
    query_values: dict[str, Any] = {"from": f"/runs?check_id={check_id}"}
    if project_id is not None:
        query_values["project_id"] = int(project_id)
    query = urlencode(query_values)
    return f"{base_url}/runs/{run_id}?{query}"


def _format_message(
    check: dict[str, Any],
    run: dict[str, Any],
    transition: dict[str, Any],
    recovery: bool,
    settings: dict[str, Any],
    group_runs: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    check_type = "UI" if check["type"] == "ui" else "API"
    title = "PulseGuard 探活恢复" if recovery else "PulseGuard 探活失败"
    detail_url = _run_detail_url(check, run, settings)
    lines = [
        f"#### {title}",
        "",
        f"- 类型：{check_type}",
        f"- 任务：{check['name']}",
        f"- 时间：{run.get('finished_at') or run.get('started_at')}",
        f"- 运行记录：{run['id']}",
    ]
    if detail_url:
        lines.append(f"- 详情页：{detail_url}")

    if recovery:
        lines.append("- 状态：已恢复")
    else:
        lines.extend(
            [
                f"- 错误：{mask_text(run.get('error_message') or '未提供错误摘要', settings)}",
                f"- 连续失败：{transition.get('consecutive_failures', 1)}",
                f"- 耗时：{run.get('duration_ms') or 0}ms",
            ]
        )
    lines.extend(_node_matrix_lines(group_runs, settings))
    return title, "\n".join(lines)


def _node_matrix_lines(group_runs: list[dict[str, Any]] | None, settings: dict[str, Any]) -> list[str]:
    """Describe how each node voted when more than one took part.

    The aggregate verdict names a single representative run, which hides the
    most useful fact a multi-node probe produces: whether every node agrees, or
    only one region is affected.
    """
    runs = [run for run in (group_runs or []) if isinstance(run, dict)]
    if len(runs) < 2:
        return []
    ok = 0
    target_failures = 0
    runner_issues = 0
    for run in runs:
        status = str(run.get("status") or "")
        if status == "ok":
            ok += 1
        elif str(run.get("failure_kind") or "") == "target" and status in {"failed", "timeout"}:
            target_failures += 1
        else:
            runner_issues += 1
    lines = [
        "",
        f"- 节点结果：{len(runs)} 个节点，成功 {ok}，目标失败 {target_failures}，执行异常 {runner_issues}",
    ]
    for run in runs:
        lines.append(f"  - {_node_result_line(run, settings)}")
    return lines


def _node_result_line(run: dict[str, Any], settings: dict[str, Any]) -> str:
    name = str(run.get("runner_name") or run.get("runner_id") or "local").strip() or "local"
    region = str(run.get("runner_region") or "").strip()
    label = f"{name}（{region}）" if region else name
    status = str(run.get("status") or "unknown")
    failure_kind = str(run.get("failure_kind") or "none")
    if status == "ok":
        return f"{label}：成功"
    verdict = "目标失败" if failure_kind == "target" else "执行异常"
    detail = mask_text(str(run.get("error_message") or ""), settings).strip()
    if detail:
        return f"{label}：{verdict}（{status}）- {_brief(detail)}"
    return f"{label}：{verdict}（{status}）"


def _brief(text: str, limit: int = 120) -> str:
    single_line = " ".join(text.split())
    return single_line if len(single_line) <= limit else f"{single_line[:limit]}…"


def _format_runner_unavailable_message(runner: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    affected = [str(check.get("name") or f"#{check.get('id')}") for check in checks[:5]]
    lines = [
        "#### PulseGuard 执行节点不可用",
        "",
        f"- Runner：{runner.get('name') or runner.get('runner_id')}",
        f"- Runner ID：{runner.get('runner_id')}",
        f"- 网络区域：{runner.get('network_region') or 'local'}",
        f"- 地址：{runner.get('address') or '-'}",
        f"- 最后心跳：{runner.get('last_seen_at') or '-'}",
    ]
    if affected:
        lines.append(f"- 影响任务：{'、'.join(affected)}")
    return "\n".join(lines)


def _format_system_error_message(event: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str]:
    title = "PulseGuard 系统异常"
    message = mask_text(str(event.get("message") or "系统异常"), settings)
    source = str(event.get("source") or "system")
    severity = str(event.get("severity") or "error")
    lines = [
        f"#### {title}",
        "",
        f"- 来源：{source}",
        f"- 级别：{severity}",
        f"- 时间：{event.get('time') or storage.now_iso()}",
        f"- 错误：{message}",
    ]
    check_name = str(event.get("check_name") or "").strip()
    if check_name:
        lines.append(f"- 任务：{mask_text(check_name, settings)}")
    if event.get("check_id") is not None:
        lines.append(f"- 任务 ID：{event.get('check_id')}")
    if event.get("run_id") is not None:
        lines.append(f"- 运行占位 ID：{event.get('run_id')}")
    runner_name = str(event.get("runner_name") or "").strip()
    if runner_name:
        lines.append(f"- 节点：{mask_text(runner_name, settings)}")
    trigger = str(event.get("trigger") or "").strip()
    if trigger:
        lines.append(f"- 触发：{trigger}")
    return title, "\n".join(lines)


def _webhook_payload(
    webhook_type: str,
    title: str,
    text: str,
    mentions: Any = None,
) -> dict[str, Any]:
    normalized_mentions = [mention for mention in mentions or [] if isinstance(mention, dict)]
    text = _text_with_mentions(webhook_type, text, normalized_mentions)
    if webhook_type == "dingtalk":
        return {
            "msgtype": "markdown",
            "markdown": {"title": title[:64], "text": text},
            "at": {
                "atMobiles": _mention_values(normalized_mentions, "mobile"),
                "atUserIds": _mention_values(normalized_mentions, "user_id"),
                "isAtAll": False,
            },
        }
    if webhook_type == "wecom":
        return {
            "msgtype": "text",
            "text": {
                "content": _plain_text(text),
                "mentioned_list": _mention_values(normalized_mentions, "user_id"),
                "mentioned_mobile_list": _mention_values(normalized_mentions, "mobile"),
            },
        }
    return {"msg_type": "text", "content": {"text": _plain_text(text)}}


def _text_with_mentions(webhook_type: str, text: str, mentions: list[dict[str, Any]]) -> str:
    if not mentions:
        return text
    if webhook_type == "feishu":
        values = [
            f'<at id="{html.escape(str(mention.get("user_id") or ""), quote=True)}"></at>'
            for mention in mentions
            if mention.get("user_id")
        ]
    elif webhook_type == "dingtalk":
        values = [
            f'@{mention.get("mobile") or mention.get("name") or mention.get("user_id")}'
            for mention in mentions
        ]
    else:
        values = [f'@{mention.get("name") or mention.get("user_id") or mention.get("mobile")}' for mention in mentions]
    return f"{text}\n- 相关成员：{' '.join(values)}" if values else text


def _mention_values(mentions: list[dict[str, Any]], key: str) -> list[str]:
    return list(dict.fromkeys(str(mention.get(key) or "").strip() for mention in mentions if str(mention.get(key) or "").strip()))


def _plain_text(text: str) -> str:
    return text.replace("#### ", "").replace("- ", "")
