"""Tests for the 2026-08-26 world-boss protocol.

The server stopped shipping a window timetable in ``/start``: windows are now
revealed one at a time through ``/window`` while the battle runs, and every
``/hit`` must carry a ``chargeTicket`` issued by ``/charge-start``. These tests
pin that flow, including the late-reveal trade-off between hold length and
timing accuracy.
"""

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from world_boss_features import (
    WORLD_BOSS_HOLD_MIN_MS,
    WORLD_BOSS_HOLD_MS,
    WorldBossMonitor,
    extract_world_boss_entry,
)

from tests.test_world_boss_features import DummyMessage, FakeActor, FakeTransport


class RevealClock:
    """Monotonic clock that only advances when the code awaits sleep()."""

    def __init__(self, start=100.0):
        self.now = start

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += max(0.0, float(seconds or 0))


def build_monitor(clock, post_json, account="main"):
    return WorldBossMonitor(
        FakeActor(avatars=["缘生子"]),
        account,
        logger=logging.getLogger("world-boss-reveal-test"),
        transport=FakeTransport(42),
        post_json=post_json,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        finish_grace_seconds=0,
    )


class WorldBossRevealProtocolTests(unittest.TestCase):
    def test_windows_revealed_one_at_a_time_are_all_struck(self):
        async def run():
            clock = RevealClock()
            calls = []
            # Three windows on the historical 5713ms cadence.
            revealed = [
                {"id": "w_1", "centerMs": 1800, "hitMs": 620, "perfectMs": 210},
                {"id": "w_2", "centerMs": 7513, "hitMs": 620, "perfectMs": 210},
                {"id": "w_3", "centerMs": 13226, "hitMs": 620, "perfectMs": 210},
            ]

            async def post_json(origin, path, payload, timeout):
                calls.append((path.rsplit("/", 1)[-1], dict(payload)))
                if path.endswith("/start"):
                    return {
                        "sessionToken": "session_fixture",
                        "boss": {"actionsUsed": 0, "actionsRemaining": 1},
                        "player": {"maxHp": 100},
                        "challenge": {
                            "challengeId": "challenge_fixture",
                            "windowCount": 3,
                            "windows": [],
                            "attacks": [
                                {"id": "a_1", "name": "七焰扇", "width": 58.0}
                            ],
                        },
                    }
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/window"):
                    after = str(payload.get("afterWindowId") or "")
                    index = 0
                    if after:
                        index = [w["id"] for w in revealed].index(after) + 1
                    if index >= len(revealed):
                        return {"done": True, "windowCount": 3}
                    return {
                        "window": revealed[index],
                        "windowCount": 3,
                        "done": index == len(revealed) - 1,
                    }
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "ticket_" + str(payload.get("windowId"))}
                if path.endswith("/hit"):
                    return {"hit": {"damageYi": 500, "perfect": True}}
                if path.endswith("/finish"):
                    return {"result": {"grade": "甲等", "score": 100, "player_hp": 100}}
                raise AssertionError(path)

            monitor = build_monitor(clock, post_json)
            entry = extract_world_boss_entry(DummyMessage())
            with patch(
                "world_boss_features.request_webview_init_data",
                new=AsyncMock(return_value="signed_init_data"),
            ):
                outcome = await monitor._participate(entry)

            self.assertEqual(outcome["window_count"], 3)
            self.assertEqual(outcome["hit_count"], 3)
            self.assertEqual(outcome["perfect_count"], 3)

            # Every hit carried the ticket minted for its own window.
            hits = [payload for name, payload in calls if name == "hit"]
            self.assertEqual(len(hits), 3)
            for index, hit in enumerate(hits):
                self.assertEqual(hit["windowId"], revealed[index]["id"])
                self.assertEqual(hit["chargeTicket"], "ticket_" + revealed[index]["id"])
                self.assertEqual(hit["holdMs"], WORLD_BOSS_HOLD_MS)

            # Paging used afterWindowId rather than refetching from the start.
            window_calls = [payload for name, payload in calls if name == "window"]
            self.assertEqual(
                [str(item.get("afterWindowId") or "") for item in window_calls],
                ["", "w_1", "w_2"],
            )

            reveal = outcome["diagnostics"]["window_reveal"]
            self.assertEqual(reveal["mode"], "reveal")
            self.assertEqual(reveal["revealed_count"], 3)
            self.assertEqual(reveal["error_count"], 0)
            self.assertGreater(reveal["min_lead_ms"], 0)

        asyncio.run(run())

    def test_not_ready_is_treated_as_keep_waiting(self):
        async def run():
            clock = RevealClock()
            window_attempts = []

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/window"):
                    window_attempts.append(str(payload.get("afterWindowId") or ""))
                    if len(window_attempts) < 4:
                        raise MiniAppBeastError("boss_window_not_ready", 409)
                    if len(window_attempts) == 4:
                        return {
                            "window": {
                                "id": "w_1",
                                "centerMs": 4000,
                                "hitMs": 620,
                                "perfectMs": 210,
                            },
                            "windowCount": 1,
                        }
                    return {"done": True, "windowCount": 1}
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "ticket"}
                if path.endswith("/hit"):
                    return {"hit": {"damageYi": 7, "perfect": True}}
                raise AssertionError(path)

            monitor = build_monitor(clock, post_json)
            queue: asyncio.Queue = asyncio.Queue()
            reveal_log = []
            await monitor._reveal_windows(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                queue,
                reveal_log,
                1,
            )

            # Four polls: three "not ready", then the reveal.
            self.assertEqual(len(window_attempts), 4)
            first = await queue.get()
            self.assertEqual(first["id"], "w_1")
            self.assertIsNone(await queue.get())
            # A "not ready" reply is normal pacing, not an error worth recording.
            self.assertEqual(
                [item["status"] for item in reveal_log],
                ["revealed"],
            )

        asyncio.run(run())

    def test_hit_is_not_sent_without_a_charge_ticket(self):
        async def run():
            clock = RevealClock()
            calls = []

            async def post_json(origin, path, payload, timeout):
                calls.append(path.rsplit("/", 1)[-1])
                if path.endswith("/charge-start"):
                    raise MiniAppBeastError("boss_charge_rejected", 409)
                raise AssertionError("hit must not be attempted: " + path)

            monitor = build_monitor(clock, post_json)
            result = await monitor._hit_window(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                {"id": "w_1", "centerMs": 3000, "hitMs": 620, "perfectMs": 210},
                1,
                0,
            )

            self.assertEqual(calls, ["charge-start"])
            self.assertFalse(result["ok"])
            self.assertFalse(result["perfect"])
            self.assertEqual(result["error"], "boss_charge_rejected")
            self.assertEqual(result["diagnostic"]["server_status"], "not_sent")
            self.assertFalse(result["diagnostic"]["charge"]["granted"])

        asyncio.run(run())

    def test_late_reveal_delays_strike_only_while_perfect_stays_reachable(self):
        async def run():
            clock = RevealClock()
            sent = {}

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "ticket"}
                if path.endswith("/hit"):
                    sent.update(payload)
                    return {"hit": {"damageYi": 1, "perfect": True}}
                raise AssertionError(path)

            monitor = build_monitor(clock, post_json, account="waaiging")
            # Window centre is only 300ms away: a full 1200ms hold cannot fit, but
            # a 520ms hold still lands inside the 620ms perfect tolerance.
            result = await monitor._hit_window(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                {"id": "w_1", "centerMs": 300, "hitMs": 900, "perfectMs": 620},
                1,
                0,
            )

            self.assertTrue(result["ok"])
            # Strike was pushed out to reach the minimum creditable hold.
            self.assertEqual(sent["holdMs"], WORLD_BOSS_HOLD_MIN_MS)
            self.assertTrue(result["perfect"])
            diagnostic = result["diagnostic"]
            self.assertEqual(diagnostic["ideal_target_ms"], 295)
            self.assertEqual(diagnostic["target_ms"], WORLD_BOSS_HOLD_MIN_MS)

        asyncio.run(run())

    def test_late_reveal_keeps_accuracy_when_perfect_is_unreachable(self):
        async def run():
            clock = RevealClock()
            sent = {}

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "ticket"}
                if path.endswith("/hit"):
                    sent.update(payload)
                    return {"hit": {"damageYi": 1}}
                raise AssertionError(path)

            monitor = build_monitor(clock, post_json)
            # Tight perfect window: delaying to reach a creditable hold would push
            # the strike past the perfect tolerance, so accuracy must win instead.
            result = await monitor._hit_window(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                {"id": "w_1", "centerMs": 200, "hitMs": 620, "perfectMs": 210},
                1,
                0,
            )

            self.assertTrue(result["ok"])
            diagnostic = result["diagnostic"]
            # Strike stayed at the ideal moment rather than chasing hold length.
            self.assertEqual(diagnostic["target_ms"], diagnostic["ideal_target_ms"])
            self.assertLess(sent["holdMs"], WORLD_BOSS_HOLD_MIN_MS)

        asyncio.run(run())

    def test_a_stalled_hit_does_not_delay_the_next_window_charge(self):
        """A slow /hit must not push the next window's charge past its moment.

        /hit retries twice with its own timeout, so a single slow response used to
        be able to eat the following window entirely. The first hit here blocks
        until the *second* window's charge has been observed: if strikes were
        awaited one after another this would never happen and the wait times out.
        """

        async def run():
            clock = RevealClock()
            second_charge_seen = asyncio.Event()
            revealed = [
                {"id": "w_1", "centerMs": 1800, "hitMs": 620, "perfectMs": 210},
                {"id": "w_2", "centerMs": 7513, "hitMs": 620, "perfectMs": 210},
            ]

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/start"):
                    return {
                        "sessionToken": "session_fixture",
                        "boss": {"actionsUsed": 0, "actionsRemaining": 1},
                        "player": {"maxHp": 100},
                        "challenge": {
                            "challengeId": "challenge_fixture",
                            "windowCount": 2,
                            "windows": [],
                        },
                    }
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/window"):
                    after = str(payload.get("afterWindowId") or "")
                    index = 0
                    if after:
                        index = [w["id"] for w in revealed].index(after) + 1
                    if index >= len(revealed):
                        return {"done": True, "windowCount": 2}
                    return {"window": revealed[index], "windowCount": 2}
                if path.endswith("/charge-start"):
                    if str(payload.get("windowId")) == "w_2":
                        second_charge_seen.set()
                    return {"chargeTicket": "ticket"}
                if path.endswith("/hit"):
                    if str(payload.get("windowId")) == "w_1":
                        await second_charge_seen.wait()
                    return {"hit": {"damageYi": 3, "perfect": True}}
                if path.endswith("/finish"):
                    return {"result": {"grade": "甲等", "score": 100, "player_hp": 100}}
                raise AssertionError(path)

            monitor = build_monitor(clock, post_json)
            entry = extract_world_boss_entry(DummyMessage())
            with patch(
                "world_boss_features.request_webview_init_data",
                new=AsyncMock(return_value="signed_init_data"),
            ):
                outcome = await asyncio.wait_for(monitor._participate(entry), timeout=5)

            self.assertEqual(outcome["hit_count"], 2)
            self.assertTrue(second_charge_seen.is_set())

        asyncio.run(run())

    def test_transient_errors_do_not_consume_the_remaining_window_budget(self):
        async def run():
            clock = RevealClock()
            revealed = [
                {"id": "w_1", "centerMs": 1800, "hitMs": 620, "perfectMs": 210},
                {"id": "w_2", "centerMs": 7513, "hitMs": 620, "perfectMs": 210},
                {"id": "w_3", "centerMs": 13226, "hitMs": 620, "perfectMs": 210},
            ]
            calls = {"n": 0}

            async def post_json(origin, path, payload, timeout):
                if not path.endswith("/window"):
                    raise AssertionError(path)
                calls["n"] += 1
                # Two real errors interleaved with the reveals.
                if calls["n"] in (2, 4):
                    raise MiniAppBeastError("server_error", 500)
                after = str(payload.get("afterWindowId") or "")
                index = 0
                if after:
                    index = [w["id"] for w in revealed].index(after) + 1
                if index >= len(revealed):
                    return {"done": True, "windowCount": 3}
                return {"window": revealed[index], "windowCount": 3}

            monitor = build_monitor(clock, post_json)
            queue: asyncio.Queue = asyncio.Queue()
            reveal_log = []
            await monitor._reveal_windows(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                queue,
                reveal_log,
                3,
            )

            drained = []
            while True:
                item = await queue.get()
                if item is None:
                    break
                drained.append(item["id"])

            # All three windows still arrive despite the two errors.
            self.assertEqual(drained, ["w_1", "w_2", "w_3"])
            self.assertEqual(
                sum(1 for item in reveal_log if item["status"] == "revealed"), 3
            )
            self.assertEqual(
                sum(1 for item in reveal_log if item["status"] == "error"), 2
            )

        asyncio.run(run())

    def test_reveal_stops_and_reports_when_no_window_ever_arrives(self):
        async def run():
            clock = RevealClock()
            polls = []

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/start"):
                    return {
                        "sessionToken": "session_fixture",
                        "boss": {"actionsUsed": 0, "actionsRemaining": 1},
                        "player": {"maxHp": 100},
                        "challenge": {
                            "challengeId": "challenge_fixture",
                            "windowCount": 16,
                            "windows": [],
                        },
                    }
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/window"):
                    polls.append(1)
                    raise MiniAppBeastError("boss_window_not_ready", 409)
                raise AssertionError(path)

            monitor = build_monitor(clock, post_json)
            entry = extract_world_boss_entry(DummyMessage())
            with patch(
                "world_boss_features.request_webview_init_data",
                new=AsyncMock(return_value="signed_init_data"),
            ):
                with self.assertRaises(MiniAppBeastError) as caught:
                    await monitor._participate(entry)

            self.assertEqual(caught.exception.code, "boss_windows_invalid")
            details = caught.exception.details
            self.assertIn("begin_response", details)
            self.assertIn("window_reveal", details)
            # The stall guard stops the loop instead of holding the worker for the
            # whole battle duration.
            self.assertGreater(len(polls), 5)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
