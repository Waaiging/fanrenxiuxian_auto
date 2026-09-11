"""Arrival and ticket-age regressions from the 2026-09-11 Qingyuanzi round.

All requests below use an isolated model or a loopback HTTP server. Verdicts
are calculated from the model server's clock, never from reported elapsedMs.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from miniapp_beast import (MiniAppBeastError, MiniAppCircuitOpenError, MiniAppCircuitPermit,
                           MiniAppReadDeadlineError, MiniAppTransportCircuitBreaker)
from tests.test_world_boss_features import DummyMessage, FakeActor
from tests.test_world_boss_reveal_protocol import RevealClock, build_monitor
from tests.test_world_boss_transport import HealthyCircuit
from world_boss_features import WorldBossMonitor, _PersistentWorldBossJsonClient, extract_world_boss_entry


class WorldBossTimingTests(unittest.TestCase):
    def test_main_sixteen_window_timing_replay_recovers_from_begin_bias(self):
        # The recorded reveal/HTTP timings are fixed. One-way delays below are
        # reconstructed from the early server deltas and ticket ages under a
        # 200 ms route model; this is a counterfactual replay, not a new result.
        # (center, reveal elapsed, charge outbound, hit outbound, charge RTT, hit RTT)
        rows = (
            (1946, 844, 106.9, 231.5, 194, 358),
            (9890, 8629, 115.2, 122.1, 193, 197),
            (17636, 16719, 118.5, 114.4, 193, 196),
            (23566, 22245, 549.4, 117.9, 674, 198),
            (26727, 25243, 114.6, 388.1, 193, 512),
            (35159, 33768, 115.5, 120.2, 190, 215),
            (38191, 39320, None, None, 193, None),
            (43960, 42531, 114.6, 113.4, 194, 193),
            (46855, 45975, 115.2, 113.5, 191, 195),
            (50474, 49469, 114.6, 121.4, 190, 198),
            (56388, 55285, 114.9, 114.5, 189, 194),
            (64857, 63331, 113.5, 121.4, 191, 200),
            (73001, 71924, 164.6, 112.8, 244, 192),
            (76115, 74981, 111.1, 121.9, 191, 201),
            (79563, 78125, 114.2, 113.8, 192, 199),
            (86104, 84962, 116.8, 117.1, 192, 194),
        )
        server_start = 100 + (731 - 200) / 2000

        async def run(battle_start, lead):
            clock = RevealClock()
            state, results = {}, []

            async def request(origin, path, payload, timeout):
                row = rows[int(payload["windowId"]) - 1]
                center, _, charge_out, hit_out, charge_rtt, hit_rtt = row
                if path.endswith("/charge-start"):
                    if charge_out is None:
                        clock.now += charge_rtt / 1000
                        raise MiniAppBeastError("boss_window_expired", 409)
                    self.assertTrue(0 <= charge_out <= charge_rtt)
                    state["issued"] = clock.now + charge_out / 1000
                    clock.now += charge_rtt / 1000
                    return {"chargeTicket": "ticket-fixture"}
                self.assertTrue(path.endswith("/hit"))
                self.assertTrue(0 <= hit_out <= hit_rtt)
                arrived_at = clock.now + hit_out / 1000
                delta = abs((arrived_at - server_start) * 1000 - center)
                hold = (arrived_at - state["issued"]) * 1000
                clock.now += hit_rtt / 1000
                return {"hit": {"perfect": delta <= 210 and 520 <= hold <= 1250,
                                "deltaMs": delta, "holdMs": hold, "damageYi": 1}}

            monitor = build_monitor(clock, request)
            for sequence, row in enumerate(rows, 1):
                clock.now = max(clock.now, 100 + row[1] / 1000)
                results.append(await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), "init-fixture", "session-fixture",
                    "challenge-fixture", battle_start,
                    {"id": str(sequence), "centerMs": row[0], "hitMs": 620, "perfectMs": 210},
                    sequence, lead,
                ))
            self.assertEqual(results[6]["error"], "boss_window_expired")
            return sum(result["accepted_perfect"] for result in results)

        before = asyncio.run(run(100, 366))
        after = asyncio.run(run(server_start, 100))
        self.assertEqual(before, 0)
        self.assertGreaterEqual(after, 12)
        print(json.dumps({"replay": "2026-09-11-main-timing-model", "before": before,
                          "after": after, "windows": 16, "original_late_window_retained": True}))

    def test_entry_clock_requires_fresh_network_evidence_and_resists_a_short_outlier(self):
        def observation(duration, received=99, ok=True):
            return {"request": {"response_received_monotonic": received,
                                "attempts": [{"ok": ok, "duration_ms": 1,
                                              "network_duration_ms": duration}]}}

        observations = [observation(duration) for duration in (200, 202, 210, 700, 1)]
        observations += [observation(5, ok=False), observation(None), observation(5, received=0)]
        payload = {"_client_diagnostics": {"entry_requests": observations}}
        self.assertEqual(WorldBossMonitor._entry_rtt_reference(payload, 100), (200, 5))
        payload["_client_diagnostics"]["entry_requests"] = [observation(200), observation(202)]
        self.assertEqual(WorldBossMonitor._entry_rtt_reference(payload, 100), (None, 2))
        for value in (None, "invalid", {"entry_requests": None}, {"entry_requests": [None, {"request": "invalid"}]}):
            self.assertEqual(WorldBossMonitor._entry_rtt_reference({"_client_diagnostics": value}, 100), (None, 0))

    def test_short_read_deadline_preserves_shared_failures_and_open_circuit(self):
        async def run(directory):
            now = [1000.0]
            circuit = MiniAppTransportCircuitBreaker(str(Path(directory) / "health.json"),
                                                     backoff_seconds=(10, 20), clock=lambda: now[0])
            actor = FakeActor()
            actor.state_file = str(Path(directory) / "state.json")
            monitor = WorldBossMonitor(actor, "main")
            origin = "https://fixture.invalid"
            path = "/api/miniapp/xianxia-world-boss/window"
            circuit.record_failure(origin, MiniAppCircuitPermit(), "api_timeout")
            try:
                with patch("miniapp_beast._MINIAPP_CIRCUIT", new=circuit), patch.object(
                    _PersistentWorldBossJsonClient, "post_sync",
                    side_effect=MiniAppReadDeadlineError("boss_window_poll_timeout"),
                ) as request:
                    before = circuit.snapshot(origin)
                    for _ in range(4):
                        with self.assertRaises(MiniAppReadDeadlineError):
                            await monitor._request(origin, path, {}, timeout=1, time_critical=False)
                    self.assertEqual(circuit.snapshot(origin), before)
                    request.side_effect = MiniAppBeastError("api_timeout")
                    with self.assertRaises(MiniAppBeastError):
                        await monitor._request(origin, path, {}, timeout=5, time_critical=False)
                    self.assertEqual(circuit.snapshot(origin)["consecutive_failures"], 2)
                    with self.assertRaises(MiniAppCircuitOpenError):
                        await monitor._request(origin, path, {}, timeout=5, time_critical=False)
                    self.assertEqual(circuit.snapshot(origin)["status"], "open")
                    calls = request.call_count
                    with self.assertRaises(MiniAppCircuitOpenError):
                        await monitor._request(origin, path, {}, timeout=1, time_critical=False)
                    self.assertEqual(request.call_count, calls)
                    # A failed recovery probe must never clear the circuit.
                    now[0] += 11
                    request.side_effect = MiniAppReadDeadlineError("boss_window_poll_timeout")
                    with self.assertRaises(MiniAppCircuitOpenError):
                        await monitor._request(origin, path, {}, timeout=1, time_critical=False)
                    self.assertEqual(circuit.snapshot(origin)["status"], "open")
                    self.assertEqual(circuit.snapshot(origin)["backoff_level"], 2)
            finally:
                await monitor.stop()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(directory))

    def test_warm_entry_samples_correct_slow_begin_without_changing_other_accounts(self):
        async def run(account, unverified_ms, verified_ms, transport_ms, browser_wait=48):
            clock = RevealClock()
            state = {"starts": 0}
            arrivals = []

            async def request(origin, path, payload, timeout, **kwargs):
                trace = kwargs["network_trace"]
                # Deliberately slow local bookkeeping must not enter the RTT.
                if path.endswith("/start"):
                    clock.now += 0.4
                trace["sent_monotonic"] = clock.now
                try:
                    if path.endswith("/start"):
                        state["starts"] += 1
                        clock.now += 0.2
                        if state["starts"] < 4:
                            return {"boss": {"joinRemainingSeconds": 2}}
                        return {"challenge": {"challengeId": "timing-fixture", "windows": [
                            {"id": "w1", "centerMs": 2500, "hitMs": 620, "perfectMs": 210},
                        ]}}
                    if path.endswith("/begin"):
                        if not payload.get("turnstileToken"):
                            clock.now += unverified_ms / 1000
                            raise MiniAppBeastError("turnstile_failed", 403)
                        clock.now += verified_ms / 1000
                        state["battle_start"] = clock.now + 1.499 - transport_ms / 2000
                        return {"startsInMs": 1499}
                    if path.endswith("/charge-start"):
                        state["charged_at"] = clock.now + transport_ms / 2000
                        clock.now += transport_ms / 1000
                        return {"chargeTicket": "ticket-fixture"}
                    if path.endswith("/hit"):
                        arrived_at = clock.now + transport_ms / 2000
                        delta = abs((arrived_at - state["battle_start"]) * 1000 - 2500)
                        hold = (arrived_at - state["charged_at"]) * 1000
                        arrivals.append(delta)
                        clock.now += transport_ms / 1000
                        return {"hit": {"deltaMs": delta, "holdMs": hold, "damageYi": 1,
                                        "perfect": delta <= 210 and 520 <= hold <= 1250}}
                    if path.endswith("/finish"):
                        return {"result": {"score": 100}}
                    raise AssertionError(path)
                finally:
                    trace["received_monotonic"] = clock.now

            async def handoff(*args):
                clock.now += browser_wait
                return "browser-fixture", "request-fixture"

            with tempfile.TemporaryDirectory() as directory:
                actor = FakeActor()
                actor.state_file = str(Path(directory) / "state.json")
                monitor = WorldBossMonitor(actor, account, sleep=clock.sleep,
                                           monotonic=clock.monotonic, finish_grace_seconds=0)
                monitor._save_checkpoint = AsyncMock()
                monitor._wait_for_turnstile_token = handoff
                monitor._record_turnstile_result = lambda *args, **kwargs: None
                entry = extract_world_boss_entry(DummyMessage())
                with patch("world_boss_features._post_json", new=request):
                    try:
                        token, payload = await monitor._wait_for_challenge(entry, "init-fixture", 42)
                        result = await monitor._fight(entry, "init-fixture", token, payload)
                    finally:
                        await monitor.stop()
            self.assertEqual(result["perfect_count"], 1)
            self.assertLessEqual(arrivals[0], 210)
            sync = result["diagnostics"]["clock_sync"]
            self.assertEqual(sync["round_trip_ms"], transport_ms)
            self.assertEqual(sync["request"]["successful_attempt_ms"], verified_ms)
            return sync

        # Waaiging's 194 ms reference was already sound. Main's 731 ms sample
        # was a transient delay; its other entry requests and strikes took 200 ms.
        for account, unverified, verified, transport in (
            ("main", 731, 1311, 200), ("sub", 193, 1528, 193),
            ("xiaohao", 189, 1231, 189), ("waaiging", 194, 2143, 194),
        ):
            with self.subTest(account=account):
                sync = asyncio.run(run(account, unverified, verified, transport))
                if account == "main":
                    self.assertEqual(sync["request"]["clock_rtt_source"], "entry_requests")
                    self.assertGreaterEqual(sync["request"]["entry_rtt_sample_count"], 3)

        # A long browser wait makes the old fast samples stale. If the route
        # has become slower, retain the fresh /begin timing instead.
        with self.subTest(account="main", stale_entry=True):
            sync = asyncio.run(run("main", 731, 1311, 731, browser_wait=180))
            self.assertEqual(sync["request"]["clock_rtt_source"], "pre_verification_begin")

    def test_poll_interval_includes_http_time_and_keeps_wait_diagnostics(self):
        async def run():
            clock = RevealClock()
            starts = []

            async def request(origin, path, payload, timeout):
                starts.append(clock.now)
                clock.now += 0.2
                if len(starts) < 4:
                    raise MiniAppBeastError("boss_window_not_ready", 409)
                return {"window": {"id": "w1", "centerMs": 3000, "hitMs": 620, "perfectMs": 210},
                        "done": True}

            monitor = build_monitor(clock, request)
            queue = asyncio.Queue()
            log = []
            await monitor._reveal_windows(extract_world_boss_entry(DummyMessage()),
                                          "init-fixture", "session-fixture", "challenge-fixture",
                                          100, queue, log, 1)
            self.assertEqual(len(starts), 4)
            for previous, current in zip(starts, starts[1:]):
                self.assertAlmostEqual(current - previous, 0.36, places=6)
            self.assertEqual(log[0]["polling"]["wait_count"], 3)
            self.assertEqual(log[0]["polling"]["max_request_ms"], 200)
            self.assertEqual(log[0]["polling"]["max_poll_gap_ms"], 360)
            self.assertEqual((await queue.get())["id"], "w1")
            self.assertIsNone(await queue.get())

        asyncio.run(run())

    def test_slow_charge_receipt_can_still_reach_server_minimum_hold(self):
        async def run(charge_ms, issued_ms, expect_perfect):
            clock = RevealClock(154.0)
            state = {}

            async def request(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    state["pressed"] = clock.now
                    state["issued"] = clock.now + issued_ms / 1000
                    clock.now += charge_ms / 1000
                    return {"chargeTicket": "ticket-fixture"}
                self.assertTrue(path.endswith("/hit"))
                state["released"] = clock.now
                arrived = clock.now + 0.094
                delta = abs((arrived - 100) * 1000 - 55766)
                server_hold = (arrived - state["issued"]) * 1000
                state["reported_hold"] = payload["holdMs"]
                clock.now += 0.196
                return {"hit": {"perfect": delta <= 210 and 520 <= server_hold <= 1250,
                                "deltaMs": delta, "holdMs": server_hold, "damageYi": 1}}

            monitor = build_monitor(clock, request, account="xiaohao")
            monitor._drift_ms, monitor._drift_samples = 33, 1
            result = await monitor._hit_window(
                extract_world_boss_entry(DummyMessage()), "init-fixture", "session-fixture",
                "challenge-fixture", 100,
                {"id": "w11", "centerMs": 55766, "hitMs": 620, "perfectMs": 210}, 11, 94,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["accepted_perfect"], expect_perfect)
            self.assertEqual(state["reported_hold"], round((state["released"] - state["pressed"]) * 1000))
            diagnostic = result["diagnostic"]
            self.assertEqual(diagnostic["wake_lateness_ms"],
                             max(0, diagnostic["sent_elapsed_ms"] - diagnostic["target_ms"]))
            if expect_perfect:
                self.assertGreaterEqual(diagnostic["server_hold_ms"], 520)
                self.assertLessEqual(state["reported_hold"], 1250)
            return diagnostic

        # Xiaohao #11: the local hold was ~1 s, but the late ticket only aged
        # ~417 ms on the server. A small real delay still fits inside 210 ms.
        diagnostic = asyncio.run(run(796, 675, True))
        self.assertGreater(diagnostic["target_ms"], diagnostic["ideal_target_ms"])
        # A late return path must not force a normal ticket outside its band.
        asyncio.run(run(796, 94, True))
        # If both requirements cannot fit, retain accuracy and the real verdict.
        diagnostic = asyncio.run(run(1100, 990, False))
        self.assertEqual(diagnostic["target_ms"], diagnostic["ideal_target_ms"])

    def test_stalled_window_read_is_retried_before_the_preparation_window_expires(self):
        release = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload["afterWindowId"])
                if len(requests) == 1:
                    release.wait(4)
                    body = {"ok": False, "error": "boss_window_not_ready"}
                else:
                    release.set()
                    body = {"window": {"id": "w1", "centerMs": 3000, "hitMs": 620, "perfectMs": 210},
                            "done": True}
                data = json.dumps(body).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        async def run():
            from dataclasses import replace
            entry = replace(extract_world_boss_entry(DummyMessage()),
                            origin=f"http://127.0.0.1:{server.server_port}")
            monitor = WorldBossMonitor(FakeActor(), "main")
            queue, log = asyncio.Queue(), []
            try:
                with patch("miniapp_beast._MINIAPP_CIRCUIT", new=HealthyCircuit()):
                    await monitor._reveal_windows(entry, "init-fixture", "session-fixture",
                                                  "challenge-fixture", time.monotonic(), queue, log, 1)
                revealed = [row for row in log if row["status"] == "revealed"]
                self.assertEqual(log[0]["error"], "boss_window_poll_timeout")
                self.assertEqual(len(revealed), 1)
                self.assertGreater(revealed[0]["lead_ms"], 1000)
                self.assertEqual(requests, ["", ""])
                self.assertEqual((await queue.get())["id"], "w1")
                self.assertIsNone(await queue.get())
            finally:
                release.set()
                await monitor.stop()

        try:
            asyncio.run(run())
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(3)
