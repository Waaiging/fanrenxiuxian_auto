import asyncio
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

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
            updated_by="tester",
        )

        self.assertEqual(saved["accounts"], ["main", "xiaohao"])
        self.assertEqual(saved["minimum_amount"], "1.25")
        self.assertEqual(red_packet_features.load_red_packet_settings(), saved)

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


if __name__ == "__main__":
    unittest.main()
