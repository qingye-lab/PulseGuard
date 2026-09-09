from __future__ import annotations

import os
import unittest

from playwright.async_api import async_playwright

from backend.app.ui_scan_script import UI_SCAN_SCRIPT
from backend.app.ui_assertions import _wait_for_selector_collection


class UiScanRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        try:
            self.browser = await self.playwright.chromium.launch()
        except Exception as exc:
            missing_browser = "Executable doesn't exist" in str(exc)
            required = os.getenv("PULSEGUARD_REQUIRE_BROWSER_TESTS") == "1"
            if missing_browser and not required:
                self.skipTest("Chromium missing; run: uv run python -m playwright install chromium")
            raise
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()

    async def fixture(self, count: int, module: bool = True, suffix: str = "abc") -> None:
        def cls(role: str) -> str:
            stem = "inspection-report-card" + role
            return f"_{stem}_{suffix}_1" if module else stem
        await self.page.set_content("<style>div {padding:10px;margin:5px}</style>" + "".join(
            f'<div class="other {cls("")}"><div class="{cls("__title-row")}">Title {i}</div>'
            f'<div class="{cls("__content")}">Content {i}</div></div>' for i in range(count)
        ))

    async def test_bem_roles_are_distinct_and_healthy_single_card_passes(self) -> None:
        for module in (False, True):
            with self.subTest(module=module):
                await self.fixture(1, module)
                scan = await self.page.evaluate(UI_SCAN_SCRIPT)
                title = next(c for c in scan["candidates"] if c["text"] == "Title 0")
                self.assertEqual(await self.page.locator(title["selector"]).count(), 1)
                self.assertTrue((await _wait_for_selector_collection(self.page, title, "visible", 1000))[0])

    async def test_repeated_fields_survive_build_hash_change_and_detect_missing_field(self) -> None:
        await self.fixture(2)
        scan = await self.page.evaluate(UI_SCAN_SCRIPT)
        title = next(c for c in scan["candidates"] if c["text"] == "Title 0")
        self.assertEqual(title["match_mode"], "every_scope")
        self.assertNotEqual(title["scope_selector"], title["relative_selector"])
        self.assertEqual(await self.page.locator(title["scope_selector"]).count(), 2)
        await self.fixture(2, suffix="newBuild")
        self.assertTrue((await _wait_for_selector_collection(self.page, title, "visible", 1000))[0])
        await self.page.locator(title["selector"]).last.evaluate("el => el.remove()")
        passed, actual = await _wait_for_selector_collection(self.page, title, "visible", 10)
        self.assertFalse(passed)
        self.assertIn("缺失 1", actual)

    async def test_nested_scopes_are_not_recommended_as_peer_collections(self) -> None:
        await self.page.set_content('''<style>div{padding:20px}</style>
          <div class="card"><div class="card-title">Outer</div>
            <div class="card"><div class="card-title">Inner</div></div></div>''')
        scan = await self.page.evaluate(UI_SCAN_SCRIPT)
        for candidate in scan["candidates"]:
            self.assertNotEqual(candidate["match_mode"], "every_scope")
