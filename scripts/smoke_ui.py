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


def smoke_project_module_library_simple(page, context, base: str, output: Path, first_project: dict, second_project: dict) -> None:
    """Exercise the current project library through the real compact UI."""
    from playwright.sync_api import expect

    project_id = int(first_project["id"])
    headers = {"X-PulseGuard-Project-Id": str(project_id)}
    module_name = "smoke_methods"
    module_source = (
        "def marker(ctx):\n"
        "    return 'ok'\n"
    )
    module_source_v2 = module_source.replace("'ok'", "'updated'")

    def library_response(response):
        return response.url.endswith(f"/api/projects/{project_id}/library") and response.request.method == "PUT"

    page.goto(f"{base}/projects/modules?project_id={project_id}")
    expect(page.get_by_role("heading", name="公共方法", exact=True)).to_be_visible()
    page.get_by_role("button", name="新建脚本", exact=True).click()
    editor = page.get_by_role("dialog", name="新建脚本", exact=True)
    editor.locator("input").nth(0).fill(module_name)
    editor.locator("input").nth(1).fill("Smoke helper")
    set_monaco_source(page, module_source, context, base)
    with page.expect_response(library_response) as create_response:
        editor.get_by_role("button", name="保存", exact=True).click()
    created = create_response.value.json()
    assert create_response.value.ok, created
    assert created["effective_modules"][0]["name"] == module_name, created
    expect(page.get_by_role("row").filter(has_text=module_name)).to_be_visible()

    row = page.get_by_role("row").filter(has_text=module_name).first
    row.get_by_role("button", name="复制引用", exact=True).click()
    expect(page.get_by_text("引用已复制", exact=True)).to_be_visible()
    copied = page.evaluate("navigator.clipboard.readText()")
    assert copied == f'ctx.modules.use("{module_name}")', copied

    row.get_by_role("button", name="编辑", exact=True).click()
    editor = page.get_by_role("dialog", name="编辑脚本", exact=True)
    set_monaco_source(page, "def broken(:\n    pass\n", context, base)
    with page.expect_response(library_response) as invalid_response:
        editor.get_by_role("button", name="保存", exact=True).click()
    assert invalid_response.value.status == 400, invalid_response.value.text()
    expect(editor.locator(".ant-alert")).to_be_visible()
    set_monaco_source(page, module_source_v2, context, base)
    editor.locator("input").nth(1).fill("Smoke helper updated")
    set_monaco_source(page, module_source_v2, context, base)
    with page.expect_response(library_response) as update_response:
        editor.get_by_role("button", name="保存", exact=True).click()
    updated = update_response.value.json()
    assert update_response.value.ok, updated
    assert updated["effective_modules"][0]["description"] == "Smoke helper updated", updated
    expect(page.get_by_text("Smoke helper updated", exact=True)).to_be_visible()
    page.screenshot(path=str(output / "module-library-list.png"), full_page=True)
    row = page.get_by_role("row").filter(has_text=module_name).first
    row.get_by_role("button", name="编辑", exact=True).click()
    editor = page.get_by_role("dialog", name="编辑脚本", exact=True)
    expect(editor).to_be_visible()
    page.screenshot(path=str(output / "module-library-editor.png"), full_page=True)
    page.locator(".module-editor-drawer .ant-drawer-footer button").first.click()

    # Seed real task references through the temporary API. Both API script and
    # UI setup/script use the same saved module, so deletion exercises the
    # server-owned static reference guard rather than a UI-only check.
    api_payload = {
        "name": "Smoke Module API",
        "type": "api",
        "enabled": False,
        "interval_seconds": 300,
        "timeout_ms": 15000,
        "entry_url": f"{base}/__smoke/api-seed",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": "[]",
        "setup_script": "",
        "script": f'async def check(ctx):\n    helper = ctx.modules.use("{module_name}")\n    helper.marker(ctx)\n    await ctx.request()\n',
        "tags": "",
        "alert_policy_json": "{}",
        "runner_selection_mode": "selected_parallel",
        "runner_ids": ["local"],
        "browser_selection_mode": "selected_parallel",
        "browser_types": [],
    }
    ui_payload = {
        "name": "Smoke Module UI",
        "type": "ui",
        "enabled": False,
        "interval_seconds": 300,
        "timeout_ms": 15000,
        "entry_url": f"{base}/artifacts/responses/smoke.html",
        "viewport_mode": "web",
        "method": "",
        "headers_json": "{}",
        "body": "",
        "assertions_json": "[]",
        "setup_script": f'async def setup(ctx, page):\n    helper = ctx.modules.use("{module_name}")\n    helper.marker(ctx)\n',
        "script": f'async def check(ctx):\n    helper = ctx.modules.use("{module_name}")\n    helper.marker(ctx)\n',
        "tags": "",
        "alert_policy_json": "{}",
        "runner_selection_mode": "selected_parallel",
        "runner_ids": ["local"],
        "browser_selection_mode": "selected_parallel",
        "browser_types": ["chromium"],
    }
    created_api = context.request.post(f"{base}/api/checks", data=api_payload, headers=headers)
    assert created_api.ok, created_api.text()
    created_ui = context.request.post(f"{base}/api/checks", data=ui_payload, headers=headers)
    assert created_ui.ok, created_ui.text()

    page.reload()
    row = page.get_by_role("row").filter(has_text=module_name).first
    expect(row).to_be_visible()
    row.get_by_role("button", name="删除", exact=True).click()
    confirm_delete = page.locator(".ant-popconfirm-buttons .ant-btn-primary").last
    expect(confirm_delete).to_be_visible()
    with page.expect_response(library_response) as delete_response:
        confirm_delete.click()
    assert delete_response.value.status == 400, delete_response.value.text()
    expect(page.get_by_role("row").filter(has_text=module_name)).to_be_visible()
    expect(page.get_by_text(re.compile(f"模块不存在.*{re.escape(module_name)}"))).to_be_visible()
    page.keyboard.press("Escape")
    page.screenshot(path=str(output / "module-library-delete-blocked.png"), full_page=True)

    page.get_by_role("button", name=re.compile("当前项目：")).click()
    page.get_by_role("menuitem", name=second_project["name"], exact=True).click()
    expect(page.get_by_role("button", name=re.compile(f"当前项目：{re.escape(second_project['name'])}"))).to_be_visible()
    expect(page.get_by_role("row").filter(has_text=module_name)).to_have_count(0)
    page.screenshot(path=str(output / "module-library-project-switch.png"), full_page=True)

    # Delay a real delete response while switching projects. The old request
    # must neither keep the new project busy nor overwrite its library.
    second_url = f"{base}/api/projects/{second_project['id']}/library"
    second_headers = {"X-PulseGuard-Project-Id": str(second_project["id"])}
    second_library = context.request.get(second_url, headers=second_headers).json()
    seeded = context.request.put(second_url, headers=second_headers, data={
        "expected_revision": second_library["effective_revision"],
        "modules": [{"name": "delete_pending", "source": module_source}],
    })
    assert seeded.ok, seeded.text()
    page.goto(f"{base}/projects/modules?project_id={second_project['id']}")
    pending = []

    def delay_delete(route):
        if route.request.method == "PUT":
            pending.append(route)
        else:
            route.continue_()

    page.route(second_url, delay_delete)
    page.get_by_role("row").filter(has_text="delete_pending").get_by_role("button", name="删除", exact=True).click()
    page.locator(".ant-popconfirm-buttons .ant-btn-primary").last.click()
    expect(page.get_by_role("button", name="新建脚本", exact=True)).to_be_disabled()
    page.get_by_role("button", name=re.compile("当前项目：")).click()
    page.get_by_role("menuitem", name=first_project["name"], exact=True).click()
    expect(page.get_by_role("button", name="新建脚本", exact=True)).to_be_enabled()
    expect(page.get_by_role("row").filter(has_text=module_name)).to_be_visible()
    assert pending, "delete request was not intercepted"
    for route in pending:
        route.fulfill(response=route.fetch())
    page.unroute(second_url, delay_delete)
    expect(page.get_by_role("button", name="新建脚本", exact=True)).to_be_enabled()
    expect(page.get_by_role("row").filter(has_text=module_name)).to_be_visible()
    expect(page.get_by_role("row").filter(has_text="delete_pending")).to_have_count(0)
    page.screenshot(path=str(output / "module-library-pending-switch.png"), full_page=True)


def set_monaco_source(page, source: str, context, base: str) -> None:
    """Replace a Monaco model through its input area without auto-indent drift."""
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
    page.evaluate("async (text) => navigator.clipboard.writeText(text)", source)
    editor = page.locator(".module-editor-drawer .monaco-editor").first
    editor.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Control+V")


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
        storage.update_settings({"prewarmed_browser_types": [], "system_alerts_enabled": False, "api_retry_attempts": 0})
        first_project = storage.get_project(storage.default_project_id())
        second_project = storage.create_project("Smoke Other")
        from backend.app.main import app
        api_requests: list[str] = []

        @app.get('/__smoke/api-seed')
        def api_seed():
            api_requests.append('seed')
            return {'id': 'fresh'}

        @app.get('/__smoke/api-target/{item_id}')
        def api_target(item_id: str):
            api_requests.append('target:' + item_id)
            return {'returncode': 17 if item_id == 'bad' else 0, 'result': {'records': [{'id': item_id}]}}

        # The app's SPA fallback is already registered; fixtures must precede it.
        fixture_routes = app.router.routes[-2:]
        del app.router.routes[-2:]
        app.router.routes[0:0] = fixture_routes

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
                        drawer.get_by_role("button", name="试运行草稿", exact=True).click()
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

                    # A real dependency request supplies the configured target URL. Visible
                    # rules must evaluate that last response without repeating the request.
                    chain_name = 'Smoke dynamic API'
                    chain_script = (
                        'async def check(ctx):\n'
                        f'    response = await ctx.http.get("{base}/__smoke/api-seed")\n'
                        '    ctx.assert_status(response, 200)\n'
                        '    ctx.vars.set("SMOKE_ITEM", response.json()["id"])\n'
                        '    await ctx.request()\n'
                    )
                    chain_payload = {
                        'name': chain_name, 'type': 'api', 'enabled': False,
                        'entry_url': base + '/__smoke/api-target/${SMOKE_ITEM}',
                        'method': 'GET', 'script': chain_script,
                        'assertions_json': json.dumps([
                            {'id': 'http', 'type': 'status_code', 'expected_status': 200},
                            {'id': 'business', 'type': 'json_path_equals', 'path': '$.returncode', 'expected_value': '0'},
                            {'id': 'records', 'type': 'json_path_type', 'path': '$.result.records', 'expected_type': 'array'},
                        ]),
                    }
                    project_headers = {'X-PulseGuard-Project-Id': str(first_project['id'])}
                    created_chain = context.request.post(f'{base}/api/checks', data=chain_payload, headers=project_headers)
                    assert created_chain.ok, created_chain.text()
                    chain_check = created_chain.json()
                    page.goto(f'{base}/api-checks')
                    page.get_by_role('button', name=f'{chain_name} 编辑', exact=True).click()
                    chain_drawer = page.get_by_role('dialog', name='编辑任务', exact=True)
                    if not chain_drawer.get_by_role('button', name='执行请求', exact=True).is_visible():
                        chain_drawer.get_by_text('健康判定规则', exact=True).first.click()
                    expect(chain_drawer.get_by_text('3 项启用', exact=True).first).to_be_visible()
                    before_requests = len(api_requests)
                    with page.expect_response(lambda r: r.url.endswith('/api/checks/inspect-api')) as inspected_chain:
                        chain_drawer.get_by_role('button', name='执行请求', exact=True).click()
                    assert inspected_chain.value.ok, inspected_chain.value.text()
                    inspected = inspected_chain.value.json()
                    assert any(item['path'] == '$.result.records' for item in inspected['json_paths']), inspected
                    assert api_requests[before_requests:] == ['seed', 'target:fresh'], api_requests
                    before_requests = len(api_requests)
                    with page.expect_response(lambda r: r.url.endswith('/api/checks/debug')) as debug_chain_response:
                        chain_drawer.get_by_role('button', name='试运行草稿', exact=True).click()
                    chain_run = debug_chain_response.value.json()
                    assert debug_chain_response.value.ok and chain_run['status'] == 'ok', chain_run
                    assert api_requests[before_requests:] == ['seed', 'target:fresh'], api_requests
                    response_snapshot = json.loads(chain_run['response_snapshot'])
                    assert len(response_snapshot['assertions']) == 3, response_snapshot
                    expect(chain_drawer.get_by_role('heading', name='调试结果', exact=True)).to_be_visible()
                    chain_drawer.get_by_role('button', name='删除校验项', exact=True).last.scroll_into_view_if_needed()
                    page.screenshot(path=str(output / 'api-dynamic-rules.png'), full_page=True, animations='disabled')
                    chain_drawer.get_by_role('tab', name='执行信息', exact=True).click()
                    chain_drawer.get_by_text('接口请求链', exact=True).scroll_into_view_if_needed()
                    expect(chain_drawer.get_by_text('目标请求耗时', exact=True).first).to_be_visible()
                    expect(chain_drawer.get_by_text('全程探测耗时', exact=True).first).to_be_visible()
                    expect(chain_drawer.get_by_text('最近 2 条', exact=True)).to_be_visible()
                    page.screenshot(path=str(output / 'api-request-history.png'), full_page=True, animations='disabled')
                    failed_payload = {**chain_payload, 'script': chain_script.replace('response.json()["id"]', '"bad"')}
                    before_requests = len(api_requests)
                    negative = context.request.post(f'{base}/api/checks/debug', data=failed_payload, headers=project_headers)
                    assert negative.ok, negative.text()
                    negative_run = negative.json()
                    assert negative_run['status'] == 'failed', negative_run
                    assert api_requests[before_requests:] == ['seed', 'target:bad'], api_requests
                    failed_assertions = json.loads(negative_run['response_snapshot'])['assertions']
                    assert any(item.get('path') == '$.returncode' and item['status'] == 'failed' for item in failed_assertions), failed_assertions

                    dependency_payload = {**chain_payload, 'script': chain_script.replace('ctx.assert_status(response, 200)', 'ctx.assert_status(response, 503)')}
                    updated = context.request.put(f'{base}/api/checks/{chain_check["id"]}', data=dependency_payload, headers=project_headers)
                    assert updated.ok, updated.text()
                    page.goto(f'{base}/api-checks')
                    page.get_by_role('button', name=f'{chain_name} 编辑', exact=True).click()
                    chain_drawer = page.get_by_role('dialog', name='编辑任务', exact=True)
                    before_requests = len(api_requests)
                    with page.expect_response(lambda r: r.url.endswith('/api/checks/inspect-api')) as failed_preview:
                        chain_drawer.get_by_role('button', name='执行请求', exact=True).click()
                    assert failed_preview.value.status == 400, failed_preview.value.text()
                    assert api_requests[before_requests:] == ['seed'], api_requests
                    chain_drawer.get_by_role('button', name='执行详情', exact=True).click()
                    evidence_dialog = page.get_by_role('dialog', name='执行详情', exact=True)
                    expect(evidence_dialog.get_by_role('tab', name='请求记录', exact=True)).to_be_visible()
                    expect(evidence_dialog.get_by_text('最近请求与响应（最多 10 条）', exact=True)).to_be_visible()
                    page.screenshot(path=str(output / 'api-preview-failure.png'), full_page=True, animations='disabled')
                    page.keyboard.press('Escape')
                    expect(evidence_dialog).not_to_be_visible()

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
                    smoke_project_module_library_simple(page, context, base, output, first_project, second_project)
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
