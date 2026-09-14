from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.context import RunContext, RunFailure
from backend.app.resource_pool import ProbeResourcePool
from backend.app.runner import CheckRunner


class RunContextViewportTests(unittest.TestCase):
    def test_h5_task_uses_mobile_browser_context_options(self) -> None:
        ctx = RunContext(
            {
                "name": "H5 页面",
                "type": "ui",
                "entry_url": "https://example.com",
                "viewport_mode": "h5",
                "method": "",
                "headers_json": "{}",
                "body": "",
                "timeout_ms": 15000,
            },
            1,
            {"browser_viewport": "1440x900"},
            artifacts=None,  # type: ignore[arg-type]
        )

        options = ctx._browser_context_options()

        self.assertEqual(options["viewport"], {"width": 390, "height": 844})
        self.assertTrue(options["is_mobile"])
        self.assertTrue(options["has_touch"])

    def test_web_task_uses_configured_browser_viewport(self) -> None:
        ctx = RunContext(
            {
                "name": "Web 页面",
                "type": "ui",
                "entry_url": "https://example.com",
                "viewport_mode": "web",
                "method": "",
                "headers_json": "{}",
                "body": "",
                "timeout_ms": 15000,
            },
            1,
            {"browser_viewport": "1366x768"},
            artifacts=None,  # type: ignore[arg-type]
        )

        options = ctx._browser_context_options()

        self.assertEqual(options, {"viewport": {"width": 1366, "height": 768}})

    def test_trace_artifacts_are_disabled_by_default_and_configurable(self) -> None:
        base_check = {
            "name": "UI",
            "type": "ui",
            "entry_url": "https://example.com",
            "viewport_mode": "web",
            "method": "",
            "headers_json": "{}",
            "body": "",
            "timeout_ms": 15000,
        }
        disabled = RunContext(base_check, 1, {}, artifacts=None)  # type: ignore[arg-type]
        enabled = RunContext(base_check, 2, {"trace_artifacts_enabled": True}, artifacts=None)  # type: ignore[arg-type]
        try:
            self.assertFalse(disabled.trace_artifacts_enabled)
            self.assertTrue(enabled.trace_artifacts_enabled)
        finally:
            asyncio.run(disabled.close(False))
            asyncio.run(enabled.close(False))


class RunContextVariableTests(unittest.TestCase):
    def test_resolves_placeholders_for_entry_url_headers_and_body(self) -> None:
        settings = variable_settings()
        ctx = RunContext(
            {
                "name": "API",
                "type": "api",
                "entry_url": "${API_HOST}/v1",
                "method": "POST",
                "headers_json": '{"Authorization":"Bearer ${SERVICE_TOKEN}","X-Label":"${PUBLIC_LABEL}"}',
                "body": '{"token":"${SERVICE_TOKEN}","label":"${PUBLIC_LABEL}"}',
                "timeout_ms": 10000,
            },
            1,
            settings,
            artifacts=None,  # type: ignore[arg-type]
        )

        try:
            self.assertEqual(ctx.entry_url, "https://api.example.com/v1")
            self.assertEqual(
                ctx.headers,
                {"Authorization": "Bearer token-secret-123", "X-Label": "visible-value"},
            )
            self.assertEqual(ctx.body, '{"token":"token-secret-123","label":"visible-value"}')
        finally:
            asyncio.run(ctx.close(False))

    def test_request_and_response_snapshots_mask_secret_variable_values(self) -> None:
        settings = variable_settings()
        ctx = RunContext(
            {
                "name": "API",
                "type": "api",
                "entry_url": "${API_HOST}/v1",
                "method": "POST",
                "headers_json": '{"Authorization":"Bearer ${SERVICE_TOKEN}"}',
                "body": '{"token":"${SERVICE_TOKEN}"}',
                "timeout_ms": 10000,
            },
            1,
            settings,
            artifacts=None,  # type: ignore[arg-type]
        )

        async def scenario() -> None:
            response = httpx.Response(
                200,
                text='{"token":"token-secret-123"}',
                headers={"X-Token": "token-secret-123"},
                request=httpx.Request("POST", ctx.entry_url),
            )
            ctx.http._client.request = AsyncMock(return_value=response)  # type: ignore[method-assign]
            await ctx.request()
            await ctx.close(False)

        asyncio.run(scenario())
        request_snapshot = json.dumps(ctx.request_snapshot, ensure_ascii=False)
        response_snapshot = json.dumps(ctx.response_snapshot, ensure_ascii=False)

        self.assertNotIn("token-secret-123", request_snapshot)
        self.assertNotIn("token-secret-123", response_snapshot)
        self.assertIn("***", request_snapshot)
        self.assertIn("***", response_snapshot)

    def test_request_error_is_reported_as_target_run_failure(self) -> None:
        ctx = RunContext(
            {
                "name": "API",
                "type": "api",
                "entry_url": "https://api.example.com/v1",
                "method": "GET",
                "headers_json": "{}",
                "body": "",
                "timeout_ms": 10000,
            },
            1,
            {},
            artifacts=None,  # type: ignore[arg-type]
        )

        async def scenario() -> None:
            request = httpx.Request("GET", ctx.entry_url)
            ctx.http._client.request = AsyncMock(side_effect=httpx.ConnectError("dns failed", request=request))  # type: ignore[method-assign]
            with self.assertRaises(RunFailure) as cm:
                await ctx.request()
            self.assertIn("请求目标失败", str(cm.exception))
            await ctx.close(False)

        asyncio.run(scenario())

    def test_request_uses_shared_http_pool_without_closing_pool_client(self) -> None:
        response = httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("GET", "https://api.example.com/v1"),
        )
        resources = FakeResourcePool(response=response)
        ctx = RunContext(
            {
                "name": "API",
                "type": "api",
                "entry_url": "https://api.example.com/v1",
                "method": "GET",
                "headers_json": "{}",
                "body": "",
                "timeout_ms": 10000,
            },
            1,
            {},
            artifacts=None,  # type: ignore[arg-type]
            resources=resources,  # type: ignore[arg-type]
        )

        async def scenario() -> None:
            result = await ctx.request()
            self.assertEqual(result.status_code, 200)
            await ctx.close(False)

        asyncio.run(scenario())

        self.assertEqual(resources.http_acquired, 1)
        self.assertEqual(resources.client.requests[0]["url"], "https://api.example.com/v1")
        self.assertEqual(resources.client.requests[0]["kwargs"]["timeout"], 10)
        self.assertFalse(resources.client.closed)

    def test_runner_masks_secret_values_before_persisting_run_payload(self) -> None:
        secret = "runner-secret-123"
        settings = {
            "max_concurrency": 2,
            "max_ui_concurrency": 1,
            "max_queue_size": 50,
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "environment_variables": [
                {"id": "token", "name": "SERVICE_TOKEN", "value": secret, "secret": True},
            ],
        }
        check = {
            "id": 0,
            "name": "API",
            "type": "api",
            "enabled": True,
            "entry_url": "https://api.example.com",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "timeout_ms": 10000,
            "assertions_json": "[]",
            "script": (
                "async def check(ctx):\n"
                f"    ctx.log('log {secret}')\n"
                f"    ctx.request_snapshot = {{'headers': {{'Authorization': 'Bearer {secret}'}}, 'body': '{secret}'}}\n"
                f"    ctx.response_snapshot = {{'headers': {{'X-Token': '{secret}'}}, 'body': 'body {secret}'}}\n"
                f"    raise RuntimeError('error {secret}')\n"
            ),
            "tags": "",
        }
        finished_runs: list[dict[str, object]] = []

        def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            finished = {"id": run_id, "check_id": 0, **data}
            finished_runs.append(finished)
            return finished

        with patch("backend.app.runner.storage.get_settings", return_value=settings), patch(
            "backend.app.runner.storage.finish_run", side_effect=finish_run
        ), patch("backend.app.runner.storage.get_run", return_value=None), patch(
            "backend.app.runner.storage.update_run_notification"
        ):
            result = asyncio.run(CheckRunner()._execute(check, "manual", 77, record_status=False, notify=False))

        self.assertEqual(result["status"], "failed")
        payload = finished_runs[0]
        persisted_text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, persisted_text)
        self.assertIn("***", persisted_text)

    def test_runner_records_missing_variable_as_task_failure(self) -> None:
        settings = {
            "max_concurrency": 2,
            "max_ui_concurrency": 1,
            "max_queue_size": 50,
            "max_task_runtime_seconds": 60,
            "browser_type": "chromium",
            "browser_headless": True,
            "environment_variables": [],
        }
        check = {
            "id": 11,
            "name": "API",
            "type": "api",
            "enabled": True,
            "entry_url": "${MISSING_URL}/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "timeout_ms": 10000,
            "assertions_json": "[]",
            "script": "async def check(ctx):\n    pass\n",
            "tags": "",
        }
        finished_runs: list[dict[str, object]] = []

        def finish_run(run_id: int, data: dict[str, object]) -> dict[str, object]:
            finished = {"id": run_id, "check_id": 11, **data}
            finished_runs.append(finished)
            return finished

        with patch("backend.app.runner.storage.get_settings", return_value=settings), patch(
            "backend.app.runner.storage.finish_run", side_effect=finish_run
        ), patch("backend.app.runner.storage.get_run", return_value=None), patch(
            "backend.app.runner.storage.update_check_status", return_value={"current_status": "failed"}
        ) as update_check_status, patch("backend.app.runner.notifier.maybe_notify", new=AsyncMock()):
            result = asyncio.run(CheckRunner()._execute(check, "manual", 78, record_status=True, notify=True))

        self.assertEqual(result["status"], "failed")
        self.assertIn("MISSING_URL", str(result["error_message"]))
        update_check_status.assert_called_once()


class RunContextProbeHelperTests(unittest.TestCase):
    def test_http_assertion_helpers_check_body_redirect_and_url(self) -> None:
        ctx = api_context()
        redirect = httpx.Response(302, request=httpx.Request("GET", "https://example.com/start"))
        response = httpx.Response(
            200,
            text="hello keyword",
            request=httpx.Request("GET", "https://example.com/login"),
            history=[redirect],
        )

        try:
            ctx.assert_body_contains(response, "keyword")
            ctx.assert_redirect_occurred(response)
            ctx.assert_url_contains(response, "/login")
            with self.assertRaises(RunFailure):
                ctx.assert_body_contains(response, "missing")
        finally:
            asyncio.run(ctx.close(False))

    def test_expect_heartbeat_accepts_recent_ok_heartbeat(self) -> None:
        ctx = api_context()
        heartbeat = {
            "key": "nightly-job",
            "status": "ok",
            "message": "",
            "payload": {},
            "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

        try:
            with patch("backend.app.storage.get_heartbeat", return_value=heartbeat):
                self.assertEqual(ctx.expect_heartbeat("nightly-job", max_age_seconds=60), heartbeat)
        finally:
            asyncio.run(ctx.close(False))

    def test_expect_heartbeat_rejects_stale_or_failed_heartbeat(self) -> None:
        ctx = api_context()
        stale = {
            "key": "nightly-job",
            "status": "ok",
            "message": "",
            "payload": {},
            "received_at": (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds"),
        }

        try:
            with patch("backend.app.storage.get_heartbeat", return_value=stale):
                with self.assertRaises(RunFailure):
                    ctx.expect_heartbeat("nightly-job", max_age_seconds=60)
        finally:
            asyncio.run(ctx.close(False))

    def test_network_helpers_delegate_to_network_check_functions(self) -> None:
        ctx = api_context()

        async def scenario() -> None:
            with patch("backend.app.context.network_checks.tcp_connect", return_value={"host": "example.com", "port": 443, "duration_ms": 12}) as tcp_connect, patch(
                "backend.app.context.network_checks.resolve_hostname",
                return_value={"host": "example.com", "addresses": ["93.184.216.34"]},
            ) as resolve_hostname, patch(
                "backend.app.context.network_checks.tls_certificate",
                return_value={"host": "example.com", "port": 443, "expires_at": "2026-12-31T00:00:00+00:00", "days_remaining": 120},
            ) as tls_certificate:
                self.assertEqual((await ctx.tcp_connect("example.com:443"))["duration_ms"], 12)
                self.assertEqual((await ctx.dns_resolve("example.com"))["addresses"], ["93.184.216.34"])
                self.assertEqual((await ctx.tls_certificate("example.com", warn_days=14))["days_remaining"], 120)
                tcp_connect.assert_called_once()
                resolve_hostname.assert_called_once_with("example.com")
                tls_certificate.assert_called_once()
            await ctx.close(False)

        asyncio.run(scenario())


class RunContextPoolTests(unittest.TestCase):
    def test_repeated_cancellation_waits_for_context_release(self) -> None:
        async def scenario() -> None:
            resources = FakeResourcePool()
            ctx = RunContext({"type": "ui", "entry_url": "https://example.com"}, 1, {}, None, resources=resources)
            await ctx.new_page()
            started, release = asyncio.Event(), asyncio.Event()
            original_close = resources.browser.contexts[0].close

            async def slow_close():
                started.set()
                await release.wait()
                await original_close()

            resources.browser.contexts[0].close = slow_close
            task = asyncio.create_task(ctx.close(False))
            try:
                await asyncio.wait_for(started.wait(), 1)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertEqual(resources.released_context, resources.context_lease)
                self.assertTrue(resources.browser.contexts[0].closed)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(scenario())

    def test_hanging_context_close_still_releases_lease_and_http_client(self) -> None:
        async def scenario() -> None:
            from backend.tests.test_resource_pool import FakePlaywright, browser_pool, settings

            runner_settings = settings()
            resources = ProbeResourcePool(api_pool_size=1, browser_pool_sizes_value={"chromium": 1})
            pool = browser_pool(FakePlaywright(), size=1)
            resources.browsers["chromium"] = pool
            await pool.start(runner_settings)
            lease = await pool.acquire_context(runner_settings, {})
            ctx = RunContext({"type": "ui"}, 1, runner_settings, None, resources=resources)
            ctx._browser_context = lease.context
            ctx._browser_context_lease = lease

            async def hanging_close():
                await asyncio.Event().wait()

            lease.context.close = hanging_close
            try:
                with patch("backend.app.context.RESOURCE_CLEANUP_TIMEOUT_SECONDS", 0.02), patch(
                    "backend.app.resource_pool.RESOURCE_CLEANUP_TIMEOUT_SECONDS", 0.02
                ), patch.object(ctx.http, "close", new=AsyncMock()) as close_http:
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(ctx.close(False), 1)
                    close_http.assert_awaited_once()
                self.assertEqual(pool.snapshot()["in_use"], 0)
                self.assertIsNone(ctx._browser_context_lease)
                self.assertTrue(ctx._close_task.done())
            finally:
                await resources.shutdown()

        asyncio.run(scenario())

    def test_pooled_browser_keeps_browser_and_closes_isolated_context(self) -> None:
        resources = FakeResourcePool()
        ctx = RunContext(
            {
                "name": "UI",
                "type": "ui",
                "entry_url": "https://example.com",
                "viewport_mode": "web",
                "method": "",
                "headers_json": "{}",
                "body": "",
                "timeout_ms": 15000,
            },
            1,
            {"browser_viewport": "1366x768"},
            artifacts=None,  # type: ignore[arg-type]
            resources=resources,  # type: ignore[arg-type]
        )

        async def scenario() -> None:
            first = await ctx.new_page()
            second = await ctx.new_page()
            self.assertIsNot(first, second)
            await ctx.close(False)

        asyncio.run(scenario())

        browser = resources.browser
        self.assertEqual(resources.context_acquired, 1)
        self.assertEqual(resources.context_options[0], {"viewport": {"width": 1366, "height": 768}})
        self.assertEqual(len(browser.contexts), 1)
        self.assertEqual(browser.contexts[0].options, {"viewport": {"width": 1366, "height": 768}})
        self.assertEqual(len(browser.contexts[0].pages), 2)
        self.assertTrue(browser.contexts[0].closed)
        self.assertFalse(browser.closed)
        self.assertEqual(resources.released_context, resources.context_lease)
        self.assertTrue(resources.release_healthy)


def api_context() -> RunContext:
    return RunContext(
        {
            "name": "API",
            "type": "api",
            "entry_url": "https://example.com",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "timeout_ms": 10000,
        },
        1,
        variable_settings(),
        artifacts=None,  # type: ignore[arg-type]
    )


def variable_settings() -> dict[str, object]:
    return {
        "environment_variables": [
            {"id": "host", "name": "API_HOST", "value": "https://api.example.com", "secret": False},
            {"id": "label", "name": "PUBLIC_LABEL", "value": "visible-value", "secret": False},
            {"id": "token", "name": "SERVICE_TOKEN", "value": "token-secret-123", "secret": True},
        ],
    }


class FakeHttpClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []
        self.closed = False

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        return self.response

    async def aclose(self) -> None:
        self.closed = True


class FakeTracing:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self, **_: object) -> None:
        self.started = True

    async def stop(self, **_: object) -> None:
        self.stopped = True


class FakePage:
    def __init__(self) -> None:
        self.timeout = 0
        self.handlers: dict[str, object] = {}

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler


class FakeBrowserContext:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        self.tracing = FakeTracing()
        self.pages: list[FakePage] = []
        self.closed = False

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeBrowserContext] = []
        self.closed = False

    async def new_context(self, **options: object) -> FakeBrowserContext:
        context = FakeBrowserContext(options)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return True


class FakeResourcePool:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.client = FakeHttpClient(
            response
            or httpx.Response(200, request=httpx.Request("GET", "https://example.com"))
        )
        self.http_acquired = 0
        self.browser = FakeBrowser()
        self.browser_lease = SimpleNamespace(browser=self.browser, version="chromium 120.0")
        self.context_lease: object | None = None
        self.browser_acquired = 0
        self.context_acquired = 0
        self.context_options: list[dict[str, object]] = []
        self.released_browser: object | None = None
        self.released_context: object | None = None
        self.release_healthy: bool | None = None

    @asynccontextmanager
    async def http_client(self):  # type: ignore[no-untyped-def]
        self.http_acquired += 1
        yield self.client

    async def acquire_browser(self, settings: dict[str, object]) -> object:
        self.browser_acquired += 1
        return self.browser_lease

    async def release_browser(self, lease: object, *, healthy: bool = True) -> None:
        self.released_browser = lease
        self.release_healthy = healthy

    async def acquire_browser_context(self, settings: dict[str, object], context_options: dict[str, object]) -> object:
        self.context_acquired += 1
        self.context_options.append(context_options)
        context = await self.browser.new_context(**context_options)
        self.context_lease = SimpleNamespace(browser=self.browser, context=context, version="chromium 120.0")
        return self.context_lease

    async def release_browser_context(self, lease: object, *, healthy: bool = True) -> None:
        self.released_context = lease
        self.release_healthy = healthy


if __name__ == "__main__":
    unittest.main()
