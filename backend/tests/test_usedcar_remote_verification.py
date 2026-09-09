from __future__ import annotations

import copy
import importlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


migration = importlib.import_module("scripts.migrate_usedcar_modules")
remote = importlib.import_module("scripts.verify_usedcar_remote")


class UsedcarRemoteVerificationTests(unittest.TestCase):
    project_id = 2

    def _plan(self) -> dict[str, object]:
        live: list[dict[str, object]] = []
        update: list[dict[str, object]] = []
        for check_id in migration.TARGET_IDS:
            script = "" if check_id in migration.STATIC_IDS else "async def check(ctx):\n    pass\n"
            payload: dict[str, object] = {
                "name": f"usedcar-{check_id}",
                "project_id": self.project_id,
                "type": "api",
                "enabled": True,
                "interval_seconds": 60,
                "timeout_ms": 10000,
                "entry_url": f"https://example.test/{check_id}",
                "method": "GET",
                "headers_json": "{}",
                "body": "",
                "assertions_json": json.dumps(
                    [
                        {
                            "id": "status",
                            "type": "status_code",
                            "enabled": True,
                            "expected_status": 200,
                        }
                    ]
                ),
                "setup_script": "",
                "script": script,
                "tags": "usedcar",
                "alert_policy_json": "{}",
                "runner_selection_mode": "selected_parallel",
                "runner_ids": ["local"],
                "browser_selection_mode": "selected_parallel",
                "browser_types": [],
            }
            live.append({"id": check_id, **copy.deepcopy(payload)})
            candidate = copy.deepcopy(payload)
            candidate.pop("project_id", None)
            update.append({"id": check_id, "payload": candidate})
        source = (
            Path(__file__).resolve().parents[2] / "scripts" / "examples" / "usedcar_module.py"
        ).read_text(encoding="utf-8")
        return migration._build_plan(live, {"checks": update}, source, self.project_id)

    def _live_candidate_checks(self, plan: dict[str, object]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item in plan["checks"]:  # type: ignore[index]
            payload = copy.deepcopy(item["payload"])  # type: ignore[index]
            check_id = int(item["id"])  # type: ignore[index]
            payload["id"] = check_id
            payload["project_id"] = self.project_id
            payload.update(
                {
                    "current_status": "healthy",
                    "monitor_status": "healthy",
                    "consecutive_failures": 0,
                    "consecutive_successes": 2,
                    "last_run_id": 1000 + check_id,
                    "last_scheduled_run_id": 1000 + check_id,
                }
            )
            result.append(payload)
        return result

    def _module_identity(self, plan: dict[str, object]) -> dict[str, object]:
        module = plan["module_bundle"]["modules"][0]  # type: ignore[index]
        return {
            "project_id": self.project_id,
            "release_id": 7,
            "release_hash": "r" * 64,
            "content_hash": "c" * 64,
            "module_hashes": {"usedcar": module["hash"]},  # type: ignore[index]
        }

    def _run_result(
        self,
        plan: dict[str, object],
        check_id: int,
        module_identity: dict[str, object],
        *,
        status: str = "ok",
        observation_kind: str = "observation",
        affects_health: bool = True,
        notification_status: str = "not_required",
        intended_negative: bool = False,
    ) -> dict[str, object]:
        matrix = plan["verification_matrix"]["checks"][str(check_id)]  # type: ignore[index]
        assertion_count = int(matrix["assertion_count"])  # type: ignore[index]
        assertions = [{} for _ in range(assertion_count)]
        if intended_negative:
            assertions[0] = {"status": "failed", "expected": 599}
        dynamic = bool(matrix["module_refs"])  # type: ignore[index]
        modules = (
            module_identity
            if dynamic
            else {
                "project_id": self.project_id,
                "release_id": None,
                "release_hash": None,
                "content_hash": remote.bundle_content_hash([]),
                "module_hashes": {},
            }
        )
        return {
            "id": 7000 + check_id,
            "check_id": 0 if observation_kind == "draft" else check_id,
            "status": status,
            "observation_kind": observation_kind,
            "affects_health": affects_health,
            "notification_status": notification_status,
            "request_snapshot": json.dumps(
                {
                    "diagnostics": {
                        "http_requests": [{} for _ in range(migration.REQUEST_COUNTS[check_id])],
                        "modules": modules,
                    }
                }
            ),
            "response_snapshot": json.dumps({"assertions": assertions}),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    def test_reverse_debug_is_draft_only_and_health_stable(self) -> None:
        plan = self._plan()
        live = self._live_candidate_checks(plan)
        module_identity = self._module_identity(plan)
        calls: list[tuple[str, str]] = []
        check_reads = 0

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            nonlocal check_reads
            method = str(kwargs.get("method") or "GET")
            calls.append((method, path))
            if method == "GET" and path == "/api/runs?type=api&limit=500":
                return []
            if method == "POST" and path == "/api/checks/debug":
                payload = kwargs.get("payload")
                check_id = int(str(payload["entry_url"]).rsplit("/", 1)[-1])  # type: ignore[index]
                return self._run_result(
                    plan,
                    check_id,
                    module_identity,
                    status="failed",
                    observation_kind="draft",
                    affects_health=False,
                    intended_negative=True,
                )
            if method == "GET" and path == "/api/checks?type=api":
                check_reads += 1
                return live
            raise AssertionError((method, path, kwargs))

        with patch.object(remote, "_api_json", side_effect=fake_api):
            result = remote.run_reverse_draft_debug(
                "http://example.test",
                self.project_id,
                plan,
                module_identity,
            )

        self.assertEqual(result["count"], 15)
        self.assertTrue(result["all_draft_safe"])
        self.assertTrue(result["live_health_unchanged"])
        self.assertTrue(result["live_health_unchanged_by_draft"])
        self.assertEqual(sum(path == "/api/checks/debug" for _, path in calls), 15)
        self.assertNotIn(("PUT", "/api/checks/41"), calls)
        self.assertNotIn(("POST", "/api/checks/41/run"), calls)

    def test_reverse_debug_ignores_health_changes_from_concurrent_scheduled_observation(self) -> None:
        plan = self._plan()
        live = self._live_candidate_checks(plan)
        module_identity = self._module_identity(plan)
        after_live = copy.deepcopy(live)
        changed = next(item for item in after_live if int(item["id"]) == 34)
        changed.update(
            {
                "current_status": "failed",
                "monitor_status": "failed",
                "consecutive_failures": 1,
                "consecutive_successes": 0,
                "last_run_id": 9999,
                "last_scheduled_run_id": 9999,
            }
        )
        calls: list[tuple[str, str]] = []
        check_reads = 0

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            nonlocal check_reads
            method = str(kwargs.get("method") or "GET")
            calls.append((method, path))
            if method == "GET" and path == "/api/runs?type=api&limit=500":
                if sum(item == ("GET", path) for item in calls) == 1:
                    return []
                return [
                    {
                        **self._run_result(plan, 34, module_identity),
                        "id": 9999,
                        "check_id": 34,
                        "trigger": "scheduled",
                        "runner_id": "local",
                    }
                ]
            if method == "POST" and path == "/api/checks/debug":
                payload = kwargs.get("payload")
                check_id = int(str(payload["entry_url"]).rsplit("/", 1)[-1])  # type: ignore[index]
                return self._run_result(
                    plan,
                    check_id,
                    module_identity,
                    status="failed",
                    observation_kind="draft",
                    affects_health=False,
                    intended_negative=True,
                )
            if method == "GET" and path == "/api/checks?type=api":
                check_reads += 1
                if check_reads == 1:
                    return live
                return after_live
            raise AssertionError((method, path, kwargs))

        with patch.object(remote, "_api_json", side_effect=fake_api):
            result = remote.run_reverse_draft_debug(
                "http://example.test",
                self.project_id,
                plan,
                module_identity,
            )

        self.assertTrue(result["all_draft_safe"])
        self.assertFalse(result["live_health_unchanged"])
        self.assertTrue(result["live_health_unchanged_by_draft"])
        self.assertTrue(result["live_health_changed_by_concurrent_scheduled"])
        self.assertEqual(result["concurrent_scheduled_ids"], [34])
        self.assertEqual(result["health_changes"]["34"]["scheduled_run"]["run_id"], 9999)

    def test_positive_manual_runs_all_tasks_and_checks_request_and_rule_counts(self) -> None:
        plan = self._plan()
        module_identity = self._module_identity(plan)
        calls: list[tuple[str, str]] = []

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            method = str(kwargs.get("method") or "GET")
            calls.append((method, path))
            if method == "POST" and path.startswith("/api/checks/") and path.endswith("/run"):
                check_id = int(path.split("/")[3])
                return self._run_result(plan, check_id, module_identity)
            raise AssertionError((method, path, kwargs))

        with patch.object(remote, "_api_json", side_effect=fake_api):
            result = remote.run_positive_manual("http://example.test", self.project_id, plan, module_identity)

        self.assertEqual(result["count"], 15)
        self.assertTrue(result["all_positive_ok"])
        self.assertTrue(result["request_counts_match"])
        self.assertTrue(result["assertion_counts_match"])
        self.assertEqual(
            [path for method, path in calls if method == "POST"],
            [f"/api/checks/{check_id}/run" for check_id in migration.TARGET_IDS],
        )

    def test_positive_manual_rejects_wrong_published_module_metadata(self) -> None:
        plan = self._plan()
        module_identity = self._module_identity(plan)
        wrong_identity = {**module_identity, "release_id": 999}

        def fake_api(base_url: str, path: str, project_id: int, **kwargs: object) -> object:
            check_id = int(path.split("/")[3])
            return self._run_result(
                plan,
                check_id,
                wrong_identity if check_id == 34 else module_identity,
            )

        with patch.object(remote, "_api_json", side_effect=fake_api):
            result = remote.run_positive_manual("http://example.test", self.project_id, plan, module_identity)

        self.assertFalse(result["all_positive_ok"])
        self.assertFalse(result["checks"]["34"]["module_metadata_ok"])
        self.assertTrue(result["checks"]["35"]["module_metadata_ok"])

    def test_draft_evidence_rejects_request_count_or_module_metadata_drift(self) -> None:
        plan = self._plan()
        module_identity = self._module_identity(plan)
        result = self._run_result(
            plan,
            34,
            module_identity,
            status="failed",
            observation_kind="draft",
            affects_health=False,
            intended_negative=True,
        )
        snapshot = json.loads(str(result["request_snapshot"]))
        snapshot["diagnostics"]["http_requests"] = []
        snapshot["diagnostics"]["modules"] = {**module_identity, "content_hash": "x" * 64}
        result["request_snapshot"] = json.dumps(snapshot)
        row = remote._draft_row(
            34,
            result,
            expected_requests=migration.REQUEST_COUNTS[34],
            expected_assertions=remote._expected_assertions(plan, 34),
            module_evidence=remote._run_module_evidence(34, result, plan, module_identity),
        )

        self.assertFalse(row["counts_match"])
        self.assertFalse(row["module_metadata_ok"])
        self.assertFalse(row["draft_safe"])

    def test_scheduled_evidence_requires_finished_ok_counts_and_modules(self) -> None:
        plan = self._plan()
        module_identity = self._module_identity(plan)
        lower_bound = datetime(2026, 1, 1, tzinfo=timezone.utc)
        running = self._run_result(plan, 34, module_identity)
        running.update(
            {
                "id": 9034,
                "check_id": 34,
                "status": "running",
                "trigger": "scheduled",
                "runner_id": "local",
                "started_at": (lower_bound + timedelta(seconds=61)).isoformat(),
                "finished_at": None,
            }
        )
        wrong = self._run_result(plan, 35, module_identity)
        wrong_snapshot = json.loads(str(wrong["request_snapshot"]))
        wrong_snapshot["diagnostics"]["http_requests"] = []
        wrong_snapshot["diagnostics"]["modules"] = {**module_identity, "release_id": 999}
        wrong["request_snapshot"] = json.dumps(wrong_snapshot)
        wrong.update(
            {
                "id": 9035,
                "check_id": 35,
                "trigger": "scheduled",
                "runner_id": "local",
                "started_at": (lower_bound + timedelta(seconds=61)).isoformat(),
                "finished_at": (lower_bound + timedelta(seconds=62)).isoformat(),
            }
        )

        evidence = remote._scheduled_evidence(
            [running, wrong],
            {34: set(), 35: set()},
            lower_bound,
            plan=plan,
            module_identity=module_identity,
        )
        self.assertEqual(evidence, {})

    def test_scheduler_poll_waits_sixty_seconds_and_requires_all_execution_fields(self) -> None:
        plan = self._plan()
        module_identity = self._module_identity(plan)
        checks = {
            check_id: {"interval_seconds": 60}
            for check_id in migration.TARGET_IDS
        }
        baseline_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = {"seconds": 0.0}
        current = {"value": baseline_at}
        runs = []
        for check_id in migration.TARGET_IDS:
            run = self._run_result(plan, check_id, module_identity)
            run.update(
                {
                    "id": 5000 + check_id,
                    "check_id": check_id,
                    "trigger": "scheduled",
                    "runner_id": "local",
                    "started_at": (baseline_at + timedelta(seconds=60)).isoformat(),
                    "finished_at": (baseline_at + timedelta(seconds=61)).isoformat(),
                }
            )
            runs.append(run)

        def fake_sleep(seconds: float) -> None:
            clock["seconds"] += seconds
            current["value"] += timedelta(seconds=seconds)

        def fake_now() -> datetime:
            return current["value"]

        def fake_monotonic() -> float:
            return clock["seconds"]

        with patch.object(remote, "_api_json", return_value=runs) as api:
            result = remote.poll_next_scheduled_runs(
                "http://example.test",
                self.project_id,
                checks,
                plan,
                module_identity,
                baseline_runs=[],
                baseline_at=baseline_at,
                poll_interval_seconds=5,
                timeout_after_lower_bound_seconds=120,
                sleep=fake_sleep,
                monotonic=fake_monotonic,
                now=fake_now,
            )

        self.assertEqual(result["lower_bound_seconds"], 60)
        self.assertGreaterEqual(clock["seconds"], 60)
        self.assertTrue(result["all_15_observed"])
        self.assertEqual(len(result["checks"]), 15)
        api.assert_called_once_with("http://example.test", "/api/runs?type=api&limit=500", self.project_id)

    def test_scheduler_evidence_ignores_non_scheduled_or_non_health_runs(self) -> None:
        lower_bound = datetime(2026, 1, 1, tzinfo=timezone.utc)
        runs = [
            {
                "id": 1,
                "check_id": 34,
                "status": "failed",
                "trigger": "manual",
                "observation_kind": "observation",
                "affects_health": True,
                "runner_id": "local",
                "started_at": (lower_bound + timedelta(seconds=61)).isoformat(),
            },
            {
                "id": 2,
                "check_id": 35,
                "status": "failed",
                "trigger": "scheduled",
                "observation_kind": "draft",
                "affects_health": False,
                "runner_id": "local",
                "started_at": (lower_bound + timedelta(seconds=61)).isoformat(),
            },
        ]
        evidence = remote._scheduled_evidence(runs, {34: set(), 35: set()}, lower_bound)
        self.assertEqual(evidence, {})


if __name__ == "__main__":
    unittest.main()
