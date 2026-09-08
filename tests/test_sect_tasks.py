import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from common_command_features import CommonCommandMixin
import log_utils
import dashboard_server as dashboard
from miniapp_command_routing import MiniAppCommandRouter
from miniapp_dwelling import apply_dwelling_snapshot
import sect_task_features as sect
from sub_cultivator import SubCultivator


GUIDE, ASK = sect.SECT_DAILY_TASKS
NOW = datetime(2026, 9, 8, 22, 0)
SUCCESS = "你引动【水之道】，获得了 **100点神识**！\n并领悟了临时增益**【润水之息】**：\n普通闭关修炼时，获得的修为增加45%。"


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


class Actor(CommonCommandMixin):
    def __init__(self, account="sub"):
        self.account_key = account
        self.avatars = ["寒续尘"]
        self.state = {"avatars": {"寒续尘": {}}, "identity_sect_names": {"主魂": "散修", "寒续尘": "落云宗"}}
        self.identity_sect_names = self.state["identity_sect_names"]
        self.current_identity = "主魂"
        self.is_running = True
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self._sect_tasks_changed = asyncio.Event()
        self.save_state = Mock()
        self.log = Mock()
        self.dashboard_command_paused = Mock(return_value=False)
        self.identity_pause_seconds = Mock(return_value=0)
        self.send_and_wait_feedback_identity = AsyncMock(return_value=SUCCESS)
        self.send_and_wait_feedback = AsyncMock(return_value=SUCCESS)
        self._wait_for_main_identity = AsyncMock()
        self.record_daily_reward_event = Mock()

    def get_avatar_state(self, identity):
        return self.state["avatars"].setdefault(identity, {})

    def common_command_logger(self):
        return self.log

    parse_wait_time = SubCultivator.parse_wait_time


class SectTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = Actor()
        for module in (sect,):
            clock = patch.object(module, "datetime", Clock)
            clock.start()
            self.addCleanup(clock.stop)
        import common_command_features as common
        clock = patch.object(common, "datetime", Clock)
        clock.start()
        self.addCleanup(clock.stop)
        wings = patch("common_command_features.wind_thunder_enabled", return_value=False)
        wings.start()
        self.addCleanup(wings.stop)

    def join(self, identity="寒续尘", guild="太一门"):
        self.actor.sync_identity_sect_from_text(identity, "你成功拜入【" + guild + "】！")

    async def test_join_starts_guide_and_repeat_tick_respects_cooldown(self):
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()
        self.join()
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_count, 1)
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_args.args, ("寒续尘", ".引道 水"))
        self.assertEqual(self.actor.get_avatar_state("寒续尘")["next_taiyi_guide_time"], "2026-09-09 10:00:00")

    async def test_any_account_main_soul_can_join_taiyi(self):
        for account in ("main", "sub", "xiaohao", "waaiging"):
            self.actor = Actor(account)
            self.join("主魂")
            await self.actor.execute_sect_daily_once(GUIDE, "主魂")
            self.actor.send_and_wait_feedback.assert_awaited_once()
            self.assertEqual(self.actor.state["taiyi_guide_last_status"], "success")

    async def test_leaving_sect_blocks_old_task_and_preserves_cooldown(self):
        self.join()
        self.actor.record_avatar_taiyi_guide_response("寒续尘", SUCCESS)
        due = self.actor.get_avatar_state("寒续尘")["next_taiyi_guide_time"]
        self.actor.sync_identity_sect_from_text("寒续尘", "你已退出宗门。")
        self.assertEqual(self.actor.identity_sect_name("寒续尘"), "散修")
        self.assertFalse(self.actor.sect_command_allowed(".引道 水", "寒续尘"))
        self.assertEqual(self.actor.get_avatar_state("寒续尘")["next_taiyi_guide_time"], due)
        self.join()
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    def test_rejected_or_instructional_join_does_not_activate(self):
        for text in ("你无法拜入太一门。", "请先加入太一门。", "若要拜入太一门，请先退出当前宗门。", "加入太一门需要修为。"):
            self.assertFalse(self.actor.sync_identity_sect_from_text("寒续尘", text), text)
        self.assertEqual(self.actor.identity_sect_name("寒续尘"), "落云宗")

    def test_real_join_reply_activates_correct_identity(self):
        self.actor.sync_identity_sect_from_text("寒续尘", "[Avatar: 寒续尘]\n恭喜 @Ding303 道友，你已通过考验，成功拜入**【太一门】**，成为本门弟子！")
        self.assertTrue(self.actor.sect_command_allowed(".引道 水", "寒续尘"))
        self.assertFalse(self.actor.sect_command_allowed(".引道 水", "主魂"))

    async def test_manual_reply_entry_restores_avatar_cooldown(self):
        msg = SimpleNamespace(id=11, chat_id=100, reply_to_msg_id=10, date=datetime(2026, 9, 8, 14, 27, 24))
        with patch.object(log_utils, "is_reply_to_manual_command", return_value=True), \
                patch.object(log_utils, "manual_command_text_for_reply", return_value=".引道 水"), \
                patch.object(log_utils, "manual_command_identity_for_reply", return_value="寒续尘"), \
                patch.object(log_utils, "log_incoming_message", new=AsyncMock()), \
                patch.object(log_utils, "remember_manual_reply_logged_message"), \
                patch.object(log_utils, "mentions_other_user", return_value=False):
            self.assertTrue(await log_utils.record_manual_command_reply_state_if_needed(self.actor, msg, SUCCESS))
        self.assertEqual(self.actor.get_avatar_state("寒续尘")["next_taiyi_guide_time"], "2026-09-09 02:27:24")
        self.assertNotIn("next_taiyi_guide_time", self.actor.state)

    async def test_miniapp_checks_new_membership_after_auth_wait(self):
        self.join(guild="元婴宗")
        self.actor.client = object()
        self.actor.config = {"miniapp_beast": {"entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"}}
        router = MiniAppCommandRouter(self.actor, "sub", logger=self.actor.log, start_background_tasks=False)
        router._route_active = True
        router.transport.identity_player_ids = {"主魂": 1, "寒续尘": -2}
        router.transport.command = AsyncMock()
        async def refresh():
            self.join(guild="太一门")
        router._maybe_refresh_auth = refresh
        fallback = AsyncMock()
        result = await router._route("寒续尘", ".问道", fallback, (), {})
        self.assertIsNone(result)
        router.transport.command.assert_not_awaited()
        fallback.assert_not_awaited()

    def test_dashboard_moves_daily_controls_with_membership(self):
        state = {"sect_name": "元婴宗", "identity_sect_names": {"主魂": "元婴宗", "寒续尘": "太一门"},
                 "avatars": {"寒续尘": {"sect_name": "太一门", "next_taiyi_guide_time": "2026-09-09 02:27:24"}}}
        controls = {"sub": {"寒续尘": {".引道 水": {"disabled": True}}}}
        with patch.object(dashboard, "load_custom_commands", return_value={}), \
                patch.object(dashboard, "load_command_controls", return_value=controls):
            panels = {p["identity"]: p for p in dashboard.build_command_panels("sub", state)}
            rows = [row for row in panels["寒续尘"]["commands"] if row["command"] == ".引道 水"]
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["control_disabled"])
            self.assertEqual(rows[0]["at"], "2026-09-09 02:27:24")
            self.assertEqual(rows[0]["classification"]["subcategory"], "太一门")
            state["identity_sect_names"]["寒续尘"] = "元婴宗"
            panels = {p["identity"]: p for p in dashboard.build_command_panels("sub", state)}
            commands = [row["command"] for row in panels["寒续尘"]["commands"]]
            self.assertNotIn(".引道 水", commands)
            self.assertEqual(commands.count(".问道"), 1)

    def test_snapshot_changes_membership_and_wakes_scheduler(self):
        apply_dwelling_snapshot(self.actor, "寒续尘", {"account": {"profile": {"sectName": "太一门"}}})
        self.assertTrue(self.actor._sect_tasks_changed.is_set())
        self.assertTrue(self.actor.sect_command_allowed(".引道 水", "寒续尘"))
        apply_dwelling_snapshot(self.actor, "寒续尘", {"account": {"profile": {"sectName": "读取中"}}})
        self.assertTrue(self.actor.sect_command_allowed(".引道 水", "寒续尘"))

    async def test_renamed_identity_uses_current_name_and_keeps_cooldown(self):
        self.join()
        self.actor.refresh_avatar_dao_name("寒续尘", "新道号", player_id=-1003340352216)
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_args.args[0], "新道号")
        self.assertNotIn("寒续尘", self.actor.state["avatars"])
        self.assertTrue(self.actor.state["avatars"]["新道号"]["last_taiyi_guide_time"])

    async def test_dashboard_and_identity_pause_are_not_overridden_by_join(self):
        self.actor.dashboard_command_paused.return_value = True
        self.join()
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.actor.dashboard_command_paused.return_value = False
        self.actor.identity_pause_seconds.return_value = 3600
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.actor.identity_pause_seconds.return_value = 0
        self.actor.state["is_paused"] = True
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_sect_change_while_waiting_for_atomic_task_prevents_send(self):
        self.join()
        @asynccontextmanager
        async def atomic(*args, **kwargs):
            self.actor.sync_identity_sect_from_text("寒续尘", "你已退出宗门。")
            yield
        self.actor.common_atomic_task = atomic
        await self.actor.execute_sect_daily_once(GUIDE, "寒续尘")
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    def test_real_success_reply_is_not_confused_with_meditation(self):
        self.assertEqual(log_utils.text_response_family(SUCCESS), "taiyi_guide")
        self.assertTrue(log_utils.feedback_response_matches_command(".引道 水", SUCCESS))
        self.assertFalse(log_utils.feedback_response_matches_command(".引道 水", "闭关修炼成功"))

    def test_unknown_reply_and_denied_action_do_not_count_as_success(self):
        for text, status in (("宗门介绍：太一门", "unknown"), ("你并非太一门弟子，无法参悟大道本源。", "blocked"), ("", "empty")):
            self.assertEqual(self.actor.record_avatar_taiyi_guide_response("寒续尘", text), status)
            self.assertNotIn("last_taiyi_guide_time", self.actor.get_avatar_state("寒续尘"))

    def test_cooldown_uses_bot_duration_with_buffer(self):
        self.actor.record_avatar_taiyi_guide_response("寒续尘", "引道冷却中，请在 3小时 后再来引道。")
        self.assertEqual(self.actor.get_avatar_state("寒续尘")["next_taiyi_guide_time"], "2026-09-09 01:01:00")

    def test_manual_success_uses_message_time_not_processing_time(self):
        self.actor.record_avatar_taiyi_guide_response("寒续尘", SUCCESS, observed_at=datetime(2026, 9, 8, 14, 27, 24))
        state = self.actor.get_avatar_state("寒续尘")
        self.assertEqual(state["next_taiyi_guide_time"], "2026-09-09 02:27:24")
        self.actor.record_avatar_taiyi_guide_response("寒续尘", "引道冷却中，请在 3小时 后再来引道。", observed_at=datetime(2026, 9, 8, 13))
        self.assertEqual(state["next_taiyi_guide_time"], "2026-09-09 02:27:24")

    async def test_yuanying_ask_dao_updates_the_joining_avatar_only(self):
        self.join(guild="元婴宗")
        self.actor.send_and_wait_feedback_identity.return_value = "问道参悟成功，道韵萦绕。"
        await self.actor.execute_sect_daily_once(ASK, "寒续尘")
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_args.args, ("寒续尘", ".问道"))
        self.assertEqual(self.actor.get_avatar_state("寒续尘")["next_ask_dao_time"], "2026-09-09 10:00:00")
        self.assertNotIn("next_ask_dao_time", self.actor.state)
        self.assertEqual(self.actor.record_daily_reward_event.call_args.args[0], "寒续尘")

    async def test_running_scheduler_wakes_after_join_without_restart(self):
        self.actor.restore_sect_command_cooldowns = Mock()
        sent = asyncio.Event()
        async def send(*args, **kwargs):
            self.actor.is_running = False
            sent.set()
            return SUCCESS
        self.actor.send_and_wait_feedback_identity.side_effect = send
        task = asyncio.create_task(self.actor.run_sect_daily_loop())
        try:
            await asyncio.sleep(0)
            self.actor.send_and_wait_feedback_identity.assert_not_awaited()
            self.join()
            await asyncio.wait_for(sent.wait(), timeout=1)
            await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_history_requires_matching_outgoing_account_chat_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE message_events (id INTEGER PRIMARY KEY, account TEXT, chat_id INTEGER, msg_id INTEGER, reply_to_msg_id INTEGER, direction TEXT, is_game_bot INTEGER, command TEXT, identity TEXT, text TEXT, created_at TEXT)")
                rows = [
                    (1, "sub", 100, 10, None, "manual_out", 0, ".引道 水", "寒续尘", ".引道 水", "2026-09-08 14:27:24"),
                    (2, "sub", 100, 11, 10, "bot_in", 1, ".引道 水", "主魂", SUCCESS, "2026-09-08 14:27:24"),
                    (3, "sub", 200, 12, 10, "bot_in", 1, ".引道 水", "寒续尘", SUCCESS, "2026-09-08 21:00:00"),
                    (4, "main", 100, 12, 10, "bot_in", 1, ".引道 水", "寒续尘", SUCCESS, "2026-09-08 21:00:00"),
                    (5, "sub", 100, 13, None, "bot_in", 1, ".引道 水", "寒续尘", SUCCESS, "2026-09-08 21:00:00"),
                ]
                db.executemany("INSERT INTO message_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
                db.commit()
            with patch.object(sect, "MESSAGE_EVENTS_DB_FILE", path):
                self.actor.restore_sect_command_cooldowns()
                state = self.actor.get_avatar_state("寒续尘")
                self.assertEqual(state["next_taiyi_guide_time"], "2026-09-09 02:27:24")
                self.assertNotIn("next_taiyi_guide_time", self.actor.state)
                state["next_taiyi_guide_time"] = "2026-09-09 09:00:00"
                self.actor.restore_sect_command_cooldowns()
                self.assertEqual(state["next_taiyi_guide_time"], "2026-09-09 09:00:00")


if __name__ == "__main__":
    unittest.main()
