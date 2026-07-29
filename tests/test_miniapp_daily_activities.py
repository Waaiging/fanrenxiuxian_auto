import asyncio
import unittest
from datetime import datetime

from miniapp_daily_activities import (
    MiniAppDailyActivities,
    choose_hunt_cell,
)


class FirstChoice:
    def choice(self, values):
        return list(values)[0]


class FakeActor:
    def __init__(self, avatars=None):
        self.account_key = "main"
        self.config = {"miniapp_beast": {}}
        self.avatars = list(avatars or [])
        self.state = {"avatars": {name: {} for name in self.avatars}}
        self.saved = 0
        self.rewards = []
        self.is_running = True
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self.pause_event = asyncio.Event()
        self.pause_event.set()

    def get_avatar_state(self, identity):
        return self.state["avatars"][identity]

    def save_state(self):
        self.saved += 1

    def identity_pause_seconds(self, identity):
        return 0

    def record_daily_reward_event(self, identity, command, text, **kwargs):
        self.rewards.append((identity, command, text, kwargs))
        return True


class FakeLogger:
    def __init__(self):
        self.info_messages = []

    def info(self, *args, **kwargs):
        message = args[0] if args else ""
        self.info_messages.append(message % args[1:] if len(args) > 1 else message)

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class MiniAppDailyActivityTests(unittest.TestCase):
    def test_hunt_cell_prefers_yellow_marker_matching_direction(self):
        cells = [{"index": index, "revealed": False} for index in range(25)]
        cells[12] = {
            "index": 12,
            "revealed": True,
            "type": "clue",
            "hint": {
                "text": "灵气流向东。古符轻鸣，近处疑有宝气。",
                "markers": [
                    {"index": 7, "kind": "treasure"},
                    {"index": 13, "kind": "treasure"},
                    {"index": 11, "kind": "risk"},
                    {"index": 17, "kind": "resource"},
                ],
            },
        }
        run = {"size": 5, "ap": 7, "cells": cells}

        self.assertEqual(choose_hunt_cell(run, rng=FirstChoice()), 13)

    def test_hunt_cell_avoids_red_and_green_markers_without_yellow(self):
        cells = [{"index": index, "revealed": False} for index in range(25)]
        cells[0] = {
            "index": 0,
            "revealed": True,
            "type": "clue",
            "hint": {
                "text": "灵气流向东南。",
                "markers": [
                    {"index": 1, "kind": "risk"},
                    {"index": 5, "kind": "resource"},
                ],
            },
        }
        run = {"size": 5, "ap": 7, "cells": cells}

        chosen = choose_hunt_cell(run, rng=FirstChoice())

        self.assertNotIn(chosen, {1, 5})
        self.assertEqual(chosen, 6)

    def test_target_reward_settles_immediately(self):
        actor = FakeActor()
        actor.state.update({
            "miniapp_hunt_summary_date": "2026-07-29",
            "miniapp_hunt_summary_loot": {"灵石": 90},
            "miniapp_hunt_summary_completed": 2,
            "miniapp_hunt_summary_logged_date": "",
        })

        class Transport:
            identity_player_ids = {"主魂": 100}

            def __init__(self):
                self.reveals = []
                self.settles = []

            async def initialize(self):
                return {}

            async def hunt_snapshot(self, identity):
                return {
                    "dwelling": {
                        "hasDwelling": True,
                        "hunt": {"used": 2, "limit": 3, "remaining": 1, "actionPoints": 8},
                    }
                }

            async def hunt_start(self, identity):
                return {
                    "dwelling": {
                        "hunt": {"used": 3, "limit": 3, "remaining": 0, "actionPoints": 8},
                    },
                    "huntRun": {
                        "sessionId": "hunt-1",
                        "size": 5,
                        "ap": 8,
                        "maxAp": 8,
                        "status": "active",
                        "foundMain": False,
                        "loot": [],
                        "cells": [
                            {"index": index, "revealed": False}
                            for index in range(25)
                        ],
                    },
                }

            async def hunt_reveal(self, identity, session_id, index):
                self.reveals.append(index)
                cells = [{"index": cell, "revealed": False} for cell in range(25)]
                cells[index] = {
                    "index": index,
                    "revealed": True,
                    "type": "chest",
                    "title": "宝匣",
                    "loot": {"name": "阴凝之晶", "quantity": 1},
                }
                return {
                    "huntRun": {
                        "sessionId": session_id,
                        "size": 5,
                        "ap": 7,
                        "maxAp": 8,
                        "status": "active",
                        "foundMain": False,
                        "loot": [{"name": "阴凝之晶", "quantity": 1}],
                        "cells": cells,
                    }
                }

            async def hunt_settle(self, identity, session_id):
                self.settles.append(session_id)
                return {
                    "dwelling": {
                        "hunt": {"used": 3, "limit": 3, "remaining": 0, "actionPoints": 8},
                    },
                    "huntResult": {
                        "grade": "丙等",
                        "score": 30,
                        "revealedCount": 1,
                        "foundMain": False,
                        "contribution": 0,
                        "loot": [{"name": "阴凝之晶", "quantity": 1}],
                    },
                }

        transport = Transport()
        logger = FakeLogger()
        runner = MiniAppDailyActivities(actor, transport, "main", logger)
        runner.rng = FirstChoice()

        result = asyncio.run(runner.run_hunt_identity("主魂", today="2026-07-29"))

        self.assertEqual(result, "completed")
        self.assertEqual(transport.reveals, [0])
        self.assertEqual(transport.settles, ["hunt-1"])
        self.assertEqual(actor.state["miniapp_hunt_last_stop_reason"], "target_reward")
        self.assertEqual(actor.state["miniapp_hunt_last_date"], "2026-07-29")
        self.assertEqual(actor.state["miniapp_hunt_summary_completed"], 3)
        self.assertEqual(
            actor.state["miniapp_hunt_summary_loot"],
            {"灵石": 90, "阴凝之晶": 1},
        )
        self.assertEqual(actor.state["miniapp_hunt_last_result"], "灵石 x90，阴凝之晶 x1")
        self.assertEqual(len(actor.rewards), 1)
        combined = "\n".join(logger.info_messages)
        self.assertIn("OUT [Mini App | 主魂]:\n洞府寻宝（每日 3 局）", combined)
        self.assertIn(
            "IN [Mini App | 主魂]:\n洞府寻宝（每日 3 局） -> 灵石 x90，阴凝之晶 x1",
            combined,
        )
        self.assertNotIn("得分", combined)
        self.assertNotIn("主宝匣", combined)

        self.assertEqual(
            asyncio.run(runner.run_hunt_identity("主魂", today="2026-07-29")),
            "done",
        )
        self.assertEqual(len(logger.info_messages), 2)
        self.assertEqual(len(actor.rewards), 1)

    def test_pagoda_runs_only_once_per_identity_per_day(self):
        actor = FakeActor(avatars=["素缘子"])

        class Transport:
            identity_player_ids = {"主魂": 100, "素缘子": -200}

            def __init__(self):
                self.challenges = []

            async def initialize(self):
                return {}

            async def pagoda_snapshot(self, identity):
                return {
                    "state": {
                        "canChallenge": True,
                        "todayHighest": 0,
                        "failedFloor": 0,
                        "resetsToday": 0,
                    }
                }

            async def pagoda_challenge(self, identity):
                self.challenges.append(identity)
                return {
                    "state": {"canChallenge": False, "todayHighest": 20, "failedFloor": 21},
                    "replay": {
                        "clearedCount": 20,
                        "endFloor": 20,
                        "failedFloor": 21,
                        "report": "修为增加 1200 点，获得塔印 40 点。",
                    },
                }

        transport = Transport()
        runner = MiniAppDailyActivities(actor, transport, "main", FakeLogger())

        first = asyncio.run(
            runner.run_pagoda_daily_once(datetime(2026, 7, 29, 23, 0, 1))
        )
        second = asyncio.run(
            runner.run_pagoda_daily_once(datetime(2026, 7, 29, 23, 30, 1))
        )

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(transport.challenges, ["主魂", "素缘子"])
        self.assertEqual(actor.state["last_tower_date"], "2026-07-29")
        self.assertEqual(
            actor.state["avatars"]["素缘子"]["last_tower_date"],
            "2026-07-29",
        )
        self.assertEqual(len(actor.rewards), 2)


if __name__ == "__main__":
    unittest.main()
