"""Exercise the built console against a disposable local instance."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "playwright" / "smoke")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        parser.error("build the frontend first with npm run build")
    sys.path.insert(0, str(ROOT))

    with tempfile.TemporaryDirectory(prefix="pulseguard-smoke-") as temporary:
        root = Path(temporary)
        for key in list(os.environ):
            if key.startswith("PULSEGUARD_"):
                del os.environ[key]
        os.environ.update({
            "PULSEGUARD_DATA_DIR": str(root / "data"),
            "PULSEGUARD_DB_PATH": str(root / "data" / "test.db"),
            "PULSEGUARD_REPORTS_DIR": str(root / "reports"),
            "PULSEGUARD_BACKUPS_DIR": str(root / "backups"),
            "PULSEGUARD_STATIC_DIR": str(ROOT / "frontend" / "dist"),
        })
        os.environ["NO_PROXY"] = ",".join(filter(None, [os.getenv("NO_PROXY"), "127.0.0.1", "localhost"]))
        from backend.app import storage
        import uvicorn
        from playwright.sync_api import expect, sync_playwright

        storage.init_db()
        for check in storage.list_checks():
            storage.update_check(check["id"], {**check, "enabled": False})
        storage.update_settings({"prewarmed_browser_types": [], "system_alerts_enabled": False})
        first_project = storage.get_project(storage.default_project_id())
        second_project = storage.create_project("Smoke Other")
        from backend.app.main import app
        (root / "reports" / "responses" / "smoke.html").write_text(
            '<!doctype html><html lang="en"><title>Smoke target</title>'
            '<body><button id="smoke-save">Save item</button></body></html>', encoding="utf-8"
        )

        # Hold an ephemeral loopback socket through startup: no port race or live service collision.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("temporary API did not start")
                time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                page.set_default_timeout(15000)
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                name = "Smoke API"
                try:
                    page.goto(f'{base}/api-checks?project_id={first_project["id"]}')
                    expect(page.get_by_role("button", name="新增任务", exact=True).first).to_be_visible()
                    page.get_by_role("button", name="新增任务", exact=True).first.click()
                    drawer = page.get_by_role("dialog", name="新增任务", exact=True)
                    drawer.get_by_role("button", name="应用模板", exact=True).click()
                    drawer.locator('input[name="check-name"]').fill(name)
                    drawer.locator('input[name="check-entry-url"]').fill(f"{base}/api/health")
                    with page.expect_response(lambda response: response.url.endswith("/api/checks/debug") and response.request.method == "POST") as debug_response:
                        drawer.get_by_role("button", name="试运行当前配置", exact=True).click()
                    debug = debug_response.value.json()
                    assert debug_response.value.ok and debug["status"] == "ok", debug
                    expect(drawer.get_by_role("heading", name="调试结果", exact=True)).to_be_visible()
                    page.screenshot(path=str(output / "debug.png"), full_page=True)
                    with page.expect_response(lambda response: response.url.endswith("/api/checks") and response.request.method == "POST") as saved_response:
                        drawer.get_by_role("button", name="保存", exact=True).click()
                    saved = saved_response.value.json()
                    assert saved_response.value.ok and saved["name"] == name, saved
                    expect(drawer).not_to_be_visible()
                    with page.expect_response(lambda response: response.url.endswith(f'/api/checks/{saved["id"]}/run')) as run_response:
                        page.get_by_role("button", name=f"{name} 执行", exact=True).click()
                    run = run_response.value.json()
                    assert run_response.value.ok and run["status"] == "ok", run

                    page.goto(f"{base}/runs")
                    page.get_by_placeholder("任务名称…").fill(name)
                    row = page.get_by_role("row").filter(has_text=name).first
                    expect(row).to_be_visible()
                    row.get_by_role("button", name="查看详情", exact=True).click()
                    detail = page.get_by_role("dialog")
                    expect(detail).to_contain_text(name)
                    detail.get_by_role("button", name="完整详情", exact=True).click()
                    expect(page).to_have_url(re.compile(r"/runs/\d+"))
                    expect(page.get_by_text(name, exact=True).first).to_be_visible()
                    page.screenshot(path=str(output / "run-detail.png"), full_page=True)

                    page.goto(f"{base}/monitoring")
                    trend = page.get_by_role("button", name=f"查看「{name}」趋势详情", exact=True)
                    expect(trend).to_be_visible()
                    trend.focus()
                    expect(trend).to_be_focused()
                    page.screenshot(path=str(output / "monitoring-1440.png"), full_page=True)
                    trend.press("Enter")
                    expect(page.get_by_role("dialog")).to_contain_text(name)
                    page.keyboard.press("Escape")
                    page.set_viewport_size({"width": 1280, "height": 900})
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    page.wait_for_function("document.documentElement.scrollWidth <= window.innerWidth", timeout=5000)
                    page.screenshot(path=str(output / "monitoring-1280.png"), full_page=True)

                    page.goto(f"{base}/api-checks")
                    page.get_by_role("button", name=re.compile("当前项目：")).click()
                    with page.expect_response(lambda response: "/api/checks" in response.url and response.request.headers.get("x-pulseguard-project-id") == str(second_project["id"])):
                        page.get_by_role("menuitem", name="Smoke Other", exact=True).click()
                    expect(page.get_by_role("row").filter(has_text=name)).to_have_count(0)
                    page.reload()
                    expect(page.get_by_role("button", name=re.compile("当前项目：Smoke Other"))).to_be_visible()
                    page.get_by_role("button", name=re.compile("当前项目：")).click()
                    page.get_by_role("menuitem", name=first_project["name"], exact=True).click()
                    expect(page.get_by_role("row").filter(has_text=name)).to_have_count(1)

                    page.goto(f"{base}/ui-checks")
                    page.get_by_role("button", name="新增任务", exact=True).first.click()
                    drawer = page.get_by_role("dialog", name="新增任务", exact=True)
                    drawer.locator('input[name="check-name"]').fill("Smoke UI")
                    drawer.locator('input[name="check-entry-url"]').fill(f"{base}/artifacts/responses/smoke.html")
                    drawer.get_by_text("健康判定规则", exact=True).click()
                    with page.expect_response(lambda response: response.url.endswith("/api/checks/inspect-ui"), timeout=45000) as scan_response:
                        drawer.get_by_role("button", name="选择元素", exact=True).click()
                    assert scan_response.value.ok, scan_response.value.text()
                    picker = page.get_by_role("dialog", name="选择页面元素", exact=True)
                    candidate = picker.get_by_role("button", name="选择候选元素 Save_item", exact=True)
                    candidate.focus()
                    expect(candidate).to_be_focused()
                    candidate.press("Enter")
                    candidate_row = picker.locator("article").filter(
                        has=page.get_by_role("button", name="选择候选元素 Save_item", exact=True)
                    )
                    expect(candidate_row).to_have_class(re.compile(r"\bselected\b"))
                    candidate_row.get_by_role("checkbox", name="可见", exact=True).check()
                    page.screenshot(path=str(output / "ui-candidate.png"), full_page=True)
                    picker.get_by_role("button", name="保存选择", exact=True).click()
                    expect(picker).not_to_be_visible()
                    expect(drawer.get_by_text("1 项启用", exact=True)).to_be_visible()
                    expect(drawer.get_by_placeholder("CSS / text= / role= Playwright 选择器…")).to_have_value("#smoke-save")
                    assert not errors, errors
                    (output / "result.json").write_text(json.dumps({"status": "ok", "check_id": saved["id"], "run_id": run["id"], "page_errors": errors}, indent=2), encoding="utf-8")
                    print(f"PASS browser smoke: create, debug, history, trend/candidate keyboard access and project persistence — {output}")
                except BaseException:
                    page.screenshot(path=str(output / "failure.png"), full_page=True)
                    (output / "failure.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
                    raise
                finally:
                    context.tracing.stop(path=str(output / "trace.zip"))
                    context.close()
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=20)
            listener.close()
            if thread.is_alive():
                raise RuntimeError("temporary API did not stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
