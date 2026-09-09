from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from backend.app.api_assertions import ApiInspectionFailure, api_inspection_diagnostics, inspect_api_response
from backend.app.artifacts import ArtifactStore
from backend.app.context import RunContext, RunFailure
from backend.app.resource_pool import ProbeResourcePool
from backend.app.runner import CheckRunner


class _ApiHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        if parsed.path == "/seed":
            payload = {"request_id": "seed-7"}
            status = 200
        elif parsed.path == "/dependency":
            payload = {"ok": False, "dependency": "unavailable"}
            status = 503
        elif parsed.path == "/target":
            query = parse_qs(parsed.query)
            payload = {
                "data": {"ok": query.get("ok", ["true"])[0] == "true"},
                "token": "super-secret-token",
                "request_id": query.get("id", [""])[0],
                "echo": query.get("secret", [""])[0],
            }
            status = 200
        elif parsed.path == "/slow":
            time.sleep(0.8)
            payload = {"ok": True}
            status = 200
        else:
            payload = {"detail": "missing"}
            status = 404
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            # The preview timeout intentionally closes the client connection.
            pass

    def log_message(self, *_args: object) -> None:
        return


class _ApiServer:
    def __enter__(self) -> "_ApiServer":
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        self.server.requests = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def requests(self) -> list[str]:
        return self.server.requests  # type: ignore[attr-defined]


class ApiScriptFlowTests(unittest.TestCase):
    def test_script_runs_before_rules_and_rules_use_last_response_without_duplicate_request(self) -> None:
        with _ApiServer() as server:
            check = self._check(
                server,
                entry_url="${API_HOST}/target?id=${REQUEST_ID}",
                script=(
                    "async def check(ctx):\n"
                    "    ctx.vars.set('REQUEST_ID', 'seed-7')\n"
                    "    await ctx.request()\n"
                ),
                assertions=[
                    {"type": "status_code", "expected_status": 200},
                    {"type": "json_path_equals", "path": "$.data.ok", "expected_value": "true"},
                ],
            )
            ctx = self._context(check, server)
            try:
                duration = asyncio.run(CheckRunner()._run_check_attempt(ctx, check, self._settings(server)))
                ctx.set_task_duration(duration)
            finally:
                asyncio.run(ctx.close(False))

            self.assertEqual(len(server.requests), 1)
            self.assertEqual(parse_qs(urlsplit(server.requests[0]).query)["id"], ["seed-7"])
            self.assertEqual([item["status"] for item in ctx.response_snapshot["assertions"]], ["ok", "ok"])
            self.assertEqual(ctx.response_snapshot["timings"]["request_ms"], ctx.last_request_duration_ms)
            self.assertEqual(ctx.response_snapshot["timings"]["task_ms"], duration)

    def test_last_response_business_rule_failure_is_target_failure(self) -> None:
        with _ApiServer() as server:
            check = self._check(
                server,
                entry_url=f"{server.base_url}/target?ok=false",
                script="async def check(ctx):\n    await ctx.request()\n",
                assertions=[{"type": "json_path_equals", "path": "$.data.ok", "expected_value": "true"}],
            )
            ctx = self._context(check, server)
            try:
                with self.assertRaisesRegex(RunFailure, "JSON 字段值不匹配"):
                    asyncio.run(CheckRunner()._run_check_attempt(ctx, check, self._settings(server)))
            finally:
                asyncio.run(ctx.close(False))
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(ctx.response_snapshot["assertions"][0]["status"], "failed")

    def test_dependency_failure_keeps_exchange_error_or_response_diagnostics(self) -> None:
        with _ApiServer() as server:
            check = self._check(
                server,
                entry_url=f"{server.base_url}/dependency",
                script=(
                    "async def check(ctx):\n"
                    "    response = await ctx.request()\n"
                    "    ctx.assert_status(response, 200)\n"
                ),
                assertions=[],
            )
            ctx = self._context(check, server)
            try:
                with self.assertRaisesRegex(RunFailure, "HTTP 状态码不匹配"):
                    asyncio.run(CheckRunner()._run_check_attempt(ctx, check, self._settings(server)))
            finally:
                asyncio.run(ctx.close(False))
            self.assertEqual(len(ctx.http_exchanges), 1)
            self.assertEqual(ctx.http_exchanges[0]["response"]["status_code"], 503)
            self.assertIn("duration_ms", ctx.http_exchanges[0])

    def test_script_plus_rules_without_response_fails_clearly_and_stale_parameters_surface(self) -> None:
        with _ApiServer() as server:
            check = self._check(
                server,
                entry_url="${API_HOST}/target",
                script="async def check(ctx):\n    ctx.vars.set('REQUEST_ID', '7')\n",
                assertions=[{"type": "status_code", "expected_status": 200}],
            )
            ctx = self._context(check, server)
            try:
                with self.assertRaisesRegex(RunFailure, "脚本未产生 HTTP 响应"):
                    asyncio.run(CheckRunner()._run_check_attempt(ctx, check, self._settings(server)))
            finally:
                asyncio.run(ctx.close(False))

            stale = self._context(
                self._check(
                    server,
                    entry_url="${MISSING_HOST}/target",
                    script="async def check(ctx):\n    pass\n",
                    assertions=[],
                ),
                server,
            )
            try:
                with self.assertRaisesRegex(RunFailure, "MISSING_HOST"):
                    asyncio.run(CheckRunner()._run_check_attempt(stale, stale.check, self._settings(server)))
            finally:
                asyncio.run(stale.close(False))

    def test_preview_uses_the_same_script_request_flow_and_returns_diagnostics(self) -> None:
        with _ApiServer() as server:
            settings = self._settings(server)
            result = asyncio.run(
                inspect_api_response(
                    {
                        "type": "api",
                        "entry_url": "${API_HOST}/target?id=${REQUEST_ID}",
                        "method": "GET",
                        "headers_json": "{}",
                        "body": "",
                        "timeout_ms": 10000,
                        "script": (
                            "async def check(ctx):\n"
                            "    ctx.vars.set('REQUEST_ID', 'preview-1')\n"
                            "    await ctx.request()\n"
                        ),
                    },
                    settings,
                )
            )
            self.assertEqual(result["status_code"], 200)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(result["diagnostics"]["http_requests"][-1]["duration_ms"], result["timings"]["request_ms"])
            self.assertEqual(result["timings"]["task_ms"], result["diagnostics"]["timings"]["task_ms"])
            self.assertEqual(result["diagnostics"]["variables"], {"REQUEST_ID": "preview-1"})

    def test_history_uses_httpx_request_url_with_params(self) -> None:
        with _ApiServer() as server:
            check = self._check(
                server,
                entry_url=f"{server.base_url}/target",
                script=(
                    "async def check(ctx):\n"
                    "    await ctx.http.get(ctx.entry_url, params={'id': 'from-params'})\n"
                ),
                assertions=[],
            )
            ctx = self._context(check, server)
            try:
                asyncio.run(CheckRunner()._run_check_attempt(ctx, check, self._settings(server)))
            finally:
                asyncio.run(ctx.close(False))

            self.assertIn("id=from-params", ctx.request_snapshot["url"])
            self.assertIn("id=from-params", ctx.http_exchanges[-1]["request"]["url"])

    def test_preview_masks_runtime_secret_in_body_paths_and_history(self) -> None:
        with _ApiServer() as server:
            result = asyncio.run(
                inspect_api_response(
                    {
                        "type": "api",
                        "entry_url": f"{server.base_url}/target",
                        "method": "GET",
                        "headers_json": "{}",
                        "body": "",
                        "timeout_ms": 10000,
                        "script": (
                            "async def check(ctx):\n"
                            "    ctx.vars.set('RUNTIME_SECRET', 'runtime-only-secret', secret=True)\n"
                            "    await ctx.http.get(ctx.entry_url, params={'secret': '${RUNTIME_SECRET}'})\n"
                        ),
                    },
                    self._settings(server),
                )
            )
            serialized = json.dumps(result, ensure_ascii=False)

            self.assertNotIn("runtime-only-secret", serialized)
            self.assertIn("***", result["body"])
            self.assertEqual(result["diagnostics"]["variables"]["RUNTIME_SECRET"], "***")

    def test_preview_timeout_releases_pooled_http_client(self) -> None:
        with _ApiServer() as server:
            async def scenario() -> dict[str, object]:
                resources = ProbeResourcePool(1, 1)
                settings = self._settings(server)
                await resources.start(settings, api_pool_size=1, browser_pool_sizes_value=1)
                try:
                    with self.assertRaises(ApiInspectionFailure):
                        await inspect_api_response(
                            {
                                "type": "api",
                                "entry_url": f"{server.base_url}/slow",
                                "method": "GET",
                                "headers_json": "{}",
                                "body": "",
                                "timeout_ms": 500,
                            },
                            settings,
                            resources=resources,
                        )
                    return resources.snapshot()["api"]
                finally:
                    await resources.shutdown()

            snapshot = asyncio.run(scenario())
            self.assertEqual(snapshot["in_use"], 0)

    def test_preview_external_cancel_closes_context_and_releases_pooled_http_client(self) -> None:
        with _ApiServer() as server:
            async def scenario() -> tuple[dict[str, int], int]:
                resources = ProbeResourcePool(1, 1)
                settings = self._settings(server)
                await resources.start(settings, api_pool_size=1, browser_pool_sizes_value=1)
                closed: list[RunContext] = []
                original_close = RunContext.close

                async def tracking_close(ctx: RunContext, failed: bool) -> None:
                    closed.append(ctx)
                    await original_close(ctx, failed)

                task = None
                try:
                    with patch.object(RunContext, "close", new=tracking_close):
                        task = asyncio.create_task(
                            inspect_api_response(
                                {
                                    "type": "api",
                                    "entry_url": f"{server.base_url}/target",
                                    "method": "GET",
                                    "headers_json": "{}",
                                    "body": "",
                                    "timeout_ms": 10000,
                                    "script": (
                                        "async def check(ctx):\n"
                                        "    await ctx.http.get(ctx.entry_url)\n"
                                        "    await asyncio.sleep(60)\n"
                                    ),
                                },
                                settings,
                                resources=resources,
                            )
                        )
                        for _ in range(100):
                            if server.requests:
                                break
                            await asyncio.sleep(0.01)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    return resources.snapshot()["api"], len(closed)
                finally:
                    if task is not None and not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    await resources.shutdown()

            snapshot, close_count = asyncio.run(scenario())
            self.assertEqual(snapshot["in_use"], 0)
            self.assertEqual(close_count, 1)

    def test_timeout_keeps_bounded_masked_exchange_history(self) -> None:
        with _ApiServer() as server:
            check = self._check(
                server,
                entry_url=f"{server.base_url}/target",
                script=(
                    "async def check(ctx):\n"
                    "    for index in range(12):\n"
                    "        await ctx.http.get(ctx.entry_url + '?id=' + str(index))\n"
                    "    await asyncio.sleep(1.2)\n"
                ),
                assertions=[],
                timeout_ms=500,
            )
            ctx = self._context(check, server)
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    asyncio.run(CheckRunner()._run_check_attempt(ctx, check, self._settings(server)))
            finally:
                asyncio.run(ctx.close(False))

            self.assertEqual(len(ctx.http_exchanges), 10)
            text = json.dumps(ctx.http_exchanges, ensure_ascii=False)
            self.assertNotIn("super-secret-token", text)
            self.assertIn("***", text)
            diagnostics = api_inspection_diagnostics(ctx)
            self.assertEqual(len(diagnostics["http_requests"]), 10)
            self.assertTrue(all("duration_ms" in exchange for exchange in diagnostics["http_requests"]))

    def test_response_masking_happens_before_body_truncation(self) -> None:
        with _ApiServer() as server:
            ctx = self._context(
                self._check(server, entry_url=f"{server.base_url}/target", script="", assertions=[]),
                server,
            )
            try:
                response = httpx.Response(
                    200,
                    text=("x" * 4996) + "super-secret-token" + ("y" * 100),
                    request=httpx.Request("GET", f"{server.base_url}/target"),
                )
                snapshot = ctx.response_snapshot_from(response)
            finally:
                asyncio.run(ctx.close(False))

            self.assertIn("***", snapshot["body"])
            self.assertNotIn("super-secret-token", snapshot["body"])

    def test_preview_failure_message_is_masked_and_keeps_diagnostics(self) -> None:
        with _ApiServer() as server:
            with self.assertRaises(ApiInspectionFailure) as raised:
                asyncio.run(
                    inspect_api_response(
                        {
                            "type": "api",
                            "entry_url": f"{server.base_url}/target",
                            "method": "GET",
                            "headers_json": "{}",
                            "body": "",
                            "timeout_ms": 10000,
                            "script": "async def check(ctx):\n    ctx.fail('super-secret-token')\n",
                        },
                        self._settings(server),
                    )
                )

            self.assertNotIn("super-secret-token", str(raised.exception))
            self.assertNotIn("super-secret-token", json.dumps(raised.exception.diagnostics, ensure_ascii=False))
            self.assertEqual(raised.exception.diagnostics["status"], "failed")

    @staticmethod
    def _settings(server: _ApiServer) -> dict[str, object]:
        return {
            "max_task_runtime_seconds": 5,
            "environment_variables": [
                {"id": "api-host", "name": "API_HOST", "value": server.base_url, "secret": False},
                {"id": "secret", "name": "SECRET_TOKEN", "value": "super-secret-token", "secret": True},
            ],
        }

    def _check(self, server: _ApiServer, *, entry_url: str, script: str, assertions: list[dict[str, object]], timeout_ms: int = 10000) -> dict[str, object]:
        return {
            "id": 1,
            "name": "API script flow",
            "type": "api",
            "entry_url": entry_url,
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "timeout_ms": timeout_ms,
            "assertions_json": json.dumps(assertions),
            "script": script,
        }

    def _context(self, check: dict[str, object], server: _ApiServer) -> RunContext:
        return RunContext(check, 1, self._settings(server), ArtifactStore())


if __name__ == "__main__":
    unittest.main()
