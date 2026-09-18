"""Regressions found by comparing running schedules with command receipts."""
import asyncio
import copy
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import dashboard_server as dashboard
from cultivator_xiaohao import CultivatorXiaoHao
from intelligent_cultivator import Cultivator
from miniapp_command_routing import MiniAppCommandRouter
from soul_curse_features import SOUL_CURSE_INFER_COMMAND, SOUL_CURSE_PROTECT_COMMAND, SOUL_CURSE_PUBLISH_COMMAND
from sub_cultivator import SubCultivator
from tests.test_log_recovery import YuanyingActor
from tests.test_miniapp_journey import FakeActor, FakeLogger, SequenceTransport


class PublisherScheduleDisplayTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 18, 10)
        clock = patch.object(dashboard, "datetime", wraps=datetime)
        clock.start().now.return_value = self.now
        self.addCleanup(clock.stop)
        enabled = patch.object(dashboard, "soul_curse_identity_enabled_for_dashboard", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.state = {"soul_curse": {
            "chain_stage": "done",
            "next_chain_time": "2026-09-11 05:48:41",
            "next_infer_time": "2026-09-18 17:15:00",
            "next_protect_time": "2026-09-18 17:16:00",
        }}

    def rows(self, controls=None, identity="主魂", root_state=None):
        with patch.object(dashboard, "load_command_controls", return_value=controls or {}):
            return {row["command"]: row for row in dashboard.soul_curse_publisher_commands(
                self.state, "main", identity, root_state=root_state,
            )}

    def test_inference_uses_its_confirmed_cooldown(self):
        rows = self.rows()
        self.assertEqual(rows[SOUL_CURSE_INFER_COMMAND]["at"], "2026-09-18 17:15:00")
        self.assertEqual(rows[SOUL_CURSE_INFER_COMMAND]["tone"], "cooldown")

    def test_paused_publication_does_not_override_personal_deadlines(self):
        self.state["soul_curse"]["next_chain_time"] = "2026-09-18 11:00:00"
        controls = {"main": {"主魂": {SOUL_CURSE_PUBLISH_COMMAND: {"disabled": True}}}}
        before = copy.deepcopy(self.state)
        rows = self.rows(controls)
        self.assertEqual(rows[SOUL_CURSE_INFER_COMMAND]["at"], "2026-09-18 17:15:00")
        self.assertEqual(rows[SOUL_CURSE_PROTECT_COMMAND]["at"], "2026-09-18 17:16:00")
        self.assertEqual(self.state, before)

    def test_enabled_publication_preserves_chain_cooldown(self):
        self.state["soul_curse"]["next_chain_time"] = "2026-09-18 11:00:00"
        rows = self.rows()
        for command in (SOUL_CURSE_INFER_COMMAND, SOUL_CURSE_PROTECT_COMMAND, SOUL_CURSE_PUBLISH_COMMAND):
            self.assertEqual(rows[command]["at"], "2026-09-18 11:00:00")
            self.assertEqual(rows[command]["status"], "整链冷却")

    def test_paused_publication_honors_old_dao_name_control(self):
        self.state["soul_curse"]["next_chain_time"] = "2026-09-18 11:00:00"
        root = {"avatar_dao_name_aliases": {"缘生子": "玄续子"}}
        controls = {"main": {"缘生子": {SOUL_CURSE_PUBLISH_COMMAND: {"disabled": True}}}}
        rows = self.rows(controls, "玄续子", root)
        self.assertEqual(rows[SOUL_CURSE_INFER_COMMAND]["at"], "2026-09-18 17:15:00")

    def test_due_independent_protection_is_not_labeled_as_chain_only(self):
        self.state["soul_curse"]["next_protect_time"] = "2026-09-18 09:00:00"
        controls = {"main": {"主魂": {SOUL_CURSE_PUBLISH_COMMAND: {"disabled": True}}}}
        self.assertEqual(self.rows(controls)[SOUL_CURSE_PROTECT_COMMAND]["status"], "可护持")


class XiaoHaoJourneyOwnershipTests(unittest.TestCase):
    def make_actor(self):
        identities = ["主魂", "问心子", "素心子", "灵脉玄"]
        actor = FakeActor("xiaohao", identities[1:], {name: "万灵宗" for name in identities})
        transport = SequenceTransport(daily_limit=8)
        transport.identity_player_ids = {name: number for number, name in enumerate(identities, 100)}
        logger = FakeLogger()
        router = MiniAppCommandRouter(actor, "xiaohao", logger, transport=transport, start_background_tasks=False)
        router.star_farm_identities = lambda: []
        # Profile synchronization is independently tested and owned by this worker.
        router.run_profile_sync_loop = None
        actor._miniapp_command_router = router
        actor._miniapp_daily_activities = None
        actor._scheduler_task_registry = {}
        registered = {}

        def register(name, factory):
            registered[name] = factory
            actor._scheduler_task_registry[name] = SimpleNamespace(done=lambda: False)

        actor.create_scheduler_task = Mock(side_effect=register)
        return actor, router, transport, logger, registered

    def test_full_worker_registers_router_journey_once_and_reuses_transport(self):
        actor, router, transport, _, registered = self.make_actor()
        runner = router.tianxing_journey.run_loop = AsyncMock()
        CultivatorXiaoHao.start_miniapp_scheduler_tasks(actor)
        CultivatorXiaoHao.start_miniapp_scheduler_tasks(actor)
        self.assertIn("miniapp_journey", registered)
        asyncio.run(registered["miniapp_journey"]())
        runner.assert_awaited_once()
        self.assertIs(router.tianxing_journey.transport, transport)
        self.assertFalse(router.start_background_tasks)
        self.assertEqual(actor.create_scheduler_task.call_count, 1)

    def test_full_worker_journey_runs_all_four_selected_identities(self):
        actor, router, transport, logger, registered = self.make_actor()
        CultivatorXiaoHao.start_miniapp_scheduler_tasks(actor)
        self.assertIn("miniapp_journey", registered)

        now = datetime(2026, 9, 18, 10)

        async def end_cycle(seconds):
            nonlocal now
            now += timedelta(seconds=seconds)
            actor.is_running = not all(transport.counts.get(name) == 8 for name in transport.identity_player_ids)

        with patch("miniapp_journey.miniapp_journey_identities_for_account", return_value=list(transport.identity_player_ids)), \
             patch("miniapp_journey.miniapp_journey_settings", return_value={"enabled": True}), \
             patch("miniapp_journey.datetime", wraps=datetime) as clock, \
             patch("miniapp_journey.asyncio.sleep", side_effect=end_cycle):
            clock.now.side_effect = lambda: now
            asyncio.run(registered["miniapp_journey"]())
        self.assertEqual(transport.counts, {identity: 8 for identity in transport.identity_player_ids})
        self.assertEqual(len([text for text in logger.info_messages if "野外历练" in text]), 4)

    def test_disabled_journey_loop_can_observe_later_participation_changes(self):
        actor, router, _, _, registered = self.make_actor()
        CultivatorXiaoHao.start_miniapp_scheduler_tasks(actor)
        self.assertIn("miniapp_journey", registered)
        run_once = router.tianxing_journey.run_once = AsyncMock(return_value=(0, 60))
        enabled = {"enabled": False}
        cycles = []

        async def advance(_):
            cycles.append(enabled["enabled"])
            if enabled["enabled"]:
                actor.is_running = False
            enabled["enabled"] = True

        with patch("miniapp_journey.miniapp_journey_identities_for_account", return_value=["主魂"]), \
             patch("miniapp_journey.miniapp_journey_settings", side_effect=lambda: enabled), \
             patch("miniapp_journey.asyncio.sleep", side_effect=advance):
            asyncio.run(registered["miniapp_journey"]())
        run_once.assert_awaited_once()
        self.assertEqual(cycles, [False, True])


class EventScheduleDisplayTests(unittest.TestCase):
    def test_expired_formation_cooldown_waits_for_invitation(self):
        state = {"next_formation_time": "2026-09-13 18:36:02", "sect_name": "星宫"}
        rows = dashboard.xiaohao_avatar_commands("素心子", state)
        row = next(row for row in rows if row["command"] == ".助阵")
        self.assertEqual(row["status"], "等待邀请")
        self.assertIsNone(row.get("next_seconds"))
        self.assertEqual(row.get("schedule_type"), "event")

    def test_formation_still_displays_real_cooldown(self):
        state = {"next_formation_time": (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), "sect_name": "星宫"}
        row = next(row for row in dashboard.xiaohao_avatar_commands("素心子", state) if row["command"] == ".助阵")
        self.assertEqual(row["tone"], "cooldown")
        self.assertGreater(row["next_seconds"], 0)

    def assist_rows(self, state, shared, root=None):
        with patch.object(dashboard, "read_soul_curse_shared_state", return_value=shared, create=True), \
             patch.object(dashboard, "soul_curse_identity_enabled_for_dashboard", return_value=True):
            return dashboard.soul_curse_assist_commands(
                state, "main", "玄续子", root_state=root, include_switch=False,
            )

    def test_assist_uses_latest_owner_receipt_when_waiting_for_commission(self):
        state = {
            "soul_curse_assist": {"commission_id": "100", "last_detail": "旧委托资源不足", "updated_at": "2026-09-11 10:00:00"},
            "soul_curse_assists": {"waaiging": {
                "owner_account": "waaiging", "commission_id": "200", "completed_commission_id": "200",
                "target_username": "@example", "status": "completed", "last_detail": "剥离咒源完成，委托 200",
                "updated_at": "2026-09-18 09:00:00",
            }},
        }
        before = copy.deepcopy(state)
        rows = self.assist_rows(state, {})
        for row in rows:
            self.assertEqual(row["status"], "等待委托")
            self.assertIn("委托 200", row["detail"])
            self.assertNotIn("旧委托资源不足", row["detail"])
            self.assertIsNone(row.get("next_seconds"))
        self.assertEqual(state, before)

    def test_assigned_live_commission_takes_priority_over_newer_history(self):
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        state = {"soul_curse_assists": {
            "sub": {"owner_account": "sub", "commission_id": "100", "target_username": "@active", "next_identify_time": future},
            "waaiging": {"owner_account": "waaiging", "commission_id": "200", "updated_at": "2026-09-18 09:00:00", "status": "completed"},
        }}
        shared = {"sub": {"commission_id": "100", "assistant_account": "main", "assistant_identity": "玄续子", "status": "accepted"}}
        rows = self.assist_rows(state, shared)
        row = next(row for row in rows if row["command"].startswith(".辨认咒纹"))
        self.assertIn("@active", row["command"])
        self.assertEqual(row["tone"], "cooldown")
        self.assertEqual(row["at"], future)

    def test_other_assistant_and_terminal_commissions_do_not_create_due_work(self):
        shared = {
            "sub": {"commission_id": "100", "assistant_account": "sub", "assistant_identity": "岚衍子", "status": "accepted"},
            "waaiging": {"commission_id": "200", "assistant_account": "main", "assistant_identity": "玄续子", "status": "completed"},
        }
        for row in self.assist_rows({}, shared):
            self.assertEqual(row["status"], "等待委托")

    def test_account_local_live_commission_is_still_displayed(self):
        state = {"soul_curse_assist": {"owner_account": "main", "commission_id": "100", "target_username": "@local"}}
        root = {"soul_curse": {"commission_id": "100", "commission_status": "published"}}
        rows = self.assist_rows(state, {}, root)
        self.assertNotEqual(rows[0]["status"], "等待委托")
        self.assertTrue(any("@local" in row["command"] for row in rows))


class YuanyingRetreatMembershipTests(unittest.TestCase):
    def actor(self, sect):
        actor = YuanyingActor()
        actor.account_key = "sub"
        actor.avatars = []
        actor.yuanying_main_command = ".元婴闭关"
        actor.identity_sect_names = {"主魂": sect}
        actor._wait_for_main_identity = AsyncMock()
        actor.save_state = Mock()
        actor.parse_wait_time = lambda text: Cultivator.parse_wait_time(actor, text)
        actor.send_timed_command_plan = AsyncMock(return_value="开始闭关，持续提供修为。")
        return actor

    def test_wrong_or_unknown_sect_skips_without_changing_cooldown(self):
        for sect in ("落云宗", ""):
            with self.subTest(sect=sect):
                actor = self.actor(sect)
                actor.state["next_yuanying_out_time"] = "2026-09-01 00:00:00"
                before = copy.deepcopy(actor.state)
                self.assertEqual(asyncio.run(actor.common_main_yuanying_out_tick()), 300)
                actor.send_timed_command_plan.assert_not_awaited()
                self.assertEqual(actor.state, before)
                self.assertFalse(actor.sect_command_allowed(".元婴闭关", "主魂"))

    def test_current_profile_allows_retreat_after_rejoining_sect(self):
        actor = self.actor("落云宗")
        asyncio.run(actor.common_main_yuanying_out_tick())
        actor.identity_sect_names["主魂"] = "元婴宗"
        self.assertEqual(asyncio.run(actor.common_main_yuanying_out_tick()), 5)
        actor.send_timed_command_plan.assert_awaited_once()
        self.assertTrue(actor.state["yuanying_out_active"])

    def test_ongoing_retreat_still_waits_for_its_settlement(self):
        actor = self.actor("落云宗")
        actor.state.update(yuanying_out_active=True, next_yuanying_out_time="", yuanying_out_end_time="")
        self.assertEqual(asyncio.run(actor.common_main_yuanying_out_tick()), 600)
        self.assertTrue(actor.state["yuanying_out_active"])
        actor.send_timed_command_plan.assert_not_awaited()

    def test_sect_change_during_settlement_prevents_second_send(self):
        actor = self.actor("元婴宗")

        async def settle(*args):
            actor.identity_sect_names["主魂"] = "落云宗"
            return "元婴闭关结算，修为增加。"

        actor.send_timed_command_plan.side_effect = settle
        with patch("common_command_features.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(asyncio.run(actor.common_main_yuanying_out_tick()), 300)
        actor.send_timed_command_plan.assert_awaited_once()
        self.assertFalse(actor.state["yuanying_out_active"])

    def test_watchdog_does_not_restart_worker_for_ineligible_retreat(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.state = {"next_yuanying_out_time": "2026-09-01 00:00:00"}
        actor.avatars = []
        actor.main_star_palace_enabled = False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False
        actor.identity_sect_name = lambda identity: "落云宗"
        self.assertEqual(actor.stale_scheduler_due_items(), [])

    def test_dashboard_keeps_control_and_explains_membership_requirement(self):
        state = {"sect_name": "落云宗", "identity_sect_names": {"主魂": "落云宗"}}
        rows = dashboard.build_command_panels("sub", state)[0]["commands"]
        row = next(row for row in rows if row["command"] == ".元婴闭关")
        self.assertEqual(row["status"], "宗门不符")
        self.assertIn("落云宗", row["detail"])
        self.assertIsNone(row.get("next_seconds"))
        self.assertFalse(row["actionable"])
        self.assertTrue(row["controllable"])


if __name__ == "__main__":
    unittest.main()
