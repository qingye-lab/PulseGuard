from __future__ import annotations

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, patch

from backend.app import browser_installation


class _BlockingProcess:
    pid = 12345

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.communication_cancelled = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.communication_cancelled = True
            raise
        return b"", b""

    async def wait(self) -> int | None:
        return self.returncode


class _CompletedProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


class BrowserInstallationTests(unittest.TestCase):
    def _reset_installation_state(self) -> None:
        browser_installation._install_tasks.clear()
        browser_installation._install_status.clear()

    def test_installed_browser_types_uses_direct_probe_outside_event_loop(self) -> None:
        with patch.object(browser_installation, "_installed_browser_types_direct", return_value=["chromium"]) as direct, patch.object(
            browser_installation, "_installed_browser_types_subprocess", return_value=[]
        ) as subprocess_probe:
            self.assertEqual(browser_installation.installed_browser_types(), ["chromium"])

        direct.assert_called_once_with()
        subprocess_probe.assert_not_called()

    def test_installed_browser_types_uses_subprocess_probe_inside_event_loop(self) -> None:
        async def run() -> list[str]:
            with patch.object(browser_installation, "_installed_browser_types_direct", return_value=[]) as direct, patch.object(
                browser_installation, "_installed_browser_types_subprocess", return_value=["chromium"]
            ) as subprocess_probe:
                result = browser_installation.installed_browser_types()
            direct.assert_not_called()
            subprocess_probe.assert_called_once_with()
            return result

        self.assertEqual(asyncio.run(run()), ["chromium"])

    def test_shutdown_cancels_lock_waiting_install_and_allows_retry(self) -> None:
        async def run() -> None:
            self._reset_installation_state()
            browser_installation._install_lock = asyncio.Lock()
            await browser_installation._install_lock.acquire()
            try:
                with patch.object(browser_installation, "installed_browser_types", return_value=[]), patch.object(
                    browser_installation.asyncio, "create_subprocess_exec", new=AsyncMock()
                ) as create_process:
                    browser_installation.schedule_browser_install(["chromium"])
                    task = browser_installation._install_tasks["chromium"]
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())

                    await browser_installation.shutdown_browser_installation()

                    self.assertTrue(task.cancelled())
                    create_process.assert_not_awaited()
                    self.assertNotIn("chromium", browser_installation._install_status)
            finally:
                browser_installation._install_lock.release()

            completed_process = _CompletedProcess()
            with patch.object(browser_installation, "installed_browser_types", return_value=[]), patch.object(
                browser_installation.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=completed_process),
            ):
                result = browser_installation.schedule_browser_install(["chromium"])
                retry_task = browser_installation._install_tasks["chromium"]
                await retry_task

            self.assertEqual(result["scheduled_browser_types"], ["chromium"])
            self.assertEqual(browser_installation._install_status["chromium"]["status"], "installed")
            await browser_installation.shutdown_browser_installation()

        asyncio.run(run())

    def test_terminate_process_stops_harmless_child_process_tree(self) -> None:
        async def run() -> None:
            child_script = "import time; time.sleep(30)"
            parent_script = (
                "import subprocess, sys, time; "
                f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
                "print(child.pid, flush=True); time.sleep(30)"
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                parent_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **browser_installation._installer_process_kwargs(),
            )
            try:
                await asyncio.wait_for(process.stdout.readline(), timeout=1)
                await browser_installation._terminate_process(process)

                self.assertIsNotNone(process.returncode)
                self.assertEqual(await asyncio.wait_for(process.stdout.read(), timeout=1), b"")
            finally:
                if process.returncode is None:
                    await browser_installation._terminate_process(process)

        asyncio.run(run())

    def test_terminate_process_uses_process_group_tree_termination(self) -> None:
        async def run() -> None:
            process = _BlockingProcess()
            if sys.platform == "win32":
                with patch.object(browser_installation, "_taskkill_process_tree", new=AsyncMock()) as taskkill:
                    await browser_installation._terminate_process(process)
                taskkill.assert_awaited_once_with(process.pid)
            else:
                with patch.object(browser_installation.os, "killpg") as killpg:
                    await browser_installation._terminate_process(process)
                killpg.assert_called_once_with(process.pid, browser_installation.signal.SIGTERM)

        asyncio.run(run())

    def test_shutdown_terminates_active_download_and_allows_retry(self) -> None:
        async def run() -> None:
            self._reset_installation_state()
            browser_installation._install_lock = asyncio.Lock()
            process = _BlockingProcess()
            with patch.object(browser_installation, "_terminate_process", new=AsyncMock()) as terminate_process, patch.object(
                browser_installation, "installed_browser_types", return_value=[]
            ), patch.object(
                browser_installation.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ):
                browser_installation.schedule_browser_install(["chromium"])
                task = browser_installation._install_tasks["chromium"]
                await asyncio.wait_for(process.started.wait(), timeout=1)

                await browser_installation.shutdown_browser_installation()

            self.assertTrue(task.cancelled())
            self.assertTrue(process.communication_cancelled)
            terminate_process.assert_awaited_once_with(process)
            self.assertEqual(browser_installation._install_tasks, {})
            self.assertNotIn("chromium", browser_installation._install_status)

            completed_process = _CompletedProcess()
            with patch.object(browser_installation, "installed_browser_types", return_value=[]), patch.object(
                browser_installation.asyncio,
                "create_subprocess_exec",
                new=AsyncMock(return_value=completed_process),
            ):
                result = browser_installation.schedule_browser_install(["chromium"])
                retry_task = browser_installation._install_tasks["chromium"]
                await retry_task

            self.assertEqual(result["scheduled_browser_types"], ["chromium"])
            self.assertEqual(browser_installation._install_status["chromium"]["status"], "installed")
            await browser_installation.shutdown_browser_installation()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
