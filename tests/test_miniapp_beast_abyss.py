import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from dashboard_server import build_command_panels
from miniapp_beast_abyss import (
    MiniAppBeastAbyssWorker,
    abyss_status,
    choose_abyss_beast,
)
from miniapp_dwelling import MiniAppDwellingTransport


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
START = {
    "ok": True,
    "identity": {
        "selectedPlayerId": 100,
        "choices": [{"playerId": 100, "daoName": "主号", "source": "personal"}],
    },
}


def beast(
    beast_id,
    name,
    power,
    *,
    can_explore=True,
    stamina=100,
):
    return {
        "id": beast_id,
        "full_name": name,
        "status": "休息中",
        "species": "1阶灵兽",
        "beast_type": "灵兽",
        "tier": 1,
        "level": 1,
        "power": power,
        "stamina": stamina,
        "exp": 0,
        "can_explore_abyss": can_explore,
    }


class FakeLogger:
    def __init__(self):
        self.info_messages = []

    def info(self, message, *args, **kwargs):
        self.info_messages.append(message % args if args else message)

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeActor:
    def __init__(self, account="main", sect="万灵宗"):
        self.account_key = account
        self.client = object()
        self.config = {"miniapp_beast": {"entry_url": ENTRY}}
        self.state = {
            "miniapp_sect_name": sect,
            "sect_name": sect,
            "identity_sect_names": {"主魂": sect},
        }
        self.identity_sect_names = {"主魂": sect}
        self.sect_name = sect
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.is_running = True
        self.beast_lock = None
        self.saved = 0
        self.rewards = []

    def save_state(self):
        self.saved += 1

    def identity_pause_seconds(self, identity):
        return 0

    def record_daily_reward_event(self, identity, command, text, **kwargs):
        self.rewards.append((identity, command, text, kwargs))
        return True


class FakeTransport:
    def __init__(self, snapshots, action_payload=None):
        self.identity_player_ids = {"主魂": 100}
        self.snapshots = list(snapshots)
        self.action_payload = action_payload or {}
        self.enter_calls = []
        self.snapshot_calls = []
        self.initialize_calls = 0

    async def initialize(self):
        self.initialize_calls += 1
        return START

    async def spirit_beast_snapshot(self, identity, log_operation=True):
        self.snapshot_calls.append((identity, log_operation))
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    async def spirit_beast_abyss_enter(self, identity, beast_id, beast_name=""):
        self.enter_calls.append((identity, beast_id, beast_name))
        return self.action_payload


def snapshot(beasts, *, ready=True, remaining=0, ready_at=""):
    abyss = {
        "cooldownHours": 6,
        "minStamina": 30,
        "ready": ready,
        "remainingSeconds": remaining,
    }
    if ready_at:
        abyss["readyAt"] = ready_at
    return {
        "beasts": list(beasts),
        "player": {"sect": "万灵宗"},
        "raw": {"ok": True, "abyss": abyss},
    }


class MiniAppBeastAbyssTests(unittest.TestCase):
    def test_ready_at_takes_precedence_over_remaining_seconds(self):
        now = datetime(2026, 7, 30, 10, 0, 0)
        status = abyss_status({
            "abyss": {
                "ready": False,
                "readyAt": "2026-07-30 11:00:00",
                "remainingSeconds": 5,
            }
        }, now=now)

        self.assertEqual(status["remaining_seconds"], 3600)
        self.assertEqual(status["cooldown_source"], "readyAt")

    def test_highest_power_explicitly_eligible_beast_is_selected(self):
        selected = choose_abyss_beast([
            beast(1, "不可用强兽", 99999, can_explore=False),
            beast(2, "可用弱兽", 500),
            beast(3, "可用强兽", 1200),
        ])

        self.assertEqual(selected["id"], 3)

    def test_cooldown_snapshot_does_not_enter_and_is_silent(self):
        actor = FakeActor()
        transport = FakeTransport([
            snapshot(
                [beast(1, "灵狐", 100)],
                ready=False,
                remaining=1234,
            )
        ])
        worker = MiniAppBeastAbyssWorker(actor, transport, "main", FakeLogger())

        wait = asyncio.run(worker.run_once(now=datetime(2026, 7, 30, 10, 0, 0)))

        self.assertEqual(wait, 1239)
        self.assertEqual(transport.enter_calls, [])
        self.assertEqual(transport.snapshot_calls, [("主魂", False)])
        self.assertEqual(actor.state["next_abyss_time"], "2026-07-30 10:20:34")

    def test_win_or_loss_records_result_and_new_cooldown(self):
        for won in (True, False):
            with self.subTest(won=won):
                actor = FakeActor()
                action = {
                    "ok": True,
                    "message": "探渊胜利" if won else "虽未取胜，灵兽已归来",
                    "result": {"won": won, "title": "万兽渊激战"},
                    "abyss": {
                        "cooldownHours": 6,
                        "ready": False,
                        "remainingSeconds": 21600,
                    },
                }
                transport = FakeTransport([
                    snapshot([
                        beast(1, "低战力", 500),
                        beast(2, "高战力", 1200),
                    ])
                ], action_payload=action)
                worker = MiniAppBeastAbyssWorker(actor, transport, "main", FakeLogger())

                wait = asyncio.run(worker.run_once())

                self.assertEqual(wait, 21605)
                self.assertEqual(transport.enter_calls, [("主魂", 2, "高战力")])
                self.assertEqual(actor.state["beast_abyss_miniapp_last_won"], won)
                self.assertEqual(actor.state["beast_abyss_miniapp_last_error"], "")
                self.assertEqual(actor.rewards[0][0:2], ("主魂", ".探渊 高战力"))
                next_time = datetime.strptime(actor.state["next_abyss_time"], "%Y-%m-%d %H:%M:%S")
                self.assertGreater((next_time - datetime.now()).total_seconds(), 5 * 3600)

    def test_no_available_beast_retries_without_entering(self):
        actor = FakeActor()
        transport = FakeTransport([
            snapshot([beast(1, "出战灵兽", 9999, can_explore=False)])
        ])
        worker = MiniAppBeastAbyssWorker(actor, transport, "xiaohao", FakeLogger())

        wait = asyncio.run(worker.run_once())

        self.assertEqual(wait, 900)
        self.assertEqual(transport.enter_calls, [])
        self.assertEqual(
            actor.state["beast_abyss_miniapp_last_error"],
            "beast_abyss_no_available_beast",
        )

    def test_stale_post_action_page_cannot_trigger_an_immediate_second_enter(self):
        actor = FakeActor()
        ready_snapshot = snapshot([beast(1, "灵狐", 100)])
        transport = FakeTransport(
            [ready_snapshot, ready_snapshot],
            action_payload={
                "ok": True,
                "message": "灵狐已经进入万兽渊",
                "result": {"won": False},
            },
        )
        worker = MiniAppBeastAbyssWorker(actor, transport, "main", FakeLogger())

        wait = asyncio.run(worker.run_once())

        self.assertEqual(wait, 21605)
        self.assertEqual(len(transport.enter_calls), 1)
        self.assertEqual(
            actor.state["beast_abyss_miniapp_refresh_error"],
            "stale_state_after_action",
        )
        next_time = datetime.strptime(actor.state["next_abyss_time"], "%Y-%m-%d %H:%M:%S")
        self.assertGreater((next_time - datetime.now()).total_seconds(), 5 * 3600)

    def test_only_requested_wanling_accounts_are_enabled(self):
        for account, expected in (
            ("main", True),
            ("xiaohao", True),
            ("sub", False),
            ("waaiging", False),
        ):
            actor = FakeActor(account=account)
            worker = MiniAppBeastAbyssWorker(
                actor,
                FakeTransport([snapshot([beast(1, "灵狐", 100)])]),
                account,
                FakeLogger(),
            )
            self.assertEqual(worker.enabled, expected, account)
        actor = FakeActor(account="main", sect="天星宗")
        worker = MiniAppBeastAbyssWorker(
            actor,
            FakeTransport([snapshot([beast(1, "灵狐", 100)])]),
            "main",
            FakeLogger(),
        )
        self.assertEqual(worker.identities(), [])

    def test_transport_uses_abyss_endpoint_and_only_logs_the_action(self):
        calls = []
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path == "/api/miniapp/xianxia-dwelling/start":
                return START
            if path == "/api/miniapp/xianxia-dwelling/external":
                return {"ok": True, "url": "/miniapp/spirit?startapp=spiritbeast_fixture"}
            if path == "/api/miniapp/xianxia-spirit-beast/start":
                return {
                    "ok": True,
                    "abyss": {"ready": True, "remainingSeconds": 0},
                    "beasts": [{
                        "id": 7,
                        "name": "幻光鲤",
                        "beastType": "灵鲤",
                        "tier": 3,
                        "stamina": 100,
                        "combatPower": 1075,
                        "canExploreAbyss": True,
                    }],
                }
            if path == "/api/miniapp/xianxia-spirit-beast/abyss/enter":
                return {
                    "ok": True,
                    "message": "幻光鲤探渊归来，获得阴凝之晶 x1",
                    "result": {"won": True},
                }
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            async def scenario():
                await transport.spirit_beast_snapshot("主魂", log_operation=False)
                return await transport.spirit_beast_abyss_enter("主魂", 7, "幻光鲤")

            payload = asyncio.run(scenario())

        self.assertTrue(payload["result"]["won"])
        action_path, action_body = calls[-1]
        self.assertEqual(action_path, "/api/miniapp/xianxia-spirit-beast/abyss/enter")
        self.assertEqual(action_body["beastId"], 7)
        combined = "\n".join(logger.info_messages)
        self.assertNotIn("读取万兽谷灵兽列表", combined)
        self.assertIn("OUT [Mini App | 主魂]:\n万兽谷探渊（幻光鲤）", combined)
        self.assertIn("幻光鲤探渊归来", combined)

    def test_dashboard_marks_abyss_as_miniapp_without_restoring_group_command(self):
        state = {
            "done": [],
            "avatars": {},
            "beast_abyss_miniapp_next_time": (
                datetime.now() + timedelta(hours=4)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for account in ("main", "xiaohao"):
            panel = build_command_panels(account, state)[0]
            rows = {item["command"]: item for item in panel["commands"]}
            self.assertIn("miniapp:spirit-beast-abyss", rows)
            self.assertEqual(
                rows["miniapp:spirit-beast-abyss"]["execution_channel"],
                "miniapp",
            )
            self.assertNotIn(".探渊 <灵兽>", rows)


if __name__ == "__main__":
    unittest.main()
