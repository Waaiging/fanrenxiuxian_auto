import asyncio
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import log_utils


class EditedMessageLoggingTests(unittest.TestCase):
    def setUp(self):
        self.old_db_file = log_utils.MESSAGE_EVENTS_DB_FILE
        self.temp_dir = tempfile.TemporaryDirectory()
        log_utils.MESSAGE_EVENTS_DB_FILE = os.path.join(
            self.temp_dir.name, "message_events.sqlite3"
        )
        log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def tearDown(self):
        log_utils.MESSAGE_EVENTS_DB_FILE = self.old_db_file
        log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
        self.temp_dir.cleanup()

    @staticmethod
    def actor():
        return SimpleNamespace(
            account_key="main",
            state_file="state_main.json",
            target_chat_id=-100123456,
            current_identity="主魂",
        )

    @staticmethod
    def message(msg_id, text, *, reply_to_msg_id=None, out=False):
        reply_to = (
            SimpleNamespace(reply_to_msg_id=reply_to_msg_id)
            if reply_to_msg_id is not None
            else None
        )
        return SimpleNamespace(
            id=msg_id,
            text=text,
            raw_text=text,
            chat_id=-100123456,
            sender_id=90001,
            reply_to=reply_to,
            out=out,
        )

    def test_persisted_auto_command_keeps_edited_reply_attribution(self):
        actor = self.actor()
        command = self.message(100, ".洞府", out=True)
        self.assertTrue(
            log_utils.record_command_sent(
                actor, command, ".洞府", identity="缘生子", source="auto"
            )
        )

        restarted_actor = self.actor()
        reply = self.message(101, "最终洞府结算", reply_to_msg_id=100)

        self.assertTrue(log_utils.is_reply_to_tracked_command(restarted_actor, reply))
        self.assertEqual(log_utils.tracked_command_text_for_reply(restarted_actor, reply), ".洞府")
        self.assertEqual(log_utils.tracked_command_identity_for_reply(restarted_actor, reply), "缘生子")
        self.assertTrue(
            log_utils.is_relevant_game_bot_edited_message(
                restarted_actor, reply, reply.text
            )
        )

    def test_edited_bot_reply_is_written_after_pending_memory_is_gone(self):
        actor = self.actor()
        command = self.message(200, ".洞府", out=True)
        log_utils.record_command_sent(actor, command, ".洞府", identity="主魂", source="auto")
        reply = self.message(201, "最终洞府结算", reply_to_msg_id=200)
        sender = SimpleNamespace(username="hantianzun_test_bot", first_name="韩天尊")

        class Event:
            message = reply

            async def get_sender(self):
                return sender

        with patch.object(log_utils, "is_game_bot_sender", return_value=True), patch.object(
            log_utils, "managed_mention_identities", return_value=[]
        ), patch.object(log_utils, "record_game_bot_activity"):
            with self.assertLogs("SimpleNamespace", level="INFO") as captured:
                self.assertTrue(
                    asyncio.run(log_utils.log_edited_message_if_needed(actor, Event()))
                )

        self.assertIn(".洞府 edited 201", "\n".join(captured.output))
        conn = sqlite3.connect(log_utils.MESSAGE_EVENTS_DB_FILE)
        try:
            row = conn.execute(
                """
                SELECT direction, identity, command, text
                FROM message_events
                WHERE account='main' AND event_kind='edited' AND msg_id=201
                """
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ("bot_edited", "主魂", ".洞府", "最终洞府结算"))

    def test_logged_initial_version_keeps_later_edit_relevant(self):
        actor = self.actor()
        reply = self.message(301, "最终结算", reply_to_msg_id=999)
        actor._logged_incoming_message_ids = {
            301: {"ts": 1.0, "text": "处理中..."}
        }

        self.assertTrue(
            log_utils.is_relevant_game_bot_edited_message(actor, reply, reply.text)
        )

    def test_other_users_untracked_edited_reply_stays_irrelevant(self):
        actor = self.actor()
        reply = self.message(401, "别人的最终结算", reply_to_msg_id=998)

        self.assertFalse(
            log_utils.is_relevant_game_bot_edited_message(actor, reply, reply.text)
        )


if __name__ == "__main__":
    unittest.main()
