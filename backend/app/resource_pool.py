from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from .browser_types import browser_pool_size_for, prewarmed_browser_types, settings_for_browser_type
from .variables import resolve_text
from .viewport import browser_context_options, browser_viewport


class ResourcePoolError(Exception):
    """Expected failure while preparing shared runner resources."""


@dataclass(frozen=True)
class BrowserLaunchConfig:
    browser_type: str
    headless: bool
    proxy_url: str

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "BrowserLaunchConfig":
        return cls(
            browser_type=str(settings.get("browser_type") or "chromium"),
            headless=bool(settings.get("browser_headless", True)),
            proxy_url=resolve_text(settings.get("browser_proxy") or "", settings).strip(),
        )


@dataclass(eq=False)
class BrowserLease:
    browser: Any
    version: str
    config: BrowserLaunchConfig


@dataclass(eq=False)
class BrowserContextLease:
    browser: Any
    context: Any
    version: str
    config: BrowserLaunchConfig
    context_options: dict[str, Any]


class HttpClientPool:
    def __init__(self, size: int) -> None:
        self._target_size = max(1, int(size))
        self._clients: set[httpx.AsyncClient] = set()
        self._available: asyncio.Queue[httpx.AsyncClient] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._closing = False

    async def start(self) -> None:
        async with self._lock:
            self._closing = False
            await self._fill_locked()

    async def resize(self, size: int) -> None:
        to_close: list[httpx.AsyncClient] = []
        async with self._lock:
            self._target_size = max(1, int(size))
            self._closing = False
            await self._fill_locked()
            to_close.extend(self._drain_idle_locked(remove_all=False))
        await _close_http_clients(to_close)

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        await self.start()
        client = await self._available.get()
        close_client = False
        try:
            yield client
        finally:
            async with self._lock:
                close_client = self._closing or client.is_closed or len(self._clients) > self._target_size
                if close_client:
                    self._clients.discard(client)
                else:
                    self._available.put_nowait(client)
            if close_client:
                await client.aclose()
            if not self._closing:
                await self.start()

    async def shutdown(self) -> None:
        async with self._lock:
            self._closing = True
            to_close = self._drain_idle_locked(remove_all=True)
        await _close_http_clients(to_close)

    def snapshot(self) -> dict[str, int]:
        ready = self._available.qsize()
        total = len(self._clients)
        return {
            "limit": self._target_size,
            "ready": ready,
            "in_use": max(0, total - ready),
            "total": total,
        }

    async def _fill_locked(self) -> None:
        while not self._closing and len(self._clients) < self._target_size:
            client = httpx.AsyncClient(follow_redirects=True)
            self._clients.add(client)
            self._available.put_nowait(client)

    def _drain_idle_locked(self, *, remove_all: bool) -> list[httpx.AsyncClient]:
        to_close: list[httpx.AsyncClient] = []
        while not self._available.empty() and (remove_all or len(self._clients) > self._target_size):
            client = self._available.get_nowait()
            self._clients.discard(client)
            to_close.append(client)
        return to_close


class BrowserPool:
    def __init__(self, size: int) -> None:
        self._target_size = max(1, int(size))
        self._recycle_after_runs = 20
        self._completed_contexts_since_launch = 0
        self._browser_lease: BrowserLease | None = None
        self._ready_contexts: list[BrowserContextLease] = []
        self._active_contexts: set[BrowserContextLease] = set()
        self._condition = asyncio.Condition()
        self._closing = False
        self._config: BrowserLaunchConfig | None = None
        self._default_context_options: dict[str, Any] = {}
        self._playwright: Any = None
        self._retire_browser_when_idle = False
        self._last_error = ""

    async def start(self, settings: dict[str, Any]) -> None:
        await self.resize(self._target_size, settings)

    async def resize(self, size: int, settings: dict[str, Any]) -> None:
        config = self._config_from_settings(settings)
        default_context_options = default_context_options_from_settings(settings)
        async with self._condition:
            self._target_size = max(1, int(size))
            self._recycle_after_runs = _recycle_after_runs_from_settings(settings)
            self._closing = False
            self._default_context_options = default_context_options
            if self._config != config:
                self._config = config
                if self._browser_lease is not None:
                    self._retire_browser_when_idle = True
            await self._reconcile_locked()
            self._condition.notify_all()

    async def acquire(self, settings: dict[str, Any]) -> BrowserLease:
        config = self._config_from_settings(settings)
        default_context_options = default_context_options_from_settings(settings)
        async with self._condition:
            self._closing = False
            self._recycle_after_runs = _recycle_after_runs_from_settings(settings)
            self._default_context_options = default_context_options
            if self._config != config:
                self._config = config
                if self._browser_lease is not None:
                    self._retire_browser_when_idle = True
            while True:
                await self._reconcile_locked()
                if self._browser_lease is not None and not self._retire_browser_when_idle:
                    return self._browser_lease
                await self._condition.wait()

    async def acquire_context(self, settings: dict[str, Any], context_options: dict[str, Any]) -> BrowserContextLease:
        config = self._config_from_settings(settings)
        default_context_options = default_context_options_from_settings(settings)
        requested_options = dict(context_options)
        async with self._condition:
            self._closing = False
            self._recycle_after_runs = _recycle_after_runs_from_settings(settings)
            self._default_context_options = default_context_options
            if self._config != config:
                self._config = config
                if self._browser_lease is not None:
                    self._retire_browser_when_idle = True
            while True:
                await self._reconcile_locked()
                if self._browser_lease is None or self._retire_browser_when_idle:
                    await self._condition.wait()
                    continue
                lease = self._pop_ready_context_locked(requested_options)
                if lease is None:
                    if self._context_count_locked() >= self._target_size:
                        if self._ready_contexts:
                            await self._close_context(self._ready_contexts.pop(0))
                        else:
                            await self._condition.wait()
                            continue
                    lease = await self._new_context_locked(requested_options)
                self._active_contexts.add(lease)
                self._condition.notify_all()
                return lease

    async def release(self, lease: BrowserLease, *, healthy: bool = True) -> None:
        async with self._condition:
            if lease is self._browser_lease and (not healthy or not self._browser_connected(lease.browser)):
                self._retire_browser_when_idle = True
            try:
                await self._reconcile_locked()
            except ResourcePoolError:
                pass
            self._condition.notify_all()
        await self._stop_playwright_if_idle()

    async def release_context(self, lease: BrowserContextLease, *, healthy: bool = True) -> None:
        async with self._condition:
            self._active_contexts.discard(lease)
            released_current_browser = self._browser_lease is not None and lease.browser is self._browser_lease.browser
            await self._close_context(lease)
            if not healthy:
                self._retire_browser_when_idle = True
            elif released_current_browser:
                self._completed_contexts_since_launch += 1
                if self._completed_contexts_since_launch >= self._recycle_after_runs:
                    self._retire_browser_when_idle = True
            try:
                if healthy and not self._closing and not self._retire_browser_when_idle and self._browser_lease is not None:
                    await self._fill_contexts_locked(lease.context_options)
                await self._reconcile_locked()
            except ResourcePoolError:
                pass
            self._condition.notify_all()
        await self._stop_playwright_if_idle()

    async def shutdown(self) -> None:
        async with self._condition:
            self._closing = True
            await self._reconcile_locked()
            self._condition.notify_all()
        await self._stop_playwright_if_idle()

    def snapshot(self) -> dict[str, Any]:
        ready = len(self._ready_contexts)
        active = len(self._active_contexts)
        return {
            "limit": self._target_size,
            "ready": ready,
            "in_use": active,
            "total": ready + active,
            "browser_processes": int(self._browser_lease is not None and self._browser_connected(self._browser_lease.browser)),
            "completed_contexts": self._completed_contexts_since_launch,
            "recycle_after_runs": self._recycle_after_runs,
            "retiring": self._retire_browser_when_idle,
            "error": self._last_error,
        }

    async def _reconcile_locked(self) -> None:
        if self._browser_lease is not None and not self._browser_connected(self._browser_lease.browser):
            self._retire_browser_when_idle = True

        if self._closing:
            await self._close_ready_contexts_locked()
            if not self._active_contexts:
                await self._close_browser_locked()
            return

        if self._retire_browser_when_idle:
            await self._close_ready_contexts_locked()
            if self._active_contexts:
                return
            await self._close_browser_locked()
            self._retire_browser_when_idle = False

        if self._browser_lease is None:
            if self._config is None:
                return
            self._browser_lease = await self._launch_locked(self._config)
            self._completed_contexts_since_launch = 0

        await self._trim_ready_contexts_locked()
        await self._fill_contexts_locked(self._default_context_options)

    async def _fill_contexts_locked(self, context_options: dict[str, Any]) -> None:
        while not self._closing and self._browser_lease is not None and self._context_count_locked() < self._target_size:
            self._ready_contexts.append(await self._new_context_locked(context_options))
        self._last_error = ""

    async def _new_context_locked(self, context_options: dict[str, Any]) -> BrowserContextLease:
        if self._browser_lease is None:
            raise ResourcePoolError("浏览器池没有可用实例")
        options = dict(context_options)
        try:
            context = await self._browser_lease.browser.new_context(**options)
        except Exception as exc:
            self._last_error = f"浏览器 Context 创建失败：{exc}"
            raise ResourcePoolError(self._last_error) from exc
        return BrowserContextLease(
            browser=self._browser_lease.browser,
            context=context,
            version=self._browser_lease.version,
            config=self._browser_lease.config,
            context_options=options,
        )

    async def _launch_locked(self, config: BrowserLaunchConfig) -> BrowserLease:
        try:
            playwright = await self._ensure_playwright_locked()
            browser_type = getattr(playwright, config.browser_type, None)
            if browser_type is None:
                raise ResourcePoolError(f"不支持的浏览器类型：{config.browser_type}")
            launch_options: dict[str, Any] = {"headless": config.headless}
            if config.proxy_url:
                launch_options["proxy"] = {"server": config.proxy_url}
            browser = await browser_type.launch(**launch_options)
            raw_version = getattr(browser, "version", "")
            if callable(raw_version):
                raw_version = raw_version()
            version = " ".join(part for part in (config.browser_type, str(raw_version or "").strip()) if part)
            return BrowserLease(browser=browser, version=version, config=config)
        except ResourcePoolError as exc:
            self._last_error = str(exc)
            raise
        except ImportError as exc:
            self._last_error = "Playwright 未安装，请先安装后再执行 UI 探活"
            raise ResourcePoolError(self._last_error) from exc
        except Exception as exc:
            self._last_error = f"浏览器环境启动失败：{exc}"
            raise ResourcePoolError(self._last_error) from exc

    async def _ensure_playwright_locked(self) -> Any:
        if self._playwright is None:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
        return self._playwright

    def _pop_ready_context_locked(self, context_options: dict[str, Any]) -> BrowserContextLease | None:
        key = context_options_key(context_options)
        for index, lease in enumerate(self._ready_contexts):
            if context_options_key(lease.context_options) == key:
                return self._ready_contexts.pop(index)
        return None

    def _context_count_locked(self) -> int:
        return len(self._ready_contexts) + len(self._active_contexts)

    async def _trim_ready_contexts_locked(self) -> None:
        while self._ready_contexts and self._context_count_locked() > self._target_size:
            await self._close_context(self._ready_contexts.pop())

    async def _close_ready_contexts_locked(self) -> None:
        while self._ready_contexts:
            await self._close_context(self._ready_contexts.pop())

    async def _close_browser_locked(self) -> None:
        await self._close_ready_contexts_locked()
        if self._browser_lease is not None:
            try:
                await self._browser_lease.browser.close()
            except Exception:
                pass
            finally:
                self._browser_lease = None
                self._completed_contexts_since_launch = 0

    async def _close_context(self, lease: BrowserContextLease) -> None:
        try:
            await lease.context.close()
        except Exception:
            pass

    async def _stop_playwright_if_idle(self) -> None:
        playwright = None
        async with self._condition:
            if self._closing and self._browser_lease is None and not self._active_contexts and self._playwright is not None:
                playwright = self._playwright
                self._playwright = None
                self._config = None
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass

    @staticmethod
    def _browser_connected(browser: Any) -> bool:
        is_connected = getattr(browser, "is_connected", None)
        return bool(is_connected()) if callable(is_connected) else True

    def _config_from_settings(self, settings: dict[str, Any]) -> BrowserLaunchConfig:
        try:
            return BrowserLaunchConfig.from_settings(settings)
        except Exception as exc:
            self._last_error = f"浏览器环境配置无效：{exc}"
            raise ResourcePoolError(self._last_error) from exc


class ProbeResourcePool:
    def __init__(self, api_pool_size: int, browser_pool_sizes_value: dict[str, int] | int) -> None:
        self.http_clients = HttpClientPool(api_pool_size)
        self.browsers: dict[str, BrowserPool] = {}
        self._browser_pool_sizes = _coerce_pool_sizes(browser_pool_sizes_value)

    async def start(self, settings: dict[str, Any], *, api_pool_size: int, browser_pool_sizes_value: dict[str, int] | int) -> None:
        await self.http_clients.resize(api_pool_size)
        self._browser_pool_sizes = _coerce_pool_sizes(browser_pool_sizes_value)
        await self._resize_browser_pools(settings)

    async def reload(self, settings: dict[str, Any], *, api_pool_size: int, browser_pool_sizes_value: dict[str, int] | int) -> None:
        await self.start(settings, api_pool_size=api_pool_size, browser_pool_sizes_value=browser_pool_sizes_value)

    async def shutdown(self) -> None:
        await self.http_clients.shutdown()
        pools = list(self.browsers.values())
        for pool in pools:
            await pool.shutdown()

    @asynccontextmanager
    async def http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with self.http_clients.client() as client:
            yield client

    async def acquire_browser(self, settings: dict[str, Any]) -> BrowserLease:
        browser_type = str(settings.get("browser_type") or "chromium")
        pool = self._browser_pool(browser_type)
        return await pool.acquire(settings_for_browser_type(settings, browser_type))

    async def release_browser(self, lease: BrowserLease, *, healthy: bool = True) -> None:
        pool = self.browsers.get(lease.config.browser_type)
        if pool is not None:
            await pool.release(lease, healthy=healthy)

    async def acquire_browser_context(self, settings: dict[str, Any], context_options: dict[str, Any]) -> BrowserContextLease:
        browser_type = str(settings.get("browser_type") or "chromium")
        pool = self._browser_pool(browser_type)
        return await pool.acquire_context(settings_for_browser_type(settings, browser_type), context_options)

    async def release_browser_context(self, lease: BrowserContextLease, *, healthy: bool = True) -> None:
        pool = self.browsers.get(lease.config.browser_type)
        if pool is not None:
            await pool.release_context(lease, healthy=healthy)

    def snapshot(self) -> dict[str, Any]:
        return {
            "api": self.http_clients.snapshot(),
            "browser": {browser_type: pool.snapshot() for browser_type, pool in sorted(self.browsers.items())},
        }

    async def _resize_browser_pools(self, settings: dict[str, Any]) -> None:
        wanted = set(prewarmed_browser_types(settings))
        for browser_type in wanted:
            pool = self._browser_pool(browser_type)
            try:
                await pool.resize(browser_pool_size_for(settings, browser_type), settings_for_browser_type(settings, browser_type))
            except ResourcePoolError:
                pass
        for browser_type in list(self.browsers):
            if browser_type in wanted:
                continue
            await self.browsers[browser_type].shutdown()

    def _browser_pool(self, browser_type: str) -> BrowserPool:
        pool = self.browsers.get(browser_type)
        if pool is None:
            pool = BrowserPool(self._browser_pool_sizes.get(browser_type, 5))
            self.browsers[browser_type] = pool
        return pool


async def _close_http_clients(clients: list[httpx.AsyncClient]) -> None:
    for client in clients:
        await client.aclose()


def default_context_options_from_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return browser_context_options("web", browser_viewport(settings))


def context_options_key(options: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted((str(key), _freeze_context_option(value)) for key, value in options.items()))


def _freeze_context_option(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze_context_option(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze_context_option(item) for item in value)
    return value


def _coerce_pool_sizes(value: dict[str, int] | int) -> dict[str, int]:
    if isinstance(value, dict):
        return {str(key): max(1, int(size)) for key, size in value.items()}
    size = max(1, int(value))
    return dict.fromkeys(("chromium", "firefox", "webkit"), size)


def _recycle_after_runs_from_settings(settings: dict[str, Any]) -> int:
    try:
        return max(1, int(settings.get("browser_recycle_after_runs", 20)))
    except (TypeError, ValueError):
        return 20
