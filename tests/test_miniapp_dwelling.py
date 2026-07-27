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

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            response = asyncio.run(transport.command(".查看闭关"))

        self.assertIn("深度闭关总结", response.text)
        self.assertEqual(calls[-2:], [
            ("/api/miniapp/xianxia-dwelling/deep-seclusion", "status"),
            ("/api/miniapp/xianxia-dwelling/deep-seclusion", "settle"),
        ])

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

    def test_snapshot_updates_legacy_meditation_state(self):
        actor = SimpleNamespace(state={}, get_avatar_state=lambda name: actor.state.setdefault("avatar", {}))
        payload = {
            "account": {"playerId": -200, "daoName": "素心子", "profile": {"sectName": "星宫"}},
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

    def test_command_center_placeholder_does_not_clear_deep_meditation(self):
        actor = SimpleNamespace(
            state={
                "in_deep_meditation": True,
                "deep_meditation_end_time": "2026-07-28 08:00:00",
                "meditation_restart_pending": False,
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
