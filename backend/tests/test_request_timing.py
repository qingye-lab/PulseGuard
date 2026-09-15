from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app import request_timing


class RequestTimingTests(unittest.TestCase):
    def test_concurrent_sync_handlers_have_isolated_metrics_and_safe_logs(self) -> None:
        app = FastAPI()
        app.add_middleware(request_timing.RequestTimingMiddleware)
        ready = Barrier(2)

        @app.get("/api/runs-page")
        def read(rows: int):
            request_timing.handler_started()
            with request_timing.phase("first_query" if rows == 1 else "second_query"):
                ready.wait(5)
            request_timing.record_count("runs_rows", rows)
            return {"rows": rows}

        def get(rows: int):
            return TestClient(app).get(
                f"/api/runs-page?rows={rows}&token=private-query",
                headers={"Authorization": "Bearer private-header", "X-Request-ID": "untrusted-id"},
            )

        with patch.object(request_timing, "SLOW_REQUEST_MS", 0), self.assertLogs(
            request_timing.logger, level="WARNING"
        ) as logs, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(get, [1, 2]))
        ids = {response.headers["x-request-id"] for response in results}
        self.assertEqual(len(ids), 2)
        self.assertIn("first_query", results[0].headers["server-timing"])
        self.assertNotIn("second_query", results[0].headers["server-timing"])
        self.assertIn("second_query", results[1].headers["server-timing"])
        self.assertNotIn("first_query", results[1].headers["server-timing"])
        records = [json.loads(record.getMessage().split("slow_api_request ", 1)[1]) for record in logs.records]
        self.assertEqual({record["request_id"] for record in records}, ids)
        self.assertEqual({record["counts"]["runs_rows"] for record in records}, {1, 2})
        for secret in ["private-query", "private-header", "untrusted-id"]:
            self.assertNotIn(secret, " ".join(logs.output))
        self.assertIsNone(request_timing._current.get())

    def test_error_timing_is_closed_and_does_not_leak_into_next_request(self) -> None:
        app = FastAPI()
        app.add_middleware(request_timing.RequestTimingMiddleware)

        @app.get("/api/runs-page")
        def fail():
            with request_timing.phase("failed_query"):
                raise HTTPException(400, "invalid filter")

        @app.get("/api/health")
        def health():
            return {"ok": True}

        @app.post("/api/runs-page")
        def write():
            return {"ok": True}

        with TestClient(app) as client:
            failure = client.get("/api/runs-page")
            healthy = client.get("/api/health")
            excluded = client.post("/api/runs-page")
        self.assertEqual(failure.status_code, 400)
        self.assertIn("failed_query", failure.headers["server-timing"])
        self.assertNotIn("failed_query", healthy.headers["server-timing"])
        self.assertNotIn("server-timing", excluded.headers)
        self.assertIsNone(request_timing._current.get())
