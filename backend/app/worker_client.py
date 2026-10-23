"""Authenticated worker requests shared by administration and dispatch recovery."""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException

from . import storage
from .async_storage import call as storage_call


def http_response_detail(response: httpx.Response) -> str:
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


async def worker_request(
    runner: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    runner_id = str(runner.get("runner_id") or "")
    try:
        (await storage_call(storage.ensure_probe_runner_transport_allowed, runner))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = (await storage_call(storage.get_probe_runner_token, runner_id))
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
        raise HTTPException(status_code=400, detail=f"Runner 请求失败：HTTP {response.status_code}: {http_response_detail(response)}")
    data = response.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Runner 响应格式无效")
    return data


async def fetch_runner_health(runner: dict[str, Any]) -> dict[str, Any]:
    payload = await worker_request(runner, "GET", "/api/worker/health", timeout=5)
    if not payload.get("ok"):
        raise RuntimeError("Runner health response is invalid")
    return payload
