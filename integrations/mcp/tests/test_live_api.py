from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters


ROOT = Path(__file__).resolve().parents[3]
BACKEND_PYTHON = Path(os.environ.get("PULSEGUARD_TEST_BACKEND_PYTHON", str(
    ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
)))


@unittest.skipUnless(BACKEND_PYTHON.is_file(), "Sync the root backend environment first with uv sync --locked")
class LiveApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_to_real_api_execution_history_and_project_isolation(self):
        with tempfile.TemporaryDirectory(prefix="pulseguard-mcp-test-") as temporary:
            root = Path(temporary)
            fixture = root / "instance"
            fixture.mkdir()
            with (root / "api.log").open("w+", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [str(BACKEND_PYTHON), str(Path(__file__).with_name("api_fixture.py")), str(fixture)],
                    cwd=ROOT, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    deadline = time.monotonic() + 30
                    while not (fixture / "ready.json").exists():
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            self.fail("Fixture failed to start: " + log.read())
                        await asyncio.sleep(0.1)
                    info = json.loads((fixture / "ready.json").read_text(encoding="utf-8"))
                    project = info["project_id"]
                    env = {k: v for k, v in os.environ.items() if not k.startswith("PULSEGUARD_")}
                    env.update({
                        "PULSEGUARD_MCP_BASE_URL": info["url"], "PULSEGUARD_MCP_PROJECT_IDS": str(project),
                        "PULSEGUARD_MCP_ALLOW_EXECUTION": "true", "PULSEGUARD_MCP_EXECUTION_TIMEOUT": "20",
                        "PULSEGUARD_MCP_ALLOW_CONFIGURATION": "true", "PULSEGUARD_MCP_ALLOW_PLATFORM_READ": "true",
                    })
                    uv = shutil.which("uv")
                    self.assertIsNotNone(uv, "uv is required for the documented MCP launch command")
                    params = StdioServerParameters(command=uv, args=[
                        "run", "--locked", "--directory", str(ROOT / "integrations" / "mcp"), "pulseguard-mcp",
                    ], env=env)
                    # Legacy mode proves compatibility with existing MCP hosts as well as
                    # the SDK's current in-memory protocol covered by test_server.py.
                    async with Client(params, mode="legacy", read_timeout_seconds=30) as client:
                        tools = (await client.list_tools()).tools
                        self.assertEqual(len(tools), 29)
                        async def call(name, args=None):
                            result = await client.call_tool(name, args or {})
                            self.assertFalse(result.is_error, str(result))
                            return result.structured_content
                        health = await call("health")
                        self.assertEqual(health["service"]["status"], "ok")
                        projects = await call("list_projects")
                        self.assertEqual([item["id"] for item in projects["items"]], [project])
                        checks = await call("list_checks", {"project_id": project, "tag": "smoke"})
                        self.assertEqual(checks["items"][0]["id"], info["check_id"])
                        self.assertNotIn("fixture-secret", str(checks))
                        forbidden = await client.call_tool("get_check", {"project_id": info["other_project_id"], "check_id": info["other_check_id"]})
                        self.assertTrue(forbidden.is_error)
                        crossed = await client.call_tool("get_check", {"project_id": project, "check_id": info["other_check_id"]})
                        self.assertTrue(crossed.is_error)

                        first = await call("run_check", {"project_id": project, "check_id": info["check_id"]})
                        self.assertTrue(first["passed"], first)
                        second = await call("run_check", {"project_id": project, "check_id": info["check_id"]})
                        self.assertFalse(second["passed"])
                        self.assertEqual(second["run"]["status"], "failed")
                        run_id = second["run"]["id"]
                        runs = await call("list_runs", {"project_id": project, "check_id": info["check_id"]})
                        self.assertEqual(runs["total"], 2)
                        self.assertEqual(runs["items"][0]["id"], run_id)
                        stats = await call("get_run_statistics", {"project_id": project,
                            "start": "2020-01-01T00:00:00+08:00", "end": "2029-01-01T00:00:00+08:00"})
                        self.assertEqual(stats["total"], 2)
                        self.assertEqual(stats["sources"], {"retained": 2, "archived": 0})
                        self.assertFalse(stats["coverage"]["complete"])
                        overview = await call("get_project_overview", {"project_id": project})
                        self.assertEqual(overview["project_id"], project)
                        detail = await call("get_run", {"project_id": project, "run_id": run_id, "include_logs": True})
                        self.assertNotIn("fixture-secret", str(detail))
                        summary = await call("get_failure_summary", {"project_id": project, "run_id": run_id})
                        self.assertEqual(summary["failure_kind"], "target")
                        comparison = await call("compare_success", {"project_id": project, "run_id": run_id})
                        self.assertTrue(comparison["available"])
                        self.assertEqual(comparison["baseline_run"]["id"], first["run"]["id"])
                        absent = await client.call_tool("get_screenshot", {"project_id": project, "run_id": run_id})
                        self.assertTrue(absent.is_error)
                        today = datetime.now().astimezone()
                        trends = await call("get_monitoring_trends", {"project_id": project,
                            "start": (today - timedelta(days=1)).isoformat(), "end": today.isoformat()})
                        self.assertEqual(trends["metric_scope"], "health_observations")
                        incidents = await call("list_incidents", {"project_id": project})
                        self.assertIn("items", incidents)
                        audit = await call("list_audit_events", {"project_id": project})
                        self.assertTrue(all(row["project_id"] == project for row in audit["items"]))
                        platform = await call("get_platform_status")
                        self.assertGreater(platform["database"]["file_size_bytes"], 0)
                        nodes = await call("list_runners")
                        self.assertIn("items", nodes)
                        disabled = await call("set_check_enabled", {"project_id": project, "check_id": info["check_id"], "enabled": False})
                        self.assertFalse(disabled["enabled"])
                        enabled = await call("set_check_enabled", {"project_id": project, "check_id": info["check_id"], "enabled": True})
                        self.assertTrue(enabled["enabled"])
                        recovery = await call("confirm_recovery", {"project_id": project, "check_id": info["check_id"]})
                        self.assertFalse(recovery["passed"])  # Fixture remains unavailable.
                        run_id = recovery["run"]["id"]
                        edit = await call("get_check_edit_context", {"project_id": project, "check_id": info["check_id"]})
                        self.assertNotIn("fixture-secret", str(edit))
                        schema = await call("get_check_edit_schema", {"project_id": project})
                        self.assertIn("script", schema["schema"]["properties"])
                        draft = await call("preview_check_change", {"project_id": project, "check_id": info["check_id"],
                            "expected_hash": edit["definition_hash"], "changes": {"name": "MCP edited task", "interval_seconds": 600}})
                        for _ in range(2):
                            saved = await call("apply_check_change", {"project_id": project, "change_id": draft["id"], "expected_digest": draft["digest"]})
                            self.assertEqual(saved["state"], "applied")
                        versions = await call("list_check_versions", {"project_id": project, "check_id": info["check_id"]})
                        self.assertEqual(len(versions["items"]), 2)
                        receipt = await call("submit_execution_batch", {"project_id": project,
                            "check_ids": [info["check_id"]], "idempotency_key": "contract-batch-1"})
                        repeated = await call("submit_execution_batch", {"project_id": project,
                            "check_ids": [info["check_id"]], "idempotency_key": "contract-batch-1"})
                        self.assertEqual(receipt["id"], repeated["id"])
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            receipt = await call("get_execution_batch", {"project_id": project, "idempotency_key": "contract-batch-1"})
                            if receipt["state"] == "finished":
                                break
                            await asyncio.sleep(0.1)
                        self.assertEqual(receipt["counts"], {"completed": 1})
                        self.assertEqual(receipt["items"][0]["result_status"], "failed")
                        run_id = receipt["items"][0]["run_id"]
                        self.assertEqual(receipt["items"][0]["run_ids"], [run_id])
                        runs = await call("list_runs", {"project_id": project, "trigger": receipt["items"][0]["trigger"]})
                        self.assertEqual(runs["total"], 1)

                    # The server-owned health state was changed by the real execution.
                    async with httpx.AsyncClient() as http:
                        result = await http.get(info["url"] + f"/api/checks/{info['check_id']}", headers={"X-PulseGuard-Project-Id": str(project)})
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(result.json()["last_run_id"], run_id)
                finally:
                    process.terminate()
                    await asyncio.to_thread(process.wait, timeout=10)


if __name__ == "__main__":
    unittest.main()
