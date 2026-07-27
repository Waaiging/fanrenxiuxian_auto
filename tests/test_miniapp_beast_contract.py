import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from miniapp_beast_contract import MiniAppBeastContractWorker
from miniapp_dwelling import MiniAppDwellingTransport


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
START = {
    "ok": True,
    "identity": {
        "selectedPlayerId": 100,
        "choices": [{"playerId": 100, "daoName": "主号", "source": "personal"}],
    },
}
BEASTS = [
    {
        "id": 1,
        "full_name": "大圣",
        "status": "休息中",
        "species": "6阶金刚巨猿",
        "beast_type": "金刚巨猿",
        "tier": 6,
        "level": 100,
        "power": 59150,
        "stamina": 35,
        "exp": 1200,
    },
    {
        "id": 2,
        "full_name": "灵狐",
        "status": "休息中",
        "species": "1阶灵狐",
        "beast_type": "灵狐",
        "tier": 1,
        "level": 1,
        "power": 20,
        "stamina": 80,
        "exp": 0,
    },
]


class DummyActor:
    def __init__(self, sect="万灵宗"):
        self.client = object()
        self.config = {
            "miniapp_beast": {
                "enabled": True,
                "entry_url": ENTRY,
                "contract_interaction_seconds": 7200,
                "contract_interaction_retry_seconds": 300,
                "contract_interaction_delay_seconds": 0,
            }
        }
        self.identity_sect_names = {"主魂": sect}
        self.sect_name = sect
        self.state = {}
        self.beast_lock = asyncio.Lock()
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.is_running = True
        self.saved = 0

    def save_state(self):
        self.saved += 1


class FakeTransport:
    def __init__(self, fail_once=None):
        self.fail_once = set(fail_once or [])
        self.calls = []

    async def spirit_beast_snapshot(self, identity):
        return {"beasts": copy.deepcopy(BEASTS), "player": {"sect": "万灵宗"}}

    async def spirit_beast_interaction(self, identity, beast_id, interaction):
        self.calls.append((identity, beast_id, interaction))
        if beast_id in self.fail_once:
            self.fail_once.remove(beast_id)
            raise MiniAppBeastError("temporary_failure")
        source = next(item for item in BEASTS if item["id"] == beast_id)
        return {
            "ok": True,
            "message": f"{source['full_name']} 安抚完成，体力 +10",
            "beast": {"id": beast_id, "stamina": source["stamina"] + 10},
        }


class MiniAppBeastContractTests(unittest.TestCase):
    def test_transport_uses_confirmed_action_endpoint_and_body(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path == "/api/miniapp/xianxia-dwelling/start":
                return START
            if path == "/api/miniapp/xianxia-dwelling/external":
                return {"ok": True, "url": "/miniapp/spirit?startapp=spiritbeast_fixture"}
            if path == "/api/miniapp/xianxia-spirit-beast/action":
                return {"ok": True, "message": "安抚完成", "beast": {"id": 7, "stamina": 42}}
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            payload = asyncio.run(transport.spirit_beast_interaction("主魂", 7, "安抚"))

        self.assertEqual(payload["beast"]["stamina"], 42)
        action_path, action_body = calls[-1]
        self.assertEqual(action_path, "/api/miniapp/xianxia-spirit-beast/action")
        self.assertEqual(action_body["action"], "interact")
        self.assertEqual(action_body["beastId"], 7)
        self.assertEqual(action_body["interaction"], "安抚")
        self.assertEqual(action_body["token"], "spiritbeast_fixture")
        self.assertEqual(action_body["initData"], "signed")

    def test_cycle_interacts_with_every_beast_once_and_updates_stamina(self):
        actor = DummyActor()
        transport = FakeTransport()
        worker = MiniAppBeastContractWorker(actor, transport)

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(transport.calls, [
            ("主魂", 1, "安抚"),
            ("主魂", 2, "安抚"),
        ])
        self.assertEqual(actor.state["beasts_cache"][0]["stamina"], 45)
        self.assertEqual(actor.state["beasts_cache"][1]["stamina"], 90)
        self.assertEqual(actor.state["beast_contract_interaction_success_count"], 2)
        self.assertEqual(actor.state["beast_contract_interaction_completed_count"], 1)
        self.assertEqual(actor.state["beast_contract_interaction_last_error"], "")

    def test_partial_failure_retries_only_failed_beast(self):
        actor = DummyActor()
        transport = FakeTransport(fail_once={2})
        worker = MiniAppBeastContractWorker(actor, transport)

        self.assertFalse(asyncio.run(worker.run_cycle()))
        self.assertEqual([call[1] for call in transport.calls], [1, 2])
        self.assertEqual(actor.state["beast_contract_interaction_results"]["1"]["status"], "success")
        self.assertEqual(actor.state["beast_contract_interaction_results"]["2"]["status"], "failed")

        actor.state["beast_contract_interaction_results"]["2"]["next_time"] = "2000-01-01 00:00:00"
        actor.state["beast_contract_interaction_next_time"] = "2000-01-01 00:00:00"
        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual([call[1] for call in transport.calls], [1, 2, 2])
        self.assertEqual(actor.state["beast_contract_interaction_completed_count"], 1)
        self.assertEqual(actor.state["beast_contract_interaction_last_error"], "")

    def test_non_wanling_account_is_excluded(self):
        actor = DummyActor(sect="天星宗")
        transport = FakeTransport()
        worker = MiniAppBeastContractWorker(actor, transport)

        self.assertFalse(worker.enabled)
        actor.is_running = False
        asyncio.run(worker.run())
        self.assertEqual(transport.calls, [])
        self.assertFalse(actor.state["beast_contract_interaction_enabled"])


if __name__ == "__main__":
    unittest.main()
