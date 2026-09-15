"""Regressions from the 2026-09-15 round, using clocks and recorded replies only."""

import asyncio
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.test_world_boss_features import DummyMessage, FakeActor
from tests.test_world_boss_reveal_protocol import RevealClock, build_monitor
from world_boss_features import WorldBossMonitor, extract_world_boss_entry


class WorldBossRoundReviewTests(unittest.TestCase):
    def test_task_start_delay_does_not_add_another_relative_sleep(self):
        async def run(stall):
            clock = RevealClock()
            monitor = WorldBossMonitor(FakeActor(), "waaiging", monotonic=clock.monotonic)
            monitor._boss_defeat_marker = Path(monitor.actor.state_file).with_name("stop.json")
            loop = asyncio.get_running_loop()
            stop = asyncio.Event()
            target = clock.now + .822  # Waaiging #14: charge receipt to release.

            def block_loop():
                clock.now += stall

            async def wait():
                # This callback runs after _sleep_until schedules its wait, but
                # before any child task gets its first turn. No wall-clock sleep.
                loop.call_soon(block_loop)
                return await monitor._sleep_until(target)

            with patch.object(loop, "time", clock.monotonic), \
                    patch.object(monitor, "_boss_stop_requested", return_value=False), \
                    patch.object(monitor, "_wait_for_boss_stop", new=stop.wait):
                task = asyncio.create_task(wait())
                try:
                    for _ in range(12):
                        await asyncio.sleep(0)
                    clock.now = max(clock.now, target)
                    for _ in range(12):
                        await asyncio.sleep(0)
                    self.assertTrue(task.done(), "The original deadline already passed")
                    self.assertTrue(task.result())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await monitor.stop()

        for stall in (.421, 1.875):
            with self.subTest(stall_seconds=stall):
                asyncio.run(run(stall))

    def test_dead_boss_receipt_is_separate_from_a_real_final_hit(self):
        async def run(hit, expected_hits, expected_ended):
            clock, calls, proofs = RevealClock(), [], []

            async def post(origin, path, payload, timeout):
                calls.append(path.rsplit("/", 1)[-1])
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "fixture-ticket"}
                if path.endswith("/hit"):
                    return {"hit": hit}
                self.assertTrue(path.endswith("/finish"))
                proofs.append(deepcopy(payload["bossProof"]))
                # A legacy settlement without counts must also be correct.
                return {"result": {"grade": "甲等", "score": 100}}

            monitor = build_monitor(clock, post)
            try:
                outcome = await monitor._fight(
                    extract_world_boss_entry(DummyMessage()), "init", "session",
                    {"challenge": {"challengeId": "fixture", "windows": [
                        {"id": "w1", "centerMs": 3000, "hitMs": 620, "perfectMs": 210},
                    ]}},
                )
                self.assertEqual(outcome["hit_count"], expected_hits)
                self.assertEqual(outcome["ended_hit_count"], expected_ended)
                self.assertEqual(outcome["failed_hit_count"], 0)
                self.assertEqual(outcome["skipped_hit_count"], 0)
                self.assertEqual(outcome["hit_error_counts"], {})
                self.assertEqual(calls, ["begin", "charge-start", "hit", "finish"])
                self.assertEqual(len(proofs[0]["actions"]), 1, "Keep the real release in the proof")
                self.assertEqual(proofs[0]["clientStats"]["hits"], 1)
                if expected_ended:
                    self.assertEqual(outcome["diagnostics"]["hits"][0]["server_status"], "ended")
                    self.assertEqual(outcome["perfect_count"], 0)
                    self.assertIn("Boss结束回执 1 次", monitor._outcome_summary(outcome))
            finally:
                await monitor.stop()

        cases = (
            ({"bossHp": 0, "damageYi": 0, "perfect": False}, 0, 1),
            ({"bossHp": 0, "damageYi": 10, "perfect": True, "deltaMs": 4}, 1, 0),
            ({"bossHp": 0, "damageYi": 0, "perfect": False, "deltaMs": 4, "holdMs": 1000}, 1, 0),
        )
        for hit, hits, ended in cases:
            with self.subTest(hit=hit):
                asyncio.run(run(hit, hits, ended))

    def test_finish_counts_are_authoritative_without_rewriting_actions_or_history(self):
        async def run(result, expected, source):
            calls = []

            async def post(origin, path, payload, timeout):
                self.assertTrue(path.endswith("/finish"))
                calls.append(deepcopy(payload["bossProof"]))
                return {"result": result}

            monitor = build_monitor(RevealClock(), post)
            history = [{"status": "completed", "hit_count": 16}]
            monitor.actor.state["world_boss_events"] = deepcopy(history)
            proof = {"durationMs": 89000, "actions": [{"t": 2204, "holdMs": 987}],
                     "clientStats": {"hits": 16, "perfects": 16}}
            checkpoint = {"session_token": "session", "init_data": "init", "proof": deepcopy(proof),
                          "outcome": {"hit_count": 16, "perfect_count": 16, "player_hp": 100,
                                      "window_count": 16, "local_perfect_count": 16,
                                      "damage_yi_total": 598603523, "diagnostics": {}}}
            try:
                outcome = await monitor._submit_finish(extract_world_boss_entry(DummyMessage()), checkpoint)
                self.assertEqual((outcome["hit_count"], outcome["perfect_count"]), expected)
                self.assertEqual(outcome["damage_yi_total"], 598603523)
                self.assertEqual(outcome["local_perfect_count"], 16)
                self.assertEqual(calls, [proof])
                self.assertEqual(checkpoint["proof"], proof)
                self.assertEqual(monitor.actor.state["world_boss_events"], history)
                counts = outcome["diagnostics"]["finish"]["counts"]
                self.assertEqual(counts["hits_source"], source)
                self.assertEqual(counts["reported_hits"], 16)
            finally:
                await monitor.stop()

        cases = (
            ({"hits": 15, "perfects": 14, "realtime_hit_count": 15}, (15, 14), "finish.hits"),
            ({"realtime_hit_count": 15, "perfects": 14}, (15, 14), "finish.realtime_hit_count"),
            ({"hits": 0, "perfects": 0}, (0, 0), "finish.hits"),
            ({}, (16, 16), "realtime_responses"),
            ({"hits": 15}, (15, 15), "finish.hits"),
        )
        for result, expected, source in cases:
            with self.subTest(result=result):
                asyncio.run(run(result, expected, source))

    def test_invalid_finish_counts_do_not_overwrite_recorded_counts(self):
        async def run(value):
            async def post(*args):
                return {"result": {"hits": value, "perfects": value}}

            monitor = build_monitor(RevealClock(), post)
            checkpoint = {"session_token": "session", "init_data": "init", "proof": {"durationMs": 1000},
                          "outcome": {"hit_count": 15, "perfect_count": 14, "window_count": 16,
                                      "player_hp": 84, "diagnostics": {}}}
            try:
                outcome = await monitor._submit_finish(extract_world_boss_entry(DummyMessage()), checkpoint)
                self.assertEqual((outcome["hit_count"], outcome["perfect_count"]), (15, 14))
            finally:
                await monitor.stop()

        for value in (True, -1, 1.5, "unknown", float("inf"), 17):
            with self.subTest(value=value):
                asyncio.run(run(value))

    def test_slow_charge_feedback_cannot_push_the_next_hold_to_the_limit(self):
        async def run():
            clock, pressed, calls = RevealClock(101.217), {}, []

            async def sleep(seconds):
                clock.now += seconds + .001

            async def post(origin, path, payload, timeout):
                sequence = int(payload["windowId"])
                calls.append((sequence, path.rsplit("/", 1)[-1]))
                if path.endswith("/charge-start"):
                    pressed[sequence] = clock.now
                    clock.now += .981 if sequence == 1 else .190
                    return {"chargeTicket": "fixture-ticket"}
                self.assertTrue(path.endswith("/hit"))
                self.assertEqual(payload["holdMs"], round((clock.now - pressed[sequence]) * 1000))
                clock.now += .192
                return {"hit": {"damageYi": 1, "perfect": sequence > 1, "deltaMs": 19.8,
                                "holdMs": 201.1 if sequence == 1 else payload["holdMs"] + .3}}

            monitor = build_monitor(clock, post, account="xiaohao")
            monitor.sleep = sleep
            try:
                first = await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), "init", "session", "challenge", 100,
                    {"id": "1", "centerMs": 2308, "hitMs": 620, "perfectMs": 210}, 1, 95,
                )
                self.assertFalse(first["accepted_perfect"], "Do not alter the recorded verdict")
                self.assertEqual(first["diagnostic"]["hold_ms"], 987)
                self.assertEqual(monitor._planned_hold_ms(), 1000)
                self.assertEqual(first["diagnostic"]["hold_feedback"]["reason"], "slow_charge_request")
                second = await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), "init", "session", "challenge", 100,
                    {"id": "2", "centerMs": 7513, "hitMs": 620, "perfectMs": 210}, 2, 95,
                )
                self.assertEqual(second["diagnostic"]["planned_hold_ms"], 1000)
                self.assertTrue(second["diagnostic"]["hold_feedback"]["update_applied"])
                self.assertEqual(calls, [(1, "charge-start"), (1, "hit"), (2, "charge-start"), (2, "hit")])
            finally:
                await monitor.stop()

        asyncio.run(run())

    def test_slow_hit_feedback_is_ignored_but_stable_skew_still_adapts(self):
        async def run(hit_rtt, expected_update):
            clock = RevealClock()

            async def post(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    clock.now += .193
                    return {"chargeTicket": "fixture-ticket"}
                clock.now += hit_rtt / 1000
                return {"hit": {"damageYi": 1, "perfect": True, "deltaMs": 20,
                                "holdMs": payload["holdMs"] + 200}}

            monitor = build_monitor(clock, post)
            try:
                result = await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), "init", "session", "challenge", 100,
                    {"id": "1", "centerMs": 3000, "hitMs": 620, "perfectMs": 210}, 1, 95,
                )
                self.assertEqual(monitor._planned_hold_ms(), 930 if expected_update else 1000)
                self.assertEqual(result["diagnostic"]["hold_feedback"]["update_applied"], expected_update)
            finally:
                await monitor.stop()

        for hit_rtt in (192, 438, 533):
            with self.subTest(hit_rtt=hit_rtt):
                asyncio.run(run(hit_rtt, hit_rtt == 192))
