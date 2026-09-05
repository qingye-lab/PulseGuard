from __future__ import annotations

import json
import re
from typing import Any
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .api_assertions import has_enabled_api_assertions, normalize_api_assertions
from .browser_types import BROWSER_TYPE_SET, normalize_browser_pool_sizes, normalize_browser_selection_mode, normalize_browser_types
from .ui_assertions import has_enabled_ui_assertions, normalize_ui_assertions
from .variables import VARIABLE_NAME_PATTERN


CheckType = Literal["ui", "api"]
RunnerSelectionMode = Literal["selected_parallel", "round_robin_all"]
BrowserSelectionMode = Literal["selected_parallel", "round_robin_all"]
RunStatus = Literal["pending", "running", "ok", "failed", "timeout", "skipped"]
NotificationStatus = Literal["disabled", "not_required", "suppressed", "sent", "failed"]
NOTIFICATION_STATUSES = {"disabled", "not_required", "suppressed", "sent", "failed"}


class CheckBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: CheckType
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=5, le=86400)
    timeout_ms: int = Field(default=15000, ge=500, le=300000)
    entry_url: str = Field(min_length=1, max_length=2048)
    viewport_mode: Literal["web", "h5"] = "web"
    method: str | None = None
    headers_json: str | None = "{}"
    body: str | None = ""
    assertions_json: str | None = "[]"
    setup_script: str | None = ""
    script: str | None = ""
    tags: str | None = ""
    alert_policy_json: str | None = "{}"
    runner_selection_mode: RunnerSelectionMode = "selected_parallel"
    runner_ids: list[str] = Field(default_factory=lambda: ["local"])
    browser_selection_mode: BrowserSelectionMode = "selected_parallel"
    browser_types: list[str] = Field(default_factory=lambda: ["chromium"])

    @field_validator("script")
    @classmethod
    def script_must_define_entry(cls, value: str | None) -> str:
        text = value or ""
        if text and "async def check" not in text:
            raise ValueError("脚本必须定义 async def check(ctx)")
        return text

    @field_validator("setup_script")
    @classmethod
    def setup_script_must_define_entry(cls, value: str | None) -> str:
        text = value or ""
        if text and "async def setup" not in text:
            raise ValueError("前置脚本必须定义 async def setup(ctx, page)")
        return text

    @field_validator("headers_json")
    @classmethod
    def headers_must_be_json_object(cls, value: str | None) -> str:
        if not value or not value.strip():
            return "{}"
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Headers 必须是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Headers 必须是 JSON Object")
        return json.dumps(parsed, ensure_ascii=False)

    @field_validator("assertions_json")
    @classmethod
    def assertions_must_be_json_array(cls, value: str | None) -> str:
        if not value or not value.strip():
            return "[]"
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("校验项必须是合法 JSON") from exc
        if not isinstance(parsed, list):
            raise ValueError("校验项必须是 JSON Array")
        return json.dumps(parsed, ensure_ascii=False)

    @field_validator("alert_policy_json")
    @classmethod
    def alert_policy_must_be_json_object(cls, value: str | None) -> str:
        if not value or not value.strip():
            return "{}"
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("告警策略必须是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("告警策略必须是 JSON Object")
        return json.dumps(_alert_policy(parsed), ensure_ascii=False)

    @field_validator("runner_ids")
    @classmethod
    def runner_ids_must_be_unique(cls, value: list[str] | None) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in value or []:
            runner_id = str(raw or "").strip()
            if not runner_id:
                continue
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", runner_id):
                raise ValueError("Runner ID 格式无效")
            if runner_id in seen:
                continue
            seen.add(runner_id)
            result.append(runner_id)
        return result or ["local"]

    @field_validator("browser_types")
    @classmethod
    def browser_types_must_be_unique(cls, value: list[str] | None) -> list[str]:
        return normalize_browser_types(value, default=["chromium"])

    @model_validator(mode="after")
    def normalize_method(self) -> "CheckBase":
        if self.type == "api":
            self.method = (self.method or "GET").upper()
            self.viewport_mode = "web"
            self.setup_script = ""
            self.browser_selection_mode = "selected_parallel"
            self.browser_types = []
            self.assertions_json = json.dumps(normalize_api_assertions(self.assertions_json), ensure_ascii=False)
            if not has_enabled_api_assertions(self.assertions_json) and not (self.script or "").strip():
                raise ValueError("接口任务请至少添加一个校验项，或填写高级脚本")
        else:
            self.method = ""
            self.setup_script = self.setup_script or ""
            self.headers_json = "{}"
            self.body = ""
            self.browser_selection_mode = normalize_browser_selection_mode(self.browser_selection_mode)  # type: ignore[assignment]
            self.browser_types = normalize_browser_types(self.browser_types, default=["chromium"])
            self.assertions_json = json.dumps(normalize_ui_assertions(self.assertions_json), ensure_ascii=False)
            if not has_enabled_ui_assertions(self.assertions_json) and "async def check" not in (self.script or ""):
                raise ValueError("UI 任务请至少添加一个校验项，或填写高级脚本 async def check(ctx)")
        return self


class CheckCreate(CheckBase):
    pass


class CheckUpdate(CheckBase):
    pass


class CheckBatchRequest(BaseModel):
    action: Literal["enable", "disable", "run"]
    type: CheckType
    ids: list[int] = Field(..., min_length=1, max_length=1000)
    expected_count: int | None = Field(default=None, ge=0, le=10000)

    @field_validator("ids")
    @classmethod
    def normalize_ids(cls, value: list[int]) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for raw in value:
            check_id = int(raw)
            if check_id <= 0 or check_id in seen:
                continue
            seen.add(check_id)
            result.append(check_id)
        if not result:
            raise ValueError("请选择要操作的任务")
        return result

    @model_validator(mode="after")
    def validate_action_payload(self) -> "CheckBatchRequest":
        if self.expected_count is not None and self.expected_count != len(self.ids):
            raise ValueError("选中任务数量已变化，请刷新后重试")
        return self


class SettingsUpdate(BaseModel):
    values: dict[str, object]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("项目名称不能为空")
        return text


class ProjectUpdate(ProjectCreate):
    pass


class ProjectAlertSettingsUpdate(BaseModel):
    values: dict[str, object]


class ConfigImportRequest(BaseModel):
    bundle: dict[str, Any]
    replace_existing: bool = False


class ApiInspectRequest(BaseModel):
    type: Literal["api"] = "api"
    entry_url: str = Field(min_length=1, max_length=2048)
    method: str | None = "GET"
    headers_json: str | None = "{}"
    body: str | None = ""
    timeout_ms: int = Field(default=10000, ge=500, le=300000)

    @field_validator("headers_json")
    @classmethod
    def headers_must_be_json_object(cls, value: str | None) -> str:
        return CheckBase.headers_must_be_json_object(value)

    @model_validator(mode="after")
    def normalize_method(self) -> "ApiInspectRequest":
        self.method = (self.method or "GET").upper()
        return self


class UiInspectRequest(BaseModel):
    type: Literal["ui"] = "ui"
    entry_url: str = Field(min_length=1, max_length=2048)
    timeout_ms: int = Field(default=15000, ge=500, le=300000)
    viewport_mode: Literal["web", "h5"] = "web"
    viewport_width: int | None = Field(default=None, ge=240, le=3840)
    viewport_height: int | None = Field(default=None, ge=320, le=4096)
    setup_script: str | None = ""

    @field_validator("setup_script")
    @classmethod
    def setup_script_must_define_entry(cls, value: str | None) -> str:
        return CheckBase.setup_script_must_define_entry(value)


class UiInspectRulesRequest(UiInspectRequest):
    assertions_json: str | None = "[]"

    @field_validator("assertions_json")
    @classmethod
    def assertions_must_be_json_array(cls, value: str | None) -> str:
        return CheckBase.assertions_must_be_json_array(value)


SETTING_RANGES: dict[str, tuple[int, int]] = {
    "default_interval_seconds": (5, 86400),
    "default_ui_timeout_ms": (500, 300000),
    "default_api_timeout_ms": (500, 300000),
    "max_concurrency": (1, 20),
    "max_ui_concurrency": (1, 5),
    "max_queue_size": (1, 1000),
    "api_pool_size": (1, 20),
    "browser_pool_size": (1, 5),
    "browser_recycle_after_runs": (1, 1000),
    "max_task_runtime_seconds": (1, 600),
    "alert_cooldown_minutes": (1, 1440),
    "alert_delivery_attempts": (1, 5),
    "api_failure_confirmation_count": (1, 10),
    "ui_failure_confirmation_count": (1, 10),
    "recovery_confirmation_count": (1, 10),
    "api_retry_attempts": (0, 3),
    "ui_retry_attempts": (0, 3),
    "stale_after_intervals": (1, 10),
    "run_retention_days": (1, 365),
    "screenshot_retention_days": (1, 365),
    "trace_retention_days": (1, 365),
    "response_retention_days": (1, 365),
    "similar_failure_retention_count": (1, 100),
    "database_backup_retention": (1, 30),
}

SETTING_LABELS = {
    "default_interval_seconds": "默认执行频率",
    "default_ui_timeout_ms": "默认 UI 超时",
    "default_api_timeout_ms": "默认 API 超时",
    "max_concurrency": "最大并发任务数",
    "max_ui_concurrency": "最大 UI 并发数",
    "max_queue_size": "执行队列容量",
    "api_pool_size": "API 请求池大小",
    "browser_pool_size": "浏览器 Context 池大小",
    "max_task_runtime_seconds": "单任务最大运行时长",
    "alert_cooldown_minutes": "告警冷却时间",
    "alert_detail_base_url": "告警详情链接前缀",
    "api_failure_confirmation_count": "API 故障确认次数",
    "ui_failure_confirmation_count": "UI 故障确认次数",
    "api_retry_attempts": "API 失败重试次数",
    "ui_retry_attempts": "UI 失败重试次数",
    "run_retention_days": "运行记录保留",
    "screenshot_retention_days": "截图保留",
    "trace_retention_days": "Trace 保留",
    "response_retention_days": "Response Body 保留",
    "similar_failure_retention_count": "同错失败保留",
    "alerts_enabled": "启用告警",
    "system_alerts_enabled": "启用系统告警",
    "notification_channels": "通知渠道",
    "execution_notification_channel_ids": "执行告警渠道",
    "system_notification_channel_ids": "系统告警渠道",
    "members": "成员",
    "member_name": "成员名称",
    "member_ids": "关联成员",
    "feishu_open_id": "飞书 Open ID",
    "wecom_user_id": "企业微信 User ID",
    "wecom_mobile": "企业微信手机号",
    "dingtalk_user_id": "钉钉 User ID",
    "dingtalk_mobile": "钉钉手机号",
    "alert_tag_policies": "标签告警策略",
    "alert_policy_json": "任务告警策略",
    "alert_policy_tag": "标签告警策略标签",
    "alert_policy_enabled": "标签告警策略启用状态",
    "environment_variables": "环境变量",
    "environment_variable_name": "环境变量名称",
    "environment_variable_value": "环境变量值",
    "environment_variable_secret": "环境变量密钥标记",
    "recovery_notification": "恢复通知",
    "browser_headless": "Headless",
    "webhook_type": "通知渠道类型",
    "browser_type": "浏览器类型",
    "browser_recycle_after_runs": "浏览器回收周期",
    "webhook_url": "Webhook URL",
    "browser_proxy": "浏览器代理",
    "browser_viewport": "浏览器 Viewport",
    "read_only_token": "只读访问令牌",
    "read_only_tokens": "只读访问令牌",
    "read_only_token_id": "只读访问令牌 ID",
    "read_only_token_name": "只读访问令牌名称",
    "local_runner_name": "本机 Runner 名称",
    "local_runner_address": "本机 Runner 地址",
    "local_runner_region": "本机 Runner 网络区域",
    "maintenance_enabled": "维护公告启用状态",
    "maintenance_title": "维护公告标题",
    "maintenance_message": "维护公告内容",
    "maintenance_starts_at": "维护开始时间",
    "maintenance_ends_at": "维护结束时间",
    "dingtalk_secret": "钉钉加签密钥",
    "channel_enabled": "通知渠道启用状态",
}

BOOLEAN_SETTINGS = {
    "alerts_enabled",
    "system_alerts_enabled",
    "recovery_notification",
    "browser_headless",
    "maintenance_enabled",
    "success_response_artifacts_enabled",
    "trace_artifacts_enabled",
}

WEBHOOK_TYPES = {"feishu", "wecom", "dingtalk"}
VIEWPORT_PATTERN = re.compile(r"^[1-9]\d{2,4}x[1-9]\d{2,4}$")


def normalize_settings_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if key in SETTING_RANGES:
            normalized[key] = _bounded_int(key, value, *SETTING_RANGES[key])
        elif key in BOOLEAN_SETTINGS:
            normalized[key] = _coerce_bool(key, value)
        elif key in {"notification_channels", "system_notification_channels"}:
            normalized[key] = _notification_channels(value)
        elif key in {"execution_notification_channel_ids", "system_notification_channel_ids"}:
            normalized[key] = _string_list(key, value, max_length=80)
        elif key == "members":
            normalized[key] = _members(value)
        elif key == "alert_tag_policies":
            normalized[key] = _alert_tag_policies(value)
        elif key == "environment_variables":
            normalized[key] = _environment_variables(value)
        elif key == "alert_detail_base_url":
            normalized[key] = _absolute_http_url(key, value)
        elif key == "browser_type":
            normalized[key] = _enum(key, value, BROWSER_TYPE_SET)
        elif key == "enabled_browser_types":
            normalized[key] = normalize_browser_types(value, default=["chromium"])
        elif key == "prewarmed_browser_types":
            normalized[key] = normalize_browser_types(value, default=["chromium"], allow_empty=True)
        elif key == "browser_pool_sizes":
            normalized[key] = normalize_browser_pool_sizes(value)
        elif key == "browser_proxy":
            normalized[key] = _string(key, value, max_length=2048)
        elif key == "read_only_token":
            normalized[key] = _string(key, value, max_length=256)
        elif key == "read_only_tokens":
            normalized[key] = _read_only_tokens(value)
        elif key in {"local_runner_name", "local_runner_address", "local_runner_region"}:
            normalized[key] = _string(key, value, max_length=120)
        elif key in {"maintenance_title", "maintenance_starts_at", "maintenance_ends_at"}:
            normalized[key] = _string(key, value, max_length=200)
        elif key == "maintenance_message":
            normalized[key] = _string(key, value, max_length=2000)
        elif key == "browser_viewport":
            viewport = _string(key, value, max_length=32).lower().replace(" ", "")
            if not VIEWPORT_PATTERN.fullmatch(viewport):
                raise ValueError("浏览器 Viewport 必须使用 1440x900 这类格式")
            normalized[key] = viewport
    return normalized


class RunnerHeartbeatRequest(BaseModel):
    runner_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(default="", max_length=2048)
    network_region: str = Field(default="local", max_length=120)
    browser_version: str = Field(default="", max_length=120)
    installed_browser_types: list[str] = Field(default_factory=list)
    available_browser_types: list[str] = Field(default_factory=list)
    status: Literal["ok", "warning", "offline"] = "ok"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("installed_browser_types", "available_browser_types")
    @classmethod
    def heartbeat_browser_types_must_be_valid(cls, value: list[str] | None) -> list[str]:
        return normalize_browser_types(value, default=[], allow_empty=True)


class ProbeRunnerCreate(BaseModel):
    runner_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    address: str = Field(min_length=1, max_length=2048)
    network_region: str = Field(default="local", max_length=120)
    enabled: bool = True
    token: str = Field(min_length=1, max_length=4096)
    allow_insecure_http: bool = False

    @field_validator("address")
    @classmethod
    def address_must_be_worker_base_url(cls, value: str) -> str:
        return _runner_address(value, required=True) or ""

    @field_validator("token")
    @classmethod
    def token_must_be_worker_token(cls, value: str) -> str:
        return _worker_token(value, required=True) or ""

    @model_validator(mode="after")
    def insecure_http_requires_confirmation(self) -> "ProbeRunnerCreate":
        if self.address.startswith("http://") and not self.allow_insecure_http:
            raise ValueError("手动 HTTP 节点会明文传输 token 和任务数据，请显式确认允许不安全 HTTP")
        return self


class ProbeRunnerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    address: str | None = Field(default=None, max_length=2048)
    network_region: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    token: str | None = Field(default=None, max_length=4096)
    allow_insecure_http: bool | None = None

    @field_validator("token")
    @classmethod
    def token_must_be_worker_token(cls, value: str | None) -> str | None:
        return _worker_token(value, required=False)


class RunnerProvisionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    network_region: str = Field(default="local", max_length=120)
    target_platform: Literal["linux", "powershell"] = "linux"
    compose_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True

    @field_validator("network_region")
    @classmethod
    def network_region_must_be_text(cls, value: str) -> str:
        return value.strip() or "local"

    @field_validator("compose_url")
    @classmethod
    def compose_url_must_be_http_url(cls, value: str) -> str:
        text = value.strip()
        parts = urlsplit(text)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("Compose URL 必须是完整 http(s) 地址")
        return text


class WorkerUpdateRequest(BaseModel):
    target_image: str | None = Field(default=None, max_length=512)
    update_id: str | None = Field(default=None, max_length=120)
    force: bool = False

    @field_validator("target_image")
    @classmethod
    def target_image_must_be_image_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if len(text) > 512:
            raise ValueError("目标镜像地址过长")
        if any(char.isspace() for char in text) or text.startswith("-"):
            raise ValueError("目标镜像地址格式无效")
        return text

    @field_validator("update_id")
    @classmethod
    def update_id_must_be_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", text):
            raise ValueError("更新批次 ID 格式无效")
        return text


class BrowserInstallRequest(BaseModel):
    browser_types: list[str] = Field(default_factory=list)

    @field_validator("browser_types")
    @classmethod
    def browser_types_must_be_valid(cls, value: list[str] | None) -> list[str]:
        return normalize_browser_types(value, default=[], allow_empty=True)


class ReadOnlyTokenCreate(BaseModel):
    read_only_token_name: str = Field(default="", max_length=120)
    project_ids: list[int] = Field(..., min_length=1)

    @field_validator("project_ids")
    @classmethod
    def normalize_project_ids(cls, value: list[int]) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for raw in value:
            project_id = int(raw)
            if project_id <= 0 or project_id in seen:
                continue
            seen.add(project_id)
            result.append(project_id)
        if not result:
            raise ValueError("只读令牌至少需要绑定一个项目")
        return result


def _bounded_int(key: str, value: Any, minimum: int, maximum: int) -> int:
    label = _setting_label(key)
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return number


def _coerce_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{_setting_label(key)}必须是布尔值")


def _enum(key: str, value: Any, allowed: set[str]) -> str:
    text = _string(key, value, max_length=32)
    if text not in allowed:
        raise ValueError(f"{_setting_label(key)}不支持：{text}")
    return text


def _string(key: str, value: Any, max_length: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{_setting_label(key)}必须是字符串")
    text = value.strip()
    if len(text) > max_length:
        raise ValueError(f"{_setting_label(key)} 过长")
    return text


def _read_only_tokens(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("只读访问令牌必须是数组")

    tokens: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个只读访问令牌格式不正确")
        token_id = _string("read_only_token_id", raw.get("id"), max_length=80)
        name = _string("read_only_token_name", raw.get("name"), max_length=120) or "未命名令牌"
        token = _string("read_only_token", raw.get("token"), max_length=256)
        created_at = _string("created_at", raw.get("created_at"), max_length=80)
        if not token_id or not token:
            raise ValueError(f"第 {index} 个只读访问令牌格式不正确")
        if token_id in seen_ids:
            raise ValueError("只读访问令牌 ID 不能重复")
        if token in seen_tokens:
            raise ValueError("只读访问令牌不能重复")
        seen_ids.add(token_id)
        seen_tokens.add(token)
        raw_project_ids = raw.get("project_ids")
        if not isinstance(raw_project_ids, list):
            raise ValueError(f"第 {index} 个只读访问令牌缺少项目范围")
        project_ids: list[int] = []
        seen_project_ids: set[int] = set()
        for raw_project_id in raw_project_ids:
            project_id = int(raw_project_id)
            if project_id <= 0 or project_id in seen_project_ids:
                continue
            seen_project_ids.add(project_id)
            project_ids.append(project_id)
        if not project_ids:
            raise ValueError(f"第 {index} 个只读访问令牌缺少项目范围")
        tokens.append(
            {
                "id": token_id,
                "name": name,
                "token": token,
                "created_at": created_at,
                "project_ids": project_ids,
            }
        )
    return tokens


DEFAULT_PROJECT_ALERT_SETTINGS_KEYS = {
    "alerts_enabled",
    "notification_channels",
    "execution_notification_channel_ids",
    "alert_tag_policies",
    "alert_cooldown_minutes",
    "recovery_notification",
}


def normalize_project_alert_settings_values(values: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(values) - DEFAULT_PROJECT_ALERT_SETTINGS_KEYS)
    if unknown:
        raise ValueError(f"不支持的项目告警设置项：{', '.join(unknown)}")
    return normalize_settings_values(values)


def _absolute_http_url(key: str, value: Any) -> str:
    text = _string(key, value, max_length=2048).rstrip("/")
    if not text:
        raise ValueError(f"{_setting_label(key)}不能为空")
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"{_setting_label(key)}必须是完整的 http:// 或 https:// 地址")
    if parts.query or parts.fragment:
        raise ValueError(f"{_setting_label(key)}不能包含查询参数或锚点")
    return text


_WORKER_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])pgrn_[A-Za-z0-9_-]{16,256}(?![A-Za-z0-9_-])")


def _runner_address(value: Any, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError("节点地址不能为空")
        return None
    if not isinstance(value, str):
        raise ValueError("节点地址必须是字符串")
    text = value.strip().rstrip("/")
    if not text:
        if required:
            raise ValueError("节点地址不能为空")
        return None
    if len(text) > 2048:
        raise ValueError("节点地址过长")
    if "://" not in text:
        if "/" in text or "?" in text or "#" in text:
            raise ValueError("节点地址不能包含路径、查询参数或锚点")
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("节点地址必须是 http(s) 地址，或填写子节点 IP/域名")
    if parts.username or parts.password:
        raise ValueError("节点地址不能包含用户名或密码")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("节点地址不能包含路径、查询参数或锚点")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("节点地址端口无效") from exc
    hostname = parts.hostname
    if not hostname:
        raise ValueError("节点地址必须包含主机名或 IP")
    host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{parts.scheme}://{host}:{port or 8788}"


def _worker_token(value: Any, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError("认证信息不能为空")
        return None
    if not isinstance(value, str):
        raise ValueError("认证信息必须是字符串")
    text = value.strip()
    if not text:
        if required:
            raise ValueError("认证信息不能为空")
        return None
    matches = list(dict.fromkeys(_WORKER_TOKEN_PATTERN.findall(text)))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("认证信息只能包含一个 worker token")
    raise ValueError("认证信息必须包含 pgrn_ 开头的 worker token")


def _dingtalk_secret(value: Any) -> str:
    text = _string("dingtalk_secret", value, max_length=256)
    if text and not text.startswith("SEC") and not _is_variable_placeholder(text):
        raise ValueError("钉钉加签密钥必须以 SEC 开头")
    return text


def _is_variable_placeholder(value: str) -> bool:
    return value.startswith("${") and value.endswith("}") and VARIABLE_NAME_PATTERN.fullmatch(value[2:-1]) is not None


def _notification_channels(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("通知渠道必须是列表")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个通知渠道格式不正确")
        channel_id = _string("notification_channels", raw.get("id") or f"channel-{index}", max_length=80)
        if channel_id in seen_ids:
            raise ValueError("通知渠道 ID 不能重复")
        seen_ids.add(channel_id)
        channel_type = _enum("webhook_type", raw.get("type") or "feishu", WEBHOOK_TYPES)
        channel = {
            "id": channel_id,
            "name": _string("notification_channels", raw.get("name") or "", max_length=80),
            "type": channel_type,
            "enabled": _coerce_bool("channel_enabled", raw.get("enabled", True)),
            "webhook_url": _string("webhook_url", raw.get("webhook_url") or "", max_length=2048),
            "dingtalk_secret": _dingtalk_secret(raw.get("dingtalk_secret") or ""),
        }
        normalized.append(channel)
    return normalized


def _members(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("成员必须是列表")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_accounts: dict[str, set[str]] = {
        "feishu_open_id": set(),
        "wecom_user_id": set(),
        "wecom_mobile": set(),
        "dingtalk_user_id": set(),
        "dingtalk_mobile": set(),
    }
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个成员格式不正确")

        member_id = _string("members", raw.get("id") or f"member-{index}", max_length=80)
        if member_id in seen_ids:
            raise ValueError("成员 ID 不能重复")
        seen_ids.add(member_id)

        name = _string("member_name", raw.get("name") or "", max_length=80)
        if not name:
            raise ValueError("成员名称不能为空")
        normalized_name = name.lower()
        if normalized_name in seen_names:
            raise ValueError("成员名称不能重复")
        seen_names.add(normalized_name)

        member = {
            "id": member_id,
            "name": name,
            "feishu_open_id": _string("feishu_open_id", raw.get("feishu_open_id") or "", max_length=120),
            "wecom_user_id": _string("wecom_user_id", raw.get("wecom_user_id") or "", max_length=120),
            "wecom_mobile": _string("wecom_mobile", raw.get("wecom_mobile") or "", max_length=32),
            "dingtalk_user_id": _string("dingtalk_user_id", raw.get("dingtalk_user_id") or "", max_length=120),
            "dingtalk_mobile": _string("dingtalk_mobile", raw.get("dingtalk_mobile") or "", max_length=32),
        }
        if not any(member[field] for field in seen_accounts):
            raise ValueError(f"成员“{name}”至少需要配置一个通知渠道账号")
        for field, accounts in seen_accounts.items():
            account = member[field]
            if not account:
                continue
            if account in accounts:
                raise ValueError(f"{_setting_label(field)}不能被多个成员重复使用")
            accounts.add(account)
        normalized.append(member)
    return normalized


def _alert_tag_policies(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("标签告警策略必须是列表")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_tags: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个标签告警策略格式不正确")

        policy_id = _string("alert_tag_policies", raw.get("id") or f"tag-policy-{index}", max_length=80)
        if policy_id in seen_ids:
            raise ValueError("标签告警策略 ID 不能重复")
        seen_ids.add(policy_id)

        tag = _string("alert_policy_tag", raw.get("tag") or "", max_length=80)
        if not tag:
            raise ValueError("标签告警策略标签不能为空")
        if "," in tag:
            raise ValueError("标签告警策略标签不能包含逗号")
        normalized_tag = tag.lower()
        if normalized_tag in seen_tags:
            raise ValueError("标签告警策略标签不能重复")
        seen_tags.add(normalized_tag)

        policy = _alert_policy(raw)
        policy.pop("member_ids", None)
        policy.update(
            {
                "id": policy_id,
                "name": _string("alert_tag_policies", raw.get("name") or "", max_length=80),
                "tag": tag,
                "enabled": _coerce_bool("alert_policy_enabled", raw.get("enabled", True)),
            }
        )
        normalized.append(policy)
    return normalized


def _alert_policy(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("告警策略必须是 JSON Object")

    policy: dict[str, Any] = {}
    if "alert_cooldown_minutes" in value and value.get("alert_cooldown_minutes") not in {None, ""}:
        policy["alert_cooldown_minutes"] = _bounded_int("alert_cooldown_minutes", value.get("alert_cooldown_minutes"), 1, 1440)
    if "recovery_notification" in value and value.get("recovery_notification") is not None:
        policy["recovery_notification"] = _coerce_bool("recovery_notification", value.get("recovery_notification"))
    if "notification_channel_ids" in value and value.get("notification_channel_ids") is not None:
        channel_ids = _string_list("notification_channels", value.get("notification_channel_ids"), max_length=80)
        policy["notification_channel_ids"] = channel_ids
    if "member_ids" in value and value.get("member_ids") is not None:
        policy["member_ids"] = _string_list("member_ids", value.get("member_ids"), max_length=80)
    return policy


def _string_list(key: str, value: Any, max_length: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{_setting_label(key)}必须是列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _string(key, item, max_length=max_length)
        if not text:
            continue
        if text in seen:
            raise ValueError(f"{_setting_label(key)}不能重复")
        seen.add(text)
        result.append(text)
    return result


def _environment_variables(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("环境变量必须是列表")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 个环境变量格式不正确")

        variable_id = _string("environment_variables", raw.get("id") or f"variable-{index}", max_length=80)
        if variable_id in seen_ids:
            raise ValueError("环境变量 ID 不能重复")
        seen_ids.add(variable_id)

        name = _string("environment_variable_name", raw.get("name") or "", max_length=80)
        if not name:
            raise ValueError("环境变量名称不能为空")
        if not VARIABLE_NAME_PATTERN.fullmatch(name):
            raise ValueError("环境变量名称必须使用 NAME 或 SERVICE_TOKEN 这类格式")
        if name in seen_names:
            raise ValueError("环境变量名称不能重复")
        seen_names.add(name)

        normalized.append(
            {
                "id": variable_id,
                "name": name,
                "value": _string("environment_variable_value", raw.get("value") or "", max_length=4096),
                "secret": _coerce_bool("environment_variable_secret", raw.get("secret", False)),
            }
        )
    return normalized


def _setting_label(key: str) -> str:
    return SETTING_LABELS.get(key, key)
