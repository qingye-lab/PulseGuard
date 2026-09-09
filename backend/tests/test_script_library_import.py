from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import config_transfer, storage


def module_payload(name: str, source: str) -> dict[str, str]:
    return {"name": name, "source": source, "description": f"{name} for import tests"}


def project_bundle(
    *,
    modules: list[dict[str, str]] | None = None,
    checks: list[dict[str, object]] | None = None,
    version: int = config_transfer.PROJECT_EXPORT_SCHEMA_VERSION,
) -> dict[str, object]:
    library = None
    if modules is not None:
        library = {"published": {"revision": 1, "modules": modules}}
    bundle: dict[str, object] = {
        "schema": "pulseguard.project",
        "version": version,
        "exported_at": "2026-09-09T00:00:00+08:00",
        "project": {"name": "Imported project"},
        "alerts": {},
        "checks": checks or [],
    }
    if library is not None:
        bundle["library"] = library
    return bundle


def api_check(name: str, script: str = "") -> dict[str, object]:
    return {
        "name": name,
        "type": "api",
        "enabled": True,
        "interval_seconds": 60,
        "timeout_ms": 5000,
        "entry_url": "https://import.example.test/health",
        "viewport_mode": "web",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": json.dumps(
            [{"id": "status", "type": "status_code", "enabled": True, "expected_status": 200}]
        ),
        "setup_script": "",
        "script": script,
        "tags": "import-test",
        "alert_policy_json": "{}",
    }


class ScriptLibraryImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp_dir.cleanup)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        storage.init_db()
        self.target_id = storage.default_project_id()

    def _published(self, project_id: int) -> dict[str, object] | None:
        return storage.get_project_library(project_id).get("published")  # type: ignore[return-value]

    def test_import_keeps_existing_draft_separate_from_imported_publication(self) -> None:
        base = module_payload("base", "def value():\n    return 'base'\n")
        draft_only = module_payload("draft_only", "def value():\n    return 'local draft'\n")
        incoming = module_payload("incoming", "def value():\n    return 'imported'\n")

        storage.update_project_library_draft(self.target_id, 0, [base])
        storage.publish_project_library(self.target_id, 1)
        storage.update_project_library_draft(self.target_id, 1, [base, draft_only])

        result = config_transfer.apply_project_import(
            project_bundle(modules=[incoming]),
            self.target_id,
        )

        self.assertTrue(result["ok"])
        library = storage.get_project_library(self.target_id)
        published_names = {item["name"] for item in library["published"]["modules"]}
        draft_names = {item["name"] for item in library["modules"]}
        self.assertEqual(published_names, {"base", "incoming"})
        self.assertEqual(draft_names, {"base", "draft_only", "incoming"})
        self.assertNotIn("draft_only", published_names)

    def test_import_reuses_same_source_and_rejects_same_name_different_source(self) -> None:
        original = module_payload("shared", "def verify(value):\n    return value\n")
        storage.update_project_library_draft(self.target_id, 0, [original])
        first = storage.publish_project_library(self.target_id, 1)
        before = storage.get_project_library(self.target_id)
        before_versions = storage.list_project_library_versions(self.target_id)

        reused = config_transfer.apply_project_import(
            project_bundle(modules=[original]),
            self.target_id,
        )

        self.assertEqual(reused["library"]["version_id"], first["version_id"])
        after_reuse = storage.get_project_library(self.target_id)
        self.assertEqual(after_reuse["published"]["version_id"], before["published"]["version_id"])
        self.assertEqual(storage.list_project_library_versions(self.target_id), before_versions)
        self.assertEqual(len(after_reuse["published"]["modules"]), 1)

        conflict = module_payload("shared", "def verify(value):\n    return 'other'\n")
        preview = config_transfer.preview_project_import(
            project_bundle(modules=[conflict]),
            self.target_id,
        )
        self.assertFalse(preview["valid"])
        self.assertIn("shared", "；".join(preview["issues"]))
        with self.assertRaisesRegex(ValueError, "导入预检失败"):
            config_transfer.apply_project_import(project_bundle(modules=[conflict]), self.target_id)
        self.assertEqual(storage.get_project_library(self.target_id), after_reuse)

    def test_project_import_accepts_legacy_v1_bundle_without_library(self) -> None:
        legacy = project_bundle(
            version=1,
            checks=[api_check("Legacy task")],
        )

        preview = config_transfer.preview_project_import(legacy, self.target_id)
        self.assertTrue(preview["valid"])
        result = config_transfer.apply_project_import(legacy, self.target_id)

        self.assertEqual(result["checks_created"], 1)
        self.assertIsNotNone(
            next(
                (check for check in storage.list_checks(project_id=self.target_id) if check["name"] == "Legacy task"),
                None,
            )
        )
        self.assertIsNone(self._published(self.target_id))

    def test_import_preview_rejects_missing_module_and_export_static_references(self) -> None:
        before_checks = storage.list_checks(project_id=self.target_id)
        missing_module_check = api_check(
            "Missing module",
            "async def check(ctx):\n    await ctx.modules.use('missing').verify(ctx)\n",
        )
        missing_module_preview = config_transfer.preview_project_import(
            project_bundle(checks=[missing_module_check]),
            self.target_id,
        )
        self.assertFalse(missing_module_preview["valid"])
        self.assertIn("module_not_found", "；".join(missing_module_preview["issues"]))

        missing_export_check = api_check(
            "Missing export",
            "async def check(ctx):\n    await ctx.modules.use('shared').missing(ctx)\n",
        )
        shared = module_payload("shared", "def verify(ctx):\n    return True\n")
        missing_export_preview = config_transfer.preview_project_import(
            project_bundle(modules=[shared], checks=[missing_export_check]),
            self.target_id,
        )
        self.assertFalse(missing_export_preview["valid"])
        self.assertIn("module_export_missing", "；".join(missing_export_preview["issues"]))
        self.assertEqual(storage.list_checks(project_id=self.target_id), before_checks)
        self.assertIsNone(self._published(self.target_id))

    def test_import_rolls_back_task_and_library_when_late_project_write_fails(self) -> None:
        shared = module_payload("shared", "def verify(ctx):\n    return True\n")
        check = api_check(
            "Atomic import",
            "async def check(ctx):\n    await ctx.modules.use('shared').verify(ctx)\n",
        )
        bundle = project_bundle(modules=[shared], checks=[check])
        before = config_transfer.export_project(self.target_id, redact=False)

        with patch.object(storage, "update_project_variables", side_effect=RuntimeError("variables write failed")):
            with self.assertRaisesRegex(RuntimeError, "variables write failed"):
                config_transfer.apply_project_import(
                    {**bundle, "variables": [{"id": "v", "name": "IMPORT_VALUE", "value": "x"}]},
                    self.target_id,
                )

        after = config_transfer.export_project(self.target_id, redact=False)
        self.assertEqual(after["checks"], before["checks"])
        library = storage.get_project_library(self.target_id)
        self.assertEqual(library["modules"], [])
        self.assertIsNone(library["published"])
        self.assertEqual(storage.list_project_library_versions(self.target_id), [])


if __name__ == "__main__":
    unittest.main()
