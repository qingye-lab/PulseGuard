from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import storage
from backend.app.main import app


def project_headers() -> dict[str, str]:
    return {"X-PulseGuard-Project-Id": "1"}


class RunComparisonRouteTests(unittest.TestCase):
    def test_failed_api_run_compares_with_recent_success_for_same_check(self) -> None:
        with isolated_storage():
            check = create_check("API health", "api")
            stale_success = storage.create_run(check)
            storage.finish_run(
                int(stale_success["id"]),
                run_payload(
                    "ok",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/old"},
                    response_snapshot={"status_code": 200, "assertions": []},
                ),
            )
            baseline = storage.create_run(check)
            storage.finish_run(
                int(baseline["id"]),
                run_payload(
                    "ok",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/health"},
                    response_snapshot={
                        "status_code": 200,
                        "assertions": [
                            assertion("status code is 200", "status_code", "ok", 200, 200, "ok"),
                            assertion("body has id", "$.id", "ok", "123", "123", "ok"),
                        ],
                    },
                ),
            )
            other_check = create_check("Other API", "api")
            other_success = storage.create_run(other_check)
            storage.finish_run(
                int(other_success["id"]),
                run_payload(
                    "ok",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/other"},
                    response_snapshot={"status_code": 200, "assertions": []},
                ),
            )
            current = storage.create_run(check)
            storage.finish_run(
                int(current["id"]),
                run_payload(
                    "failed",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/health"},
                    response_snapshot={
                        "status_code": 500,
                        "assertions": [
                            assertion("status code is 200", "status_code", "failed", 500, 200, "status mismatch"),
                            assertion("body has id", "$.id", "ok", "123", "123", "ok"),
                        ],
                    },
                    error_message="status mismatch",
                ),
            )

            response = TestClient(app).get(f"/api/runs/{current['id']}/compare-success", headers=project_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["current_run"]["id"], current["id"])
        self.assertEqual(payload["baseline_run"]["id"], baseline["id"])

        fields = fields_by_key(payload)
        self.assertEqual(fields["url"]["current"], "https://api.example.com/v1/health")
        self.assertEqual(fields["url"]["baseline"], "https://api.example.com/v1/health")
        self.assertFalse(fields["url"]["changed"])
        self.assertEqual(fields["status_code"]["current"], 500)
        self.assertEqual(fields["status_code"]["baseline"], 200)
        self.assertTrue(fields["status_code"]["changed"])

        assertions = assertions_by_key(payload)
        status_diff = assertions["status code is 200|status_code"]
        self.assertEqual(status_diff["current_status"], "failed")
        self.assertEqual(status_diff["baseline_status"], "ok")
        self.assertEqual(status_diff["current_actual"], 500)
        self.assertEqual(status_diff["baseline_actual"], 200)
        self.assertTrue(status_diff["changed"])
        self.assertNotIn("body has id|$.id", assertions)

    def test_timeout_ui_run_compares_page_url_title_and_assertions(self) -> None:
        with isolated_storage():
            check = create_check("Dashboard page", "ui")
            baseline = storage.create_run(check)
            storage.finish_run(
                int(baseline["id"]),
                run_payload(
                    "ok",
                    response_snapshot={
                        "page": {"url": "https://app.example.com/dashboard", "title": "Dashboard"},
                        "assertions": [
                            assertion("title contains", "title", "ok", "Dashboard", "Dashboard", "ok"),
                        ],
                    },
                ),
            )
            current = storage.create_run(check)
            storage.finish_run(
                int(current["id"]),
                run_payload(
                    "timeout",
                    response_snapshot={
                        "page": {"url": "https://app.example.com/dashboard", "title": "Error"},
                        "assertions": [
                            assertion("title contains", "title", "failed", "Error", "Dashboard", "title mismatch"),
                        ],
                    },
                    error_message="page load timeout",
                ),
            )

            response = TestClient(app).get(f"/api/runs/{current['id']}/compare-success", headers=project_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["baseline_run"]["id"], baseline["id"])

        fields = fields_by_key(payload)
        self.assertEqual(fields["url"]["current"], "https://app.example.com/dashboard")
        self.assertEqual(fields["url"]["baseline"], "https://app.example.com/dashboard")
        self.assertFalse(fields["url"]["changed"])
        self.assertEqual(fields["title"]["current"], "Error")
        self.assertEqual(fields["title"]["baseline"], "Dashboard")
        self.assertTrue(fields["title"]["changed"])

        title_diff = assertions_by_key(payload)["title contains|title"]
        self.assertEqual(title_diff["current_status"], "failed")
        self.assertEqual(title_diff["baseline_status"], "ok")
        self.assertTrue(title_diff["changed"])

    def test_compare_success_preserves_duplicate_assertion_differences(self) -> None:
        with isolated_storage():
            check = create_check("API duplicate assertions", "api")
            baseline = storage.create_run(check)
            storage.finish_run(
                int(baseline["id"]),
                run_payload(
                    "ok",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/items"},
                    response_snapshot={
                        "status_code": 200,
                        "assertions": [
                            assertion("field equals", "$.items[0].state", "ok", "ready", "ready", "ok", operator="="),
                            assertion("field equals", "$.items[0].state", "ok", "active", "active", "ok", operator="="),
                        ],
                    },
                ),
            )
            current = storage.create_run(check)
            storage.finish_run(
                int(current["id"]),
                run_payload(
                    "failed",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/items"},
                    response_snapshot={
                        "status_code": 200,
                        "assertions": [
                            assertion(
                                "field equals",
                                "$.items[0].state",
                                "failed",
                                "stale",
                                "ready",
                                "first item mismatch",
                                operator="=",
                            ),
                            assertion(
                                "field equals",
                                "$.items[0].state",
                                "failed",
                                "inactive",
                                "active",
                                "second item mismatch",
                                operator="=",
                            ),
                        ],
                    },
                    error_message="duplicate assertion failures",
                ),
            )

            response = TestClient(app).get(f"/api/runs/{current['id']}/compare-success", headers=project_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        duplicate_diffs = assertion_diffs_for(payload, "field equals", "$.items[0].state")
        self.assertEqual(len(duplicate_diffs), 2)
        self.assertEqual([item["current_actual"] for item in duplicate_diffs], ["stale", "inactive"])
        self.assertEqual([item["baseline_actual"] for item in duplicate_diffs], ["ready", "active"])
        self.assertEqual(len({item["key"] for item in duplicate_diffs}), 2)
        self.assertTrue(all(item["changed"] for item in duplicate_diffs))

    def test_compare_success_reports_operator_and_marks_semantic_assertion_changes(self) -> None:
        with isolated_storage():
            check = create_check("API assertion semantics", "api")
            baseline = storage.create_run(check)
            storage.finish_run(
                int(baseline["id"]),
                run_payload(
                    "ok",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/items"},
                    response_snapshot={
                        "status_code": 200,
                        "assertions": [
                            assertion("items length", "$.items", "ok", 5, 3, "ok", operator=">="),
                            assertion("items threshold", "$.items", "ok", 5, 2, "ok", operator=">="),
                        ],
                    },
                ),
            )
            current = storage.create_run(check)
            storage.finish_run(
                int(current["id"]),
                run_payload(
                    "failed",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/items"},
                    response_snapshot={
                        "status_code": 500,
                        "assertions": [
                            assertion("items length", "$.items", "ok", 5, 3, "ok", operator=">"),
                            assertion("items threshold", "$.items", "ok", 5, 4, "ok", operator=">="),
                        ],
                    },
                    error_message="status mismatch",
                ),
            )

            response = TestClient(app).get(f"/api/runs/{current['id']}/compare-success", headers=project_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        operator_diffs = assertion_diffs_for(payload, "items length", "$.items")
        self.assertEqual(len(operator_diffs), 1)
        operator_diff = operator_diffs[0]
        self.assertEqual(operator_diff["current_operator"], ">")
        self.assertEqual(operator_diff["baseline_operator"], ">=")
        self.assertTrue(operator_diff["changed"])

        config_diffs = assertion_diffs_for(payload, "items threshold", "$.items")
        self.assertEqual(len(config_diffs), 1)
        config_diff = config_diffs[0]
        self.assertEqual(config_diff["current_operator"], ">=")
        self.assertEqual(config_diff["baseline_operator"], ">=")
        self.assertEqual(config_diff["current_expected"], 4)
        self.assertEqual(config_diff["baseline_expected"], 2)
        self.assertTrue(config_diff["changed"])

    def test_compare_success_unavailable_without_success_baseline(self) -> None:
        with isolated_storage():
            check = create_check("API without baseline", "api")
            current = storage.create_run(check)
            storage.finish_run(
                int(current["id"]),
                run_payload(
                    "failed",
                    request_snapshot={"method": "GET", "url": "https://api.example.com/v1/health"},
                    response_snapshot={"status_code": 503, "assertions": []},
                    error_message="service unavailable",
                ),
            )

            response = TestClient(app).get(f"/api/runs/{current['id']}/compare-success", headers=project_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["available"])
        self.assertTrue(payload["message"])
        self.assertEqual(payload["current_run"]["id"], current["id"])
        self.assertIsNone(payload["baseline_run"])
        self.assertEqual(payload["fields"], [])
        self.assertEqual(payload["assertions"], [])


class isolated_storage:
    def __enter__(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._patch = patch.object(storage, "DB_PATH", Path(self._temp_dir.name) / "pulseguard.db")
        self._patch.__enter__()
        storage.init_db()
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._patch.__exit__(exc_type, exc, traceback)
        self._temp_dir.__exit__(exc_type, exc, traceback)


def create_check(name: str, check_type: str) -> dict[str, object]:
    return storage.create_check(
        {
            "name": name,
            "type": check_type,
            "enabled": True,
            "interval_seconds": 300,
            "timeout_ms": 10000,
            "entry_url": "https://example.com",
            "viewport_mode": "web",
            "method": "GET" if check_type == "api" else "",
            "headers_json": "{}",
            "body": "",
            "assertions_json": "[]",
            "setup_script": "",
            "script": "async def check(ctx):\n    pass\n",
            "tags": "",
        }
    )


def run_payload(
    status: str,
    request_snapshot: dict[str, object] | None = None,
    response_snapshot: dict[str, object] | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "finished_at": storage.now_iso(),
        "duration_ms": 50,
        "error_message": error_message,
        "error_stack": None,
        "logs": "",
        "screenshot_path": None,
        "trace_path": None,
        "response_path": None,
        "request_snapshot": json.dumps(request_snapshot, ensure_ascii=False) if request_snapshot else None,
        "response_snapshot": json.dumps(response_snapshot, ensure_ascii=False) if response_snapshot else None,
    }


def assertion(
    rule: str,
    path: str,
    status: str,
    actual: object,
    expected: object,
    message: str,
    operator: str | None = None,
) -> dict[str, object]:
    result = {
        "rule": rule,
        "path": path,
        "status": status,
        "actual": actual,
        "expected": expected,
        "message": message,
    }
    if operator is not None:
        result["operator"] = operator
    return result


def fields_by_key(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(item["key"]): item for item in payload["fields"]}  # type: ignore[index]


def assertions_by_key(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(item["key"]): item for item in payload["assertions"]}  # type: ignore[index]


def assertion_diffs_for(payload: dict[str, object], rule: str, path: str) -> list[dict[str, object]]:
    return [
        item
        for item in payload["assertions"]  # type: ignore[index]
        if item.get("rule") == rule and item.get("path") == path
    ]


if __name__ == "__main__":
    unittest.main()
