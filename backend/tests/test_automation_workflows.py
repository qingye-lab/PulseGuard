from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock

from fastapi.testclient import TestClient

from backend.app import storage, automation_jobs as jobs, check_changes as changes
from backend.app.main import app, _validated_check_data
from backend.app.runner import CheckRunner


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(storage, "DB_PATH", Path(self.temp.name) / "test.db"))
        storage.init_db()
        self.check = storage.list_checks("api")[0]
        self.project = self.check["project_id"]
        self.headers = {"X-PulseGuard-Project-Id": str(self.project)}

    def prepare(self, updates, **kwargs):
        return changes.prepare(self.project, updates, check_id=self.check["id"],
            expected_hash=jobs.definition_hash(storage.get_check(self.check["id"])), validate=_validated_check_data, **kwargs)

    def test_concurrent_same_key_creates_one_receipt_and_rejects_changed_payload(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            receipts = list(pool.map(lambda _: jobs.submit(self.project, "stable-key", [self.check["id"]]), range(6)))
        self.assertEqual(len({r["id"] for r in receipts}), 1)
        self.assertEqual(receipts[0]["counts"], {"queued": 1})
        with self.assertRaises(ValueError):
            jobs.submit(self.project, "stable-key", [self.check["id"], 99999])
        with storage._connect(read_only=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM execution_batch_items").fetchone()[0], 1)

    def test_restart_does_not_replay_claimed_items_and_cancel_only_stops_queued(self):
        other = storage.create_check({**self.check, "name": "second"}, project_id=self.project)
        receipt = jobs.submit(self.project, "restart-key", [self.check["id"], other["id"]])
        item = jobs.claim()
        result = jobs.cancel(self.project, receipt["id"])
        self.assertEqual(result["counts"], {"running": 1, "cancelled": 1})
        jobs.recover_interrupted()
        self.assertEqual(jobs.get_batch(self.project, receipt["id"])["counts"], {"unknown": 1, "cancelled": 1})
        self.assertIsNone(jobs.claim())
        self.assertIsNone(jobs.get_batch(self.project + 100, receipt["id"]))
        self.assertIsNone(jobs.cancel(self.project + 100, receipt["id"]))
        self.assertEqual(jobs.submit(self.project, "restart-key", [other["id"], self.check["id"]])["id"], item["batch_id"])

    def test_batch_scope_is_atomic_and_disabled_tasks_reject(self):
        project = storage.create_project("Other automation")
        other = storage.create_check({**self.check, "name": "foreign"}, project_id=project["id"])
        for ids in ([self.check["id"], other["id"]], [99999]):
            with self.assertRaises(ValueError):
                jobs.submit(self.project, "scope-key", ids)
        storage.set_check_enabled(self.check["id"], False)
        with self.assertRaises(ValueError):
            jobs.submit(self.project, "scope-key", [self.check["id"]])
        self.assertIsNone(jobs.get_batch(self.project, key="scope-key"))

    def test_definition_race_rejected_before_runner_submission(self):
        receipt = jobs.submit(self.project, "stale-key", [self.check["id"]])
        item = jobs.claim()
        storage.update_check(self.check["id"], {**self.check, "name": "changed"})
        runner = CheckRunner()
        runner._submit = AsyncMock()
        with self.assertRaises(jobs.DispatchRejected):
            asyncio.run(runner.run_check(self.check["id"], project_id=self.project,
                expected_definition_hash=item["definition_hash"]))
        runner._submit.assert_not_called()
        self.assertEqual(jobs.get_batch(self.project, receipt["id"])["counts"], {"running": 1})

    def test_restore_invalidates_stale_pending_work_and_project_history_prevents_deletion(self):
        receipt = jobs.submit(self.project, "restore-key", [self.check["id"]])
        draft = self.prepare({"name": "do not apply after restore"})
        jobs.invalidate_after_restore()
        self.assertEqual(jobs.get_batch(self.project, receipt["id"])["counts"], {"cancelled": 1})
        self.assertEqual(changes.get_change(self.project, draft["id"])["state"], "cancelled")
        project = storage.create_project("draft history")
        candidate = storage._check_snapshot(self.check)
        changes.prepare(project["id"], candidate, validate=_validated_check_data)
        with self.assertRaises(ValueError):
            storage.delete_empty_project(project["id"])

    def test_preview_is_nonexecuting_partial_edit_preserves_opaque_fields(self):
        storage.update_check(self.check["id"], {**self.check, "headers_json": '{"Authorization":"private-secret"}'})
        context = changes.context(self.project, self.check["id"])
        self.assertNotIn("private-secret", str(context))
        draft = self.prepare({"name": "updated", "interval_seconds": 600})
        self.assertEqual(storage.get_check(self.check["id"])["name"], self.check["name"])
        self.assertNotIn("private-secret", str(draft))
        for _ in range(2):
            applied = changes.apply(self.project, draft["id"], draft["digest"], validate=_validated_check_data)
            self.assertEqual(applied["state"], "applied")
        saved = storage.get_check(self.check["id"])
        self.assertEqual(saved["name"], "updated")
        self.assertIn("private-secret", saved["headers_json"])
        self.assertEqual(len(storage.list_check_versions(self.check["id"])), 2)
        with storage._connect(read_only=True) as conn:
            self.assertIsNone(conn.execute("SELECT candidate_json FROM check_change_drafts WHERE id=?", (draft["id"],)).fetchone()[0])

    def test_concurrent_apply_creates_exactly_one_task(self):
        candidate = storage._check_snapshot(self.check)
        candidate.pop("enabled")
        draft = changes.prepare(self.project, candidate, validate=_validated_check_data)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: changes.apply(self.project, draft["id"], draft["digest"], validate=_validated_check_data), range(4)))
        ids = {r["result"]["check_id"] for r in results}
        self.assertEqual(len(ids), 1)
        self.assertFalse(storage.get_check(ids.pop())["enabled"])

    def test_stale_preview_wrong_digest_expiry_cancel_and_cross_project_rejected(self):
        draft = self.prepare({"name": "attempt"})
        with self.assertRaises(ValueError):
            changes.apply(self.project, draft["id"], "0" * 64, validate=_validated_check_data)
        self.assertIsNone(changes.apply(self.project + 100, draft["id"], draft["digest"], validate=_validated_check_data))
        storage.update_check(self.check["id"], {**self.check, "name": "newer edit"})
        with self.assertRaises(ValueError):
            changes.apply(self.project, draft["id"], draft["digest"], validate=_validated_check_data)
        self.assertEqual(storage.get_check(self.check["id"])["name"], "newer edit")
        changes.cancel(self.project, draft["id"])
        self.assertEqual(changes.get_change(self.project, draft["id"])["state"], "cancelled")
        another = self.prepare({"name": "expired"})
        with storage._connect() as conn:
            conn.execute("UPDATE check_change_drafts SET expires_at='2000-01-01' WHERE id=?", (another["id"],))
        with self.assertRaises(ValueError):
            changes.apply(self.project, another["id"], another["digest"], validate=_validated_check_data)
        storage.init_db()
        self.assertEqual(changes.get_change(self.project, another["id"])["state"], "expired")

    def test_restore_uses_owned_version_and_remains_preview_until_applied(self):
        version = storage.record_check_version(self.check, "before")
        storage.update_check(self.check["id"], {**self.check, "name": "new"})
        draft = self.prepare({}, version_id=version["id"])
        self.assertEqual(storage.get_check(self.check["id"])["name"], "new")
        changes.apply(self.project, draft["id"], draft["digest"], validate=_validated_check_data)
        self.assertEqual(storage.get_check(self.check["id"])["name"], self.check["name"])

    def test_http_validation_scope_and_scheduler_sync_retry(self):
        client = TestClient(app)
        invalid = client.post("/api/execution-batches", headers=self.headers,
            json={"idempotency_key": "valid-key", "check_ids": [0]})
        self.assertEqual(invalid.status_code, 422)
        batch = client.post("/api/execution-batches", headers=self.headers,
            json={"idempotency_key": "valid-key", "check_ids": [self.check["id"]]}).json()
        self.assertEqual(client.get("/api/execution-batches", headers=self.headers,
                         params={"idempotency_key": "valid-key"}).json()["id"], batch["id"])
        response = client.post("/api/check-changes/preview", headers=self.headers,
            json={"changes": {"name": "invalid-private", "type": "api", "entry_url": "x", "script": "private-secret"}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-secret", response.text)
        draft = self.prepare({"name": "saved once"})
        with patch.object(app.state, "scheduler", Mock(), create=True) as scheduler:
            for _ in range(2):
                result = client.post(f"/api/check-changes/{draft['id']}/apply", headers=self.headers, json={"expected_digest": draft["digest"]})
                self.assertEqual(result.status_code, 200)
            self.assertEqual(scheduler.sync_check.call_count, 2)
        self.assertEqual(len(storage.list_check_versions(self.check["id"])), 2)


if __name__ == "__main__":
    unittest.main()
