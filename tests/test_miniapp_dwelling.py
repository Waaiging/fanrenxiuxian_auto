import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import (
    MiniAppCommandResponse,
    MiniAppDwellingTransport,
    apply_dwelling_snapshot,
    miniapp_command_allowed,
    sect_farm_action_result_ok,
    sect_farm_snapshot_status,
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

    def test_command_whitelist_rejects_group_only_actions(self):
        for command in (
            ".闭关修炼",
            ".元婴出窍",
            ".我的侍妾",
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
            ".一键收取精华",
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
            ".问道",
            ".小世界",
            ".显灵",
            ".安抚信徒",
            ".神迹 布道",
            ".我的阴罗幡",
            ".每日献祭",
            ".召回魔影",
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
            status = asyncio.run(transport.command(".查看闭关", identity="主魂"))
            manifest = asyncio.run(transport.command(".显灵", identity="主魂"))
            soothe = asyncio.run(transport.command(".安抚信徒", identity="主魂"))
            collected = asyncio.run(transport.small_world_action("主魂", "collect"))

        self.assertEqual(transport.player_id("主魂"), 100)
        self.assertEqual(transport.player_id("素心子"), -200)
        self.assertEqual(response.text, "reply:.元婴出窍")
        self.assertEqual(puzzle.text, "reply:.拼图")
        self.assertEqual(status.text, "reply:status")
        self.assertEqual(manifest.text, "reply:manifest")
        self.assertEqual(soothe.text, "reply:soothe")
        self.assertEqual(collected["actionResult"]["rawMessage"], "reply:collect")
        self.assertEqual(calls[1][0], "/api/miniapp/xianxia-dwelling/command-center")
        self.assertEqual(calls[1][1]["playerId"], -200)
        self.assertEqual(calls[2][0], "/api/miniapp/xianxia-dwelling/command-center")
        self.assertEqual(calls[2][1]["command"], ".拼图")
        self.assertEqual(calls[3][0], "/api/miniapp/xianxia-dwelling/deep-seclusion")
        self.assertEqual(calls[4][0], "/api/miniapp/xianxia-dwelling/small-world")
        self.assertEqual(calls[4][1]["action"], "manifest")
        self.assertEqual(calls[5][0], "/api/miniapp/xianxia-dwelling/small-world")
        self.assertEqual(calls[5][1]["action"], "soothe")
        self.assertEqual(calls[6][0], "/api/miniapp/xianxia-dwelling/small-world")
        self.assertEqual(calls[6][1]["action"], "collect")

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
                    "replay": {"clearedCount": 20, "endFloor": 20, "failedFloor": 21},
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
        self.assertNotIn("洞府寻宝入府", combined)
        self.assertNotIn("洞府寻宝探查", combined)
        self.assertNotIn("洞府寻宝见好就收", combined)

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
            avatars=["厚土", "缘生子", "寻真子"],
            identity_sect_names={
                "主魂": "元婴宗",
                "厚土": "星宫",
                "缘生子": "阴罗宗",
                "寻真子": "落云宗",
            },
        )
        sub_router = MiniAppCommandRouter(sub_actor, "sub")
        sub_router.transport.identity_player_ids = {
            "主魂": 200,
            "厚土": -201,
            "缘生子": -202,
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
        router = MiniAppCommandRouter(actor, "main")
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

        async def action(identity, action, plot_key="", star_name=""):
            if action == "soothe":
                plots = [
                    {"key": "1", "status": "可收集"},
                    {"key": "2", "status": "可收集"},
                    {"key": "3", "empty": True},
                ]
                message = "安抚完成"
            elif action == "collect":
                plots = [
                    {"key": "1", "status": "正常"},
                    {"key": "2", "empty": True},
                    {"key": "3", "empty": True},
                ]
                message = "成功收集星辰精华"
            else:
                plots = [
                    {"key": "2", "name": "天雷星", "remainingSeconds": 3598},
                    {"key": "3", "name": "天雷星", "remainingSeconds": 3600},
                ]
                message = f"已在星位 {plot_key} 牵引{star_name}"
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
            ],
        )
        pull_calls = router.transport.sect_farm_action.await_args_list[-2:]
        self.assertEqual([call.kwargs["plot_key"] for call in pull_calls], ["2", "3"])
        self.assertTrue(all(call.kwargs["star_name"] == "天雷星" for call in pull_calls))
        self.assertEqual(actor.rewards[0][0:2], ("素缘子", ".收集精华"))

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

    def test_unknown_identity_is_rejected(self):
        transport = MiniAppDwellingTransport(object(), ENTRY)
        transport.identity_player_ids = {"主魂": 100}
        with self.assertRaises(MiniAppBeastError):
            transport.player_id("不存在")


if __name__ == "__main__":
    unittest.main()
