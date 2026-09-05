from __future__ import annotations

import base64
import hmac
import json
import unittest
from datetime import datetime, timedelta
from hashlib import sha256
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

from backend.app import notifier


class DingtalkNotifierTests(unittest.TestCase):
    def test_disabled_alerts_do_not_record_unused_channel(self) -> None:
        run = {"id": 42, "status": "failed"}
        transition = {"current_status": "failing", "previous_status": "healthy"}
        settings = {
            "alerts_enabled": False,
            "notification_channels": [
                {
                    "id": "ding",
                    "name": "值班钉钉",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token",
                }
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification:
            self.run_async(notifier.maybe_notify({"id": 1, "type": "api", "name": "接口"}, run, transition))

        update_run_notification.assert_called_once_with(42, "disabled", channel=None, error=None, sent_at=None)

    def test_not_required_alerts_do_not_record_unused_channel(self) -> None:
        run = {"id": 43, "status": "skipped"}
        transition = {"current_status": "unknown", "previous_status": "healthy"}
        settings = {
            "alerts_enabled": True,
            "notification_channels": [
                {
                    "id": "ding",
                    "name": "值班钉钉",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token",
                }
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification:
            self.run_async(notifier.maybe_notify({"id": 1, "type": "api", "name": "接口"}, run, transition))

        update_run_notification.assert_called_once_with(43, "not_required", channel=None, error=None, sent_at=None)

    def test_maybe_notify_sends_to_all_enabled_configured_channels(self) -> None:
        run = {"id": 44, "project_id": 7, "status": "failed", "started_at": "2026-06-05T10:00:00+08:00"}
        transition = {"current_status": "failing", "previous_status": "suspected_failing", "consecutive_failures": 2}
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "alert_detail_base_url": "http://10.168.78.49:8787",
            "recovery_notification": True,
            "notification_channels": [
                {
                    "id": "feishu",
                    "name": "飞书群",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/token",
                },
                {
                    "id": "ding",
                    "name": "钉钉群",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token",
                    "dingtalk_secret": "SECabc123",
                },
                {
                    "id": "disabled",
                    "name": "停用渠道",
                    "type": "wecom",
                    "enabled": False,
                    "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=token",
                },
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier.storage, "get_project", return_value={"id": 7, "status": "active"}
        ), patch.object(
            notifier.storage, "get_project_alert_settings", return_value=settings
        ), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:00:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification, patch.object(notifier.storage, "update_last_notified") as update_last_notified:
            self.run_async(
                notifier.maybe_notify({"id": 11, "project_id": 7, "type": "api", "name": "接口"}, run, transition)
            )

        self.assertEqual(send_webhook_alert.await_count, 2)
        sent_channel_ids = [call.args[0]["id"] for call in send_webhook_alert.await_args_list]
        self.assertEqual(sent_channel_ids, ["feishu", "ding"])
        sent_messages = [call.args[2] for call in send_webhook_alert.await_args_list]
        self.assertTrue(
            all(
                "http://10.168.78.49:8787/runs/44?from=%2Fruns%3Fcheck_id%3D11&project_id=7" in message
                for message in sent_messages
            )
        )
        update_run_notification.assert_called_once_with(
            44,
            "sent",
            channel="飞书群、钉钉群",
            error=None,
            sent_at="2026-06-05T10:00:01+08:00",
        )
        update_last_notified.assert_called_once_with(11, "2026-06-05T10:00:01+08:00")

    def test_maybe_notify_uses_execution_channel_selection(self) -> None:
        run = {"id": 55, "project_id": 7, "status": "failed", "started_at": "2026-06-05T10:00:00+08:00"}
        transition = {"current_status": "failing", "previous_status": "healthy", "consecutive_failures": 1}
        global_settings = {
            "members": [],
            "system_notification_channels": [
                {"id": "infra", "name": "系统群", "type": "wecom", "enabled": True, "webhook_url": "https://example.test/infra"},
            ],
        }
        project_settings = {
            "alerts_enabled": True,
            "execution_notification_channel_ids": ["biz"],
            "notification_channels": [
                {"id": "biz", "name": "业务群", "type": "feishu", "enabled": True, "webhook_url": "https://example.test/biz"},
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=global_settings), patch.object(
            notifier.storage, "get_project", return_value={"id": 7, "status": "active"}
        ), patch.object(
            notifier.storage, "get_project_alert_settings", return_value=project_settings
        ) as get_project_alert_settings, patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:00:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ), patch.object(notifier.storage, "update_last_notified"):
            self.run_async(notifier.maybe_notify({"id": 11, "project_id": 7, "type": "api", "name": "接口"}, run, transition))

        get_project_alert_settings.assert_called_once_with(7)
        send_webhook_alert.assert_awaited_once()
        self.assertEqual(send_webhook_alert.await_args.args[0]["id"], "biz")

    def test_notify_system_error_uses_only_system_channels(self) -> None:
        settings = {
            "system_alerts_enabled": True,
            "alert_delivery_attempts": 1,
            "system_notification_channel_ids": ["infra"],
            "system_notification_channels": [
                {"id": "biz", "name": "业务群", "type": "feishu", "enabled": True, "webhook_url": "https://example.test/biz"},
                {"id": "infra", "name": "系统群", "type": "wecom", "enabled": True, "webhook_url": "https://example.test/infra"},
            ],
        }

        with patch.object(notifier, "send_webhook_alert", new_callable=AsyncMock) as send_webhook_alert:
            result = self.run_async(
                notifier.notify_system_error(
                    {
                        "source": "runner",
                        "message": "Page.title: Target crashed",
                        "check_name": "经销商商详白页监控",
                        "run_id": 214236,
                    },
                    settings=settings,
                )
            )

        self.assertTrue(result["sent"])
        send_webhook_alert.assert_awaited_once()
        self.assertEqual(send_webhook_alert.await_args.args[0]["id"], "infra")
        self.assertIn("系统异常", send_webhook_alert.await_args.args[1])
        self.assertIn("Target crashed", send_webhook_alert.await_args.args[2])

    def test_notify_system_error_requires_explicit_system_channels(self) -> None:
        settings = {
            "system_alerts_enabled": True,
            "alert_delivery_attempts": 1,
            "system_notification_channel_ids": [],
            "notification_channels": [
                {"id": "biz", "name": "业务群", "type": "feishu", "enabled": True, "webhook_url": "https://example.test/biz"},
                {"id": "disabled", "name": "停用群", "type": "wecom", "enabled": False, "webhook_url": "https://example.test/disabled"},
                {"id": "missing-url", "name": "未配置群", "type": "dingtalk", "enabled": True, "webhook_url": ""},
            ],
        }

        with patch.object(notifier, "send_webhook_alert", new_callable=AsyncMock) as send_webhook_alert:
            result = self.run_async(
                notifier.notify_system_error(
                    {
                        "source": "runner",
                        "message": "Page.title: Target crashed",
                        "check_name": "经销商商详白页监控",
                        "run_id": 214236,
                    },
                    settings=settings,
                )
            )

        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "no_channels")
        send_webhook_alert.assert_not_awaited()

    def test_manual_failure_obeys_continuous_failure_cooldown(self) -> None:
        run = {"id": 45, "status": "failed", "started_at": "2026-06-05T10:05:00+08:00"}
        transition = {
            "current_status": "failing",
            "previous_status": "failing",
            "last_notified_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "consecutive_failures": 3,
            "trigger": "manual",
        }
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "alert_detail_base_url": "http://10.168.78.49:8787",
            "notification_channels": [
                {
                    "id": "ding",
                    "name": "值班群",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token",
                }
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:05:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification, patch.object(notifier.storage, "update_last_notified") as update_last_notified:
            self.run_async(notifier.maybe_notify({"id": 11, "type": "ui", "name": "商品详情"}, run, transition))

        send_webhook_alert.assert_not_awaited()
        update_last_notified.assert_not_called()
        update_run_notification.assert_called_once()
        self.assertEqual(update_run_notification.call_args.args[:2], (45, "suppressed"))
        self.assertIn("冷却窗口", update_run_notification.call_args.kwargs["error"])

    def test_manual_failure_does_not_send_while_fault_is_still_unconfirmed(self) -> None:
        run = {"id": 54, "status": "failed", "started_at": "2026-06-05T10:06:00+08:00"}
        transition = {
            "current_status": "suspected_failing",
            "previous_status": "healthy",
            "last_notified_at": None,
            "consecutive_failures": 1,
            "trigger": "manual",
        }
        settings = {
            "alerts_enabled": True,
            "notification_channels": [
                {
                    "id": "ding",
                    "name": "值班群",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token",
                }
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:06:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification, patch.object(notifier.storage, "update_last_notified") as update_last_notified:
            self.run_async(notifier.maybe_notify({"id": 11, "type": "ui", "name": "商品详情"}, run, transition))

        send_webhook_alert.assert_not_awaited()
        update_last_notified.assert_not_called()
        update_run_notification.assert_called_once()
        self.assertEqual(update_run_notification.call_args.args[:2], (54, "suppressed"))
        self.assertIn("故障确认", update_run_notification.call_args.kwargs["error"])

    def test_scheduled_continuous_failure_records_cooldown_suppression_reason(self) -> None:
        run = {"id": 46, "status": "failed", "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        last_notified_at = datetime.now().astimezone().isoformat(timespec="seconds")
        transition = {
            "current_status": "failing",
            "previous_status": "failing",
            "last_notified_at": last_notified_at,
            "consecutive_failures": 4,
            "trigger": "scheduled",
        }
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "notification_channels": [
                {
                    "id": "ding",
                    "name": "值班群",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=token",
                }
            ],
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "update_run_notification") as update_run_notification, patch.object(
            notifier.storage, "update_last_notified"
        ) as update_last_notified:
            self.run_async(notifier.maybe_notify({"id": 11, "type": "ui", "name": "商品详情"}, run, transition))

        send_webhook_alert.assert_not_awaited()
        update_last_notified.assert_not_called()
        update_run_notification.assert_called_once()
        self.assertEqual(update_run_notification.call_args.args[:2], (46, "suppressed"))
        self.assertIn("冷却窗口", update_run_notification.call_args.kwargs["error"])

    def test_check_alert_policy_overrides_cooldown_and_channel_selection_without_secret_material(self) -> None:
        run = {"id": 48, "status": "failed", "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        transition = {
            "current_status": "failing",
            "previous_status": "failing",
            "last_notified_at": (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(timespec="seconds"),
            "consecutive_failures": 3,
            "trigger": "scheduled",
        }
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "recovery_notification": True,
            "notification_channels": [
                {
                    "id": "global",
                    "name": "Global",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/global-token",
                },
                {
                    "id": "ops",
                    "name": "Ops",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=global-ops-token",
                    "dingtalk_secret": "SECglobalops",
                },
            ],
        }
        check_policy = {
            "alert_cooldown_minutes": 1,
            "notification_channel_ids": ["ops"],
            "webhook_url": "https://example.invalid/should-not-be-used",
            "dingtalk_secret": "SECtasklocalshouldnotbeused",
        }
        check = {
            "id": 12,
            "type": "api",
            "name": "API",
            "tags": "",
            "alert_policy_json": json.dumps(check_policy),
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:20:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification, patch.object(notifier.storage, "update_last_notified") as update_last_notified:
            self.run_async(notifier.maybe_notify(check, run, transition))

        send_webhook_alert.assert_awaited_once()
        channel = send_webhook_alert.await_args.args[0]
        self.assertEqual(channel["id"], "ops")
        self.assertEqual(channel["webhook_url"], "https://oapi.dingtalk.com/robot/send?access_token=global-ops-token")
        self.assertEqual(channel["dingtalk_secret"], "SECglobalops")
        self.assertNotIn("SECtasklocalshouldnotbeused", json.dumps(channel, ensure_ascii=False))
        update_run_notification.assert_called_once_with(
            48,
            "sent",
            channel="Ops",
            error=None,
            sent_at="2026-06-05T10:20:01+08:00",
        )
        update_last_notified.assert_called_once_with(12, "2026-06-05T10:20:01+08:00")

    def test_check_alert_policy_can_disable_recovery_notifications(self) -> None:
        run = {"id": 49, "status": "ok", "started_at": "2026-06-05T10:21:00+08:00"}
        transition = {"current_status": "healthy", "previous_status": "suspected_recovery", "consecutive_failures": 0}
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "recovery_notification": True,
            "notification_channels": [
                {
                    "id": "ops",
                    "name": "Ops",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/ops-token",
                }
            ],
        }
        check = {
            "id": 13,
            "type": "api",
            "name": "API",
            "tags": "",
            "alert_policy_json": json.dumps({"recovery_notification": False, "notification_channel_ids": ["ops"]}),
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "update_run_notification") as update_run_notification, patch.object(
            notifier.storage, "update_last_notified"
        ) as update_last_notified:
            self.run_async(notifier.maybe_notify(check, run, transition))

        send_webhook_alert.assert_not_awaited()
        update_last_notified.assert_not_called()
        update_run_notification.assert_called_once()
        self.assertEqual(update_run_notification.call_args.args[:2], (49, "suppressed"))

    def test_alert_tag_policy_overrides_global_cooldown_and_channels_for_matching_tag(self) -> None:
        run = {"id": 50, "status": "failed", "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        transition = {
            "current_status": "failing",
            "previous_status": "failing",
            "last_notified_at": (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(timespec="seconds"),
            "consecutive_failures": 5,
            "trigger": "scheduled",
        }
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "recovery_notification": True,
            "notification_channels": [
                {
                    "id": "global",
                    "name": "Global",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/global-token",
                },
                {
                    "id": "tag",
                    "name": "Tag",
                    "type": "wecom",
                    "enabled": True,
                    "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=tag-token",
                },
            ],
            "alert_tag_policies": [
                {
                    "id": "critical-policy",
                    "tag": "critical",
                    "alert_cooldown_minutes": 1,
                    "recovery_notification": True,
                    "notification_channel_ids": ["tag"],
                }
            ],
        }
        check = {"id": 14, "type": "api", "name": "API", "tags": "critical, checkout"}

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:22:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification, patch.object(notifier.storage, "update_last_notified") as update_last_notified:
            self.run_async(notifier.maybe_notify(check, run, transition))

        send_webhook_alert.assert_awaited_once()
        self.assertEqual(send_webhook_alert.await_args.args[0]["id"], "tag")
        update_run_notification.assert_called_once_with(
            50,
            "sent",
            channel="Tag",
            error=None,
            sent_at="2026-06-05T10:22:01+08:00",
        )
        update_last_notified.assert_called_once_with(14, "2026-06-05T10:22:01+08:00")

    def test_alert_tag_policy_can_disable_recovery_notifications_for_matching_tag(self) -> None:
        run = {"id": 51, "status": "ok", "started_at": "2026-06-05T10:23:00+08:00"}
        transition = {"current_status": "healthy", "previous_status": "suspected_recovery", "consecutive_failures": 0}
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "recovery_notification": True,
            "notification_channels": [
                {
                    "id": "tag",
                    "name": "Tag",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/tag-token",
                }
            ],
            "alert_tag_policies": [
                {
                    "id": "critical-policy",
                    "tag": "critical",
                    "recovery_notification": False,
                    "notification_channel_ids": ["tag"],
                }
            ],
        }
        check = {"id": 15, "type": "api", "name": "API", "tags": "critical"}

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "update_run_notification") as update_run_notification, patch.object(
            notifier.storage, "update_last_notified"
        ) as update_last_notified:
            self.run_async(notifier.maybe_notify(check, run, transition))

        send_webhook_alert.assert_not_awaited()
        update_last_notified.assert_not_called()
        update_run_notification.assert_called_once()
        self.assertEqual(update_run_notification.call_args.args[:2], (51, "suppressed"))

    def test_check_alert_policy_takes_priority_over_matching_tag_policy(self) -> None:
        run = {"id": 52, "status": "failed", "started_at": "2026-06-05T10:24:00+08:00"}
        transition = {"current_status": "failing", "previous_status": "suspected_failing", "consecutive_failures": 2}
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "recovery_notification": True,
            "notification_channels": [
                {
                    "id": "global",
                    "name": "Global",
                    "type": "feishu",
                    "enabled": True,
                    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/global-token",
                },
                {
                    "id": "tag",
                    "name": "Tag",
                    "type": "wecom",
                    "enabled": True,
                    "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=tag-token",
                },
                {
                    "id": "task",
                    "name": "Task",
                    "type": "dingtalk",
                    "enabled": True,
                    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=task-token",
                    "dingtalk_secret": "SECtaskglobal",
                },
            ],
            "alert_tag_policies": [
                {
                    "id": "critical-policy",
                    "tag": "critical",
                    "alert_cooldown_minutes": 1,
                    "recovery_notification": True,
                    "notification_channel_ids": ["tag"],
                }
            ],
        }
        check = {
            "id": 16,
            "type": "api",
            "name": "API",
            "tags": "critical",
            "alert_policy_json": json.dumps(
                {
                    "alert_cooldown_minutes": 1,
                    "recovery_notification": True,
                    "notification_channel_ids": ["task"],
                }
            ),
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:24:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ) as update_run_notification, patch.object(notifier.storage, "update_last_notified") as update_last_notified:
            self.run_async(notifier.maybe_notify(check, run, transition))

        send_webhook_alert.assert_awaited_once()
        self.assertEqual(send_webhook_alert.await_args.args[0]["id"], "task")
        update_run_notification.assert_called_once_with(
            52,
            "sent",
            channel="Task",
            error=None,
            sent_at="2026-06-05T10:24:01+08:00",
        )
        update_last_notified.assert_called_once_with(16, "2026-06-05T10:24:01+08:00")

    def test_maybe_notify_resolves_selected_member_accounts_per_channel(self) -> None:
        run = {"id": 53, "status": "failed", "started_at": "2026-06-05T10:25:00+08:00"}
        transition = {"current_status": "failing", "previous_status": "healthy", "consecutive_failures": 2}
        settings = {
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "notification_channels": [
                {"id": "fei", "name": "Feishu", "type": "feishu", "enabled": True, "webhook_url": "https://example.test/fei"},
                {"id": "wx", "name": "WeCom", "type": "wecom", "enabled": True, "webhook_url": "https://example.test/wx"},
                {"id": "ding", "name": "Ding", "type": "dingtalk", "enabled": True, "webhook_url": "https://example.test/ding"},
            ],
            "members": [
                {
                    "id": "alice",
                    "name": "Alice",
                    "feishu_open_id": "ou_alice",
                    "wecom_user_id": "alice.wx",
                    "wecom_mobile": "13800000001",
                    "dingtalk_user_id": "alice.ding",
                    "dingtalk_mobile": "13900000001",
                },
                {
                    "id": "bob",
                    "name": "Bob",
                    "feishu_open_id": "ou_bob",
                    "wecom_user_id": "bob.wx",
                    "wecom_mobile": "",
                    "dingtalk_user_id": "",
                    "dingtalk_mobile": "",
                },
            ],
        }
        check = {
            "id": 17,
            "type": "api",
            "name": "API",
            "alert_policy_json": json.dumps({"member_ids": ["alice"]}),
        }

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new_callable=AsyncMock
        ) as send_webhook_alert, patch.object(notifier.storage, "now_iso", return_value="2026-06-05T10:25:01+08:00"), patch.object(
            notifier.storage, "update_run_notification"
        ), patch.object(notifier.storage, "update_last_notified"):
            self.run_async(notifier.maybe_notify(check, run, transition))

        channels = {call.args[0]["id"]: call.args[0] for call in send_webhook_alert.await_args_list}
        self.assertEqual(channels["fei"]["mentions"], [{"name": "Alice", "user_id": "ou_alice"}])
        self.assertEqual(
            channels["wx"]["mentions"],
            [{"name": "Alice", "user_id": "alice.wx", "mobile": "13800000001"}],
        )
        self.assertEqual(
            channels["ding"]["mentions"],
            [{"name": "Alice", "user_id": "alice.ding", "mobile": "13900000001"}],
        )

    def test_webhook_payload_uses_native_mention_fields_for_each_channel(self) -> None:
        feishu = notifier._webhook_payload(
            "feishu",
            "Alert",
            "Body",
            [{"name": "Alice", "user_id": "ou_alice"}],
        )
        wecom = notifier._webhook_payload(
            "wecom",
            "Alert",
            "Body",
            [{"name": "Alice", "user_id": "alice.wx", "mobile": "13800000001"}],
        )
        dingtalk = notifier._webhook_payload(
            "dingtalk",
            "Alert",
            "Body",
            [{"name": "Alice", "user_id": "alice.ding", "mobile": "13900000001"}],
        )

        self.assertIn('<at id="ou_alice"></at>', feishu["content"]["text"])
        self.assertEqual(wecom["text"]["mentioned_list"], ["alice.wx"])
        self.assertEqual(wecom["text"]["mentioned_mobile_list"], ["13800000001"])
        self.assertIn("@Alice", wecom["text"]["content"])
        self.assertEqual(dingtalk["at"]["atUserIds"], ["alice.ding"])
        self.assertEqual(dingtalk["at"]["atMobiles"], ["13900000001"])
        self.assertIn("@13900000001", dingtalk["markdown"]["text"])

    def test_dingtalk_request_url_adds_expected_signature_without_preserving_stale_values(self) -> None:
        settings = {"dingtalk_secret": "SECabc123"}
        webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=token-value&timestamp=old&sign=old"

        with patch.object(notifier.time, "time", return_value=1710000000.123):
            request_url = notifier._request_url("dingtalk", webhook_url, settings)

        parts = urlsplit(request_url)
        query = parse_qs(parts.query)
        expected_timestamp = "1710000000123"
        expected_sign = base64.b64encode(
            hmac.new(
                settings["dingtalk_secret"].encode("utf-8"),
                f"{expected_timestamp}\n{settings['dingtalk_secret']}".encode("utf-8"),
                digestmod=sha256,
            ).digest()
        ).decode("utf-8")

        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "oapi.dingtalk.com")
        self.assertEqual(parts.path, "/robot/send")
        self.assertEqual(query["access_token"], ["token-value"])
        self.assertEqual(query["timestamp"], [expected_timestamp])
        self.assertEqual(query["sign"], [expected_sign])

    def test_dingtalk_request_url_without_secret_keeps_url_unchanged(self) -> None:
        webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=token-value"

        request_url = notifier._request_url("dingtalk", webhook_url, {"dingtalk_secret": ""})

        self.assertEqual(request_url, webhook_url)

    def test_alert_preview_reports_channels_and_never_leaks_secret_material(self) -> None:
        token = "abc123SECRET456"
        secret = "SECabcdef1234567890SECRET"
        preview = notifier.build_test_alert_preview(
            {
                "notification_channels": [
                    {
                        "id": "ding",
                        "name": "钉钉群",
                        "type": "dingtalk",
                        "enabled": True,
                        "webhook_url": f"https://oapi.dingtalk.com/robot/send?access_token={token}",
                        "dingtalk_secret": secret,
                    }
                ]
            }
        )

        encoded_preview = json.dumps(preview, ensure_ascii=False)
        channel = preview["channels"][0]

        self.assertEqual(channel["type"], "dingtalk")
        self.assertEqual(channel["name"], "钉钉群")
        self.assertTrue(channel["signing_enabled"])
        self.assertEqual(channel["payload"]["msgtype"], "markdown")
        self.assertEqual(channel["payload"]["markdown"]["title"], "PulseGuard 测试告警")
        self.assertEqual(channel["target"]["origin"], "https://oapi.dingtalk.com")
        self.assertEqual(channel["target"]["path"], "/robot/send")
        self.assertEqual(channel["target"]["query_keys"], ["access_token", "timestamp", "sign"])
        self.assertNotIn(token, encoded_preview)
        self.assertNotIn(secret, encoded_preview)

    def test_alert_preview_resolves_variable_placeholders_without_leaking_secret_material(self) -> None:
        token = "token-secret-123"
        secret = "SECsecret456789"
        settings = variable_settings(token, secret)

        channels = notifier._notification_channels(settings, enabled_only=False, require_webhook=False)
        preview = notifier.build_test_alert_preview(settings)
        encoded_preview = json.dumps(preview, ensure_ascii=False)

        self.assertEqual(channels[0]["webhook_url"], f"https://oapi.dingtalk.com/robot/send?access_token={token}")
        self.assertEqual(channels[0]["dingtalk_secret"], secret)
        self.assertTrue(preview["channels"][0]["signing_enabled"])
        self.assertEqual(preview["channels"][0]["target"]["query_keys"], ["access_token", "timestamp", "sign"])
        self.assertNotIn(token, encoded_preview)
        self.assertNotIn(secret, encoded_preview)
        self.assertNotIn("${DING_TOKEN}", encoded_preview)
        self.assertNotIn("${DING_SECRET}", encoded_preview)

    def test_delivery_error_masks_variable_backed_webhook_and_dingtalk_secret_values(self) -> None:
        token = "token-secret-123"
        secret = "SECsecret456789"
        run = {"id": 47, "status": "failed", "started_at": "2026-06-05T10:00:00+08:00"}
        transition = {"current_status": "failing", "previous_status": "suspected_failing", "consecutive_failures": 2}
        settings = {
            **variable_settings(token, secret),
            "alerts_enabled": True,
            "alert_cooldown_minutes": 30,
            "recovery_notification": True,
        }

        async def fail_delivery(channel: dict[str, object], title: str, text: str) -> None:
            self.assertEqual(channel["webhook_url"], f"https://oapi.dingtalk.com/robot/send?access_token={token}")
            self.assertEqual(channel["dingtalk_secret"], secret)
            raise notifier.AlertDeliveryError(f"send failed {token} {secret}")

        with patch.object(notifier.storage, "get_settings", return_value=settings), patch.object(
            notifier, "send_webhook_alert", new=AsyncMock(side_effect=fail_delivery)
        ), patch.object(notifier.storage, "update_run_notification") as update_run_notification, patch.object(
            notifier.storage, "update_last_notified"
        ) as update_last_notified:
            self.run_async(notifier.maybe_notify({"id": 11, "type": "api", "name": "API"}, run, transition))

        update_last_notified.assert_not_called()
        update_run_notification.assert_called_once()
        self.assertEqual(update_run_notification.call_args.args[:2], (47, "failed"))
        error = update_run_notification.call_args.kwargs["error"]
        self.assertNotIn(token, error)
        self.assertNotIn(secret, error)
        self.assertIn("***", error)

    def test_dingtalk_robot_error_uses_errcode_and_errmsg(self) -> None:
        response = httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})

        error = notifier._robot_error("dingtalk", response)

        self.assertEqual(error, "钉钉返回 310000：sign not match")

    def test_robot_error_accepts_string_error_codes(self) -> None:
        response = httpx.Response(200, json={"errcode": "310000", "errmsg": "sign not match"})

        error = notifier._robot_error("dingtalk", response)

        self.assertEqual(error, "钉钉返回 310000：sign not match")

    def test_robot_error_reports_non_numeric_error_codes_without_crashing(self) -> None:
        response = httpx.Response(200, json={"errcode": "invalid", "errmsg": "bad response"})

        error = notifier._robot_error("dingtalk", response)

        self.assertEqual(error, "钉钉返回 invalid：bad response")

    def test_robot_error_treats_string_zero_as_success(self) -> None:
        response = httpx.Response(200, json={"errcode": "0", "errmsg": "ok"})

        error = notifier._robot_error("wecom", response)

        self.assertIsNone(error)

    def test_feishu_robot_error_uses_status_code_fallback(self) -> None:
        response = httpx.Response(200, json={"code": None, "StatusCode": 999, "StatusMessage": "token expired"})

        error = notifier._robot_error("feishu", response)

        self.assertEqual(error, "飞书返回 999：token expired")

    @staticmethod
    def run_async(awaitable):
        import asyncio

        return asyncio.run(awaitable)


def variable_settings(token: str, secret: str) -> dict[str, object]:
    return {
        "environment_variables": [
            {
                "id": "ding-token",
                "name": "DING_TOKEN",
                "value": token,
                "secret": True,
            },
            {"id": "ding-secret", "name": "DING_SECRET", "value": secret, "secret": True},
        ],
        "notification_channels": [
            {
                "id": "ding",
                "name": "Ding",
                "type": "dingtalk",
                "enabled": True,
                "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=${DING_TOKEN}",
                "dingtalk_secret": "${DING_SECRET}",
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
