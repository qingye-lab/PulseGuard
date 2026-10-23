from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from backend.app import storage, ui_assertions
from backend.app.context import RunContext, RunFailure, PreparationFailure
from backend.app.main import app
from backend.app.runner import CheckRunner
from backend.app.variables import RuntimeVariables, VariableResolutionError, mask_text, resolve_url
from backend.tests.test_ui_assertions import SetupInspectPage


class RuntimeVariableTests(unittest.TestCase):
    def test_overlay_is_local_and_keeps_falsey_values(self):
        settings = {"environment_variables": [{"name": "skuId", "value": "project"}]}
        first, second = RuntimeVariables(settings), RuntimeVariables(settings)
        first.set("skuId", 0)
        first.set("empty", "")
        first.set("flag", False)
        self.assertEqual(first.get("skuId"), "0")
        self.assertEqual(first.get("empty"), "")
        self.assertEqual(first.get("flag"), "False")
        self.assertEqual(second.get("skuId"), "project")
        self.assertEqual(settings["environment_variables"][0]["value"], "project")
        for name, value in [("bad-name", 1), ("null", None), ("object", {})]:
            with self.assertRaises(VariableResolutionError):
                first.set(name, value)

    def test_url_encodes_only_substitutions_and_only_once(self):
        variables = RuntimeVariables({})
        image = "https://images.example/a%20b.webp?size=640&name=北京+#fragment"
        variables.set("image", image)
        variables.set("skuId", "123")
        target = resolve_url("https://example/detail?source=686%2C010&sku=${skuId}&image=${image}", variables.settings)
        self.assertIn("source=686%2C010", target)
        self.assertEqual(parse_qs(urlsplit(target).query)["image"], [image])
        self.assertEqual(resolve_url(target, variables.settings), target)
        with self.assertRaisesRegex(VariableResolutionError, "missing"):
            resolve_url("https://example/?id=${missing}", variables.settings)

    def test_secret_history_masks_encoded_and_overwritten_values(self):
        variables = RuntimeVariables({})
        variables.set("id", "private/a+b&c", secret=True)
        old_url = resolve_url("https://example/?id=${id}", variables.settings)
        variables.set("id", "next-value")
        self.assertEqual(mask_text(old_url, variables.settings), "https://example/?id=***")
        self.assertEqual(variables.snapshot(), {"id": "***"})

    def test_legacy_global_url_components_keep_their_existing_semantics(self):
        settings = {"environment_variables": [{"name": "IMAGE", "value": "https%3A%2F%2Fimg.example"}]}
        self.assertEqual(resolve_url("https://example/?image=${IMAGE}", settings), "https://example/?image=https%3A%2F%2Fimg.example")


class DynamicUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_navigation_uses_secret_value_not_masked_evidence(self):
        ctx = self.context()
        async def setup(run_ctx, page):
            run_ctx.vars.set("skuId", "private-id", secret=True)
            run_ctx.vars.set("image", "image")
        try:
            page = await ui_assertions.prepare_ui_prerequisites(ctx, setup)
            self.assertNotIn("private-id", ctx.request_snapshot["url"])
            await ui_assertions.navigate_ui_target(ctx, page)
            self.assertIn("private-id", page.url)
        finally:
            await ctx.close(False)

    async def test_preparation_result_does_not_update_health_or_notify(self):
        result = {"id": 1, "status": "preparation_failed", "failure_kind": "preparation", "affects_health": False}
        with patch.object(CheckRunner, "_execute_core", new_callable=AsyncMock, return_value=result), patch(
            "backend.app.runner.storage.get_settings", return_value={}
        ), patch("backend.app.runner.storage.finish_run", return_value=result), patch(
            "backend.app.runner.storage.get_run", return_value=result
        ), patch("backend.app.runner.storage.update_run_notification"), patch(
            "backend.app.runner.storage.update_check_status"
        ) as health, patch("backend.app.runner.notifier.maybe_notify", new_callable=AsyncMock) as notify:
            await CheckRunner()._execute({"id": 1, "project_id": 1}, "manual", 1)
        health.assert_not_called()
        notify.assert_not_awaited()

    async def test_remote_preparation_status_preserves_non_observation(self):
        result = CheckRunner()._remote_result_to_finish_payload(
            {"run": {"status": "preparation_failed", "failure_kind": "preparation"}}, 1, {"runner_id": "edge"},
        )
        self.assertFalse(result["affects_health"])
        self.assertEqual(result["failure_kind"], "preparation")

    async def test_preparation_failure_is_not_a_target_observation(self):
        for script in (
            "async def setup(ctx, page):\n    raise AssertionError('login failed')",
            "async def setup(ctx, page):\n    await ctx.pause(1)",
        ):
            ctx = self.context()
            ctx.check.update({"setup_script": script, "timeout_ms": 10})
            ctx.timeout_ms = 10
            with patch("backend.app.runner.RunContext", return_value=ctx):
                result = await CheckRunner()._execute_core(ctx.check, "manual", 0, {"ui_retry_attempts": 2})
            self.assertEqual(result["status"], "preparation_failed")
            self.assertEqual(result["failure_kind"], "preparation")
            self.assertFalse(result["affects_health"])
            self.assertEqual(result["duration_ms"], 0)
            self.assertEqual(ctx.new_page.return_value.gotos, [])
            self.assertEqual(ctx.new_page.await_count, 1)
            self.assertEqual(json.loads(result["request_snapshot"])["preparation"]["status"], "failed")

    async def test_scripted_target_has_its_own_clock_after_setup(self):
        ctx = self.context()
        ctx.check.update({
            "entry_url": "https://example.com", "assertions_json": "[]", "timeout_ms": 150,
            "setup_script": "async def setup(ctx, page):\n    await ctx.pause(0.1)",
            "script": "async def check(ctx):\n    await ctx.pause(0.1)",
        })
        ctx.timeout_ms = 150
        ctx.entry_url = ctx.check["entry_url"]
        try:
            duration = await CheckRunner()._run_check_attempt(ctx, ctx.check, {"max_task_runtime_seconds": 60})
            self.assertGreaterEqual(ctx.request_snapshot["preparation"]["duration_ms"], 90)
            self.assertGreaterEqual(duration, 90)
            self.assertLess(duration, 190)
        finally:
            await ctx.close(False)

    async def test_target_timeout_after_successful_setup_remains_target_failure(self):
        ctx = self.context()
        # Windows' event-loop clock can have 15.625 ms resolution. Use a target
        # budget well above that tick while still requiring most of it to elapse.
        ctx.check.update({
            "entry_url": "https://example.com", "assertions_json": "[]", "timeout_ms": 200,
            "setup_script": "async def setup(ctx, page):\n    pass",
            "script": "async def check(ctx):\n    await ctx.pause(1)",
        })
        ctx.timeout_ms = 200
        ctx.entry_url = ctx.check["entry_url"]
        with patch("backend.app.runner.RunContext", return_value=ctx):
            result = await CheckRunner()._execute_core(ctx.check, "manual", 0, {"ui_retry_attempts": 0})
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["failure_kind"], "target")
        self.assertGreaterEqual(result["duration_ms"], 175)

    def context(self):
        check = {"type": "ui", "entry_url": "https://example.com/detail?sku=${skuId}&image=${image}",
                 "timeout_ms": 1000, "assertions_json": json.dumps([
                     {"type": "title_contains", "expected_text": "${title}"},
                     {"type": "element_visible", "selector": "${selector}"}])}
        ctx = RunContext(check, 0, {}, artifacts=None)
        ctx.pause = asyncio.sleep
        ctx.new_page = AsyncMock(return_value=SetupInspectPage())
        return ctx

    async def test_all_three_entrypoints_execute_setup_before_resolving(self):
        urls = []
        for mode in ("scan", "rules", "run"):
            ctx = self.context()
            calls = []
            async def setup(run_ctx, page):
                calls.append(page)
                run_ctx.vars.set("skuId", 321)
                run_ctx.vars.set("image", "https://img.example/a?x=1&y=2")
                run_ctx.vars.set("title", "PulseGuard")
                run_ctx.vars.set("selector", ".card-name")
            try:
                if mode == "scan":
                    await ui_assertions.inspect_ui_page(ctx.check, {}, ctx, setup)
                elif mode == "rules":
                    result = await ui_assertions.inspect_ui_rule_selectors(ctx.check, {}, ctx, setup)
                    self.assertTrue(all(item["status"] == "ok" for item in result["results"]))
                else:
                    await ui_assertions.run_structured_ui_check(ctx, setup)
                    self.assertTrue(all(item["status"] == "ok" for item in ctx.response_snapshot["assertions"]))
                page = ctx.new_page.return_value
                self.assertEqual(calls, [page])
                self.assertEqual(len(page.gotos), 1)
                urls.append(page.gotos[0])
                self.assertEqual(ctx.request_snapshot["url"], urls[-1])
                self.assertEqual(ctx.request_snapshot["preparation"]["status"], "ok")
                self.assertFalse(any("scrollTo" in script for script in page.evaluated_scripts))
            finally:
                await ctx.close(False)
        self.assertEqual(len(set(urls)), 1)

    async def test_setup_failure_and_missing_variable_never_navigate_target(self):
        for fail_setup in (False, True):
            ctx = self.context()
            async def setup(run_ctx, page):
                if fail_setup:
                    raise RunFailure("source empty")
                run_ctx.vars.set("skuId", "321")
            try:
                with self.assertRaises((RunFailure, VariableResolutionError)):
                    await ui_assertions.prepare_ui_target(ctx, setup)
                self.assertEqual(ctx.new_page.return_value.gotos, [])
                self.assertEqual(ctx.request_snapshot["preparation"]["status"], "failed")
            finally:
                await ctx.close(False)

    async def test_concurrent_contexts_are_isolated(self):
        async def run(value):
            ctx = self.context()
            async def setup(run_ctx, page):
                run_ctx.vars.set("skuId", value)
                await asyncio.sleep(0)
                run_ctx.vars.set("image", "image")
            try:
                await ui_assertions.prepare_ui_target(ctx, setup)
                return parse_qs(urlsplit(ctx.entry_url).query)["sku"][0]
            finally:
                await ctx.close(False)
        self.assertEqual(await asyncio.gather(run("first"), run("second")), ["first", "second"])

    async def test_script_loader_exposes_vars_without_imports(self):
        ctx = self.context()
        runner = object.__new__(CheckRunner)
        setup = runner._load_check_function(
            "async def setup(ctx, page):\n    ctx.vars.set('skuId', 7)\n    ctx.vars.set('image', 'raw')\n",
            ctx, function_name="setup", expected_signature="async def setup(ctx, page)")
        try:
            await ui_assertions.prepare_ui_target(ctx, setup)
            self.assertIn("sku=7", ctx.entry_url)
        finally:
            await ctx.close(False)

    async def test_setup_timeout_cancels_before_navigation(self):
        ctx = self.context()
        ctx.timeout_ms = 1
        async def setup(run_ctx, page):
            await asyncio.Event().wait()
        try:
            with self.assertRaises(PreparationFailure):
                await ui_assertions.prepare_ui_target(ctx, setup)
            self.assertEqual(ctx.new_page.return_value.gotos, [])
            self.assertEqual(ctx.request_snapshot["preparation"]["status"], "failed")
        finally:
            await ctx.close(False)

    async def test_disabled_selectors_do_not_resolve_missing_variables(self):
        result = await ui_assertions.inspect_ui_selector_rules(json.dumps([
            {"type": "element_visible", "selector": "${unused}", "enabled": False},
        ]), SetupInspectPage(), {})
        self.assertEqual(result[0]["status"], "disabled")


class ProjectVariableTests(unittest.TestCase):
    def test_existing_database_adds_variable_table_without_changing_checks_or_settings(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, patch.object(storage, "DB_PATH", Path(folder) / "db.sqlite"):
            storage.init_db()
            with storage._connect() as conn:
                conn.execute("DROP TABLE project_variables")
                before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in ("checks", "settings", "projects")}
            storage.init_db()
            with storage._connect(read_only=True) as conn:
                after = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in before}
            self.assertEqual(after, before)
            self.assertEqual(storage.get_project_variables(storage.default_project_id()), [])

    def test_project_bundle_roundtrip_and_old_bundle_retains_variables(self):
        from backend.app import config_transfer
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, patch.object(storage, "DB_PATH", Path(folder) / "db.sqlite"):
            storage.init_db()
            first = storage.default_project_id()
            second = storage.create_project("Import vars")["id"]
            storage.update_project_variables(first, [{"id": "one", "name": "IMAGE", "value": "https://img/a?x=1&y=2"}])
            bundle = config_transfer.export_project(first, redact=False)
            bundle["checks"] = []
            config_transfer.apply_project_import(bundle, second)
            self.assertEqual(storage.get_project_variables(first), storage.get_project_variables(second))
            settings = storage.settings_for_project({}, second)
            self.assertEqual(parse_qs(urlsplit(resolve_url("https://example/?image=${IMAGE}", settings)).query)["image"], ["https://img/a?x=1&y=2"])
            self.assertTrue(CheckRunner._worker_settings(None, settings)["environment_variables"][0]["url_encode"])
            del bundle["variables"]
            config_transfer.apply_project_import(bundle, second)
            self.assertEqual(len(storage.get_project_variables(second)), 1)

    def test_persistence_scope_secret_preservation_and_migration(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, patch.object(storage, "DB_PATH", Path(folder) / "db.sqlite"):
            storage.init_db()
            first = storage.default_project_id()
            second = storage.create_project("Other vars")["id"]
            value = {"id": "one", "name": "skuId", "value": "project-value", "secret": True}
            self.assertEqual(storage.update_project_variables(first, [value])[0]["value"], "")
            storage.update_project_variables(first, [{**value, "value": ""}])
            self.assertEqual(storage.get_project_variables(first)[0]["value"], "project-value")
            self.assertEqual(storage.get_project_variables(second), [])
            settings = {"environment_variables": [{"name": "skuId", "value": "global"}]}
            self.assertEqual(RuntimeVariables(storage.settings_for_project(settings, first)).get("skuId"), "project-value")
            self.assertEqual(RuntimeVariables(storage.settings_for_project(settings, second)).get("skuId"), "global")
            storage.init_db()
            self.assertEqual(storage.get_project_variables(first)[0]["value"], "project-value")
            storage.archive_project(second)
            with self.assertRaisesRegex(ValueError, "归档"):
                storage.update_project_variables(second, [value])

    def test_project_api_uses_header_scope(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, patch.object(storage, "DB_PATH", Path(folder) / "db.sqlite"):
            storage.init_db()
            first = storage.default_project_id()
            second = storage.create_project("API vars")["id"]
            client = TestClient(app)
            endpoint = "/api/project-variables"
            response = client.put(endpoint, headers={"X-PulseGuard-Project-Id": str(first)}, json=[
                {"id": "id", "name": "skuId", "value": "123"}])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get(endpoint, headers={"X-PulseGuard-Project-Id": str(second)}).json(), [])
            self.assertEqual(client.get(endpoint, headers={"X-PulseGuard-Project-Id": str(first)}).json()[0]["value"], "123")


if __name__ == "__main__":
    unittest.main()
