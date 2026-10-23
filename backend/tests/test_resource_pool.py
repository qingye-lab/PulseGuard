from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend.app.resource_pool import BrowserPool, ProbeResourcePool, ResourcePoolError


class BrowserPoolTests(unittest.TestCase):
    def test_hanging_context_release_wakes_waiter_and_retires_browser(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            pool = browser_pool(fake, size=1)
            await pool.start(settings())
            lease = await pool.acquire_context(settings(), {})
            original_browser = lease.browser

            async def hanging_close():
                await asyncio.Event().wait()

            lease.context.close = hanging_close
            waiter = asyncio.create_task(pool.acquire_context(settings(), {}))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            try:
                with patch("backend.app.resource_pool.RESOURCE_CLEANUP_TIMEOUT_SECONDS", 0.02):
                    await pool.release_context(lease)
                self.assertTrue(pool.snapshot()["retiring"])
                self.assertFalse(original_browser.closed)
                next_lease = await asyncio.wait_for(waiter, 1)
                self.assertTrue(original_browser.closed)
                self.assertIsNot(next_lease.browser, original_browser)
                self.assertNotIn(lease, pool._active_contexts)
                await pool.release_context(next_lease)
                self.assertEqual(pool.snapshot()["in_use"], 0)
            finally:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
                await pool.shutdown()

        asyncio.run(scenario())

    def test_start_prepares_single_browser_and_five_default_contexts(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            pool = browser_pool(fake, size=5)

            await pool.start(settings())

            browser = fake.chromium.launches[0]
            self.assertEqual(len(fake.chromium.launches), 1)
            self.assertEqual(len(browser.contexts), 5)
            self.assertTrue(all(context.options == {"viewport": {"width": 1366, "height": 768}} for context in browser.contexts))
            self.assertEqual(pool.snapshot()["browser_processes"], 1)
            self.assertEqual(pool.snapshot()["ready"], 5)
            self.assertEqual(pool.snapshot()["in_use"], 0)

            await pool.shutdown()
            self.assertTrue(browser.closed)
            self.assertTrue(all(context.closed for context in browser.contexts))
            self.assertTrue(fake.stopped)

        asyncio.run(scenario())

    def test_context_release_closes_task_context_and_replenishes_pool(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            pool = browser_pool(fake, size=5)
            context_options = {"viewport": {"width": 1366, "height": 768}}

            await pool.start(settings())
            lease = await pool.acquire_context(settings(), context_options)

            browser = fake.chromium.launches[0]
            self.assertIs(lease.browser, browser)
            self.assertEqual(pool.snapshot()["ready"], 4)
            self.assertEqual(pool.snapshot()["in_use"], 1)

            await pool.release_context(lease)

            self.assertFalse(browser.closed)
            self.assertEqual(len(fake.chromium.launches), 1)
            self.assertEqual(pool.snapshot()["ready"], 4)
            self.assertEqual(pool.snapshot()["in_use"], 0)
            self.assertEqual(pool.snapshot()["total"], 4)
            self.assertEqual(sum(1 for context in browser.contexts if context.closed), 1)

            next_lease = await pool.acquire_context(settings(), context_options)
            self.assertEqual(pool.snapshot()["ready"], 4)
            self.assertEqual(pool.snapshot()["total"], 5)
            self.assertEqual(sum(1 for context in browser.contexts if context.closed), 1)
            await pool.release_context(next_lease)
            self.assertEqual(pool.snapshot()["ready"], 4)
            self.assertEqual(pool.snapshot()["in_use"], 0)

            await pool.shutdown()

        asyncio.run(scenario())

    def test_context_release_recycles_browser_after_configured_run_count(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            pool = browser_pool(fake, size=1)
            pool_settings = {**settings(), "browser_recycle_after_runs": 2}
            context_options = {"viewport": {"width": 1366, "height": 768}}

            await pool.start(pool_settings)
            first_browser = fake.chromium.launches[0]

            first_lease = await pool.acquire_context(pool_settings, context_options)
            await pool.release_context(first_lease)
            self.assertFalse(first_browser.closed)
            self.assertEqual(len(fake.chromium.launches), 1)
            self.assertEqual(pool.snapshot()["completed_contexts"], 1)

            second_lease = await pool.acquire_context(pool_settings, context_options)
            await pool.release_context(second_lease)

            self.assertFalse(first_browser.closed)
            self.assertTrue(pool.snapshot()["retiring"])
            self.assertEqual(pool.snapshot()["completed_contexts"], 2)

            third_lease = await pool.acquire_context(pool_settings, context_options)
            self.assertTrue(first_browser.closed)
            self.assertEqual(len(fake.chromium.launches), 2)
            self.assertIsNot(third_lease.browser, first_browser)
            self.assertEqual(pool.snapshot()["completed_contexts"], 0)
            self.assertEqual(pool.snapshot()["recycle_after_runs"], 2)
            await pool.release_context(third_lease)

            await pool.shutdown()

        asyncio.run(scenario())

    def test_h5_context_uses_matching_options_without_launching_second_browser(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            pool = browser_pool(fake, size=5)
            h5_options = {
                "viewport": {"width": 390, "height": 844},
                "is_mobile": True,
                "has_touch": True,
                "user_agent": "mobile",
            }

            await pool.start(settings())
            lease = await pool.acquire_context(settings(), h5_options)

            browser = fake.chromium.launches[0]
            self.assertEqual(len(fake.chromium.launches), 1)
            self.assertEqual(lease.context.options, h5_options)
            self.assertEqual(pool.snapshot()["total"], 5)
            self.assertEqual(sum(1 for context in browser.contexts if context.closed), 1)

            await lease.context.close()
            await pool.release_context(lease)
            self.assertEqual(pool.snapshot()["ready"], 4)
            self.assertEqual(len(fake.chromium.launches), 1)

            await pool.shutdown()

    def test_recycle_does_not_block_release_when_browser_launch_is_slow(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            pool = browser_pool(fake, size=1)
            pool_settings = {**settings(), "browser_recycle_after_runs": 1}

            await pool.start(pool_settings)
            first_browser = fake.chromium.launches[0]
            fake.chromium.launch_delay = 0.05

            lease = await pool.acquire_context(pool_settings, {})
            with patch("backend.app.resource_pool.RESOURCE_CLEANUP_TIMEOUT_SECONDS", 0.01):
                await pool.release_context(lease)

            self.assertTrue(pool.snapshot()["retiring"])
            self.assertFalse(first_browser.closed)
            self.assertEqual(len(fake.chromium.launches), 1)

            next_lease = await pool.acquire_context(pool_settings, {})
            self.assertTrue(first_browser.closed)
            self.assertEqual(len(fake.chromium.launches), 2)
            self.assertIsNot(next_lease.browser, first_browser)
            self.assertEqual(pool.snapshot()["completed_contexts"], 0)
            await pool.release_context(next_lease)

            await pool.shutdown()

        asyncio.run(scenario())

    def test_browser_launch_timeout_reports_clear_error(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            fake.chromium.launch_delay = 1.0
            pool = browser_pool(fake, size=1)

            with patch("backend.app.resource_pool.BROWSER_LAUNCH_TIMEOUT_SECONDS", 0.02):
                with self.assertRaises(ResourcePoolError) as raised:
                    await pool.start(settings())

            self.assertIn("浏览器启动超时", str(raised.exception))
            self.assertIn("浏览器启动超时", pool.snapshot()["error"])
            self.assertEqual(pool.snapshot()["browser_processes"], 0)

            await pool.shutdown()

        asyncio.run(scenario())

    def test_probe_pool_keeps_retired_browser_pool_until_active_context_is_released(self) -> None:
        async def scenario() -> None:
            fake = FakePlaywright()
            resources = ProbeResourcePool(api_pool_size=1, browser_pool_sizes_value={"chromium": 5})
            pool = resources._browser_pool("chromium")  # type: ignore[attr-defined]

            async def ensure_playwright() -> FakePlaywright:
                pool._playwright = fake  # type: ignore[attr-defined]
                return fake

            pool._ensure_playwright_locked = ensure_playwright  # type: ignore[method-assign]

            active_settings = {
                **settings(),
                "enabled_browser_types": ["chromium"],
                "prewarmed_browser_types": ["chromium"],
                "browser_pool_sizes": {"chromium": 5},
            }
            cold_settings = {
                **settings(),
                "enabled_browser_types": ["chromium"],
                "prewarmed_browser_types": [],
                "browser_pool_sizes": {"chromium": 5},
            }

            await resources.start(active_settings, api_pool_size=1, browser_pool_sizes_value={"chromium": 5})
            lease = await resources.acquire_browser_context(active_settings, {"viewport": {"width": 1366, "height": 768}})
            browser = fake.chromium.launches[0]

            await resources.reload(cold_settings, api_pool_size=1, browser_pool_sizes_value={"chromium": 5})
            self.assertFalse(browser.closed)

            await lease.context.close()
            await resources.release_browser_context(lease)
            self.assertTrue(browser.closed)

        asyncio.run(scenario())


def browser_pool(fake: "FakePlaywright", size: int) -> BrowserPool:
    pool = BrowserPool(size)

    async def ensure_playwright() -> FakePlaywright:
        pool._playwright = fake  # type: ignore[attr-defined]
        return fake

    pool._ensure_playwright_locked = ensure_playwright  # type: ignore[method-assign]
    return pool


def settings() -> dict[str, object]:
    return {
        "browser_type": "chromium",
        "browser_headless": True,
        "browser_viewport": "1366x768",
        "browser_proxy": "",
    }


class FakePlaywright:
    def __init__(self) -> None:
        self.chromium = FakeBrowserType()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeBrowserType:
    def __init__(self) -> None:
        self.launches: list[FakeBrowser] = []
        self.launch_options: list[dict[str, object]] = []
        self.launch_delay = 0.0

    async def launch(self, **options: object) -> "FakeBrowser":
        if self.launch_delay:
            await asyncio.sleep(self.launch_delay)
        browser = FakeBrowser()
        self.launch_options.append(options)
        self.launches.append(browser)
        return browser


class FakeBrowser:
    version = "120.0"

    def __init__(self) -> None:
        self.contexts: list[FakeBrowserContext] = []
        self.closed = False

    async def new_context(self, **options: object) -> "FakeBrowserContext":
        context = FakeBrowserContext(options)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return not self.closed


class FakeBrowserContext:
    def __init__(self, options: dict[str, object]) -> None:
        self.options = options
        self.closed = False

    async def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
