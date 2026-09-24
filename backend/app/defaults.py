from __future__ import annotations

from .config import ALERT_DETAIL_BASE_URL


DEFAULT_SETTINGS: dict[str, object] = {
    "default_interval_seconds": 300,
    "default_ui_timeout_ms": 15000,
    "default_api_timeout_ms": 10000,
    "max_concurrency": 2,
    "max_ui_concurrency": 1,
    "max_queue_size": 50,
    "api_pool_size": 5,
    "browser_pool_size": 5,
    "max_task_runtime_seconds": 60,
    "system_alerts_enabled": True,
    "alert_detail_base_url": ALERT_DETAIL_BASE_URL,
    "system_notification_channels": [],
    "system_notification_channel_ids": [],
    "members": [],
    "environment_variables": [],
    "alert_delivery_attempts": 3,
    "api_failure_confirmation_count": 2,
    "ui_failure_confirmation_count": 3,
    "recovery_confirmation_count": 2,
    "api_retry_attempts": 1,
    "ui_retry_attempts": 1,
    "stale_after_intervals": 2,
    "browser_headless": True,
    "browser_type": "chromium",
    "enabled_browser_types": ["chromium"],
    "prewarmed_browser_types": ["chromium"],
    "browser_pool_sizes": {"chromium": 5, "firefox": 5, "webkit": 5},
    "browser_recycle_after_runs": 20,
    "browser_proxy": "",
    "browser_viewport": "1440x900",
    "run_retention_days": 365,
    "run_detail_retention_days": 30,
    "screenshot_retention_days": 30,
    "trace_retention_days": 7,
    "response_retention_days": 30,
    "similar_failure_retention_count": 10,
    "trace_artifacts_enabled": False,
    "success_response_artifacts_enabled": False,
    "database_backup_retention": 7,
    "read_only_token": "",
    "read_only_tokens": [],
    "local_runner_name": "local",
    "local_runner_address": "127.0.0.1",
    "local_runner_region": "local",
    "maintenance_enabled": False,
    "maintenance_title": "",
    "maintenance_message": "",
    "maintenance_starts_at": "",
    "maintenance_ends_at": "",
}


DEFAULT_PROJECT_ALERT_SETTINGS: dict[str, object] = {
    "alerts_enabled": False,
    "notification_channels": [],
    "execution_notification_channel_ids": [],
    "alert_tag_policies": [],
    "alert_cooldown_minutes": 30,
    "recovery_notification": True,
}


UI_SCRIPT_TEMPLATE = """async def check(ctx):
    page = await ctx.new_page()

    await page.goto(ctx.entry_url, wait_until="networkidle")

    await ctx.expect_text(page, "Example Domain")
    await ctx.expect_hidden(page, "text=系统异常")
"""


API_SCRIPT_TEMPLATE = """async def check(ctx):
    resp = await ctx.request()

    ctx.assert_status(resp, 200)

    data = resp.json()

    ctx.assert_json_schema(data, {
        "type": "object",
        "required": ["userId", "id", "title", "completed"],
        "properties": {
            "userId": {"type": "integer"},
            "id": {"type": "integer"},
            "title": {"type": "string"},
            "completed": {"type": "boolean"}
        }
    })

    ctx.save_response(resp)
"""


API_ASSERTIONS_TEMPLATE = """[
  {"id": "status-200", "type": "status_code", "enabled": true, "expected_status": 200},
  {"id": "duration-1000", "type": "response_time", "enabled": true, "max_ms": 1000},
  {"id": "field-id", "type": "json_path_exists", "enabled": true, "path": "$.id"},
  {"id": "field-title", "type": "json_path_exists", "enabled": true, "path": "$.title"}
]"""


DEMO_CHECKS = [
    {
        "name": "Example Domain 页面",
        "type": "ui",
        "enabled": True,
        "interval_seconds": 300,
        "timeout_ms": 15000,
        "entry_url": "https://example.com",
        "method": "",
        "headers_json": "{}",
        "body": "",
        "assertions_json": "[]",
        "script": UI_SCRIPT_TEMPLATE,
        "tags": "demo,public",
    },
    {
        "name": "JSONPlaceholder Todo 接口",
        "type": "api",
        "enabled": True,
        "interval_seconds": 300,
        "timeout_ms": 10000,
        "entry_url": "https://jsonplaceholder.typicode.com/todos/1",
        "method": "GET",
        "headers_json": "{}",
        "body": "",
        "assertions_json": API_ASSERTIONS_TEMPLATE,
        "script": "",
        "tags": "demo,public",
    },
]
