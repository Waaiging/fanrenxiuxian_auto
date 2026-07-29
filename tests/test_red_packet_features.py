import asyncio
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import red_packet_features


class RedPacketFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.settings_patch = patch.object(
            red_packet_features,
            "RED_PACKET_SETTINGS_FILE",
            red_packet_features.Path(self.tempdir.name) / "red_packet_settings.json",
        )
        self.config_dir_patch = patch.object(
            red_packet_features,
            "CONFIG_DIR",
            red_packet_features.Path(self.tempdir.name),
        )
        self.settings_patch.start()
        self.config_dir_patch.start()

    def tearDown(self):
        self.config_dir_patch.stop()
        self.settings_patch.stop()
        self.tempdir.cleanup()

    def test_settings_require_an_account_when_enabled(self):
        with self.assertRaisesRegex(ValueError, "at least one account"):
            red_packet_features.save_red_packet_settings(
                enabled=True,
                accounts=[],
                minimum_amount="1",
            )

    def test_settings_filter_unknown_accounts_and_preserve_decimal(self):
        saved = red_packet_features.save_red_packet_settings(
            enabled=True,
            accounts=["xiaohao", "unknown", "main"],
            minimum_amount="1.2500",
            delay_seconds="2.500",
            schedule_enabled=True,
            schedule_start="22:30",
            schedule_end="06:15",
            updated_by="tester",
        )

        self.assertEqual(saved["accounts"], ["main", "xiaohao"])
        self.assertEqual(saved["minimum_amount"], "1.25")
        self.assertEqual(saved["delay_seconds"], "2.5")
        self.assertTrue(saved["schedule_enabled"])
        self.assertEqual(saved["schedule_start"], "22:30")
        self.assertEqual(saved["schedule_end"], "06:15")
        self.assertEqual(red_packet_features.load_red_packet_settings(), saved)

    def test_settings_reject_invalid_delay(self):
        with self.assertRaisesRegex(ValueError, "invalid delay seconds"):
            red_packet_features.save_red_packet_settings(
                enabled=True,
                accounts=["main"],
                minimum_amount="1",
                delay_seconds="300.1",
            )

    def test_settings_reject_invalid_schedule_time(self):
        with self.assertRaisesRegex(ValueError, "invalid schedule time"):
            red_packet_features.save_red_packet_settings(
                enabled=True,
                accounts=["main"],
                minimum_amount="1",
                schedule_enabled=True,
                schedule_start="24:00",
                schedule_end="08:00",
            )

    def test_schedule_supports_daytime_and_cross_midnight_ranges(self):
        daytime = {
            "schedule_enabled": True,
            "schedule_start": "09:00",
            "schedule_end": "18:00",
        }
        overnight = {
            "schedule_enabled": True,
            "schedule_start": "22:00",
            "schedule_end": "06:00",
        }
        self.assertTrue(
            red_packet_features.is_within_red_packet_schedule(
                daytime, now=datetime(2026, 7, 27, 9, 0)
            )
        )
        self.assertFalse(
            red_packet_features.is_within_red_packet_schedule(
                daytime, now=datetime(2026, 7, 27, 18, 0)
            )
        )
        self.assertTrue(
            red_packet_features.is_within_red_packet_schedule(
                overnight, now=datetime(2026, 7, 27, 23, 30)
            )
        )
        self.assertTrue(
            red_packet_features.is_within_red_packet_schedule(
                overnight, now=datetime(2026, 7, 27, 5, 59)
            )
        )
        self.assertFalse(
            red_packet_features.is_within_red_packet_schedule(
                overnight, now=datetime(2026, 7, 27, 12, 0)
            )
        )
        self.assertTrue(
            red_packet_features.is_within_red_packet_schedule(
                {**daytime, "schedule_start": "00:00", "schedule_end": "00:00"},
                now=datetime(2026, 7, 27, 12, 0),
            )
        )

    def test_extract_amount_from_labeled_and_currency_text(self):
        self.assertEqual(
            red_packet_features.extract_red_packet_amount("🧧 红包金额：1,234.50 USDT\n数量：20"),
            Decimal("1234.50"),
        )
        self.assertEqual(
            red_packet_features.extract_red_packet_amount("有人发了 8.8 USDT 红包"),
            Decimal("8.8"),
        )
        self.assertEqual(
            red_packet_features.extract_red_packet_amount(
                "总金额 / 类型:\n1100.00 LDC / 拼手气红包"
            ),
            Decimal("1100.00"),
        )

    def test_extract_amount_does_not_treat_packet_count_as_amount(self):
        self.assertIsNone(red_packet_features.extract_red_packet_amount("红包数量：50\n快来领取"))
        self.assertIsNone(red_packet_features.extract_red_packet_amount("红包数量：50 users"))

    def test_extract_amount_from_money_icon_and_full_width_digits(self):
        self.assertEqual(
            red_packet_features.extract_red_packet_amount("红包来啦\n💰：１２．５０ USDT\n数量：5"),
            Decimal("12.50"),
        )

    def test_extract_amount_from_currency_symbol_or_button(self):
        self.assertEqual(
            red_packet_features.extract_red_packet_amount("新红包", ["抢 ￥18.8"]),
            Decimal("18.8"),
        )
        self.assertEqual(
            red_packet_features.extract_red_packet_amount("新红包", ["抢 20 USDT"]),
            Decimal("20"),
        )

    def test_extract_amount_from_total_and_count_label(self):
        self.assertEqual(
            red_packet_features.extract_red_packet_amount("总金额 / 份数：30.5 / 10"),
            Decimal("30.5"),
        )

    def test_extract_claim_receipt_uses_personal_amount(self):
        receipt = red_packet_features.extract_claim_receipt(
            "🧧 恭喜 Waaiging 抢到 0.36 LDC！\n"
            "✅ 已自动分发到论坛账户\n"
            "（剩余 4 / 5 份，9.64 LDC）"
        )
        self.assertEqual(receipt["name"], "Waaiging")
        self.assertEqual(receipt["amount"], Decimal("0.36"))
        self.assertEqual(receipt["currency"], "LDC")

    def test_red_packet_button_accepts_short_and_decorated_labels(self):
        short = SimpleNamespace(text="抢")
        decorated = SimpleNamespace(text="🧧 抢红包")
        self.assertIs(red_packet_features.red_packet_button(SimpleNamespace(buttons=[[short]])), short)
        self.assertIs(
            red_packet_features.red_packet_button(SimpleNamespace(buttons=[[decorated]])),
            decorated,
        )

    def test_topic_id_prefers_reply_to_top_id(self):
        message = SimpleNamespace(
            id=99,
            reply_to=SimpleNamespace(reply_to_top_id=42, reply_to_msg_id=55, forum_topic=True),
        )
        self.assertEqual(red_packet_features.message_topic_id(message), 42)

    def test_topic_id_uses_forum_root_reply(self):
        message = SimpleNamespace(
            id=99,
            reply_to=SimpleNamespace(reply_to_top_id=None, reply_to_msg_id=42, forum_topic=True),
        )
        self.assertEqual(red_packet_features.message_topic_id(message), 42)

    def _message(self, button, amount="8.8"):
        return SimpleNamespace(
            id=100,
            reply_to=SimpleNamespace(reply_to_top_id=42, reply_to_msg_id=42, forum_topic=True),
            raw_text=f"🧧 红包金额：{amount} USDT",
            text=f"🧧 红包金额：{amount} USDT",
            buttons=[[button]],
        )

    def test_below_minimum_does_not_click(self):
        class FakeButton:
            text = "抢红包"
            url = ""
            button = type("KeyboardButtonCallback", (), {"data": b"claim"})()

            def __init__(self):
                self.clicks = 0

            async def click(self):
                self.clicks += 1

        button = FakeButton()
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        with patch.object(
            red_packet_features,
            "load_red_packet_settings",
            return_value={"enabled": True, "accounts": ["main"], "minimum_amount": "10"},
        ):
            asyncio.run(monitor.process_message(self._message(button), source="new"))

        self.assertEqual(button.clicks, 0)
        self.assertIn(100, monitor._handled_set)

    def test_matching_amount_clicks_callback_once(self):
        class FakeButton:
            text = "抢红包"
            url = ""
            button = type("KeyboardButtonCallback", (), {"data": b"claim"})()

            def __init__(self):
                self.clicks = 0

            async def click(self):
                self.clicks += 1
                return SimpleNamespace(message="领取成功")

        button = FakeButton()
        message = self._message(button, amount="10")
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        with patch.object(
            red_packet_features,
            "load_red_packet_settings",
            return_value={"enabled": True, "accounts": ["main"], "minimum_amount": "10"},
        ):
            asyncio.run(monitor.process_message(message, source="new"))
            asyncio.run(monitor.process_message(message, source="edited"))

        self.assertEqual(button.clicks, 1)
        status = red_packet_features.load_red_packet_status("main")
        self.assertEqual(status["last_action"], "clicked")
        self.assertEqual(status["last_result"], "领取成功")

    def test_short_claim_button_clicks(self):
        class FakeButton:
            text = "抢"
            url = ""
            button = type("KeyboardButtonCallback", (), {"data": b"claim-short"})()

            def __init__(self):
                self.clicks = 0

            async def click(self):
                self.clicks += 1

        button = FakeButton()
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        with patch.object(
            red_packet_features,
            "load_red_packet_settings",
            return_value={"enabled": True, "accounts": ["main"], "minimum_amount": "10"},
        ):
            asyncio.run(monitor.process_message(self._message(button, amount="12"), source="new"))

        self.assertEqual(button.clicks, 1)

    def test_matching_amount_waits_for_configured_delay(self):
        class FakeButton:
            text = "抢红包"
            url = ""
            button = type("KeyboardButtonCallback", (), {"data": b"claim-delayed"})()

            def __init__(self):
                self.clicks = 0

            async def click(self):
                self.clicks += 1

        button = FakeButton()
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        settings = {
            "enabled": True,
            "accounts": ["main"],
            "minimum_amount": "10",
            "delay_seconds": "2.5",
        }
        sleep_mock = AsyncMock()
        with (
            patch.object(red_packet_features, "load_red_packet_settings", return_value=settings),
            patch.object(red_packet_features.asyncio, "sleep", sleep_mock),
        ):
            asyncio.run(monitor.process_message(self._message(button, amount="12"), source="new"))

        sleep_mock.assert_awaited_once_with(2.5)
        self.assertEqual(button.clicks, 1)
        status = red_packet_features.load_red_packet_status("main")
        self.assertEqual(status["last_action"], "clicked")
        self.assertEqual(status["last_delay_seconds"], "2.5")

    def test_delay_cancels_when_switch_is_disabled(self):
        class FakeButton:
            text = "抢红包"
            url = ""
            button = type("KeyboardButtonCallback", (), {"data": b"claim-cancel"})()

            def __init__(self):
                self.clicks = 0

            async def click(self):
                self.clicks += 1

        button = FakeButton()
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        enabled = {
            "enabled": True,
            "accounts": ["main"],
            "minimum_amount": "10",
            "delay_seconds": "3",
        }
        disabled = {**enabled, "enabled": False}
        with (
            patch.object(
                red_packet_features,
                "load_red_packet_settings",
                side_effect=[enabled, disabled],
            ),
            patch.object(red_packet_features.asyncio, "sleep", new=AsyncMock()),
        ):
            asyncio.run(monitor.process_message(self._message(button, amount="12"), source="new"))

        self.assertEqual(button.clicks, 0)
        self.assertIn(100, monitor._handled_set)
        status = red_packet_features.load_red_packet_status("main")
        self.assertEqual(status["last_action"], "delay_cancelled")
        self.assertEqual(status["last_error"], "自动抢红包已关闭")

    def test_outside_schedule_does_not_click(self):
        button = SimpleNamespace(
            text="抢红包",
            url="",
            button=type("KeyboardButtonCallback", (), {"data": b"claim-scheduled"})(),
            click=AsyncMock(),
        )
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        settings = {
            "enabled": True,
            "accounts": ["main"],
            "minimum_amount": "10",
            "delay_seconds": "0",
            "schedule_enabled": True,
            "schedule_start": "09:00",
            "schedule_end": "10:00",
        }
        with (
            patch.object(red_packet_features, "load_red_packet_settings", return_value=settings),
            patch.object(
                red_packet_features,
                "is_within_red_packet_schedule",
                return_value=False,
            ),
        ):
            asyncio.run(monitor.process_message(self._message(button, amount="12"), source="new"))

        button.click.assert_not_awaited()
        self.assertIn(100, monitor._handled_set)
        status = red_packet_features.load_red_packet_status("main")
        self.assertEqual(status["last_action"], "outside_schedule")

    def test_matching_receipt_sends_personal_amount_notification(self):
        client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1)))
        monitor = red_packet_features.RedPacketMonitor(client, "main")
        monitor.topic_id = 42
        monitor.self_names = {"waaiging"}
        monitor._register_pending_claim(100, Decimal("10"))
        receipt_message = SimpleNamespace(
            id=101,
            sender_id=8547797815,
            reply_to=SimpleNamespace(
                reply_to_top_id=None,
                reply_to_msg_id=42,
                forum_topic=True,
            ),
            raw_text="🧧 恭喜 Waaiging 抢到 0.36 LDC！\n✅ 已自动分发到论坛账户",
            text="",
        )

        asyncio.run(monitor.process_receipt(receipt_message))

        client.send_message.assert_awaited_once()
        target, notification = client.send_message.await_args.args
        self.assertEqual(target, red_packet_features.RED_PACKET_NOTIFY_TARGET)
        self.assertIn("金额：0.36 LDC", notification)
        status = red_packet_features.load_red_packet_status("main")
        self.assertEqual(status["last_claimed_amount"], "0.36")
        self.assertEqual(status["last_claimed_currency"], "LDC")
        self.assertEqual(status["last_claim_message_id"], 100)

    def test_failed_notification_stays_pending_and_is_not_marked_sent(self):
        client = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("Too many requests")))
        monitor = red_packet_features.RedPacketMonitor(client, "xiaohao")
        monitor.topic_id = 42
        monitor.self_names = {"waaiging"}
        monitor._register_pending_claim(100, Decimal("10"))
        receipt_message = SimpleNamespace(
            id=101,
            sender_id=8547797815,
            reply_to=SimpleNamespace(
                reply_to_top_id=None,
                reply_to_msg_id=42,
                forum_topic=True,
            ),
            raw_text="🧧 恭喜 Waaiging 抢到 73.19 LDC！",
            text="",
        )

        with (
            patch.object(red_packet_features.asyncio, "sleep", new=AsyncMock()),
            patch.object(monitor, "_ensure_notification_retry_task") as retry_task,
        ):
            asyncio.run(monitor.process_receipt(receipt_message))

        self.assertEqual(client.send_message.await_count, 3)
        self.assertNotIn(101, monitor._notified_receipt_set)
        self.assertEqual(monitor._pending_notification_ids(), {101})
        retry_task.assert_called_once_with(
            initial_delay=red_packet_features.NOTIFICATION_BACKGROUND_RETRY_SECONDS,
        )
        status = red_packet_features.load_red_packet_status("xiaohao")
        self.assertEqual(status["pending_notifications"][0]["receipt_id"], 101)
        self.assertIn("Too many requests", status["last_notification_error"])

    def test_restricted_notification_uses_bot_api_before_limited_client(self):
        (red_packet_features.CONFIG_DIR / "config.json").write_text(
            '{"notify_bot_token":"test-token","notify_target":"12345"}',
            encoding="utf-8",
        )
        client = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("Too many requests")))
        monitor = red_packet_features.RedPacketMonitor(client, "xiaohao")
        record = {
            "receipt_id": 101,
            "claim_message_id": 100,
            "amount": "73.19",
            "currency": "LDC",
            "created_at": "2026-07-30 01:33:55",
        }
        monitor._enqueue_notification(record)

        with patch.object(red_packet_features, "_send_notification_bot_sync") as bot_send:
            delivered = asyncio.run(monitor._deliver_notification(record, retry_delays=(0,)))

        self.assertTrue(delivered)
        bot_send.assert_called_once()
        self.assertEqual(bot_send.call_args.args[1], 12345)
        client.send_message.assert_not_awaited()
        self.assertIn(101, monitor._notified_receipt_set)
        self.assertEqual(monitor._pending_notifications, [])

    def test_failed_notification_is_recovered_after_restart(self):
        red_packet_features._atomic_write_json(
            red_packet_features._status_path("xiaohao"),
            {
                "account": "xiaohao",
                "notified_receipt_ids": [101],
                "last_claimed_amount": "73.19",
                "last_claimed_currency": "LDC",
                "last_claim_receipt_id": 101,
                "last_claim_message_id": 100,
                "last_notification_error": "Too many requests",
                "updated_at": "2026-07-29 20:31:08",
            },
        )
        client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1)))
        monitor = red_packet_features.RedPacketMonitor(client, "xiaohao")

        self.assertNotIn(101, monitor._notified_receipt_set)
        self.assertEqual(monitor._pending_notification_ids(), {101})

        delivered = asyncio.run(
            monitor._deliver_notification(
                dict(monitor._pending_notifications[0]),
                retry_delays=(0,),
            )
        )

        self.assertTrue(delivered)
        self.assertIn(101, monitor._notified_receipt_set)
        self.assertEqual(monitor._pending_notifications, [])
        status = red_packet_features.load_red_packet_status("xiaohao")
        self.assertEqual(status["last_notification_error"], "")
        self.assertEqual(status["pending_notifications"], [])

    def test_rejected_callback_does_not_leave_pending_notification(self):
        class FakeButton:
            text = "抢红包"
            url = ""
            button = type("KeyboardButtonCallback", (), {"data": b"claim-finished"})()

            async def click(self):
                return SimpleNamespace(message="红包已抢完")

        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        with patch.object(
            red_packet_features,
            "load_red_packet_settings",
            return_value={"enabled": True, "accounts": ["main"], "minimum_amount": "10"},
        ):
            asyncio.run(
                monitor.process_message(self._message(FakeButton(), amount="12"), source="new")
            )

        self.assertEqual(monitor._pending_claims, [])

    def test_receipt_without_pending_auto_claim_does_not_notify(self):
        client = SimpleNamespace(send_message=AsyncMock())
        monitor = red_packet_features.RedPacketMonitor(client, "main")
        monitor.topic_id = 42
        monitor.self_names = {"waaiging"}
        receipt_message = SimpleNamespace(
            id=101,
            sender_id=8547797815,
            reply_to=SimpleNamespace(
                reply_to_top_id=None,
                reply_to_msg_id=42,
                forum_topic=True,
            ),
            raw_text="🧧 恭喜 Waaiging 抢到 0.36 LDC！",
            text="",
        )

        asyncio.run(monitor.process_receipt(receipt_message))

        client.send_message.assert_not_awaited()

    def test_unrecognized_receipt_name_keeps_pending_and_skips_notify(self):
        client = SimpleNamespace(send_message=AsyncMock())
        monitor = red_packet_features.RedPacketMonitor(client, "main")
        monitor.topic_id = 42
        monitor.self_names = {"waaiging"}
        monitor._register_pending_claim(100, Decimal("10"))
        receipt_message = SimpleNamespace(
            id=101,
            sender_id=8547797815,
            reply_to=SimpleNamespace(
                reply_to_top_id=None,
                reply_to_msg_id=42,
                forum_topic=True,
            ),
            raw_text="🧧 恭喜 SomeoneElse 抢到 0.36 LDC！",
            text="",
        )

        asyncio.run(monitor.process_receipt(receipt_message))

        client.send_message.assert_not_awaited()
        self.assertEqual(len(monitor._pending_claims), 1)

    def test_settings_claim_names_round_trip(self):
        data = red_packet_features.normalize_red_packet_settings(
            {"claim_names": [" Waaiging ", "", 123]}
        )
        self.assertEqual(data["claim_names"], ["Waaiging", "123"])

    def test_forged_receipt_from_member_does_not_notify(self):
        client = SimpleNamespace(send_message=AsyncMock())
        monitor = red_packet_features.RedPacketMonitor(client, "main")
        monitor.topic_id = 42
        monitor.self_names = {"waaiging"}
        monitor._register_pending_claim(100, Decimal("10"))
        receipt_message = SimpleNamespace(
            id=101,
            sender_id=123456,
            reply_to=SimpleNamespace(
                reply_to_top_id=None,
                reply_to_msg_id=42,
                forum_topic=True,
            ),
            raw_text="🧧 恭喜 Waaiging 抢到 999 LDC！",
            text="",
        )

        asyncio.run(monitor.process_receipt(receipt_message))

        client.send_message.assert_not_awaited()
        self.assertEqual(len(monitor._pending_claims), 1)

    def test_unknown_amount_status_keeps_message_diagnostics(self):
        button = SimpleNamespace(
            text="抢红包",
            url="",
            button=type("KeyboardButtonCallback", (), {"data": b"claim:opaque"})(),
        )
        monitor = red_packet_features.RedPacketMonitor(None, "main")
        monitor.topic_id = 42
        message = self._message(button)
        message.raw_text = "红包数量：5"
        message.text = message.raw_text
        with patch.object(
            red_packet_features,
            "load_red_packet_settings",
            return_value={"enabled": True, "accounts": ["main"], "minimum_amount": "10"},
        ):
            asyncio.run(monitor.process_message(message, source="new"))

        status = red_packet_features.load_red_packet_status("main")
        self.assertEqual(status["last_action"], "amount_unknown")
        self.assertEqual(status["last_message_text"], "红包数量：5")
        self.assertEqual(status["last_buttons"][0]["text"], "抢红包")
        self.assertEqual(status["last_buttons"][0]["data_text"], "claim:opaque")


if __name__ == "__main__":
    unittest.main()
