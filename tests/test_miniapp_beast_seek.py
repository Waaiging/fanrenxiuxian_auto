import asyncio
import copy
import unittest
from datetime import datetime

from dashboard_server import build_command_panels
from miniapp_beast_seek import MiniAppBeastSeekWorker


EXISTING = [
    {
        "id": 1,
        "full_name": "六翼",
        "beast_type": "六翼霜蚣",
        "species": "3阶六翼霜蚣",
        "tier": 3,
        "power": 900,
        "stamina": 80,
        "status": "休息中",
    },
    {
        "id": 2,
        "full_name": "风希",
        "beast_type": "风雀",
        "species": "1阶风雀",
        "tier": 1,
        "power": 300,
        "stamina": 100,
        "status": "休息中",
    },
]


class FakeActor:
    def __init__(self, *, account="xiaohao", settings=None):
        self.account_key = account
        self.sect_name = "万灵宗"
        self.identity_sect_names = {"主魂": "万灵宗"}
        self.config = {
            "miniapp_beast": {
                "enabled": True,
                "entry_url": "https://t.me/fanrenxiuxian_bot?startapp=fixture",
                "beast_seek_enabled": True,
                "beast_seek_interval_seconds": 21600,
                "beast_seek_retry_seconds": 60,
                "beast_seek_target": "双瞳鼠",
                **(settings or {}),
            }
        }
        self.state = {
            "beast_hunt_stopped": True,
            "beast_hunt_stopped_reason": "旧风雀规则",
            "next_hunt_time": "",
        }
        self.saved = 0
        self.is_running = True
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.beast_lock = asyncio.Lock()

    def save_state(self):
        self.saved += 1

    def dashboard_command_paused(self, command, identity):
        return False

    def update_best_beast_tracking(self, beast=None):
        return beast


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []
        self.error_messages = []

    @staticmethod
    def _format(message, args):
        return message % args if args else message

    def info(self, message, *args, **kwargs):
        self.info_messages.append(self._format(message, args))

    def warning(self, message, *args, **kwargs):
        self.warning_messages.append(self._format(message, args))

    def error(self, message, *args, **kwargs):
        self.error_messages.append(self._format(message, args))

    def critical(self, message, *args, **kwargs):
        self.error_messages.append(self._format(message, args))


class FakeTransport:
    def __init__(self, current, found=None):
        self.current = copy.deepcopy(current)
        self.found = copy.deepcopy(found)
        self.seek_calls = []
        self.release_calls = []
        self.snapshot_calls = []
        self.initialize_calls = 0

    def _snapshot(self):
        return {
            "beasts": copy.deepcopy(self.current),
            "raw": {
                "capacity": {
                    "used": len(self.current),
                    "limit": 10,
                    "remaining": max(0, 10 - len(self.current)),
                }
            },
        }

    async def initialize(self):
        self.initialize_calls += 1
        return {}

    async def spirit_beast_snapshot(self, identity, log_operation=True):
        self.snapshot_calls.append((identity, log_operation))
        return self._snapshot()

    async def spirit_beast_seek(self, identity):
        self.seek_calls.append(identity)
        if self.found is not None:
            self.current.append(copy.deepcopy(self.found))
        result = self._snapshot()
        result["message"] = (
            f"寻得【{self.found['full_name']}】" if self.found is not None else "灵兽都被吓跑了"
        )
        return result

    async def spirit_beast_release(self, identity, beast_id, beast_name=""):
        self.release_calls.append((identity, beast_id, beast_name))
        self.current = [item for item in self.current if item["id"] != beast_id]
        result = self._snapshot()
        result["message"] = f"已解除与【{beast_name}】的灵契"
        return result


class MiniAppBeastSeekTests(unittest.TestCase):
    def test_target_beast_is_kept_and_existing_roster_is_untouched(self):
        target = {
            "id": 99,
            "full_name": "灵眼",
            "beast_type": "双瞳鼠",
            "species": "1阶双瞳鼠",
            "tier": 1,
            "power": 120,
            "stamina": 100,
            "status": "休息中",
        }
        actor = FakeActor()
        transport = FakeTransport(EXISTING, target)
        worker = MiniAppBeastSeekWorker(actor, transport, "xiaohao", FakeLogger())

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(transport.seek_calls, ["主魂"])
        self.assertEqual(transport.release_calls, [])
        self.assertEqual({item["id"] for item in transport.current}, {1, 2, 99})
        self.assertTrue(actor.state["beast_seek_miniapp_last_found_is_target"])
        self.assertEqual(actor.state["beast_seek_miniapp_pending_release_id"], 0)
        self.assertFalse(actor.state["beast_hunt_stopped"])
        self.assertIn("已保留", actor.state["beast_seek_miniapp_last_result"])

    def test_only_new_non_target_beast_is_released(self):
        found = {
            "id": 99,
            "full_name": "新来的噬金虫",
            "beast_type": "噬金虫",
            "species": "1阶噬金虫",
            "tier": 1,
            "power": 160,
            "stamina": 100,
            "status": "休息中",
        }
        actor = FakeActor()
        transport = FakeTransport(EXISTING, found)
        logger = FakeLogger()
        worker = MiniAppBeastSeekWorker(actor, transport, "xiaohao", logger)

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(
            transport.release_calls,
            [("主魂", 99, "新来的噬金虫")],
        )
        self.assertEqual({item["id"] for item in transport.current}, {1, 2})
        self.assertEqual(
            {item["id"] for item in actor.state["beasts_cache"]},
            {1, 2},
        )
        self.assertEqual(actor.state["beast_seek_miniapp_last_release_id"], 99)
        self.assertEqual(actor.state["beast_seek_miniapp_pending_release_id"], 0)
        self.assertIn("非目标，已放生", actor.state["beast_seek_miniapp_last_result"])
        self.assertTrue(any("id=99" in message for message in logger.info_messages))

    def test_pending_release_retry_uses_persisted_new_id_only(self):
        pending = {
            "id": 77,
            "full_name": "刚寻到的玉尾蝎",
            "beast_type": "玉尾蝎",
            "species": "1阶玉尾蝎",
            "tier": 1,
            "power": 140,
            "stamina": 100,
            "status": "休息中",
        }
        actor = FakeActor()
        actor.state.update({
            "beast_seek_miniapp_pending_release_id": 77,
            "beast_seek_miniapp_pending_release_name": pending["full_name"],
            "beast_seek_miniapp_pending_release_type": pending["beast_type"],
            "beast_seek_miniapp_seek_next_time": "2099-01-01 00:00:00",
        })
        transport = FakeTransport([*EXISTING, pending])
        worker = MiniAppBeastSeekWorker(actor, transport, "xiaohao", FakeLogger())

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(transport.seek_calls, [])
        self.assertEqual(transport.release_calls, [("主魂", 77, pending["full_name"])])
        self.assertEqual({item["id"] for item in transport.current}, {1, 2})

    def test_restart_recovers_new_beast_from_saved_pre_seek_ids(self):
        found = {
            "id": 88,
            "full_name": "刚寻到的灵狐",
            "beast_type": "灵狐",
            "species": "1阶灵狐",
            "tier": 1,
            "power": 180,
            "stamina": 100,
            "status": "休息中",
        }
        actor = FakeActor()
        actor.state.update({
            "beast_seek_miniapp_inflight_started_at": datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "beast_seek_miniapp_inflight_existing_ids": [1, 2],
        })
        transport = FakeTransport([*EXISTING, found])
        worker = MiniAppBeastSeekWorker(actor, transport, "xiaohao", FakeLogger())

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(transport.seek_calls, [])
        self.assertEqual(transport.release_calls, [("主魂", 88, found["full_name"])])
        self.assertEqual({item["id"] for item in transport.current}, {1, 2})

    def test_pending_target_id_is_never_released(self):
        target = {
            "id": 66,
            "full_name": "灵眼",
            "beast_type": "双瞳鼠",
            "species": "1阶双瞳鼠",
            "tier": 1,
            "power": 120,
            "stamina": 100,
            "status": "休息中",
        }
        actor = FakeActor()
        actor.state.update({
            "beast_seek_miniapp_pending_release_id": 66,
            "beast_seek_miniapp_pending_release_name": target["full_name"],
            "beast_seek_miniapp_pending_release_type": target["beast_type"],
        })
        transport = FakeTransport([*EXISTING, target])
        worker = MiniAppBeastSeekWorker(actor, transport, "xiaohao", FakeLogger())

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(transport.release_calls, [])
        self.assertIn(66, {item["id"] for item in transport.current})
        self.assertEqual(actor.state["beast_seek_miniapp_pending_release_id"], 0)
        self.assertIn("安全拦截", actor.state["beast_seek_miniapp_last_release_result"])

    def test_full_roster_never_releases_an_existing_beast(self):
        full = [
            {
                "id": index,
                "full_name": f"旧灵兽{index}",
                "beast_type": "风雀",
                "species": "1阶风雀",
                "tier": 1,
                "power": index,
                "stamina": 100,
                "status": "休息中",
            }
            for index in range(1, 11)
        ]
        actor = FakeActor()
        transport = FakeTransport(full)
        worker = MiniAppBeastSeekWorker(actor, transport, "xiaohao", FakeLogger())

        self.assertTrue(asyncio.run(worker.run_cycle()))

        self.assertEqual(transport.seek_calls, [])
        self.assertEqual(transport.release_calls, [])
        self.assertEqual(len(transport.current), 10)
        self.assertIn("未放生任何现有灵兽", actor.state["beast_seek_miniapp_last_result"])

    def test_non_wanling_account_is_disabled(self):
        actor = FakeActor(account="sub")
        actor.sect_name = "元婴宗"
        actor.identity_sect_names = {"主魂": "元婴宗"}
        worker = MiniAppBeastSeekWorker(
            actor,
            FakeTransport(EXISTING),
            "sub",
            FakeLogger(),
        )

        self.assertFalse(worker.enabled)

    def test_xiaohao_dashboard_shows_seek_as_miniapp_scheduler(self):
        state = {
            "restricted_miniapp_active": True,
            "next_hunt_time": "2099-01-01 00:00:00",
            "avatars": {},
        }

        panel = next(
            item
            for item in build_command_panels("xiaohao", state)
            if item.get("identity") == "主魂"
        )
        row = next(
            item for item in panel["commands"] if item.get("command") == ".寻觅灵兽"
        )

        self.assertEqual(row["execution_channel"], "miniapp")
        self.assertIn("定时任务", row["execution_channel_detail"])


if __name__ == "__main__":
    unittest.main()
