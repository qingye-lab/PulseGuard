from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .browser_types import BROWSER_TYPES, browser_type_status, normalize_browser_types


_install_lock = asyncio.Lock()
_install_tasks: dict[str, asyncio.Task[None]] = {}
_install_status: dict[str, dict[str, Any]] = {}
_PROCESS_TERMINATE_TIMEOUT_SECONDS = 5.0


def installed_browser_types() -> list[str]:
    if _has_running_event_loop():
        return _installed_browser_types_subprocess()
    return _installed_browser_types_direct()


def _installed_browser_types_direct() -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    installed: list[str] = []
    try:
        with sync_playwright() as playwright:
            for browser_type in BROWSER_TYPES:
                candidate = getattr(playwright, browser_type, None)
                executable_path = getattr(candidate, "executable_path", "") if candidate is not None else ""
                if executable_path and Path(str(executable_path)).exists():
                    installed.append(browser_type)
    except Exception:
        return []
    return installed


def _installed_browser_types_subprocess() -> list[str]:
    script = """
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

installed = []
with sync_playwright() as playwright:
    for browser_type in ("chromium", "firefox", "webkit"):
        candidate = getattr(playwright, browser_type, None)
        executable_path = getattr(candidate, "executable_path", "") if candidate is not None else ""
        if executable_path and Path(str(executable_path)).exists():
            installed.append(browser_type)
print(json.dumps(installed))
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        parsed = json.loads(result.stdout or "[]")
    except Exception:
        return []
    return normalize_browser_types(parsed, default=[], allow_empty=True) if isinstance(parsed, list) else []


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def browser_capabilities(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    installed = installed_browser_types()
    status = browser_type_status(installed, settings or {})
    available = [browser_type for browser_type in BROWSER_TYPES if status[browser_type]["available"]]
    return {
        "installed_browser_types": installed,
        "available_browser_types": available,
        "browser_type_status": _merge_install_status(status),
    }


def schedule_browser_install(browser_types: list[str]) -> dict[str, Any]:
    requested = normalize_browser_types(browser_types, default=[], allow_empty=True)
    installed = set(installed_browser_types())
    scheduled: list[str] = []
    for browser_type in requested:
        if browser_type in installed:
            _install_status[browser_type] = {"status": "installed", "message": "", "updated_at": ""}
            continue
        status = _install_status.get(browser_type) or {}
        if status.get("status") == "installing":
            continue
        _install_status[browser_type] = {"status": "installing", "message": "", "updated_at": ""}
        _install_tasks[browser_type] = asyncio.create_task(_install_one(browser_type))
        scheduled.append(browser_type)
    return {
        "requested_browser_types": requested,
        "scheduled_browser_types": scheduled,
        "status": _merge_install_status(browser_type_status(installed_browser_types(), {})),
    }


async def ensure_browser_types_installed(browser_types: list[str]) -> dict[str, Any]:
    requested = normalize_browser_types(browser_types, default=[], allow_empty=True)
    installed = set(installed_browser_types())
    missing = [browser_type for browser_type in requested if browser_type not in installed]
    if not missing:
        return {"requested_browser_types": requested, "missing_browser_types": [], "scheduled": []}
    schedule_browser_install(missing)
    return {"requested_browser_types": requested, "missing_browser_types": missing, "scheduled": missing}


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    pid = process.pid
    if sys.platform == "win32":
        await _taskkill_process_tree(pid)
    elif os.name == "posix":
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        try:
            process.terminate()
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)
    except ProcessLookupError:
        return
    except asyncio.TimeoutError:
        if sys.platform == "win32":
            await _taskkill_process_tree(pid)
        elif os.name == "posix":
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            try:
                process.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ProcessLookupError):
            return


async def _taskkill_process_tree(pid: int) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        await asyncio.wait_for(killer.communicate(), timeout=_PROCESS_TERMINATE_TIMEOUT_SECONDS)
    except (FileNotFoundError, ProcessLookupError, asyncio.TimeoutError):
        return


def _installer_process_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": creationflags}
    return {"start_new_session": True}


async def _install_one(browser_type: str) -> None:
    process: asyncio.subprocess.Process | None = None
    try:
        async with _install_lock:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                browser_type,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_installer_process_kwargs(),
            )
            stdout, stderr = await process.communicate()
            output = "\n".join(
                part.decode("utf-8", errors="replace").strip()
                for part in (stdout, stderr)
                if part
            ).strip()
            if process.returncode == 0:
                _install_status[browser_type] = {"status": "installed", "message": output[-1000:], "updated_at": ""}
            else:
                _install_status[browser_type] = {"status": "failed", "message": output[-1000:], "updated_at": ""}
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_process(process)
        _install_status.pop(browser_type, None)
        raise
    except Exception as exc:
        if process is not None:
            await _terminate_process(process)
        _install_status[browser_type] = {"status": "failed", "message": str(exc), "updated_at": ""}


async def shutdown_browser_installation() -> None:
    """Cancel and drain local browser installers before the application stops."""
    tasks = list(_install_tasks.values())
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    for browser_type, task in list(_install_tasks.items()):
        if task.cancelled() or _install_status.get(browser_type, {}).get("status") == "installing":
            _install_status.pop(browser_type, None)
    _install_tasks.clear()


def _merge_install_status(status: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    merged = {browser_type: dict(payload) for browser_type, payload in status.items()}
    for browser_type, payload in _install_status.items():
        merged.setdefault(browser_type, {}).update(payload)
    for browser_type in BROWSER_TYPES:
        item = merged.setdefault(browser_type, {})
        if not item.get("status"):
            item["status"] = "installed" if item.get("installed") else "missing"
        item.setdefault("message", "")
    return merged
