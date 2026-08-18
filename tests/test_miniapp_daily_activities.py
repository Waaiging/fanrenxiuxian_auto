import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from miniapp_daily_activities import (
    MiniAppDailyActivities,
    choose_hunt_cell,
    fate_cards_wait_seconds,
    solve_tianji_trial_challenge,
)
from miniapp_dwelling import MiniAppCommandResponse


class FirstChoice:
    def choice(self, values):
        return list(values)[0]


class FixedChoice:
    def __init__(self, value):
        self.value = value

    def choice(self, values):
        self.assertion_values = tuple(values)
        return self.value


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
    def test_tianji_trial_solvers_cover_all_modes(self):
        fixtures = [
            {
                "challengeId": "planarity",
                "mode": "tianjiPlanarityV1",
                "minDurationMs": 2200,
                "nodes": [
                    {"id": "a", "x": 10, "y": 10},
                    {"id": "b", "x": 90, "y": 90},
                    {"id": "c", "x": 90, "y": 10},
                    {"id": "d", "x": 10, "y": 90},
                ],
                "edges": [
                    {"from": "a", "to": "b"},
                    {"from": "c", "to": "d"},
                    {"from": "a", "to": "c"},
                    {"from": "b", "to": "d"},
                ],
            },
            {
                "challengeId": "stargaze",
                "mode": "tianjiStargazeV1",
                "stars": [
                    {"id": "sun", "angle": 20, "targetAngle": 90},
                    {"id": "moon", "angle": 180, "targetAngle": 180, "locked": True},
                ],
            },
            {
                "challengeId": "lights",
                "mode": "tianjiLightsOutV1",
                "gridSize": 4,
                "targetState": 1,
                "cells": [1] * 16,
            },
            {
                "challengeId": "memory",
                "mode": "tianjiMemoryV1",
                "cards": [
                    {"id": "a1", "pair": "a"},
                    {"id": "b1", "pair": "b"},
                    {"id": "a2", "pair": "a"},
                    {"id": "b2", "pair": "b"},
                ],
            },
            {
                "challengeId": "meridian",
                "mode": "tianjiMeridianV1",
                "sequence": ["p1", "p3", "p2"],
            },
        ]

        proofs = [solve_tianji_trial_challenge(fixture) for fixture in fixtures]

        self.assertEqual([proof["mode"] for proof in proofs], [item["mode"] for item in fixtures])
        self.assertEqual(proofs[1]["angles"], {"sun": 90.0, "moon": 180.0})
        self.assertEqual(proofs[2]["events"], [])
        self.assertEqual(proofs[2]["cells"], [1] * 16)
        self.assertEqual([event["id"] for event in proofs[3]["events"]], ["a1", "a2", "b1", "b2"])
        self.assertEqual([event["id"] for event in proofs[4]["events"]], ["p1", "p3", "p2"])

    def test_lights_out_solver_handles_nontrivial_board(self):
        size = 4
        target = [1] * (size * size)
        cells = list(target)
        for index in (0, 5, 10):
            for neighbor in (
                [index]
                + ([index - size] if index >= size else [])
                + ([index + size] if index < size * (size - 1) else [])
                + ([index - 1] if index % size else [])
                + ([index + 1] if index % size < size - 1 else [])
            ):
                cells[neighbor] ^= 1

        proof = solve_tianji_trial_challenge(
            {
                "challengeId": "lights-nontrivial",
                "mode": "tianjiLightsOutV1",
                "gridSize": size,
                "targetState": 1,
                "cells": cells,
            }
        )

        self.assertEqual(proof["cells"], target)
        self.assertGreater(len(proof["events"]), 0)

    def test_planarity_solver_preserves_locked_node_without_crossings(self):
        challenge = {
            "challengeId": "locked-planarity",
            "mode": "tianjiPlanarityV1",
            "lockedNodeIds": ["0"],
            "nodes": [
                {"id": "0", "x": 20, "y": 80, "locked": True},
                {"id": "1", "x": 12, "y": 13},
                {"id": "2", "x": 79, "y": 11},
                {"id": "3", "x": 58, "y": 37},
                {"id": "4", "x": 64, "y": 13},
            ],
            "edges": [
                {"from": "0", "to": "1"},
                {"from": "0", "to": "2"},
                {"from": "0", "to": "3"},
                {"from": "0", "to": "4"},
                {"from": "1", "to": "2"},
                {"from": "1", "to": "4"},
                {"from": "2", "to": "3"},
                {"from": "3", "to": "4"},
            ],
        }

        proof = solve_tianji_trial_challenge(challenge)

        self.assertEqual(proof["positions"]["0"], {"x": 20.0, "y": 80.0})
        self.assertTrue(
            all(
                4 <= point[axis] <= 96
                for point in proof["positions"].values()
                for axis in ("x", "y")
            )
        )

    def test_tianji_trial_runs_all_three_returned_challenges(self):
        actor = FakeActor()

        class Transport:
            identity_player_ids = {"主魂": 100}

            def __init__(self):
                self.finished = []

            async def tianji_trial_start(self, identity):
                return {
                    "dailyProgress": {"completed": 0, "limit": 3},
                    "challenge": {
                        "challengeId": "trial-1",
                        "mode": "tianjiMeridianV1",
                        "minDurationMs": 350,
                        "sequence": ["p1"],
                    },
                }

            async def tianji_trial_finish(self, identity, proof):
                self.finished.append(proof["challengeId"])
                completed = len(self.finished)
                payload = {
                    "dailyProgress": {"completed": completed, "limit": 3},
                    "result": {
                        "grade": "甲等",
                        "reward_trace": 5,
                        "daily_progress": completed,
                        "daily_limit": 3,
                        "balance": completed * 5,
                    },
                }
                if completed < 3:
                    payload["nextChallenge"] = {
                        "challengeId": f"trial-{completed + 1}",
                        "mode": "tianjiMeridianV1",
                        "minDurationMs": 350,
                        "sequence": [f"p{completed + 1}"],
                    }
                return payload

        transport = Transport()
        logger = FakeLogger()
        runner = MiniAppDailyActivities(actor, transport, "main", logger)

        with patch("miniapp_daily_activities.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(
                runner.run_tianji_trial_identity("主魂", today="2026-08-14")
            )

        self.assertEqual(result, "completed")
        self.assertEqual(transport.finished, ["trial-1", "trial-2", "trial-3"])
        self.assertEqual(actor.state["miniapp_tianji_trial_last_date"], "2026-08-14")
        self.assertEqual(actor.state["miniapp_tianji_trial_completed"], 3)
        self.assertIn("3 关完成", actor.state["miniapp_tianji_trial_last_result"])
        self.assertEqual(len(actor.rewards), 1)
        combined = "\n".join(logger.info_messages)
        self.assertIn("OUT [Mini App | 主魂]:\n天机试炼（每日 3 关）", combined)
        self.assertIn("天机试炼汇总（今日完成 3/3 关）", combined)
        self.assertIn("第 1 关：甲等，天机残痕 +5", combined)
        self.assertIn("第 2 关：甲等，天机残痕 +5", combined)
        self.assertIn("第 3 关：甲等，天机残痕 +5", combined)
        self.assertIn("合计：天机残痕 +15，余额 15", combined)

    def test_tianji_trial_honors_dashboard_identity_selection_and_switch(self):
        actor = FakeActor(avatars=["无咎子", "缘生子", "素缘子"])

        class Transport:
            identity_player_ids = {
                "主魂": 100,
                "无咎子": -101,
                "缘生子": -102,
                "素缘子": -103,
            }

        runner = MiniAppDailyActivities(actor, Transport(), "main", FakeLogger())
        selected = {
            "miniapp_tianji_trial": {
                "enabled": True,
                "participants": ["main|无咎子", "main|素缘子", "sub|主魂"],
            }
        }
        disabled = {
            "miniapp_tianji_trial": {
                "enabled": False,
                "participants": ["main|主魂"],
            }
        }

        with patch("miniapp_daily_activities.load_automation_settings", return_value=selected):
            self.assertEqual(runner.tianji_trial_identities(), ["无咎子", "素缘子"])
        with patch("miniapp_daily_activities.load_automation_settings", return_value=disabled):
            self.assertEqual(runner.tianji_trial_identities(), [])

    def test_fate_cards_hide_waits_then_settles_and_logs_final_result(self):
        actor = FakeActor()

        class Transport:
            identity_player_ids = {"主魂": 100}

            def __init__(self):
                self.calls = []
                self.record = {}

            async def fate_cards_start(self, identity):
                self.calls.append(("start", identity))
                if self.record and self.record.get("choiceKey") == "hide":
                    self.record["quest"]["canSettle"] = True
                return {"record": self.record} if self.record else {"hasDrawn": False}

            async def fate_cards_draw(self, identity, question_key):
                self.calls.append(("draw", identity, question_key))
                self.record = {
                    "questionKey": question_key,
                    "question": {"name": "机缘"},
                    "cards": [
                        {"positionName": "前因", "title": "掌天瓶", "orientation": "正位"},
                        {"positionName": "今时", "title": "天机阁", "orientation": "逆位"},
                        {"positionName": "后果", "title": "韩立", "orientation": "正位"},
                    ],
                }
                return {"record": self.record, "reward": {"tianjiTrace": 1}}

            async def fate_cards_interpret(self, identity):
                self.calls.append(("interpret", identity))
                self.record["aiReading"] = {"source": "ai", "overview": "守中见机"}
                return {"record": self.record}

            async def fate_cards_choose(self, identity, choice_key):
                self.calls.append(("choose", identity, choice_key))
                self.record["choiceKey"] = choice_key
                self.record["quest"] = {
                    "title": "避劫·藏锋",
                    "status": "active",
                    "metric": "wait_seconds",
                    "target": 180,
                    "progress": 180,
                    "unit": "秒",
                    "canSettle": False,
                }
                return {"record": self.record}

            async def fate_cards_settle(self, identity):
                self.calls.append(("settle", identity))
                self.record["quest"].update(status="settled", canSettle=False)
                return {
                    "record": self.record,
                    "reward": {"tianjiTrace": 1, "kunwuPass": 1, "balance": 22},
                }

        transport = Transport()
        runner = MiniAppDailyActivities(actor, transport, "main", FakeLogger())
        runner.rng = FixedChoice("hide")

        with patch("miniapp_daily_activities.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(runner.run_fate_cards_identity("主魂", today="2026-08-18"))

        self.assertEqual(result, "completed")
        self.assertEqual(
            transport.calls,
            [
                ("start", "主魂"),
                ("draw", "主魂", "opportunity"),
                ("interpret", "主魂"),
                ("choose", "主魂", "hide"),
                ("start", "主魂"),
                ("settle", "主魂"),
            ],
        )
        state = actor.state
        self.assertEqual(state["miniapp_fate_cards_last_date"], "2026-08-18")
        self.assertEqual(state["miniapp_fate_cards_last_choice"], "hide")
        self.assertIn("昆吾通行令 +1", state["miniapp_fate_cards_last_result"])
        self.assertEqual(actor.rewards[0][1], ".天机命脉")
        combined = "\n".join(runner.log.info_messages)
        self.assertIn("OUT [Mini App | 主魂]:\n天机命脉（问天·机缘）", combined)
        self.assertIn("命择：藏锋避劫", combined)

    def test_fate_cards_accept_forces_exit_restarts_retreat_then_settles(self):
        actor = FakeActor()

        class Transport:
            identity_player_ids = {"主魂": 100}

            def __init__(self):
                self.calls = []
                self.record = {
                    "question": {"name": "机缘"},
                    "cards": [{"positionName": "前因", "title": "掌天瓶"}],
                    "aiReading": {"overview": "顺势而为"},
                }

            async def fate_cards_start(self, identity):
                self.calls.append(("start", identity))
                return {"record": self.record}

            async def fate_cards_choose(self, identity, choice_key):
                self.calls.append(("choose", identity, choice_key))
                self.record["choiceKey"] = choice_key
                self.record["quest"] = {
                    "title": "承命·积修为",
                    "status": "active",
                    "target": 30,
                    "progress": 0,
                    "unit": "修为",
                    "canSettle": False,
                }
                return {"record": self.record}

            async def deep_seclusion_action(self, identity, action, log_operation=True):
                self.calls.append(("deep", identity, action, log_operation))
                if action == "start":
                    self.record["quest"].update(progress=30, canSettle=True)
                return {"actionResult": {"ok": True}}

            async def fate_cards_settle(self, identity):
                self.calls.append(("settle", identity))
                self.record["quest"].update(status="settled", canSettle=False)
                return {"record": self.record, "reward": {"tianjiTrace": 2}}

        transport = Transport()
        runner = MiniAppDailyActivities(actor, transport, "main", FakeLogger())
        runner.rng = FixedChoice("accept")

        with patch("miniapp_daily_activities.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(runner.run_fate_cards_identity("主魂", today="2026-08-18"))

        self.assertEqual(result, "completed")
        self.assertEqual(
            [call for call in transport.calls if call[0] == "deep"],
            [
                ("deep", "主魂", "force", False),
                ("deep", "主魂", "start", False),
            ],
        )
        self.assertEqual(actor.state["miniapp_fate_cards_accept_prepared_date"], "2026-08-18")
        self.assertEqual(actor.state["miniapp_fate_cards_last_choice"], "accept")
        self.assertIn("命择：顺势承命", actor.state["miniapp_fate_cards_last_result"])

    def test_fate_cards_wait_uses_server_progress_and_started_time(self):
        now = datetime.fromisoformat("2026-08-18T10:02:00+08:00")
        quest = {
            "target": 180,
            "progress": 10,
            "startedAt": "2026-08-18T10:00:00+08:00",
            "canSettle": False,
        }

        self.assertEqual(fate_cards_wait_seconds(quest, now=now), 60)
        self.assertEqual(fate_cards_wait_seconds({**quest, "canSettle": True}, now=now), 0)

    def test_fate_cards_accept_preserves_non_deep_meditation_mode(self):
        actor = FakeActor()
        actor.state.update({
            "miniapp_fate_cards_accept_prepared_date": "2026-08-18",
            "miniapp_fate_cards_stage": "quest_pending",
        })

        class Transport:
            identity_player_ids = {"主魂": 100}

            def __init__(self):
                self.calls = []

            async def deep_seclusion_action(self, identity, action, log_operation=True):
                self.calls.append(("deep", action))
                raise MiniAppBeastError("deep_not_active")

            async def command(self, command, identity="主魂", log_operation=True):
                self.calls.append(("command", command))
                payload = {"actionResult": {"ok": True, "rawMessage": "修为增加 100"}}
                return MiniAppCommandResponse("修为增加 100", payload)

        transport = Transport()
        runner = MiniAppDailyActivities(actor, transport, "main", FakeLogger())

        result = asyncio.run(runner._prepare_fate_cards_accept("主魂", "2026-08-18"))

        self.assertTrue(result)
        self.assertEqual(
            transport.calls,
            [("deep", "force"), ("command", ".闭关修炼")],
        )
        self.assertEqual(actor.state["miniapp_fate_cards_stage"], "accept_cultivated")

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
