import asyncio
import copy
import json
import logging
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import dashboard_server as dashboard
import log_utils
from automation_command_controls import CommandControlPaused, FISHING, HUNT, JOURNEY, PAGODA, TRIAL, FATE_CARDS, WORLD_BOSS
from miniapp_dwelling import MiniAppDwellingTransport
from miniapp_daily_activities import MiniAppDailyActivities
from miniapp_journey import MiniAppTianxingJourney
from miniapp_fishing import MiniAppFishingAutomation
from world_boss_features import WorldBossMonitor


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"


class ControlFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "command_controls.json"
        self.state = {"sect_name": "天星宗", "avatars": {"化身甲": {}, "化身乙": {}},
                      "identity_sect_names": {"主魂": "天星宗", "化身甲": "天星宗", "化身乙": "万灵宗"}}
        self.actor = SimpleNamespace(account_key="main", state=self.state, avatars=["化身甲", "化身乙"],
                                     config={"miniapp_beast": {"entry_url": ENTRY}}, client=Mock(),
                                     current_identity="主魂", save_state=Mock(), identity_pause_seconds=lambda identity: 0)
        self.actor.get_avatar_state = lambda identity: self.state["avatars"][identity]
        self.actor.dashboard_command_paused = lambda command, identity: log_utils.dashboard_command_disabled(self.actor, command, identity)[0]
        for obj, name, value in (
            (log_utils, "COMMAND_CONTROL_FILE", str(self.path)),
            (dashboard, "CONFIG_DIR", str(self.directory)),
            (dashboard, "get_state", lambda account: self.state),
            (dashboard, "load_custom_commands", lambda: {}),
        ):
            changer = patch.object(obj, name, value)
            changer.start()
            self.addCleanup(changer.stop)

    def controls(self, data):
        dashboard.save_command_controls(data)

    def pause(self, command, identity="主魂", account="main", disabled=True):
        return asyncio.run(dashboard.set_command_control({
            "account": account, "identity": identity, "command": command,
            "control_key": log_utils.command_control_key(command), "disabled": disabled,
        }, username="test"))

    def paused(self, command, identity="主魂"):
        return log_utils.dashboard_command_disabled(self.actor, command, identity)[0]


class IdentityCommandControlTests(ControlFixture, unittest.TestCase):
    def test_parameter_templates_control_real_arguments_and_reply_only_duels(self):
        for template, concrete in (
            (".定命 <命星>", ".定命 紫微"), (".斗法 <目标>", ".斗法"),
            (".上架 <物品及价格>", ".上架 风雷翅 100"),
            (".收取精华 <槽位>", ".收取精华 8"),
            (".化功为煞 <数量>", ".化功为煞 10000"),
            (".改换星移 <目标>", ".改换星移 @someone"),
            (".作答 <选项>", ".作答 A"),
        ):
            with self.subTest(template=template):
                self.controls({})
                self.assertTrue(self.pause(template, "化身甲")["disabled"])
                self.assertTrue(self.paused(concrete, "化身甲"))
                self.assertFalse(self.paused(concrete, "化身乙"))
                self.assertFalse(self.paused(concrete + "测试", "主魂"))
                self.assertFalse(self.pause(template, "化身甲", disabled=False)["disabled"])
                self.assertFalse(self.paused(concrete, "化身甲"))

    def test_switch_control_belongs_to_destination_identity(self):
        self.pause(".切换 化身甲", "化身甲")
        self.assertTrue(self.paused(".切换 化身甲", "主魂"))
        self.assertFalse(self.paused(".切换 化身乙", "化身甲"))
        self.assertFalse(self.paused(".状态", "化身甲"))

    def test_same_named_identity_in_another_account_is_independent(self):
        self.pause(PAGODA, "化身甲", account="sub")
        self.assertFalse(self.paused(PAGODA, "化身甲"))
        self.actor.account_key = "sub"
        self.assertTrue(self.paused(PAGODA, "化身甲"))

    def test_rebirth_alias_keeps_pause_and_allows_explicit_resume(self):
        self.state["avatar_dao_name_aliases"] = {"旧道号": "化身甲"}
        self.controls({"main": {"旧道号": {PAGODA: {"disabled": True}}}})
        self.assertTrue(self.paused(PAGODA, "化身甲"))
        result = self.pause(PAGODA, "旧道号", disabled=False)
        self.assertEqual(result["identity"], "化身甲")
        self.assertFalse(self.paused(PAGODA, "化身甲"))
        self.assertIn("旧道号", dashboard.load_command_controls()["main"])

    def test_legacy_page_alias_can_resume_without_changing_other_options(self):
        self.controls({"main": {"主魂": {"miniapp:spirit-beast:xiaohao": {"disabled": True, "option": 7}}}})
        self.assertTrue(self.paused("miniapp:spirit-beast"))
        self.assertFalse(self.pause("miniapp:spirit-beast", disabled=False)["disabled"])
        self.assertFalse(self.paused("miniapp:spirit-beast:xiaohao"))
        self.assertEqual(dashboard.load_command_controls()["main"]["主魂"]["miniapp:spirit-beast"]["option"], 7)

    def test_account_wide_pause_is_reported_instead_of_false_success(self):
        self.controls({"main": {"*": {".定命 *": {"disabled": True}}}})
        result = self.pause(".定命 <命星>", disabled=False)
        self.assertTrue(result["disabled"])
        self.assertIn("账号级", result["msg"])
        self.assertTrue(self.paused(".定命 紫微"))

    def test_feature_pause_controls_children_without_erasing_individual_choices(self):
        self.pause("miniapp:star-farm-soothe")
        self.pause("miniapp:star-farm")
        self.assertTrue(self.paused("miniapp:star-farm-collect"))
        result = self.pause("miniapp:star-farm-collect", disabled=False)
        self.assertTrue(result["disabled"])
        self.pause("miniapp:star-farm", disabled=False)
        self.assertFalse(self.paused("miniapp:star-farm-collect"))
        self.assertTrue(self.paused("miniapp:star-farm-soothe"))

    def test_default_paused_command_can_resume_without_client_default_flag(self):
        self.assertFalse(self.pause(".共历心劫", disabled=False)["disabled"])
        self.assertIs(dashboard.load_command_controls()["main"]["主魂"][".共历心劫"]["disabled"], False)

    def test_existing_exact_controls_keep_their_scope(self):
        self.pause(".推命 炼制")
        self.assertFalse(self.paused(".推命 闭关"))
        self.assertFalse(self.paused(".推命测试 炼制"))
        self.assertTrue(self.paused(".推命 炼制"))

    def test_all_thirteen_identity_panels_keep_controls_without_changing_state(self):
        counts = {"main": 3, "sub": 3, "xiaohao": 3, "waaiging": 0}
        panels_seen = 0
        for account, count in counts.items():
            self.state["avatars"] = {f"身份{i}": {} for i in range(count)}
            before = copy.deepcopy(self.state)
            for panel in dashboard.build_command_panels(account, self.state):
                panels_seen += 1
                by_command = {row["command"]: row for row in panel["commands"]}
                for command in (".闭关修炼", ".服用 合气丹", ".稳", PAGODA, HUNT, TRIAL, FATE_CARDS, FISHING, JOURNEY, WORLD_BOSS):
                    self.assertIn(command, by_command, (account, panel["identity"], command))
                    self.assertTrue(by_command[command]["controllable"])
                self.assertNotIn(".闯塔", by_command)
                self.assertNotIn(".野外历练", by_command)
                self.assertNotIn(".红尘寻缘", by_command)
            self.assertEqual(self.state, before)
        self.assertEqual(panels_seen, 13)
        self.assertFalse(self.path.exists())

    def test_flows_and_page_operations_show_actual_paused_status(self):
        for command in (".定命 <命星>", PAGODA, FISHING, "miniapp:forge"):
            self.pause(command, "化身甲")
        panels = {p["identity"]: p for p in dashboard.build_command_panels("main", self.state)}
        for row in panels["化身甲"]["commands"]:
            if row["control_disabled"]:
                self.assertEqual((row["status"], row["tone"]), ("已暂停", "paused"))
        self.assertNotIn(".定命 <命星>", {r["command"] for r in panels["化身乙"]["commands"]})

    def test_star_farm_panel_has_both_parent_and_all_individual_controls(self):
        self.state["identity_sect_names"]["化身甲"] = "星宫"
        panel = next(p for p in dashboard.build_command_panels("main", self.state) if p["identity"] == "化身甲")
        commands = {row["command"] for row in panel["commands"]}
        self.assertTrue({"miniapp:star-farm", "miniapp:star-farm-soothe", "miniapp:star-farm-collect",
                         "miniapp:star-farm-pull"}.issubset(commands))

    def test_tianxing_exploration_prefix_keeps_an_independent_flow_control(self):
        panel = dashboard.build_command_panels("main", self.state)[0]
        row = next(row for row in panel["commands"] if row["command"] == ".推命 探索")
        self.assertTrue(row["controllable"])
        self.assertFalse(row["actionable"])
        self.assertIsNone(row.get("next_seconds"))
        self.pause(row["command"])
        self.assertTrue(self.paused(".推命 探索"))
        self.assertFalse(self.paused(".推命 闭关"))
        self.assertFalse(self.paused(".推命 炼制"))


class MiniAppControlBoundaryTests(ControlFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.post = AsyncMock(return_value={"ok": True})
        self.transport = MiniAppDwellingTransport(Mock(), ENTRY, post_json=self.post)
        self.transport.sect_actor = self.actor
        self.transport.init_data = "fixture"
        self.transport.start_payload = {"ok": True}
        self.transport.identity_player_ids = {"主魂": 1, "化身甲": -2, "化身乙": -3}

    def disable(self, command, identity="主魂"):
        self.controls({"main": {identity: {log_utils.command_control_key(command): {"disabled": True}}}})

    async def test_direct_page_mutations_are_blocked_at_the_actual_request(self):
        for command, route, body in (
            (JOURNEY, "journey", {"action": "wild_experience", "mode": "deep"}),
            (HUNT, "hunt", {}), (HUNT, "hunt/reveal", {"index": 1}),
            (".安抚信徒", "small-world", {"action": "soothe"}),
            (".神迹 布道", "small-world", {"action": "miracle_relief"}),
            ("miniapp:small-world-collect", "small-world", {"action": "collect"}),
            ("miniapp:forge", "forge/craft", {"targetItemId": "fixture"}),
            (".定命 <命星>", "command-center", {"command": ".定命 紫微"}),
            ("miniapp:inventory", "inventory", {}),
            ("miniapp:meditation-settle", "deep-seclusion", {"action": "settle"}),
        ):
            with self.subTest(command=command, route=route):
                self.disable(command)
                with self.assertRaises(CommandControlPaused):
                    await self.transport.request("/api/miniapp/xianxia-dwelling/" + route, body)
        self.post.assert_not_awaited()

    async def test_external_page_actions_cannot_bypass_switches(self):
        for command, route, body in (
            (PAGODA, "pagoda/challenge", {}), (TRIAL, "trial/start", {}),
            (FATE_CARDS, "fate-cards/draw", {}),
            ("miniapp:star-farm", "sect-farm/start", {}),
            ("miniapp:star-farm-collect", "sect-farm/action", {"action": "collect"}),
            ("miniapp:spirit-beast-contract", "spirit-beast/action", {"action": "interact"}),
            (".灵兽出战 <灵兽>", "spirit-beast/action", {"action": "active"}),
            ("miniapp:spirit-beast-rest", "spirit-beast/action", {"action": "rest"}),
        ):
            with self.subTest(command=command):
                self.disable(command)
                with self.assertRaises(CommandControlPaused):
                    await self.transport._external_request_unlocked("主魂", "fixture", "fixture_", "/api/miniapp/xianxia-" + route, body)
        self.post.assert_not_awaited()

    async def test_pause_while_authenticating_is_rechecked_before_mutation(self):
        self.transport.init_data = ""
        async def initialize():
            self.disable(JOURNEY)
            self.transport.init_data = "new-fixture"
        self.transport._initialize_unlocked = AsyncMock(side_effect=initialize)
        with self.assertRaises(CommandControlPaused):
            await self.transport.journey_action("主魂")
        self.post.assert_not_awaited()

    async def test_pause_while_external_token_waits_is_rechecked(self):
        async def token(*args):
            self.disable(PAGODA)
            return "fixture"
        self.transport._external_token_unlocked = AsyncMock(side_effect=token)
        with self.assertRaises(CommandControlPaused):
            await self.transport.pagoda_challenge("主魂")
        self.post.assert_not_awaited()

    async def test_fishing_pauses_new_casts_but_preserves_receipt_settlement(self):
        self.disable(FISHING)
        with self.assertRaises(CommandControlPaused):
            await self.transport.fishing_next_cast("主魂", "fixture", "pond", "bait")
        await self.transport.fishing_finish("主魂", "fixture", {"challengeId": "existing"})
        await self.transport.fishing_result("主魂", "fixture")
        self.assertEqual([call.args[1].rsplit("/", 1)[-1] for call in self.post.await_args_list], ["finish", "result"])

    async def test_daily_pauses_do_not_record_completion_or_touch_progress(self):
        runner = MiniAppDailyActivities(self.actor, self.transport, "main", logging.getLogger("test"))
        before = copy.deepcopy(self.state)
        for command, run in ((PAGODA, runner.run_pagoda_identity), (TRIAL, runner.run_tianji_trial_identity),
                             (FATE_CARDS, runner.run_fate_cards_identity), (HUNT, runner.run_hunt_identity)):
            self.disable(command, "化身甲")
            self.assertEqual(await run("化身甲"), "paused")
        self.assertEqual(self.state, before)
        self.post.assert_not_awaited()

    async def test_journey_and_fishing_pause_before_preconditions_or_spending(self):
        self.disable(JOURNEY)
        runner = MiniAppTianxingJourney(self.actor, self.transport, "main", Mock())
        self.assertEqual(await runner.run_identity("主魂"), (False, 60))
        self.disable(FISHING)
        fishing = MiniAppFishingAutomation(self.actor, self.transport, "main", Mock())
        self.assertEqual(await fishing.run_cycle({"enabled": True}, identity="主魂"), 60)
        self.assertFalse(fishing._last_round_completed)
        self.post.assert_not_awaited()

    async def test_world_boss_pause_blocks_begin_without_changing_battle_timing(self):
        from tests.test_world_boss_features import FakeActor, DummyMessage, extract_world_boss_entry
        actor = FakeActor()
        actor.account_key = "main"
        monitor = WorldBossMonitor(actor, "main")
        self.disable(WORLD_BOSS)
        monitor._request = AsyncMock()
        with self.assertRaises(CommandControlPaused):
            await monitor._begin_with_turnstile(extract_world_boss_entry(DummyMessage()), "主魂", "fixture", "session", "challenge")
        monitor._request.assert_not_awaited()


class CommandPanelBrowserTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for Dashboard interaction checks")
    def test_all_row_types_toggle_and_failures_restore_every_matching_row(self):
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('dashboard.html', 'utf8');
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const rows = ['cooldown','daily','flow','manual','watch',''].map((kind, i) => ({
  command: i < 2 ? '.状态' : '.定命 <命星>', control_key: i < 2 ? '.状态' : '.定命 *',
  label: kind || '无排程', schedule_type: kind, actionable: i < 2, status: '按需', tone: 'manual'
}));
let release, requests = 0, failed = false;
const context = {window: {}, pollBusy: {status:false}, commandPausePending: {}, dashboardActionPending: {}, dualCultivationDrafts: {},
  commandIdentityKey: (a,i) => a + '|' + i, commandModalKey: 'main|主魂',
  nextDueCommandKey: () => '', commandScheduleText: () => '按需', commandClassification: () => ({pending:false}),
  escapeHtml: String, escapeJs: String, apiUrl: String, renderCommandModal: () => {},
  console, alerts: [], alert: value => context.alerts.push(value),
  updateStatus: async () => {},
  fetch: async (url, options) => {
    requests++;
    const payload = JSON.parse(options.body);
    assert.equal(payload.identity, '主魂'); assert.equal(payload.account, 'main');
    assert.equal(payload.control_key, '.定命 *');
    await new Promise(resolve => {release = resolve;});
    return {ok: !failed, status: failed ? 500 : 200,
      json: async () => ({success: !failed, disabled: payload.disabled, msg: '保存失败'})};
  }
};
vm.createContext(context);
vm.runInContext(extract('function commandPauseKey(', 'function commandClassification('), context);
vm.runInContext(extract('function isDashboardCommand(', 'function openIdentityCommands('), context);
vm.runInContext(extract('function renderCommandRows(', 'function renderCommandPanels('), context);
vm.runInContext(extract('async function toggleCommandPause(', 'function markAutomationSettingsDirty('), context);
(async () => {
  context.registerCommandPanels('main', [{identity:'主魂',commands:rows}, {identity:'化身',commands:structuredClone(rows)}]);
  assert.equal(context.window.commandPanelDataMap['main|主魂'].panel.commands.length, 6);
  assert.equal((context.renderCommandRows('main|主魂',rows).match(/role="switch"/g) || []).length, 6);
  let saving = context.toggleCommandPause('main|主魂','main','主魂','.定命 *','.定命 <命星>','定命',true);
  assert.equal(rows.filter(r => r.control_disabled).length, 4);
  assert.equal(context.window.commandPanelDataMap['main|化身'].panel.commands.filter(r=>r.control_disabled).length, 0);
  await context.toggleCommandPause('main|主魂','main','主魂','.定命 *','.定命 <命星>','定命',true);
  assert.equal(requests, 1);
  assert.match(context.renderCommandRows('main|主魂',rows), /aria-checked="false"[^>]*disabled/);
  // A stale status poll must not undo an in-flight switch.
  context.registerCommandPanels('main',[{identity:'主魂', commands: structuredClone(rows).map(r=>({...r,control_disabled:false}))}]);
  assert.equal(context.window.commandPanelDataMap['main|主魂'].panel.commands.filter(r=>r.control_disabled).length, 4);
  release(); await saving;
  failed = true;
  saving = context.toggleCommandPause('main|主魂','main','主魂','.定命 *','.定命 <命星>','定命',false);
  release(); await saving;
  assert.equal(context.alerts.length, 1);
  assert.equal(context.window.commandPanelDataMap['main|主魂'].panel.commands.filter(r=>r.control_disabled).length, 4);
  assert.equal(Object.keys(context.commandPausePending).length, 0);
})().catch(error=>{console.error(error); process.exitCode=1;});
'''
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
