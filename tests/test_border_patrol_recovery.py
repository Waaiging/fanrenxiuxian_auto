"""A missing patrol receipt must be reconciled before starting another trip."""
import asyncio
import copy
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from cultivator_xiaohao import CultivatorXiaoHao


START = ".灵兽巡边 青蛟 袭营"
ACTIVE = "【巡边状态】灵兽【青蛟】正在边境巡行，剩余 74分钟。"


class BorderPatrolRecoveryTests(unittest.TestCase):
    def actor(self, responses):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {"beasts_cache": [
            {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 95},
        ]}
        actor.save_state = Mock()
        actor.send_and_wait_feedback = AsyncMock(side_effect=responses)
        return actor

    def commands(self, actor):
        return [call.args[0] for call in actor.send_and_wait_feedback.await_args_list]

    def test_timeout_queries_status_without_blind_resend(self):
        actor = self.actor([None, ACTIVE])
        self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(self.commands(actor), [START, ".巡边状态"])
        options = actor.send_and_wait_feedback.await_args_list[0].kwargs
        self.assertFalse(options["retry_on_timeout"])
        self.assertEqual(options["max_retries"], 0)
        self.assertEqual(actor.state["beast_border_patrol_name"], "青蛟")
        self.assertFalse(actor.state.get("beast_border_patrol_reconcile_pending"))
        due = datetime.strptime(actor.state["next_beast_border_patrol_time"], "%Y-%m-%d %H:%M:%S")
        self.assertGreater((due - datetime.now()).total_seconds(), 70 * 60)

    def test_unconfirmed_status_keeps_retry_as_query_only(self):
        actor = self.actor([None, None, None])
        self.assertFalse(asyncio.run(actor.run_beast_border_patrol()))
        self.assertFalse(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(self.commands(actor), [START, ".巡边状态", ".巡边状态"])
        self.assertTrue(actor.state["beast_border_patrol_reconcile_pending"])
        self.assertFalse(actor.state.get("beast_border_patrol_name"))

    def test_restart_after_interrupted_start_queries_before_any_new_trip(self):
        actor = self.actor([RuntimeError("connection lost")])
        with self.assertRaises(RuntimeError):
            asyncio.run(actor.run_beast_border_patrol())
        restored = self.actor([ACTIVE])
        restored.state = copy.deepcopy(actor.state)
        self.assertTrue(asyncio.run(restored.run_beast_border_patrol()))
        self.assertEqual(self.commands(restored), [".巡边状态"])

    def test_clear_status_defers_restart_until_next_scheduler_tick(self):
        actor = self.actor([None, "暂无灵兽巡边。"])
        self.assertFalse(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(self.commands(actor), [START, ".巡边状态"])
        self.assertFalse(actor.state.get("beast_border_patrol_reconcile_pending"))
        self.assertTrue(actor.state["next_beast_border_patrol_time"])

    def test_confirmed_start_does_not_add_status_queries(self):
        actor = self.actor(["灵兽【青蛟】领命前往边境巡行，预计 75分钟 后归来。"])
        self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(self.commands(actor), [START])
        self.assertFalse(actor.state.get("beast_border_patrol_reconcile_pending"))


if __name__ == "__main__":
    unittest.main()
