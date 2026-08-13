import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from log_utils import (
    actor_message_target,
    is_edited_message_for_current_account,
    match_pending_feedback_by_id,
    remember_incoming_message_context,
    resolve_target_chat_ids,
    telegram_event_message_context,
    tracked_command_identity_for_reply,
    tracked_command_text_for_reply,
    incoming_message_context_for_msg,
)


class _Entity:
    def __init__(self, entity_id):
        self.id = entity_id
        self.broadcast = False
        self.megagroup = True
        self.forum = True


class DualChatRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_target_chat_ids_deduplicates_primary_and_aliases(self):
        client = SimpleNamespace(get_entity=AsyncMock(return_value=_Entity(22)))
        result = await resolve_target_chat_ids(client, 11, [11, "@old"])
        self.assertEqual(result, [11, 22])
        self.assertEqual(client.get_entity.await_count, 1)

    async def test_event_context_routes_to_source_chat_and_topic(self):
        actor = SimpleNamespace(target_chat_id=100, topic_id=7)
        source = SimpleNamespace(
            chat_id=-100200,
            reply_to=SimpleNamespace(reply_to_top_id=55),
        )
        with telegram_event_message_context(source):
            self.assertEqual(actor_message_target(actor), (-100200, 55))
            child = asyncio.create_task(asyncio.sleep(0, result=actor_message_target(actor)))
            self.assertEqual(await child, (-100200, 55))
        self.assertEqual(actor_message_target(actor), (100, 7))

    async def test_pending_feedback_cannot_cross_chat_with_same_message_id(self):
        pending = asyncio.Event()
        actor = SimpleNamespace(
            feedback_events={42: pending},
            feedback_commands={42: ".查看闭关"},
            feedback_identities={42: "主魂"},
            feedback_senders={42: 999},
            feedback_chat_ids={42: 100},
            last_feedback_text={},
            last_feedback_msg={},
            target_chat_id=100,
            current_identity="主魂",
            account_key="main",
        )
        sender = SimpleNamespace(id=123, username="fanrenxiuxian_bot", bot=True)
        wrong_chat = SimpleNamespace(id=43, chat_id=200, sender_id=123)
        self.assertFalse(
            match_pending_feedback_by_id(
                actor,
                42,
                wrong_chat,
                "当前未在闭关",
                sender=sender,
            )
        )
        self.assertFalse(pending.is_set())

    async def test_tracked_command_lookup_cannot_cross_chat(self):
        actor = SimpleNamespace(
            feedback_commands={42: ".查看闭关"},
            feedback_identities={42: "缘生子"},
            feedback_chat_ids={42: 100},
            target_chat_id=100,
        )
        wrong_chat = SimpleNamespace(
            id=50,
            chat_id=200,
            reply_to_msg_id=42,
            reply_to=None,
        )
        self.assertEqual(tracked_command_text_for_reply(actor, wrong_chat), "")
        self.assertEqual(tracked_command_identity_for_reply(actor, wrong_chat), "")

    async def test_incoming_context_and_edited_send_cache_are_chat_scoped(self):
        actor = SimpleNamespace(
            target_chat_id=100,
            _script_sent_message_ids={42},
            _script_sent_message_keys={(100, 42)},
        )
        first = SimpleNamespace(id=50, chat_id=100, text="first")
        second = SimpleNamespace(id=50, chat_id=200, text="second")
        remember_incoming_message_context(actor, first, command=".状态", identity="主魂")
        remember_incoming_message_context(actor, second, command=".我的灵根", identity="缘生子")
        self.assertEqual(incoming_message_context_for_msg(actor, first)["command"], ".状态")
        self.assertEqual(incoming_message_context_for_msg(actor, second)["command"], ".我的灵根")

        wrong_reply = SimpleNamespace(
            id=51,
            chat_id=200,
            reply_to_msg_id=42,
            reply_to=None,
        )
        self.assertFalse(is_edited_message_for_current_account(actor, wrong_reply, ""))


if __name__ == "__main__":
    unittest.main()
