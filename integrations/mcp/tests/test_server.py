from __future__ import annotations

import asyncio
import base64
import json
import unittest
from dataclasses import replace

import httpx
from mcp import Client

from pulseguard_mcp.client import PulseGuardClient
from pulseguard_mcp.config import Settings
from pulseguard_mcp.output import sanitize
from pulseguard_mcp.server import create_server


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
SETTINGS = Settings("http://127.0.0.1:8787", frozenset({1}))


class SettingsTests(unittest.TestCase):
    def test_requires_explicit_project_scope(self):
        with self.assertRaisesRegex(ValueError, "PROJECT_IDS"):
            Settings.from_env({})
        settings = Settings.from_env({"PULSEGUARD_MCP_PROJECT_IDS": "1, 2,1"})
        self.assertEqual(settings.project_ids, frozenset({1, 2}))
        self.assertFalse(settings.allow_execution)
        self.assertIsNone(Settings.from_env({"PULSEGUARD_MCP_PROJECT_IDS": "*"}).project_ids)

    def test_invalid_configuration_fails_closed(self):
        for key, value in (
            ("PROJECT_IDS", "0"), ("PROJECT_IDS", "1,"), ("PROJECT_IDS", "-1"),
            ("ALLOW_EXECUTION", "yes"), ("EXECUTION_TIMEOUT", "0"), ("REQUEST_TIMEOUT", "nan"),
            ("BASE_URL", "http://user:secret@example.test"), ("BASE_URL", "http://example.test/?token=abc"),
            ("BASE_URL", "file:///tmp/data"), ("BASE_URL", "http://example.test:bad"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Settings.from_env({"PULSEGUARD_MCP_PROJECT_IDS": "1", f"PULSEGUARD_MCP_{key}": value})

    def test_gateway_token_is_not_an_insecure_remote_or_log_credential(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            Settings.from_env({"PULSEGUARD_MCP_PROJECT_IDS": "1", "PULSEGUARD_MCP_BASE_URL": "http://lan.test", "PULSEGUARD_MCP_API_TOKEN": "private"})
        settings = Settings.from_env({"PULSEGUARD_MCP_PROJECT_IDS": "1", "PULSEGUARD_MCP_BASE_URL": "https://example.test/pulseguard/", "PULSEGUARD_MCP_API_TOKEN": "private"})
        self.assertNotIn("private", repr(settings))
        self.assertEqual(settings.base_url, "https://example.test/pulseguard")

    def test_output_reduction_covers_queries_credentials_nested_values_and_size(self):
        result = sanitize({
            "url": "https://user:pass@target.test/api?access=secret#private",
            "authorization": "Bearer private", "nested": {"api_key": "private"},
            "logs": "Authorization: Bearer private\npassword=secret\n" + "x" * 5000,
        }, token="private")
        text = json.dumps(result)
        self.assertNotIn("private", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("user:pass", text)
        self.assertIn("truncated", result["logs"])

    def test_sensitive_assertion_paths_hide_compared_values(self):
        result = sanitize({"assertions": [{"path": "$.access_token", "current_actual": "private", "baseline_actual": "old-private"}]})
        self.assertNotIn("private", str(result))


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, name, args=None, *, handler=None, settings=SETTINGS):
        requests = []

        def capture(request):
            requests.append(request)
            return handler(request) if handler else httpx.Response(200, json={"status": "ok"})

        api = PulseGuardClient(settings, httpx.MockTransport(capture))
        async with Client(create_server(settings, api), raise_exceptions=True) as client:
            result = await client.call_tool(name, args or {})
        return result, requests

    async def test_discovery_hides_execution_and_marks_mutation_accurately(self):
        for enabled in (False, True):
            settings = replace(SETTINGS, allow_execution=enabled)
            async with Client(create_server(settings), raise_exceptions=True) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual("run_check" in tools, enabled)
            self.assertEqual(len(tools), 19 if enabled else 15)
            self.assertTrue(tools["get_run"].annotations.read_only_hint)
            self.assertIn("project_id", tools["get_run"].input_schema["required"])
            if enabled:
                self.assertFalse(tools["run_check"].annotations.read_only_hint)
                self.assertFalse(tools["run_check"].annotations.idempotent_hint)
                self.assertTrue(tools["run_check"].annotations.open_world_hint)

    async def test_health_is_service_health_and_projects_are_scoped(self):
        result, _ = await self.invoke("health")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["project_scope"], [1])
        result, _ = await self.invoke("list_projects", handler=lambda _: httpx.Response(200, json=[
            {"id": 1, "name": "Visible", "status": "active"}, {"id": 2, "name": "Hidden"},
        ]))
        self.assertEqual(result.structured_content["total"], 1)
        self.assertNotIn("Hidden", str(result))

    async def test_project_list_can_reach_later_pages(self):
        result, _ = await self.invoke("list_projects", {"page": 2, "page_size": 1},
            settings=replace(SETTINGS, project_ids=None), handler=lambda _: httpx.Response(200, json=[
                {"id": 1, "name": "First"}, {"id": 2, "name": "Second"},
            ]))
        self.assertEqual(result.structured_content["total"], 2)
        self.assertEqual(result.structured_content["items"][0]["id"], 2)

    async def test_invalid_or_out_of_scope_inputs_do_not_call_api(self):
        for args in ({"project_id": 2, "check_id": 10}, {"project_id": 0, "check_id": 10},
                     {"project_id": True, "check_id": 10}, {"check_id": 10}):
            with self.subTest(args=args):
                result, requests = await self.invoke("get_check", args)
                self.assertTrue(result.is_error)
                self.assertEqual(requests, [])

    async def test_check_filter_pagination_and_safe_metadata(self):
        checks = [
            {"id": 10, "project_id": 1, "name": "A", "tags": "SMOKE,prod", "enabled": True, "script": "secret"},
            {"id": 11, "project_id": 1, "name": "B", "tags": "smoketest", "enabled": True},
            {"id": 12, "project_id": 1, "name": "C", "tags": "smoke", "enabled": False},
        ]
        result, requests = await self.invoke("list_checks", {"project_id": 1, "tag": "smoke", "enabled_only": True, "page_size": 1},
                                             handler=lambda _: httpx.Response(200, json=checks))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["total"], 1)
        self.assertEqual(result.structured_content["items"][0]["id"], 10)
        self.assertNotIn("script", result.structured_content["items"][0])
        self.assertEqual(requests[0].headers["X-PulseGuard-Project-Id"], "1")
        bad, requests = await self.invoke("list_checks", {"project_id": 1, "tag": "a b"})
        self.assertTrue(bad.is_error)
        self.assertEqual(requests, [])

    async def test_rejects_upstream_cross_project_resource(self):
        result, _ = await self.invoke("get_check", {"project_id": 1, "check_id": 10},
                                      handler=lambda _: httpx.Response(200, json={"id": 10, "project_id": 2, "name": "hidden"}))
        self.assertTrue(result.is_error)
        self.assertNotIn("hidden", str(result))

    async def test_run_pagination_and_optional_logs(self):
        run = {"id": 7, "project_id": 1, "check_id": 10, "status": "failed", "logs": "Authorization: private", "response_snapshot": "raw secret"}
        result, requests = await self.invoke("list_runs", {"project_id": 1, "check_id": 10, "status": "failed", "page": 2},
                                             handler=lambda _: httpx.Response(200, json={"items": [run], "page": 2, "page_size": 20, "total": 21}))
        self.assertEqual(result.structured_content["total"], 21)
        self.assertEqual(requests[0].url.params["status"], "failed")
        self.assertNotIn("logs", result.structured_content["items"][0])
        for logs in (False, True):
            result, _ = await self.invoke("get_run", {"project_id": 1, "run_id": 7, "include_logs": logs},
                                          handler=lambda _: httpx.Response(200, json=run))
            self.assertEqual("logs" in result.structured_content, logs)
            self.assertNotIn("raw secret", str(result))
            self.assertNotIn("private", str(result))

    async def test_failure_summary_and_absent_baseline(self):
        run = {"id": 7, "project_id": 1, "check_id": 10, "status": "failed"}
        for name, suffix, payload in (
            ("get_failure_summary", "failure-summary", {"run_id": 7, "failure_kind": "runner", "summary": "Runner failed"}),
            ("compare_success", "compare-success", {"available": False, "message": "No successful baseline"}),
        ):
            result, requests = await self.invoke(name, {"project_id": 1, "run_id": 7}, handler=lambda req: httpx.Response(
                200, json=payload if req.url.path.endswith(suffix) else run))
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, payload)
            self.assertTrue(all(req.headers["X-PulseGuard-Project-Id"] == "1" for req in requests))

    async def test_screenshot_is_loaded_from_owned_run(self):
        def handler(req):
            if req.url.path.startswith("/artifacts/"):
                return httpx.Response(200, content=PNG)
            return httpx.Response(200, json={"id": 7, "project_id": 1, "screenshot_path": "screenshots/run-7-test.png"})
        result, requests = await self.invoke("get_screenshot", {"project_id": 1, "run_id": 7}, handler=handler)
        self.assertFalse(result.is_error)
        self.assertEqual(base64.b64decode(result.content[0].data), PNG)
        self.assertEqual(requests[-1].url.path, "/artifacts/screenshots/run-7-test.png")

    async def test_screenshot_never_fetches_arbitrary_url_or_traversal(self):
        for path in ("https://evil.test/image.png", "screenshots/../secret.png", "screenshots/%2e%2e.png", ""):
            result, requests = await self.invoke("get_screenshot", {"project_id": 1, "run_id": 7},
                handler=lambda _: httpx.Response(200, json={"id": 7, "project_id": 1, "screenshot_path": path}))
            self.assertTrue(result.is_error)
            self.assertEqual(len(requests), 1)

    async def test_execution_disabled_cannot_be_invoked(self):
        result, requests = await self.invoke("run_check", {"project_id": 1, "check_id": 10})
        self.assertTrue(result.is_error)
        self.assertEqual(requests, [])

    async def test_independent_platform_execution_and_configuration_capabilities(self):
        for execution, configuration, platform in ((False, False, False), (True, False, False),
                                                    (False, True, False), (False, False, True)):
            settings = replace(SETTINGS, allow_execution=execution, allow_configuration=configuration, allow_platform_read=platform)
            async with Client(create_server(settings), raise_exceptions=True) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertEqual("set_check_enabled" in names, configuration)
            self.assertEqual("confirm_recovery" in names, execution)
            self.assertEqual("list_runners" in names, platform)
            self.assertEqual("get_platform_status" in names, platform)

    async def test_expanded_filters_and_retained_record_warning(self):
        result, requests = await self.invoke("list_runs", {
            "project_id": 1, "check_type": "api", "status": "preparation_failed",
            "start": "2026-07-01T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00",
            "runner_id": "edge", "observation_kind": "observation", "trigger": "scheduled", "run_group_id": "rg-1",
        }, handler=lambda _: httpx.Response(200, json={"items": [], "page": 1, "page_size": 20, "total": 0}))
        self.assertFalse(result.is_error)
        self.assertEqual(requests[0].url.params["type"], "api")
        self.assertEqual(requests[0].url.params["runner_id"], "edge")
        self.assertEqual(result.structured_content["coverage"], "retained_records_only")
        invalid, calls = await self.invoke("list_runs", {"project_id": 1, "start": "2026-07-01"})
        self.assertTrue(invalid.is_error)
        self.assertEqual(calls, [])

    async def test_historical_statistics_preserve_incompleteness_and_scope(self):
        args = {"project_id": 1, "start": "2026-07-01T00:00:00+08:00", "end": "2026-10-01T00:00:00+08:00"}
        payload = {"project_id": 1, "total": 12, "sources": {"retained": 2, "archived": 10},
                   "coverage": {"complete": False}, "warnings": ["Legacy gap"]}
        result, calls = await self.invoke("get_run_statistics", args, handler=lambda _: httpx.Response(200, json=payload))
        self.assertEqual(result.structured_content, payload)
        self.assertEqual(calls[0].url.path, "/api/run-statistics")
        denied, calls = await self.invoke("get_run_statistics", {**args, "project_id": 2})
        self.assertTrue(denied.is_error)
        self.assertEqual(calls, [])
        crossed, _ = await self.invoke("get_run_statistics", args, handler=lambda _: httpx.Response(200, json={**payload, "project_id": 2}))
        self.assertTrue(crossed.is_error)

    async def test_configuration_cannot_be_enabled_by_execution_permission(self):
        args = {"project_id": 1, "check_id": 10, "enabled": False}
        denied, calls = await self.invoke("set_check_enabled", args, settings=replace(SETTINGS, allow_execution=True))
        self.assertTrue(denied.is_error)
        self.assertEqual(calls, [])
        result, calls = await self.invoke("set_check_enabled", args, settings=replace(SETTINGS, allow_configuration=True),
            handler=lambda _: httpx.Response(200, json={"id": 10, "project_id": 1, "enabled": False, "script": "private"}))
        self.assertFalse(result.is_error)
        self.assertEqual(calls[0].url.path, "/api/checks/10/disable")
        self.assertNotIn("private", str(result))
        failed, calls = await self.invoke("set_check_enabled", args, settings=replace(SETTINGS, allow_configuration=True),
            handler=lambda _: httpx.Response(503, json={}))
        self.assertTrue(failed.is_error)
        self.assertIn("Configuration outcome unknown", str(failed))
        self.assertEqual(len(calls), 1)

    async def test_new_read_outputs_do_not_expose_global_audit_or_runner_secrets(self):
        events = [{"id": 1, "project_id": None, "summary": "global-private"},
                  {"id": 2, "project_id": 1, "summary": "changed", "payload": "raw-private"}]
        result, _ = await self.invoke("list_audit_events", {"project_id": 1}, handler=lambda _: httpx.Response(200, json=events))
        self.assertEqual(len(result.structured_content["items"]), 1)
        self.assertNotIn("private", str(result))
        result, _ = await self.invoke("list_runners", settings=replace(SETTINGS, allow_platform_read=True),
            handler=lambda _: httpx.Response(200, json=[{"id": "local", "available": True,
                "available_browser_types": ["chromium"], "token": "private", "metadata": {"command": "private"}}]))
        self.assertTrue(result.structured_content["items"][0]["available"])
        self.assertNotIn("private", str(result))

    async def test_execution_distinguishes_probe_failure_from_tool_failure(self):
        for status in ("ok", "failed", "skipped", "timeout"):
            result, requests = await self.invoke("run_check", {"project_id": 1, "check_id": 10},
                settings=replace(SETTINGS, allow_execution=True), handler=lambda _: httpx.Response(200, json={
                    "id": 7, "project_id": 1, "check_id": 10, "status": status, "affects_health": True,
                }))
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["passed"], status == "ok")
            self.assertEqual(requests[0].method, "POST")
            self.assertEqual(len(requests), 1)

    async def test_unknown_execution_outcome_never_retries_or_leaks_transport_details(self):
        def timeout(req):
            raise httpx.ReadTimeout("private endpoint and credentials", request=req)
        for handler in (timeout, lambda _: httpx.Response(502, text="private"),
                        lambda _: httpx.Response(202, json={"accepted": True}),
                        lambda _: httpx.Response(200, text="not json private")):
            result, requests = await self.invoke("run_check", {"project_id": 1, "check_id": 10},
                settings=replace(SETTINGS, allow_execution=True), handler=handler)
            self.assertTrue(result.is_error)
            self.assertIn("outcome unknown", str(result))
            self.assertNotIn("private", str(result))
            self.assertEqual(len(requests), 1)

    async def test_redirects_and_api_errors_do_not_leak_credentials(self):
        settings = replace(SETTINGS, base_url="https://gateway.test/pulseguard", api_token="private")
        for code in (302, 401, 403, 404, 409):
            result, requests = await self.invoke("get_check", {"project_id": 1, "check_id": 10}, settings=settings,
                handler=lambda _: httpx.Response(code, headers={"Location": "https://evil.test"}, text="private"))
            self.assertTrue(result.is_error)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.path, "/pulseguard/api/checks/10")
            self.assertEqual(requests[0].headers["Authorization"], "Bearer private")
            self.assertNotIn("private", str(result))

    async def test_response_limit_is_an_error_not_truncated_success(self):
        result, _ = await self.invoke("health", handler=lambda _: httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)))
        self.assertTrue(result.is_error)
        self.assertIn("8 MiB", str(result))

    async def test_cancellation_propagates_without_retry(self):
        started = asyncio.Event()
        calls = []
        async def handler(req):
            calls.append(req)
            started.set()
            await asyncio.Event().wait()
        api = PulseGuardClient(replace(SETTINGS, allow_execution=True), httpx.MockTransport(handler))
        task = asyncio.create_task(api.request("POST", "/api/checks/10/run", project_id=1))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
