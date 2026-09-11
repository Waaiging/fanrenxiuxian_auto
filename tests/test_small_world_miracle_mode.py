"""A saved miracle choice controls the Mini App action without resetting its clock."""
import copy
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import dashboard_server as dashboard
import intelligent_cultivator as core
import log_utils
from command_modules import SMALL_WORLD_MIRACLE_CONTROL_KEY as CONTROL_KEY
from cultivator_waaiging import WaaigingCultivator


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 11, 12, 0, 0, tzinfo=tz)


def world(cooldown=0, ok=True):
    return {
        "actionResult": {"ok": ok, "message": "神迹结果"},
        "account": {"smallWorld": {"hasWorld": True, "actions": {"edictRemainingSeconds": cooldown}}},
    }


class SmallWorldMiracleModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "command_controls.json"
        self.enterContext(patch.object(dashboard, "command_control_path", return_value=str(self.path)))
        self.enterContext(patch.object(log_utils, "COMMAND_CONTROL_FILE", str(self.path)))
        self.enterContext(patch.object(dashboard, "get_state", return_value={"avatars": {}}))
        self.enterContext(patch.object(dashboard, "load_custom_commands", return_value={}))
        self.enterContext(patch.object(core, "datetime", Clock))
        self.enterContext(patch.object(dashboard, "datetime", Clock))
        self.enterContext(patch.dict(dashboard.app.dependency_overrides, {dashboard.authenticate: lambda: "test"}))
        self.client = self.enterContext(TestClient(dashboard.app))

    def choose(self, account, mode):
        response = self.client.post("/api/small-world-miracle-mode", json={
            "account": account, "identity": "主魂", "mode": mode,
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])

    def actor(self, account):
        cls = core.Cultivator if account == "main" else WaaigingCultivator
        actor = cls.__new__(cls)
        actor.account_key = account
        actor.state_file = str(self.path.parent / f"state_{account}.json")
        actor.state = {"small_world_calamity_pending": False, "next_miracle_preach_time": "2026-09-11 15:00:00"}
        actor.save_state = Mock()
        actor.active_atomic_task = None
        actor.should_wait_for_atomic_task = lambda command: False
        actor._miniapp_command_router = SimpleNamespace(transport=SimpleNamespace(
            small_world_snapshot=AsyncMock(return_value=world()),
            small_world_action=AsyncMock(return_value=world(10800)),
        ))
        return actor

    def row(self, account, state=None):
        state = state or {"next_miracle_preach_time": "2026-09-11 15:00:00", "avatars": {}}
        rows = dashboard.build_command_panels(account, state)[0]["commands"]
        return next(row for row in rows if row.get("control_key") == CONTROL_KEY)

    async def test_unconfigured_accounts_keep_sermon_and_expose_both_choices(self):
        for account in ("main", "waaiging"):
            actor = self.actor(account)
            self.assertEqual(actor.configured_small_world_miracle_mode(), "布道")
            row = self.row(account)
            self.assertEqual(row["miracle_mode_options"], ["布道", "赈灾"])
            self.assertEqual(row["miracle_mode_value"], "布道")
            self.assertEqual(row["execution_channel"], "miniapp")
        self.assertFalse(self.path.exists())

    async def test_choice_is_per_account_and_survives_new_worker(self):
        self.choose("main", "赈灾")
        self.choose("waaiging", "布道")
        for account, mode, action in (("main", "赈灾", "miracle_relief"), ("waaiging", "布道", "miracle_sermon")):
            with self.subTest(account=account):
                actor = self.actor(account)
                self.assertTrue(await actor.execute_miracle_preach_once())
                actor._miniapp_command_router.transport.small_world_action.assert_awaited_once_with("主魂", action)
                self.assertEqual(actor.state["last_small_world_miracle_mode"], mode)
                self.assertEqual(actor.state["next_miracle_preach_time"], "2026-09-11 15:00:00")
                self.assertEqual(self.actor(account).configured_small_world_miracle_mode(), mode)
                row = self.row(account, actor.state)
                self.assertEqual(row["command"], f".神迹 {mode}")
                self.assertEqual(row["label"], f"神迹 {mode}")
                self.assertFalse(row["classification"]["pending"])

    async def test_switching_options_preserves_clock_and_legacy_pause(self):
        self.path.write_text(json.dumps({"main": {"主魂": {CONTROL_KEY: True}}}), encoding="utf-8")
        actor = self.actor("main")
        before = copy.deepcopy(actor.state)
        self.choose("main", "赈灾")
        self.assertEqual(actor.state, before)
        row = self.row("main", actor.state)
        self.assertTrue(row["control_disabled"])
        self.assertEqual(row["at"], before["next_miracle_preach_time"])
        self.assertFalse(await actor.execute_miracle_preach_once())
        actor._miniapp_command_router.transport.small_world_action.assert_not_awaited()
        self.assertTrue(actor.dashboard_command_paused(".神迹 赈灾", "主魂"))

    async def test_pause_resume_does_not_discard_selected_mode(self):
        self.choose("waaiging", "赈灾")
        for disabled in (True, False):
            response = self.client.post("/api/command-control", json={
                "account": "waaiging", "identity": "主魂", "command": ".神迹 赈灾",
                "control_key": CONTROL_KEY, "disabled": disabled,
            })
            self.assertTrue(response.json()["success"])
            actor = self.actor("waaiging")
            self.assertEqual(actor.configured_small_world_miracle_mode(), "赈灾")
            self.assertEqual(actor.dashboard_command_paused(".神迹 赈灾", "主魂"), disabled)
            self.assertEqual(self.row("waaiging")["miracle_mode_value"], "赈灾")

    async def test_account_wide_pause_still_blocks_either_mode(self):
        self.path.write_text(json.dumps({"main": {"*": {CONTROL_KEY: True}}}), encoding="utf-8")
        self.choose("main", "赈灾")
        actor = self.actor("main")
        self.assertTrue(actor.dashboard_command_paused(".神迹 赈灾", "主魂"))
        self.assertFalse(await actor.execute_miracle_preach_once())
        actor._miniapp_command_router.transport.small_world_action.assert_not_awaited()

    async def test_live_choice_after_snapshot_wait_controls_the_action(self):
        actor = self.actor("main")

        async def read_world(identity):
            self.choose("main", "赈灾")
            return world()

        transport = actor._miniapp_command_router.transport
        transport.small_world_snapshot.side_effect = read_world
        self.assertTrue(await actor.execute_miracle_preach_once())
        transport.small_world_action.assert_awaited_once_with("主魂", "miracle_relief")

    async def test_relief_obeys_existing_shared_edict_cooldown(self):
        self.choose("main", "赈灾")
        actor = self.actor("main")
        transport = actor._miniapp_command_router.transport
        transport.small_world_snapshot.return_value = world(1800)
        self.assertFalse(await actor.execute_miracle_preach_once())
        transport.small_world_action.assert_not_awaited()
        self.assertEqual(actor.state["next_miracle_preach_time"], "2026-09-11 12:30:00")

    async def test_failed_relief_keeps_choice_and_does_not_try_sermon(self):
        self.choose("waaiging", "赈灾")
        actor = self.actor("waaiging")
        transport = actor._miniapp_command_router.transport
        transport.small_world_action.return_value = world(ok=False)
        self.assertFalse(await actor.execute_miracle_preach_once())
        transport.small_world_action.assert_awaited_once_with("主魂", "miracle_relief")
        self.assertEqual(actor.state["miniapp_miracle_preach_last_error"], "miracle_relief_failed")
        self.assertEqual(actor.configured_small_world_miracle_mode(), "赈灾")

    async def test_invalid_selections_do_not_modify_settings(self):
        self.choose("main", "赈灾")
        before = self.path.read_bytes()
        for payload in (
            {"account": "sub", "mode": "赈灾"},
            {"account": "xiaohao", "mode": "布道"},
            {"account": "main", "identity": "无咎子", "mode": "赈灾"},
            {"account": "main", "mode": "安抚信徒"},
            {"account": "main", "mode": ""},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/small-world-miracle-mode", json=payload)
                self.assertFalse(response.json()["success"])
                self.assertEqual(self.path.read_bytes(), before)

    async def test_mode_endpoint_requires_authentication(self):
        with patch.dict(dashboard.app.dependency_overrides, {}, clear=True), \
                patch.object(dashboard, "DASHBOARD_PASSWORD", "test-password"):
            response = self.client.post("/api/small-world-miracle-mode", json={"account": "main", "mode": "赈灾"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.path.exists())


class SmallWorldMiracleDashboardTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Dashboard behavior validation")
    def test_dropdown_saves_choice_shows_pending_value_and_recovers_from_failure(self):
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const html = fs.readFileSync('dashboard.html', 'utf8');
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
const row = {command: '.神迹 布道', label: '神迹 布道', control_key: '.神迹 布道',
  miracle_mode_options: ['布道', '赈灾'], miracle_mode_value: '布道', execution_channel: 'miniapp'};
let posted, failed = false, refreshed = 0, pendingMarkup = '', releaseOlderPoll;
const context = {commandPausePending: {}, dashboardActionPending: {}, dualCultivationDrafts: {},
  pollBusy: {status: false},
  window: {commandPanelDataMap: {main: {account: 'main', panel: {identity: '主魂'}}},
    setTimeout: callback => {releaseOlderPoll = callback;}},
  commandModalKey: 'main', nextDueCommandKey: () => '', commandItemKey: item => item.command,
  commandScheduleText: () => '3小时冷却', commandPauseKey: (a, i, k) => [a, i, k].join('|'),
  escapeHtml: String, escapeJs: String, commandClassification: () => ({pending: false}),
  apiUrl: String, alerts: [], console: {error() {}},
  alert: text => context.alerts.push(text),
  fetch: async (url, options) => {
    assert.equal(url, '/api/small-world-miracle-mode');
    assert.equal(options.method, 'POST');
    posted = JSON.parse(options.body);
    return {ok: !failed, status: failed ? 400 : 200, json: async () => ({success: !failed, msg: '保存失败'})};
  },
  updateStatus: async () => {
    assert.equal(context.pollBusy.status, false);
    assert.equal(Object.keys(context.commandPausePending).length, 1);
    refreshed++;
    if (!failed) row.miracle_mode_value = posted.mode;
  },
  renderCommandModal: () => {pendingMarkup = context.renderCommandRows('main', [row]);},
};
vm.createContext(context);
vm.runInContext(extract('function renderCommandRows(', 'function renderCommandPanels('), context);
vm.runInContext(extract('async function setSmallWorldMiracleMode(', 'async function setFishingAutoHolder('), context);
(async () => {
  const initial = context.renderCommandRows('main', [row]);
  assert.match(initial, /aria-label="神迹方式"/);
  assert.match(initial, /value="布道" selected/);
  context.pollBusy.status = true;
  const saving = context.setSmallWorldMiracleMode('main', 'main', '主魂', '.神迹 布道', {value: '赈灾'});
  assert.match(pendingMarkup, /value="赈灾" selected/);
  assert.match(pendingMarkup, /<select[^>]*disabled/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(refreshed, 0);
  assert.match(context.renderCommandRows('main', [row]), /value="赈灾" selected/);
  context.pollBusy.status = false;
  assert.equal(typeof releaseOlderPoll, 'function');
  releaseOlderPoll();
  await saving;
  assert.deepEqual(posted, {account: 'main', identity: '主魂', mode: '赈灾'});
  assert.equal(refreshed, 1);
  assert.equal(Object.keys(context.commandPausePending).length, 0);
  assert.match(context.renderCommandRows('main', [row]), /value="赈灾" selected/);
  failed = true;
  await context.setSmallWorldMiracleMode('main', 'main', '主魂', '.神迹 布道', {value: '布道'});
  assert.equal(context.alerts.length, 1);
  assert.equal(refreshed, 2);
  assert.equal(Object.keys(context.commandPausePending).length, 0);
  assert.match(context.renderCommandRows('main', [row]), /value="赈灾" selected/);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run(["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
                                text=True, encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
