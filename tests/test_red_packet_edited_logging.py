import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from red_packet_features import RED_PACKET_ANCHOR_MESSAGE_ID, RedPacketMonitor


class RedPacketEditedLoggingTests(unittest.TestCase):
    @staticmethod
    def message(msg_id=501, text="编辑后的 Bot 回复"):
        return SimpleNamespace(
            id=msg_id,
            text=text,
            raw_text=text,
            sender_id=7900199668,
            reply_to=SimpleNamespace(
                reply_to_top_id=RED_PACKET_ANCHOR_MESSAGE_ID,
                reply_to_msg_id=RED_PACKET_ANCHOR_MESSAGE_ID,
                forum_topic=True,
            ),
            buttons=None,
        )

    @staticmethod
    def event(message):
        sender = SimpleNamespace(
            id=7900199668,
            username="hantianzz_bot",
            bot=True,
        )

        class Event:
            async def get_sender(self):
                return sender

        event = Event()
        event.message = message
        return event

    def monitor(self):
        logger = logging.getLogger("test.red_packet.edited")
        with patch("red_packet_features.load_red_packet_status", return_value={}):
            monitor = RedPacketMonitor(SimpleNamespace(), "main", logger=logger)
        monitor.topic_id = RED_PACKET_ANCHOR_MESSAGE_ID
        monitor.process_receipt = AsyncMock()
        monitor.process_message = AsyncMock()
        return monitor

    def test_edited_bot_reply_is_logged_and_reprocessed_as_receipt(self):
        monitor = self.monitor()
        message = self.message()

        with self.assertLogs("test.red_packet.edited", level="INFO") as captured:
            self.assertTrue(
                asyncio.run(monitor.process_edited_message(self.event(message)))
            )

        self.assertIn("Red-packet bot edited message 501", "\n".join(captured.output))
        self.assertIn("编辑后的 Bot 回复", "\n".join(captured.output))
        monitor.process_receipt.assert_awaited_once_with(message)
        monitor.process_message.assert_awaited_once_with(message, source="edited")

    def test_duplicate_edited_version_is_logged_once(self):
        monitor = self.monitor()
        message = self.message()
        event = self.event(message)

        with self.assertLogs("test.red_packet.edited", level="INFO") as captured:
            self.assertTrue(asyncio.run(monitor.process_edited_message(event)))
            self.assertFalse(asyncio.run(monitor.process_edited_message(event)))

        matching = [
            line for line in captured.output if "Red-packet bot edited message 501" in line
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(monitor.process_receipt.await_count, 2)
        self.assertEqual(monitor.process_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
