from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.app.context import RunContext, RunFailure
from backend.app.main import app, _active_project_context
from backend.app.runner import AsyncCapacityLimiter, CheckRunner
from backend.app.ui_diagnostics import UiInspectionFailure, inspection_diagnostics
from backend.tests.test_ui_assertions import SetupInspectPage


class UiDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runner = object.__new__(CheckRunner)
        self.runner.artifacts = None
        self.runner.resources = None
        self.runner._ui_limiter = AsyncCapacityLimiter(1)
        self.contexts = []
        self.settings = {"max_task_runtime_seconds": 5, "environment_variables": [
            {"name": "SOURCE", "value": "https://source.example/list"},
            {"name": "TOKEN", "value": "private/&+value", "secret": True},
        ]}
        self.handler = lambda request: httpx.Response(200, json={"items": []})
        self.cleanup_error = False
        self.navigation_error = False
        self.payload = {"type": "ui", "entry_url": "https://target.example/?id=${id}", "timeout_ms": 1000,
                        "setup_script": "async def setup(ctx, page):\n    response = await ctx.http.get('${SOURCE}')\n    items = response.json()['items']\n    ctx.log('items count=' + str(len(items)))\n    if not items:\n        ctx.fail('items empty')\n    ctx.vars.set('id', items[0]['id'])"}

        def context(*args, **kwargs):
            ctx = RunContext(*args, **kwargs)
            # Reuse the real client instance with a deterministic transport.
            original_client = ctx.http._client
            ctx.http._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.handler(request)))
            page = SetupInspectPage()
            if self.navigation_error:
                page.goto = AsyncMock(side_effect=RuntimeError("navigation failed"))
            ctx.new_page = AsyncMock(return_value=page)
            close = ctx.close
            async def cleanup(failed):
                await original_client.aclose()
                await close(failed)
                if self.cleanup_error:
                    raise RuntimeError("cleanup failed")
            ctx.close = AsyncMock(side_effect=cleanup)
            self.contexts.append(ctx)
            return ctx

        self.patches = [patch("backend.app.runner.RunContext", side_effect=context),
                        patch("backend.app.runner.storage.get_settings", return_value=self.settings),
                        patch("backend.app.runner.storage.settings_for_project", side_effect=lambda settings, project_id: settings)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()

    async def test_empty_source_evidence_survives_both_preview_entrypoints(self):
        for method in (self.runner.inspect_ui, self.runner.inspect_ui_rules):
            with self.assertRaises(UiInspectionFailure) as failure:
                await method(self.payload)
            detail = failure.exception.diagnostics
            self.assertEqual(detail["stage"], "setup")
            self.assertEqual(detail["variables"], {})
            self.assertEqual(detail["url_variables"], {"id": "missing"})
            self.assertEqual(detail["http_requests"][0]["request"]["url"], "https://source.example/list")
            self.assertEqual(json.loads(detail["http_requests"][0]["response"]["body"]), {"items": []})
            self.assertIn("items count=0", detail["logs"])
            self.assertEqual(self.contexts[-1].new_page.return_value.gotos, [])
            self.contexts[-1].close.assert_awaited_once()

    async def test_retry_uses_fresh_variables_and_success_has_evidence(self):
        self.handler = lambda request: httpx.Response(200, json={"items": [{"id": 42}]})
        result = await self.runner.inspect_ui(self.payload)
        self.assertEqual(result["diagnostics"]["variables"], {"id": "42"})
        self.assertEqual(result["diagnostics"]["request"]["url"], "https://target.example/?id=42")
        self.handler = lambda request: httpx.Response(200, json={"items": []})
        with self.assertRaises(UiInspectionFailure) as failure:
            await self.runner.inspect_ui(self.payload)
        self.assertEqual(failure.exception.diagnostics["variables"], {})
        self.assertEqual(self.contexts[-1].new_page.return_value.gotos, [])

    async def test_compile_missing_variable_and_navigation_have_distinct_stages(self):
        for script, expected in [("bad python!", "compile"), ("", "resolve_url")]:
            with self.assertRaises(UiInspectionFailure) as failure:
                await self.runner.inspect_ui({**self.payload, "setup_script": script})
            self.assertEqual(failure.exception.diagnostics["stage"], expected)
        self.navigation_error = True
        with self.assertRaises(UiInspectionFailure) as failure:
            await self.runner.inspect_ui({**self.payload, "setup_script": "async def setup(ctx, page):\n    ctx.vars.set('id', 1)"})
        self.assertEqual(failure.exception.diagnostics["stage"], "navigate")
        self.assertEqual(failure.exception.diagnostics["request"]["navigation"]["status"], "failed")

    async def test_second_http_failure_never_reuses_first_response(self):
        def handler(request):
            if request.url.path == "/bad":
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(200, json={"ok": True})
        self.handler = handler
        script = "async def setup(ctx, page):\n    await ctx.http.get('${SOURCE}')\n    await ctx.http.get('https://source.example/bad')"
        with self.assertRaises(UiInspectionFailure) as failure:
            await self.runner.inspect_ui({**self.payload, "setup_script": script})
        exchanges = failure.exception.diagnostics["http_requests"]
        self.assertEqual(len(exchanges), 2)
        self.assertEqual(exchanges[0]["response"]["status_code"], 200)
        self.assertIsNone(exchanges[1]["response"])
        self.assertIsNone(failure.exception.diagnostics["request"]["preparation"]["last_response"])

    async def test_timeout_has_message_and_cleanup_does_not_mask_failure(self):
        self.cleanup_error = True
        with self.assertRaises(UiInspectionFailure) as failure:
            await self.runner.inspect_ui(self.payload)
        self.assertEqual(str(failure.exception), "items empty")
        self.assertEqual(failure.exception.diagnostics["cleanup_error"], "cleanup failed")
        self.settings["max_task_runtime_seconds"] = 0
        with self.assertRaises(UiInspectionFailure) as failure:
            await self.runner.inspect_ui(self.payload)
        self.assertEqual(failure.exception.diagnostics["stage"], "queue")
        self.assertTrue(str(failure.exception))

    async def test_cancellation_is_not_converted_to_business_failure(self):
        self.cleanup_error = True
        payload = {**self.payload, "setup_script": "async def setup(ctx, page):\n    await asyncio.sleep(100)"}
        task = asyncio.create_task(self.runner.inspect_ui(payload))
        for _ in range(100):
            await asyncio.sleep(0)
            if self.contexts and self.contexts[-1].ui_stage == "setup":
                break
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.contexts[-1].close.assert_awaited_once()
        self.assertEqual(self.contexts[-1].new_page.return_value.gotos, [])

    async def test_secret_masking_and_bounded_history(self):
        self.handler = lambda request: httpx.Response(200, json={"token": "not-configured-secret", "text": "private/&+value" + "x" * 6000})
        script = "async def setup(ctx, page):\n    for i in range(12):\n        await ctx.http.get('${SOURCE}', headers={'Authorization': '${TOKEN}'})\n    ctx.fail('private/&+value')"
        with self.assertRaises(UiInspectionFailure) as failure:
            await self.runner.inspect_ui({**self.payload, "setup_script": script})
        data = failure.exception.diagnostics
        self.assertEqual(len(data["http_requests"]), 10)
        serialized = json.dumps(data)
        self.assertNotIn("private/&+value", serialized)
        self.assertNotIn("not-configured-secret", serialized)
        self.assertLess(len(data["http_requests"][0]["response"]["body"]), 5100)

    async def test_documented_recipe_handles_mixed_records_and_rejects_bad_data(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "ui-dynamic-variables.md").read_text(encoding="utf-8")
        script = doc.split("```python\n", 1)[1].split("```", 1)[0]
        self.settings["environment_variables"].append({"name": "PRODUCT_API", "value": "https://source.example/list"})
        payload = {**self.payload, "entry_url": "https://target.example/?sku=${skuId}&spu=${spuId}&image=${goodsImage640Url}", "setup_script": script}
        product = {"skuId": "7", "spuId": "8", "goodsImage640Url": "https://img.example/a?x=1&y=2",
                   "goodsName": "动态名称", "goodsPrice": 0, "usedCarDetail": {"cityName": "城市"}}
        for body, message in [([], "JSON 对象"), ({"returncode": 0}, "缺失或不是数组"),
                              ({"returncode": 0, "result": {"records": []}}, "为空"),
                              ({"returncode": 0, "result": {"records": [{"image": "banner"}]}}, "没有同时具备"),
                              ({"returncode": 0, "result": {"records": [{**product, "goodsName": None}]}}, "缺少字段")]:
            self.handler = lambda request, body=body: httpx.Response(200, json=body)
            with self.assertRaisesRegex(UiInspectionFailure, message):
                await self.runner.inspect_ui(payload)
            self.assertEqual(self.contexts[-1].new_page.return_value.gotos, [])
        self.handler = lambda request: httpx.Response(200, json={"returncode": 0, "result": {"records": [{"image": "banner"}, product]}})
        result = await self.runner.inspect_ui(payload)
        self.assertEqual(result["diagnostics"]["variables"]["skuId"], "7")
        self.assertEqual(result["diagnostics"]["variables"]["goodsPrice"], "0")
        self.assertIn("records[1]", result["diagnostics"]["logs"])


class UiDiagnosticsApiTests(unittest.TestCase):
    def test_http_error_keeps_string_detail_and_adds_diagnostics(self):
        diagnostics = {"stage": "setup", "status": "failed", "variables": {}, "http_requests": []}
        runner = SimpleNamespace(inspect_ui=AsyncMock(side_effect=UiInspectionFailure("items empty", diagnostics)),
                                 inspect_ui_rules=AsyncMock(side_effect=UiInspectionFailure("items empty", diagnostics)))
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[_active_project_context] = lambda: 1
        try:
            with patch.object(app.state, "runner", runner, create=True), patch("backend.app.main.storage.get_settings", return_value={}):
                client = TestClient(app)
                for path in ("inspect-ui", "inspect-ui-rules"):
                    response = client.post("/api/checks/" + path, json={"type": "ui", "entry_url": "https://example.test", "assertions_json": "[]"})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIsInstance(response.json()["detail"], str)
                    self.assertEqual(response.json()["diagnostics"], diagnostics)
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
