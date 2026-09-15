import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.types import (
    InputPeerChannel, KeyboardButtonCallback, KeyboardButtonRow, KeyboardButtonUrl, Message,
    MessageReplyHeader, PeerChannel, PeerUser, ReplyInlineMarkup,
)

import automation_settings as settings
import command_feedback as feedback
import xuangu_quiz_features as quiz


STEM = "玄骨妄图炼化乾蓝冰焰，是想将其修成什么魔焰？"
OPTIONS = {"A": "紫罗极火", "B": "碧焰天火", "C": "修罗圣火", "D": "六极真魔火"}


def question_message(target="my_player", message_id=400, chat_id=quiz.JA_CHAT, topic=quiz.JA_TOPIC,
                     stem=STEM, options=None, age=0, sender_id=quiz.QUESTION_BOT_ID, kind="玄骨考校"):
    options = OPTIONS if options is None else options
    introduction = {
        "玄骨考校": f"神念直入脑海，一个苍老的声音向 @{target} 提问：",
        "玄骨窥鼎": f"那道魔念绕着你识海中的虚天鼎缓缓打转，向 @{target} 阴恻恻地发问：",
        "玄骨夺焰": f"一缕魔念直逼你识海中的乾蓝寒焰，玄骨上人的声音在 @{target} 脑海中炸响：",
    }[kind]
    text = (f"{introduction}\n\n**“{stem}”**\n\n" +
            "\n".join(f"**{letter}.** {answer}" for letter, answer in options.items()) +
            "\n\n小辈，你有 **300秒** 的时间，可点击按钮，也可回复本消息 `.作答 <选项>`。")
    return SimpleNamespace(id=message_id, chat_id=chat_id, sender_id=sender_id, text=text,
        date=datetime.now(timezone.utc) - timedelta(seconds=age), entities=[], out=False,
        reply_to_msg_id=topic, reply_to=SimpleNamespace(reply_to_msg_id=topic, reply_to_top_id=topic, forum_topic=bool(topic)))


def event(message, username="fanrenxiuxian_bot"):
    return SimpleNamespace(message=message, get_sender=AsyncMock(return_value=SimpleNamespace(
        id=message.sender_id, username=username, bot=True)))


class QuizTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.actors = []
        self.enabled = True
        self.emit_result = True
        self.gate = None
        self.preparing = asyncio.Event()
        self.dispatched = asyncio.Event()
        self.sent = []
        self._patch(quiz, "STATE_FILE", Path(self.temp.name) / "quiz.json")
        self._patch(quiz, "xuangu_quiz_enabled", lambda *args, **kwargs: self.enabled)
        self.ledger = self._patch(quiz, "record_command_response_for_command_id", MagicMock())
        self.delta = self._patch(quiz, "record_cultivation_delta_from_text", MagicMock())
        for name in ("record_command_sent", "record_recent_profile_command", "remember_script_sent_message",
                     "remember_script_send_intent", "schedule_command_auto_delete"):
            self._patch(feedback, name, MagicMock())
        self._patch(feedback, "command_send_allowed", MagicMock(return_value=True))
        self._patch(feedback, "wait_for_bot_activity_before_send", AsyncMock(return_value=True))
        self._patch(feedback, "_handle_telegram_send_protection", AsyncMock())
        self._patch(feedback, "record_telegram_send_success", AsyncMock())
        self._patch(feedback.asyncio, "sleep", AsyncMock())
        self.msg = question_message()
        self.actor = self.make_actor()

    def _patch(self, module, name, value):
        patcher = patch.object(module, name, value)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    async def asyncTearDown(self):
        tasks = [task for actor in self.actors for task in (getattr(actor, "_xuangu_quiz_tasks", {}) or {}).values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.temp.cleanup()

    def make_actor(self, account="main", username="my_player"):
        actor = SimpleNamespace(account_key=account, my_info=SimpleNamespace(id=12345, username=username),
            avatars=[], avatar_usernames={}, avatar_identities={}, identity_usernames={"主魂": [username]},
            target_chat_id=quiz.LINUXDO_CHAT, target_chat_ids=[quiz.JA_CHAT, quiz.LINUXDO_CHAT], topic_id=None,
            current_identity="主魂", state={}, config={}, mc={}, is_running=True,
            cmd_lock=asyncio.Lock(), pause_event=asyncio.Event(), feedback_events={}, feedback_commands={},
            feedback_sent_ts={}, last_feedback_text={}, last_feedback_msg={})
        actor.pause_event.set()
        async def send(chat, command, reply_to):
            self.sent.append((chat, command, reply_to))
            self.dispatched.set()
            if self.emit_result:
                await quiz.maybe_handle_xuangu_quiz(actor, self.result_event(self.msg, command[-1]))
            return SimpleNamespace(id=self.msg.id + 1, sender_id=actor.my_info.id, chat_id=chat, text=command)
        actor.client = SimpleNamespace(get_messages=AsyncMock(side_effect=lambda *args, **kw: self.msg),
                                       send_message=AsyncMock(side_effect=send))
        async def send_identity(identity, command, **kwargs):
            self.preparing.set()
            if self.gate:
                await self.gate.wait()
            actor.current_identity = identity
            return await feedback.send_and_wait_feedback_common(actor, MagicMock(), command,
                reply_to=kwargs["reply_to"], retry_on_timeout=kwargs["retry_on_timeout"], delete_after=False)
        actor.send_and_wait_feedback_identity = AsyncMock(side_effect=send_identity)
        self.actors.append(actor)
        return actor

    def result_event(self, question, letter="C", outcome="答对", message_id=None):
        parsed = quiz.parse_question(question.text)
        target = parsed.target
        body = f"【{parsed.kind}·{outcome}】\n@{target} 的答案 {letter} 完全正确！\n你获得了 5000 点修为作为奖赏！"
        if outcome == "答错":
            body = f"【{parsed.kind}·答错】\n@{target} 的答案 A 错得离谱！（正确答案: {letter}）"
        msg = SimpleNamespace(id=message_id or question.id + 3, text=body, chat_id=question.chat_id,
            sender_id=8964348409, date=datetime.now(timezone.utc), entities=[], out=False,
            reply_to_msg_id=question.reply_to_msg_id, reply_to=question.reply_to)
        return event(msg, "hantianzun22_bot")

    async def handle(self, msg=None, actor=None):
        return await quiz.maybe_handle_xuangu_quiz(actor or self.actor, event(msg or self.msg))

    async def finish(self, actor=None):
        tasks = list(getattr(actor or self.actor, "_xuangu_quiz_tasks", {}).values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    def state_entry(self, msg=None):
        return quiz._state()["events"][quiz._event_key(msg or self.msg)]

    def add_buttons(self, msg=None, token="MY68XJ"):
        msg = msg or self.msg
        msg.reply_markup = ReplyInlineMarkup(rows=[KeyboardButtonRow(buttons=[
            KeyboardButtonCallback(text=letter, data=f"xgq:{token}:{letter}".encode("ascii"))
            for letter in letters]) for letters in ("AB", "CD")])
        async def click(*, data):
            self.dispatched.set()
            if self.emit_result:
                await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(msg, data[-1:].decode("ascii")))
            return SimpleNamespace(message="答案已提交", alert=False)
        msg.click = AsyncMock(side_effect=click)
        return msg

    async def test_main_soul_button_answers_when_ja_group_sends_are_banned(self):
        self.actor.account_key = "xiaohao"
        self.actor.avatars = ["问心子"]
        self.actor.current_identity = "问心子"
        self.actor.client.send_message.side_effect = RuntimeError("You're banned from sending messages in supergroups/channels")
        self.add_buttons()
        await self.handle()
        await self.handle()
        await self.finish()
        await self.handle()
        self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:C")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.actor.client.send_message.assert_not_awaited()
        self.assertEqual(self.actor.current_identity, "问心子")
        self.assertEqual(self.state_entry()["transport"], "callback")
        self.assertEqual(self.state_entry()["status"], "correct")
        self.assertNotIn("sent_message_id", self.state_entry())
        self.ledger.assert_not_called()  # A button is not an outgoing chat message.
        self.delta.assert_called_once()

    async def test_restricted_monitor_answers_main_soul_with_buttons_only(self):
        from red_packet_account import install_restricted_quiz_monitor
        self.actor.client.add_event_handler = MagicMock()
        handlers = install_restricted_quiz_monitor(self.actor)
        self.add_buttons()
        await handlers[0][0](event(self.msg))
        await self.finish()
        self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:C")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertEqual(self.state_entry()["status"], "correct")

    async def test_real_telethon_message_uses_callback_request_for_original_question(self):
        self.add_buttons()
        peer = InputPeerChannel(-quiz.JA_CHAT - 1000000000000, 123)
        message = Message(id=self.msg.id, peer_id=PeerChannel(peer.channel_id),
            from_id=PeerUser(quiz.QUESTION_BOT_ID), date=self.msg.date, message=self.msg.text,
            reply_to=MessageReplyHeader(reply_to_msg_id=quiz.JA_TOPIC, reply_to_top_id=quiz.JA_TOPIC, forum_topic=True),
            reply_markup=self.msg.reply_markup)
        async def request(call):
            self.assertIsInstance(call, GetBotCallbackAnswerRequest)
            self.assertEqual((call.peer, call.msg_id, call.data), (peer, 400, b"xgq:MY68XJ:C"))
            await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(message))
            return SimpleNamespace(message="答案已提交", alert=False)
        client = AsyncMock(side_effect=request)
        client.parse_mode = None
        client.get_messages = AsyncMock(return_value=message)
        client.send_message = AsyncMock()
        message._client, message._input_chat = client, peer
        self.msg, self.actor.client = message, client
        await self.handle()
        await self.finish()
        client.assert_awaited_once()
        client.send_message.assert_not_awaited()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertEqual(self.state_entry()["status"], "correct")

    async def test_restricted_monitor_does_not_send_plain_answers_for_any_identity(self):
        self.actor.xuangu_quiz_callback_only = True
        await self.handle()  # Main soul with no keyboard.
        self.actor.avatars = ["问心子"]
        self.actor.avatar_usernames = {"avatar_player": "问心子"}
        self.msg = question_message(target="avatar_player", message_id=500)
        await self.handle()
        await self.finish()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertFalse(self.sent)

    async def test_avatar_prefers_original_button_without_sending_group_commands(self):
        self.actor.avatars = ["问心子"]
        self.actor.avatar_usernames = {"avatar_player": "问心子"}
        self.msg = self.add_buttons(question_message(target="avatar_player"))
        await self.handle()
        await self.finish()
        self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:C")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertFalse(self.sent)
        self.assertEqual(self.state_entry()["identity"], "问心子")
        self.assertEqual(self.state_entry()["status"], "correct")

    async def test_avatar_without_buttons_keeps_identity_command_fallback(self):
        self.actor.avatars = ["问心子"]
        self.actor.avatar_usernames = {"avatar_player": "问心子"}
        self.msg = question_message(target="avatar_player")
        await self.handle()
        await self.finish()
        self.assertEqual(self.sent, [(quiz.JA_CHAT, ".作答 C", 400)])
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_args.args[0], "问心子")

    async def test_all_thirteen_identities_and_three_quiz_kinds_prefer_buttons(self):
        participants = {
            "main": ("主魂", "无咎子", "缘生子", "素缘子"),
            "sub": ("主魂", "厚土", "缘生子", "寻真子"),
            "xiaohao": ("主魂", "问心子", "素心子", "缘生子"),
            "waaiging": ("主魂",),
        }
        bank = json.loads(quiz.BANK_FILE.read_text(encoding="utf-8"))["questions"]
        message_id = 1000
        for account, slots in participants.items():
            self.actor = self.make_actor(account, account + "_main")
            identities = [settings.canonical_automation_identity(account, name) for name in slots]
            self.actor.avatars = identities[1:]
            aliases = {identity: f"{account}_avatar_{index}" for index, identity in enumerate(identities[1:])}
            self.actor.avatar_usernames = {name: identity for identity, name in aliases.items()}
            # The standby worker exposes the same native buttons without group sends.
            self.actor.xuangu_quiz_callback_only = account in {"xiaohao", "waaiging"}
            for identity in identities:
                for kind in ("玄骨考校", "玄骨窥鼎", "玄骨夺焰"):
                    with self.subTest(account=account, identity=identity, kind=kind):
                        row = next(row for row in bank if row["event_type"] == kind)
                        target = self.actor.my_info.username if identity == "主魂" else aliases[identity]
                        self.msg = self.add_buttons(question_message(target=target, message_id=message_id,
                            kind=kind, stem=row["question"], options={"A": "干扰甲", "B": "干扰乙", "C": row["answer"], "D": "干扰丁"}))
                        message_id += 10
                        await self.handle()
                        await self.finish()
                        self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:C")
                        self.assertEqual(self.state_entry()["identity"], identity)
                        self.assertEqual(self.state_entry()["status"], "correct")
                        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
                        self.actor.client.send_message.assert_not_awaited()

    async def test_answer_button_labels_can_include_or_equal_option_text(self):
        formats = ("{letter}", "{letter}. {text}", "{letter}、{text}", "{letter}: {text}", "{letter} {text}", "{text}")
        for index, template in enumerate(formats):
            with self.subTest(template=template):
                username = f"label_player_{index}"
                self.actor.my_info.username = username
                self.actor.identity_usernames["主魂"] = [username]
                self.msg = self.add_buttons(question_message(target=username, message_id=500 + index))
                for row in self.msg.reply_markup.rows:
                    for button in row.buttons:
                        button.text = template.format(letter=button.text, text=OPTIONS[button.text])
                await self.handle()
                await self.finish()
                self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:C")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_button_only_question_does_not_need_a_text_command_hint(self):
        self.add_buttons()
        self.msg.text = self.msg.text.replace("，也可回复本消息 `.作答 <选项>`", "")
        await self.handle()
        await self.finish()
        self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:C")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertEqual(self.state_entry()["status"], "correct")

    async def test_opaque_original_callback_data_and_unrelated_controls_are_supported(self):
        self.add_buttons()
        for row in self.msg.reply_markup.rows:
            for button in row.buttons:
                button.data = b"\x00quiz\xff" + button.text.encode("ascii")
        self.msg.reply_markup.rows.append(KeyboardButtonRow(buttons=[KeyboardButtonUrl("帮助", "https://example.invalid/help")]))
        await self.handle()
        await self.finish()
        self.msg.click.assert_awaited_once_with(data=b"\x00quiz\xffC")
        self.assertEqual(self.state_entry()["callback_data_hex"], b"\x00quiz\xffC".hex())
        self.assertEqual(self.state_entry()["status"], "correct")

    async def test_duplicate_callback_data_cannot_select_a_unique_answer(self):
        self.add_buttons()
        for row in self.msg.reply_markup.rows:
            for button in row.buttons:
                button.data = b"same-payload"
        await self.handle()
        await self.finish()
        self.msg.click.assert_not_awaited()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertEqual(self.state_entry()["status"], "skipped")

    async def test_avatar_callback_failure_never_resubmits_as_a_group_command(self):
        self.actor.avatars = ["问心子"]
        self.actor.avatar_usernames = {"avatar_player": "问心子"}
        self.msg = self.add_buttons(question_message(target="avatar_player"))
        self.msg.click.side_effect = TimeoutError("callback response lost")
        await self.handle()
        await self.finish()
        await self.handle()
        await quiz.resume_pending_xuangu_quiz_events(self.actor)
        self.msg.click.assert_awaited_once()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.assertEqual(self.state_entry()["status"], "send_uncertain")

    async def test_button_answer_uses_current_option_order(self):
        self.msg = self.add_buttons(question_message(options={
            "A": "修罗圣火", "B": "紫罗极火", "C": "六极真魔火", "D": "碧焰天火"}))
        await self.handle()
        await self.finish()
        self.msg.click.assert_awaited_once_with(data=b"xgq:MY68XJ:A")
        self.assertEqual(self.state_entry()["status"], "correct")

    async def test_malformed_keyboard_never_falls_back_to_group_send(self):
        for index, (label, data) in enumerate((
                ("C", b"other:MY68XJ:C"), ("C", b"xgq:OTHER:C"),
                ("B", b"xgq:MY68XJ:C"), ("C", b"xgq:MY68XJ:D"), ("C", None))):
            with self.subTest(label=label, data=data):
                self.msg = self.add_buttons(question_message(message_id=400 + index))
                button = self.msg.reply_markup.rows[1].buttons[0]
                button.text, button.data = label, data
                await self.handle()
                await self.finish()
                self.msg.click.assert_not_awaited()
                self.assertEqual(self.state_entry()["status"], "skipped")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_removed_or_replaced_keyboard_prevents_any_submission(self):
        for index, replacement in enumerate((None, "DIFFERENT")):
            with self.subTest(replacement=replacement):
                self.msg = self.add_buttons(question_message(message_id=400 + index))
                live = question_message(message_id=self.msg.id)
                if replacement:
                    self.add_buttons(live, token=replacement)
                self.actor.client.get_messages.side_effect = None
                self.actor.client.get_messages.return_value = live
                await self.handle()
                await self.finish()
                self.msg.click.assert_not_awaited()
                self.assertEqual(self.state_entry()["status"], "invalid")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_button_question_must_belong_to_this_user_and_source(self):
        for msg in (question_message(target="somebody_else"), question_message(sender_id=999),
                    question_message(topic=999), question_message(chat_id=-1009999999999),
                    question_message(age=301), question_message(stem="未知的新题？")):
            with self.subTest(target=msg.text, chat=msg.chat_id):
                self.msg = self.add_buttons(msg)
                await self.handle()
                await self.finish()
                self.msg.click.assert_not_awaited()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_refetched_question_must_have_the_same_message_id(self):
        self.add_buttons()
        live = self.add_buttons(question_message(message_id=500))
        self.actor.client.get_messages.side_effect = None
        self.actor.client.get_messages.return_value = live
        await self.handle()
        await self.finish()
        live.click.assert_not_awaited()
        self.assertEqual(self.state_entry()["status"], "invalid")

    async def test_controls_are_rechecked_after_callback_question_fetch(self):
        for index, stop in enumerate(("disabled", "paused", "identity_pause", "command_guard", "expired")):
            with self.subTest(stop=stop):
                self.enabled = True
                self.actor.pause_event.set()
                self.actor.identity_pause_seconds = MagicMock(return_value=0)
                self.msg = self.add_buttons(question_message(message_id=400 + index))
                fetching, release = asyncio.Event(), asyncio.Event()
                async def fetch(*args, **kwargs):
                    fetching.set()
                    await release.wait()
                    return self.msg
                self.actor.client.get_messages.side_effect = fetch
                await self.handle()
                await asyncio.wait_for(fetching.wait(), 1)
                if stop == "disabled":
                    self.enabled = False
                elif stop == "paused":
                    self.actor.pause_event.clear()
                elif stop == "identity_pause":
                    self.actor.identity_pause_seconds.return_value = 60
                with patch.object(quiz, "command_send_precheck", return_value=False) if stop == "command_guard" else \
                        patch.object(quiz.time, "time", return_value=self.msg.date.timestamp() + 301) if stop == "expired" else \
                        nullcontext():
                    release.set()
                    await self.finish()
                self.msg.click.assert_not_awaited()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_callback_failure_or_empty_response_never_retries_or_falls_back(self):
        for index, error in enumerate((TimeoutError("response lost"), RuntimeError("request failed"), None)):
            with self.subTest(error=error):
                self.msg = self.add_buttons(question_message(message_id=400 + index))
                self.msg.click.side_effect = error
                self.msg.click.return_value = None
                await self.handle()
                await self.finish()
                await self.handle()
                await quiz.resume_pending_xuangu_quiz_events(self.actor)
                self.msg.click.assert_awaited_once()
                self.assertEqual(self.state_entry()["status"], "send_uncertain")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_callback_acceptance_waits_for_actual_judgment(self):
        self.emit_result = False
        self.add_buttons()
        await self.handle()
        await asyncio.wait_for(self.dispatched.wait(), 1)
        self.assertNotEqual(self.state_entry()["status"], "correct")
        self.delta.assert_not_called()
        await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(self.msg))
        await self.finish()
        self.assertEqual(self.state_entry()["status"], "correct")
        self.delta.assert_called_once()

    async def test_edited_original_question_can_confirm_callback_result(self):
        self.emit_result = False
        self.add_buttons()
        await self.handle()
        await asyncio.wait_for(self.dispatched.wait(), 1)
        result = self.result_event(self.msg, message_id=self.msg.id)
        result.message.sender_id = quiz.QUESTION_BOT_ID
        await quiz.maybe_handle_xuangu_quiz(self.actor, result)
        await self.finish()
        await quiz.maybe_handle_xuangu_quiz(self.actor, result)
        self.assertEqual(self.state_entry()["status"], "correct")
        self.delta.assert_called_once()

    async def test_pending_callback_can_resume_in_restricted_worker(self):
        self.actor.xuangu_quiz_callback_only = True
        self.add_buttons()
        self.msg.get_sender = event(self.msg).get_sender
        entry = quiz._question_entry(self.msg, quiz.parse_question(self.msg.text))
        entry.update(status="queued", account=self.actor.account_key, identity="主魂", owner="old-process")
        quiz._update(lambda state: state["events"].update({quiz._event_key(self.msg): entry}))
        await quiz.resume_pending_xuangu_quiz_events(self.actor)
        await self.finish()
        await quiz.resume_pending_xuangu_quiz_events(self.actor)
        self.msg.click.assert_awaited_once()
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    def test_answer_content_tracks_shuffled_options(self):
        self.assertEqual(len(quiz._bank()), 14)
        self.assertEqual(quiz.answer_for(quiz.parse_question(self.msg.text)), "C")
        shuffled = question_message(options={"A": "修罗圣火", "B": "紫罗极火", "C": "六极真魔火", "D": "碧焰天火"})
        self.assertEqual(quiz.answer_for(quiz.parse_question(shuffled.text)), "A")

    def test_similar_flame_questions_have_different_answers(self):
        question = quiz.Question("玄骨夺焰", "my_player", "乾蓝冰焰后续化极时，要冲击蜕变成什么更凶的焰相？",
                                 {"A": "南明离火", "B": "修罗圣火", "C": "紫罗极火", "D": "九幽冥焰"})
        self.assertEqual(quiz.answer_for(question), "C")
        self.assertEqual(question.options[quiz.answer_for(question)], "紫罗极火")

    def test_target_is_exact_and_ambiguous_identity_is_rejected(self):
        self.assertIsNone(quiz.target_identity(self.actor, "my_player_extra"))
        self.assertIsNone(quiz.target_identity(self.actor, "陌生中文名字"))
        self.actor.avatars = ["无咎子"]
        self.actor.avatar_usernames = {"avatar_player": "无咎子"}
        self.assertEqual(quiz.target_identity(self.actor, "avatar_player"), "无咎子")
        self.actor.identity_usernames["无咎子"] = ["my_player"]
        self.assertIsNone(quiz.target_identity(self.actor, "my_player"))

    async def test_known_question_is_sent_once_to_source_chat_and_reply(self):
        await self.handle()
        await self.handle()  # duplicate delivery before dispatch
        await self.finish()
        await self.handle()  # duplicate delivery after judgment
        self.assertEqual(self.sent, [(quiz.JA_CHAT, ".作答 C", 400)])
        self.assertEqual(self.state_entry()["status"], "correct")
        self.assertEqual(self.state_entry()["sent_message_id"], 401)
        self.assertEqual(self.actor.feedback_events, {})
        self.ledger.assert_called_once()
        self.delta.assert_called_once()

    async def test_wrong_group_topic_sender_and_expired_questions_do_not_send(self):
        for msg in (question_message(chat_id=-1009999999999), question_message(topic=999),
                    question_message(sender_id=999), question_message(age=301)):
            await self.handle(msg)
        self.assertFalse(self.sent)
        self.assertFalse(quiz._state().get("events"))

    async def test_linuxdo_and_unmarked_config_ids_route_to_source_chat(self):
        self.actor.target_chat_ids = [1680975844, 2083016447]
        self.msg = question_message(chat_id=quiz.LINUXDO_CHAT, topic=None)
        await self.handle()
        await self.finish()
        self.assertEqual(self.sent, [(quiz.LINUXDO_CHAT, ".作答 C", 400)])
        self.assertEqual(self.state_entry()["chat_id"], quiz.LINUXDO_CHAT)
        self.assertIsNone(self.state_entry()["topic_id"])

    async def test_other_player_is_never_answered(self):
        self.msg = question_message(target="someone_else")
        await self.handle()
        self.assertEqual(self.state_entry()["status"], "observed")
        self.assertFalse(self.sent)

    async def test_missing_question_does_not_fall_back_to_plain_command(self):
        self.actor.client.get_messages.side_effect = None
        self.actor.client.get_messages.return_value = None
        await self.handle()
        await self.finish()
        self.assertEqual(self.state_entry()["status"], "invalid")
        self.assertFalse(self.sent)

    async def test_disabled_and_restricted_accounts_only_observe(self):
        self.enabled = False
        await self.handle()
        self.assertFalse(self.sent)
        self.enabled = True
        self.actor.xuangu_quiz_read_only = True
        self.msg = question_message(message_id=500)
        await self.handle()
        self.assertFalse(self.sent)

    async def test_disable_while_waiting_prevents_dispatch(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        self.enabled = False
        self.gate.set()
        await self.finish()
        self.assertFalse(self.sent)
        self.assertEqual(self.state_entry()["status"], "skipped")

    async def test_expiry_is_rechecked_at_dispatch(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        with patch.object(quiz.time, "time", return_value=self.msg.date.timestamp() + 301):
            self.gate.set()
            await self.finish()
        self.assertFalse(self.sent)
        self.assertEqual(self.state_entry()["status"], "expired")

    async def test_question_deleted_while_queued_is_rechecked_before_send(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        self.actor.client.get_messages.side_effect = None
        self.actor.client.get_messages.return_value = None
        self.gate.set()
        await self.finish()
        self.assertFalse(self.sent)
        self.assertEqual(self.state_entry()["status"], "invalid")

    async def test_unreported_question_edit_while_queued_stops_send(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        self.actor.client.get_messages.side_effect = None
        self.actor.client.get_messages.return_value = question_message(stem="题干发生变化？")
        self.gate.set()
        await self.finish()
        self.assertFalse(self.sent)
        self.assertEqual(self.state_entry()["status"], "invalid")

    async def test_manual_reply_cancels_queued_automatic_answer(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        manual = SimpleNamespace(**{**vars(self.msg), "id": 401, "text": ".作答 C", "sender_id": 12345,
                                    "reply_to_msg_id": self.msg.id})
        await quiz.maybe_handle_xuangu_quiz(self.actor, event(manual, "my_player"))
        self.gate.set()
        await self.finish()
        self.assertEqual(self.state_entry()["status"], "manual")
        self.assertFalse(self.sent)

    async def test_edit_to_invalid_cancels_queued_answer(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        invalid = SimpleNamespace(**{**vars(self.msg), "text": "【玄骨考校·题面失效】\n@my_player 未能及时作答..."})
        await self.handle(invalid)
        self.gate.set()
        await self.finish()
        self.assertEqual(self.state_entry()["status"], "invalid")
        self.assertFalse(self.sent)

    async def test_avatar_manual_reply_cancels_queue_using_channel_map(self):
        self.actor.avatars = ["问心子"]
        self.actor.avatar_usernames = {"avatar_player": "问心子"}
        self.actor._avatar_chat_ids = {"-1003658665113": "问心子"}
        self.msg = question_message(target="avatar_player")
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        manual = SimpleNamespace(**{**vars(self.msg), "id": 401, "text": ".作答 C",
                                    "sender_id": -1003658665113, "reply_to_msg_id": self.msg.id})
        await quiz.maybe_handle_xuangu_quiz(self.actor, event(manual, "avatar_player"))
        self.gate.set()
        await self.finish()
        self.assertEqual(self.state_entry()["status"], "manual")
        self.assertFalse(self.sent)

    async def test_invalid_edit_does_not_consume_later_judgment(self):
        self.emit_result = False
        await self.handle()
        await asyncio.wait_for(self.dispatched.wait(), 1)
        invalid = SimpleNamespace(**{**vars(self.msg), "text": "【玄骨考校·题面失效】\n@my_player 未能及时作答..."})
        await self.handle(invalid)
        self.delta.assert_not_called()
        await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(self.msg))
        await self.finish()
        self.assertEqual(self.state_entry()["status"], "correct")
        self.delta.assert_called_once()

    async def test_unknown_question_is_durable_and_manual_answer_applies_next_time(self):
        self.msg = question_message(stem="一条新加入的题目？")
        await self.handle()
        question = quiz.parse_question(self.msg.text)
        payload = quiz.quiz_dashboard_payload()
        self.assertEqual(payload["pending_count"], 1)
        self.assertEqual(payload["pending"][0]["options"], OPTIONS)
        self.assertFalse(self.sent)
        with self.assertRaises(ValueError):
            quiz.confirm_quiz_answer(question.key, "不在选项中的猜测")
        quiz.confirm_quiz_answer(question.key, "修罗圣火", "test-user")
        self.assertEqual(quiz.quiz_dashboard_payload()["pending_count"], 0)
        self.msg = question_message(stem=question.text, message_id=500, chat_id=quiz.LINUXDO_CHAT, topic=None)
        await self.handle()
        await self.finish()
        self.assertEqual(self.sent, [(quiz.LINUXDO_CHAT, ".作答 C", 500)])

    async def test_ambiguous_judgment_does_not_attach_to_either_question(self):
        self.enabled = False
        await self.handle()
        await self.handle(question_message(message_id=401))
        await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(self.msg))
        self.assertTrue(all("result" not in entry for entry in quiz._state()["events"].values()))
        self.delta.assert_not_called()

    async def test_conflicting_judgment_suspends_answer_until_confirmed(self):
        self.msg = question_message(target="other_player")
        await self.handle()
        await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(self.msg, "A", "答错"))
        q = quiz.parse_question(self.msg.text)
        self.assertIsNone(quiz.answer_for(q))
        self.assertEqual(quiz.quiz_dashboard_payload()["pending"][0]["observed_answer"], "紫罗极火")

    async def test_other_observer_receives_result_first_without_stealing_owner_processing(self):
        self.emit_result = False
        await self.handle()
        await asyncio.wait_for(self.dispatched.wait(), 1)
        other = self.make_actor(account="sub", username="other_account")
        response = self.result_event(self.msg)
        await quiz.maybe_handle_xuangu_quiz(other, response)
        self.delta.assert_not_called()
        await quiz.maybe_handle_xuangu_quiz(self.actor, response)
        await self.finish()
        await quiz.maybe_handle_xuangu_quiz(self.actor, self.result_event(self.msg, message_id=405))
        self.delta.assert_called_once()
        self.ledger.assert_called_once()

    async def test_ambiguous_network_failure_is_not_retried(self):
        self.actor.client.send_message.side_effect = TimeoutError("connection lost after dispatch")
        await self.handle()
        await self.finish()
        await self.handle()
        await quiz.resume_pending_xuangu_quiz_events(self.actor)
        self.actor.client.send_message.assert_awaited_once()
        self.assertEqual(self.state_entry()["status"], "send_uncertain")

    async def test_queued_event_can_resume_but_sent_event_cannot(self):
        self.gate = asyncio.Event()
        await self.handle()
        await asyncio.wait_for(self.preparing.wait(), 1)
        task = next(iter(self.actor._xuangu_quiz_tasks.values()))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.state_entry()["status"], "queued")
        self.gate = None
        self.msg.get_sender = event(self.msg).get_sender
        await quiz.resume_pending_xuangu_quiz_events(self.actor)
        await self.finish()
        await quiz.resume_pending_xuangu_quiz_events(self.actor)
        self.assertEqual(len(self.sent), 1)


class QuizSettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_identity_selection_controls_enabled_state(self):
        self.assertFalse(settings.xuangu_quiz_enabled("main", "主魂", {}))
        selected = {"xuangu_quiz": {"enabled": True, "participants": ["main|主魂"]}}
        self.assertTrue(settings.xuangu_quiz_enabled("main", "主魂", selected))
        self.assertFalse(settings.xuangu_quiz_enabled("main", "无咎子", selected))
        selected["xuangu_quiz"]["enabled"] = False
        self.assertFalse(settings.xuangu_quiz_enabled("main", "主魂", selected))

    def test_saving_other_settings_preserves_quiz_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, "AUTOMATION_SETTINGS_FILE", Path(directory) / "settings.json"):
            current = settings.default_automation_settings()
            current["xuangu_quiz"] = {"enabled": True, "participants": ["main|主魂"]}
            settings.AUTOMATION_SETTINGS_FILE.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            saved = settings.save_automation_settings(world_boss_participants=current["world_boss"]["participants"],
                                                       mulan_support_mode=current["mulan_support"]["mode"])
            self.assertEqual(saved["xuangu_quiz"], current["xuangu_quiz"])


if __name__ == "__main__":
    unittest.main()
