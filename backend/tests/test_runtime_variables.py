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
from backend.app.context import RunContext, RunFailure
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
    def context(self):
        check = {"type": "ui", "entry_url": "https://example.com/detail?sku=${skuId}&image=${image}",
                 "timeout_ms": 1000, "assertions_json": json.dumps([
                     {"type": "title_contains", "expected_text": "${title}"},
                     {"type": "element_visible", "selector": "${selector}"}])}
        ctx = RunContext(check, 0, {}, artifacts=None)
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
            with self.assertRaises(asyncio.TimeoutError):
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
