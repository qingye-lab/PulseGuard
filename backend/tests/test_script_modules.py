from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from backend.app.api_assertions import api_inspection_diagnostics
from backend.app.context import RunFailure, compile_check_function
from backend.app.script_modules import (
    ModuleBundleError,
    ModuleRuntime,
    analyze_script,
    bundle_content_hash,
    capture_module_bundle,
    compile_script_entry,
    module_metadata,
    normalize_module_bundle,
    script_virtual_filename,
    source_hash,
    validate_module_source,
)
from backend.app.ui_diagnostics import inspection_diagnostics


def _bundle(*, project_id: int = 1, release_id: int | None = None, release_hash: str | None = None, modules: list[dict] | None = None) -> dict:
    return capture_module_bundle(project_id, release_id, modules or [], release_hash=release_hash)


class ScriptModuleSourceTests(unittest.TestCase):
    def test_compile_errors_are_rejected_without_executing_source(self) -> None:
        for source in (
            "def broken():\n    break\n",
            "def broken():\n    continue\n",
            "def broken():\n    await asyncio.sleep(0)\n",
            "def broken():\n    nonlocal missing\n",
        ):
            with self.subTest(source=source):
                result = validate_module_source(source, name="broken")
                self.assertFalse(result["valid"])
                diagnostic = result["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "syntax_error")
                self.assertEqual(diagnostic["line"], 2)
                self.assertEqual(diagnostic["filename"], result["filename"])
        self.assertTrue(validate_module_source("def later():\n    raise ValueError('only on call')\n")["valid"])

    def test_validation_derives_one_export_metadata_shape(self) -> None:
        source = '''"""时间工具。"""
VALUE = {"unit": "ms"}

async def fetch(ctx, item, *, wait=1):
    """读取一项并返回。"""
    return item

__all__ = ["VALUE", "fetch"]
'''
        result = validate_module_source(source, name="time_tools")

        self.assertTrue(result["valid"])
        self.assertEqual(result["exports"], ["VALUE", "fetch"])
        metadata = {item["name"]: item for item in result["export_metadata"]}
        self.assertEqual(metadata["VALUE"]["kind"], "constant")
        self.assertEqual(metadata["fetch"]["kind"], "async")
        self.assertEqual(metadata["fetch"]["positional"], ["ctx", "item"])
        self.assertEqual(metadata["fetch"]["required_positional"], 2)
        self.assertEqual(metadata["fetch"]["keyword_only"], ["wait"])
        self.assertEqual(metadata["fetch"]["required_keyword_only"], [])
        self.assertEqual(metadata["fetch"]["docstring"], "读取一项并返回。")
        self.assertIn("fetch(", metadata["fetch"]["signature"])

    def test_module_load_policy_rejects_calls_imports_and_mutating_targets(self) -> None:
        source = '''import json
ready = make_ready()
config.value = 1
config["value"] = 2

@decorate()
def decorated():
    return 1

def default(value=make_default()):
    return value

def annotated(value: make_annotation()):
    return value
'''
        result = validate_module_source(source, name="bad")
        codes = {item["code"] for item in result["diagnostics"]}

        self.assertFalse(result["valid"])
        self.assertTrue({"import_forbidden", "top_level_call", "top_level_assignment", "load_side_effect"} <= codes)

    def test_module_cannot_reference_another_module(self) -> None:
        result = validate_module_source(
            'def helper(ctx):\n    return ctx.modules.use("other")\n',
            name="one",
        )

        self.assertFalse(result["valid"])
        self.assertTrue(any(item["code"] == "cross_module_reference" for item in result["diagnostics"]))

    def test_bundle_hashes_use_normalized_source_and_positive_release_id(self) -> None:
        source = "VALUE = 1\r\n"
        module_hash = source_hash(source)
        bundle = _bundle(
            project_id=4,
            release_id=9,
            release_hash="A" * 64,
            modules=[{"name": "one", "source": source, "hash": module_hash, "description": "说明"}],
        )

        self.assertEqual(bundle["modules"][0]["source"], "VALUE = 1\n")
        self.assertEqual(bundle["release_id"], 9)
        self.assertEqual(bundle["release_hash"], "a" * 64)
        self.assertEqual(bundle["content_hash"], bundle_content_hash(bundle["modules"]))
        self.assertEqual(module_metadata(bundle)["module_hashes"], {"one": module_hash})
        with self.assertRaises(ModuleBundleError):
            normalize_module_bundle({**bundle, "release_id": True})
        with self.assertRaises(ModuleBundleError):
            normalize_module_bundle({**bundle, "content_hash": "0" * 64})


class ScriptModuleStaticCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = _bundle(
            modules=[
                {
                    "name": "one",
                    "source": '''async def get(ctx, value):
    return value
''',
                }
            ]
        )

    def _codes(self, source: str) -> set[str]:
        return {item["code"] for item in analyze_script(source, module_bundle=self.bundle)["diagnostics"]}

    def test_direct_and_local_alias_calls_get_definite_export_and_argument_checks(self) -> None:
        missing_export = self._codes('''async def check(ctx):
    ctx.modules.use("one").missing()
''')
        missing_argument = self._codes('''async def check(ctx):
    module = ctx.modules.use("one")
    module.get()
''')
        dynamic_name = self._codes('''async def check(ctx):
    name = "one"
    ctx.modules.use(name)
''')

        self.assertIn("module_export_missing", missing_export)
        self.assertIn("module_call_arguments", missing_argument)
        self.assertIn("dynamic_module_name", dynamic_name)

    def test_kwargs_and_starred_calls_are_left_for_runtime_binding(self) -> None:
        codes = self._codes('''async def check(ctx):
    module = ctx.modules.use("one")
    values = [ctx.value]
    await module.get(**ctx.kwargs)
    await module.get(*values)
''')

        self.assertNotIn("module_call_arguments", codes)

    def test_keyword_binding_satisfies_required_positional_parameter(self) -> None:
        codes = self._codes('''async def check(ctx):
    await ctx.modules.use("one").get(ctx=ctx, value="ok")
''')

        self.assertNotIn("module_call_arguments", codes)

    def test_alias_analysis_does_not_cross_parameter_shadow_reassignment_or_branch(self) -> None:
        source = '''async def check(ctx):
    module = ctx.modules.use("one")
    def helper(module):
        return module.missing()
    if ctx.branch:
        module = ctx.other
    return module.missing()
'''
        branch_source = '''async def check(ctx):
    if ctx.branch:
        module = ctx.modules.use("one")
        return module.missing()
'''
        codes = self._codes(source)
        branch_codes = self._codes(branch_source)

        self.assertNotIn("module_export_missing", codes)
        self.assertIn("module_export_missing", branch_codes)

    def test_diagnostics_keep_virtual_filename_and_line(self) -> None:
        filename = script_virtual_filename("check", check_id=8, attempt=3)
        result = analyze_script(
            'async def check(ctx):\n    ctx.modules.use("one").missing()\n',
            module_bundle=self.bundle,
            filename=filename,
        )
        diagnostic = next(item for item in result["diagnostics"] if item["code"] == "module_export_missing")

        self.assertEqual(diagnostic["filename"], filename)
        self.assertEqual(diagnostic["line"], 2)


class ScriptModuleRuntimeTests(unittest.TestCase):
    def test_supported_stdlib_names_and_helper_functions_run_in_one_namespace(self) -> None:
        bundle = _bundle(
            modules=[
                {
                    "name": "runtime",
                    "source": '''async def transform(value):
    await asyncio.sleep(0)
    return json.loads(json.dumps({"value": value}))
''',
                }
            ]
        )
        ctx = SimpleNamespace(project_id=1, original_check_id=2, modules=ModuleRuntime(bundle))
        script = '''async def helper(ctx):
    module = ctx.modules.use("runtime")
    return await module.transform("ok")

async def check(ctx):
    ctx.result = await helper(ctx)
'''
        function = compile_script_entry(script, ctx, modules=ctx.modules, check_id=2, attempt=1)

        asyncio.run(function(ctx))
        self.assertEqual(ctx.result, {"value": "ok"})

    def test_setup_and_check_share_attempt_runtime_but_attempts_are_isolated(self) -> None:
        bundle = _bundle(
            modules=[
                {
                    "name": "counter",
                    "source": '''values = []
def push(value):
    values.append(value)
    return len(values)
''',
                }
            ]
        )
        ctx = SimpleNamespace(project_id=1, original_check_id=3, modules=ModuleRuntime(bundle))
        setup = compile_script_entry(
            'async def setup(ctx, page):\n    ctx.modules.use("counter").push("setup")\n',
            ctx,
            function_name="setup",
            expected_signature="async def setup(ctx, page)",
            modules=ctx.modules,
            attempt=1,
        )
        check = compile_script_entry(
            'async def check(ctx):\n    ctx.result = ctx.modules.use("counter").push("check")\n',
            ctx,
            modules=ctx.modules,
            attempt=1,
        )
        asyncio.run(setup(ctx, None))
        asyncio.run(check(ctx))
        self.assertEqual(ctx.result, 2)

        second_ctx = SimpleNamespace(project_id=1, original_check_id=3, modules=ModuleRuntime(bundle))
        second_check = compile_script_entry(
            'async def check(ctx):\n    ctx.result = ctx.modules.use("counter").push("second")\n',
            second_ctx,
            modules=second_ctx.modules,
            attempt=2,
        )
        asyncio.run(second_check(second_ctx))
        self.assertEqual(second_ctx.result, 1)

    def test_compile_failure_is_reported_with_attempt_owned_diagnostics(self) -> None:
        bundle = _bundle(
            modules=[{"name": "one", "source": "VALUE = 1\n"}],
        )
        ctx = SimpleNamespace(project_id=1, original_check_id=12, modules=ModuleRuntime(bundle), compile_diagnostics=[])

        with self.assertRaises(RunFailure):
            compile_check_function(
                'async def check(ctx):\n    ctx.modules.use("one").missing()\n',
                ctx,
                module_bundle=bundle,
                attempt=4,
            )

        self.assertEqual(ctx.compile_diagnostics[0]["line"], 2)
        self.assertIn("task-12", ctx.compile_diagnostics[0]["filename"])
        self.assertIn("attempt-4", ctx.compile_diagnostics[0]["filename"])

    def test_inspection_paths_expose_only_canonical_module_metadata(self) -> None:
        bundle = _bundle(project_id=1, release_id=5, modules=[{"name": "one", "source": "VALUE = 1\n"}])
        ctx = SimpleNamespace(
            settings={},
            check={"entry_url": "https://example.test"},
            vars=SimpleNamespace(snapshot=lambda: {}),
            request_snapshot=None,
            response_snapshot=None,
            http_exchanges=[],
            logs=[],
            api_stage="prepare",
            ui_stage="prepare",
            last_request_duration_ms=None,
            task_duration_ms=None,
            library_metadata=module_metadata(bundle),
            compile_diagnostics=[],
        )

        api = api_inspection_diagnostics(ctx)
        ui = inspection_diagnostics(ctx)
        self.assertEqual(api["modules"], ui["modules"])
        self.assertEqual(api["modules"]["module_hashes"], {"one": source_hash("VALUE = 1\n")})
        self.assertEqual(api["compile"], [])
        self.assertNotIn("modules", api["modules"])
