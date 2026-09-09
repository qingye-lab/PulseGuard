from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app import artifacts, storage
from backend.app.runner import CheckRunner


class ModulePreflightMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(storage, "DB_PATH", self.temp_root / "pulseguard.db"))
        self.enterContext(patch.object(storage, "BACKUPS_DIR", self.temp_root / "backups"))
        self.enterContext(patch.object(storage, "ensure_runtime_dirs"))
        directories = {
            name: self.temp_root / name.lower()
            for name in ("SCREENSHOTS_DIR", "TRACES_DIR", "RESPONSES_DIR")
        }
        for directory in directories.values():
            directory.mkdir()
        self.enterContext(patch.multiple(artifacts, **directories))
        storage.init_db()

        self.project = storage.create_project("预检版本元数据")
        self.project_id = int(self.project["id"])
        storage.update_project_library_draft(
            self.project_id,
            0,
            [
                {
                    "name": "shared",
                    "source": "async def verify(ctx):\n    return True\n",
                }
            ],
        )
        self.release = storage.publish_project_library(self.project_id, 1)

    def _check_payload(self, script: str) -> dict[str, object]:
        return {
            "name": "模块预检失败",
            "type": "api",
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 5000,
            "entry_url": "https://example.test/health",
            "method": "GET",
            "headers_json": "{}",
            "body": "",
            "assertions_json": "[]",
            "setup_script": "",
            "script": script,
            "tags": "",
            "alert_policy_json": "{}",
            "runner_selection_mode": "selected_parallel",
            "runner_ids": ["local"],
            "browser_selection_mode": "selected_parallel",
            "browser_types": ["chromium"],
        }

    def _runtime_metadata(self, run: dict[str, object]) -> dict[str, object]:
        snapshot = json.loads(str(run["request_snapshot"]))
        diagnostics = snapshot["diagnostics"]
        self.assertIsInstance(diagnostics, dict)
        modules = diagnostics["modules"]
        self.assertIsInstance(modules, dict)
        return modules

    def test_run_check_persists_published_metadata_on_module_preflight_failure(self) -> None:
        check = storage.create_check(
            self._check_payload(
                'async def check(ctx):\n'
                '    await ctx.modules.use("shared").missing(ctx)\n'
            ),
            project_id=self.project_id,
        )

        result = asyncio.run(
            CheckRunner().run_check(
                int(check["id"]),
                trigger="module-preflight-test",
                project_id=self.project_id,
            )
        )
        persisted = storage.get_run(int(result["id"]), project_id=self.project_id)

        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(
            self._runtime_metadata(persisted),
            {
                "project_id": self.project_id,
                "release_id": int(self.release["release_id"]),
                "release_hash": self.release["release_hash"],
                "content_hash": self.release["content_hash"],
                "module_hashes": {"shared": self.release["modules"][0]["hash"]},
            },
        )

    def test_debug_run_persists_published_metadata_on_module_preflight_failure(self) -> None:
        result = asyncio.run(
            CheckRunner().run_draft(
                {
                    "id": 901,
                    "project_id": self.project_id,
                    "name": "草稿模块预检失败",
                    "type": "api",
                    "enabled": True,
                    "interval_seconds": 300,
                    "timeout_ms": 5000,
                    "entry_url": "https://example.test/health",
                    "method": "GET",
                    "headers_json": "{}",
                    "body": "",
                    "assertions_json": "[]",
                    "setup_script": "",
                    "script": (
                        "async def check(ctx):\n"
                        "    await ctx.modules.use(\"shared\").verify()\n"
                    ),
                    "tags": "",
                    "alert_policy_json": "{}",
                },
                trigger="draft-debug",
            )
        )
        persisted = storage.get_run(int(result["id"]), project_id=self.project_id)

        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted["check_id"], 0)
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["observation_kind"], "draft")
        self.assertEqual(
            self._runtime_metadata(persisted),
            {
                "project_id": self.project_id,
                "release_id": int(self.release["release_id"]),
                "release_hash": self.release["release_hash"],
                "content_hash": self.release["content_hash"],
                "module_hashes": {"shared": self.release["modules"][0]["hash"]},
            },
        )


if __name__ == "__main__":
    unittest.main()
