import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import intelligent_cultivator as core
from miniapp_dwelling import small_world_incense_plan


NOW = datetime(2026, 9, 8, 7, 0, 0)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def snapshot(stock=62, pending=0.5, hourly=4, cooldown=0):
    return {"account": {"smallWorld": {
        "hasWorld": True,
        "summary": {
            "incensePoints": stock, "uncollectedIncense": pending,
            "collectableIncense": int(pending), "hourlyIncense": hourly,
        },
        "actions": {
            "sootheCost": 300, "canCollect": pending >= 1,
            "edictRemainingSeconds": cooldown,
        },
    }}}


class SmallWorldSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = patch.object(core, "datetime", Clock)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.logger = Mock()
        self.logs = patch.object(core, "log", self.logger)
        self.logs.start()
        self.addCleanup(self.logs.stop)
        self.actor = core.Cultivator.__new__(core.Cultivator)
        self.actor.state = {"small_world_calamity_pending": True}
        self.actor.save_state = Mock()
        self.actor.active_atomic_task = None
        self.actor.should_wait_for_atomic_task = lambda command: False
        self.actor.dashboard_command_paused = lambda *args: False
        self.actor.small_world_calamity_event = asyncio.Event()
        self.transport = SimpleNamespace(
            small_world_snapshot=AsyncMock(return_value=snapshot()),
            small_world_action=AsyncMock(return_value={"actionResult": {"ok": True, "message": "信徒已安定"}}),
        )
        self.actor._miniapp_command_router = SimpleNamespace(transport=self.transport)

    def next_after(self, seconds):
        self.assertEqual(
            self.actor.state["next_small_world_calamity_time"],
            (NOW + timedelta(seconds=seconds)).strftime(core.TIME_FORMAT),
        )

    async def test_shortage_waits_for_fractional_production_without_small_collections(self):
        self.transport.small_world_snapshot.return_value = snapshot(pending=1.5)
        self.assertFalse(await self.actor.execute_small_world_calamity_once())
        self.next_after(212850 + 2)  # (300 - 62 - 1.5) / 4 hours.
        self.transport.small_world_action.assert_not_awaited()
        plan = self.actor.state["small_world_calamity_resource_plan"]
        self.assertEqual(plan["deficit"], 238)
        self.assertEqual(plan["production_deficit"], 236.5)
        self.assertIn("4/小时", self.actor.state["small_world_calamity_last_response"])
        self.logger.error.assert_not_called()
        self.assertIsNone(self.actor.active_atomic_task)

    async def test_edict_and_resource_deadlines_use_the_later_time(self):
        self.transport.small_world_snapshot.return_value = snapshot(stock=299, pending=0, hourly=3600, cooldown=7200)
        await self.actor.execute_small_world_calamity_once()
        self.next_after(7200)
        self.transport.small_world_action.assert_not_awaited()
        self.assertEqual(self.actor.state["next_miracle_preach_time"], self.actor.state["next_small_world_calamity_time"])

    async def test_zero_or_unknown_production_does_not_invent_an_eta(self):
        for hourly in (0, None, "NaN", "Infinity"):
            with self.subTest(hourly=hourly):
                self.transport.small_world_snapshot.return_value = snapshot(hourly=hourly)
                await self.actor.execute_small_world_calamity_once()
                self.next_after(3600)
                self.assertEqual(self.actor.state["small_world_calamity_resource_plan"]["estimated_ready_at"], "")
        self.transport.small_world_action.assert_not_awaited()

    async def test_missing_stock_is_unknown_instead_of_zero(self):
        self.transport.small_world_snapshot.return_value = snapshot(stock=None)
        await self.actor.execute_small_world_calamity_once()
        self.next_after(3600)
        self.assertIsNone(self.actor.state["small_world_calamity_resource_plan"]["stock"])
        self.assertEqual(self.actor.state["small_world_calamity_last_status"], "incense_unavailable")
        self.transport.small_world_action.assert_not_awaited()

    async def test_collect_message_without_summary_is_followed_by_fresh_stock(self):
        self.transport.small_world_snapshot.side_effect = [snapshot(stock=62, pending=238), snapshot(stock=300, pending=0)]
        self.transport.small_world_action.side_effect = [
            {"actionResult": {"ok": True, "message": "收割 238 点香火，当前香火库存: 300"}},
            {"actionResult": {"ok": True, "message": "信徒已安定"}},
        ]
        self.assertTrue(await self.actor.execute_small_world_calamity_once())
        self.assertEqual([call.args for call in self.transport.small_world_action.await_args_list], [("主魂", "collect"), ("主魂", "soothe")])
        self.assertFalse(self.actor.state["small_world_calamity_pending"])
        self.assertEqual(self.actor.state["small_world_calamity_resource_plan"]["stock"], 300)

    async def test_missing_post_collect_snapshot_never_attempts_soothing(self):
        self.transport.small_world_snapshot.side_effect = [snapshot(stock=62, pending=238), {}]
        await self.actor.execute_small_world_calamity_once()
        self.transport.small_world_action.assert_awaited_once_with("主魂", "collect")
        self.next_after(3600)
        self.assertIsNone(self.actor.state["small_world_calamity_resource_plan"]["stock"])

    async def test_ready_stock_does_not_require_a_production_rate_or_collect(self):
        self.transport.small_world_snapshot.return_value = snapshot(stock=300, pending=0, hourly=None)
        self.assertTrue(await self.actor.execute_small_world_calamity_once())
        self.transport.small_world_action.assert_awaited_once_with("主魂", "soothe")

    async def test_server_shortage_after_snapshot_refreshes_and_recalculates(self):
        self.transport.small_world_snapshot.side_effect = [snapshot(stock=300), snapshot(stock=100, pending=0, hourly=20)]
        self.transport.small_world_action.return_value = {"actionResult": {"ok": False, "error": "insufficient_incense", "message": "香火库存不足"}}
        self.assertFalse(await self.actor.execute_small_world_calamity_once())
        self.next_after(36002)
        self.transport.small_world_action.assert_awaited_once_with("主魂", "soothe")
        self.logger.error.assert_not_called()

    async def test_top_level_shortage_is_not_mistaken_for_completed_soothing(self):
        self.transport.small_world_snapshot.side_effect = [snapshot(stock=300), snapshot(stock=100, pending=0, hourly=20)]
        self.transport.small_world_action.return_value = {"ok": False, "error": "insufficient_incense", "message": "香火库存不足"}
        self.assertFalse(await self.actor.execute_small_world_calamity_once())
        self.next_after(36002)
        self.assertTrue(self.actor.state["small_world_calamity_pending"])

    async def test_prayer_snapshot_replans_when_stock_or_production_changes(self):
        self.actor.defer_small_world_calamity_for_resources(snapshot())
        previous = self.actor.state["next_small_world_calamity_time"]
        self.actor.record_small_world_miniapp_state(snapshot(stock=290, pending=0, hourly=20))
        self.next_after(1802)
        self.assertLess(self.actor.state["next_small_world_calamity_time"], previous)
        self.assertTrue(self.actor.small_world_calamity_event.is_set())

    async def test_partial_prayer_response_preserves_existing_resource_estimate(self):
        self.actor.defer_small_world_calamity_for_resources(snapshot())
        previous = self.actor.state["next_small_world_calamity_time"]
        self.actor.record_small_world_miniapp_state({"smallWorld": {"summary": {"incensePoints": 62}}})
        self.assertEqual(self.actor.state["next_small_world_calamity_time"], previous)

    async def test_restart_preserves_resource_wait_without_querying_or_sending(self):
        self.actor.defer_small_world_calamity_for_resources(snapshot())
        previous = self.actor.state["next_small_world_calamity_time"]
        self.actor.startup_done = asyncio.Event()
        self.actor.startup_done.set()
        self.actor.pause_event = asyncio.Event()
        self.actor.pause_event.set()
        self.actor.is_running = True

        async def stop_waiting(awaitable, timeout):
            awaitable.close()
            self.actor.is_running = False

        with patch.object(core.asyncio, "wait_for", side_effect=stop_waiting):
            await self.actor.run_small_world_calamity_loop()
        self.assertEqual(self.actor.state["next_small_world_calamity_time"], previous)
        self.transport.small_world_snapshot.assert_not_awaited()

    async def test_legacy_pending_retry_gets_one_fresh_resource_plan(self):
        self.actor.state["next_small_world_calamity_time"] = "2026-09-08 10:00:00"
        self.actor.startup_done = asyncio.Event()
        self.actor.startup_done.set()
        self.actor.pause_event = asyncio.Event()
        self.actor.pause_event.set()
        self.actor.is_running = True

        async def read_once(identity):
            self.actor.is_running = False
            return snapshot()

        self.transport.small_world_snapshot.side_effect = read_once
        await self.actor.run_small_world_calamity_loop()
        self.transport.small_world_snapshot.assert_awaited_once()
        self.next_after(213752)

    def test_numeric_strings_and_fractional_point_boundary(self):
        data = snapshot(stock="1,299", pending=0.985, hourly="36")
        data["account"]["smallWorld"]["actions"]["sootheCost"] = "1,300"
        plan = small_world_incense_plan(data)
        self.assertEqual(plan["production_wait_seconds"], 2)  # ceil avoids an early collect.
        self.assertEqual(plan["collectable"], 0)


if __name__ == "__main__":
    unittest.main()
