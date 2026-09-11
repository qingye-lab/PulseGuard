from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from urllib.parse import urldefrag

from .context import RunContext, RunFailure, PreparationFailure
from .ui_scan_script import UI_SCAN_SCRIPT
from .variables import mask_data, mask_text, resolve_data, resolve_text, resolve_url
from .viewport import browser_context_options, viewport_for_mode


UI_ASSERTION_TYPES = {
    "element_visible",
    "element_hidden",
    "element_not_empty",
    "page_not_blank",
    "text_present",
    "text_absent",
    "title_contains",
    "url_contains",
    "console_error_absent",
    "element_count",
}

SELECTOR_ASSERTION_TYPES = {"element_visible", "element_hidden", "element_not_empty", "element_count"}
TEXT_ASSERTION_TYPES = {"text_present", "text_absent", "title_contains", "url_contains"}
COUNT_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte"}
SELECTOR_STABILITY_LEVELS = {"high", "medium", "low"}
SELECTOR_MATCH_MODES = {"first", "all", "every_scope"}
SELECTOR_COUNT_MODES = {"attached", "visible"}


async def inspect_ui_page(payload: dict[str, Any], settings: dict[str, Any], ctx: RunContext | None = None, setup_func: Any | None = None) -> dict[str, Any]:
    entry_url = str(payload.get("entry_url") or "").strip()
    timeout_ms = int(payload.get("timeout_ms") or 15000)
    if not entry_url:
        raise ValueError("页面 URL 不能为空")
    viewport = viewport_for_mode(payload.get("viewport_mode"), settings, payload.get("viewport_width"), payload.get("viewport_height"))

    if ctx is not None:
        page = await prepare_ui_target(ctx, setup_func)
        settings = ctx.settings
        ctx.ui_stage = "scan"
        await _wait_for_initial_ui(page)

        candidates = await _run_phase_with_timeout(_collect_ui_scan(page), timeout_ms, "页面扫描执行超时")
        screenshot = await page.screenshot(type="jpeg", quality=72, full_page=True)
        return {
            "title": await page.title(),
            "url": mask_text(page.url, settings),
            "viewport": candidates.get("viewport") or viewport,
            "page_size": candidates.get("page_size") or candidates.get("viewport") or viewport,
            "candidates": mask_data(candidates.get("candidates") or [], settings),
            "screenshot": f"data:image/jpeg;base64,{base64.b64encode(screenshot).decode('ascii')}",
            "logs": mask_text("\n".join(ctx.logs), settings),
        }

    entry_url = resolve_url(entry_url, settings)
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RunFailure("Playwright 未安装，请先安装后再扫描页面") from exc

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser_type_name = str(settings.get("browser_type") or "chromium")
        browser_type = getattr(playwright, browser_type_name, None)
        if browser_type is None:
            raise ValueError(f"不支持的浏览器类型：{browser_type_name}")

        launch_options: dict[str, Any] = {"headless": bool(settings.get("browser_headless", True))}
        proxy_url = resolve_text(settings.get("browser_proxy") or "", settings).strip()
        if proxy_url:
            launch_options["proxy"] = {"server": proxy_url}

        browser = await browser_type.launch(**launch_options)
        context_options = browser_context_options(payload.get("viewport_mode"), viewport)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        if setup_func is not None:
            if ctx is None:
                raise RunFailure("扫描前置脚本缺少运行上下文")
            ctx.log("执行 UI 扫描前置脚本")
            await _run_phase_with_timeout(setup_func(ctx, page), timeout_ms, "扫描前置脚本执行超时")
            if _page_is_closed(page):
                raise RunFailure("前置脚本关闭了扫描页面")
            ctx.log("UI 扫描前置脚本完成")

        await _goto_ui_page(page, entry_url, timeout_ms, "页面扫描加载超时")
        await _wait_for_initial_ui(page)

        candidates = await _run_phase_with_timeout(_collect_ui_scan(page), timeout_ms, "页面扫描执行超时")
        screenshot = await page.screenshot(type="jpeg", quality=72, full_page=True)
        return {
            "title": await page.title(),
            "url": mask_text(page.url, settings),
            "viewport": candidates.get("viewport") or viewport,
            "page_size": candidates.get("page_size") or candidates.get("viewport") or viewport,
            "candidates": mask_data(candidates.get("candidates") or [], settings),
            "screenshot": f"data:image/jpeg;base64,{base64.b64encode(screenshot).decode('ascii')}",
            "logs": mask_text("\n".join(ctx.logs), settings) if ctx else "",
        }
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


def _selector_stability(score: int | float) -> str:
    if score >= 80:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


async def _validated_selector_options(frame: Any, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for raw_option in candidate.get("selector_options") or []:
        if not isinstance(raw_option, dict):
            continue
        option = dict(raw_option)
        selector = str(option.get("selector") or "").strip()
        if not selector:
            continue
        try:
            count = await _visible_locator_count(frame.locator(selector))
        except Exception:
            continue
        if count <= 0:
            continue

        was_unique = bool(option.get("unique"))
        is_unique = count == 1
        try:
            score = int(float(option.get("score") or 1))
        except (TypeError, ValueError):
            score = 1
        if is_unique and not was_unique:
            score += 14
        elif was_unique and not is_unique:
            score -= 14
        score = max(1, min(100, score))

        option["match_count"] = count
        option["unique"] = is_unique
        option["score"] = score
        option["stability"] = _selector_stability(score)
        option["count_mode"] = "visible"
        if option.get("match_mode") != "every_scope":
            option["match_mode"] = "first" if is_unique else "all"
        validated.append(option)
    return validated


async def _scan_frame(frame: Any, frame_path: list[int], offset_x: float = 0, offset_y: float = 0) -> list[dict[str, Any]]:
    try:
        scan = await frame.evaluate(UI_SCAN_SCRIPT)
    except Exception:
        return []

    output: list[dict[str, Any]] = []
    for raw_candidate in scan.get("candidates") or []:
        if not isinstance(raw_candidate, dict):
            continue
        candidate = dict(raw_candidate)
        options = await _validated_selector_options(frame, candidate)
        if not options:
            continue
        best = options[0]
        candidate.update(
            {
                "selector": best["selector"],
                "selector_type": best.get("selector_type"),
                "stability": best.get("stability"),
                "score": best.get("score"),
                "selector_options": options,
                "scope_selector": best.get("scope_selector"),
                "relative_selector": best.get("relative_selector"),
                "match_mode": best.get("match_mode"),
                "count_mode": best.get("count_mode"),
                "family_id": best.get("family_id") or candidate.get("family_id"),
                "frame_path": list(frame_path),
            }
        )
        box = dict(candidate.get("box") or {})
        box["x"] = max(0, float(box.get("x") or 0) + offset_x)
        box["y"] = max(0, float(box.get("y") or 0) + offset_y)
        candidate["box"] = box
        for option in options:
            option["frame_path"] = list(frame_path)
        output.append(candidate)

    for index, child in enumerate(list(getattr(frame, "child_frames", []) or [])):
        child_offset_x = offset_x
        child_offset_y = offset_y
        try:
            frame_element = await child.frame_element()
            frame_box = await frame_element.bounding_box()
            if frame_box:
                # Playwright reports iframe boxes in the main-frame viewport, so do not
                # add the parent offset a second time for nested frames.
                child_offset_x = float(frame_box.get("x") or 0)
                child_offset_y = float(frame_box.get("y") or 0)
        except Exception:
            pass
        output.extend(await _scan_frame(child, [*frame_path, index], child_offset_x, child_offset_y))
    return output


async def _collect_ui_scan(page: Any) -> dict[str, Any]:
    main_frame = getattr(page, "main_frame", None)
    if main_frame is None:
        return await page.evaluate(UI_SCAN_SCRIPT)

    candidates = await _scan_frame(main_frame, [])
    candidates.sort(key=lambda item: float(item.get("_rank_score") or item.get("score") or 0), reverse=True)
    name_counts: dict[str, int] = {}
    for index, candidate in enumerate(candidates[:240], start=1):
        candidate.pop("_rank_score", None)
        name = str(candidate.get("name") or "element")
        name_counts[name] = name_counts.get(name, 0) + 1
        candidate["candidate_id"] = f"candidate-{index}"
        if name_counts[name] > 1:
            candidate["name"] = f"{name}_{name_counts[name]}"

    try:
        viewport = await main_frame.evaluate("() => ({ width: window.innerWidth, height: window.innerHeight })")
        page_size = await main_frame.evaluate(
            "() => ({ width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0, window.innerWidth), "
            "height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0, window.innerHeight) })"
        )
    except Exception:
        viewport = {}
        page_size = {}
    return {"viewport": viewport, "page_size": page_size or viewport, "candidates": candidates[:240]}


def normalize_ui_assertions(raw: str | None) -> list[dict[str, Any]]:
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("UI 校验项必须是合法 JSON") from exc
    if not isinstance(items, list):
        raise ValueError("UI 校验项必须是数组")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个 UI 校验项格式不正确")
        assertion_type = str(item.get("type") or "").strip()
        if assertion_type not in UI_ASSERTION_TYPES:
            raise ValueError(f"不支持的 UI 校验类型：{assertion_type or '空'}")

        assertion: dict[str, Any] = {
            "id": str(item.get("id") or f"ui-assertion-{index}"),
            "type": assertion_type,
            "enabled": bool(item.get("enabled", True)),
        }
        if assertion_type in SELECTOR_ASSERTION_TYPES:
            assertion["selector"] = _required_string(item.get("selector"), "选择器")
            _copy_selector_metadata(assertion, item)
        if assertion_type in TEXT_ASSERTION_TYPES:
            assertion["expected_text"] = _required_string(item.get("expected_text"), "预期文本")
            if assertion_type in {"text_present", "text_absent"} and str(item.get("selector") or "").strip():
                assertion["selector"] = _required_string(item.get("selector"), "选择器")
                _copy_selector_metadata(assertion, item)
        if assertion_type == "element_count":
            assertion["operator"] = _enum(item.get("operator", "eq"), COUNT_OPERATORS, "数量关系")
            assertion["expected_count"] = _bounded_int(item.get("expected_count", 1), 0, 100000, "预期数量")
        normalized.append(assertion)
    return normalized


def has_enabled_ui_assertions(raw: str | None) -> bool:
    return any(assertion.get("enabled", True) for assertion in normalize_ui_assertions(raw))


async def inspect_ui_selector_rules(raw: str | None, page: Any, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    resolved_settings = settings or {}
    assertions = normalize_ui_assertions(raw)
    results: list[dict[str, Any]] = []
    for assertion in assertions:
        assertion_type = assertion["type"]
        if assertion_type not in SELECTOR_ASSERTION_TYPES and not assertion.get("selector"):
            continue
        result = {
            "id": assertion["id"],
            "type": assertion_type,
            "selector": assertion["selector"],
            "status": "ok",
            "count": None,
            "message": "",
        }
        if not assertion.get("enabled", True):
            result.update({"status": "disabled", "message": "规则已停用，未检测"})
            results.append(result)
            continue
        assertion = resolve_data(assertion, resolved_settings)
        result["selector"] = assertion["selector"]
        try:
            locator = await _locator_for_assertion(page, assertion)
            count = await _visible_locator_count(locator) if assertion.get("count_mode") == "visible" else int(await locator.count())
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            lowered = message.lower()
            status = "invalid_selector" if "selector" in lowered or "parse" in lowered or "unexpected" in lowered else "error"
            result.update({"status": status, "message": f"selector 无法解析：{message}" if status == "invalid_selector" else f"检测失败：{message}"})
            results.append(result)
            continue

        result["count"] = count
        if count == 0:
            result.update({"status": "missing", "message": "selector 当前未匹配到元素"})
        elif assertion_type != "element_count" and assertion.get("match_mode") == "every_scope":
            missing_scopes, inspected_scopes = await _inspect_every_scope_matches(page, assertion)
            if missing_scopes:
                result.update(
                    {
                        "status": "missing",
                        "message": f"{inspected_scopes} 个作用域中有 {missing_scopes} 个未匹配到元素",
                    }
                )
            else:
                result["message"] = f"{inspected_scopes} 个作用域均匹配正常"
        elif assertion_type != "element_count" and assertion.get("match_mode", "first") == "first" and count > 1:
            result.update({"status": "multiple", "message": f"selector 当前匹配 {count} 个元素"})
        else:
            result["message"] = "selector 当前匹配正常"
        results.append(result)
    return results


async def inspect_ui_rule_selectors(payload: dict[str, Any], settings: dict[str, Any], ctx: RunContext | None = None, setup_func: Any | None = None) -> dict[str, Any]:
    entry_url = str(payload.get("entry_url") or "").strip()
    timeout_ms = int(payload.get("timeout_ms") or 15000)
    if not entry_url:
        raise ValueError("页面 URL 不能为空")
    viewport = viewport_for_mode(payload.get("viewport_mode"), settings, payload.get("viewport_width"), payload.get("viewport_height"))

    if ctx is not None:
        page = await prepare_ui_target(ctx, setup_func)
        settings = ctx.settings
        ctx.ui_stage = "selectors"
        await _wait_for_initial_ui(page)
        results = await inspect_ui_selector_rules(payload.get("assertions_json"), page, settings)
        return {
            "title": await page.title(),
            "url": mask_text(page.url, settings),
            "results": mask_data(results, settings),
            "logs": mask_text("\n".join(ctx.logs), settings),
        }

    entry_url = resolve_url(entry_url, settings)
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RunFailure("Playwright 未安装，请先安装后再检测规则") from exc

    playwright = await async_playwright().start()
    browser = None
    context = None
    try:
        browser_type_name = str(settings.get("browser_type") or "chromium")
        browser_type = getattr(playwright, browser_type_name, None)
        if browser_type is None:
            raise ValueError(f"不支持的浏览器类型：{browser_type_name}")

        launch_options: dict[str, Any] = {"headless": bool(settings.get("browser_headless", True))}
        proxy_url = resolve_text(settings.get("browser_proxy") or "", settings).strip()
        if proxy_url:
            launch_options["proxy"] = {"server": proxy_url}

        browser = await browser_type.launch(**launch_options)
        context_options = browser_context_options(payload.get("viewport_mode"), viewport)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        if setup_func is not None:
            if ctx is None:
                raise RunFailure("规则检测前置脚本缺少运行上下文")
            ctx.log("执行 UI 规则检测前置脚本")
            await _run_phase_with_timeout(setup_func(ctx, page), timeout_ms, "规则检测前置脚本执行超时")
            if _page_is_closed(page):
                raise RunFailure("前置脚本关闭了规则检测页面")
            ctx.log("UI 规则检测前置脚本完成")

        await _goto_ui_page(page, entry_url, timeout_ms, "规则检测页面加载超时")
        await _wait_for_initial_ui(page)
        results = await inspect_ui_selector_rules(payload.get("assertions_json"), page, settings)
        return {
            "title": await page.title(),
            "url": mask_text(page.url, settings),
            "results": mask_data(results, settings),
            "logs": mask_text("\n".join(ctx.logs), settings) if ctx else "",
        }
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await playwright.stop()


async def prepare_ui_prerequisites(ctx: RunContext, setup_func: Any | None = None) -> Any:
    """Complete prerequisites before starting the target clock."""
    ctx.ui_stage = "browser"
    page = await ctx.new_page()
    started = time.perf_counter()
    template = ctx.check.get("entry_url") or ""
    preparation: dict[str, Any] = {"status": "running"}
    ctx.request_snapshot = {
        "type": "ui", "url_template": template, "url": template,
        "timeout_ms": ctx.timeout_ms, "viewport_mode": ctx.viewport_mode,
        "preparation": preparation,
    }
    target_snapshot = ctx.request_snapshot
    try:
        ctx.ui_stage = "setup"
        if setup_func is not None:
            ctx.log("执行 UI 前置脚本")
            await _run_phase_with_timeout(setup_func(ctx, page), ctx.timeout_ms, "前置脚本执行超时")
            if _page_is_closed(page):
                raise RunFailure("前置脚本关闭了校验页面")
            ctx.log("UI 前置脚本完成")
        ctx.ui_stage = "resolve_url"
        target_url = ctx.entry_url.strip()
        if not target_url:
            raise RunFailure("页面 URL 不能为空")
        target_snapshot["url"] = target_url
        preparation["status"] = "ok"
    except Exception as exc:
        preparation["status"] = "failed"
        preparation["failed_stage"] = ctx.ui_stage
        raise PreparationFailure(str(exc) or exc.__class__.__name__) from exc
    finally:
        preparation["duration_ms"] = max(0, int((time.perf_counter() - started) * 1000))
        preparation["variables"] = ctx.vars.snapshot()
        if ctx.request_snapshot is not target_snapshot:
            preparation["last_request"] = ctx.request_snapshot
            preparation["last_response"] = ctx.response_snapshot
        ctx.request_snapshot = mask_data(target_snapshot, ctx.settings)

    return page


async def navigate_ui_target(ctx: RunContext, page: Any) -> Any:
    # Evidence is masked; navigation must use the actual runtime variables.
    target_url = ctx.entry_url.strip()
    ctx.ui_stage = "navigate"
    page_load_started = time.perf_counter()
    try:
        await _goto_ui_page(page, target_url, ctx.timeout_ms, "页面加载超时")
    except BaseException:
        ctx.request_snapshot["navigation"] = {"status": "failed"}
        raise
    ctx.request_snapshot["navigation"] = {"status": "ok"}
    ctx.request_snapshot["page_load_ms"] = max(0, int((time.perf_counter() - page_load_started) * 1000))
    ctx.log(f"目标页面：{mask_text(target_url, ctx.settings)}")
    return page


async def prepare_ui_target(ctx: RunContext, setup_func: Any | None = None) -> Any:
    page = await prepare_ui_prerequisites(ctx, setup_func)
    return await navigate_ui_target(ctx, page)


async def run_structured_ui_check(ctx: RunContext, setup_func: Any | None = None, *, page: Any | None = None) -> None:
    assertions = [item for item in normalize_ui_assertions(ctx.check.get("assertions_json")) if item.get("enabled", True)]
    if not assertions:
        raise RunFailure("请至少启用一个 UI 校验项")
    if page is None:
        page = await prepare_ui_target(ctx, setup_func)
    ctx.ui_stage = "assertions"
    assertions = [resolve_data(item, ctx.settings) for item in assertions]
    page_load_ms = ctx.request_snapshot["page_load_ms"]
    ctx.log(f"页面加载完成：{page_load_ms} ms")

    assertion_deadline = _deadline_from_now(ctx.timeout_ms)
    assertion_started = time.perf_counter()
    ctx.log(f"并发执行 UI 校验：{len(assertions)} 项，单项最长等待 {ctx.timeout_ms}ms")
    assertion_results = list(
        await asyncio.gather(
            *(_evaluate_assertion(assertion, page, ctx, assertion_deadline) for assertion in assertions)
        )
    )
    assertion_ms = max(0, int((time.perf_counter() - assertion_started) * 1000))

    page_title = await page.title()
    page_url = page.url
    ctx.response_snapshot = {
        "page": {
            "title": page_title,
            "url": page_url,
            "wait_until": "domcontentloaded",
            "viewport_mode": ctx.viewport_mode,
            "load_ms": page_load_ms,
        },
        "timings": {"page_load_ms": page_load_ms, "assertions_ms": assertion_ms, "assertion_timeout_ms": ctx.timeout_ms},
        "assertions": assertion_results,
    }

    failed = [item for item in assertion_results if item.get("status") != "ok"]
    if failed:
        raise RunFailure(f"UI 结构化校验失败：{failed[0].get('message') or failed[0].get('rule')}")


async def _evaluate_assertion(assertion: dict[str, Any], page: Any, ctx: RunContext, deadline: float | None = None) -> dict[str, Any]:
    assertion_type = assertion["type"]
    label = assertion_label(assertion)
    wait_ms = _assertion_wait_ms(ctx, deadline)

    try:
        if assertion_type == "element_visible":
            selector = assertion["selector"]
            passed, actual = await _wait_for_selector_collection(page, assertion, "visible", wait_ms)
            return _result(
                label,
                selector,
                "ok" if passed else "failed",
                actual,
                "",
                "可见",
                "元素可见" if passed else f"元素未在 {wait_ms}ms 内变为可见或匹配范围未全部满足：{selector}",
            )

        if assertion_type == "element_hidden":
            selector = assertion["selector"]
            passed, actual = await _wait_for_selector_collection(page, assertion, "hidden", wait_ms)
            return _result(
                label,
                selector,
                "ok" if passed else "failed",
                actual,
                "",
                "隐藏或不存在",
                "元素隐藏或不存在" if passed else f"元素未在 {wait_ms}ms 内隐藏：{selector}",
            )

        if assertion_type == "element_not_empty":
            selector = assertion["selector"]
            passed, actual = await _wait_for_selector_collection(page, assertion, "not_empty", wait_ms)
            return _result(
                label,
                selector,
                "ok" if passed else "failed",
                actual,
                "不为空",
                "有内容",
                "组件不为空" if passed else f"校验失败：匹配范围内存在空组件或没有可见内容：{selector}",
            )

        if assertion_type == "page_not_blank":
            info = await page.evaluate(
                """
                () => {
                  const visible = (node) => {
                    const style = getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    return style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0 && rect.width > 1 && rect.height > 1;
                  };
                  const nodes = Array.from(document.body?.querySelectorAll("*") || []);
                  const visibleNodes = nodes.filter(visible);
                  const text = visibleNodes.map((node) => node.innerText || node.textContent || "").join(" ").trim().replace(/\\s+/g, " ");
                  const mediaCount = visibleNodes.filter((node) => node.matches("img,svg,canvas,video,picture,iframe")).length;
                  const interactiveCount = visibleNodes.filter((node) => node.matches("button,a,input,textarea,select,[role]")).length;
                  return {
                    textLength: text.length,
                    preview: text.slice(0, 120),
                    mediaCount,
                    interactiveCount,
                    visibleElementCount: visibleNodes.length
                  };
                }
                """
            )
            passed = bool(info.get("textLength") or info.get("mediaCount") or info.get("interactiveCount") or info.get("visibleElementCount"))
            actual = (
                f"文本 {info.get('textLength') or 0} 字，媒体 {info.get('mediaCount') or 0} 个，"
                f"交互 {info.get('interactiveCount') or 0} 个，可见节点 {info.get('visibleElementCount') or 0} 个"
            )
            return _result(label, "page", "ok" if passed else "failed", actual, "不为空", "页面有内容", "页面不空白" if passed else "页面为空白或无可见内容")

        if assertion_type == "text_present":
            text = assertion["expected_text"]
            if assertion.get("selector"):
                locator = await _locator_for_assertion(page, assertion)
                try:
                    await locator.filter(has_text=text).first.wait_for(state="visible", timeout=wait_ms)
                except Exception as exc:
                    if _is_playwright_timeout(exc):
                        return _result(label, assertion["selector"], "failed", "限定区域内未找到", "包含", text, f"校验失败：限定区域内文本未出现：{text}")
                    raise
                return _result(label, assertion["selector"], "ok", "限定区域内出现", "包含", text, "限定区域文本出现")
            try:
                await page.get_by_text(text).first.wait_for(state="visible", timeout=wait_ms)
            except Exception as exc:
                if _is_playwright_timeout(exc):
                    count = await page.get_by_text(text).count()
                    actual = "未找到" if count == 0 else f"匹配 {count} 个，未可见"
                    return _result(label, text, "failed", actual, "包含", text, f"校验失败：文本未出现：{text}")
                raise
            return _result(label, text, "ok", "出现", "包含", text, "文本出现")

        if assertion_type == "text_absent":
            text = assertion["expected_text"]
            if assertion.get("selector"):
                locator = await _locator_for_assertion(page, assertion)
                matching = locator.filter(has_text=text)
                try:
                    await matching.first.wait_for(state="hidden", timeout=wait_ms)
                except Exception as exc:
                    if _is_playwright_timeout(exc):
                        return _result(label, assertion["selector"], "failed", "限定区域内仍可见", "不包含", text, f"校验失败：限定区域内文本仍然出现：{text}")
                    raise
                return _result(label, assertion["selector"], "ok", "限定区域内未出现", "不包含", text, "限定区域文本未出现")
            try:
                await page.get_by_text(text).first.wait_for(state="hidden", timeout=wait_ms)
            except Exception as exc:
                if _is_playwright_timeout(exc):
                    return _result(label, text, "failed", "仍可见", "不包含", text, f"校验失败：文本仍然出现：{text}")
                raise
            return _result(label, text, "ok", "未出现", "不包含", text, "文本未出现")

        if assertion_type == "title_contains":
            title = await page.title()
            expected = assertion["expected_text"]
            passed = expected in title
            return _result(label, "title", "ok" if passed else "failed", title, "包含", expected, "标题包含预期文本" if passed else "标题不包含预期文本")

        if assertion_type == "url_contains":
            current_url = page.url
            expected = assertion["expected_text"]
            passed = expected in current_url
            return _result(label, "url", "ok" if passed else "failed", current_url, "包含", expected, "URL 包含预期文本" if passed else "URL 不包含预期文本")

        if assertion_type == "console_error_absent":
            errors = [item for item in ctx.console_messages if item.get("type") == "error"]
            passed = not errors
            actual = "无错误" if passed else "；".join(str(item.get("text") or "") for item in errors[:5])
            return _result(label, "console", "ok" if passed else "failed", actual, "不存在", "console error", "控制台无错误" if passed else "控制台存在错误")

        if assertion_type == "element_count":
            selector = assertion["selector"]
            expected_count = int(assertion["expected_count"])
            operator = assertion["operator"]
            locator = await _locator_for_assertion(page, assertion)
            actual_count, passed = await _wait_for_element_count(
                locator,
                operator,
                expected_count,
                wait_ms,
                visible_only=assertion.get("count_mode") == "visible",
            )
            return _result(
                label,
                selector,
                "ok" if passed else "failed",
                actual_count,
                _operator_label(operator),
                expected_count,
                "元素数量满足预期" if passed else f"元素数量不匹配：实际 {actual_count}，期望 {_operator_label(operator)} {expected_count}",
            )
    except Exception as exc:
        return _result(label, assertion.get("selector") or assertion.get("expected_text") or assertion_type, "failed", "执行失败", "", "", f"校验失败：{str(exc) or exc.__class__.__name__}")

    return _result(label, assertion_type, "failed", "-", "", "", "不支持的 UI 校验项")


def assertion_label(assertion: dict[str, Any]) -> str:
    assertion_type = assertion["type"]
    if assertion_type == "element_visible":
        return "元素可见"
    if assertion_type == "element_hidden":
        return "元素隐藏"
    if assertion_type == "element_not_empty":
        return "组件不为空"
    if assertion_type == "page_not_blank":
        return "页面不空白"
    if assertion_type == "text_present":
        return "文本出现"
    if assertion_type == "text_absent":
        return "文本不存在"
    if assertion_type == "title_contains":
        return "标题包含"
    if assertion_type == "url_contains":
        return "URL 包含"
    if assertion_type == "console_error_absent":
        return "控制台无错误"
    if assertion_type == "element_count":
        return "元素数量"
    return assertion_type


def _result(rule: str, path: str, status: str, actual: Any, operator: str, expected: Any, message: str) -> dict[str, Any]:
    return {
        "rule": rule,
        "path": path,
        "status": status,
        "actual": actual,
        "operator": operator,
        "expected": expected,
        "message": message,
    }


def _required_string(value: Any, label: str) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    text = value.strip()
    if len(text) > 1000:
        raise ValueError(f"{label}过长")
    return text


def _copy_selector_metadata(assertion: dict[str, Any], item: dict[str, Any]) -> None:
    selector_type = str(item.get("selector_type") or "").strip()
    selector_stability = str(item.get("selector_stability") or "").strip()
    selector_score = item.get("selector_score")
    if selector_type:
        assertion["selector_type"] = selector_type[:80]
    if selector_stability in SELECTOR_STABILITY_LEVELS:
        assertion["selector_stability"] = selector_stability
    if isinstance(selector_score, bool):
        score = None
    else:
        try:
            score = float(selector_score)
        except (TypeError, ValueError):
            score = None
    if score is not None:
        assertion["selector_score"] = int(score) if score.is_integer() else score

    scope_selector = str(item.get("scope_selector") or "").strip()
    relative_selector = str(item.get("relative_selector") or "").strip()
    match_mode = str(item.get("match_mode") or "").strip()
    count_mode = str(item.get("count_mode") or "").strip()
    frame_path = item.get("frame_path")
    if scope_selector:
        assertion["scope_selector"] = scope_selector[:1000]
    if relative_selector:
        assertion["relative_selector"] = relative_selector[:1000]
    if match_mode in SELECTOR_MATCH_MODES:
        assertion["match_mode"] = match_mode
    if count_mode in SELECTOR_COUNT_MODES:
        assertion["count_mode"] = count_mode
    if isinstance(frame_path, list) and len(frame_path) <= 8 and all(isinstance(index, int) and not isinstance(index, bool) and index >= 0 for index in frame_path):
        assertion["frame_path"] = frame_path


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{label}必须在 {minimum} 到 {maximum} 之间")
    return number


def _enum(value: Any, allowed: set[str], label: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"{label}不支持：{text}")
    return text


def _compare_count(actual: int, operator: str, expected: int) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "ne":
        return actual != expected
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    if operator == "lt":
        return actual < expected
    if operator == "lte":
        return actual <= expected
    return False


async def _frame_for_assertion(page: Any, assertion: dict[str, Any]) -> Any:
    frame = page
    for index in assertion.get("frame_path") or []:
        children = list(getattr(frame, "child_frames", []) or [])
        if index < 0 or index >= len(children):
            raise ValueError(f"iframe 路径已失效：{assertion.get('frame_path')}")
        frame = children[index]
    return frame


async def _locator_for_assertion(page: Any, assertion: dict[str, Any]) -> Any:
    frame = await _frame_for_assertion(page, assertion)
    return frame.locator(assertion["selector"])


async def _inspect_every_scope_matches(page: Any, assertion: dict[str, Any]) -> tuple[int, int]:
    scope_selector = assertion.get("scope_selector")
    relative_selector = assertion.get("relative_selector")
    if not scope_selector or not relative_selector:
        return 0, 0

    frame = await _frame_for_assertion(page, assertion)
    scopes = frame.locator(scope_selector)
    missing_scopes = 0
    inspected_scopes = 0
    for index in range(int(await scopes.count())):
        scope = scopes.nth(index)
        if assertion.get("count_mode") == "visible" and not await scope.is_visible():
            continue
        inspected_scopes += 1
        matches = scope.locator(relative_selector)
        match_count = await _visible_locator_count(matches) if assertion.get("count_mode") == "visible" else int(await matches.count())
        if match_count == 0:
            missing_scopes += 1
    return missing_scopes, inspected_scopes


async def _visible_locator_count(locator: Any) -> int:
    return int(
        await locator.evaluate_all(
            r"""
            (elements) => elements.filter((el) => {
              if (!(el instanceof Element)) return false;
              let current = el;
              while (current) {
                const style = getComputedStyle(current);
                if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
                current = current.parentElement || current.getRootNode?.().host || null;
              }
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            }).length
            """
        )
    )


async def _element_content_info(locator: Any) -> dict[str, Any]:
    return await locator.evaluate(
        r"""
        (el) => {
          const visible = (node) => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            let parent = node.parentElement || node.getRootNode?.().host || null;
            let ancestorVisible = true;
            while (parent && ancestorVisible) {
              const parentStyle = getComputedStyle(parent);
              ancestorVisible = parentStyle.visibility !== "hidden" && parentStyle.display !== "none" && Number(parentStyle.opacity) !== 0;
              parent = parent.parentElement || parent.getRootNode?.().host || null;
            }
            return ancestorVisible && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
          };
          const mediaSelector = "img,svg,canvas,video,picture,iframe,input,textarea,select";
          const hasMedia = (el.matches(mediaSelector) && visible(el)) || Array.from(el.querySelectorAll(mediaSelector)).some(visible);
          const text = (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
          const visibleChildCount = Array.from(el.children || []).filter(visible).length;
          const rect = el.getBoundingClientRect();
          return { text, hasMedia, visibleChildCount, width: rect.width, height: rect.height, visible: visible(el) };
        }
        """
    )


async def _locator_satisfies(locator: Any, state: str) -> bool:
    if state == "visible":
        return bool(await locator.is_visible())
    if state == "hidden":
        return not bool(await locator.is_visible())
    info = await _element_content_info(locator)
    return bool(info.get("visible") and (info.get("text") or info.get("hasMedia") or info.get("visibleChildCount")))


async def _wait_for_selector_collection(page: Any, assertion: dict[str, Any], state: str, wait_ms: int) -> tuple[bool, str]:
    frame = await _frame_for_assertion(page, assertion)
    mode = assertion.get("match_mode") or "first"
    if mode == "first":
        locator = frame.locator(assertion["selector"]).first
        try:
            await locator.wait_for(state="hidden" if state == "hidden" else "visible", timeout=wait_ms)
        except Exception as exc:
            if _is_playwright_timeout(exc):
                count = int(await frame.locator(assertion["selector"]).count())
                return False, "未找到" if count == 0 else f"匹配 {count} 个，状态不满足"
            raise
        if state == "not_empty":
            info = await _element_content_info(locator)
            passed = bool(info.get("visible") and (info.get("text") or info.get("hasMedia") or info.get("visibleChildCount")))
            actual = info.get("text") or ("包含媒体/表单" if info.get("hasMedia") else f"{info.get('visibleChildCount') or 0} 个可见子元素")
            return passed, str(actual)
        return True, "隐藏或不存在" if state == "hidden" else "可见"

    deadline = time.monotonic() + max(1, wait_ms) / 1000
    last_actual = "未找到"
    while True:
        targets: list[Any] = []
        missing_scopes = 0
        if mode == "every_scope" and assertion.get("scope_selector") and assertion.get("relative_selector"):
            scopes = frame.locator(assertion["scope_selector"])
            scope_count = int(await scopes.count())
            for scope_index in range(scope_count):
                scope = scopes.nth(scope_index)
                if assertion.get("count_mode") == "visible" and state != "hidden" and not await scope.is_visible():
                    continue
                matches = scope.locator(assertion["relative_selector"])
                match_count = int(await matches.count())
                if match_count == 0:
                    missing_scopes += 1
                    continue
                targets.extend(matches.nth(index) for index in range(match_count))
            total_scope = len(targets) + missing_scopes
        else:
            locator = frame.locator(assertion["selector"])
            count = int(await locator.count())
            targets = []
            for index in range(count):
                target = locator.nth(index)
                if assertion.get("count_mode") == "visible" and state != "hidden" and not await target.is_visible():
                    continue
                targets.append(target)
            total_scope = count

        satisfied = 0
        for target in targets:
            if await _locator_satisfies(target, state):
                satisfied += 1
        if state == "hidden":
            passed = satisfied == len(targets)
        else:
            passed = total_scope > 0 and missing_scopes == 0 and bool(targets) and satisfied == len(targets)
        last_actual = f"{satisfied}/{len(targets)} 个元素满足，缺失 {missing_scopes} 个作用域"
        if passed:
            return True, last_actual
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_actual
        await asyncio.sleep(min(0.1, remaining))


async def _wait_for_element_count(locator: Any, operator: str, expected: int, wait_ms: int, visible_only: bool = False) -> tuple[int, bool]:
    deadline = time.monotonic() + max(1, wait_ms) / 1000
    actual = 0
    satisfied_since: float | None = None
    quiet_window = min(0.5, max(0.15, wait_ms / 5000))
    while True:
        actual = await _visible_locator_count(locator) if visible_only else int(await locator.count())
        now = time.monotonic()
        if _compare_count(actual, operator, expected):
            if operator in {"gt", "gte"}:
                return actual, True
            if satisfied_since is None:
                satisfied_since = now
            elif now - satisfied_since >= quiet_window:
                return actual, True
        else:
            satisfied_since = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return actual, False
        await asyncio.sleep(min(0.1, remaining))


def _operator_label(operator: str) -> str:
    return {
        "eq": "=",
        "ne": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }.get(operator, operator)


async def _goto_ui_page(page: Any, url: str, timeout_ms: int, timeout_label: str) -> None:
    current_url = str(getattr(page, "url", "") or "")
    if current_url and urldefrag(current_url).url.rstrip("/") == urldefrag(url).url.rstrip("/"):
        return
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        if _is_playwright_timeout(exc):
            raise asyncio.TimeoutError(f"{timeout_label}：{timeout_ms}ms") from exc
        raise


def _page_is_closed(page: Any) -> bool:
    is_closed = getattr(page, "is_closed", None)
    return bool(is_closed()) if callable(is_closed) else False


async def _run_phase_with_timeout(awaitable: Any, timeout_ms: int, timeout_label: str) -> Any:
    timeout = _timeout_seconds(timeout_ms)
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise asyncio.TimeoutError(f"{timeout_label}：{_configured_timeout_ms(timeout_ms)}ms") from exc


def _configured_timeout_ms(timeout_ms: int | None) -> int:
    return max(1, int(timeout_ms or 15000))


def _timeout_seconds(timeout_ms: int | None) -> float:
    return max(0.001, _configured_timeout_ms(timeout_ms) / 1000)


def _deadline_from_now(timeout_ms: int | None) -> float:
    return time.monotonic() + _timeout_seconds(timeout_ms)


def _assertion_wait_ms(ctx: RunContext, deadline: float | None = None) -> int:
    configured = max(1, int(ctx.timeout_ms or 15000))
    if deadline is None:
        return configured
    remaining = int((deadline - time.monotonic()) * 1000)
    return max(1, min(configured, remaining))


def _is_playwright_timeout(exc: Exception) -> bool:
    return exc.__class__.__name__ == "TimeoutError" and "playwright" in exc.__class__.__module__


async def _wait_for_initial_ui(page: Any) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    try:
        await page.evaluate(
            """
            () => new Promise((resolve) => {
              const target = document.body || document.documentElement;
              if (!target) {
                resolve();
                return;
              }
              let quietTimer;
              let hardTimer;
              const finish = () => {
                clearTimeout(quietTimer);
                clearTimeout(hardTimer);
                observer.disconnect();
                resolve();
              };
              const resetQuietWindow = () => {
                clearTimeout(quietTimer);
                quietTimer = setTimeout(finish, 300);
              };
              const observer = new MutationObserver(resetQuietWindow);
              observer.observe(target, { subtree: true, childList: true, attributes: true, characterData: true });
              hardTimer = setTimeout(finish, 2000);
              resetQuietWindow();
            })
            """
        )
    except Exception:
        await page.wait_for_timeout(300)
