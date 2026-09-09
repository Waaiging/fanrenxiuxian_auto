import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import dashboard_server as dashboard
import hehuan_features
import log_utils
from miniapp_beast_contract import MiniAppBeastContractWorker
from miniapp_dwelling import MiniAppDwellingTransport
from sect_rules import SectTaskStopped, command_allowed, command_sect
import soul_curse_features as curse
from yinluo_features import YinluoMixin
from tests.test_miniapp_beast_contract import FakeTransport as ContractTransport
from tests.test_sect_tasks import Actor


class DynamicActor(Actor, YinluoMixin, curse.SoulCurseMixin):
    def __init__(self, account="sub"):
        super().__init__(account)
        self.config = {"miniapp_beast": {
            "entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
            "contract_interaction_delay_seconds": 0,
        }}
        self.client = SimpleNamespace(get_messages=AsyncMock())
        self.target_chat_id = 123
        self.beast_lock = asyncio.Lock()
        self.response_text = self.common_response_text
        self.atomic_hook = None

    @asynccontextmanager
    async def common_atomic_task(self, *args, **kwargs):
        if self.atomic_hook:
            self.atomic_hook()
        yield

    def join(self, identity, sect):
        self.sync_identity_sect_from_text(identity, "你成功拜入【" + sect + "】！")


class DynamicSectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = DynamicActor()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "curse.json"
        self.settings.write_text(json.dumps({"enabled": True, "identities": {}}), encoding="utf8")
        self.controls = Path(self.tmp.name) / "controls.json"
        for module, field, value in (
            (curse, "SOUL_CURSE_SETTINGS_FILE", str(self.settings)),
            (curse, "SOUL_CURSE_SHARED_FILE", str(Path(self.tmp.name) / "shared.json")),
            (dashboard, "SOUL_CURSE_SETTINGS_FILE", str(self.settings)),
            (log_utils, "COMMAND_CONTROL_FILE", str(self.controls)),
        ):
            item = patch.object(module, field, value)
            item.start()
            self.addCleanup(item.stop)
        log_utils._COMMAND_CONTROLS_CACHE["mtime"] = None

    def test_all_accounts_and_identity_types_use_live_membership(self):
        commands = {"天星宗": ".观命", "阴罗宗": ".召唤魔影", "凌霄宫": ".登天阶",
                    "万灵宗": ".寻觅灵兽", "合欢宗": ".双修 温养"}
        for account in ("main", "sub", "xiaohao", "waaiging"):
            actor = DynamicActor(account)
            for identity in ("主魂", "寒续尘"):
                for sect, command in commands.items():
                    actor.join(identity, sect)
                    self.assertTrue(command_allowed(actor, identity, command), (account, identity, sect))
                    for other, other_command in commands.items():
                        if other != sect:
                            self.assertFalse(command_allowed(actor, identity, other_command))
                    actor.dashboard_command_paused.return_value = True
                    self.assertFalse(command_allowed(actor, identity, command))
                    actor.dashboard_command_paused.return_value = False

    async def test_manager_dispatches_each_new_sect(self):
        for sect, handler in (("天星宗", "observe_tianxing_destiny"), ("阴罗宗", "yinluo_tick"),
                              ("凌霄宫", "lingxiao_tick"), ("万灵宗", "run_wanling_identity_once"),
                              ("合欢宗", "execute_dual_cultivation_once")):
            actor = DynamicActor()
            actor.join("寒续尘", sect)
            actor._sect_identity_signals = {"寒续尘": asyncio.Event()}
            actor.daily_one_shot_should_defer = Mock(return_value=False)
            async def finish(identity):
                actor.is_running = False
                return 60
            callback = AsyncMock(side_effect=finish)
            setattr(actor, handler, callback)
            await actor.run_sect_identity_features("寒续尘")
            callback.assert_awaited_once_with("寒续尘")
            self.assertEqual(actor.get_avatar_state("寒续尘")["sect_task_runtime"]["sect"], sect)

    async def test_reconcile_and_rename_keep_one_manager_per_identity(self):
        gate = asyncio.Event()
        async def wait_forever(*_):
            await gate.wait()
        self.actor.run_sect_identity_features = wait_forever
        self.actor.reconcile_sect_features()
        original = dict(self.actor._sect_identity_tasks)
        self.actor.reconcile_sect_features()
        self.actor.refresh_avatar_dao_name("寒续尘", "新道号", player_id=-1003340352216)
        self.actor.reconcile_sect_features()
        self.assertEqual(original, self.actor._sect_identity_tasks)
        self.assertEqual(len(original), 2)
        for task in original.values():
            task.cancel()
        await asyncio.gather(*original.values(), return_exceptions=True)

    async def test_stopping_parent_cancels_all_identity_managers(self):
        self.actor.restore_sect_command_cooldowns = Mock()
        parent = asyncio.create_task(self.actor.run_sect_daily_loop())
        await asyncio.sleep(0)
        tasks = list(self.actor._sect_identity_tasks.values())
        parent.cancel()
        await asyncio.gather(parent, return_exceptions=True)
        self.assertTrue(tasks)
        self.assertTrue(all(task.done() for task in tasks))

    async def test_tianji_rechecks_membership_after_atomic_wait(self):
        self.actor.join("寒续尘", "天星宗")
        self.actor.ensure_tianxing_destiny_for_action = AsyncMock(return_value=True)
        self.actor.atomic_hook = lambda: self.actor.join("寒续尘", "合欢宗")
        transport = SimpleNamespace(command=AsyncMock(), forge_treasure=AsyncMock())
        self.assertFalse(await self.actor._run_tianxing_tianji_identity_round("寒续尘", 1, "round", transport))
        transport.command.assert_not_awaited()
        transport.forge_treasure.assert_not_awaited()

    async def test_tianji_stops_after_prefix_snapshot_changes_sect(self):
        self.actor.join("寒续尘", "天星宗")
        self.actor.ensure_tianxing_destiny_for_action = AsyncMock(return_value=True)
        self.actor.tianxing_prefix_response_ok = Mock(return_value=True)
        response = SimpleNamespace(payload={"account": {"profile": {"sectName": "合欢宗"}}}, text="推命成功")
        transport = SimpleNamespace(command=AsyncMock(return_value=response), forge_treasure=AsyncMock())
        await self.actor._run_tianxing_tianji_identity_round("寒续尘", 1, "round", transport)
        transport.command.assert_awaited_once()
        transport.forge_treasure.assert_not_awaited()

    async def test_transport_checks_membership_after_authentication(self):
        self.actor.join("寒续尘", "凌霄宫")
        transport = MiniAppDwellingTransport(object(), self.actor.config["miniapp_beast"]["entry_url"], post_json=AsyncMock())
        transport.sect_actor = self.actor
        async def initialize(*args, **kwargs):
            self.actor.join("寒续尘", "太一门")
        transport._initialize_unlocked = initialize
        with self.assertRaises(SectTaskStopped):
            await transport.command(".引九天罡风", identity="寒续尘")
        transport.post_json.assert_not_awaited()

    async def test_external_action_checks_membership_after_token_wait(self):
        self.actor.join("寒续尘", "万灵宗")
        transport = MiniAppDwellingTransport(object(), self.actor.config["miniapp_beast"]["entry_url"], post_json=AsyncMock())
        transport.sect_actor = self.actor
        async def token(*args, **kwargs):
            self.actor.join("寒续尘", "凌霄宫")
            return "fixture"
        transport._external_token_unlocked = token
        with self.assertRaises(SectTaskStopped):
            await transport.spirit_beast_interaction("寒续尘", 1, "安抚")
        transport.post_json.assert_not_awaited()

    async def test_tianji_forge_rechecks_membership_after_transport_wait(self):
        self.actor.join("寒续尘", "天星宗")
        transport = MiniAppDwellingTransport(object(), self.actor.config["miniapp_beast"]["entry_url"], post_json=AsyncMock())
        transport.sect_actor = self.actor
        async def initialize(*args, **kwargs):
            self.actor.join("寒续尘", "太一门")
        transport._initialize_unlocked = initialize
        with self.assertRaises(SectTaskStopped):
            await transport.forge_treasure("寒续尘", "treasure_001", required_command=".推命 炼制")
        transport.post_json.assert_not_awaited()

    def test_transport_resolves_renamed_identity_to_same_player(self):
        transport = MiniAppDwellingTransport(object(), self.actor.config["miniapp_beast"]["entry_url"])
        transport.sect_actor = self.actor
        self.actor.refresh_avatar_dao_name("寒续尘", "新道号", player_id=-1003340352216)
        transport.identity_player_ids = {"新道号": -1003340352216, "主魂": 1}
        self.assertEqual(transport.player_id("寒续尘"), -1003340352216)

    async def test_new_tianxing_identity_gets_prefix_without_duplicating_existing_prefix(self):
        self.actor.join("寒续尘", "天星宗")
        self.actor.tianxing_prefix_response_ok = Mock(return_value=True)
        for explicit in (False, True):
            post = AsyncMock(return_value={"actionResult": {"ok": True, "message": "推命成功"}})
            transport = MiniAppDwellingTransport(object(), self.actor.config["miniapp_beast"]["entry_url"], post_json=post)
            transport.sect_actor = self.actor
            transport.init_data = "fixture"
            transport.start_payload = {"ok": True}
            transport.identity_player_ids = {"寒续尘": -2}
            if explicit:
                await transport.command(".推命 闭关", identity="寒续尘")
            await transport.command(".闭关修炼", identity="寒续尘", meditation_prefix=True)
            self.assertEqual(post.await_count, 2)
            calls = [item.args[1] for item in post.await_args_list]
            self.assertEqual(calls, ["/api/miniapp/xianxia-dwelling/command-center", "/api/miniapp/xianxia-dwelling/cultivation"])

    async def test_meditation_stops_if_paused_after_prefix(self):
        self.actor.join("寒续尘", "天星宗")
        self.actor.tianxing_prefix_response_ok = Mock(return_value=True)
        async def post(*args, **kwargs):
            self.actor.dashboard_command_paused.return_value = True
            return {"actionResult": {"ok": True, "message": "推命成功"}}
        transport = MiniAppDwellingTransport(object(), self.actor.config["miniapp_beast"]["entry_url"], post_json=AsyncMock(side_effect=post))
        transport.sect_actor = self.actor
        transport.init_data = "fixture"
        transport.start_payload = {"ok": True}
        transport.identity_player_ids = {"寒续尘": -2}
        with self.assertRaises(SectTaskStopped):
            await transport.command(".闭关修炼", identity="寒续尘", meditation_prefix=True)
        self.assertEqual(transport.post_json.await_count, 1)

    async def test_contract_cooldowns_are_independent_for_main_and_avatar(self):
        for identity in ("主魂", "寒续尘"):
            self.actor.join(identity, "万灵宗")
        transport = ContractTransport()
        for identity in ("主魂", "寒续尘"):
            worker = MiniAppBeastContractWorker(self.actor, transport, Mock(), identity=identity)
            await worker.run_cycle()
            await worker.run_cycle()
        self.assertEqual([call[0] for call in transport.calls], ["主魂", "主魂", "寒续尘", "寒续尘"])
        avatar = self.actor.get_avatar_state("寒续尘")
        self.assertIsNot(self.actor.state["beast_contract_interaction_results"], avatar["beast_contract_interaction_results"])

    async def test_contract_stops_after_first_beast_and_resumes_without_repeating(self):
        self.actor.join("寒续尘", "万灵宗")
        transport = ContractTransport()
        send = transport.spirit_beast_interaction
        async def change_after_first(*args):
            result = await send(*args)
            self.actor.join("寒续尘", "太一门")
            return result
        transport.spirit_beast_interaction = change_after_first
        worker = MiniAppBeastContractWorker(self.actor, transport, Mock(), identity="寒续尘")
        with self.assertRaises(SectTaskStopped):
            await worker.run_cycle()
        self.assertEqual([call[1] for call in transport.calls], [1])
        self.actor.join("寒续尘", "万灵宗")
        transport.spirit_beast_interaction = send
        await worker.run_cycle()
        self.assertEqual([call[1] for call in transport.calls], [1, 2])

    async def test_yinluo_rechecks_after_waiting_for_other_atomic_task(self):
        self.actor.join("寒续尘", "阴罗宗")
        self.actor.active_atomic_task = object()
        async def release(*args):
            self.actor.join("寒续尘", "太一门")
            self.actor.active_atomic_task = None
        with patch("yinluo_features.asyncio.sleep", new=release):
            with self.assertRaises(SectTaskStopped):
                await self.actor.send_yinluo_command("寒续尘", ".血洗山林", edited_wait=6)
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    async def test_lingxiao_stops_climb_when_membership_changes_after_wind(self):
        self.actor.join("寒续尘", "凌霄宫")
        self.actor.get_avatar_state("寒续尘")["cloud_stairs_progress"] = "1 / 12 阶"
        async def respond(identity, command, **kwargs):
            self.actor.join(identity, "合欢宗")
            return "引九天罡风成功"
        self.actor.send_and_wait_feedback_identity.side_effect = respond
        with patch("lingxiao_features.asyncio.sleep", new=AsyncMock()):
            await self.actor.lingxiao_tick("寒续尘")
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_count, 1)
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_args.args[1], ".引九天罡风")

    async def test_hehuan_main_waits_for_partner_configuration(self):
        self.actor.account_key = "main"
        self.actor.join("主魂", "合欢宗")
        self.assertFalse(await self.actor.execute_dual_cultivation_once())
        self.assertEqual(self.actor.state["dual_cultivation_last_status"], "target_unconfigured")
        self.actor.client.get_messages.assert_not_awaited()
        self.actor.send_and_wait_feedback.assert_not_awaited()

    async def test_hehuan_stops_if_paused_during_target_lookup(self):
        self.actor.join("寒续尘", "合欢宗")
        async def lookup(identity):
            self.actor.dashboard_command_paused.return_value = True
            return SimpleNamespace(id=10)
        self.actor.find_dual_cultivation_target = lookup
        self.assertFalse(await self.actor.execute_dual_cultivation_once("寒续尘"))
        self.actor.send_and_wait_feedback_identity.assert_not_awaited()

    def test_soul_curse_discovers_any_yinluo_but_preserves_explicit_false(self):
        self.actor.join("主魂", "阴罗宗")
        self.actor.join("寒续尘", "阴罗宗")
        self.assertEqual(self.actor.soul_curse_yinluo_identities(), ["主魂", "寒续尘"])
        self.assertIsNone(self.actor.soul_curse_publisher_profile())
        self.assertTrue(self.actor.soul_curse_assistant_enabled("寒续尘"))
        self.assertFalse(self.actor.soul_curse_identity_enabled(identity="寒续尘"))
        self.settings.write_text(json.dumps({"enabled": True, "identities": {"sub": {"寒续尘": False}}}), encoding="utf8")
        self.assertFalse(self.actor.soul_curse_assistant_enabled("寒续尘"))
        self.assertTrue(self.actor.soul_curse_assistant_enabled("主魂"))
        self.settings.write_text("{", encoding="utf8")
        self.assertFalse(self.actor.soul_curse_assistant_enabled("主魂"))

    def test_soul_curse_new_member_does_not_inherit_another_members_switch(self):
        self.actor.account_key = "main"
        self.actor.join("寒续尘", "阴罗宗")
        self.settings.write_text(json.dumps({"enabled": True, "identities": {"main": {"缘生子": False}}}), encoding="utf8")
        self.assertTrue(self.actor.soul_curse_assistant_enabled("寒续尘"))
        self.assertNotIn("缘生子", self.actor.soul_curse_identity_setting_candidates("main", "寒续尘"))

    async def test_soul_curse_stops_retry_after_leaving(self):
        self.actor.join("寒续尘", "阴罗宗")
        self.actor.send_and_wait_feedback_identity.return_value = None
        async def leave(*args):
            self.actor.join("寒续尘", "合欢宗")
        with patch.object(curse.asyncio, "sleep", new=leave):
            await self.actor.soul_curse_send_identity("寒续尘", ".接取解咒委托 123")
        self.assertEqual(self.actor.send_and_wait_feedback_identity.await_count, 1)

    def test_commission_is_claimed_by_exactly_one_identity(self):
        curse.upsert_soul_curse_shared_commission("main", {"commission_id": "123", "status": "published"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda identity: curse.claim_soul_curse_commission("main", "123", "sub", identity), ["甲", "乙"]))
        self.assertEqual(sorted(results), [False, True])
        self.assertIn(curse.read_soul_curse_shared_state()["main"]["assistant_identity"], {"甲", "乙"})

    def test_dashboard_shows_only_current_sect_tasks_for_every_account(self):
        for account in ("main", "sub", "xiaohao", "waaiging"):
            for identity in ("主魂", "寒续尘"):
                for sect in ("天星宗", "阴罗宗", "凌霄宫", "万灵宗", "合欢宗"):
                    actor = DynamicActor(account)
                    actor.join(identity, sect)
                    with patch.object(dashboard, "load_custom_commands", return_value={}), patch.object(dashboard, "load_command_controls", return_value={}):
                        panels = dashboard.build_command_panels(account, actor.state)
                    rows = next(panel["commands"] for panel in panels if panel["identity"] == identity)
                    sects = {command_sect(row["command"]) for row in rows if command_sect(row["command"])}
                    self.assertEqual(sects, {sect}, (account, identity, sect))

    async def test_partner_editor_and_pause_toggle_preserve_each_other(self):
        self.actor.join("寒续尘", "合欢宗")
        with patch.object(dashboard, "get_state", return_value=self.actor.state), patch.object(dashboard, "command_control_path", return_value=str(self.controls)):
            result = await dashboard.set_dual_cultivation_target({"account": "sub", "identity": "寒续尘", "target_username": "@ExampleUser"}, username="test")
            self.assertTrue(result["success"])
            await dashboard.set_command_control({"account": "sub", "identity": "寒续尘", "command": ".双修 温养", "disabled": True}, username="test")
            await dashboard.set_command_control({"account": "sub", "identity": "寒续尘", "command": ".双修 温养", "disabled": False}, username="test")
            self.assertEqual(self.actor.dual_cultivation_target("寒续尘"), "ExampleUser")
            entry = json.loads(self.controls.read_text(encoding="utf8"))["sub"]["寒续尘"][".双修 温养"]
            self.assertFalse(entry["disabled"])
            self.assertEqual(entry["target_username"], "ExampleUser")
            result = await dashboard.set_dual_cultivation_target({"account": "main", "identity": "主魂", "target_username": "Weeguu"}, username="test")
            self.assertFalse(result["success"])

    async def test_rename_preserves_controls_and_current_name_resume_wins(self):
        self.actor.join("寒续尘", "合欢宗")
        self.controls.write_text(json.dumps({"sub": {"寒续尘": {
            ".登天阶": {"disabled": True},
            ".双修 温养": {"disabled": True, "target_username": "ExampleUser"},
        }}}), encoding="utf8")
        self.actor.refresh_avatar_dao_name("寒续尘", "新道号", player_id=-1003340352216)
        self.assertTrue(log_utils.dashboard_command_disabled(self.actor, ".登天阶", "新道号")[0])
        self.assertEqual(self.actor.dual_cultivation_target("新道号"), "ExampleUser")
        with patch.object(dashboard, "get_state", return_value=self.actor.state), patch.object(
            dashboard, "command_control_path", return_value=str(self.controls)
        ):
            rows = next(panel["commands"] for panel in dashboard.build_command_panels("sub", self.actor.state)
                        if panel["identity"] == "新道号")
            row = next(row for row in rows if row["command"] == ".双修 温养")
            self.assertTrue(row["control_disabled"])
            self.assertEqual(row["dual_cultivation_target"], "ExampleUser")
            for command in (".登天阶", ".双修 温养"):
                result = await dashboard.set_command_control({
                    "account": "sub", "identity": "新道号", "command": command, "disabled": False,
                }, username="test")
                self.assertTrue(result["success"])
                self.assertFalse(log_utils.dashboard_command_disabled(self.actor, command, "寒续尘")[0])
            self.actor.refresh_avatar_dao_name("新道号", "再次改名", player_id=-1003340352216)
            self.assertEqual(self.actor.dual_cultivation_target("再次改名"), "ExampleUser")
            self.assertFalse(log_utils.dashboard_command_disabled(self.actor, ".登天阶", "再次改名")[0])

    async def test_editing_partner_after_rename_keeps_inherited_pause(self):
        self.actor.join("寒续尘", "合欢宗")
        self.controls.write_text(json.dumps({"sub": {"寒续尘": {
            ".双修 温养": {"disabled": True, "target_username": "ExampleUser"},
        }}}), encoding="utf8")
        self.actor.refresh_avatar_dao_name("寒续尘", "新道号", player_id=-1003340352216)
        with patch.object(dashboard, "get_state", return_value=self.actor.state), patch.object(
            dashboard, "command_control_path", return_value=str(self.controls)
        ):
            result = await dashboard.set_dual_cultivation_target({
                "account": "sub", "identity": "新道号", "target_username": "AnotherUser",
            }, username="test")
        self.assertTrue(result["success"])
        self.assertEqual(self.actor.dual_cultivation_target("新道号"), "AnotherUser")
        self.assertTrue(log_utils.dashboard_command_disabled(self.actor, ".双修 温养", "新道号")[0])

    def test_renamed_identity_cannot_override_account_wide_pause(self):
        self.actor.refresh_avatar_dao_name("寒续尘", "新道号", player_id=-1003340352216)
        controls = {"sub": {
            "寒续尘": {".登天阶": {"disabled": True}},
            "新道号": {".登天阶": {"disabled": False}},
            "*": {".登天阶": {"disabled": True}},
        }}
        with patch.object(log_utils, "load_command_controls", return_value=controls):
            self.assertTrue(log_utils.dashboard_command_disabled(self.actor, ".登天阶", "新道号")[0])
        self.assertTrue(dashboard.command_control_disabled(
            controls, "sub", "新道号", ".登天阶", root_state=self.actor.state,
        ))
        controls["sub"].pop("*")
        with patch.object(log_utils, "load_command_controls", return_value=controls):
            self.assertFalse(log_utils.dashboard_command_disabled(self.actor, ".登天阶", "新道号")[0])
        self.assertFalse(dashboard.command_control_disabled(
            controls, "sub", "新道号", ".登天阶", root_state=self.actor.state,
        ))

    def test_main_soul_sect_prefers_persisted_membership_over_default(self):
        self.actor.sect_name = "元婴宗"
        self.actor.join("主魂", "合欢宗")
        self.actor.sect_name = "元婴宗"
        self.assertEqual(self.actor.account_sect_name(), "合欢宗")

    def test_restricted_mode_disables_group_only_sect_actions(self):
        self.actor._restricted_miniapp_worker = object()
        for sect, command in (("合欢宗", ".双修 温养"), ("太一门", ".引道 水"), ("阴罗宗", ".接取解咒委托 12")):
            self.actor.join("寒续尘", sect)
            self.assertFalse(self.actor.sect_operation_allowed("寒续尘", command))
        self.actor.join("寒续尘", "凌霄宫")
        self.assertTrue(self.actor.sect_operation_allowed("寒续尘", ".登天阶"))


if __name__ == "__main__":
    unittest.main()
