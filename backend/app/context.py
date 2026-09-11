from __future__ import annotations

import asyncio
import json
from datetime import datetime
from time import perf_counter
from typing import Any

import httpx
from jsonschema import ValidationError, validate

from . import network_checks
from .artifacts import ArtifactStore
from .resource_pool import ProbeResourcePool, ResourcePoolError
from .script_modules import ModuleBundleError, ModuleRuntime, ScriptStaticError, compile_script_entry, normalize_module_bundle
from .variables import RuntimeVariables, mask_data, mask_text, resolve_data, resolve_text, resolve_url
from .viewport import browser_context_options, normalize_viewport_mode, viewport_for_mode


class RunFailure(Exception):
    """Expected failure raised by a user script or ctx assertion."""


class RunnerEnvironmentFailure(RunFailure):
    """Expected failure caused by local runner dependencies or browser setup."""


class PreparationFailure(RunFailure):
    """The target could not be checked because its prerequisites failed."""


def compile_check_function(
    script: str,
    ctx: "RunContext",
    function_name: str = "check",
    expected_signature: str = "async def check(ctx)",
    script_label: str = "脚本",
    *,
    module_bundle: dict[str, Any] | None = None,
    filename: str | None = None,
    attempt: int | None = None,
) -> Any:
    try:
        return compile_script_entry(
            script,
            ctx,
            function_name=function_name,
            expected_signature=expected_signature,
            script_label=script_label,
            module_bundle=module_bundle,
            modules=getattr(ctx, "modules", None),
            filename=filename,
            check_id=getattr(ctx, "original_check_id", None),
            attempt=attempt,
        )
    except ScriptStaticError as exc:
        ctx.compile_diagnostics = list(exc.diagnostics)
        raise RunFailure(str(exc)) from exc
    except ModuleBundleError as exc:
        raise RunFailure(str(exc)) from exc


class PulseHttpClient:
    def __init__(self, ctx: "RunContext", resources: ProbeResourcePool | None = None) -> None:
        self.ctx = ctx
        self._resources = resources
        self._client = None if resources is not None else httpx.AsyncClient(follow_redirects=True)
        self.last_response: httpx.Response | None = None

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # A failed next request must not appear to have the previous response.
        self.last_response = None
        self.ctx.response_snapshot = None
        self.ctx.last_request_duration_ms = None
        if self.ctx.check.get("type") == "api":
            self.ctx.api_stage = "request"
        self.ctx.request_snapshot = {"method": method.upper(), "url_template": url}
        exchange: dict[str, Any] = {"request": self.ctx.request_snapshot, "response": None}
        if self.ctx.check.get("type") in {"api", "ui"}:
            self.ctx.http_exchanges.append(exchange)
            del self.ctx.http_exchanges[:-10]
        started = perf_counter()
        resolved_url = str(url)
        resolved_kwargs: dict[str, Any] = {}
        request_url = resolved_url
        try:
            resolved_url = resolve_text(url, self.ctx.settings)
            resolved_kwargs = resolve_data(kwargs, self.ctx.settings)
            resolved_kwargs.setdefault("timeout", self.ctx.timeout_ms / 1000)
            request_url = self._request_url_with_params(method, resolved_url, resolved_kwargs)
            self.ctx.request_snapshot = mask_data(
                {
                    "method": method.upper(),
                    "url": request_url,
                    "headers": dict(resolved_kwargs.get("headers") or {}),
                    "body": self._snapshot_body(resolved_kwargs),
                },
                self.ctx.settings,
            )
            exchange["request"] = self.ctx.request_snapshot
            if self._resources is not None:
                async with self._resources.http_client() as client:
                    response = await client.request(method, resolved_url, **resolved_kwargs)
            elif self._client is not None:
                response = await self._client.request(method, resolved_url, **resolved_kwargs)
            else:
                raise RuntimeError("HTTP client 未初始化")
        except httpx.RequestError as exc:
            self._update_request_snapshot_url(getattr(exc, "request", None), request_url)
            duration_ms = max(0, int((perf_counter() - started) * 1000))
            self.ctx.last_request_duration_ms = duration_ms
            exchange["duration_ms"] = duration_ms
            exchange["error"] = mask_text(str(exc) or type(exc).__name__, self.ctx.settings)
            raise RunFailure(f"请求目标失败：{exc}") from exc
        except Exception as exc:
            self._update_request_snapshot_url(getattr(exc, "request", None), request_url)
            duration_ms = max(0, int((perf_counter() - started) * 1000))
            self.ctx.last_request_duration_ms = duration_ms
            exchange["duration_ms"] = duration_ms
            exchange["error"] = mask_text(str(exc) or type(exc).__name__, self.ctx.settings)
            raise
        except asyncio.CancelledError:
            self._update_request_snapshot_url(None, request_url)
            duration_ms = max(0, int((perf_counter() - started) * 1000))
            self.ctx.last_request_duration_ms = duration_ms
            exchange["duration_ms"] = duration_ms
            exchange["error"] = "请求已取消"
            raise
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        self._update_request_snapshot_url(getattr(response, "request", None), request_url)
        self.last_response = response
        self.ctx.last_request_duration_ms = duration_ms
        self.ctx.response_snapshot = self.ctx.response_snapshot_from(response)
        self.ctx.response_snapshot["duration_ms"] = duration_ms
        self.ctx.response_snapshot["timings"] = {"request_ms": duration_ms}
        exchange["duration_ms"] = duration_ms
        exchange["response"] = self.ctx.response_snapshot
        return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _update_request_snapshot_url(self, request: Any, fallback: str) -> None:
        try:
            request_url = getattr(request, "url", None) if request is not None else None
        except Exception:
            request_url = None
        actual_url = str(request_url) if request_url is not None else str(fallback)
        if not isinstance(self.ctx.request_snapshot, dict):
            self.ctx.request_snapshot = {"method": self.ctx.method.upper()}
        self.ctx.request_snapshot["url"] = mask_text(actual_url, self.ctx.settings)

    @staticmethod
    def _request_url_with_params(method: str, url: str, kwargs: dict[str, Any]) -> str:
        params = kwargs.get("params")
        if params is None:
            return url
        try:
            return str(httpx.Request(method, url, params=params).url)
        except Exception:
            return url

    @staticmethod
    def _snapshot_body(kwargs: dict[str, Any]) -> Any:
        if "json" in kwargs:
            return kwargs["json"]
        if "data" in kwargs:
            return kwargs["data"]
        if "content" in kwargs:
            content = kwargs["content"]
            if isinstance(content, bytes):
                return content.decode("utf-8", errors="replace")
            return content
        return None


class RunContext:
    def __init__(
        self,
        check: dict[str, Any],
        run_id: int,
        settings: dict[str, Any],
        artifacts: ArtifactStore,
        resources: ProbeResourcePool | None = None,
        module_bundle: dict[str, Any] | None = None,
        attempt: int | None = None,
    ) -> None:
        self.check = check
        self.run_id = run_id
        self.project_id = int(check.get("project_id") or 0) or None
        self.original_check_id = int(
            check.get("original_check_id") or check.get("_original_check_id") or check.get("id") or 0
        )
        self.vars = RuntimeVariables(settings)
        self.settings = self.vars.settings
        self.artifacts = artifacts
        self.resources = resources
        self.entry_url = check.get("entry_url") or ""
        self.viewport_mode = normalize_viewport_mode(check.get("viewport_mode"))
        self.viewport_width = self._optional_int(check.get("viewport_width"))
        self.viewport_height = self._optional_int(check.get("viewport_height"))
        self.method = (check.get("method") or "GET").upper()
        self._headers_template = check.get("headers_json") or "{}"
        self._body_template = check.get("body") or ""
        self.timeout_ms = int(check.get("timeout_ms") or 15000)
        self.logs: list[str] = []
        self.ui_stage = "prepare"
        self.api_stage = "prepare"
        self.http_exchanges: list[dict[str, Any]] = []
        self.console_messages: list[dict[str, str]] = []
        self.schema_errors: list[str] = []
        self.screenshot_path: str | None = None
        self.trace_path: str | None = None
        self.response_path: str | None = None
        self.request_snapshot: dict[str, Any] | None = None
        self.response_snapshot: dict[str, Any] | None = None
        self.last_request_duration_ms: int | None = None
        self.task_duration_ms: int | None = None
        self.attempt = attempt
        self.compile_diagnostics: list[dict[str, Any]] = []
        raw_module_bundle = module_bundle if module_bundle is not None else (check.get("_module_bundle") or check.get("module_bundle"))
        try:
            self.module_bundle = normalize_module_bundle(raw_module_bundle, expected_project_id=self.project_id)
            self.modules = ModuleRuntime(self.module_bundle, expected_project_id=self.project_id)
        except ModuleBundleError as exc:
            raise RunFailure(str(exc)) from exc
        self.library_metadata = self.modules.metadata()
        self.browser_version: str | None = None
        self.trace_artifacts_enabled = bool(settings.get("trace_artifacts_enabled", False))
        self.http = PulseHttpClient(self, resources)
        if str(check.get("type") or "") == "ui":
            self.request_snapshot = {
                "type": "ui",
                "url": self._entry_url_template,
                "timeout_ms": self.timeout_ms,
                "viewport_mode": self.viewport_mode,
            }

        self._playwright: Any = None
        self._browser: Any = None
        self._browser_lease: Any = None
        self._browser_context_lease: Any = None
        self._browser_context: Any = None
        self._trace_started = False
        self._pages: list[Any] = []

    @property
    def entry_url(self) -> str:
        if self.check.get("type") == "ui":
            return resolve_url(self._entry_url_template, self.settings)
        return resolve_text(self._entry_url_template, self.settings)

    @entry_url.setter
    def entry_url(self, value: str) -> None:
        self._entry_url_template = value

    async def request(self, **kwargs: Any) -> httpx.Response:
        request_kwargs = dict(kwargs)
        headers = {**self.headers, **dict(request_kwargs.pop("headers", {}) or {})}

        if not {"json", "data", "content"}.intersection(request_kwargs):
            request_kwargs.update(self._configured_body_kwargs())
        if headers:
            request_kwargs["headers"] = headers

        return await self.http.request(self.method, self.entry_url, **request_kwargs)

    @property
    def headers(self) -> dict[str, str]:
        return self._parse_headers(resolve_text(self._headers_template, self.settings))

    @property
    def body(self) -> str:
        return resolve_text(self._body_template, self.settings)

    async def tcp_connect(self, host: str, port: int | None = None, timeout_seconds: float | None = None) -> dict[str, Any]:
        resolved_host = resolve_text(host, self.settings)
        target_host, target_port = network_checks.normalize_host_port(resolved_host, port or 80)
        timeout = timeout_seconds or self.timeout_ms / 1000
        try:
            result = await asyncio.to_thread(network_checks.tcp_connect, target_host, target_port, timeout)
        except Exception as exc:
            raise RunFailure(f"TCP 连接失败：{target_host}:{target_port} {exc}") from exc
        self.log(f"TCP 可达：{target_host}:{target_port} {result['duration_ms']}ms")
        return result

    async def dns_resolve(self, host: str) -> dict[str, Any]:
        resolved_host = resolve_text(host, self.settings)
        target_host, _ = network_checks.normalize_host_port(resolved_host, 80)
        try:
            result = await asyncio.to_thread(network_checks.resolve_hostname, target_host)
        except Exception as exc:
            raise RunFailure(f"DNS 解析失败：{target_host} {exc}") from exc
        self.log(f"DNS 解析：{target_host} -> {', '.join(result['addresses'])}")
        return result

    async def tls_certificate(self, host: str, port: int = 443, warn_days: int = 14, timeout_seconds: float | None = None) -> dict[str, Any]:
        resolved_host = resolve_text(host, self.settings)
        target_host, target_port = network_checks.normalize_host_port(resolved_host, port)
        timeout = timeout_seconds or self.timeout_ms / 1000
        try:
            result = await asyncio.to_thread(network_checks.tls_certificate, target_host, target_port, timeout)
        except Exception as exc:
            raise RunFailure(f"TLS 证书检查失败：{target_host}:{target_port} {exc}") from exc
        if int(result["days_remaining"]) < int(warn_days):
            raise RunFailure(f"TLS 证书即将到期：剩余 {result['days_remaining']} 天，阈值 {warn_days} 天")
        self.log(f"TLS 证书有效：{target_host}:{target_port} 剩余 {result['days_remaining']} 天")
        return result

    def expect_heartbeat(self, key: str, max_age_seconds: int, require_ok: bool = True) -> dict[str, Any]:
        from . import storage

        heartbeat_key = resolve_text(key, self.settings).strip()
        heartbeat = storage.get_heartbeat(heartbeat_key)
        if not heartbeat:
            raise RunFailure(f"未收到心跳：{heartbeat_key}")
        received_at = datetime.fromisoformat(str(heartbeat["received_at"]))
        age_seconds = int((datetime.now(received_at.tzinfo) - received_at).total_seconds())
        if age_seconds > int(max_age_seconds):
            raise RunFailure(f"心跳已过期：{heartbeat_key}，{age_seconds}s 前收到")
        if require_ok and str(heartbeat.get("status") or "ok") != "ok":
            message = str(heartbeat.get("message") or "心跳状态异常")
            raise RunFailure(f"心跳失败：{heartbeat_key} {message}")
        self.log(f"心跳正常：{heartbeat_key}，{age_seconds}s 前收到")
        return heartbeat

    async def new_page(self) -> Any:
        if self._browser_context is None:
            try:
                if self.resources is not None:
                    self._browser_context_lease = await self.resources.acquire_browser_context(
                        self.settings,
                        self._browser_context_options(),
                    )
                    self._browser = self._browser_context_lease.browser
                    self._browser_context = self._browser_context_lease.context
                    self.browser_version = self._browser_context_lease.version
                else:
                    try:
                        from playwright.async_api import async_playwright
                    except ImportError as exc:
                        raise RunnerEnvironmentFailure("Playwright 未安装，请先安装后再执行 UI 探活") from exc

                    self._playwright = await async_playwright().start()
                    browser_type_name = str(self.settings.get("browser_type") or "chromium")
                    browser_type = getattr(self._playwright, browser_type_name, None)
                    if browser_type is None:
                        raise RunnerEnvironmentFailure(f"不支持的浏览器类型：{browser_type_name}")

                    proxy_url = resolve_text(self.settings.get("browser_proxy") or "", self.settings).strip()
                    launch_options: dict[str, Any] = {
                        "headless": bool(self.settings.get("browser_headless", True)),
                    }
                    if proxy_url:
                        launch_options["proxy"] = {"server": proxy_url}

                    self._browser = await browser_type.launch(**launch_options)
                    raw_version = getattr(self._browser, "version", "")
                    if callable(raw_version):
                        raw_version = raw_version()
                    self.browser_version = " ".join(
                        part for part in (browser_type_name, str(raw_version or "").strip()) if part
                    )
                    self._browser_context = await self._browser.new_context(**self._browser_context_options())
                if self.trace_artifacts_enabled:
                    await self._browser_context.tracing.start(screenshots=True, snapshots=True, sources=True)
                    self._trace_started = True
            except RunnerEnvironmentFailure:
                await self._release_pooled_browser_context(False)
                raise
            except ResourcePoolError as exc:
                await self._release_pooled_browser_context(False)
                raise RunnerEnvironmentFailure(str(exc)) from exc
            except Exception as exc:
                await self._release_pooled_browser_context(False)
                raise RunnerEnvironmentFailure(f"浏览器环境启动失败：{exc}") from exc

        page = await self._browser_context.new_page()
        page.set_default_timeout(self.timeout_ms)
        page.on("console", lambda message: self._capture_console(message))
        page.on("requestfailed", lambda request: self.log(f"Network failed: {request.url}"))
        self._pages.append(page)
        return page

    async def expect_visible(self, page: Any, selector: str) -> None:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=self.timeout_ms)
        except Exception as exc:
            raise RunFailure(f"元素未出现：{selector}") from exc

    async def expect_hidden(self, page: Any, selector: str) -> None:
        try:
            await page.locator(selector).first.wait_for(state="hidden", timeout=self.timeout_ms)
        except Exception as exc:
            raise RunFailure(f"元素仍然可见：{selector}") from exc

    async def expect_text(self, page: Any, text: str) -> None:
        try:
            await page.get_by_text(text).first.wait_for(state="visible", timeout=self.timeout_ms)
        except Exception as exc:
            raise RunFailure(f"文本未出现：{text}") from exc

    async def screenshot(self, page: Any | None = None, name: str | None = None) -> str:
        target_page = page or (self._pages[-1] if self._pages else None)
        if target_page is None:
            raise RunFailure("当前没有可截图的页面")
        path, relative = self.artifacts.screenshot_target(self.run_id, name or "manual")
        await target_page.screenshot(path=str(path), full_page=True)
        self.screenshot_path = relative
        return relative

    def save_response(self, response: httpx.Response | None = None) -> str:
        target = response or self.http.last_response
        if target is None:
            raise RunFailure("当前没有可保存的响应体")
        existing = self.response_snapshot or {}
        payload = self.response_snapshot_from(target, include_body=True)
        for key in ("duration_ms", "timings", "assertions"):
            if key in existing:
                payload[key] = existing[key]
        self.response_snapshot = payload
        self.response_path = self.artifacts.save_response(self.run_id, payload)
        return self.response_path

    def assert_status(self, response: httpx.Response, expected: int) -> None:
        if response.status_code != expected:
            raise RunFailure(f"HTTP 状态码不匹配：期望 {expected}，实际 {response.status_code}")

    def assert_body_contains(self, response: httpx.Response, text: str) -> None:
        expected = resolve_text(text, self.settings)
        if expected not in response.text:
            raise RunFailure(f"响应内容未包含：{expected}")

    def assert_url_contains(self, response: httpx.Response, text: str) -> None:
        expected = resolve_text(text, self.settings)
        url = str(response.url)
        if expected not in url:
            raise RunFailure(f"最终 URL 未包含：{expected}，实际 {url}")

    def assert_redirect_occurred(self, response: httpx.Response) -> None:
        if not response.history:
            raise RunFailure("未发生 HTTP 跳转")

    def assert_json_schema(self, data: Any, schema: dict[str, Any]) -> None:
        try:
            validate(instance=data, schema=schema)
        except ValidationError as exc:
            path = ".".join(str(item) for item in exc.path) or "$"
            message = f"JSON Schema 校验失败：{path} {exc.message}"
            self.schema_errors.append(message)
            raise RunFailure(message) from exc

    def fail(self, message: str) -> None:
        raise RunFailure(message)

    def log(self, message: Any) -> None:
        self.logs.append(str(message))

    def set_task_duration(self, duration_ms: int | None) -> None:
        self.task_duration_ms = max(0, int(duration_ms or 0))
        if self.response_snapshot is None:
            return
        timings = self.response_snapshot.get("timings")
        if not isinstance(timings, dict):
            timings = {}
        if self.last_request_duration_ms is not None:
            timings.setdefault("request_ms", self.last_request_duration_ms)
        timings["task_ms"] = self.task_duration_ms
        self.response_snapshot["timings"] = timings

    async def close(self, failed: bool) -> None:
        if failed and self.check.get("type") == "ui" and self._pages and not self.screenshot_path:
            try:
                await self.screenshot(self._pages[-1], "failure")
            except Exception as exc:
                self.log(f"截图保存失败：{exc}")

        if self._browser_context and self._trace_started:
            try:
                if failed:
                    trace_file, relative = self.artifacts.trace_target(self.run_id)
                    await self._browser_context.tracing.stop(path=str(trace_file))
                    self.trace_path = relative
                else:
                    await self._browser_context.tracing.stop()
            except Exception as exc:
                self.log(f"Trace 保存失败：{exc}")
            finally:
                self._trace_started = False

        if failed and self.check.get("type") == "api" and self.http.last_response is not None and not self.response_path:
            try:
                self.save_response(self.http.last_response)
            except Exception as exc:
                self.log(f"响应体保存失败：{exc}")

        browser_context_error: Exception | None = None
        if self._browser_context:
            try:
                await self._browser_context.close()
            except Exception as exc:
                browser_context_error = exc
            finally:
                self._browser_context = None
        if self._browser_context_lease is not None and self.resources is not None:
            await self.resources.release_browser_context(self._browser_context_lease, healthy=browser_context_error is None)
            self._browser_context_lease = None
            self._browser = None
        if self._browser_lease is not None and self.resources is not None:
            await self.resources.release_browser(self._browser_lease, healthy=browser_context_error is None)
            self._browser_lease = None
            self._browser = None
        elif self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        await self.http.close()
        if browser_context_error is not None:
            raise browser_context_error

    async def _release_pooled_browser_context(self, healthy: bool) -> None:
        if self._browser_context_lease is not None and self.resources is not None:
            await self.resources.release_browser_context(self._browser_context_lease, healthy=healthy)
            self._browser_context_lease = None
            self._browser_context = None
            self._browser = None

    def response_snapshot_from(self, response: httpx.Response, include_body: bool = False) -> dict[str, Any]:
        body_text = mask_text(response.text, self.settings)
        try:
            body_text = json.dumps(mask_data(response.json(), self.settings), ensure_ascii=False)
        except ValueError:
            pass
        body_text = mask_text(body_text, self.settings)
        if not include_body and len(body_text) > 5000:
            body_text = body_text[:5000] + "\n... 响应体已截断。"
        return mask_data(
            {
            "status_code": response.status_code,
            "url": str(response.url),
            "headers": dict(response.headers),
            "body": body_text,
            },
            self.settings,
        )

    def _capture_console(self, message: Any) -> None:
        message_type = str(getattr(message, "type", "") or "")
        message_text = str(getattr(message, "text", "") or "")
        if message_type in {"error", "warning"}:
            self.console_messages.append({"type": message_type, "text": message_text})
            self.log(f"Console {message_type}: {message_text}")

    def _viewport(self) -> dict[str, int]:
        return viewport_for_mode(self.viewport_mode, self.settings, self.viewport_width, self.viewport_height)

    def _browser_context_options(self) -> dict[str, Any]:
        return browser_context_options(self.viewport_mode, self._viewport())

    @staticmethod
    def _parse_headers(raw: str) -> dict[str, str]:
        try:
            parsed = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): str(value) for key, value in parsed.items()}

    def _configured_body_kwargs(self) -> dict[str, Any]:
        body = self.body.strip()
        if not body:
            return {}
        try:
            return {"json": json.loads(body)}
        except json.JSONDecodeError:
            return {"content": self.body}

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
