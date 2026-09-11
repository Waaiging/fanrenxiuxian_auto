"""Mentions/replies are captured without claiming unrelated gameplay traffic."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, PeerChannel

import log_utils as lu
import telegram_message_logging as capture


class TelegramMessageLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = self.directory / "messages.sqlite3"
        self.enterContext(patch.object(lu, "MESSAGE_EVENTS_DB_FILE", str(self.db)))
        self.enterContext(patch.object(lu, "BOT_ACTIVITY_SHARED_FILE", str(self.directory / "health.json")))
        self.enterContext(patch.object(lu, "_MESSAGE_EVENTS_SCHEMA_READY", False))
        self.actor = self.new_actor()
        self.sender = SimpleNamespace(id=700, username="visitor", first_name="访客", bot=False)

    def new_actor(self, account="main"):
        return SimpleNamespace(
            account_key=account, my_info=SimpleNamespace(id=42, username="owner", first_name=""),
            target_chat_id=-100123456, target_chat_ids=[-100123456, -100654321], topic_id=10,
            _avatar_chat_ids={"-100999999": "化身"}, avatar_usernames={"avatar_user": "化身"},
            identity_usernames={"主魂": ["owner"]}, avatars=["化身"],
            current_identity="主魂", mc={}, state={}, watch_bot="trusted_game_bot",
        )

    def message(self, text="@owner 你好", *, msg_id=101, reply_id=None, chat_id=-100123456, sender_id=700, **extra):
        extra.setdefault("date", datetime.now(timezone.utc))
        return SimpleNamespace(
            id=msg_id, text=text, raw_text=text, chat_id=chat_id, sender_id=sender_id,
            reply_to=(SimpleNamespace(reply_to_msg_id=reply_id, forum_topic=False) if reply_id else None),
            out=sender_id == 42, entities=[], mentioned=False, **extra,
        )

    async def capture(self, msg, *, actor=None, sender=None, edited=False):
        return await capture.log_addressed_message_if_needed(
            actor or self.actor, msg, sender=sender or self.sender, edited=edited,
        )

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("SELECT * FROM message_events WHERE attention!='' ORDER BY id")]

    async def test_all_sender_types_are_distinct_from_game_bot_allowlist(self):
        with self.assertLogs("SimpleNamespace", level="INFO") as logs:
            for index, bot in enumerate((False, True, None)):
                sender = SimpleNamespace(username="visitor", bot=bot)
                self.assertTrue(await self.capture(self.message(msg_id=200 + index), sender=sender))
        rows = self.rows()
        self.assertEqual([row["sender_is_bot"] for row in rows], [0, 1, None])
        self.assertEqual([row["is_game_bot"] for row in rows], [0, 0, 0])
        for kind in ("human", "bot", "unknown"):
            self.assertIn(f"sender={kind}", "\n".join(logs.output))

    async def test_exact_case_insensitive_mentions_and_avatar_entities(self):
        msg = self.message("@OwNeR 和 @Avatar_User")
        self.assertEqual(capture.explicit_mention_identities(self.actor, msg, msg.text), ["主魂", "化身"])
        for text in ("@owner_other", "@avatar_user2", "mail@owner", "普通文字"):
            self.assertFalse(await self.capture(self.message(text)))
        msg = self.message("无用户名的提及")
        msg.entities = [MessageEntityMentionName(0, 3, 42),
                        MessageEntityTextUrl(3, 3, "tg://user?id=-100999999")]
        self.assertTrue(await self.capture(msg))
        self.assertEqual(json.loads(self.rows()[0]["attention"])["mentions"], ["主魂", "化身"])

    async def test_reply_to_ordinary_own_message_survives_restart_without_fetch(self):
        parent = self.message("普通聊天", msg_id=100, sender_id=42)
        lu.record_message_event(self.actor, parent)
        msg = self.message("回复你的话", reply_id=100, get_reply_message=AsyncMock())
        self.assertTrue(await self.capture(msg, actor=self.new_actor()))
        msg.get_reply_message.assert_not_awaited()
        row = self.rows()[0]
        self.assertEqual(json.loads(row["attention"])["relations"], ["reply"])
        self.assertEqual(row["command"], "")

    async def test_fetch_missing_parent_and_media_reply_to_avatar(self):
        parent = self.message("化身聊天", msg_id=98, sender_id=-100999999)
        msg = self.message("", reply_id=98, photo=True, get_reply_message=AsyncMock(return_value=parent))
        with self.assertLogs("SimpleNamespace", level="INFO") as logs:
            self.assertTrue(await self.capture(msg))
        row = self.rows()[0]
        self.assertEqual(row["identity"], "化身")
        self.assertIn("[照片]", row["text"])
        self.assertIn("[Reply to: 化身 #98]", "\n".join(logs.output))
        self.assertEqual(await capture.reply_identity(self.actor, msg), "化身")
        msg.get_reply_message.assert_awaited_once()

    async def test_cross_chat_and_account_id_collisions_do_not_claim_foreign_reply(self):
        lu.record_message_event(self.actor, self.message("我的消息", msg_id=100, sender_id=42))
        for actor, msg in (
            (self.actor, self.message("其他群的回复", chat_id=-100654321, reply_id=100)),
            (self.new_actor("sub"), self.message("其他账号的回复", reply_id=100)),
        ):
            self.assertFalse(await self.capture(msg, actor=actor))
        foreign = self.message("他人原文", msg_id=100, chat_id=-100654321)
        lu.record_message_event(self.actor, foreign)
        self.assertFalse(await self.capture(self.message("他人的回复", reply_id=100, chat_id=-100654321)))

    async def test_external_quote_uses_explicit_parent_chat(self):
        lu.record_message_event(self.actor, self.message("我的另一群消息", msg_id=100, sender_id=42, chat_id=-1000000654321))
        msg = self.message("引用回复", reply_id=100)
        msg.reply_to.reply_to_peer_id = PeerChannel(654321)
        self.assertTrue(await self.capture(msg))
        self.assertEqual(json.loads(self.rows()[0]["attention"])["relations"], ["reply"])

    async def test_topic_roots_and_mentioned_flag_alone_are_not_explicit_mentions(self):
        for reply_id, forum_topic, top_id in ((10, False, None), (99, True, None), (99, True, 99)):
            msg = self.message("话题里的一般消息", reply_id=reply_id, get_reply_message=AsyncMock())
            msg.reply_to.forum_topic = forum_topic
            msg.reply_to.reply_to_top_id = top_id
            msg.mentioned = True
            self.assertFalse(await self.capture(msg))
            msg.get_reply_message.assert_not_awaited()

    async def test_known_foreign_parent_never_fetches_and_cannot_hide_explicit_mention(self):
        lu.record_message_event(self.actor, self.message("别人的原文", msg_id=100))
        msg = self.message("@owner 看看", reply_id=100, get_reply_message=AsyncMock())
        self.assertTrue(await self.capture(msg))
        self.assertEqual(json.loads(self.rows()[0]["attention"])["relations"], ["mention"])
        msg.get_reply_message.assert_not_awaited()

    async def test_unavailable_parent_preserves_mentions_without_fabricating_reply(self):
        for failure in (TimeoutError(), ValueError("missing parent")):
            msg = self.message(reply_id=100, msg_id=101 + len(self.rows()) if self.db.exists() else 101,
                               get_reply_message=AsyncMock(side_effect=failure))
            self.assertTrue(await self.capture(msg))
        self.assertTrue(all(json.loads(row["attention"])["relations"] == ["mention"] for row in self.rows()))

    async def test_dedup_repeated_events_then_record_changed_version_after_restart(self):
        msg = self.message()
        with self.assertLogs("SimpleNamespace", level="INFO") as logs:
            self.assertTrue(await self.capture(msg))
            self.assertTrue(await self.capture(msg))
            lu.log_mention_if_needed(self.actor, msg, sender=self.sender)
            restarted = self.new_actor()
            self.assertTrue(await self.capture(msg, actor=restarted))
            lu.log_mention_if_needed(restarted, msg, sender=self.sender)
            msg.text = "改成没有 @ 的文本"
            self.assertTrue(await self.capture(msg, actor=restarted, edited=True))
            self.assertTrue(await self.capture(msg, actor=restarted, edited=True))
        self.assertEqual(len(logs.output), 2)
        rows = self.rows()
        self.assertEqual([row["event_kind"] for row in rows], ["new", "edited"])
        self.assertIn("改成没有 @", rows[-1]["text"])

    async def test_mention_plus_reply_is_one_record_and_command_reply_is_not_printed_twice(self):
        parent = self.message(".探寻裂缝", msg_id=100, sender_id=42)
        lu.record_command_sent(self.actor, parent, parent.text, identity="主魂", source="auto")
        msg = self.message("@owner 探索成功", reply_id=100)
        sender = SimpleNamespace(username="trusted_game_bot", bot=True)
        with self.assertLogs("SimpleNamespace", level="INFO") as logs:
            self.assertTrue(await self.capture(msg, sender=sender))
            lu.record_message_event(self.actor, msg, sender=sender)
            await lu.log_incoming_message(self.actor, ".探寻裂缝", msg.text, msg=msg, sender=sender)
        incoming = [text for text in logs.output if "IN [" in text]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(json.loads(self.rows()[0]["attention"])["relations"], ["mention", "reply"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_events WHERE msg_id=101").fetchone()[0], 1)

    async def test_human_reply_does_not_acknowledge_pending_command_or_update_health(self):
        parent = self.message(".探寻裂缝", msg_id=100, sender_id=42)
        lu.record_command_sent(self.actor, parent, parent.text, identity="主魂", source="auto")
        health = self.directory / "health.json"
        health_before = health.read_bytes() if health.exists() else None
        self.assertTrue(await self.capture(self.message("@owner 修为 +999，.探寻裂缝", reply_id=100)))
        self.assertEqual(health.read_bytes() if health.exists() else None, health_before)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertIsNone(conn.execute("SELECT response_msg_id FROM command_ledger").fetchone()[0])

    async def test_own_messages_are_not_incoming_attention(self):
        for sender_id in (42, -100999999):
            self.assertFalse(await self.capture(self.message(sender_id=sender_id)))

    async def test_restricted_listener_records_ordinary_parent_new_message_and_edit(self):
        self.actor.client = Mock()
        registrations = capture.install_addressed_message_monitor(self.actor)
        self.assertEqual(self.actor.client.add_event_handler.call_count, 2)
        new, edit = [callback for callback, _ in registrations]
        parent = self.message("先说一句", msg_id=100, sender_id=42)
        await new(SimpleNamespace(message=parent, get_sender=AsyncMock(return_value=self.actor.my_info)))
        msg = self.message("普通回复", reply_id=100)
        event = SimpleNamespace(message=msg, get_sender=AsyncMock(return_value=self.sender))
        await new(event)
        msg.text = "编辑过的普通回复"
        await edit(event)
        self.assertEqual([row["event_kind"] for row in self.rows()], ["new", "edited"])

    async def test_all_full_workers_capture_before_other_handlers_return(self):
        import intelligent_cultivator as main
        import sub_cultivator as sub
        import cultivator_xiaohao as xiaohao
        from cultivator_waaiging import WaaigingCultivator
        for index, (cls, module) in enumerate(((main.Cultivator, main), (sub.SubCultivator, sub),
                                             (xiaohao.CultivatorXiaoHao, xiaohao), (WaaigingCultivator, main))):
            actor = cls.__new__(cls)
            actor.__dict__.update(self.new_actor().__dict__)
            actor.account_key = ("main", "sub", "xiaohao", "waaiging")[index]
            actor.maybe_handle_fishing_control_message = AsyncMock(return_value=False)
            event = SimpleNamespace(message=self.message(msg_id=300 + index), get_sender=AsyncMock(return_value=self.sender))
            with patch.object(module, "maybe_handle_xuangu_quiz", AsyncMock(return_value=True)), \
                    patch.object(module, "record_star_gazing_event"), patch.object(module, "record_game_bot_activity"):
                await actor.handle_game_response(event)
        self.assertEqual({row["account"] for row in self.rows()}, {"main", "sub", "xiaohao", "waaiging"})

    async def test_existing_schema_migrates_without_reclassifying_history(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""CREATE TABLE message_events (
                id INTEGER PRIMARY KEY, account TEXT, event_kind TEXT, direction TEXT,
                chat_id INTEGER, msg_id INTEGER, reply_to_msg_id INTEGER, sender_id INTEGER,
                sender_username TEXT, sender_name TEXT, is_out INTEGER DEFAULT 0, is_game_bot INTEGER DEFAULT 0,
                identity TEXT, command TEXT, text TEXT, text_hash TEXT, created_at TEXT,
                UNIQUE(account, event_kind, chat_id, msg_id, text_hash))""")
            conn.execute("INSERT INTO message_events(account,text,is_game_bot) VALUES('main','history',1)")
        self.assertTrue(await self.capture(self.message()))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT text,is_game_bot,sender_is_bot,attention FROM message_events WHERE id=1").fetchone(),
                             ("history", 1, None, ""))


if __name__ == "__main__":
    unittest.main()
