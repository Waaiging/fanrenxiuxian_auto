import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError
from common_command_features import CommonCommandMixin
from miniapp_dwelling import (
    MiniAppCommandResponse,
    MiniAppDwellingTransport,
    apply_dwelling_snapshot,
    miniapp_command_allowed,
    sect_farm_action_result_ok,
    sect_farm_collection_due,
    sect_farm_snapshot_status,
    normalize_pinned_entry_url,
    pinned_entry_url_candidates,
)
from miniapp_command_routing import MiniAppCommandRouter
from dashboard_server import apply_command_execution_channels
from restricted_miniapp_worker import RestrictedMiniAppWorker, _periodic_wait_seconds


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
START = {
    "ok": True,
    "identity": {
        "selectedPlayerId": 100,
        "choices": [
            {
                "playerId": 100,
                "daoName": "主号道名",
                "username": "MainUser",
                "source": "personal",
            },
            {
                "playerId": -200,
                "daoName": "素心子",
                "username": "avatar_user",
                "source": "bound_character",
            },
        ],
    },
}


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []
        self.error_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else message

    def info(self, message, *args):
        self.info_messages.append(self._format(message, args))

    def warning(self, message, *args, **kwargs):
        self.warning_messages.append(self._format(message, args))

    def error(self, message, *args, **kwargs):
        self.error_messages.append(self._format(message, args))


class MiniAppDwellingTests(unittest.TestCase):
    def test_pinned_entry_url_parser_accepts_text_and_button_links(self):
        self.assertEqual(
            normalize_pinned_entry_url(
                "https://t.me/fanrenxiuxian_bot?startapp=df_newtoken12345"
            ),
            "https://t.me/fanrenxiuxian_bot?startapp=df_newtoken12345",
        )
        message = SimpleNamespace(
            raw_text="最新入口：https://t.me/fanrenxiuxian_bot?startapp=df_newtoken12345。",
            entities=[],
            buttons=[],
        )
        self.assertEqual(
            pinned_entry_url_candidates(message),
            ["https://t.me/fanrenxiuxian_bot?startapp=df_newtoken12345"],
        )

    def test_initialize_refreshes_expired_entry_from_pinned_message(self):
        new_entry = "https://t.me/fanrenxiuxian_bot?startapp=df_newtoken12345"
        calls = []

        class Client:
            async def get_messages(self, chat, **kwargs):
                self.chat = chat
                self.filter = kwargs.get("filter")
                return SimpleNamespace(
                    raw_text=f"入口：{new_entry}", entities=[], buttons=[]
                )

            async def get_entity(self, username):
                return object()

            async def get_input_entity(self, entity):
                return object()

            async def __call__(self, request):
                return SimpleNamespace(url="https://asc.aiopenai.app/miniapp#tgWebAppData=signed")

        async def post_json(origin, path, payload, timeout):
            calls.append((path, payload["token"]))
            if payload["token"] == "df_fixture":
                raise MiniAppBeastError("dwelling_token_expired")
            return START

        with tempfile.TemporaryDirectory() as directory:
            config_file = f"{directory}/config.json"
            with open(config_file, "w", encoding="utf-8") as handle:
                json.dump({"miniapp_beast": {"entry_url": ENTRY}}, handle)
            transport = MiniAppDwellingTransport(
                Client(), ENTRY, post_json=post_json, config_file=config_file
            )
            with patch(
                "miniapp_dwelling.request_webview_init_data",
                new=AsyncMock(return_value="signed"),
            ):
                payload = asyncio.run(transport.initialize())
            with open(config_file, encoding="utf-8") as handle:
                persisted = json.load(handle)

        self.assertEqual(payload, START)
        self.assertEqual(calls, [("/api/miniapp/xianxia-dwelling/start", "df_fixture"), ("/api/miniapp/xianxia-dwelling/start", "df_newtoken12345")])
        self.assertEqual(persisted["miniapp_beast"]["entry_url"], new_entry)

    def test_periodic_sync_without_previous_timestamp_runs_immediately(self):
        self.assertEqual(_periodic_wait_seconds("", 12 * 3600), 0)
        self.assertEqual(_periodic_wait_seconds("not-a-timestamp", 12 * 3600), 0)

    def test_star_farm_cooldown_batches_same_star_without_fixed_polling(self):
        status = sect_farm_snapshot_status({
            "domain": {
                "mode": "stars",
                "plots": [
                    {"key": "1", "name": "天雷星", "remainingSeconds": 100},
                    {"key": "2", "name": "天雷星", "remainingSeconds": 112},
                    {"key": "3", "name": "建木星", "remainingSeconds": 80},
                ],
            }
        })

        self.assertEqual(status["next_wait_seconds"], 80)

    def test_star_farm_accepts_harmless_noop_results(self):
        self.assertTrue(sect_farm_action_result_ok({
            "actionResult": {"ok": False, "error": "nothing_to_soothe"},
        }, "soothe"))
        self.assertTrue(sect_farm_action_result_ok({
            "actionResult": {"ok": False, "error": "nothing_ready"},
        }, "collect"))
        self.assertFalse(sect_farm_action_result_ok({
            "actionResult": {"ok": False, "error": "permission_denied"},
        }, "collect"))

    def test_star_farm_collection_waits_for_the_whole_batch(self):
        self.assertFalse(sect_farm_collection_due(3, 0, 18))
        self.assertFalse(sect_farm_collection_due(0, 2, 18))
        self.assertTrue(sect_farm_collection_due(8, 0, 0))
        self.assertTrue(sect_farm_collection_due(0, 8, 0))

    def test_command_whitelist_rejects_group_only_actions(self):
        for command in (
            ".闭关修炼",
            ".元婴出窍",
            ".我的侍妾",
            ".安置侍妾",
            ".拼图",
            ".登天阶",
            ".观命",
            ".定命 紫微",
            ".推命 闭关",
            ".改命 探索",
            ".问道",
            ".小世界",
            ".显灵",
            ".安抚信徒",
            ".神迹 布道",
            ".我的阴罗幡",
            ".每日献祭",
            ".血洗山林",
            ".召唤魔影",
            ".召回魔影",
            ".一键安抚幡灵",
            ".一键收取精华",
            ".收取精华 1",
            ".化功为煞 10000",
            ".囚禁魂魄 1 凶兽戾魄",
            ".安抚幡灵 1",
        ):
            self.assertTrue(miniapp_command_allowed(command), command)
        for command in (
            ".切换 主魂",
            ".洞府",
            ".我的灵兽",
            ".灵兽巡边 大圣 袭营",
            ".巡边归来",
            ".定命 不存在",
            ".接取解咒委托 19",
            ".辨认咒纹 @Weeguu",
            ".借幡镇魂 @Weeguu",
            ".剥离咒源 @Weeguu",
        ):
            self.assertFalse(miniapp_command_allowed(command), command)

    def test_dashboard_labels_supported_and_group_only_yinluo_commands(self):
        miniapp_commands = (
            ".寻觅灵兽",
            ".问道",
            ".小世界",
            ".显灵",
            ".安抚信徒",
            ".神迹 布道",
            ".我的阴罗幡",
            ".每日献祭",
            ".召回魔影",
            ".一键安抚幡灵",
            ".一键收取精华",
            ".收取精华 <槽位>",
            ".囚禁魂魄 <槽位> 凶兽戾魄",
        )
        group_commands = (
            ".接取解咒委托 19",
            ".辨认咒纹 @Weeguu",
            ".借幡镇魂 @Weeguu",
            ".剥离咒源 @Weeguu",
        )
        commands = miniapp_commands + group_commands
        panel = {"commands": [{"command": command} for command in commands]}

        apply_command_execution_channels(panel, root_state={"miniapp_route_active": False})

        rows = {row["command"]: row for row in panel["commands"]}
        for command in miniapp_commands:
            self.assertEqual(rows[command]["execution_channel"], "miniapp", command)
            self.assertIn("不回退群内", rows[command]["execution_channel_detail"])
        for command in group_commands:
            self.assertEqual(rows[command]["execution_channel"], "group", command)
            self.assertIn("继续在群内发送", rows[command]["execution_channel_detail"])

    def test_router_install_failure_blocks_supported_commands_without_group_fallback(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {}
                self.avatars = []
                self.group_sent = []

            async def send_and_wait_feedback(self, command, *args, **kwargs):
                self.group_sent.append(command)
                return f"group:{command}"

            def save_state(self):
                pass

        actor = Actor()
        router = MiniAppCommandRouter(actor, "main", logger=FakeLogger())
        router.transport.initialize = AsyncMock(side_effect=MiniAppBeastError("fixture_failure"))

        installed = asyncio.run(router.install())
        blocked = asyncio.run(actor.send_and_wait_feedback(".问道"))
        group_only = asyncio.run(actor.send_and_wait_feedback(".洞府"))

        self.assertFalse(installed)
        self.assertIsNone(blocked)
        self.assertEqual(group_only, "group:.洞府")
        self.assertEqual(actor.group_sent, [".洞府"])

    def test_router_unavailable_log_is_throttled_after_expired_entry(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {}
                self.avatars = []

            async def send_and_wait_feedback(self, command, *args, **kwargs):
                return f"group:{command}"

            def save_state(self):
                pass

        actor = Actor()
        logger = FakeLogger()
        router = MiniAppCommandRouter(actor, "main", logger=logger)
        router.transport.initialize = AsyncMock(
            side_effect=MiniAppBeastError("dwelling_token_expired")
        )

        self.assertFalse(asyncio.run(router.install()))
        asyncio.run(actor.send_and_wait_feedback(".问道"))
        asyncio.run(actor.send_and_wait_feedback(".问道"))

        self.assertEqual(len(logger.warning_messages), 1)
        self.assertIn("automatic route recovery is pending", logger.warning_messages[0])
        self.assertIn("fixed entry token expired", logger.error_messages[0])

    def test_router_recovers_after_transient_initialization_failure(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {}
                self.avatars = []
                self.is_running = True

            async def send_and_wait_feedback(self, command, *args, **kwargs):
                return f"group:{command}"

            def save_state(self):
                pass

        actor = Actor()
        logger = FakeLogger()
        router = MiniAppCommandRouter(
            actor,
            "main",
            logger=logger,
            start_background_tasks=False,
        )
        initialize_forces = []

        async def initialize(force=False):
            initialize_forces.append(force)
            if len(initialize_forces) == 1:
                raise TimeoutError("temporary timeout")
            router.transport.identity_player_ids = {"主魂": 100}

        router.transport.initialize = initialize
        router.transport.overview = AsyncMock(return_value={})

        async def scenario():
            self.assertFalse(await router.install())
            self.assertTrue(router.enabled)
            self.assertFalse(router._route_active)
            with patch("miniapp_command_routing.asyncio.sleep", new=AsyncMock()):
                await router.run_route_recovery_loop()

        asyncio.run(scenario())

        self.assertEqual(initialize_forces, [False, True])
        self.assertTrue(router._route_active)
        self.assertTrue(actor.state["miniapp_route_active"])
        self.assertEqual(actor.state["miniapp_route_last_error"], "")
        self.assertTrue(any("recovered" in row for row in logger.warning_messages))

    def test_runtime_circuit_open_starts_route_recovery_without_error_log(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {}
                self.avatars = []

            def save_state(self):
                pass

        actor = Actor()
        logger = FakeLogger()
        router = MiniAppCommandRouter(actor, "main", logger=logger)
        router._route_active = True
        router.transport.identity_player_ids = {"主魂": 100}
        router._maybe_refresh_auth = AsyncMock()
        router._start_recovery_task = Mock()
        router.transport.command = AsyncMock(
            side_effect=MiniAppCircuitOpenError(900, "2026-08-19 12:00:00")
        )

        result = asyncio.run(
            router._route("主魂", ".问道", AsyncMock(), (), {})
        )

        self.assertIsNone(result)
        self.assertFalse(router._route_active)
        self.assertFalse(actor.state["miniapp_route_active"])
        self.assertEqual(actor.state["miniapp_route_last_error"], "miniapp_circuit_open")
        self.assertEqual(actor.state["miniapp_route_retry_at"], "2026-08-19 12:00:00")
        router._start_recovery_task.assert_called_once_with()
        self.assertEqual(logger.error_messages, [])

    def test_soul_curse_chain_routes_to_group(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {"avatars": {"缘生子": {}}}
                self.avatars = ["缘生子"]

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                pass

            def identity_pause_seconds(self, identity):
                return 0

        actor = Actor()
        router = MiniAppCommandRouter(actor, "sub", logger=FakeLogger())
        router.transport.identity_player_ids = {"主魂": 100, "缘生子": -200}
        fallback = AsyncMock(return_value="group fallback")

        response = asyncio.run(
            router._route("缘生子", ".借幡镇魂 @Weeguu", fallback, (), {})
        )

        self.assertEqual(response, "group fallback")
        fallback.assert_awaited_once_with(".借幡镇魂 @Weeguu")

    def test_router_routes_group_only_heart_trial_with_reply_target_to_telegram(self):
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={"avatars": {"缘生子": {}}},
            avatars=["缘生子"],
            save_state=lambda: None,
            identity_pause_seconds=lambda identity: 0,
            dashboard_command_paused=lambda command, identity: False,
        )
        router = MiniAppCommandRouter(actor, "main", logger=FakeLogger())
        router._route_active = True
        router.transport.identity_player_ids = {"主魂": 100, "缘生子": -200}
        router.transport.command = AsyncMock()
        router._maybe_refresh_auth = AsyncMock()
        fallback = AsyncMock(return_value="group fallback")

        response = asyncio.run(router._route(
            "缘生子",
            ".共历心劫",
            fallback,
            (),
            {"reply_to": 2500, "return_response_msg": True},
        ))

        self.assertEqual(response, "group fallback")
        router.transport.command.assert_not_awaited()
        fallback.assert_awaited_once_with(
            ".共历心劫",
            reply_to=2500,
            return_response_msg=True,
        )

    def test_main_and_sub_yuanshengzi_yinluo_commands_route_to_miniapp(self):
        commands = (
            ".我的阴罗幡",
            ".囚禁魂魄 1 凶兽戾魄",
            ".一键安抚幡灵",
            ".收取精华 1",
        )

        for account in ("main", "sub"):
            with self.subTest(account=account):
                actor = SimpleNamespace(
                    client=object(),
                    config={"miniapp_beast": {"entry_url": ENTRY}},
                    state={"avatars": {"缘生子": {}}},
                    avatars=["缘生子"],
                    save_state=lambda: None,
                    identity_pause_seconds=lambda identity: 0,
                )
                router = MiniAppCommandRouter(actor, account, logger=FakeLogger())
                router._route_active = True
                router.transport.identity_player_ids = {"主魂": 100, "缘生子": -200}
                router.transport.command = AsyncMock(
                    return_value=MiniAppCommandResponse("完成", {"actionResult": {"ok": True}})
                )
                router._maybe_refresh_auth = AsyncMock()
                fallback = AsyncMock(return_value="group fallback")

                for command in commands:
                    response = asyncio.run(
                        router._route("缘生子", command, fallback, (), {})
                    )
                    self.assertEqual(response, "完成")

                self.assertEqual(
                    [call.args for call in router.transport.command.await_args_list],
                    [(command,) for command in commands],
                )
                self.assertTrue(all(call.kwargs == {"identity": "缘生子"} for call in router.transport.command.await_args_list))
                fallback.assert_not_awaited()

    def test_xiaohao_and_waaiging_route_supported_commands_without_group_fallback(self):
        commands = (
            ("主魂", ".查看闭关"),
            ("主魂", ".闭关修炼"),
            ("主魂", ".深度闭关"),
            ("主魂", ".元婴出窍"),
            ("主魂", ".安置侍妾"),
            ("问心子", ".登天阶"),
        )

        for account in ("xiaohao", "waaiging"):
            with self.subTest(account=account):
                avatars = ["问心子"] if account == "xiaohao" else []
                actor = SimpleNamespace(
                    client=object(),
                    config={"miniapp_beast": {"entry_url": ENTRY}},
                    state={"avatars": {name: {} for name in avatars}},
                    avatars=avatars,
                    save_state=lambda: None,
                    identity_pause_seconds=lambda identity: 0,
                )
                router = MiniAppCommandRouter(actor, account, logger=FakeLogger())
                router._route_active = True
                router.transport.identity_player_ids = {"主魂": 100, "问心子": -200}
                router.transport.command = AsyncMock(
                    return_value=MiniAppCommandResponse(
                        "完成", {"actionResult": {"ok": True}}
                    )
                )
                router._maybe_refresh_auth = AsyncMock()
                fallback = AsyncMock(return_value="group fallback")

                for identity, command in commands:
                    if identity != "主魂" and identity not in avatars:
                        continue
                    response = asyncio.run(
                        router._route(identity, command, fallback, (), {})
                    )
                    self.assertEqual(response, "完成")

                fallback.assert_not_awaited()

    def test_router_can_reuse_transport_without_starting_duplicate_background_tasks(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {}
                self.avatars = []

            async def send_and_wait_feedback(self, command, *args, **kwargs):
                return f"group:{command}"

            def save_state(self):
                pass

        actor = Actor()
        transport = MiniAppDwellingTransport(object(), ENTRY)
        transport.initialize = AsyncMock()
        transport.identity_player_ids = {"主魂": 100}
        transport.overview = AsyncMock(return_value={})
        router = MiniAppCommandRouter(
            actor,
            "xiaohao",
            logger=FakeLogger(),
            transport=transport,
            start_background_tasks=False,
        )

        self.assertTrue(asyncio.run(router.install()))
        self.assertIs(router.transport, transport)
        self.assertIsNone(router._profile_task)
        self.assertEqual(router._star_farm_tasks, [])
        self.assertEqual(router._daily_activity_tasks, [])

    def test_identity_mapping_and_command_routes(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/start"):
                return START
            return {
                "ok": True,
                "actionResult": {"ok": True, "rawMessage": f"reply:{payload.get('command') or payload.get('action') or 'cultivation'}"},
                "dwelling": {"meditation": {"deepSeclusion": {"active": True}}},
            }

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            asyncio.run(transport.initialize())
            response = asyncio.run(transport.command(".元婴出窍", identity="素心子"))
            puzzle = asyncio.run(transport.command(".拼图", identity="素心子"))
            placed = asyncio.run(transport.command(".安置侍妾", identity="主魂"))
            status = asyncio.run(transport.command(".查看闭关", identity="主魂"))
            manifest = asyncio.run(transport.command(".显灵", identity="主魂"))
            soothe = asyncio.run(transport.command(".安抚信徒", identity="主魂"))
            collected = asyncio.run(transport.small_world_action("主魂", "collect"))

        self.assertEqual(transport.player_id("主魂"), 100)
        self.assertEqual(transport.player_id("素心子"), -200)
        self.assertEqual(response.text, "reply:.元婴出窍")
        self.assertEqual(puzzle.text, "reply:.拼图")
        self.assertEqual(placed.text, "reply:.安置侍妾")
        self.assertEqual(status.text, "reply:status")
        self.assertEqual(manifest.text, "reply:manifest")
        self.assertEqual(soothe.text, "reply:soothe")
        self.assertEqual(collected["actionResult"]["rawMessage"], "reply:collect")
        self.assertEqual(calls[1][0], "/api/miniapp/xianxia-dwelling/command-center")
        self.assertEqual(calls[1][1]["playerId"], -200)
        self.assertEqual(calls[2][0], "/api/miniapp/xianxia-dwelling/command-center")
        self.assertEqual(calls[2][1]["command"], ".拼图")
        self.assertEqual(calls[3][0], "/api/miniapp/xianxia-dwelling/command-center")
        self.assertEqual(calls[3][1]["command"], ".安置侍妾")
        self.assertEqual(calls[4][0], "/api/miniapp/xianxia-dwelling/deep-seclusion")
        self.assertEqual(calls[5][0], "/api/miniapp/xianxia-dwelling/small-world")
        self.assertEqual(calls[5][1]["action"], "manifest")
        self.assertEqual(calls[6][0], "/api/miniapp/xianxia-dwelling/small-world")
        self.assertEqual(calls[6][1]["action"], "soothe")
        self.assertEqual(calls[7][0], "/api/miniapp/xianxia-dwelling/small-world")
        self.assertEqual(calls[7][1]["action"], "collect")

    def test_cultivation_endpoint_returns_top_level_message(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, payload))
            if path.endswith("/start"):
                return START
            self.assertEqual(path, "/api/miniapp/xianxia-dwelling/cultivation")
            return {
                "ok": True,
                "message": "【闭关成功】修为增加了 100 点，需要调息 10 分钟。",
            }

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            response = asyncio.run(transport.command(".闭关修炼", identity="主魂"))

        self.assertIn("【闭关成功】", response.text)
        self.assertIn("需要调息 10 分钟", response.text)
        self.assertEqual(calls[-1][0], "/api/miniapp/xianxia-dwelling/cultivation")

    def test_forge_treasure_uses_storage_bag_endpoint(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/forge/craft"):
                return {
                    "ok": True,
                    "actionResult": {"ok": True, "rawMessage": "炼制玄铁剑成功"},
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            result = asyncio.run(transport.forge_treasure("主魂", "treasure_001", 1))

        self.assertTrue(result["actionResult"]["ok"])
        self.assertEqual(calls[-1][0], "/api/miniapp/xianxia-dwelling/forge/craft")
        self.assertEqual(calls[-1][1]["targetItemId"], "treasure_001")
        self.assertEqual(calls[-1][1]["times"], 1)
        self.assertEqual(calls[-1][1]["playerId"], 100)
    def test_completed_status_is_settled_for_maintenance_loop(self):
        calls = []
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            calls.append((path, payload.get("action")))
            if path.endswith("/start"):
                return START
            if payload.get("action") == "status":
                return {
                    "ok": True,
                    "actionResult": {"ok": True, "rawMessage": "闭关已圆满，可结算。"},
                    "dwelling": {"meditation": {"deepSeclusion": {"completed": True, "canSettle": True}}},
                }
            return {
                "ok": True,
                "actionResult": {"ok": True, "rawMessage": "【深度闭关总结】修为增加。"},
                "dwelling": {"meditation": {"deepSeclusion": {"active": False}}},
            }

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            response = asyncio.run(transport.command(".查看闭关"))

        self.assertIn("深度闭关总结", response.text)
        self.assertEqual(calls[-2:], [
            ("/api/miniapp/xianxia-dwelling/deep-seclusion", "status"),
            ("/api/miniapp/xianxia-dwelling/deep-seclusion", "settle"),
        ])
        combined = "\n".join(logger.info_messages)
        self.assertIn("OUT [Mini App | 主魂]:\n指令 .查看闭关", combined)
        self.assertIn("IN [Mini App | 主魂]:\n指令 .查看闭关 -> 闭关已圆满，可结算。", combined)
        self.assertIn("OUT [Mini App | 主魂]:\n深度闭关自动结算", combined)
        self.assertIn("IN [Mini App | 主魂]:\n深度闭关自动结算 -> 【深度闭关总结】修为增加。", combined)

    def test_star_farm_uses_scoped_external_token(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {"ok": True, "url": "/miniapp/xianxia-sect-farm?startapp=farm_fixture"}
            if path.endswith("/xianxia-sect-farm/start"):
                return {"ok": True, "domain": {"mode": "stars", "plots": []}}
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            payload = asyncio.run(transport.sect_farm_snapshot("素心子"))

        self.assertEqual(payload["domain"]["mode"], "stars")
        external = next(item for item in calls if item[0].endswith("/external"))
        self.assertEqual(external[1]["playerId"], -200)
        self.assertEqual(external[1]["action"], "sect_farm")

    def test_user_visible_miniapp_operations_are_logged_but_polling_and_batch_items_are_quiet(self):
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/details"):
                return {"ok": True, "account": {"playerId": payload["playerId"]}}
            if path.endswith("/xianxia-dwelling/overview"):
                return {
                    "ok": True,
                    "snapshot": {"level": "overview"},
                    "account": {
                        "playerId": payload["playerId"],
                        "cultivationLevel": "元婴初期",
                        "profile": {
                            "sectName": "天星宗",
                            "spiritRoot": {"name": "异灵根(风)"},
                            "cultivation": {"current": 100, "next": 500000},
                        },
                    },
                }
            if path.endswith("/xianxia-dwelling/command-center"):
                return {"ok": True, "actionResult": {"ok": True, "rawMessage": "元婴已出窍"}}
            if path.endswith("/xianxia-dwelling/external"):
                if payload["action"] == "spirit_beast":
                    return {"ok": True, "url": "/miniapp/spirit?startapp=spiritbeast_fixture"}
                return {"ok": True, "url": "/miniapp/farm?startapp=farm_fixture"}
            if path.endswith("/xianxia-spirit-beast/start"):
                return {
                    "ok": True,
                    "beasts": [{
                        "id": 7,
                        "name": "大圣",
                        "beastType": "金瞳妖猴",
                        "tier": 3,
                        "stamina": 42,
                    }],
                }
            if path.endswith("/xianxia-spirit-beast/action"):
                return {"ok": True, "message": "大圣安抚完成"}
            if path.endswith("/xianxia-sect-farm/start"):
                return {"ok": True, "domain": {"mode": "stars", "plots": [{"plotKey": "1"}]}}
            if path.endswith("/xianxia-sect-farm/action"):
                return {"ok": True, "actionResult": {"ok": True, "message": "安抚完成"}}
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            asyncio.run(transport.details("主魂"))
            asyncio.run(transport.overview("主魂"))
            asyncio.run(transport.command(".元婴出窍", identity="素心子"))
            asyncio.run(transport.spirit_beast_snapshot("主魂"))
            asyncio.run(transport.spirit_beast_interaction("主魂", 7, "安抚"))
            asyncio.run(transport.sect_farm_snapshot("素心子"))
            asyncio.run(transport.sect_farm_action("素心子", "soothe"))

        combined = "\n".join(logger.info_messages)
        self.assertIn("OUT [Mini App | 主魂]:\n同步洞府详情", combined)
        self.assertIn("IN [Mini App | 主魂]:\n同步洞府详情 -> 完成", combined)
        self.assertNotIn("同步洞府首页", combined)
        self.assertIn("IN [Mini App | 素心子]:\n指令 .元婴出窍 -> 元婴已出窍", combined)
        self.assertIn("IN [Mini App | 主魂]:\n读取万兽谷灵兽列表 -> 1 只灵兽", combined)
        self.assertNotIn("万兽谷灵兽安抚（ID 7）", combined)
        self.assertNotIn("读取宗门灵圃", combined)
        self.assertIn("IN [Mini App | 素心子]:\n宗门灵圃安抚星辰 -> 安抚完成", combined)

    def test_spirit_beast_seek_and_release_use_exact_new_beast_id(self):
        logger = FakeLogger()
        calls = []

        def beast(beast_id, name, beast_type):
            return {
                "id": beast_id,
                "name": name,
                "beastType": beast_type,
                "tier": 1,
                "stamina": 100,
                "combatPower": 100,
                "status": "休息中",
            }

        existing = beast(7, "六翼", "六翼霜蚣")
        newly_found = beast(99, "新来的风雀", "风雀")

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {
                    "ok": True,
                    "url": "/miniapp/xianxia-spirit-beast?startapp=spiritbeast_fixture",
                }
            if path.endswith("/xianxia-spirit-beast/action"):
                if payload["action"] == "seek":
                    return {
                        "ok": True,
                        "message": "新来的风雀循着兽迹进入了灵兽袋。",
                        "beasts": [existing, newly_found],
                    }
                self.assertEqual(payload["action"], "release")
                self.assertEqual(payload["beastId"], 99)
                self.assertTrue(payload["confirm"])
                return {
                    "ok": True,
                    "message": "已解除与【新来的风雀】的灵契。",
                    "beasts": [existing],
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            sought = asyncio.run(transport.spirit_beast_seek("主魂"))
            released = asyncio.run(
                transport.spirit_beast_release("主魂", 99, "新来的风雀")
            )

        self.assertEqual({item["id"] for item in sought["beasts"]}, {7, 99})
        self.assertEqual({item["id"] for item in released["beasts"]}, {7})
        action_calls = [body for path, body in calls if path.endswith("/action")]
        self.assertEqual(action_calls[0]["action"], "seek")
        self.assertEqual(action_calls[1]["action"], "release")
        self.assertEqual(action_calls[1]["beastId"], 99)
        combined = "\n".join(logger.info_messages)
        self.assertIn("OUT [Mini App | 主魂]:\n万兽谷寻觅灵兽", combined)
        self.assertIn("万兽谷放生新寻灵兽（新来的风雀）", combined)

    def test_spirit_beast_rest_uses_exact_id(self):
        logger = FakeLogger()
        calls = []
        rested = {
            "id": 7,
            "name": "六翼",
            "beastType": "太古冰蜈",
            "tier": 4,
            "stamina": 34,
            "combatPower": 4096,
            "status": "休息中",
        }

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {
                    "ok": True,
                    "url": "/miniapp/xianxia-spirit-beast?startapp=spiritbeast_fixture",
                }
            if path.endswith("/xianxia-spirit-beast/action"):
                self.assertEqual(payload["action"], "rest")
                self.assertEqual(payload["beastId"], 7)
                return {
                    "ok": True,
                    "message": "六翼已返回灵兽袋。",
                    "beasts": [rested],
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(), ENTRY, logger=logger, post_json=post_json
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            result = asyncio.run(transport.spirit_beast_rest("主魂", 7, "六翼"))

        self.assertEqual(result["beasts"][0]["status"], "休息中")
        self.assertEqual(calls[-1][1]["action"], "rest")
        self.assertIn("万兽谷灵兽休息（六翼）", "\n".join(logger.info_messages))

    def test_pagoda_logs_action_while_hunt_steps_stay_silent(self):
        logger = FakeLogger()
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload), timeout))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {
                    "ok": True,
                    "url": "/miniapp/xianxia-pagoda?startapp=pagoda_fixture",
                }
            if path.endswith("/xianxia-pagoda/start"):
                return {"ok": True, "state": {"canChallenge": True}}
            if path.endswith("/xianxia-pagoda/challenge"):
                return {
                    "ok": True,
                    "state": {"canChallenge": False},
                    "replay": {
                        "clearedCount": 20,
                        "endFloor": 20,
                        "failedFloor": 21,
                        "report": "总收获:\n修为增加了 1200 点\n获得塔印 40 点。",
                    },
                }
            if path.endswith("/xianxia-dwelling/details"):
                return {
                    "ok": True,
                    "dwelling": {
                        "hunt": {"used": 0, "limit": 3, "remaining": 3, "actionPoints": 8}
                    },
                }
            if path.endswith("/xianxia-dwelling/hunt"):
                return {
                    "ok": True,
                    "huntRun": {"sessionId": "hunt-1", "ap": 8, "maxAp": 8},
                }
            if path.endswith("/xianxia-dwelling/hunt/reveal"):
                return {
                    "ok": True,
                    "huntRun": {
                        "sessionId": "hunt-1",
                        "ap": 7,
                        "cells": [
                            {
                                "index": 6,
                                "revealed": True,
                                "title": "主宝匣",
                                "loot": {"name": "阴凝之晶", "quantity": 1},
                            }
                        ],
                    },
                }
            if path.endswith("/xianxia-dwelling/hunt/settle"):
                return {
                    "ok": True,
                    "huntResult": {
                        "grade": "甲等",
                        "score": 90,
                        "foundMain": True,
                        "loot": [{"name": "阴凝之晶", "quantity": 1}],
                    },
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            asyncio.run(transport.pagoda_snapshot("素心子"))
            asyncio.run(transport.pagoda_challenge("素心子"))
            asyncio.run(transport.hunt_snapshot("素心子"))
            asyncio.run(transport.hunt_start("素心子"))
            asyncio.run(transport.hunt_reveal("素心子", "hunt-1", 6))
            asyncio.run(transport.hunt_settle("素心子", "hunt-1"))

        external = next(call for call in calls if call[0].endswith("/external"))
        self.assertEqual(external[1]["action"], "pagoda")
        self.assertEqual(external[1]["playerId"], -200)
        challenge = next(call for call in calls if call[0].endswith("/xianxia-pagoda/challenge"))
        self.assertGreaterEqual(challenge[2], 60)
        reveal = next(call for call in calls if call[0].endswith("/hunt/reveal"))
        self.assertEqual(reveal[1]["playerId"], -200)
        self.assertEqual(reveal[1]["sessionId"], "hunt-1")
        self.assertEqual(reveal[1]["index"], 6)
        combined = "\n".join(logger.info_messages)
        self.assertIn("OUT [Mini App | 素心子]:\n琉璃问心塔一念登塔", combined)
        self.assertIn("奖励：修为+1,200｜塔印+40", combined)
        self.assertNotIn("洞府寻宝入府", combined)
        self.assertNotIn("洞府寻宝探查", combined)
        self.assertNotIn("洞府寻宝见好就收", combined)

    def test_tianji_trial_uses_identity_scoped_external_token(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {
                    "ok": True,
                    "url": "/miniapp/xianxia-trial?startapp=trial_fixture",
                }
            if path.endswith("/xianxia-trial/start"):
                return {
                    "ok": True,
                    "dailyProgress": {"completed": 0, "limit": 3},
                    "challenge": {
                        "challengeId": "trial-1",
                        "mode": "tianjiMeridianV1",
                    },
                }
            if path.endswith("/xianxia-trial/finish"):
                return {
                    "ok": True,
                    "dailyProgress": {"completed": 1, "limit": 3},
                    "result": {"grade": "甲等", "daily_progress": 1, "daily_limit": 3},
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            started = asyncio.run(transport.tianji_trial_start("素心子"))
            settled = asyncio.run(
                transport.tianji_trial_finish(
                    "素心子",
                    {
                        "mode": "tianjiMeridianV1",
                        "challengeId": "trial-1",
                        "durationMs": 4000,
                        "events": [],
                    },
                )
            )

        self.assertEqual(started["challenge"]["challengeId"], "trial-1")
        self.assertEqual(settled["dailyProgress"]["completed"], 1)
        external = next(call for call in calls if call[0].endswith("/external"))
        self.assertEqual(external[1]["action"], "tianji_trial")
        self.assertEqual(external[1]["playerId"], -200)
        finish = next(call for call in calls if call[0].endswith("/xianxia-trial/finish"))
        self.assertEqual(finish[1]["token"], "trial_fixture")
        self.assertEqual(finish[1]["trialProof"]["challengeId"], "trial-1")

    def test_tianji_trial_start_refreshes_one_use_entry_token(self):
        calls = []
        token_index = 0

        async def post_json(origin, path, payload, timeout):
            nonlocal token_index
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                token_index += 1
                return {
                    "ok": True,
                    "url": f"/miniapp/xianxia-trial?startapp=trial_{token_index}",
                }
            if path.endswith("/xianxia-trial/start"):
                return {
                    "ok": True,
                    "dailyProgress": {"completed": 0, "limit": 3},
                    "challenge": {
                        "challengeId": f"challenge-{token_index}",
                        "mode": "tianjiMeridianV1",
                    },
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch(
            "miniapp_dwelling.request_webview_init_data",
            new=AsyncMock(return_value="signed"),
        ):
            asyncio.run(transport.tianji_trial_start("素心子"))
            asyncio.run(transport.tianji_trial_start("素心子"))

        external_calls = [call for call in calls if call[0].endswith("/external")]
        start_calls = [call for call in calls if call[0].endswith("/xianxia-trial/start")]
        self.assertEqual(len(external_calls), 2)
        self.assertEqual(
            [call[1]["token"] for call in start_calls],
            ["trial_1", "trial_2"],
        )

    def test_fate_cards_uses_real_external_action_and_page_endpoints(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload), timeout))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {
                    "ok": True,
                    "url": "/miniapp/xianxia-fate-cards?startapp=fate_fixture",
                }
            if path.endswith("/xianxia-fate-cards/start"):
                return {"ok": True, "hasDrawn": False}
            if path.endswith("/xianxia-fate-cards/draw"):
                return {"ok": True, "record": {"questionKey": payload["questionKey"]}}
            if path.endswith("/xianxia-fate-cards/interpret"):
                return {"ok": True, "record": {"aiReading": {"overview": "命解"}}}
            if path.endswith("/xianxia-fate-cards/choose"):
                return {"ok": True, "record": {"choiceKey": payload["choiceKey"]}}
            if path.endswith("/xianxia-fate-cards/settle"):
                return {"ok": True, "record": {"quest": {"status": "settled"}}}
            if path.endswith("/xianxia-dwelling/deep-seclusion"):
                return {"ok": True, "actionResult": {"ok": True, "rawMessage": "已处理"}}
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch(
            "miniapp_dwelling.request_webview_init_data",
            new=AsyncMock(return_value="signed"),
        ):
            asyncio.run(transport.fate_cards_start("素心子"))
            asyncio.run(transport.fate_cards_draw("素心子", "opportunity"))
            asyncio.run(transport.fate_cards_interpret("素心子"))
            asyncio.run(transport.fate_cards_choose("素心子", "hide"))
            asyncio.run(transport.fate_cards_settle("素心子"))
            asyncio.run(transport.deep_seclusion_action("素心子", "force", log_operation=False))
            response = asyncio.run(transport.command(".强行出关", identity="素心子"))

        external = next(call for call in calls if call[0].endswith("/external"))
        self.assertEqual(external[1]["action"], "fate_cards")
        self.assertEqual(external[1]["playerId"], -200)
        fate_calls = [call for call in calls if "/xianxia-fate-cards/" in call[0]]
        self.assertTrue(all(call[1]["token"] == "fate_fixture" for call in fate_calls))
        draw = next(call for call in fate_calls if call[0].endswith("/draw"))
        choose = next(call for call in fate_calls if call[0].endswith("/choose"))
        interpret = next(call for call in fate_calls if call[0].endswith("/interpret"))
        self.assertEqual(draw[1]["questionKey"], "opportunity")
        self.assertEqual(choose[1]["choiceKey"], "hide")
        self.assertGreaterEqual(interpret[2], 60)
        deep_actions = [
            call[1]["action"]
            for call in calls
            if call[0].endswith("/deep-seclusion")
        ]
        self.assertEqual(deep_actions, ["force", "force"])
        self.assertEqual(response.text, "已处理")

        with self.assertRaisesRegex(MiniAppBeastError, "fate_cards_choice_invalid"):
            asyncio.run(transport.fate_cards_choose("素心子", "defy"))

    def test_tianji_trial_start_refreshes_expired_trial_session(self):
        calls = []
        token_index = 0

        async def post_json(origin, path, payload, timeout):
            nonlocal token_index
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                token_index += 1
                return {
                    "ok": True,
                    "url": f"/miniapp/xianxia-trial?startapp=trial_{token_index}",
                }
            if path.endswith("/xianxia-trial/start"):
                if payload["token"] == "trial_1":
                    raise MiniAppBeastError("trial_token_expired")
                return {
                    "ok": True,
                    "dailyProgress": {"completed": 0, "limit": 3},
                    "challenge": {
                        "challengeId": "trial-2",
                        "mode": "tianjiMeridianV1",
                    },
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch(
            "miniapp_dwelling.request_webview_init_data",
            new=AsyncMock(side_effect=["signed-old", "signed-new"]),
        ):
            result = asyncio.run(transport.tianji_trial_start("素心子"))

        self.assertEqual(result["challenge"]["challengeId"], "trial-2")
        external_calls = [call for call in calls if call[0].endswith("/external")]
        start_calls = [call for call in calls if call[0].endswith("/xianxia-trial/start")]
        self.assertEqual(len(external_calls), 2)
        self.assertEqual(
            [(call[1]["token"], call[1]["initData"]) for call in start_calls],
            [("trial_1", "signed-old"), ("trial_2", "signed-new")],
        )

    def test_tianji_trial_finish_does_not_retry_old_proof_with_new_session(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                return {
                    "ok": True,
                    "url": "/miniapp/xianxia-trial?startapp=trial_fixture",
                }
            if path.endswith("/xianxia-trial/start"):
                return {
                    "ok": True,
                    "dailyProgress": {"completed": 0, "limit": 3},
                    "challenge": {
                        "challengeId": "trial-1",
                        "mode": "tianjiMeridianV1",
                    },
                }
            if path.endswith("/xianxia-trial/finish"):
                raise MiniAppBeastError("hash_mismatch")
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch(
            "miniapp_dwelling.request_webview_init_data",
            new=AsyncMock(return_value="signed"),
        ):
            asyncio.run(transport.tianji_trial_start("素心子"))
            with self.assertRaisesRegex(MiniAppBeastError, "hash_mismatch"):
                asyncio.run(
                    transport.tianji_trial_finish(
                        "素心子",
                        {
                            "mode": "tianjiMeridianV1",
                            "challengeId": "trial-1",
                            "durationMs": 4000,
                            "events": [],
                        },
                    )
                )

        external_calls = [call for call in calls if call[0].endswith("/external")]
        finish_calls = [call for call in calls if call[0].endswith("/xianxia-trial/finish")]
        self.assertEqual(len(external_calls), 1)
        self.assertEqual(len(finish_calls), 1)

    def test_external_hash_mismatch_refreshes_init_data_and_entry_token_together(self):
        logger = FakeLogger()
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/external"):
                suffix = "new" if payload["initData"] == "signed-new" else "old"
                return {
                    "ok": True,
                    "url": f"/miniapp/xianxia-pagoda?startapp=pagoda_{suffix}",
                }
            if path.endswith("/xianxia-pagoda/start"):
                if payload["initData"] == "signed-old":
                    raise MiniAppBeastError("hash_mismatch")
                self.assertEqual(payload["token"], "pagoda_new")
                return {"ok": True, "state": {"canChallenge": True}}
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        init_data = AsyncMock(side_effect=["signed-old", "signed-new"])
        with patch("miniapp_dwelling.request_webview_init_data", new=init_data):
            result = asyncio.run(transport.pagoda_snapshot("素心子"))

        self.assertTrue(result["state"]["canChallenge"])
        self.assertEqual(init_data.await_count, 2)
        pagoda_calls = [call for call in calls if call[0].endswith("/xianxia-pagoda/start")]
        self.assertEqual(
            [(call[1]["token"], call[1]["initData"]) for call in pagoda_calls],
            [("pagoda_old", "signed-old"), ("pagoda_new", "signed-new")],
        )
        self.assertIn(
            "Mini App external authorization expired for pagoda; refreshing fixed entry",
            logger.warning_messages,
        )

    def test_failed_miniapp_operation_is_logged(self):
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            if path.endswith("/xianxia-dwelling/start"):
                return START
            raise MiniAppBeastError("fixture_failure")

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            with self.assertRaises(MiniAppBeastError):
                asyncio.run(transport.command(".元婴出窍"))

        self.assertIn(
            "Mini App [主魂] 指令 .元婴出窍失败：fixture_failure",
            logger.error_messages,
        )

    def test_tianji_operations_can_suppress_success_logs_but_keep_errors(self):
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            if path.endswith("/xianxia-dwelling/start"):
                return START
            if path.endswith("/xianxia-dwelling/command-center"):
                return {
                    "ok": True,
                    "actionResult": {"ok": True, "rawMessage": "推命炼制成功"},
                }
            if path.endswith("/xianxia-dwelling/forge/craft"):
                return {
                    "ok": True,
                    "actionResult": {"ok": True, "rawMessage": "玄铁剑炼制完成"},
                }
            raise MiniAppBeastError("fixture_failure")

        transport = MiniAppDwellingTransport(
            object(), ENTRY, logger=logger, post_json=post_json
        )

        async def run():
            await transport.initialize()
            logger.info_messages.clear()
            await transport.command(".推命 炼制", log_operation=False)
            await transport.forge_treasure(
                "主魂", "treasure_001", 1, log_operation=False
            )
            async def failing_post_json(origin, path, payload, timeout):
                raise MiniAppBeastError("fixture_failure")

            transport.post_json = failing_post_json
            with self.assertRaises(MiniAppBeastError):
                await transport.command(".推命 炼制", log_operation=False)

        with patch(
            "miniapp_dwelling.request_webview_init_data",
            new=AsyncMock(return_value="signed"),
        ):
            asyncio.run(run())

        self.assertEqual(logger.info_messages, [])
        self.assertIn(
            "Mini App [主魂] 指令 .推命 炼制失败：fixture_failure",
            logger.error_messages,
        )

    def test_snapshot_updates_legacy_meditation_state(self):
        actor = SimpleNamespace(state={}, get_avatar_state=lambda name: actor.state.setdefault("avatar", {}))
        payload = {
            "snapshot": {"level": "overview"},
            "account": {
                "playerId": -200,
                "daoName": "素心子",
                "cultivationLevel": "元婴中期",
                "profile": {
                    "sectName": "星宫",
                    "spiritRoot": {"name": "天灵根(水)"},
                    "cultivation": {"current": 123456, "next": 1000000},
                },
            },
            "dwelling": {
                "meditation": {
                    "deepSeclusion": {"active": True, "endMs": 1785182546931},
                    "standardCultivation": {"canCultivate": False},
                }
            },
        }
        self.assertTrue(apply_dwelling_snapshot(actor, "素心子", payload))
        state = actor.state["avatar"]
        self.assertTrue(state["in_deep_meditation"])
        self.assertEqual(state["miniapp_player_id"], -200)
        self.assertEqual(state["miniapp_sect_name"], "星宫")
        self.assertEqual(state["sect_name"], "星宫")
        self.assertEqual(state["miniapp_spirit_root"], "天灵根(水)")
        self.assertEqual(state["spirit_root"], "天灵根(水)")
        self.assertEqual(state["miniapp_cultivation_level"], "元婴中期")
        self.assertEqual(state["level"], "元婴中期")
        self.assertEqual(state["cultivation_level"], "元婴中期")
        self.assertEqual(state["miniapp_current_exp"], 123456)
        self.assertEqual(state["miniapp_total_exp"], 1000000)
        self.assertEqual(state["current_exp"], 123456)
        self.assertEqual(state["total_exp"], 1000000)
        self.assertEqual(state["miniapp_profile_source"], "dwelling_overview")
        self.assertEqual(actor.identity_sect_names["素心子"], "星宫")
        self.assertEqual(actor.state["identity_sect_names"]["素心子"], "星宫")

    def test_snapshot_refreshes_avatar_dao_name_by_stable_player_id(self):
        class Actor(CommonCommandMixin):
            def __init__(self):
                self.avatars = ["缘生子"]
                self._avatar_chat_ids = {"-1003885521329": "缘生子"}
                self.avatar_identities = {"-1003885521329": "缘生子"}
                self.avatar_usernames = {"lvdoumiao": "缘生子"}
                self.identity_sect_names = {"缘生子": "阴罗宗"}
                self._current_identity = "缘生子"
                self._persisted_identity = "缘生子"
                self._manual_identity_label = "缘生子"
                self.command_avatar_map = {101: "缘生子"}
                self.state = {
                    "current_identity": "缘生子",
                    "identity_sect_names": {"缘生子": "阴罗宗"},
                    "identity_pauses": {
                        "缘生子": {
                            "until": "",
                            "reason": "肉体破碎/元婴虚弱，等待重生",
                            "wait_for_rebirth": True,
                        }
                    },
                    "avatars": {
                        "缘生子": {
                            "marker": "keep",
                            "miniapp_player_id": -1003885521329,
                        }
                    },
                }
                self.saved = 0

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                self.saved += 1

        actor = Actor()
        payload = {
            "snapshot": {"level": "overview"},
            "account": {
                "playerId": -1003885521329,
                "daoName": "玄续玄",
                "profile": {"sectName": "阴罗宗"},
            },
        }

        self.assertTrue(apply_dwelling_snapshot(actor, "缘生子", payload))
        self.assertEqual(actor.avatars, ["玄续玄"])
        self.assertEqual(actor._avatar_chat_ids["-1003885521329"], "玄续玄")
        self.assertEqual(actor.avatar_identities["-1003885521329"], "玄续玄")
        self.assertEqual(actor.avatar_usernames["lvdoumiao"], "玄续玄")
        self.assertEqual(actor._current_identity, "玄续玄")
        self.assertEqual(actor.state["current_identity"], "玄续玄")
        self.assertEqual(actor.command_avatar_map[101], "玄续玄")
        self.assertEqual(actor.state["avatars"]["玄续玄"]["marker"], "keep")
        self.assertEqual(actor.state["avatars"]["玄续玄"]["miniapp_dao_name"], "玄续玄")
        self.assertEqual(actor.identity_sect_names["玄续玄"], "阴罗宗")
        self.assertEqual(actor.state["avatar_dao_names_by_player_id"]["-1003885521329"], "玄续玄")
        self.assertIn("玄续玄", actor.state["identity_pauses"])
        self.assertNotIn("缘生子", actor.state["identity_pauses"])
        self.assertEqual(actor.resolve_avatar_identity("缘生子"), "玄续玄")
        self.assertEqual(actor.state["avatar_dao_name_history"][-1]["old_name"], "缘生子")
        self.assertGreater(actor.saved, 0)

    def test_snapshot_ignores_dead_avatar_placeholder_dao_name(self):
        class Actor(CommonCommandMixin):
            def __init__(self):
                self.avatars = ["玄续玄"]
                self._avatar_chat_ids = {"-1003885521329": "玄续玄"}
                self.avatar_identities = {"-1003885521329": "玄续玄"}
                self.state = {
                    "avatars": {"玄续玄": {"marker": "keep"}},
                    "avatar_dao_name_aliases": {"玄续玄": "玄续玄"},
                }
                self.saved = 0

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                self.saved += 1

        actor = Actor()
        payload = {
            "account": {
                "playerId": -1003885521329,
                "daoName": "一缕残魂",
                "profile": {"sectName": "阴罗宗"},
            }
        }

        self.assertTrue(apply_dwelling_snapshot(actor, "玄续玄", payload))
        self.assertEqual(actor.avatars, ["玄续玄"])
        self.assertNotIn("一缕残魂", actor.state.get("avatar_dao_names_by_player_id", {}).values())
        self.assertEqual(actor.state["avatars"]["玄续玄"]["marker"], "keep")

    def test_rebirth_announcement_immediately_refreshes_avatar_dao_name(self):
        player_id = -1003885521329

        class Actor(CommonCommandMixin):
            def __init__(self):
                self.account_key = ""
                self.avatars = ["玄续玄"]
                self._avatar_chat_ids = {str(player_id): "玄续玄"}
                self.avatar_identities = {str(player_id): "玄续玄"}
                self.avatar_usernames = {"lvdoumiao": "玄续玄"}
                self.identity_sect_names = {"玄续玄": "阴罗宗"}
                self._miniapp_command_router = SimpleNamespace(
                    transport=SimpleNamespace(identity_player_ids={"玄续玄": player_id})
                )
                self.state = {
                    "avatar_dao_names_by_player_id": {str(player_id): "玄续玄"},
                    "avatar_dao_name_aliases": {"缘生子": "玄续玄"},
                    "identity_pauses": {
                        "玄续玄": {
                            "until": "",
                            "reason": "肉体破碎/元婴虚弱，等待重生",
                            "wait_for_rebirth": True,
                        },
                        "一缕残魂": {
                            "until": "2099-01-01 00:00:00",
                            "reason": "元婴虚弱/待夺舍重生",
                        },
                        str(player_id): {
                            "until": "2099-01-01 00:00:00",
                            "reason": "元婴虚弱/待夺舍重生",
                        },
                    },
                    "avatars": {
                        "玄续玄": {
                            "miniapp_player_id": player_id,
                            "next_field_training_time": "2099-01-01 00:00:00",
                        }
                    },
                }
                self.saved = 0

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def set_avatar_state(self, identity, key, value):
                self.state["avatars"][identity][key] = value

            def save_state(self):
                self.saved += 1

        actor = Actor()
        text = (
            "先前肉身陨落的 @Lvdoumiao (原道号：玄续玄)，其元婴已成功夺舍重生！\n"
            "从今日起，他将以【锋脉子】为名，身负【伪灵根(木火土金)】的全新肉身。"
        )

        self.assertTrue(actor.record_identity_yuanying_recovery_from_text(
            "玄续玄", text, source="mention"
        ))
        self.assertEqual(actor.avatars, ["锋脉子"])
        self.assertEqual(actor._avatar_chat_ids[str(player_id)], "锋脉子")
        self.assertEqual(actor.avatar_identities[str(player_id)], "锋脉子")
        self.assertEqual(actor.avatar_usernames["lvdoumiao"], "锋脉子")
        self.assertEqual(actor.identity_sect_names["锋脉子"], "阴罗宗")
        self.assertEqual(actor.state["avatar_dao_names_by_player_id"][str(player_id)], "锋脉子")
        self.assertEqual(actor.resolve_avatar_identity("玄续玄"), "锋脉子")
        self.assertEqual(actor.resolve_avatar_identity("缘生子"), "锋脉子")
        self.assertIn("锋脉子", actor.state["avatars"])
        self.assertNotIn("玄续玄", actor.state["avatars"])
        self.assertNotIn("玄续玄", actor.state["identity_pauses"])
        self.assertNotIn("一缕残魂", actor.state["identity_pauses"])
        self.assertNotIn(str(player_id), actor.state["identity_pauses"])
        self.assertEqual(
            actor._miniapp_command_router.transport.identity_player_ids["锋脉子"],
            player_id,
        )
        self.assertGreater(actor.saved, 0)

    def test_restore_migrates_persisted_dead_avatar_placeholder(self):
        class Actor(CommonCommandMixin):
            def __init__(self):
                self.avatars = ["玄续玄"]
                self._avatar_chat_ids = {"-1003885521329": "玄续玄"}
                self.avatar_identities = {"-1003885521329": "玄续玄"}
                self.state = {
                    "current_identity": "一缕残魂",
                    "avatar_dao_names_by_player_id": {"-1003885521329": "一缕残魂"},
                    "avatar_dao_name_aliases": {
                        "缘生子": "一缕残魂",
                        "玄续玄": "一缕残魂",
                    },
                    "identity_pauses": {
                        "一缕残魂": {
                            "until": "",
                            "reason": "肉体破碎/元婴虚弱，等待重生",
                            "wait_for_rebirth": True,
                        },
                        "-1003885521329": {
                            "until": "2020-01-01 00:00:00",
                            "reason": "元婴虚弱/待夺舍重生",
                        },
                    },
                    "avatars": {"一缕残魂": {"marker": "keep"}},
                }
                self.saved = 0

            def save_state(self):
                self.saved += 1

        actor = Actor()
        self.assertEqual(actor.restore_avatar_dao_names(), 1)
        self.assertEqual(actor.state["current_identity"], "玄续玄")
        self.assertEqual(actor.state["avatars"]["玄续玄"]["marker"], "keep")
        self.assertIn("玄续玄", actor.state["identity_pauses"])
        self.assertNotIn("一缕残魂", actor.state["identity_pauses"])
        self.assertNotIn("-1003885521329", actor.state["identity_pauses"])
        self.assertEqual(actor.state["avatar_dao_names_by_player_id"]["-1003885521329"], "玄续玄")
        self.assertEqual(actor.resolve_avatar_identity("缘生子"), "玄续玄")

    def test_router_routes_stale_avatar_name_to_current_dao_name(self):
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={"avatars": {"玄续玄": {}}},
            avatars=["玄续玄"],
            resolve_avatar_identity=lambda identity: "玄续玄" if identity == "缘生子" else identity,
            save_state=lambda: None,
            identity_pause_seconds=lambda identity: 0,
        )
        router = MiniAppCommandRouter(actor, "sub", logger=FakeLogger())
        router._route_active = True
        router.transport.identity_player_ids = {"主魂": 100, "玄续玄": -200}
        router.transport.command = AsyncMock(
            return_value=MiniAppCommandResponse("完成", {"actionResult": {"ok": True}})
        )
        router._maybe_refresh_auth = AsyncMock()

        response = asyncio.run(router._route("缘生子", ".我的阴罗幡", AsyncMock(), (), {}))

        self.assertEqual(response, "完成")
        router.transport.command.assert_awaited_once_with(".我的阴罗幡", identity="玄续玄")

    def test_router_group_fallback_resolves_stale_avatar_name(self):
        send_identity = AsyncMock(return_value="切换成功")
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={},
            avatars=["玄续玄"],
            resolve_avatar_identity=lambda identity: "玄续玄" if identity == "缘生子" else identity,
            send_and_wait_feedback_identity=send_identity,
        )
        router = MiniAppCommandRouter(actor, "sub", logger=FakeLogger())
        router._orig_send_identity = send_identity

        response = asyncio.run(router._send_identity("缘生子", ".切换 缘生子"))

        self.assertEqual(response, "切换成功")
        send_identity.assert_awaited_once_with("玄续玄", ".切换 缘生子")

    def test_main_snapshot_updates_account_sect_and_parses_cultivation_text(self):
        actor = SimpleNamespace(
            state={"sect_name": "旧宗门", "identity_sect_names": {"主魂": "旧宗门"}},
            sect_name="旧宗门",
            identity_sect_names={"主魂": "旧宗门"},
        )
        payload = {
            "account": {
                "playerId": 100,
                "daoName": "主号道名",
                "sectName": "万灵宗",
                "cultivationLevel": "化神初期",
                "profile": {
                    "spiritRoot": "异灵根(雷)",
                    "cultivation": {"text": "802,396 / 4,000,000"},
                },
            }
        }

        self.assertTrue(apply_dwelling_snapshot(actor, "主魂", payload))
        self.assertEqual(actor.sect_name, "万灵宗")
        self.assertEqual(actor.identity_sect_names["主魂"], "万灵宗")
        self.assertEqual(actor.state["sect_name"], "万灵宗")
        self.assertEqual(actor.state["current_exp"], 802396)
        self.assertEqual(actor.state["total_exp"], 4000000)
        self.assertEqual(actor.state["spirit_root"], "异灵根(雷)")
        self.assertEqual(actor.state["level"], "化神初期")

    def test_transient_sect_placeholder_does_not_overwrite_confirmed_mapping(self):
        class Actor:
            def __init__(self):
                self.state = {
                    "identity_sect_names": {"寻真子": "落云宗"},
                    "avatars": {
                        "寻真子": {
                            "miniapp_sect_name": "落云宗",
                            "sect_name": "落云宗",
                        }
                    },
                }
                self.identity_sect_names = {"寻真子": "落云宗"}

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

        actor = Actor()
        payload = {
            "snapshot": {"level": "overview"},
            "account": {
                "playerId": -203,
                "daoName": "寻真子",
                "cultivationLevel": "元婴初期",
                "profile": {"sectName": "读取中"},
            },
        }

        self.assertTrue(apply_dwelling_snapshot(actor, "寻真子", payload))
        self.assertEqual(actor.identity_sect_names["寻真子"], "落云宗")
        self.assertEqual(actor.state["identity_sect_names"]["寻真子"], "落云宗")
        self.assertEqual(actor.state["avatars"]["寻真子"]["miniapp_sect_name"], "落云宗")
        self.assertEqual(actor.state["avatars"]["寻真子"]["sect_name"], "落云宗")

    def test_hybrid_router_syncs_home_profiles_for_every_identity(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {"avatars": {"素心子": {}}}
                self.avatars = ["素心子"]
                self.identity_sect_names = {}
                self.saved = 0

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                self.saved += 1

        actor = Actor()
        router = MiniAppCommandRouter(actor, "main")

        async def overview(identity):
            return {
                "snapshot": {"level": "overview"},
                "account": {
                    "playerId": 100 if identity == "主魂" else -200,
                    "cultivationLevel": "化神初期" if identity == "主魂" else "元婴中期",
                    "profile": {
                        "sectName": "万灵宗" if identity == "主魂" else "星宫",
                        "spiritRoot": {"name": "天灵根(火)"},
                        "cultivation": {"current": 10, "next": 100},
                    },
                },
            }

        router.transport.overview = AsyncMock(side_effect=overview)
        count = asyncio.run(router.sync_all_profiles(["主魂", "素心子"]))

        self.assertEqual(count, 2)
        self.assertEqual(actor.state["miniapp_profile_identity_count"], 2)
        self.assertEqual(actor.state["miniapp_sect_name"], "万灵宗")
        self.assertEqual(actor.state["avatars"]["素心子"]["miniapp_sect_name"], "星宫")
        self.assertGreater(actor.saved, 0)

    def test_router_discovers_main_and_sub_star_palace_identities(self):
        main_actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={},
            avatars=["无咎子", "缘生子", "素缘子"],
            identity_sect_names={
                "主魂": "万灵宗",
                "无咎子": "天星宗",
                "缘生子": "阴罗宗",
                "素缘子": "星宫",
            },
        )
        main_router = MiniAppCommandRouter(main_actor, "main")
        main_router.transport.identity_player_ids = {
            "主魂": 100,
            "无咎子": -101,
            "缘生子": -102,
            "素缘子": -103,
        }

        sub_actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={},
            avatars=["厚土", "玄续玄", "寻真子"],
            identity_sect_names={
                "主魂": "元婴宗",
                "厚土": "星宫",
                "玄续玄": "阴罗宗",
                "寻真子": "落云宗",
            },
        )
        sub_router = MiniAppCommandRouter(sub_actor, "sub")
        sub_router.transport.identity_player_ids = {
            "主魂": 200,
            "厚土": -201,
            "玄续玄": -202,
            "寻真子": -203,
        }

        self.assertEqual(main_router.star_farm_identities(), ["素缘子"])
        self.assertEqual(sub_router.star_farm_identities(), ["厚土"])

    def test_router_uses_synced_miniapp_sect_instead_of_stale_mapping(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {"avatars": {"厚土": {}, "寻真子": {}}}
                self.avatars = ["厚土", "寻真子"]
                self.identity_sect_names = {
                    "主魂": "元婴宗",
                    "厚土": "星宫",
                    "寻真子": "星宫",
                }

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                pass

        actor = Actor()
        router = MiniAppCommandRouter(actor, "sub")
        router.transport.identity_player_ids = {"主魂": 100, "厚土": -201, "寻真子": -203}

        async def overview(identity):
            sect = {"主魂": "元婴宗", "厚土": "星宫", "寻真子": "落云宗"}[identity]
            return {
                "snapshot": {"level": "overview"},
                "account": {
                    "playerId": router.transport.identity_player_ids[identity],
                    "profile": {"sectName": sect},
                },
            }

        router.transport.overview = AsyncMock(side_effect=overview)
        asyncio.run(router.sync_all_profiles(["主魂", "厚土", "寻真子"]))

        self.assertEqual(actor.identity_sect_names["寻真子"], "落云宗")
        self.assertEqual(router.star_farm_identities(), ["厚土"])

    def test_router_star_farm_cycle_soothes_collects_and_pulls_via_miniapp(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {"miniapp_beast": {"entry_url": ENTRY}}
                self.state = {"avatars": {"素缘子": {}}}
                self.avatars = ["素缘子"]
                self.identity_sect_names = {"主魂": "万灵宗", "素缘子": "星宫"}
                self.rewards = []

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def record_daily_reward_event(self, identity, command, text, source=""):
                self.rewards.append((identity, command, text, source))

            def save_state(self):
                pass

        actor = Actor()
        logger = FakeLogger()
        router = MiniAppCommandRouter(actor, "main", logger=logger)
        router.transport.identity_player_ids = {"主魂": 100, "素缘子": -200}
        router.transport.sect_farm_snapshot = AsyncMock(return_value={
            "domain": {
                "mode": "stars",
                "plots": [
                    {"key": "1", "status": "元磁紊乱"},
                    {"key": "2", "status": "元磁紊乱"},
                    {"key": "3", "empty": True},
                ],
            }
        })

        async def action(identity, action, plot_key="", star_name="", log_operation=True):
            if action == "soothe":
                plots = [
                    {"key": "1", "status": "可收集"},
                    {"key": "2", "status": "可收集"},
                    {"key": "3", "empty": True},
                ]
                message = "安抚完成"
            elif action == "collect":
                plots = [
                    {"key": "1", "empty": True},
                    {"key": "2", "empty": True},
                    {"key": "3", "empty": True},
                ]
                message = "成功收集星辰精华"
            else:
                plots = [
                    {"key": "2", "name": "天雷星", "remainingSeconds": 3598},
                    {"key": "3", "name": "天雷星", "remainingSeconds": 3600},
                ]
                cost = 100 if plot_key == "2" else 200
                message = f"已在星位 {plot_key} 牵引{star_name}，消耗修为 {cost}"
            return {
                "ok": True,
                "actionResult": {"ok": True, "rawMessage": message},
                "domain": {"mode": "stars", "plots": plots},
            }

        router.transport.sect_farm_action = AsyncMock(side_effect=action)
        with patch("miniapp_command_routing.asyncio.sleep", new=AsyncMock()):
            wait = asyncio.run(router.run_star_farm_cycle("素缘子"))

        self.assertEqual(wait, 3605)
        self.assertEqual(
            [call.args[1:] for call in router.transport.sect_farm_action.await_args_list],
            [
                ("soothe",),
                ("collect",),
                ("pull",),
                ("pull",),
                ("pull",),
            ],
        )
        pull_calls = router.transport.sect_farm_action.await_args_list[-3:]
        self.assertEqual([call.kwargs["plot_key"] for call in pull_calls], ["1", "2", "3"])
        self.assertTrue(all(call.kwargs["star_name"] == "天雷星" for call in pull_calls))
        self.assertTrue(all(call.kwargs["log_operation"] is False for call in pull_calls))
        self.assertEqual(actor.rewards[0][0:2], ("素缘子", ".收集精华"))
        combined = "\n".join(logger.info_messages)
        self.assertEqual(combined.count("OUT [Mini App | 素缘子]:\n宗门灵圃牵引星辰"), 1)
        self.assertEqual(combined.count("IN [Mini App | 素缘子]:\n宗门灵圃牵引星辰"), 1)
        self.assertIn("宗门灵圃牵引星辰（3个星位）", combined)
        self.assertIn("星位 1、2、3 已牵引天雷星，共 3 个引星盘", combined)
        self.assertIn("消耗修为 500", combined)
        self.assertIn("宗门灵圃收集精华（批量2个星位）", combined)
        self.assertIn("请求一次性收集 2 个引星盘", combined)
        self.assertIn("确认本次清空 2 个", combined)
        self.assertIn("收集后空盘 3 个", combined)

    def test_router_star_farm_waits_when_some_tianlei_plots_are_still_maturing(self):
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={"avatars": {"素缘子": {}}},
            avatars=["素缘子"],
            identity_sect_names={"素缘子": "星宫"},
            save_state=lambda: None,
        )
        actor.get_avatar_state = lambda identity: actor.state["avatars"][identity]
        router = MiniAppCommandRouter(actor, "main")
        router.transport.sect_farm_snapshot = AsyncMock(return_value={
            "domain": {
                "mode": "stars",
                "plots": [
                    {"key": "1", "name": "天雷星", "status": "可收集"},
                    {"key": "2", "name": "天雷星", "status": "可收集"},
                    {"key": "3", "name": "天雷星", "status": "可收集"},
                    {"key": "4", "name": "天雷星", "status": "凝聚中", "remainingSeconds": 12},
                    {"key": "5", "name": "天雷星", "status": "凝聚中", "remainingSeconds": 18},
                    {"key": "6", "empty": True},
                    {"key": "7", "empty": True},
                    {"key": "8", "empty": True},
                ],
            }
        })
        router.transport.sect_farm_action = AsyncMock()

        wait = asyncio.run(router.run_star_farm_cycle("素缘子"))

        self.assertEqual(wait, 23)
        router.transport.sect_farm_action.assert_not_awaited()

    def test_router_star_farm_skips_collect_when_soothed_stars_are_still_maturing(self):
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={"avatars": {"素缘子": {}}},
            avatars=["素缘子"],
            identity_sect_names={"素缘子": "星宫"},
            save_state=lambda: None,
        )
        actor.get_avatar_state = lambda identity: actor.state["avatars"][identity]
        router = MiniAppCommandRouter(actor, "main")
        router.transport.sect_farm_snapshot = AsyncMock(return_value={
            "domain": {
                "mode": "stars",
                "plots": [{"key": "1", "status": "元磁紊乱"}],
            }
        })
        router.transport.sect_farm_action = AsyncMock(return_value={
            "ok": True,
            "actionResult": {"ok": True, "message": "安抚完成"},
            "domain": {
                "mode": "stars",
                "plots": [{"key": "1", "status": "正常", "remainingSeconds": 3600}],
            },
        })

        wait = asyncio.run(router.run_star_farm_cycle("素缘子"))

        self.assertEqual(wait, 3605)
        router.transport.sect_farm_action.assert_awaited_once_with(
            "素缘子",
            "soothe",
            plot_key="",
            star_name="",
        )

    def test_command_center_placeholder_does_not_clear_deep_meditation(self):
        actor = SimpleNamespace(
            state={
                "in_deep_meditation": True,
                "deep_meditation_end_time": "2026-07-28 08:00:00",
                "meditation_restart_pending": False,
                "miniapp_player_id": 100,
                "miniapp_dao_name": "主号道名",
                "miniapp_sect_name": "天星宗",
                "miniapp_spirit_root": "异灵根(风)",
                "miniapp_cultivation_level": "元婴后期",
                "miniapp_current_exp": 832925,
                "miniapp_total_exp": 2000000,
            }
        )
        payload = {
            "account": {},
            "dwelling": {},
            "actionResult": {"ok": True, "rawMessage": "侍妾状态正常"},
        }

        self.assertTrue(apply_dwelling_snapshot(actor, "主魂", payload))
        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], "2026-07-28 08:00:00")
        self.assertFalse(actor.state["meditation_restart_pending"])
        self.assertEqual(actor.state["miniapp_player_id"], 100)
        self.assertEqual(actor.state["miniapp_dao_name"], "主号道名")
        self.assertEqual(actor.state["miniapp_sect_name"], "天星宗")
        self.assertEqual(actor.state["miniapp_spirit_root"], "异灵根(风)")
        self.assertEqual(actor.state["miniapp_cultivation_level"], "元婴后期")
        self.assertEqual(actor.state["miniapp_current_exp"], 832925)
        self.assertEqual(actor.state["miniapp_total_exp"], 2000000)

    def test_restricted_worker_blocks_non_whitelisted_command_before_transport(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {
                    "miniapp_beast": {"entry_url": ENTRY},
                    "restricted_miniapp": {},
                }
                self.state = {}
                self.pause_event = asyncio.Event()
                self.pause_event.set()

            def save_state(self):
                pass

        actor = Actor()
        worker = RestrictedMiniAppWorker(actor, "xiaohao")
        worker.transport.command = AsyncMock()
        response = asyncio.run(worker._send("主魂", ".切换 主魂"))
        self.assertIsNone(response)
        worker.transport.command.assert_not_awaited()
        self.assertEqual(actor.state["restricted_miniapp_last_blocked_command"], ".切换 主魂")

    def test_restricted_worker_starts_shared_fishing_loop(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {
                    "miniapp_beast": {"entry_url": ENTRY},
                    "restricted_miniapp": {},
                }
                self.state = {}
                self.avatars = []
                self.is_running = True
                self.pause_event = asyncio.Event()
                self.pause_event.set()
                self.startup_done = asyncio.Event()

            def save_state(self):
                pass

            async def run_custom_command_loop(self):
                pass

            async def run_meditation_timer(self):
                pass

            async def run_yuanying_out_loop(self):
                pass

        async def exercise():
            actor = Actor()
            worker = RestrictedMiniAppWorker(actor, "xiaohao")
            worker.transport.initialize = AsyncMock()
            worker.transport.identity_player_ids = {"主魂": 100}
            worker.sync_all_details = AsyncMock()
            worker.daily_activities.pagoda_enabled = False
            worker.daily_activities.hunt_enabled = False
            worker.tianxing_journey.enabled = False
            worker.beast_abyss.enabled = False
            worker.beast_seek.enabled = False
            worker.beast_enabled = False
            worker.beast_contract.enabled = False
            spawned = []

            def capture(name, coroutine):
                spawned.append(name)
                coroutine.close()

            worker._spawn = capture
            await worker.start()
            return worker, spawned

        worker, spawned = asyncio.run(exercise())

        self.assertTrue(worker.fishing.supported)
        self.assertIn("fishing", spawned)

    def test_restricted_worker_routes_puzzle_through_miniapp(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {
                    "miniapp_beast": {"entry_url": ENTRY},
                    "restricted_miniapp": {},
                }
                self.state = {"avatars": {"缘生子": {}}}
                self.pause_event = asyncio.Event()
                self.pause_event.set()

            def save_state(self):
                pass

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def identity_pause_seconds(self, identity):
                return 0

            def dashboard_command_paused(self, command, identity):
                return False

        actor = Actor()
        worker = RestrictedMiniAppWorker(actor, "xiaohao")
        response = MiniAppCommandResponse("拼图成功", {"actionResult": {"ok": True}})
        worker.transport.command = AsyncMock(return_value=response)

        result = asyncio.run(worker._send("缘生子", ".拼图", return_response_msg=True))

        self.assertIs(result, response)
        worker.transport.command.assert_awaited_once_with(
            ".拼图",
            identity="缘生子",
            meditation_prefix=False,
        )
        self.assertNotIn("restricted_miniapp_last_blocked_command", actor.state)

    def test_restricted_worker_routes_concubine_place_through_command_center(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {
                    "miniapp_beast": {"entry_url": ENTRY},
                    "restricted_miniapp": {},
                }
                self.state = {}
                self.pause_event = asyncio.Event()
                self.pause_event.set()

            def save_state(self):
                pass

            def identity_pause_seconds(self, identity):
                return 0

            def dashboard_command_paused(self, command, identity):
                return False

        actor = Actor()
        worker = RestrictedMiniAppWorker(actor, "waaiging")
        response = MiniAppCommandResponse(
            "你已将侍妾安置在藏娇阁中。",
            {"ok": True, "actionResult": {"ok": True}},
        )
        worker.transport.command = AsyncMock(return_value=response)

        result = asyncio.run(
            worker._send("主魂", ".安置侍妾", return_response_msg=True)
        )

        self.assertIs(result, response)
        worker.transport.command.assert_awaited_once_with(
            ".安置侍妾",
            identity="主魂",
            meditation_prefix=True,
        )

    def test_restricted_worker_recovers_previously_blocked_puzzle(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {
                    "miniapp_beast": {"entry_url": ENTRY},
                    "restricted_miniapp": {},
                }
                self.state = {
                    "avatars": {"缘生子": {}},
                    "restricted_miniapp_last_blocked_command": ".拼图",
                    "restricted_miniapp_last_blocked_identity": "缘生子",
                    "restricted_miniapp_last_blocked_at": "2026-07-29 08:25:14",
                }
                self.avatars = ["缘生子"]
                self.pause_event = asyncio.Event()
                self.pause_event.set()

            def save_state(self):
                pass

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def identity_pause_seconds(self, identity):
                return 0

            def dashboard_command_paused(self, command, identity):
                return False

        actor = Actor()
        worker = RestrictedMiniAppWorker(actor, "xiaohao")
        response = MiniAppCommandResponse("拼图成功", {"actionResult": {"ok": True}})
        worker.transport.command = AsyncMock(return_value=response)

        recovered = asyncio.run(worker.recover_last_blocked_command())

        self.assertTrue(recovered)
        worker.transport.command.assert_awaited_once_with(
            ".拼图",
            identity="缘生子",
            meditation_prefix=False,
        )
        self.assertNotIn("restricted_miniapp_last_blocked_command", actor.state)
        self.assertNotIn("restricted_miniapp_last_blocked_identity", actor.state)
        self.assertNotIn("restricted_miniapp_last_blocked_at", actor.state)

    def test_star_farm_ignores_retired_chat_command_controls(self):
        class Actor:
            def __init__(self):
                self.client = object()
                self.config = {
                    "miniapp_beast": {"entry_url": ENTRY},
                    "restricted_miniapp": {},
                }
                self.state = {"avatars": {"素心子": {}}}

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def dashboard_command_paused(self, command, identity):
                return True

            def save_state(self):
                pass

        actor = Actor()
        worker = RestrictedMiniAppWorker(actor, "xiaohao")
        worker.transport.sect_farm_action = AsyncMock(return_value={
            "ok": True,
            "actionResult": {"ok": True, "message": "安抚完成"},
            "domain": {"mode": "stars", "plots": []},
        })

        payload, status = asyncio.run(worker._star_action("soothe"))

        self.assertTrue(payload["ok"])
        self.assertEqual(status, (0, 0, [], 0))
        worker.transport.sect_farm_action.assert_awaited_once_with(
            "素心子",
            "soothe",
            plot_key="",
            star_name="",
        )
        self.assertEqual(actor.state["avatars"]["素心子"]["star_miniapp_last_action"], "soothe")

    def test_restricted_star_farm_continues_after_nothing_to_soothe(self):
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}, "restricted_miniapp": {}},
            state={"avatars": {"素心子": {}}},
            save_state=lambda: None,
        )
        actor.get_avatar_state = lambda identity: actor.state["avatars"][identity]
        worker = RestrictedMiniAppWorker(actor, "xiaohao")
        worker.transport.sect_farm_action = AsyncMock(return_value={
            "ok": False,
            "actionResult": {"ok": False, "error": "nothing_to_soothe"},
            "domain": {
                "mode": "stars",
                "plots": [{"key": "1", "status": "可收集"}],
            },
        })

        payload, status = asyncio.run(worker._star_action("soothe"))

        self.assertFalse(payload["ok"])
        self.assertEqual(status[0], 1)
        self.assertEqual(actor.state["avatars"]["素心子"]["star_miniapp_last_error"], "")

    def test_restricted_star_collect_records_time_and_reward(self):
        rewards = []
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}, "restricted_miniapp": {}},
            state={"avatars": {"素心子": {}}},
            save_state=lambda: None,
            record_daily_reward_event=lambda identity, command, text, source="": rewards.append(
                (identity, command, text, source)
            ),
        )
        actor.get_avatar_state = lambda identity: actor.state["avatars"][identity]
        worker = RestrictedMiniAppWorker(actor, "xiaohao")
        worker.transport.sect_farm_action = AsyncMock(return_value={
            "ok": True,
            "actionResult": {"ok": True, "message": "收集完成：天雷竹 x8"},
            "domain": {
                "mode": "stars",
                "plots": [{"key": str(index), "empty": True} for index in range(1, 9)],
            },
        })

        asyncio.run(worker._star_action("collect", log_operation=False))

        state = actor.state["avatars"]["素心子"]
        self.assertTrue(state["last_collection_time"])
        self.assertTrue(state["last_star_collect_time"])
        self.assertEqual(rewards[0][0:2], ("素心子", ".收集精华"))
        self.assertEqual(rewards[0][3], "Mini App 收集精华")

    def test_star_palace_divine_bypasses_cooling_circuit_before_manifestation(self):
        actor = SimpleNamespace(
            client=object(),
            config={"miniapp_beast": {"entry_url": ENTRY}},
            state={},
            avatars=["素缘子"],
            identity_sect_names={"素缘子": "星宫"},
            is_running=False,
            save_state=lambda: None,
        )
        router = MiniAppCommandRouter(actor, "main", start_background_tasks=False)
        router.transport.identity_player_ids = {"素缘子": -103}
        router.transport.star_palace_action = AsyncMock(return_value={
            "ok": True,
            "actionResult": {
                "message": "观星成功",
                "divination": {
                    "active": True,
                    "remainingSeconds": 25,
                },
            },
        })
        router.transport.shift_destiny_action = AsyncMock()
        manifest_dt = datetime.now() + timedelta(seconds=35)

        asyncio.run(router.run_star_palace_cycle("素缘子", manifest_dt, "@Weeguu"))

        kwargs = router.transport.star_palace_action.await_args.kwargs
        self.assertTrue(kwargs["time_critical"])
        router.transport.star_palace_action.assert_any_await(
            "素缘子",
            "divine",
            time_critical=True,
        )

    def test_unknown_identity_is_rejected(self):
        transport = MiniAppDwellingTransport(object(), ENTRY)
        transport.identity_player_ids = {"主魂": 100}
        with self.assertRaises(MiniAppBeastError):
            transport.player_id("不存在")


if __name__ == "__main__":
    unittest.main()
