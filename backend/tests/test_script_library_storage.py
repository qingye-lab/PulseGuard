from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.app import main, storage


MODULE_SOURCE = '''"""Shared helper."""

def verify():
    return True
'''

MODULE_SOURCE_UPDATED = '''"""Updated shared helper."""

def verify():
    return "updated"
'''

CHECK_SCRIPT = '''async def check(ctx):
    return ctx.modules.use("literal").verify()
'''


def check_payload(name: str = "uses literal", *, enabled: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "type": "api",
        "enabled": enabled,
        "interval_seconds": 120,
        "timeout_ms": 10000,
        "entry_url": "https://example.test/health",
        "viewport_mode": "web",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": "[]",
        "setup_script": "",
        "script": CHECK_SCRIPT,
        "tags": "",
        "alert_policy_json": "{}",
    }


class ScriptLibraryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        storage.init_db()
        self.project_id = int(storage.create_project("Library project")["id"])

    def test_save_rejects_compile_error_and_preserves_effective_library(self) -> None:
        from backend.app.script_modules import ModuleRuntime

        modules = [{"name": "literal", "source": MODULE_SOURCE}]
        saved = storage.save_project_library(self.project_id, 0, modules)
        versions = storage.list_project_library_versions(self.project_id)
        with self.assertRaises(storage.ModuleLibraryError):
            storage.save_project_library(
                self.project_id,
                saved["effective_revision"],
                [*modules, {"name": "broken", "source": "def broken():\n    break\n"}],
            )
        self.assertEqual(storage.get_project_library(self.project_id), saved)
        self.assertEqual(storage.list_project_library_versions(self.project_id), versions)
        runtime = ModuleRuntime(storage.get_published_library(self.project_id))
        self.assertTrue(runtime.use("literal").verify())

    def test_empty_read_does_not_create_library_rows(self) -> None:
        with storage._connect(read_only=True) as conn:
            initial = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (self.project_id,)).fetchone()[0])
                for table in storage.LIBRARY_PROJECT_TABLES
            }
        self.assertEqual(storage.get_project_library(self.project_id)["draft_revision"], 0)
        with storage._connect(read_only=True) as conn:
            after = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (self.project_id,)).fetchone()[0])
                for table in storage.LIBRARY_PROJECT_TABLES
            }
        self.assertEqual(initial, after)
        self.assertEqual(initial["script_library_drafts"], 0)

    def test_draft_revision_is_cas_and_exports_are_derived(self) -> None:
        updated = storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE, "exports": [{"name": "spoof"}]}],
        )
        self.assertEqual(updated["draft_revision"], 1)
        export = updated["modules"][0]["exports"][0]
        self.assertEqual(export["name"], "verify")
        self.assertEqual(export["kind"], "sync")
        self.assertIn("positional", export)
        with self.assertRaises(storage.LibraryRevisionConflict):
            storage.update_project_library_draft(
                self.project_id,
                0,
                [{"name": "literal", "source": MODULE_SOURCE}],
            )

    def test_save_makes_effective_snapshot_without_promoting_stale_draft(self) -> None:
        first = storage.save_project_library(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        self.assertEqual(first["effective_revision"], 1)
        self.assertEqual(first["effective_modules"][0]["source"], MODULE_SOURCE)
        first_version_id = int(first["published"]["version_id"])

        # An old draft edit remains an old-API artifact.  The effective view
        # and the next save must use the published pointer plus an explicit
        # module payload, never silently promote this stale source.
        stale_source = MODULE_SOURCE.replace("return True", "return False")
        stale = storage.update_project_library_draft(
            self.project_id,
            first["effective_revision"],
            [{"name": "literal", "source": stale_source}],
        )
        current = storage.get_project_library(self.project_id)
        self.assertEqual(current["effective_modules"][0]["source"], MODULE_SOURCE)
        self.assertEqual(current["effective_revision"], stale["draft_revision"])

        saved = storage.save_project_library(
            self.project_id,
            current["effective_revision"],
            [{"name": "literal", "source": MODULE_SOURCE_UPDATED}],
        )
        self.assertGreater(saved["effective_revision"], current["effective_revision"])
        self.assertEqual(saved["effective_modules"][0]["source"], MODULE_SOURCE_UPDATED)
        runtime_bundle = storage.get_published_library(self.project_id)
        assert runtime_bundle is not None
        self.assertEqual(runtime_bundle["modules"][0]["source"], MODULE_SOURCE_UPDATED)
        history = storage.list_project_library_versions(self.project_id)
        self.assertEqual({int(item["version_id"]) for item in history}, {first_version_id, int(saved["published"]["version_id"])})

        with self.assertRaises(storage.LibraryRevisionConflict):
            storage.save_project_library(
                self.project_id,
                current["effective_revision"],
                [{"name": "literal", "source": MODULE_SOURCE}],
            )

    def test_save_rejects_breaking_reference_atomically(self) -> None:
        saved = storage.save_project_library(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        storage.create_check(check_payload(), project_id=self.project_id)
        with self.assertRaises(storage.LibraryStaticReferenceConflict) as raised:
            storage.save_project_library(self.project_id, saved["effective_revision"], [])
        self.assertTrue(raised.exception.diagnostics)
        self.assertEqual(raised.exception.diagnostics[0]["code"], "module_not_found")
        current = storage.get_project_library(self.project_id)
        self.assertEqual(current["effective_modules"][0]["source"], MODULE_SOURCE)
        self.assertEqual(len(storage.list_project_library_versions(self.project_id)), 1)

    def test_rollback_advances_save_cas_token_without_reusing_older_revision(self) -> None:
        first = storage.save_project_library(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        second = storage.save_project_library(
            self.project_id,
            first["effective_revision"],
            [{"name": "literal", "source": MODULE_SOURCE_UPDATED}],
        )
        rolled_back = storage.rollback_project_library(self.project_id, int(first["published"]["version_id"]))
        current = storage.get_project_library(self.project_id)
        self.assertGreater(current["effective_revision"], second["effective_revision"])
        self.assertEqual(current["effective_modules"][0]["source"], MODULE_SOURCE)
        self.assertNotEqual(current["effective_revision"], first["effective_revision"])
        self.assertEqual(int(rolled_back["version_id"]), int(first["published"]["version_id"]))

    def test_concurrent_draft_writes_allow_one_revision(self) -> None:
        def save() -> str:
            try:
                storage.update_project_library_draft(
                    self.project_id,
                    0,
                    [{"name": "literal", "source": MODULE_SOURCE}],
                )
                return "ok"
            except storage.LibraryRevisionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _item: save(), range(2)))
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(storage.get_project_library(self.project_id)["draft_revision"], 1)

    def test_concurrent_effective_saves_allow_one_cas_winner(self) -> None:
        def save(source: str) -> str:
            try:
                storage.save_project_library(
                    self.project_id,
                    0,
                    [{"name": "literal", "source": source}],
                )
                return "ok"
            except storage.LibraryRevisionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda source: save(source), [MODULE_SOURCE, MODULE_SOURCE_UPDATED]))
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(storage.get_project_library(self.project_id)["effective_revision"], 1)

    def test_publish_without_references_is_static_only_and_returns_hashes(self) -> None:
        draft = storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        published = storage.publish_project_library(self.project_id, draft["draft_revision"])
        self.assertGreater(published["version_id"], 0)
        self.assertEqual(published["release_id"], published["version_id"])
        self.assertTrue(published["release_hash"])
        self.assertTrue(published["content_hash"])
        runtime_bundle = storage.get_published_library(self.project_id)
        assert runtime_bundle is not None
        self.assertEqual(runtime_bundle["release_id"], published["version_id"])
        self.assertEqual([item["exports"] for item in runtime_bundle["modules"]], [["verify"]])
        api_version = storage.get_project_library_version(self.project_id, published["version_id"])
        assert api_version is not None
        self.assertEqual(api_version["modules"][0]["exports"][0]["name"], "verify")

    def test_effective_global_environment_change_invalidates_validation(self) -> None:
        storage.update_settings(
            {
                "environment_variables": [
                    {"id": "global-url", "name": "BASE_URL", "value": "https://one.test", "secret": False}
                ]
            }
        )
        draft = storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        validation = storage.create_project_library_validation(self.project_id, draft["draft_revision"])
        storage.update_settings(
            {
                "environment_variables": [
                    {"id": "global-url", "name": "BASE_URL", "value": "https://two.test", "secret": False}
                ]
            }
        )
        storage.update_settings(
            {
                "environment_variables": [
                    {"id": "global-url", "name": "BASE_URL", "value": "https://one.test", "secret": False}
                ]
            }
        )
        claimed = storage.claim_project_library_validation(self.project_id, validation["id"])
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "failed")
        self.assertTrue(any(item["code"] == "validation_stale" for item in claimed["diagnostics"]))

        with storage._connect(read_only=True) as conn:
            row = conn.execute(
                "SELECT dependency_fingerprint_json FROM script_library_validations WHERE id = ?",
                (validation["id"],),
            ).fetchone()
        self.assertNotIn("https://one.test", str(row[0]))
        self.assertNotIn("https://two.test", str(row[0]))

    def test_validation_task_change_is_rechecked_at_publish(self) -> None:
        check = storage.create_check(check_payload(), project_id=self.project_id)
        draft = storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        validation = storage.create_project_library_validation(self.project_id, draft["draft_revision"])
        claimed = storage.claim_project_library_validation(self.project_id, validation["id"])
        self.assertEqual(claimed["status"], "running")
        result = storage.finish_project_library_validation(
            self.project_id,
            validation["id"],
            "succeeded",
            checks=[
                {
                    "check_id": check["id"],
                    "original_check_id": check["id"],
                    "status": "ok",
                    "runs": [{"id": 1, "status": "ok", "affects_health": False}],
                }
            ],
        )
        self.assertEqual(result["status"], "succeeded")
        changed = dict(check_payload())
        changed["timeout_ms"] = 11000
        storage.update_check(int(check["id"]), changed, project_id=self.project_id)
        with self.assertRaises(storage.LibraryValidationConflict):
            storage.publish_project_library(
                self.project_id,
                draft["draft_revision"],
                validation_id=validation["id"],
            )

    def test_rollback_checks_current_references_without_http(self) -> None:
        empty_v1 = storage.publish_project_library(self.project_id, 0)
        draft = storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        storage.publish_project_library(self.project_id, draft["draft_revision"])
        storage.create_check(check_payload(), project_id=self.project_id)
        with self.assertRaises(storage.LibraryValidationConflict):
            storage.rollback_project_library(self.project_id, empty_v1["version_id"])

    def test_project_library_data_blocks_empty_project_delete(self) -> None:
        storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        with self.assertRaisesRegex(ValueError, "空项目"):
            storage.delete_empty_project(self.project_id)

    def test_startup_settles_inflight_validation_without_replay(self) -> None:
        draft = storage.update_project_library_draft(
            self.project_id,
            0,
            [{"name": "literal", "source": MODULE_SOURCE}],
        )
        validation = storage.create_project_library_validation(self.project_id, draft["draft_revision"])
        self.assertEqual(storage.claim_project_library_validation(self.project_id, validation["id"])["status"], "running")
        storage.init_db()
        settled = storage.get_project_library_validation(self.project_id, validation["id"])
        self.assertEqual(settled["status"], "cancelled")
        self.assertTrue(any(item["code"] == "validation_interrupted" for item in settled["diagnostics"]))
        events = storage.list_audit_events(project_id=self.project_id)
        self.assertTrue(any(event["entity_type"] == "script_library_validation" for event in events))


class ScriptLibraryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_patch = patch.object(storage, "DB_PATH", Path(self.temp_dir.name) / "pulseguard.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        storage.init_db()
        self.project_id = int(storage.create_project("Library API")["id"])
        self.client = TestClient(main.app)

    def test_header_scope_and_archived_readonly_boundary(self) -> None:
        other_id = int(storage.create_project("Archived library")["id"])
        headers = {"X-PulseGuard-Project-Id": str(self.project_id)}
        mismatch = self.client.get(
            f"/api/projects/{other_id}/library",
            headers=headers,
        )
        self.assertEqual(mismatch.status_code, 400)
        storage.archive_project(other_id)
        archived_headers = {"X-PulseGuard-Project-Id": str(other_id)}
        self.assertEqual(
            self.client.get(f"/api/projects/{other_id}/library", headers=archived_headers).status_code,
            200,
        )
        draft = self.client.put(
            f"/api/projects/{other_id}/library/draft",
            headers=archived_headers,
            json={"expected_revision": 0, "modules": []},
        )
        self.assertEqual(draft.status_code, 409)

    def test_save_library_updates_effective_modules_and_uses_cas(self) -> None:
        headers = {"X-PulseGuard-Project-Id": str(self.project_id)}
        saved = self.client.put(
            f"/api/projects/{self.project_id}/library",
            headers=headers,
            json={
                "expected_revision": 0,
                "modules": [{"name": "literal", "source": MODULE_SOURCE}],
            },
        )
        self.assertEqual(saved.status_code, 200)
        body = saved.json()
        self.assertEqual(body["effective_revision"], 1)
        self.assertEqual(body["effective_modules"][0]["name"], "literal")
        self.assertEqual(body["published"]["revision"], 1)
        self.assertEqual(
            self.client.get(f"/api/projects/{self.project_id}/library", headers=headers).json()["effective_modules"][0]["name"],
            "literal",
        )

        stale = self.client.put(
            f"/api/projects/{self.project_id}/library",
            headers=headers,
            json={"expected_revision": 0, "modules": []},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["actual_revision"], 1)

        storage.create_check(check_payload(), project_id=self.project_id)
        broken = self.client.put(
            f"/api/projects/{self.project_id}/library",
            headers=headers,
            json={"expected_revision": 1, "modules": []},
        )
        self.assertEqual(broken.status_code, 400)
        self.assertEqual(broken.json()["detail"]["diagnostics"][0]["code"], "module_not_found")
        self.assertEqual(
            self.client.get(f"/api/projects/{self.project_id}/library", headers=headers).json()["effective_modules"][0]["name"],
            "literal",
        )


class _ValidationRunner:
    async def run_library_validation(self, check: dict[str, object], bundle: dict[str, object], *, trigger: str) -> dict[str, object]:
        self.last_trigger = trigger
        return {
            "check_id": int(check["id"]),
            "original_check_id": int(check["id"]),
            "status": "ok",
            "runs": [{"id": 901, "status": "ok", "affects_health": False}],
        }


class ScriptLibraryAsgiTests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_returns_202_and_reaches_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir, patch.object(
            storage, "DB_PATH", Path(temp_dir) / "pulseguard.db"
        ):
            storage.init_db()
            project_id = int(storage.create_project("ASGI library")["id"])
            storage.update_project_library_draft(
                project_id,
                0,
                [{"name": "literal", "source": MODULE_SOURCE}],
            )
            storage.create_check(check_payload(), project_id=project_id)
            runner = _ValidationRunner()
            main.app.state.runner = runner
            headers = {"X-PulseGuard-Project-Id": str(project_id)}
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.post(
                    f"/api/projects/{project_id}/library/validate",
                    headers=headers,
                    json={"candidate_revision": 1},
                )
                self.assertEqual(response.status_code, 202)
                validation_id = int(response.json()["validation_id"])
                terminal = None
                for _attempt in range(30):
                    terminal = await client.get(
                        f"/api/projects/{project_id}/library/validations/{validation_id}",
                        headers=headers,
                    )
                    if terminal.json().get("status") in {"succeeded", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(terminal.json()["status"], "succeeded")
                self.assertEqual(len(terminal.json()["checks"][0]["runs"]), 1)
            tasks = getattr(main.app.state, "background_tasks", set())
            if tasks:
                await main._cancel_background_tasks(tasks)


if __name__ == "__main__":
    unittest.main()
