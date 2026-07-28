import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import (
    MiniAppDwellingTransport,
    apply_dwelling_snapshot,
    miniapp_command_allowed,
)
from miniapp_command_routing import MiniAppCommandRouter
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
        self.error_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else message

    def info(self, message, *args):
        self.info_messages.append(self._format(message, args))

    def error(self, message, *args):
        self.error_messages.append(self._format(message, args))


class MiniAppDwellingTests(unittest.TestCase):
    def test_periodic_sync_without_previous_timestamp_runs_immediately(self):
        self.assertEqual(_periodic_wait_seconds("", 12 * 3600), 0)
        self.assertEqual(_periodic_wait_seconds("not-a-timestamp", 12 * 3600), 0)

    def test_command_whitelist_rejects_group_only_actions(self):
        for command in (
            ".闭关修炼",
            ".元婴出窍",
            ".我的侍妾",
            ".登天阶",
            ".观命",
            ".定命 紫微",
            ".推命 闭关",
            ".改命 探索",
        ):
            self.assertTrue(miniapp_command_allowed(command), command)
        for command in (
            ".切换 主魂",
            ".洞府",
            ".我的灵兽",
            ".灵兽巡边 大圣 袭营",
            ".巡边归来",
            ".定命 不存在",
        ):
            self.assertFalse(miniapp_command_allowed(command), command)

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
            status = asyncio.run(transport.command(".查看闭关", identity="主魂"))

        self.assertEqual(transport.player_id("主魂"), 100)
        self.assertEqual(transport.player_id("素心子"), -200)
        self.assertEqual(response.text, "reply:.元婴出窍")
        self.assertEqual(status.text, "reply:status")
        self.assertEqual(calls[1][0], "/api/miniapp/xianxia-dwelling/command-center")
        self.assertEqual(calls[1][1]["playerId"], -200)
        self.assertEqual(calls[2][0], "/api/miniapp/xianxia-dwelling/deep-seclusion")

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

    def test_every_semantic_miniapp_operation_is_logged(self):
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
        self.assertIn("IN [Mini App | 主魂]:\n同步洞府首页 -> 完成", combined)
        self.assertIn("IN [Mini App | 素心子]:\n指令 .元婴出窍 -> 元婴已出窍", combined)
        self.assertIn("IN [Mini App | 主魂]:\n读取万兽谷灵兽列表 -> 1 只灵兽", combined)
        self.assertIn("IN [Mini App | 主魂]:\n万兽谷灵兽安抚（ID 7） -> 大圣安抚完成", combined)
        self.assertIn("IN [Mini App | 素心子]:\n读取宗门灵圃 -> 1 个星位", combined)
        self.assertIn("IN [Mini App | 素心子]:\n宗门灵圃安抚星辰 -> 安抚完成", combined)

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

        payload = asyncio.run(worker._star_action("soothe"))

        self.assertTrue(payload["ok"])
        worker.transport.sect_farm_action.assert_awaited_once_with(
            "素心子",
            "soothe",
            plot_key="",
            star_name="",
        )
        self.assertEqual(actor.state["avatars"]["素心子"]["star_miniapp_last_action"], "soothe")

    def test_unknown_identity_is_rejected(self):
        transport = MiniAppDwellingTransport(object(), ENTRY)
        transport.identity_player_ids = {"主魂": 100}
        with self.assertRaises(MiniAppBeastError):
            transport.player_id("不存在")


if __name__ == "__main__":
    unittest.main()
