import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import automation_settings as settings
import command_feedback as feedback
import xuangu_quiz_features as quiz


STEM = "玄骨妄图炼化乾蓝冰焰，是想将其修成什么魔焰？"
OPTIONS = {"A": "紫罗极火", "B": "碧焰天火", "C": "修罗圣火", "D": "六极真魔火"}


def question_message(target="my_player", message_id=400, chat_id=quiz.JA_CHAT, topic=quiz.JA_TOPIC,
                     stem=STEM, options=None, age=0, sender_id=quiz.QUESTION_BOT_ID):
    options = OPTIONS if options is None else options
    text = (f"神念直入脑海，一个苍老的声音向 @{target} 提问：\n\n**“{stem}”**\n\n" +
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
        target = quiz.parse_question(question.text).target
        body = f"【玄骨考校·{outcome}】\n@{target} 的答案 {letter} 完全正确！\n你获得了 5000 点修为作为奖赏！"
        if outcome == "答错":
            body = f"【玄骨考校·答错】\n@{target} 的答案 A 错得离谱！（正确答案: {letter}）"
        msg = SimpleNamespace(**{**vars(question), "id": message_id or question.id + 3, "text": body,
                                  "sender_id": 8964348409, "date": datetime.now(timezone.utc)})
        return event(msg, "hantianzun22_bot")

    async def handle(self, msg=None, actor=None):
        return await quiz.maybe_handle_xuangu_quiz(actor or self.actor, event(msg or self.msg))

    async def finish(self, actor=None):
        tasks = list(getattr(actor or self.actor, "_xuangu_quiz_tasks", {}).values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    def state_entry(self, msg=None):
        return quiz._state()["events"][quiz._event_key(msg or self.msg)]

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
