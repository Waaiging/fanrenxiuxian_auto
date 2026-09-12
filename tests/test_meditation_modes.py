import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import automation_settings as settings
import command_feedback as feedback
from common_command_features import CommonCommandMixin
from intelligent_cultivator import Cultivator
from meditation_features import TIME_FORMAT
from miniapp_dwelling import MiniAppDwellingTransport
from miniapp_command_routing import MiniAppCommandRouter
from restricted_miniapp_worker import RestrictedMiniAppWorker
from sect_rules import SectTaskStopped


class MeditationActor(CommonCommandMixin):
    parse_wait_time = Cultivator.parse_wait_time

    def __init__(self, account="main"):
        self.account_key = account
        self.avatars = list(settings.automation_account_identities()[account][1:])
        self.state = {"avatars": {name: {} for name in self.avatars}}
        self.sects = {}
        self.sent = []
        self.responses = {
            ".查看闭关": "你并未处于深度闭关之中。",
            ".强行出关": "你已强行出关，深度闭关已经结束。",
            ".深度闭关": "你已进入深度闭关状态，神魂将自行吐纳 8小时。",
            ".推命 闭关": "推命命中闭关，执行成功。",
            ".闭关修炼": "闭关成功，获得修为，需要调息 10 分钟。",
            ".服用 合气丹": "成功服用合气丹。",
        }
        self.on_send = None
        self.active_atomic_task = None
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.is_running = True
        self.save_state = Mock()
        self.logger = Mock()
        self.ensure_tianxing_destiny_for_action = AsyncMock(return_value=True)
        self.paused_commands = set()

    def common_command_logger(self):
        return self.logger

    def identity_sect_name(self, identity="主魂"):
        return self.sects.get(identity, "星宫")

    def get_avatar_state(self, identity):
        return self.state["avatars"].setdefault(identity, {})

    def dashboard_command_paused(self, command, identity="主魂"):
        return (identity, command) in self.paused_commands or self.meditation_command_paused(command, identity)

    @staticmethod
    def response_text(response):
        return str(getattr(response, "text", response) or "")

    async def send_and_wait_feedback(self, command, **kwargs):
        return await self.send_and_wait_feedback_identity("主魂", command, **kwargs)

    async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
        self.sent.append((identity, command, kwargs))
        if self.on_send:
            await self.on_send(identity, command)
        return self.responses.get((identity, command), self.responses[command])

    def commands(self, identity="主魂"):
        return [command for target, command, _ in self.sent if target == identity]


class MeditationModesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "settings.json"
        for name, value in (
            ("AUTOMATION_SETTINGS_FILE", self.path),
            ("_load_main_identity_state", lambda: {}),
            ("_load_sub_identity_state", lambda: {}),
            ("_load_xiaohao_identity_state", lambda: {}),
            ("tianxing_tianji_auto_participants", lambda: []),
        ):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alert = AsyncMock()
        patcher = patch("meditation_features.send_text_alert", self.alert)
        patcher.start()
        self.addCleanup(patcher.stop)

    def select(self, key="main|主魂", **changes):
        return settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="护阵",
            meditation_identities={key: changes},
        )

    def ready(self, actor, identity="主魂", count=0):
        state = actor._meditation_identity_state(identity)
        state["meditation_runtime"] = {
            "prepared_mode": "daily",
            "prepared_switch_id": actor.meditation_config(identity)["switch_id"],
            "success_count": count,
        }
        return state["meditation_runtime"]

    def test_legacy_mode_and_pill_migrate_only_to_original_main_soul(self):
        self.path.write_text(json.dumps({"tianxing": {
            "meditation_mode": "fate", "use_heqi_pill": True, "meditation_switch_id": "old-switch",
        }}), encoding="utf-8")
        rows = settings.load_automation_settings()["meditation"]["identities"]
        self.assertEqual(len(rows), 13)
        self.assertEqual(rows["main|主魂"], {
            "enabled": True, "mode": "daily", "use_heqi_pill": True, "switch_id": "old-switch",
        })
        for key, config in rows.items():
            if key != "main|主魂":
                self.assertEqual(config, {"enabled": True, "mode": "deep", "use_heqi_pill": False, "switch_id": ""})

    def test_choices_and_switch_ids_are_independent_and_survive_unrelated_save(self):
        first = self.select(mode="daily", use_heqi_pill=True)
        second = self.select("sub|厚土", mode="daily", enabled=False)
        self.assertEqual(first["meditation"]["identities"]["main|主魂"], second["meditation"]["identities"]["main|主魂"])
        third = self.select("sub|厚土", use_heqi_pill=True)
        self.assertEqual(second["meditation"]["identities"]["sub|厚土"]["switch_id"], third["meditation"]["identities"]["sub|厚土"]["switch_id"])
        saved = settings.save_automation_settings(world_boss_participants=[], mulan_support_mode="奇袭")
        self.assertEqual(saved["meditation"], third["meditation"])
        self.assertEqual(saved["tianxing"]["meditation_mode"], "fate")

    def test_renamed_identity_retains_its_selection(self):
        self.select("main|无咎子", mode="daily", use_heqi_pill=True)
        with patch.object(settings, "_load_main_identity_state", return_value={
            "avatar_dao_name_aliases": {"无咎子": "新道号"},
            "avatar_dao_names_by_player_id": {settings.STABLE_MAIN_AVATAR_SLOTS["无咎子"]: "新道号"},
        }):
            rows = settings.load_automation_settings()["meditation"]["identities"]
            self.assertNotIn("main|无咎子", rows)
            self.assertEqual(rows["main|新道号"]["mode"], "daily")
            self.assertTrue(rows["main|新道号"]["use_heqi_pill"])

    def test_invalid_identity_mode_and_flags_do_not_write_settings(self):
        self.select(mode="daily")
        before = self.path.read_bytes()
        for update in (
            {"unknown|主魂": {"mode": "daily"}}, {"main|主魂": {"mode": ["deep"]}},
            {"main|主魂": {"mode": "unknown"}}, {"main|主魂": {"enabled": "false"}},
            {"main|主魂": {"use_heqi_pill": 1}}, [],
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                settings.save_automation_settings(world_boss_participants=[], mulan_support_mode="护阵", meditation_identities=update)
            self.assertEqual(self.path.read_bytes(), before)

    def test_all_account_souls_run_daily_using_their_live_sect(self):
        roster = settings.automation_account_identities()
        settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="护阵",
            meditation_identities={f"{account}|{identity}": {"mode": "daily"} for account, names in roster.items() for identity in names},
        )
        for account, names in roster.items():
            actor = MeditationActor(account)
            for index, identity in enumerate(names):
                actor.sects[identity] = "天星宗" if index % 2 == 0 else "万灵宗"
                with self.subTest(account=account, identity=identity):
                    self.assertTrue(asyncio.run(actor.configured_meditation_tick(identity)))
                    expected = [".查看闭关"] + ([".推命 闭关"] if index % 2 == 0 else []) + [".闭关修炼"]
                    self.assertEqual(actor.commands(identity), expected)
                    self.assertEqual(actor._meditation_runtime(identity)["success_count"], 1)
            actor.logger.exception.assert_not_called()

    def test_daily_cooldown_and_counts_survive_restart_without_duplicate_commands(self):
        self.select(mode="daily")
        actor = MeditationActor()
        asyncio.run(actor.configured_meditation_tick())
        restarted = MeditationActor()
        restarted.state = copy.deepcopy(actor.state)
        asyncio.run(restarted.configured_meditation_tick())
        self.assertEqual(restarted.sent, [])
        self.assertEqual(restarted._meditation_runtime("主魂")["success_count"], 1)

    def test_prefix_is_required_again_on_every_daily_cycle_after_sect_change(self):
        self.select("sub|厚土", mode="daily")
        actor = MeditationActor("sub")
        asyncio.run(actor.configured_meditation_tick("厚土"))
        actor.sects["厚土"] = "天星宗"
        actor._meditation_runtime("厚土")["next_daily_at"] = ""
        asyncio.run(actor.configured_meditation_tick("厚土"))
        self.assertEqual(actor.commands("厚土"), [".查看闭关", ".闭关修炼", ".推命 闭关", ".闭关修炼"])

    def test_post_pill_cultivation_has_its_own_prefix_and_counts_for_same_identity(self):
        self.select("main|无咎子", mode="daily", use_heqi_pill=True)
        actor = MeditationActor()
        actor.sects["无咎子"] = "天星宗"
        runtime = self.ready(actor, "无咎子", 1)
        asyncio.run(actor.configured_meditation_tick("无咎子"))
        self.assertEqual(actor.commands("无咎子"), [".推命 闭关", ".闭关修炼", ".服用 合气丹", ".推命 闭关", ".闭关修炼"])
        self.assertEqual(runtime["success_count"], 3)
        self.assertEqual(actor.commands("主魂"), [])
        self.assertNotIn("meditation_runtime", actor.state)

    def test_lost_pill_reply_does_not_resend_or_start_extra_cultivation(self):
        self.select(mode="daily", use_heqi_pill=True)
        actor = MeditationActor()
        runtime = self.ready(actor, count=1)
        actor.client = SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(id=42, sender_id=999, chat_id=100),
        ))
        actor.cmd_lock = asyncio.Lock()
        actor.target_chat_id, actor.topic_id = 100, None
        actor.current_identity = "主魂"
        for name in ("feedback_events", "feedback_commands", "feedback_sent_ts",
                     "last_feedback_text", "last_feedback_msg"):
            setattr(actor, name, {})
        original_send = actor.send_and_wait_feedback_identity

        async def send(identity, command, **kwargs):
            if command == ".服用 合气丹":
                return await feedback.send_and_wait_feedback_common(
                    actor, actor.logger, command, delete_after=False, **kwargs,
                )
            return await original_send(identity, command, **kwargs)

        actor.send_and_wait_feedback_identity = send
        with ExitStack() as stack:
            for name, value in (
                ("record_command_sent", Mock()), ("record_recent_profile_command", Mock()),
                ("record_bot_no_response", Mock()), ("record_telegram_send_success", AsyncMock()),
                ("send_text_alert", AsyncMock()), ("command_send_allowed", Mock(return_value=True)),
                ("wait_for_bot_activity_before_send", AsyncMock(return_value=True)),
                ("NO_RESPONSE_TIMEOUT_SECONDS", 0.01), ("NO_RESPONSE_RETRY_COUNT", 1),
            ):
                stack.enter_context(patch.object(feedback, name, value))
            stack.enter_context(patch.object(feedback.asyncio, "sleep", AsyncMock()))
            asyncio.run(actor.configured_meditation_tick())
            asyncio.run(actor.configured_meditation_tick())
        actor.client.send_message.assert_awaited_once_with(100, ".服用 合气丹", reply_to=None)
        self.assertEqual(actor.commands(), [".闭关修炼"])
        self.assertEqual(runtime["success_count"], 2)
        self.assertEqual(runtime["last_pill_count"], 2)
        self.assertTrue(actor.meditation_config()["use_heqi_pill"])

    def test_failed_prefix_blocks_cultivation_and_waits_for_retry(self):
        self.select(mode="daily")
        actor = MeditationActor()
        actor.sects["主魂"] = "天星宗"
        actor.responses[".推命 闭关"] = "推命尚在冷却，还需等待 5 分钟。"
        self.ready(actor)
        asyncio.run(actor.configured_meditation_tick())
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands(), [".推命 闭关"])

    def test_mode_change_while_prefix_in_flight_stops_the_old_chain(self):
        self.select(mode="daily")
        actor = MeditationActor()
        actor.sects["主魂"] = "天星宗"
        self.ready(actor)

        async def change_mode(identity, command):
            if command == ".推命 闭关":
                self.select(mode="deep")

        actor.on_send = change_mode
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands(), [".推命 闭关"])

    def test_switch_from_active_deep_honors_pill_option_and_forces_status_check(self):
        for use_pill in (False, True):
            with self.subTest(use_pill=use_pill):
                self.select(mode="daily", use_heqi_pill=use_pill)
                actor = MeditationActor()
                actor.state.update(in_deep_meditation=True, deep_meditation_guard_until="2099-01-01 00:00:00")
                actor.responses[".查看闭关"] = "你正在深度闭关，预计还需 5小时。"
                asyncio.run(actor.configured_meditation_tick())
                self.assertEqual(actor.commands(), [".查看闭关", ".强行出关"] + ([".服用 合气丹"] if use_pill else []) + [".闭关修炼"])
                self.assertTrue(actor.sent[0][2]["force_meditation_check"])
                self.assertFalse(actor.state["in_deep_meditation"])
                before = list(actor.sent)
                asyncio.run(actor.configured_meditation_tick())
                self.assertEqual(actor.sent, before)

    def test_unknown_status_or_failed_force_exit_never_cultivates(self):
        self.select(mode="daily", use_heqi_pill=True)
        for status, force, expected in (
            ("暂时无法获取状态", "", [".查看闭关"]),
            ("正在深度闭关，预计还需 5小时。", "无法强行出关", [".查看闭关", ".强行出关"]),
        ):
            actor = MeditationActor()
            actor.state["in_deep_meditation"] = True
            actor.responses.update({".查看闭关": status, ".强行出关": force})
            asyncio.run(actor.configured_meditation_tick())
            self.assertEqual(actor.commands(), expected)
            self.assertTrue(actor.state["in_deep_meditation"])

    def test_pill_shortage_disables_only_that_soul_and_keeps_other_settings(self):
        self.select("main|无咎子", mode="daily", use_heqi_pill=True)
        self.select("sub|厚土", mode="daily", use_heqi_pill=True)
        actor = MeditationActor()
        actor.responses[".服用 合气丹"] = "你没有足够的合气丹。"
        self.ready(actor, "无咎子", 1)
        before = settings.load_automation_settings()
        asyncio.run(actor.configured_meditation_tick("无咎子"))
        after = settings.load_automation_settings()
        expected = copy.deepcopy(before["meditation"])
        expected["identities"]["main|无咎子"]["use_heqi_pill"] = False
        self.assertEqual(after["meditation"], expected)
        self.assertEqual(actor.commands("无咎子"), [".闭关修炼", ".服用 合气丹"])
        self.alert.assert_awaited_once()

    def test_deep_switch_checks_then_starts_without_daily_cultivation(self):
        self.select(mode="daily")
        self.select(mode="deep")
        actor = MeditationActor()
        self.assertFalse(asyncio.run(actor.configured_meditation_tick()))
        self.assertEqual(actor.commands(), [".查看闭关", ".深度闭关"])
        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertGreater(datetime.strptime(actor.state["deep_meditation_end_time"], TIME_FORMAT), datetime.now() + timedelta(hours=7))

    def test_disabled_paused_and_command_disabled_identities_send_nothing(self):
        for condition in ("unselected", "paused", "state_paused", "command"):
            actor = MeditationActor()
            self.select(mode="daily", enabled=condition != "unselected")
            if condition == "paused":
                actor.pause_event.clear()
            if condition == "state_paused":
                actor.state["is_paused"] = True
            if condition == "command":
                actor.paused_commands.add(("主魂", ".闭关修炼"))
            asyncio.run(actor.configured_meditation_tick())
            self.assertEqual(actor.sent, [])

    def test_concurrent_ticks_do_not_duplicate_one_souls_daily_cycle(self):
        self.select(mode="daily")
        actor = MeditationActor()

        async def yield_send(identity, command):
            await asyncio.sleep(0)

        actor.on_send = yield_send

        async def run():
            await asyncio.gather(actor.configured_meditation_tick(), actor.configured_meditation_tick())

        asyncio.run(run())
        self.assertEqual(actor.commands(), [".查看闭关", ".闭关修炼"])

    def test_full_and_restricted_routing_apply_exactly_one_prefix_per_cultivation(self):
        for restricted in (False, True):
            with self.subTest(restricted=restricted):
                self.select("waaiging|主魂", mode="daily", use_heqi_pill=True)
                actor = MeditationActor("waaiging")
                actor.client = object()
                actor.config = {"miniapp_beast": {"entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"}}
                actor.sects["主魂"] = "天星宗"
                self.ready(actor, count=1)
                calls = []

                async def post_json(origin, path, payload, timeout):
                    command = payload.get("command") or ".闭关修炼"
                    calls.append((command, payload["playerId"]))
                    return {"ok": True, "actionResult": {"ok": True, "message": actor.responses[command]}}

                transport = MiniAppDwellingTransport(actor.client, actor.config["miniapp_beast"]["entry_url"], post_json=post_json)
                transport.init_data = "fixture"
                transport.start_payload = {"ok": True}
                transport.identity_player_ids = {"主魂": 100}
                transport.sect_actor = actor
                if restricted:
                    worker = RestrictedMiniAppWorker(actor, "waaiging", actor.logger)
                    worker.transport = transport
                    actor._restricted_miniapp_worker = worker
                    actor.send_and_wait_feedback = worker.send_main
                    actor.send_and_wait_feedback_identity = worker.send_identity
                else:
                    router = MiniAppCommandRouter(actor, "waaiging", actor.logger, transport=transport, start_background_tasks=False)
                    # These are the original group paths kept by router.install().
                    router._orig_send = actor.send_and_wait_feedback
                    router._orig_send_identity = actor.send_and_wait_feedback_identity
                    router._route_active = True
                    router._maybe_refresh_auth = AsyncMock()
                    actor.send_and_wait_feedback = router._send_main
                    actor.send_and_wait_feedback_identity = router._send_identity
                asyncio.run(actor.configured_meditation_tick())
                self.assertEqual(calls, [(".推命 闭关", 100), (".闭关修炼", 100)] * (1 if restricted else 2))
                self.assertEqual(actor.commands(), [] if restricted else [".服用 合气丹"])
                self.assertTrue(settings.meditation_identity_settings("waaiging")["use_heqi_pill"])
                self.assertEqual(actor._meditation_runtime("主魂")["success_count"], 2 if restricted else 3)
                actor.logger.exception.assert_not_called()

    def test_mode_change_during_transport_auth_blocks_stale_forced_exit(self):
        self.select(mode="daily")
        actor = MeditationActor()
        config = actor.meditation_config()
        post = AsyncMock()
        transport = MiniAppDwellingTransport(object(), "https://t.me/fanrenxiuxian_bot?startapp=df_fixture", post_json=post)
        transport.sect_actor = actor
        transport.identity_player_ids = {"主魂": 100}

        async def authenticate():
            self.select(mode="deep")
            transport.init_data = "fixture"
            transport.start_payload = {"ok": True}

        transport._initialize_unlocked = authenticate

        async def send(command, **kwargs):
            return await transport.command(command)

        actor.send_and_wait_feedback = send
        with self.assertRaises(SectTaskStopped):
            asyncio.run(actor._send_meditation_command("主魂", ".强行出关", config))
        post.assert_not_awaited()
        self.assertFalse(actor.meditation_command_paused(".查看闭关"))

    def test_real_account_schedulers_dispatch_before_waiting_for_deep_guard(self):
        from cultivator_waaiging import WaaigingCultivator
        from cultivator_xiaohao import CultivatorXiaoHao
        from sub_cultivator import SubCultivator

        for cls, method in (
            (Cultivator, "run_meditation_timer"), (WaaigingCultivator, "run_meditation_timer"),
            (CultivatorXiaoHao, "run_meditation_timer"), (SubCultivator, "run_formation_meditation_loop"),
        ):
            with self.subTest(worker=cls.__name__):
                actor = cls.__new__(cls)
                actor.is_running = True
                actor.startup_done = asyncio.Event()
                actor.startup_done.set()
                actor.main_formation_enabled = False
                actor._wait_for_main_identity = AsyncMock()
                actor.send_and_wait_feedback = AsyncMock()

                async def stop(identity):
                    actor.is_running = False
                    return True

                actor.configured_meditation_tick = AsyncMock(side_effect=stop)
                with patch("asyncio.sleep", new=AsyncMock()):
                    asyncio.run(getattr(actor, method)())
                actor.configured_meditation_tick.assert_awaited_once_with("主魂")
                actor.send_and_wait_feedback.assert_not_awaited()

    def test_sub_avatar_daily_mode_keeps_its_other_scheduled_tasks(self):
        from sub_cultivator import SubCultivator

        actor = MeditationActor("sub")
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        actor._avatar_loop_count = 0
        actor.execute_avatar_formation = AsyncMock()
        actor.execute_avatar_concubine_chain = AsyncMock()
        actor._avatar_mulan_support = AsyncMock()
        actor.avatar_formation_block_until = lambda *args: ""
        actor.concubine_voyage_auto_start_enabled = lambda *args: False

        async def stop(identity):
            actor.is_running = False
            return True

        actor.configured_meditation_tick = AsyncMock(side_effect=stop)
        with patch("asyncio.sleep", new=AsyncMock()):
            asyncio.run(SubCultivator.run_avatar_loop(actor, "厚土"))
        actor.execute_avatar_concubine_chain.assert_awaited_once_with("厚土")
        actor._avatar_mulan_support.assert_awaited_once_with("厚土")
        self.assertEqual(actor.sent, [])

    def test_settings_api_round_trip_exposes_all_souls_and_rejects_invalid_updates(self):
        import dashboard_server as dashboard
        from fastapi.testclient import TestClient

        before_overrides = dict(dashboard.app.dependency_overrides)
        try:
            dashboard.app.dependency_overrides[dashboard.authenticate] = lambda: "test-admin"
            with TestClient(dashboard.app) as client:
                payload = client.get("/api/automation-settings").json()
                self.assertEqual(len(payload["meditation"]["identities"]), 13)
                self.assertEqual([mode["name"] for mode in payload["meditation"]["modes"]], ["深度闭关", "日常闭关"])
                body = {"world_boss_participants": [], "mulan_support_mode": "护阵", "meditation": {"identities": {
                    "sub|厚土": {"enabled": True, "mode": "daily", "use_heqi_pill": True},
                    "main|主魂": {"enabled": False, "mode": "deep", "use_heqi_pill": False},
                }}}
                self.assertTrue(client.post("/api/automation-settings", json=body).json()["success"])
                saved = client.get("/api/automation-settings").json()["meditation"]["identities"]
                self.assertEqual(saved["sub|厚土"]["mode"], "daily")
                self.assertTrue(saved["sub|厚土"]["use_heqi_pill"])
                self.assertFalse(saved["main|主魂"]["enabled"])
                before = self.path.read_bytes()
                for invalid in ([], {"identities": []}, {"identities": {"sub|厚土": {"mode": "fate"}}}):
                    body["meditation"] = invalid
                    self.assertFalse(client.post("/api/automation-settings", json=body).json()["success"])
                    self.assertEqual(self.path.read_bytes(), before)
        finally:
            dashboard.app.dependency_overrides.clear()
            dashboard.app.dependency_overrides.update(before_overrides)


if __name__ == "__main__":
    unittest.main()
