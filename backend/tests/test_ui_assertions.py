from __future__ import annotations

import asyncio
import unittest

from backend.app import ui_assertions
from backend.app.context import RunFailure
from backend.app.ui_scan_script import UI_SCAN_SCRIPT
from backend.app.variables import RuntimeVariables


PlaywrightTimeoutError = type("TimeoutError", (Exception,), {"__module__": "playwright.async_api"})


class FakeLocator:
    def __init__(self, *, visible: bool = True, count: int = 1) -> None:
        self.first = self
        self.visible = visible
        self._count = count

    async def wait_for(self, state: str, timeout: int) -> None:
        if state == "visible" and not self.visible:
            raise RuntimeError("not visible")
        if state == "hidden" and self.visible:
            raise RuntimeError("still visible")

    async def count(self) -> int:
        return self._count

    async def evaluate(self, script: str) -> dict[str, object]:
        return {"text": "卡片内容", "hasMedia": False, "visibleChildCount": 0, "width": 120, "height": 80, "visible": True}

    async def evaluate_all(self, script: str) -> int:
        return self._count if self.visible else 0


class TimeoutLocator(FakeLocator):
    def __init__(self) -> None:
        super().__init__(visible=False, count=1)
        self.timeouts: list[int] = []

    async def wait_for(self, state: str, timeout: int) -> None:
        self.timeouts.append(timeout)
        raise PlaywrightTimeoutError()


class ConcurrentLocator(FakeLocator):
    def __init__(self, page: "ConcurrentPage") -> None:
        super().__init__()
        self.page = page

    async def wait_for(self, state: str, timeout: int) -> None:
        self.page.active_waits += 1
        self.page.started_waits += 1
        self.page.max_active_waits = max(self.page.max_active_waits, self.page.active_waits)
        if self.page.started_waits >= 2:
            self.page.release_waits.set()
        try:
            await asyncio.wait_for(self.page.release_waits.wait(), timeout=0.2)
        finally:
            self.page.active_waits -= 1


class FakePage:
    url = "https://example.com/dashboard"

    def __init__(self) -> None:
        self.gotos: list[str] = []
        self.setup_marker = ""

    async def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.gotos.append(url)
        self.url = "https://example.com/dashboard" if url == "https://example.com" else url

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        return None

    async def wait_for_timeout(self, timeout: int) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        if selector == ".invalid":
            raise ValueError("selector parse failed")
        if selector == ".missing":
            return FakeLocator(visible=False, count=0)
        if selector == ".many":
            return FakeLocator(visible=True, count=3)
        if selector == ".items":
            return FakeLocator(visible=True, count=3)
        return FakeLocator()

    def get_by_text(self, text: str) -> FakeLocator:
        return FakeLocator(visible=text == "首页")

    async def evaluate(self, script: str) -> dict[str, object]:
        return {"textLength": 12, "preview": "页面内容", "mediaCount": 1, "interactiveCount": 2, "width": 1440, "height": 900}

    async def title(self) -> str:
        return "PulseGuard 首页"


class TimeoutPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.timeout_locator = TimeoutLocator()

    def locator(self, selector: str) -> TimeoutLocator:
        return self.timeout_locator


class ConcurrentPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.active_waits = 0
        self.started_waits = 0
        self.max_active_waits = 0
        self.release_waits = asyncio.Event()

    def locator(self, selector: str) -> ConcurrentLocator:
        return ConcurrentLocator(self)


class GrowingCountLocator(FakeLocator):
    def __init__(self, counts: list[int]) -> None:
        super().__init__(count=0)
        self.counts = counts

    async def count(self) -> int:
        if len(self.counts) > 1:
            return self.counts.pop(0)
        return self.counts[0]


class GrowingCountPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.growing_locator = GrowingCountLocator([0, 0, 5])

    def locator(self, selector: str) -> GrowingCountLocator:
        return self.growing_locator


class InspectPage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.evaluated_scripts: list[str] = []

    async def evaluate(self, script: str) -> dict[str, object]:
        self.evaluated_scripts.append(script)
        return {
            "viewport": {"width": 390, "height": 844},
            "page_size": {"width": 390, "height": 2400},
            "candidates": [{"name": "筛选", "selector": ".filter-item"}],
        }

    async def screenshot(self, **kwargs: object) -> bytes:
        return b"jpeg"


class SetupInspectPage(InspectPage):
    url = "about:blank"


class FakeContext:
    def __init__(self, assertions_json: str) -> None:
        self.check = {"assertions_json": assertions_json}
        self.settings = {
            "environment_variables": [
                {"id": "title", "name": "EXPECTED_TITLE", "value": "PulseGuard", "secret": False}
            ]
        }
        self.entry_url = "https://example.com"
        self.vars = RuntimeVariables(self.settings)
        self.viewport_mode = "web"
        self.timeout_ms = 1000
        self.console_messages: list[dict[str, str]] = []
        self.response_snapshot = None
        self.logs: list[str] = []
        self.page = FakePage()

    async def new_page(self) -> FakePage:
        return self.page

    def log(self, message: object) -> None:
        self.logs.append(str(message))


class UiAssertionTests(unittest.TestCase):
    def test_explicit_setup_navigation_is_not_overwritten_by_a_second_goto(self) -> None:
        ctx = FakeContext("[]")
        ctx.page = SetupInspectPage()

        async def setup(run_ctx: FakeContext, page: SetupInspectPage) -> None:
            page.url = "https://example.com"
            page.setup_marker = "scrolled-by-user-script"

        asyncio.run(
            ui_assertions.inspect_ui_page(
                {"entry_url": "https://example.com", "timeout_ms": 1000, "viewport_mode": "h5"},
                {},
                ctx=ctx,  # type: ignore[arg-type]
                setup_func=setup,
            )
        )

        self.assertEqual(ctx.page.setup_marker, "scrolled-by-user-script")
        self.assertEqual(ctx.page.gotos, [])

    def test_ui_scan_does_not_scroll_without_explicit_setup_script(self) -> None:
        ctx = FakeContext("[]")
        ctx.page = InspectPage()

        result = asyncio.run(
            ui_assertions.inspect_ui_page(
                {"entry_url": "https://example.com", "timeout_ms": 1000, "viewport_mode": "h5"},
                {},
                ctx=ctx,  # type: ignore[arg-type]
            )
        )

        self.assertEqual(result["candidates"][0]["name"], "筛选")
        self.assertFalse(any("scrollTo" in script for script in ctx.page.evaluated_scripts))

    def test_ui_scan_prioritizes_pointer_surfaces_and_limits_repeated_low_stability_families(self) -> None:
        async def run_scan() -> list[dict[str, object]]:
            playwright = None
            try:
                from playwright.async_api import async_playwright

                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - depends on the local browser installation
                if playwright is not None:
                    await playwright.stop()
                self.skipTest(f"Chromium unavailable: {exc}")
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.set_content(
                    """
                    <style>
                      .filters { display: flex; }
                      .filter-item, .dropdown-item { cursor: pointer; width: 78px; height: 40px; }
                      .filter-label { cursor: inherit; }
                      ._collect-card_ab12_1 { display: grid; width: 360px; min-height: 100px; }
                      ._card-title_ab12_2, ._year-mileage_ab12_3, ._car-tags_ab12_4, ._price-container_ab12_5 { min-height: 20px; }
                      .tile-section { width: 300px; height: 40px; }
                    </style>
                    <div class="filters">
                      <div class="filter-item"><span class="filter-label">排序</span></div>
                      <div class="filter-item"><span class="filter-label">品牌</span></div>
                      <div class="filter-item"><span class="filter-label">价格</span></div>
                      <div class="filter-item"><span class="filter-label">车龄</span></div>
                      <div class="dropdown-item"><span class="filter-label">筛选</span></div>
                    </div>
                    <div class="_collect-card_ab12_1">
                      <div class="_card-title_ab12_2">车型 A</div>
                      <div class="_year-mileage_ab12_3">2024年 / 1.20万公里 / 北京市</div>
                      <div class="_car-tags_ab12_4">优选车</div>
                      <div class="_price-container_ab12_5">18.8万</div>
                    </div>
                    <div class="_collect-card_ab12_1">
                      <div class="_card-title_ab12_2">车型 B</div>
                      <div class="_year-mileage_ab12_3">2023年 / 2.40万公里 / 上海市</div>
                      <div class="_car-tags_ab12_4">认证好店</div>
                      <div class="_price-container_ab12_5">28.8万</div>
                    </div>
                    <section class="tile-section">车辆 1</section>
                    <section class="tile-section">车辆 2</section>
                    <section class="tile-section">车辆 3</section>
                    <section class="tile-section">车辆 4</section>
                    <section class="tile-section">车辆 5</section>
                    <section class="tile-section">车辆 6</section>
                    """
                )
                result = await page.evaluate(UI_SCAN_SCRIPT)
                return result["candidates"]
            finally:
                await browser.close()
                await playwright.stop()

        candidates = asyncio.run(run_scan())
        filter_texts = {str(item.get("text")) for item in candidates if item.get("kind") == "interactive"}
        repeated_sections = [item for item in candidates if "tile-section" in str(item.get("selector"))]
        structural_labels = {str(item.get("semantic_label")) for item in candidates}
        candidate_names = {str(item.get("name")) for item in candidates}
        card_candidate = next(item for item in candidates if item.get("semantic_label") == "卡片区域")
        title_candidate = next(item for item in candidates if item.get("semantic_label") == "名称区域")
        detail_candidate = next(item for item in candidates if item.get("semantic_label") == "城市 / 里程区域")
        price_candidate = next(
            item
            for item in candidates
            if item.get("semantic_label") == "价格区域" and "price-container" in str(item.get("selector"))
        )

        self.assertTrue({"排序", "筛选"}.issubset(filter_texts))
        self.assertIn("筛选", candidate_names)
        self.assertFalse(any(name.endswith(("_div", "_span", "_button", "_img", "_svg")) for name in candidate_names))
        self.assertLessEqual(len(repeated_sections), 3)
        self.assertTrue({"卡片区域", "名称区域", "城市 / 里程区域", "标签区域", "价格区域"}.issubset(structural_labels))
        self.assertIn('_collect-card_', card_candidate["selector"])
        self.assertEqual(card_candidate["selector_type"], "class")
        self.assertEqual(card_candidate["stability"], "medium")
        self.assertIn('_card-title_', title_candidate["selector"])
        self.assertIn('_year-mileage_', detail_candidate["selector"])
        self.assertIn('_price-container_', price_candidate["selector"])
        self.assertEqual(title_candidate["match_mode"], "every_scope")
        self.assertGreaterEqual(len(title_candidate["selector_options"]), 2)
        self.assertIn("text", {option["selector_type"] for option in title_candidate["selector_options"]})
        self.assertEqual(title_candidate["selector_options"][0]["match_count"], 2)

    def test_ui_scan_validates_duplicate_roles_tail_nodes_shadow_dom_and_iframes(self) -> None:
        async def run_scan() -> list[dict[str, object]]:
            playwright = None
            try:
                from playwright.async_api import async_playwright

                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - depends on the local browser installation
                if playwright is not None:
                    await playwright.stop()
                self.skipTest(f"Chromium unavailable: {exc}")
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                noise = "".join(f'<section class="layout-item">noise {index}</section>' for index in range(700))
                await page.set_content(
                    f"""
                    <style>
                      section {{ width: 120px; height: 20px; }}
                      .vehicle-card__price {{ width: 120px; height: 30px; }}
                    </style>
                    <button>保存</button><button>保存</button>
                    {noise}
                    <div class="vehicle-card__price" data-testid="price\nfield">19.8万</div>
                    <div id="shadow-host"></div>
                    <iframe srcdoc='<style>.city-field{{display:block;width:80px;height:20px}}</style><span class="city-field">北京市</span>'></iframe>
                    <script>
                      const root = document.querySelector('#shadow-host').attachShadow({{mode: 'open'}});
                      root.innerHTML = '<style>.shadow-price{{display:block;width:80px;height:20px}}</style><span class="shadow-price">29.8万</span>';
                    </script>
                    """
                )
                await page.wait_for_timeout(100)
                result = await ui_assertions._collect_ui_scan(page)
                return result["candidates"]
            finally:
                await browser.close()
                await playwright.stop()

        candidates = asyncio.run(run_scan())
        options = [option for item in candidates for option in item.get("selector_options", [])]
        role_option = next(option for option in options if str(option.get("selector", "")).startswith("role=button"))

        self.assertEqual(role_option["match_count"], 2)
        self.assertFalse(role_option["unique"])
        self.assertTrue(any(item.get("text") == "19.8万" for item in candidates))
        self.assertTrue(any("vehicle-card__price" in str(option.get("selector")) for option in options))
        self.assertTrue(any("shadow-price" in str(item.get("selector")) for item in candidates))
        self.assertTrue(any(item.get("frame_path") == [0] and "city-field" in str(item.get("selector")) for item in candidates))
        self.assertFalse(any(int(option.get("match_count") or 0) == 0 for option in options))

    def test_every_scope_requires_each_repeated_region_to_have_visible_content(self) -> None:
        async def run_check() -> tuple[tuple[bool, str], tuple[bool, str], dict[str, object], list[dict[str, object]]]:
            playwright = None
            try:
                from playwright.async_api import async_playwright

                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - depends on the local browser installation
                if playwright is not None:
                    await playwright.stop()
                self.skipTest(f"Chromium unavailable: {exc}")
            try:
                page = await browser.new_page()
                await page.set_content('<style>.card{display:block;width:100px;height:30px}.price{display:block}</style><div class="card"><span class="price">10万</span></div><div class="card"><span class="price"></span></div><img class="vehicle-image" alt="车辆" style="width:80px;height:40px">')
                assertion = {
                    "selector": ".card .price",
                    "scope_selector": ".card",
                    "relative_selector": ".price",
                    "match_mode": "every_scope",
                }
                failed = await ui_assertions._wait_for_selector_collection(page, assertion, "not_empty", 150)
                await page.locator(".card .price").nth(1).evaluate("el => el.remove()")
                inspection = await ui_assertions.inspect_ui_selector_rules(
                    '[{"id":"prices","type":"element_not_empty","selector":".card .price","scope_selector":".card","relative_selector":".price","match_mode":"every_scope","count_mode":"visible"}]',
                    page,
                )
                await page.locator(".card").nth(1).evaluate("el => el.innerHTML = '<span class=price>20万</span>'")
                passed = await ui_assertions._wait_for_selector_collection(page, assertion, "not_empty", 150)
                image_info = await ui_assertions._element_content_info(page.locator(".vehicle-image"))
                return failed, passed, image_info, inspection
            finally:
                await browser.close()
                await playwright.stop()

        failed, passed, image_info, inspection = asyncio.run(run_check())
        self.assertFalse(failed[0])
        self.assertTrue(passed[0])
        self.assertTrue(image_info["hasMedia"])
        self.assertEqual(inspection[0]["status"], "missing")

    def test_exact_element_count_must_remain_stable_before_passing(self) -> None:
        locator = GrowingCountLocator([0, 5, 7, 7, 7, 7])

        actual, passed = asyncio.run(ui_assertions._wait_for_element_count(locator, "eq", 5, 260))

        self.assertEqual(actual, 7)
        self.assertFalse(passed)

    def test_normalize_ui_assertions_requires_selector_for_element_rules(self) -> None:
        with self.assertRaisesRegex(ValueError, "选择器不能为空"):
            ui_assertions.normalize_ui_assertions('[{"type":"element_visible"}]')

    def test_normalize_ui_assertions_preserves_selector_metadata(self) -> None:
        normalized = ui_assertions.normalize_ui_assertions(
            """
            [{
              "type":"element_visible",
              "selector":"#submit",
              "selector_type":"role",
              "selector_stability":"low",
              "selector_score":72.5,
              "scope_selector":".card",
              "relative_selector":".title",
              "match_mode":"every_scope",
              "count_mode":"visible",
              "frame_path":[0]
            }]
            """
        )

        self.assertEqual(normalized[0]["selector_type"], "role")
        self.assertEqual(normalized[0]["selector_stability"], "low")
        self.assertEqual(normalized[0]["selector_score"], 72.5)
        self.assertEqual(normalized[0]["scope_selector"], ".card")
        self.assertEqual(normalized[0]["relative_selector"], ".title")
        self.assertEqual(normalized[0]["match_mode"], "every_scope")
        self.assertEqual(normalized[0]["count_mode"], "visible")
        self.assertEqual(normalized[0]["frame_path"], [0])

    def test_inspect_ui_selector_rules_reports_selector_health(self) -> None:
        page = FakePage()

        results = asyncio.run(
            ui_assertions.inspect_ui_selector_rules(
                """
                [
                  {"id":"ok","type":"element_visible","selector":"#app"},
                  {"id":"missing","type":"element_visible","selector":".missing"},
                  {"id":"multiple","type":"element_not_empty","selector":".many"},
                  {"id":"count","type":"element_count","selector":".many","operator":"gte","expected_count":2},
                  {"id":"invalid","type":"element_visible","selector":".invalid"},
                  {"id":"disabled","type":"element_visible","selector":"#app","enabled":false},
                  {"id":"text","type":"title_contains","expected_text":"PulseGuard"}
                ]
                """,
                page,
            )
        )

        statuses = {item["id"]: item["status"] for item in results}
        self.assertEqual(statuses["ok"], "ok")
        self.assertEqual(statuses["missing"], "missing")
        self.assertEqual(statuses["multiple"], "multiple")
        self.assertEqual(statuses["count"], "ok")
        self.assertEqual(statuses["invalid"], "invalid_selector")
        self.assertEqual(statuses["disabled"], "disabled")
        self.assertNotIn("text", statuses)

    def test_structured_ui_check_records_results_and_passes(self) -> None:
        ctx = FakeContext(
            """
            [
              {"type":"element_visible","selector":"#app"},
              {"type":"element_not_empty","selector":".card"},
              {"type":"page_not_blank"},
              {"type":"text_present","expected_text":"首页"},
              {"type":"title_contains","expected_text":"PulseGuard"},
              {"type":"url_contains","expected_text":"/dashboard"},
              {"type":"element_count","selector":".items","operator":"gte","expected_count":2},
              {"type":"console_error_absent"}
            ]
            """
        )

        asyncio.run(ui_assertions.run_structured_ui_check(ctx))  # type: ignore[arg-type]

        self.assertIsNotNone(ctx.response_snapshot)
        self.assertEqual(len(ctx.response_snapshot["assertions"]), 8)
        self.assertTrue(all(item["status"] == "ok" for item in ctx.response_snapshot["assertions"]))

    def test_structured_ui_check_fails_with_actionable_result(self) -> None:
        ctx = FakeContext('[{"type":"text_absent","expected_text":"首页"}]')

        with self.assertRaisesRegex(RunFailure, "UI 结构化校验失败"):
            asyncio.run(ui_assertions.run_structured_ui_check(ctx))  # type: ignore[arg-type]

        self.assertEqual(ctx.response_snapshot["assertions"][0]["status"], "failed")

    def test_structured_ui_check_uses_configured_timeout_for_assertion_wait(self) -> None:
        ctx = FakeContext('[{"type":"element_visible","selector":"#slow"}]')
        ctx.timeout_ms = 5000
        ctx.page = TimeoutPage()

        with self.assertRaisesRegex(RunFailure, "UI 结构化校验失败"):
            asyncio.run(ui_assertions.run_structured_ui_check(ctx))  # type: ignore[arg-type]

        self.assertGreater(ctx.page.timeout_locator.timeouts[0], 1500)
        message = ctx.response_snapshot["assertions"][0]["message"]
        self.assertNotIn("1500ms", message)
        self.assertIn("变为可见", message)

    def test_structured_ui_check_runs_assertions_concurrently(self) -> None:
        ctx = FakeContext(
            """
            [
              {"type":"element_visible","selector":"#first"},
              {"type":"element_visible","selector":"#second"}
            ]
            """
        )
        ctx.page = ConcurrentPage()

        asyncio.run(ui_assertions.run_structured_ui_check(ctx))  # type: ignore[arg-type]

        self.assertGreaterEqual(ctx.page.max_active_waits, 2)

    def test_element_count_waits_for_async_content(self) -> None:
        ctx = FakeContext('[{"type":"element_count","selector":".cars","operator":"gte","expected_count":5}]')
        ctx.page = GrowingCountPage()

        asyncio.run(ui_assertions.run_structured_ui_check(ctx))  # type: ignore[arg-type]

        result = ctx.response_snapshot["assertions"][0]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["actual"], 5)

    def test_setup_script_runs_on_same_page_before_assertions(self) -> None:
        ctx = FakeContext('[{"type":"title_contains","expected_text":"${EXPECTED_TITLE}"}]')
        seen: dict[str, object] = {}

        async def setup(setup_ctx: FakeContext, page: FakePage) -> None:
            seen["ctx"] = setup_ctx
            seen["page"] = page
            await page.goto("https://example.com/login", wait_until="domcontentloaded", timeout=setup_ctx.timeout_ms)
            page.setup_marker = "prepared"
            page.url = "https://example.com/session-ready"

        asyncio.run(ui_assertions.run_structured_ui_check(ctx, setup_func=setup))  # type: ignore[arg-type]

        self.assertIs(seen["ctx"], ctx)
        self.assertIs(seen["page"], ctx.page)
        self.assertEqual(ctx.page.setup_marker, "prepared")
        self.assertEqual(ctx.page.gotos, ["https://example.com/login", "https://example.com"])
        self.assertEqual(ctx.response_snapshot["page"]["url"], "https://example.com/dashboard")


if __name__ == "__main__":
    unittest.main()
