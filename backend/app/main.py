from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .api_assertions import inspect_api_response
from . import config_transfer, failure_summary, notifier, run_comparison, storage
from .defaults import DEFAULT_SETTINGS
from .config import APP_VERSION, BUILD_SHA, NODE_ROLE, RELAY_CONTROL_URL, RELAY_ENABLED, RELAY_PUBLIC_HOST, RELAY_PUBLIC_PORT, REPORTS_DIR, RESPONSES_DIR, RUNNER_HEALTH_POLL_SECONDS, SCREENSHOTS_DIR, STATIC_DIR, TRACES_DIR, WORKER_ADDRESS, WORKER_IMAGE, WORKER_NAME, WORKER_PRINT_TOKEN, WORKER_REGION, WORKER_RUNNER_ID, WORKER_TOKEN, WORKER_TOKEN_SOURCE, WORKER_UPDATE_IMAGE, WORKER_UPDATER_URL, ensure_runtime_dirs, relay_control_token
from .relay_cert import relay_fingerprint
from .runner import CheckRunner
from .scheduler import PulseScheduler
from .browser_installation import browser_capabilities, schedule_browser_install
from .browser_types import enabled_browser_types, normalize_browser_types
from .schemas import ApiInspectRequest, BrowserInstallRequest, CheckBatchRequest, CheckCreate, CheckUpdate, ConfigImportRequest, NOTIFICATION_STATUSES, ProbeRunnerCreate, ProbeRunnerUpdate, ReadOnlyTokenCreate, RunnerHeartbeatRequest, RunnerProvisionRequest, SettingsUpdate, UiInspectRequest, UiInspectRulesRequest, WorkerUpdateRequest, _runner_address
from .variables import mask_data, mask_text


HEARTBEAT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")
APP_DISPLAY_NAME = "PulseGuard"
APP_TEAM_NAME = "新零售测试团队"

VALIDATION_FIELD_LABELS = {
    "name": "任务名称",
    "action": "批量操作",
    "type": "任务类型",
    "enabled": "启用状态",
    "interval_seconds": "执行频率",
    "timeout_ms": "超时时间",
    "entry_url": "目标 URL",
    "viewport_mode": "页面模式",
    "method": "Method",
    "headers_json": "Headers",
    "body": "Body",
    "assertions_json": "校验项",
    "setup_script": "前置脚本",
    "script": "Python 脚本",
    "tag": "标签",
    "tags": "标签",
    "expected_count": "命中数量",
    "alert_policy_json": "任务告警策略",
    "values": "设置",
    "notification_channels": "通知渠道",
    "members": "成员",
    "member_ids": "关联成员",
    "alert_tag_policies": "标签告警策略",
    "alert_policy_tag": "标签告警策略标签",
    "environment_variables": "环境变量",
    "environment_variable_name": "环境变量名称",
    "environment_variable_value": "环境变量值",
    "environment_variable_secret": "环境变量密钥标记",
    "alert_cooldown_minutes": "告警冷却时间",
    "alert_detail_base_url": "告警详情链接前缀",
    "recovery_notification": "恢复通知",
    "max_queue_size": "执行队列容量",
    "api_pool_size": "API 请求池大小",
    "browser_pool_size": "浏览器 Context 池大小",
    "max_ui_concurrency": "最大 UI 并发数",
    "viewport_width": "Viewport 宽度",
    "viewport_height": "Viewport 高度",
    "browser_viewport": "浏览器 Viewport",
    "browser_type": "浏览器类型",
    "browser_proxy": "浏览器代理",
    "runner_id": "Runner ID",
    "address": "Runner 地址",
    "network_region": "Runner 网络区域",
    "browser_version": "Runner 浏览器版本",
    "metadata": "Runner 元数据",
    "maintenance_enabled": "维护公告启用状态",
    "maintenance_title": "维护公告标题",
    "maintenance_message": "维护公告内容",
    "maintenance_starts_at": "维护开始时间",
    "maintenance_ends_at": "维护结束时间",
    "read_only_tokens": "只读访问令牌",
    "read_only_token_name": "只读访问令牌名称",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_db()
    runner = CheckRunner()
    app.state.runner = runner
    await runner.start()
    asyncio.create_task(_sync_enabled_browser_type_installation(storage.get_settings()))
    scheduler = None
    health_poll_task = None
    if NODE_ROLE != "worker":
        scheduler = PulseScheduler(runner)
        app.state.scheduler = scheduler
        scheduler.start()
        health_poll_task = asyncio.create_task(_runner_health_poll_loop())
    else:
        app.state.scheduler = None
        _log_worker_startup_info()
    yield
    if health_poll_task is not None:
        health_poll_task.cancel()
        await asyncio.gather(health_poll_task, return_exceptions=True)
    if scheduler is not None:
        scheduler.shutdown()
    await runner.shutdown(record_cancelled=not storage.is_deployment_window_active())


ensure_runtime_dirs()

app = FastAPI(title="PulseGuard", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/artifacts/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")
app.mount("/artifacts/traces", StaticFiles(directory=TRACES_DIR), name="traces")
app.mount("/artifacts/responses", StaticFiles(directory=RESPONSES_DIR), name="responses")
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets", check_dir=False), name="frontend-assets")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "name": APP_DISPLAY_NAME, "team": APP_TEAM_NAME}


@app.get("/api/worker/health")
def worker_health(authorization: str = Header(default="")) -> dict[str, Any]:
    _verify_worker_token(_bearer_token(authorization))
    update_supported = bool(WORKER_UPDATER_URL)
    update_target = WORKER_UPDATE_IMAGE or WORKER_IMAGE
    update_status = _worker_update_status_for_health() if update_supported else {}
    current_image = str(update_status.get("current_image") or WORKER_IMAGE).strip()
    capabilities = browser_capabilities(storage.get_settings())
    return {
        "ok": True,
        "role": NODE_ROLE,
        "runner_id": WORKER_RUNNER_ID,
        "name": WORKER_NAME,
        "address": WORKER_ADDRESS,
        "network_region": WORKER_REGION,
        "browser_version": "",
        "installed_browser_types": capabilities["installed_browser_types"],
        "available_browser_types": capabilities["available_browser_types"],
        "status": "ok",
        "metadata": {
            "node_role": NODE_ROLE,
            "version": APP_VERSION,
            "build_sha": BUILD_SHA,
            "image": current_image,
            "update_supported": update_supported,
            "update_target_image": update_target,
            "update_status": update_status.get("status") or "",
            "update_message": update_status.get("message") or "",
            "update_current_image": current_image,
            "update_available": bool(update_supported and current_image and update_target and current_image != update_target),
            "browser_type_status": capabilities["browser_type_status"],
        },
    }


@app.post("/api/worker/run")
async def worker_run(request: Request, authorization: str = Header(default="")) -> dict[str, Any]:
    _verify_worker_token(_bearer_token(authorization))
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="worker run payload must be an object")
    check = raw.get("check")
    settings = raw.get("settings")
    if not isinstance(check, dict) or not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="worker run payload invalid")
    trigger = str(raw.get("trigger") or "remote")
    run_id = int(raw.get("run_id") or 0)
    result = await request.app.state.runner.execute_worker_run(check, trigger, run_id, settings)
    return {"ok": True, "run": result, "artifacts": _worker_artifacts(result)}


@app.post("/api/worker/update")
async def worker_update(payload: WorkerUpdateRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    _verify_worker_token(_bearer_token(authorization))
    return await _worker_updater_request("POST", "/update", payload.model_dump(exclude_none=True))


@app.post("/api/worker/browser-types/install")
async def worker_install_browser_types(payload: BrowserInstallRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    _verify_worker_token(_bearer_token(authorization))
    return {"ok": True, "install": schedule_browser_install(payload.browser_types)}


@app.get("/api/worker/update-status")
async def worker_update_status(authorization: str = Header(default="")) -> dict[str, Any]:
    _verify_worker_token(_bearer_token(authorization))
    return await _worker_updater_request("GET", "/status")


@app.post("/api/heartbeats/{key}")
async def record_heartbeat(key: str, request: Request) -> dict[str, Any]:
    heartbeat_key = _normalize_heartbeat_key(key)
    settings = storage.get_settings()
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="心跳内容必须是 JSON Object")
    status = str(raw.get("status") or "ok").strip().lower()
    if status not in {"ok", "failed"}:
        raise HTTPException(status_code=400, detail="心跳状态必须是 ok 或 failed")
    message = mask_text(str(raw.get("message") or "").strip(), settings)[:500]
    payload = mask_data(raw.get("payload") if isinstance(raw.get("payload"), dict) else {}, settings)
    heartbeat = storage.record_heartbeat(heartbeat_key, status=status, message=message, payload=payload)
    return {"ok": True, "heartbeat": mask_data(heartbeat, settings)}


@app.get("/api/heartbeats/{key}")
def heartbeat_detail(key: str) -> dict[str, Any]:
    heartbeat_key = _normalize_heartbeat_key(key)
    heartbeat = storage.get_heartbeat(heartbeat_key)
    if not heartbeat:
        raise HTTPException(status_code=404, detail="心跳不存在")
    return mask_data(heartbeat, storage.get_settings())


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    return storage.get_overview()


@app.get("/api/runtime")
def runtime_status(request: Request) -> dict[str, Any]:
    payload = request.app.state.runner.runtime_status()
    scheduler = request.app.state.scheduler
    payload["scheduler"] = _scheduler_runtime_status(scheduler)
    payload["deployment"] = storage.get_deployment_state()
    payload["node_role"] = NODE_ROLE
    payload["version"] = APP_VERSION
    payload["build_sha"] = BUILD_SHA
    return payload


@app.post("/api/deployment/prepare")
async def prepare_deployment(
    request: Request,
    wait_seconds: int = Query(default=60, ge=0, le=300),
    reason: str = Query(default="docker-deploy", min_length=1, max_length=80),
) -> dict[str, Any]:
    state = storage.start_deployment_window(reason)
    scheduler = request.app.state.scheduler
    if hasattr(scheduler, "pause"):
        scheduler.pause()
    runner = request.app.state.runner
    runner_status = (
        await runner.wait_for_idle(wait_seconds)
        if hasattr(runner, "wait_for_idle")
        else runner.runtime_status()
    )
    storage.record_audit_event(
        "started",
        "deployment",
        entity_name=state["reason"],
        summary="进入部署维护窗口",
        payload={"wait_seconds": wait_seconds, "runner": runner_status},
    )
    return {
        "ok": True,
        "deployment": storage.get_deployment_state(),
        "runner": runner_status,
        "scheduler": _scheduler_runtime_status(scheduler),
    }


@app.post("/api/deployment/complete")
async def complete_deployment(
    request: Request,
    run_enabled: bool = Query(default=True),
) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    runs = await _run_enabled_checks(request, "post-deploy", concurrent=False) if run_enabled else []
    runner = request.app.state.runner
    runner_status = (
        await runner.wait_for_idle(300)
        if hasattr(runner, "wait_for_idle")
        else runner.runtime_status()
    )
    discarded_incomplete_runs = storage.discard_incomplete_runs()
    state = storage.finish_deployment_window()
    if hasattr(scheduler, "resume"):
        scheduler.resume()
    elif hasattr(scheduler, "refresh_all"):
        scheduler.refresh_all()
    storage.record_audit_event(
        "completed",
        "deployment",
        entity_name=state.get("reason") or "docker-deploy",
        summary="完成部署维护窗口",
        payload={
            "run_enabled": run_enabled,
            "run_count": len(runs),
            "discarded_incomplete_runs": discarded_incomplete_runs,
            "runner": runner_status,
        },
    )
    return {
        "ok": True,
        "deployment": storage.get_deployment_state(),
        "runs": runs,
        "discarded_incomplete_runs": discarded_incomplete_runs,
        "runner": runner_status,
        "scheduler": _scheduler_runtime_status(scheduler),
    }


@app.get("/api/runners")
def runners(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    return mask_data(storage.list_probe_runners(limit=limit), storage.get_settings())


@app.get("/api/relay/status")
def relay_status(request: Request) -> dict[str, Any]:
    return _relay_status_payload(request)


@app.post("/api/runners")
def create_runner(payload: ProbeRunnerCreate) -> dict[str, Any]:
    try:
        runner = storage.create_probe_runner({**payload.model_dump(), "role": "child"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage.record_audit_event("created", "runner", runner.get("runner_id"), runner.get("name", ""), "创建执行节点")
    return _runner_response(runner)


@app.post("/api/runners/provision")
def provision_runner(payload: RunnerProvisionRequest, request: Request) -> dict[str, Any]:
    status = _relay_status_payload(request)
    if not status["enabled"] or not status.get("public_url") or not status.get("server_fingerprint"):
        raise HTTPException(status_code=503, detail=status.get("error") or "Relay 未启用")
    try:
        runner = storage.provision_probe_runner(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage.record_audit_event("created", "runner", runner.get("runner_id"), runner.get("name", ""), "生成 relay 子节点部署命令")
    return _runner_provision_response(runner, status, payload.target_platform, payload.compose_url)


@app.post("/api/runners/{runner_id}/provision/regenerate")
def regenerate_runner_provision(runner_id: str, payload: RunnerProvisionRequest, request: Request) -> dict[str, Any]:
    status = _relay_status_payload(request)
    if not status["enabled"] or not status.get("public_url") or not status.get("server_fingerprint"):
        raise HTTPException(status_code=503, detail=status.get("error") or "Relay 未启用")
    try:
        runner = storage.regenerate_probe_runner_provision(runner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not runner:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    storage.record_audit_event(
        "updated",
        "runner",
        runner.get("runner_id"),
        runner.get("name", ""),
        "重新生成 relay 子节点部署命令",
        {"relay_control_revoked": False},
    )
    return _runner_provision_response(runner, status, payload.target_platform, payload.compose_url)


@app.post("/api/runners/heartbeat")
def runner_heartbeat(payload: RunnerHeartbeatRequest, authorization: str = Header(default="")) -> dict[str, Any]:
    token = _bearer_token(authorization)
    if not storage.verify_probe_runner_token(payload.runner_id, token):
        raise HTTPException(status_code=403, detail="Runner token 无效")
    runner = storage.upsert_probe_runner(payload.model_dump())
    return {"ok": True, "runner": mask_data(runner, storage.get_settings())}


@app.put("/api/runners/{runner_id}")
def update_runner(runner_id: str, payload: ProbeRunnerUpdate) -> dict[str, Any]:
    current = storage.get_probe_runner(runner_id)
    if not current:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    data = payload.model_dump(exclude_none=True)
    if str(current.get("role")) != "local" and data.get("address") is not None:
        data["address"] = _runner_address(data["address"], required=True)
    try:
        runner = storage.update_probe_runner(runner_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not runner:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    if str(runner.get("connection_mode") or "") == "relay" and not runner.get("enabled"):
        _revoke_relay_session(runner_id, "runner disabled")
    storage.record_audit_event("updated", "runner", runner.get("runner_id"), runner.get("name", ""), "更新执行节点")
    return mask_data(runner, storage.get_settings())


@app.delete("/api/runners/{runner_id}")
def delete_runner(runner_id: str) -> dict[str, bool]:
    current = storage.get_probe_runner(runner_id)
    if not current:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    try:
        removed = storage.delete_probe_runner(runner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    revoked = False
    if str(current.get("connection_mode") or "") == "relay":
        revoked = _revoke_relay_session(runner_id, "runner deleted")
    storage.record_audit_event("deleted", "runner", runner_id, runner_id, "删除执行节点", {"relay_control_revoked": revoked})
    return {"ok": True}


@app.post("/api/runners/{runner_id}/rotate-token")
def rotate_runner_token(runner_id: str) -> dict[str, Any]:
    runner = storage.rotate_probe_runner_token(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    storage.record_audit_event("updated", "runner", runner.get("runner_id"), runner.get("name", ""), "轮换执行节点令牌")
    return _runner_response(runner)


@app.post("/api/runners/{runner_id}/update")
async def update_runner_node(runner_id: str, payload: WorkerUpdateRequest) -> dict[str, Any]:
    runner = storage.get_probe_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    if str(runner.get("role") or "") == "local":
        raise HTTPException(status_code=400, detail="本机 Runner 不支持远程更新")
    previous_status = str(runner.get("status") or "")
    storage.mark_probe_runner_updating(runner_id)
    try:
        result = await _worker_request(runner, "POST", "/api/worker/update", payload.model_dump(exclude_none=True), timeout=10)
    except Exception:
        storage.restore_probe_runner_status(runner_id, previous_status)
        raise
    storage.record_audit_event("updated", "runner", runner_id, runner.get("name", ""), "推送执行节点更新", {"target_image": payload.target_image or ""})
    return {"ok": True, "message": "节点更新任务已下发", "worker": result}


@app.get("/api/runners/{runner_id}/update-status")
async def runner_update_status(runner_id: str) -> dict[str, Any]:
    runner = storage.get_probe_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    if str(runner.get("role") or "") == "local":
        raise HTTPException(status_code=400, detail="本机 Runner 不支持远程更新")
    result = await _worker_request(runner, "GET", "/api/worker/update-status", timeout=10)
    return {"ok": True, "worker": result}


@app.post("/api/runners/{runner_id}/test")
async def test_runner_connection(runner_id: str) -> dict[str, Any]:
    runner = storage.get_probe_runner(runner_id)
    if not runner:
        raise HTTPException(status_code=404, detail="Runner 不存在")
    if str(runner.get("role")) == "local":
        return {"ok": True, "message": "local Runner 可用", "runner": runner}
    try:
        health = await _fetch_runner_health(runner)
        storage.mark_probe_runner_available(runner_id, health)
    except Exception as exc:
        storage.mark_probe_runner_unavailable(runner_id)
        raise HTTPException(status_code=400, detail=f"Runner 连接失败：{exc}") from exc
    return {"ok": True, "message": "Runner 连接正常", "worker": health}


@app.get("/api/status-page")
def status_page() -> dict[str, Any]:
    return _status_page_payload()


@app.get("/api/read-only/snapshot")
def read_only_snapshot(
    token: str = Query(default=""),
    x_pulseguard_read_only_token: str = Header(default=""),
    authorization: str = Header(default=""),
) -> dict[str, Any]:
    _verify_read_only_token(_read_only_token_value(token, x_pulseguard_read_only_token, authorization))
    settings = storage.get_settings()
    overview = storage.get_overview()
    return {
        "overview": _read_only_overview_payload(overview, settings),
        "checks": [_read_only_check_payload(check, settings) for check in storage.list_checks(refresh_stale=False)],
        "recent_runs": [_read_only_run_payload(run, settings) for run in storage.list_runs(limit=100, summary_only=True)],
    }


@app.get("/api/metrics.json")
def metrics_json() -> dict[str, Any]:
    return _metrics_payload()


@app.get("/api/metrics")
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(_prometheus_metrics(_metrics_payload()), media_type="text/plain; version=0.0.4")


@app.get("/api/checks")
def checks(type: str | None = Query(default=None)) -> list[dict[str, Any]]:
    if type and type not in {"ui", "api"}:
        raise HTTPException(status_code=400, detail="任务类型无效")
    return storage.list_checks(type)


@app.get("/api/audit-events")
def audit_events(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    return mask_data(storage.list_audit_events(limit=limit), storage.get_settings())


@app.get("/api/run-archives")
def run_archives(limit: int = Query(default=90, ge=1, le=365)) -> list[dict[str, Any]]:
    return storage.list_run_archives(limit=limit)


@app.get("/api/monitoring-trends")
def monitoring_trends(
    period: str = Query(default="24h"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    type: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=12, ge=1, le=50),
    hour_start: str | None = Query(default=None),
    hour_end: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        return storage.list_monitoring_trends(
            period=period,
            start=start,
            end=end,
            check_type=type,
            q=q,
            page=page,
            page_size=page_size,
            hour_start=hour_start,
            hour_end=hour_end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/anomaly-cycles")
def anomaly_cycles(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    if status and status not in {"open", "resolved"}:
        raise HTTPException(status_code=400, detail="异常周期状态无效")
    return storage.list_anomaly_cycles(limit=limit, status=status)


@app.get("/api/database-backups")
def database_backups() -> list[dict[str, Any]]:
    return storage.list_database_backups()


@app.post("/api/database-backups")
def create_database_backup() -> dict[str, Any]:
    backup = storage.create_database_backup()
    storage.record_audit_event("created", "database_backup", entity_name=backup["filename"], summary="创建数据库备份")
    return backup


@app.post("/api/database-backups/{filename}/restore")
def restore_database_backup(filename: str, request: Request) -> dict[str, Any]:
    try:
        result = storage.restore_database_backup(filename)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.scheduler.refresh_all()
    storage.record_audit_event("restored", "database_backup", entity_name=filename, summary="恢复数据库备份")
    return result


@app.post("/api/checks")
def create_check(payload: CheckCreate, request: Request) -> dict[str, Any]:
    data = _validated_check_payload(payload)
    check = storage.create_check(data)
    storage.record_check_version(check, "created")
    storage.record_audit_event("created", "check", check["id"], check["name"], "创建任务", {"type": check["type"]})
    request.app.state.scheduler.sync_check(int(check["id"]))
    return check


@app.post("/api/checks/run-all")
async def run_all_checks(request: Request, type: str | None = Query(default=None)) -> dict[str, Any]:
    if type and type not in {"ui", "api"}:
        raise HTTPException(status_code=400, detail="任务类型无效")
    results = await _run_enabled_checks(request, "manual-batch", type)
    return {"runs": results}


@app.post("/api/checks/batch")
async def batch_checks(payload: CheckBatchRequest, request: Request) -> dict[str, Any]:
    storage.refresh_stale_statuses()
    checks_by_id: dict[int, dict[str, Any]] = {}
    for check_id in payload.ids:
        check = storage.get_check(check_id, refresh_stale=False)
        if check and check.get("type") == payload.type:
            checks_by_id[int(check["id"])] = check

    ids = [check_id for check_id in payload.ids if check_id in checks_by_id]
    if payload.expected_count is not None and payload.expected_count != len(ids):
        raise HTTPException(status_code=409, detail=f"选中任务数量已变化：当前 {len(ids)} 条，请刷新后重试")

    if payload.action == "run":
        runnable_ids = [check_id for check_id in ids if bool(checks_by_id[check_id].get("enabled"))]
        if len(runnable_ids) != len(ids):
            raise HTTPException(status_code=409, detail="选中任务包含已禁用项，请刷新后重新选择")
        runs = await asyncio.gather(
            *[
                request.app.state.runner.run_check(check_id, trigger="manual-batch")
                for check_id in runnable_ids
            ]
        )
        return {"matched": len(runnable_ids), "changed": 0, "ids": runnable_ids, "runs": runs}

    if payload.action == "enable":
        changed = storage.batch_set_check_enabled(ids, True)
    else:
        changed = storage.batch_set_check_enabled(ids, False)

    for check_id in ids:
        request.app.state.scheduler.sync_check(check_id)
    storage.record_audit_event(
        payload.action,
        "check_batch",
        entity_name=payload.type,
        summary="批量操作任务",
        payload={"type": payload.type, "matched": len(ids), "changed": changed, "ids": ids},
    )
    return {"matched": len(ids), "changed": changed, "ids": ids, "runs": []}


@app.post("/api/checks/debug")
async def debug_check(payload: CheckCreate, request: Request) -> dict[str, Any]:
    return await request.app.state.runner.run_draft(_validated_check_payload(payload), trigger="draft-debug")


@app.post("/api/checks/inspect-api")
async def inspect_api_check(payload: ApiInspectRequest, request: Request) -> dict[str, Any]:
    settings = storage.get_settings()
    runner = getattr(request.app.state, "runner", None)
    resources = getattr(runner, "resources", None)
    try:
        return await inspect_api_response(payload.model_dump(), settings, resources=resources)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=mask_text(str(exc), settings)) from exc
    except httpx.HTTPError as exc:
        message = str(exc) or exc.__class__.__name__
        raise HTTPException(status_code=400, detail=mask_text(f"接口请求失败：{message}", settings)) from exc


@app.post("/api/checks/inspect-ui")
async def inspect_ui_check(payload: UiInspectRequest, request: Request) -> dict[str, Any]:
    settings = storage.get_settings()
    try:
        return await request.app.state.runner.inspect_ui(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=mask_text(str(exc), settings)) from exc
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        raise HTTPException(status_code=400, detail=mask_text(f"页面扫描失败：{message}", settings)) from exc


@app.post("/api/checks/inspect-ui-rules")
async def inspect_ui_rules(payload: UiInspectRulesRequest, request: Request) -> dict[str, Any]:
    settings = storage.get_settings()
    try:
        return await request.app.state.runner.inspect_ui_rules(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=mask_text(str(exc), settings)) from exc
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        raise HTTPException(status_code=400, detail=mask_text(f"规则检测失败：{message}", settings)) from exc


@app.get("/api/checks/{check_id}")
def check_detail(check_id: int) -> dict[str, Any]:
    check = storage.get_check(check_id)
    if not check:
        raise HTTPException(status_code=404, detail="任务不存在")
    return check


@app.get("/api/checks/{check_id}/versions")
def check_versions(check_id: int, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    if not storage.get_check(check_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return mask_data(storage.list_check_versions(check_id, limit=limit), storage.get_settings())


@app.get("/api/checks/{check_id}/trend")
def check_trend(
    check_id: int,
    period: str = Query(default="24h"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    hour_start: str | None = Query(default=None),
    hour_end: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        trend = storage.get_check_trend(
            check_id,
            period=period,
            start=start,
            end=end,
            hour_start=hour_start,
            hour_end=hour_end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not trend:
        raise HTTPException(status_code=404, detail="任务不存在")
    return trend


@app.post("/api/check-versions/{version_id}/restore")
def restore_check_version(version_id: int, request: Request) -> dict[str, Any]:
    version = storage.get_check_version(version_id)
    if not version:
        raise HTTPException(status_code=404, detail="任务版本不存在")
    snapshot = dict(version["snapshot"])
    payload = _validated_check_data(CheckCreate.model_validate(snapshot).model_dump())
    check_id = int(version["check_id"])
    current = storage.get_check(check_id)
    if current:
        storage.record_check_version(current, "restored-from")
        restored = storage.update_check(check_id, payload)
    else:
        restored = storage.create_check(payload)
        check_id = int(restored["id"])
    if not restored:
        raise HTTPException(status_code=404, detail="任务不存在")
    storage.record_audit_event("restored", "check", check_id, restored["name"], "恢复任务版本", {"version_id": version_id})
    request.app.state.scheduler.sync_check(check_id)
    return restored


@app.put("/api/checks/{check_id}")
def update_check(check_id: int, payload: CheckUpdate, request: Request) -> dict[str, Any]:
    previous = storage.get_check(check_id)
    data = _validated_check_payload(payload)
    check = storage.update_check(check_id, data)
    if not check:
        raise HTTPException(status_code=404, detail="任务不存在")
    if previous:
        storage.record_check_version(previous, "updated")
    storage.record_audit_event("updated", "check", check_id, check["name"], "更新任务", {"type": check["type"]})
    request.app.state.scheduler.sync_check(check_id)
    return check


@app.delete("/api/checks/{check_id}")
def delete_check(check_id: int, request: Request) -> dict[str, bool]:
    previous = storage.get_check(check_id)
    removed = storage.delete_check(check_id)
    if not removed:
        raise HTTPException(status_code=404, detail="任务不存在")
    if previous:
        storage.record_check_version(previous, "deleted")
        storage.record_audit_event("deleted", "check", check_id, previous["name"], "删除任务", {"type": previous["type"]})
    request.app.state.scheduler.sync_check(check_id)
    return {"ok": True}


@app.post("/api/checks/{check_id}/enable")
def enable_check(check_id: int, request: Request) -> dict[str, Any]:
    check = storage.set_check_enabled(check_id, True)
    if not check:
        raise HTTPException(status_code=404, detail="任务不存在")
    storage.record_audit_event("enabled", "check", check_id, check["name"], "启用任务", {"type": check["type"]})
    request.app.state.scheduler.sync_check(check_id)
    return check


@app.post("/api/checks/{check_id}/disable")
def disable_check(check_id: int, request: Request) -> dict[str, Any]:
    check = storage.set_check_enabled(check_id, False)
    if not check:
        raise HTTPException(status_code=404, detail="任务不存在")
    storage.record_audit_event("disabled", "check", check_id, check["name"], "禁用任务", {"type": check["type"]})
    request.app.state.scheduler.sync_check(check_id)
    return check


@app.post("/api/checks/{check_id}/run")
async def run_check(check_id: int, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.runner.run_check(check_id, trigger="manual")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/checks/{check_id}/confirm-recovery")
async def confirm_recovery(check_id: int, request: Request) -> dict[str, Any]:
    try:
        return await request.app.state.runner.run_check(check_id, trigger="confirm-recovery")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/runs")
def runs(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    notification_status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    check_id: int | None = Query(default=None),
    runner_id: str | None = Query(default=None),
    run_group_id: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    if type and type not in {"ui", "api"}:
        raise HTTPException(status_code=400, detail="任务类型无效")
    if status and status not in {"ok", "failed", "timeout", "skipped", "running", "pending"}:
        raise HTTPException(status_code=400, detail="运行状态无效")
    if notification_status and notification_status not in NOTIFICATION_STATUSES:
        raise HTTPException(status_code=400, detail="告警状态无效")
    return storage.list_runs(
        {
            "check_type": type,
            "status": status,
            "notification_status": notification_status,
            "q": q,
            "check_id": check_id,
            "runner_id": runner_id,
            "run_group_id": run_group_id,
            "start": start,
            "end": end,
        },
        limit=limit,
    )


@app.get("/api/runs-page")
def runs_page(
    type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    notification_status: str | None = Query(default=None),
    observation_kind: str | None = Query(default=None),
    trigger: str | None = Query(default=None),
    q: str | None = Query(default=None),
    check_id: int | None = Query(default=None),
    runner_id: str | None = Query(default=None),
    run_group_id: str | None = Query(default=None),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if type and type not in {"ui", "api"}:
        raise HTTPException(status_code=400, detail="任务类型无效")
    if status and status not in {"ok", "failed", "timeout", "skipped", "running", "pending"}:
        raise HTTPException(status_code=400, detail="运行状态无效")
    if observation_kind and observation_kind not in {"observation", "verification", "draft"}:
        raise HTTPException(status_code=400, detail="运行来源类型无效")
    return storage.list_runs_page(
        {
            "check_type": type,
            "status": status,
            "notification_status": notification_status,
            "observation_kind": observation_kind,
            "trigger": trigger,
            "q": q,
            "check_id": check_id,
            "runner_id": runner_id,
            "run_group_id": run_group_id,
            "start": start,
            "end": end,
        },
        page=page,
        page_size=page_size,
    )


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    settings = storage.get_settings()
    return _run_detail_payload(run, settings)


def _run_detail_payload(run: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    payload = dict(run)
    payload["check"] = _run_snapshot_request_payload(run, settings)
    return payload


def _run_snapshot_request_payload(run: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any] | None:
    check_type = str(run.get("check_type") or "")
    if check_type != "ui":
        return None
    request_snapshot = _json_object(run.get("request_snapshot"))
    response_snapshot = _json_object(run.get("response_snapshot"))
    page = _object_value(response_snapshot.get("page"))
    timings = _object_value(response_snapshot.get("timings"))
    entry_url = str(request_snapshot.get("url") or request_snapshot.get("entry_url") or page.get("url") or "")
    timeout_ms = request_snapshot.get("timeout_ms") or timings.get("assertion_timeout_ms")
    viewport_mode = request_snapshot.get("viewport_mode") or page.get("viewport_mode")
    if not entry_url and timeout_ms is None and not viewport_mode:
        return None
    payload: dict[str, Any] = {
        "id": run.get("check_id"),
        "name": run.get("check_name"),
        "type": check_type,
        "entry_url": entry_url,
        "timeout_ms": timeout_ms,
        "viewport_mode": viewport_mode or "web",
    }
    return mask_data(payload, settings)


def _json_object(value: Any) -> dict[str, Any]:
    text = str(value or "{}").strip() or "{}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _object_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@app.get("/api/runs/{run_id}/failure-summary")
def run_failure_summary(run_id: int) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    baseline = storage.get_previous_successful_run(run)
    return _run_failure_summary_payload(run, baseline)


@app.get("/api/runs/{run_id}/compare-success")
def run_success_comparison(run_id: int) -> dict[str, Any]:
    try:
        return run_comparison.compare_with_previous_success(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/rerun")
async def rerun(run_id: int, request: Request) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    if int(run["check_id"]) <= 0:
        raise HTTPException(status_code=400, detail="临时运行记录不能重新执行")
    try:
        return await request.app.state.runner.run_check(int(run["check_id"]), trigger=f"rerun:{run_id}")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return storage.get_public_settings()


@app.put("/api/settings")
async def update_settings(payload: SettingsUpdate, request: Request) -> dict[str, Any]:
    try:
        settings = storage.update_settings(payload.values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await request.app.state.runner.reload_settings()
    if "enabled_browser_types" in payload.values:
        asyncio.create_task(_sync_enabled_browser_type_installation(settings))
    request.app.state.scheduler.refresh_all()
    storage.record_audit_event("updated", "settings", summary="更新系统设置", payload={"fields": sorted(payload.values)})
    return storage.get_public_settings()


@app.post("/api/read-only-tokens")
def create_read_only_token(payload: ReadOnlyTokenCreate) -> dict[str, Any]:
    try:
        token = storage.create_read_only_token(payload.read_only_token_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage.record_audit_event(
        "created",
        "read_only_token",
        token.get("id"),
        token.get("name", ""),
        "新建只读访问令牌",
        {"name": token.get("name", "")},
    )
    return token


@app.delete("/api/read-only-tokens/{token_id}")
def delete_read_only_token(token_id: str) -> dict[str, Any]:
    try:
        settings = storage.delete_read_only_token(token_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    storage.record_audit_event(
        "deleted",
        "read_only_token",
        token_id,
        summary="删除只读访问令牌",
    )
    return settings


@app.post("/api/settings/test-alert")
async def test_alert(payload: SettingsUpdate) -> dict[str, Any]:
    settings = _settings_with_overrides(payload)
    try:
        await notifier.send_test_alert(settings)
    except notifier.AlertDeliveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "测试告警已发送"}


@app.post("/api/settings/alert-preview")
def alert_preview(payload: SettingsUpdate) -> dict[str, Any]:
    return notifier.build_test_alert_preview(_settings_with_overrides(payload))


@app.get("/api/config/export")
def export_config(redact: bool = Query(default=True)) -> dict[str, Any]:
    return config_transfer.export_config(redact=redact)


@app.post("/api/config/import-preview")
def import_config_preview(payload: ConfigImportRequest) -> dict[str, Any]:
    return config_transfer.preview_import(payload.bundle)


@app.post("/api/config/import")
async def import_config(payload: ConfigImportRequest, request: Request) -> dict[str, Any]:
    try:
        result = config_transfer.apply_import(payload.bundle, replace_existing=payload.replace_existing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await request.app.state.runner.reload_settings()
    storage.record_audit_event("imported", "config", summary="导入配置", payload=result)
    return result


def _settings_with_overrides(payload: SettingsUpdate) -> dict[str, Any]:
    settings = storage.get_settings()
    unknown = sorted(set(payload.values) - set(DEFAULT_SETTINGS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的设置项：{', '.join(unknown)}")
    try:
        for key, value in storage.normalize_settings_update_values(payload.values).items():
            if key in DEFAULT_SETTINGS:
                settings[key] = value
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return settings


def _validated_check_payload(payload: CheckCreate | CheckUpdate) -> dict[str, Any]:
    return _validated_check_data(payload.model_dump())


def _validated_check_data(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("type") != "ui":
        return data
    enabled_types = set(enabled_browser_types(storage.get_settings()))
    selected_types = [str(item) for item in data.get("browser_types") or []]
    disabled = [browser_type for browser_type in selected_types if browser_type not in enabled_types]
    if disabled:
        raise HTTPException(status_code=400, detail=f"browser type 未启用：{', '.join(disabled)}")
    return data


def _verify_read_only_token(token: str) -> None:
    settings = storage.get_settings()
    expected_tokens = [str(settings.get("read_only_token") or "").strip()]
    expected_tokens.extend(
        str(item.get("token") or "").strip()
        for item in settings.get("read_only_tokens", [])
        if isinstance(item, dict)
    )
    expected_tokens = [expected for expected in expected_tokens if expected]
    if not expected_tokens:
        raise HTTPException(status_code=403, detail="只读访问令牌未配置")
    if not any(secrets.compare_digest(token, expected) for expected in expected_tokens):
        raise HTTPException(status_code=403, detail="只读访问令牌无效")


def _read_only_token_value(query_token: str, header_token: str, authorization: str) -> str:
    auth_value = authorization.strip()
    if auth_value.lower().startswith("bearer "):
        return auth_value[7:].strip()
    return (header_token or query_token).strip()


def _bearer_token(authorization: str) -> str:
    value = str(authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _verify_worker_token(token: str) -> None:
    if not WORKER_TOKEN:
        raise HTTPException(status_code=403, detail="Worker token 未配置")
    if not secrets.compare_digest(token, WORKER_TOKEN):
        raise HTTPException(status_code=403, detail="Worker token 无效")


def _worker_artifacts(run: dict[str, Any]) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    roots = {
        "screenshot_path": SCREENSHOTS_DIR,
        "trace_path": TRACES_DIR,
        "response_path": RESPONSES_DIR,
    }
    max_bytes = 10 * 1024 * 1024
    for field, root in roots.items():
        relative = str(run.get(field) or "")
        if not relative:
            continue
        name = Path(relative.replace("\\", "/")).name
        path = (root / name).resolve()
        root_resolved = root.resolve()
        if path.parent != root_resolved or not path.exists() or not path.is_file():
            continue
        if path.stat().st_size > max_bytes:
            continue
        import base64

        artifacts[field] = {
            "filename": path.name,
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    return artifacts


def _runner_response(runner: dict[str, Any]) -> dict[str, Any]:
    token = runner.get("token")
    hidden_fields = {"token", "token_value", "token_hash", "_token", "relay_token", "worker_token"}
    visible = mask_data({key: value for key, value in runner.items() if key not in hidden_fields}, storage.get_settings())
    if token:
        visible["token"] = token
    return visible


def _scheduler_runtime_status(scheduler: Any) -> dict[str, Any]:
    if hasattr(scheduler, "runtime_status"):
        return scheduler.runtime_status()
    return {
        "running": False,
        "paused": False,
        "scheduled_checks": 0,
        "next_due_at": None,
        "overdue_jobs": 0,
    }


async def _run_enabled_checks(
    request: Request,
    trigger: str,
    check_type: str | None = None,
    concurrent: bool = True,
) -> list[dict[str, Any]]:
    checks_to_run = [check for check in storage.list_checks(check_type, enabled_only=True)]
    if not concurrent:
        results: list[dict[str, Any]] = []
        for check in checks_to_run:
            results.append(await request.app.state.runner.run_check(int(check["id"]), trigger=trigger))
        return results
    return await asyncio.gather(
        *[
            request.app.state.runner.run_check(int(check["id"]), trigger=trigger)
            for check in checks_to_run
        ]
    )


def _relay_status_payload(request: Request) -> dict[str, Any]:
    host = RELAY_PUBLIC_HOST or (request.url.hostname or "")
    public_url = f"wss://{host}:{RELAY_PUBLIC_PORT}/relay/connect" if host else ""
    error = ""
    fingerprint = ""
    if not RELAY_ENABLED:
        error = "Relay 未启用；设置 PULSEGUARD_RELAY_ENABLED=true 并启动 pulseguard-relay 服务"
    elif not host:
        error = "Relay 公网地址未配置；设置 PULSEGUARD_RELAY_PUBLIC_HOST"
    else:
        try:
            fingerprint = relay_fingerprint()
        except Exception as exc:
            error = f"Relay 自签证书不可用：{exc}"
    return {
        "enabled": bool(RELAY_ENABLED and public_url and fingerprint and not error),
        "public_host": host,
        "public_port": RELAY_PUBLIC_PORT,
        "public_url": public_url,
        "server_fingerprint": fingerprint,
        "error": error,
    }


def _revoke_relay_session(runner_id: str, reason: str) -> bool:
    if not RELAY_ENABLED or not RELAY_CONTROL_URL:
        return False
    try:
        with httpx.Client(timeout=3) as client:
            response = client.post(
                f"{RELAY_CONTROL_URL}/relay/control/revoke",
                json={"runner_id": runner_id, "reason": reason},
                headers={"Authorization": f"Bearer {relay_control_token()}"},
            )
        response.raise_for_status()
        payload = response.json()
        return bool(isinstance(payload, dict) and payload.get("revoked"))
    except (httpx.HTTPError, ValueError):
        return False


def _runner_provision_response(runner: dict[str, Any], relay_status: dict[str, Any], target_platform: str, compose_url: str) -> dict[str, Any]:
    visible = _runner_response(runner)
    command = _runner_deploy_command(runner, relay_status, target_platform, compose_url)
    visible["deployment"] = {
        "target_platform": target_platform,
        "compose_url": compose_url,
        "command": command,
        "expires_at": runner.get("deploy_command_expires_at"),
        "relay_url": relay_status.get("public_url"),
        "server_fingerprint": relay_status.get("server_fingerprint"),
    }
    return visible


def _shell_quote(value: object) -> str:
    text = str(value or "")
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _ps_quote(value: object) -> str:
    return '"' + str(value or "").replace("`", "``").replace('"', '`"') + '"'


def _runner_deploy_command(runner: dict[str, Any], relay_status: dict[str, Any], target_platform: str, compose_url: str) -> str:
    env = {
        "PULSEGUARD_WORKER_NAME": runner.get("name") or runner.get("runner_id"),
        "PULSEGUARD_WORKER_REGION": runner.get("network_region") or "local",
        "PULSEGUARD_RUNNER_ID": runner.get("runner_id"),
        "PULSEGUARD_WORKER_TOKEN": runner.get("worker_token"),
        "PULSEGUARD_RELAY_URL": relay_status.get("public_url"),
        "PULSEGUARD_RELAY_FINGERPRINT": relay_status.get("server_fingerprint"),
        "PULSEGUARD_RELAY_TOKEN": runner.get("relay_token"),
        "PULSEGUARD_RELAY_TOKEN_VERSION": runner.get("relay_token_version") or 1,
        "COMPOSE_PROJECT_NAME": "pulseguard-worker",
        "COMPOSE_PROFILES": "updater",
        "PULSEGUARD_WORKER_UPDATER_URL": "http://pulseguard-worker-updater:8790",
    }
    if target_platform == "powershell":
        lines = [
            "mkdir pulseguard-worker -Force; cd pulseguard-worker",
            f"Invoke-WebRequest -UseBasicParsing {_ps_quote(compose_url)} -OutFile docker-compose.relay-worker.yml",
            *[f"$env:{key} = {_ps_quote(value)}" for key, value in env.items()],
            "docker compose -f docker-compose.relay-worker.yml pull",
            "docker compose -f docker-compose.relay-worker.yml up -d",
            "docker update --restart unless-stopped pulseguard-worker pulseguard-relay-client pulseguard-worker-updater",
            "docker logs --tail 80 pulseguard-relay-client",
        ]
        return "\n".join(lines)
    lines = [
        "mkdir -p pulseguard-worker && cd pulseguard-worker",
        f"curl -fsSL {_shell_quote(compose_url)} -o docker-compose.relay-worker.yml",
        *[f"export {key}={_shell_quote(value)}" for key, value in env.items()],
        "docker compose -f docker-compose.relay-worker.yml pull",
        "docker compose -f docker-compose.relay-worker.yml up -d",
        "docker update --restart unless-stopped pulseguard-worker pulseguard-relay-client pulseguard-worker-updater",
        "docker logs --tail 80 pulseguard-relay-client",
    ]
    return "\n".join(lines)


async def _fetch_runner_health(runner: dict[str, Any]) -> dict[str, Any]:
    payload = await _worker_request(runner, "GET", "/api/worker/health", timeout=5)
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Runner health response is invalid")
    return payload


async def _sync_enabled_browser_type_installation(settings: dict[str, Any]) -> None:
    browser_types = enabled_browser_types(settings)
    try:
        schedule_browser_install(browser_types)
    except Exception:
        pass
    runners = [
        runner
        for runner in storage.list_enabled_probe_runners()
        if str(runner.get("role") or "") == "child" and runner.get("available")
    ]
    for runner in runners:
        try:
            missing = _missing_enabled_browser_types(runner, settings) or browser_types
            await _worker_request(runner, "POST", "/api/worker/browser-types/install", {"browser_types": missing}, timeout=10)
        except Exception:
            continue


def _missing_enabled_browser_types(source: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    installed = set(normalize_browser_types(source.get("installed_browser_types"), default=[], allow_empty=True))
    return [browser_type for browser_type in enabled_browser_types(settings) if browser_type not in installed]


async def _install_missing_enabled_browser_types_on_runner(runner: dict[str, Any], health: dict[str, Any]) -> None:
    missing = _missing_enabled_browser_types(health, storage.get_settings())
    if not missing:
        return
    await _worker_request(runner, "POST", "/api/worker/browser-types/install", {"browser_types": missing}, timeout=10)


async def _worker_request(
    runner: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    runner_id = str(runner.get("runner_id") or "")
    try:
        storage.ensure_probe_runner_transport_allowed(runner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = storage.get_probe_runner_token(runner_id)
    address = str(runner.get("address") or "").strip().rstrip("/")
    if not address or not token:
        raise RuntimeError("Runner address or token is not configured")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                f"{address}{path}",
                json=payload if method.upper() != "GET" else None,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=400, detail=f"Runner 请求超时：{int(timeout)} 秒内未返回") from exc
    except httpx.RequestError as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise HTTPException(status_code=400, detail=f"Runner 请求失败：{detail}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=400, detail=f"Runner 请求失败：HTTP {response.status_code}: {_http_response_detail(response)}")
    data = response.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Runner 响应格式无效")
    return data


def _worker_update_status_for_health() -> dict[str, Any]:
    if not WORKER_UPDATER_URL or not WORKER_TOKEN:
        return {}
    try:
        with httpx.Client(timeout=1.5) as client:
            response = client.get(f"{WORKER_UPDATER_URL}/status", headers={"Authorization": f"Bearer {WORKER_TOKEN}"})
            response.raise_for_status()
            data = response.json()
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    update = data.get("update")
    return update if isinstance(update, dict) else {}


async def _worker_updater_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not WORKER_UPDATER_URL:
        raise HTTPException(status_code=503, detail="子节点未启用 updater，不能由平台推送更新")
    if not WORKER_TOKEN:
        raise HTTPException(status_code=503, detail="子节点 token 不可用，不能调用 updater")
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.request(
                method,
                f"{WORKER_UPDATER_URL}{path}",
                json=payload if method.upper() != "GET" else None,
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=503, detail="updater 请求超时") from exc
    except httpx.RequestError as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise HTTPException(status_code=503, detail=f"updater 不可用：{detail}") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code if response.status_code < 500 else 503, detail=_http_response_detail(response))
    data = response.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=503, detail="updater 响应格式无效")
    return data


def _http_response_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
    else:
        if isinstance(data, dict):
            text = str(data.get("detail") or data.get("message") or "").strip()
        else:
            text = str(data).strip()
    return text[:500] if text else "远程服务返回错误"


async def _runner_health_poll_loop() -> None:
    while True:
        try:
            runners = [
                runner
                for runner in storage.list_enabled_probe_runners()
                if str(runner.get("role") or "") == "child"
            ]
            for runner in runners:
                runner_id = str(runner.get("runner_id") or "")
                if not runner_id:
                    continue
                try:
                    health = await _fetch_runner_health(runner)
                    storage.mark_probe_runner_available(runner_id, health)
                    await _install_missing_enabled_browser_types_on_runner(runner, health)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    storage.mark_probe_runner_unavailable(runner_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(RUNNER_HEALTH_POLL_SECONDS)


def _log_worker_startup_info() -> None:
    if os.getenv("PULSEGUARD_WORKER_INFO_PRINTED") == "1":
        return
    print("", flush=True)
    print("PulseGuard worker node is ready.", flush=True)
    print(f"  name: {WORKER_NAME}", flush=True)
    print(f"  address: {WORKER_ADDRESS}", flush=True)
    print(f"  region: {WORKER_REGION}", flush=True)
    print(f"  token: {WORKER_TOKEN if WORKER_PRINT_TOKEN else '<hidden>'}", flush=True)
    print(f"  token_source: {WORKER_TOKEN_SOURCE}", flush=True)
    if WORKER_PRINT_TOKEN:
        print("Add this child node manually in the main console with the address and token above.", flush=True)
    else:
        print("Worker token is configured; token logging is disabled for this deployment.", flush=True)
    print("", flush=True)


def _read_only_overview_payload(overview: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    return {
        "ui_count": int(overview.get("ui_count") or 0),
        "api_count": int(overview.get("api_count") or 0),
        "failing_count": int(overview.get("failing_count") or 0),
        "today_runs": int(overview.get("today_runs") or 0),
        "latest_run": _read_only_run_payload(overview.get("latest_run"), settings) if overview.get("latest_run") else None,
        "latest_recovered": mask_data(overview.get("latest_recovered"), settings) if overview.get("latest_recovered") else None,
        "recent_failures": [
            _read_only_run_payload(run, settings)
            for run in overview.get("recent_failures") or []
            if isinstance(run, dict)
        ],
        "trends": overview.get("trends") if isinstance(overview.get("trends"), list) else [],
    }


def _read_only_check_payload(check: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    status = str(check.get("current_status") or ("disabled" if not check.get("enabled") else "never"))
    return {
        "id": check.get("id"),
        "name": mask_text(str(check.get("name") or ""), settings),
        "type": check.get("type"),
        "enabled": bool(check.get("enabled")),
        "tags": mask_text(str(check.get("tags") or ""), settings),
        "entry_url": mask_text(str(check.get("entry_url") or ""), settings),
        "method": check.get("method") or "",
        "interval_seconds": check.get("interval_seconds"),
        "timeout_ms": check.get("timeout_ms"),
        "status": status,
        "last_run_at": check.get("last_run_at"),
        "last_error": _public_failure_summary(status),
    }


def _read_only_run_payload(run: dict[str, Any] | None, settings: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(run, dict):
        return None
    status = str(run.get("status") or "")
    failure_kind = str(run.get("failure_kind") or "none")
    return {
        "id": run.get("id"),
        "check_id": run.get("check_id"),
        "check_name": mask_text(str(run.get("check_name") or ""), settings),
        "check_type": run.get("check_type"),
        "status": status,
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_ms": run.get("duration_ms"),
        "consecutive_failures": int(run.get("consecutive_failures") or 0),
        "failure_kind": failure_kind,
        "runner_name": mask_text(str(run.get("runner_name") or ""), settings),
        "runner_region": mask_text(str(run.get("runner_region") or ""), settings),
        "error_message": _public_failure_summary(status, failure_kind),
    }


def _status_page_payload() -> dict[str, Any]:
    settings = storage.get_settings()
    overview = storage.get_overview()
    checks = [_status_check_payload(check, settings) for check in storage.list_checks(refresh_stale=False)]
    incidents = [
        _status_incident_payload(run, settings)
        for run in storage.list_recent_business_incidents(limit=20)
    ]
    return {
        "generated_at": storage.now_iso(),
        "summary": {
            "checks_total": len(checks),
            "checks_enabled": sum(1 for check in checks if check["enabled"]),
            "checks_failing": int(overview.get("failing_count") or 0),
            "runs_today": int(overview.get("today_runs") or 0),
        },
        "maintenance": {
            "enabled": bool(settings.get("maintenance_enabled")),
            "title": mask_text(str(settings.get("maintenance_title") or ""), settings),
            "message": mask_text(str(settings.get("maintenance_message") or ""), settings),
            "starts_at": str(settings.get("maintenance_starts_at") or ""),
            "ends_at": str(settings.get("maintenance_ends_at") or ""),
        },
        "checks": checks,
        "recent_incidents": incidents,
    }


def _status_check_payload(check: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    status = str(check.get("current_status") or ("disabled" if not check.get("enabled") else "never"))
    return {
        "id": check.get("id"),
        "name": mask_text(str(check.get("name") or ""), settings),
        "type": check.get("type"),
        "enabled": bool(check.get("enabled")),
        "tags": mask_text(str(check.get("tags") or ""), settings),
        "status": status,
        "last_run_at": check.get("last_run_at"),
        "last_error": _public_failure_summary(status),
    }


def _status_incident_payload(run: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    status = str(run.get("status") or "")
    failure_kind = str(run.get("failure_kind") or "none")
    return {
        "id": run.get("id"),
        "check_id": run.get("check_id"),
        "check_name": mask_text(str(run.get("check_name") or ""), settings),
        "check_type": run.get("check_type"),
        "status": status,
        "started_at": run.get("started_at"),
        "duration_ms": run.get("duration_ms"),
        "failure_kind": failure_kind,
        "error_message": _public_failure_summary(status, failure_kind),
    }


def _public_failure_summary(status: str, failure_kind: str = "") -> str:
    if status == "failing":
        return "当前监控故障"
    if status == "suspected_failing":
        return "疑似故障，故障确认中"
    if status == "suspected_recovery":
        return "疑似恢复，恢复确认中"
    if status == "unknown":
        return "暂无有效观测"
    if status == "stale":
        return "观测已过期"
    if status == "timeout":
        return "最近运行超时"
    if failure_kind == "runner":
        return "Runner 执行异常"
    if status == "failed":
        return "最近运行失败"
    if status == "skipped":
        return "本次运行已跳过"
    return ""


def _run_failure_summary_payload(run: dict[str, Any], baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = storage.get_settings()
    return failure_summary.build_failure_summary(run, settings, baseline)


def _metrics_payload() -> dict[str, Any]:
    overview = storage.get_overview()
    checks = storage.list_checks(refresh_stale=False)
    runs_24h = next((item for item in overview.get("trends", []) if item.get("key") == "24h"), {})
    trend_totals_24h = _trend_totals(runs_24h)
    return {
        "checks_total": len(checks),
        "checks_enabled": sum(1 for check in checks if check.get("enabled")),
        "checks_disabled": sum(1 for check in checks if not check.get("enabled")),
        "checks_failing": int(overview.get("failing_count") or 0),
        "runs_today": int(overview.get("today_runs") or 0),
        "runs_24h": trend_totals_24h["runs"],
        "failures_24h": trend_totals_24h["failure_count"],
        "success_rate_24h": trend_totals_24h["success_rate"],
    }


def _trend_totals(trend: dict[str, Any]) -> dict[str, Any]:
    series = trend.get("series") if isinstance(trend, dict) else None
    rows = series if isinstance(series, list) else []
    runs = sum(int(row.get("runs") or 0) for row in rows if isinstance(row, dict))
    successes = sum(int(row.get("success_count") or 0) for row in rows if isinstance(row, dict))
    failures = sum(int(row.get("failure_count") or 0) for row in rows if isinstance(row, dict))
    return {
        "runs": runs,
        "failure_count": failures,
        "success_rate": round((successes / runs) * 100, 1) if runs else None,
    }


def _prometheus_metrics(metrics: dict[str, Any]) -> str:
    lines = [
        "# HELP pulseguard_checks_total Total configured checks.",
        "# TYPE pulseguard_checks_total gauge",
        f"pulseguard_checks_total {metrics['checks_total']}",
        "# HELP pulseguard_checks_enabled Enabled configured checks.",
        "# TYPE pulseguard_checks_enabled gauge",
        f"pulseguard_checks_enabled {metrics['checks_enabled']}",
        "# HELP pulseguard_checks_failing Enabled checks currently failing.",
        "# TYPE pulseguard_checks_failing gauge",
        f"pulseguard_checks_failing {metrics['checks_failing']}",
        "# HELP pulseguard_runs_today Runs started since local midnight.",
        "# TYPE pulseguard_runs_today counter",
        f"pulseguard_runs_today {metrics['runs_today']}",
        "# HELP pulseguard_failures_24h Failed or timed out runs in the last 24 hours.",
        "# TYPE pulseguard_failures_24h gauge",
        f"pulseguard_failures_24h {metrics['failures_24h']}",
    ]
    if metrics.get("success_rate_24h") is not None:
        lines.extend(
            [
                "# HELP pulseguard_success_rate_24h Success rate percentage in the last 24 hours.",
                "# TYPE pulseguard_success_rate_24h gauge",
                f"pulseguard_success_rate_24h {metrics['success_rate_24h']}",
            ]
        )
    return "\n".join(lines) + "\n"


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _format_validation_errors(exc.errors())})


@app.exception_handler(ValidationError)
async def validation_exception_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": _format_validation_errors(exc.errors())})


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_operational_error_handler(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    message = str(exc).lower()
    asyncio.create_task(
        notifier.notify_system_error(
            {
                "source": "sqlite",
                "severity": "error",
                "message": str(exc) or exc.__class__.__name__,
            }
        )
    )
    if "locked" in message:
        detail = "数据库暂时被占用，请稍后重试"
    elif "unable to open" in message:
        detail = "数据库文件无法打开，请检查数据目录权限"
    else:
        detail = "数据库操作失败"
    return JSONResponse(status_code=503, content={"detail": detail})


@app.get("/")
def frontend_index():
    return _serve_frontend()


@app.get("/{full_path:path}")
def frontend_spa(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("artifacts/"):
        raise HTTPException(status_code=404, detail="资源不存在")
    static_file = _serve_frontend_static_file(full_path)
    if static_file is not None:
        return static_file
    return _serve_frontend()


def _serve_frontend_static_file(full_path: str):
    static_root = Path(STATIC_DIR).resolve()
    candidate = (static_root / full_path).resolve()
    if candidate == static_root / "index.html":
        return None
    if not candidate.is_relative_to(static_root):
        raise HTTPException(status_code=404, detail="资源不存在")
    if candidate.is_file():
        return FileResponse(candidate)
    return None


def _serve_frontend():
    index = Path(STATIC_DIR) / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache, must-revalidate"})
    return JSONResponse(
        {
            "message": "PulseGuard backend is running. Frontend build was not found.",
            "frontend_dist": str(STATIC_DIR),
            "reports_dir": str(REPORTS_DIR),
        }
    )


def _normalize_heartbeat_key(key: str) -> str:
    heartbeat_key = key.strip()
    if not HEARTBEAT_KEY_PATTERN.fullmatch(heartbeat_key):
        raise HTTPException(status_code=400, detail="心跳 key 只能包含字母、数字、点、冒号、下划线或短横线，长度不超过 120")
    return heartbeat_key


def _format_validation_errors(errors: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for error in errors:
        label = _validation_field_label(error.get("loc"))
        text = _validation_error_text(error)
        if label and text.startswith(label):
            messages.append(text)
        elif label and text.startswith(("不能为空", "过长", "过短", "必须", "不能", "格式", "低于", "超过", "选项")):
            messages.append(f"{label}{text}")
        elif label:
            messages.append(f"{label}：{text}")
        else:
            messages.append(text)
    return "；".join(dict.fromkeys(messages)) or "请求参数格式不正确"


def _validation_field_label(loc: Any) -> str:
    if not isinstance(loc, (list, tuple)):
        return ""
    keys = [str(item) for item in loc if str(item) not in {"body", "query", "path"} and not str(item).isdigit()]
    if not keys:
        return ""
    return VALIDATION_FIELD_LABELS.get(keys[-1], "")


def _validation_error_text(error: dict[str, Any]) -> str:
    error_type = str(error.get("type") or "")
    message = str(error.get("msg") or "").removeprefix("Value error, ").strip()

    if error_type == "missing":
        return "不能为空"
    if error_type in {"string_too_short", "too_short"}:
        return "不能为空"
    if error_type in {"string_too_long", "too_long"}:
        return "过长"
    if error_type in {"int_parsing", "int_type", "float_parsing", "float_type"}:
        return "必须是数字"
    if error_type in {"bool_type", "bool_parsing"}:
        return "必须是布尔值"
    if error_type in {"literal_error", "enum"}:
        return "选项无效"
    if error_type in {"greater_than_equal", "greater_than"}:
        return "低于允许范围"
    if error_type in {"less_than_equal", "less_than"}:
        return "超过允许范围"
    return message or "格式不正确"
