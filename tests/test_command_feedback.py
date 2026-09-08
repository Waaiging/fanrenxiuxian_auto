import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import command_feedback as feedback


class FeedbackRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = SimpleNamespace(id=42, sender_id=999, chat_id=100)
        self.actor = SimpleNamespace(
            client=SimpleNamespace(send_message=AsyncMock(return_value=self.sent)),
            cmd_lock=asyncio.Lock(), target_chat_id=100, topic_id=None,
            account_key="main", current_identity="main", state={},
            feedback_events={}, feedback_commands={}, feedback_sent_ts={},
            last_feedback_text={}, last_feedback_msg={},
        )
        for name in ("record_command_sent", "record_recent_profile_command", "record_bot_response",
                     "record_cultivation_profile_from_text", "record_cultivation_delta_from_text",
                     "_record_timed_response_guard", "_record_repeated_response_guard"):
            self._patch(name, MagicMock())
        self._patch("command_send_allowed", MagicMock(return_value=True))
        self._patch("wait_for_bot_activity_before_send", AsyncMock(return_value=True))
        self._patch("log_incoming_message", AsyncMock())
        self._patch("NO_RESPONSE_TIMEOUT_SECONDS", 0.1)
        self._patch("NO_RESPONSE_RETRY_COUNT", 0)
        sleep_patch = patch.object(feedback.asyncio, "sleep", AsyncMock())
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _patch(self, name, value):
        value_patch = patch.object(feedback, name, value)
        value_patch.start()
        self.addCleanup(value_patch.stop)

    def _reply(self):
        self.assertEqual(self.actor.feedback_chat_ids[42], 100)
        self.assertEqual(self.actor.feedback_commands[42], ".问道")
        self.actor.last_feedback_text[42] = "reply fixture"
        self.actor.last_feedback_msg[42] = SimpleNamespace(id=43)
        self.actor.feedback_events[42].set()

    async def _send(self):
        return await feedback.send_and_wait_feedback_common(
            self.actor, MagicMock(), ".问道", delete_after=False,
        )

    def _assert_cleaned(self):
        for name in ("feedback_events", "feedback_commands", "feedback_sent_ts", "feedback_senders",
                     "feedback_chat_ids", "feedback_identities", "last_feedback_text", "last_feedback_msg"):
            self.assertEqual(getattr(self.actor, name), {}, name)

    async def test_fast_reply_during_recovery_notification_is_not_lost(self):
        async def notify(*args, **kwargs):
            self._reply()
        self._patch("record_telegram_send_success", notify)
        self.assertEqual(await self._send(), "reply fixture")
        self.actor.client.send_message.assert_awaited_once()
        self._assert_cleaned()

    async def test_recovery_logging_failure_does_not_discard_reply(self):
        async def notify(*args, **kwargs):
            self._reply()
            raise OSError("state save failed")
        self._patch("record_telegram_send_success", notify)
        self.assertEqual(await self._send(), "reply fixture")
        self._assert_cleaned()

    async def test_cancellation_during_recovery_cleans_waiter(self):
        self._patch("record_telegram_send_success", AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await self._send()
        self._assert_cleaned()


if __name__ == "__main__":
    unittest.main()
