import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import cultivator_waaiging as waaiging
import intelligent_cultivator as core
import hehuan_features as hehuan


NOW = datetime(2026, 9, 8, 7, 3, 45)
CHAT = -100123
PENDING = "契印感应，双方灵力开始共鸣，准备进行温养双修..."
SETTLED = "**【温养双修·大成】**\n你与 @Weeguu 灵力完美交融！"


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def message(text, *, edited=None, username="Weeguu", msg_id=123):
    return SimpleNamespace(
        id=msg_id, chat_id=CHAT, text=text, date=NOW - timedelta(seconds=43),
        edit_date=edited, sender=SimpleNamespace(username=username),
    )


class DualCultivationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target in (core, waaiging, hehuan):
            clock = patch.object(target, "datetime", Clock)
            clock.start()
            self.addCleanup(clock.stop)
        self.actor = waaiging.WaaigingCultivator.__new__(waaiging.WaaigingCultivator)
        self.actor.state = {"dual_cultivation_schedule_version": 1, "sect_name": "合欢宗",
                            "identity_sect_names": {"主魂": "合欢宗"}}
        self.actor.account_key = "waaiging"
        self.actor.avatars = []
        self.actor.dashboard_command_paused = Mock(return_value=False)
        self.actor.identity_pause_seconds = Mock(return_value=0)
        self.actor.save_state = Mock()
        self.actor.target_chat_id = CHAT
        self.actor.client = SimpleNamespace(get_messages=AsyncMock())
        self.actor.send_and_wait_feedback = AsyncMock()
        self.actor._find_main_soul_recent_message = AsyncMock(return_value=message("主号历史消息"))
        self.sleeper = patch.object(waaiging.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep = self.sleeper.start()
        self.addCleanup(self.sleeper.stop)

    def assert_next(self, when):
        self.assertEqual(self.actor.state["next_dual_cultivation_time"], when.strftime(core.TIME_FORMAT))

    async def test_next_run_uses_edited_settlement_time_not_the_initial_reply_or_return_time(self):
        settled_at = NOW - timedelta(seconds=35)
        self.actor.send_and_wait_feedback.return_value = message(PENDING)
        self.actor.client.get_messages.return_value = message(SETTLED, edited=settled_at)
        self.assertTrue(await self.actor.execute_dual_cultivation_once())
        self.assert_next(settled_at + timedelta(hours=1, seconds=2))
        self.assertEqual(self.actor.state["last_dual_cultivation_time"], settled_at.strftime(core.TIME_FORMAT))
        self.assertTrue(self.actor.send_and_wait_feedback.await_args.kwargs["return_response_msg"])
        self.assertNotIn("dual_cultivation_pending_response_id", self.actor.state)

    async def test_cooldown_without_remaining_time_retries_in_one_minute(self):
        self.actor.send_and_wait_feedback.return_value = message("心神尚未恢复，无法进行双修（冷却中）。")
        self.assertFalse(await self.actor.execute_dual_cultivation_once())
        self.assert_next(NOW + timedelta(seconds=60))
        self.assertEqual(self.actor.state["dual_cultivation_last_status"], "cooldown")
        self.assertNotIn("last_dual_cultivation_time", self.actor.state)

    async def test_cooldown_uses_the_reported_remaining_time(self):
        self.actor.send_and_wait_feedback.return_value = message("双修冷却中，剩余 1分钟5秒。")
        await self.actor.execute_dual_cultivation_once()
        self.assert_next(NOW + timedelta(seconds=67))

    async def test_empty_or_failed_reply_is_not_a_success_or_another_hour_wait(self):
        for reply in (None, message("【温养双修·失败】未能完成温养")):
            with self.subTest(reply=reply):
                self.actor.send_and_wait_feedback.return_value = reply
                self.assertFalse(await self.actor.execute_dual_cultivation_once())
                self.assert_next(NOW + timedelta(minutes=5))
                self.assertNotIn("last_dual_cultivation_time", self.actor.state)

    async def test_slow_edit_saves_pending_reply_without_resending(self):
        pending = message(PENDING)
        pending.date = NOW
        self.actor.send_and_wait_feedback.return_value = pending
        self.actor.client.get_messages.return_value = pending
        self.assertFalse(await self.actor.execute_dual_cultivation_once())
        self.assertEqual(self.actor.state["dual_cultivation_last_status"], "awaiting_settlement")
        self.assertEqual(self.actor.state["dual_cultivation_pending_response_id"], pending.id)
        self.assert_next(NOW + timedelta(seconds=30))
        self.actor.client.get_messages.return_value = message(SETTLED, edited=NOW)
        self.assertTrue(await self.actor.execute_dual_cultivation_once())
        self.actor.send_and_wait_feedback.assert_awaited_once()
        self.assert_next(NOW + timedelta(hours=1, seconds=2))

    async def test_restart_recovers_a_saved_settlement_without_another_command(self):
        self.actor.state.update({
            "dual_cultivation_pending_response_id": 456,
            "dual_cultivation_pending_chat_id": CHAT,
            "dual_cultivation_pending_since": (NOW - timedelta(seconds=90)).strftime(core.TIME_FORMAT),
        })
        self.actor.client.get_messages.return_value = message(SETTLED, edited=NOW - timedelta(seconds=80), msg_id=456)
        self.assertTrue(await self.actor.execute_dual_cultivation_once())
        self.actor.send_and_wait_feedback.assert_not_awaited()
        self.actor._find_main_soul_recent_message.assert_not_awaited()

    async def test_missing_old_settlement_has_a_bounded_recovery_wait(self):
        since = NOW - timedelta(seconds=90)
        self.actor.state.update({
            "dual_cultivation_pending_response_id": 456,
            "dual_cultivation_pending_chat_id": CHAT,
            "dual_cultivation_pending_since": since.strftime(core.TIME_FORMAT),
        })
        self.actor.client.get_messages.return_value = None
        self.assertFalse(await self.actor.execute_dual_cultivation_once())
        self.assertEqual(self.actor.state["dual_cultivation_last_status"], "unconfirmed")
        self.assert_next(since + timedelta(hours=1, seconds=20))
        self.assertNotIn("dual_cultivation_pending_response_id", self.actor.state)
        self.actor.send_and_wait_feedback.assert_not_awaited()

    async def test_sender_search_finds_main_soul_outside_last_thirty_group_messages(self):
        del self.actor._find_main_soul_recent_message
        anchor = message("一小时前的主号消息", username="weeguu")
        self.actor.client.get_messages.return_value = [anchor]
        self.assertIs(await self.actor._find_main_soul_recent_message(), anchor)
        self.actor.client.get_messages.assert_awaited_once_with(CHAT, from_user="Weeguu", limit=5)
        self.assertEqual(self.actor.state["dual_cultivation_target_message_id"], anchor.id)

    async def test_valid_cached_anchor_does_not_require_a_new_main_soul_message(self):
        del self.actor._find_main_soul_recent_message
        self.actor.state.update({"dual_cultivation_target_message_id": 123, "dual_cultivation_target_chat_id": CHAT})
        anchor = message("旧主号消息")
        self.actor.client.get_messages.return_value = anchor
        self.assertIs(await self.actor._find_main_soul_recent_message(), anchor)
        self.actor.client.get_messages.assert_awaited_once_with(CHAT, ids=123)

    async def test_deleted_or_wrong_sender_anchor_is_replaced(self):
        for stale in (None, message("别人的消息", username="someone_else")):
            with self.subTest(stale=stale):
                actor = self.actor
                actor.state.update({"dual_cultivation_target_message_id": 123, "dual_cultivation_target_chat_id": CHAT})
                replacement = message("主号消息", msg_id=789)
                actor.client.get_messages.side_effect = [stale, [replacement]]
                found = await waaiging.WaaigingCultivator._find_main_soul_recent_message(actor)
                self.assertIs(found, replacement)
                self.assertEqual(actor.state["dual_cultivation_target_message_id"], 789)

    async def test_future_schedule_is_preserved_on_restart(self):
        next_run = NOW + timedelta(minutes=50)
        self.actor.state["next_dual_cultivation_time"] = next_run.strftime(core.TIME_FORMAT)
        self.actor.startup_done = asyncio.Event()
        self.actor.startup_done.set()
        self.actor.is_running = True

        async def stop_after_wait(seconds):
            self.actor.is_running = False

        self.sleep.side_effect = stop_after_wait
        await self.actor.run_dual_cultivation_loop()
        self.assert_next(next_run)
        self.actor.send_and_wait_feedback.assert_not_awaited()

    async def test_legacy_sixty_five_minute_schedule_loses_the_extra_buffer(self):
        self.actor.state.pop("dual_cultivation_schedule_version")
        self.actor.state["next_dual_cultivation_time"] = "2026-09-08 08:08:06"
        self.actor.startup_done = asyncio.Event()
        self.actor.startup_done.set()
        self.actor.is_running = False
        await self.actor.run_dual_cultivation_loop()
        self.assert_next(datetime(2026, 9, 8, 8, 3, 16))
        self.assertEqual(self.actor.state["dual_cultivation_schedule_version"], 1)


if __name__ == "__main__":
    unittest.main()
