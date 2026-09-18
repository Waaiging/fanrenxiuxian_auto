"""Offline transport regressions from the September 17 Qingyuanzi round."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import unittest
from unittest.mock import patch

from miniapp_beast import MiniAppBeastError, MiniAppCircuitPermit, MiniAppReadDeadlineError
from tests.test_world_boss_features import DummyMessage, FakeActor
from tests.test_world_boss_reveal_protocol import RevealClock, build_monitor
from tests.test_world_boss_transport import HealthyCircuit
from world_boss_features import (
    WorldBossMonitor,
    _PersistentWorldBossJsonClient,
    extract_world_boss_entry,
)


WINDOW_PATH = "/api/miniapp/xianxia-world-boss/window"


class BudgetConnection:
    """A socket clock whose individual operations respect their own timeout."""

    def __init__(self, clock, *, request_seconds=0, header_seconds=0, body_seconds=()):
        self.clock = clock
        self.request_seconds = request_seconds
        self.header_seconds = header_seconds
        self.body_seconds = list(body_seconds)
        self.sock = self
        self.timeout = 1
        self.timeouts = []
        self.requests = []
        self.closed = False
        self.status = 200
        self.will_close = False
        self.chunks = [b'{"ok":', b' true}'] if body_seconds else [b'{"ok": true}']
        self.body_index = 0

    def settimeout(self, seconds):
        self.timeout = seconds
        self.timeouts.append(seconds)

    def advance(self, seconds):
        if seconds > self.timeout:
            self.clock.now += self.timeout
            raise TimeoutError("fixture socket wait expired")
        self.clock.now += seconds

    def request(self, method, path, **kwargs):
        self.requests.append(path)
        self.advance(self.request_seconds)

    def getresponse(self):
        self.advance(self.header_seconds)
        return self

    def read1(self, size=-1):
        if self.body_index >= len(self.chunks):
            return b""
        duration = self.body_seconds[self.body_index] if self.body_seconds else 0
        self.advance(duration)
        chunk = self.chunks[self.body_index]
        self.body_index += 1
        return chunk

    def read(self):
        chunks = []
        while not self.isclosed():
            chunks.append(self.read1())
        return b"".join(chunks)

    def isclosed(self):
        return self.body_index == len(self.chunks)

    @property
    def length(self):
        return sum(map(len, self.chunks[self.body_index:]))

    def close(self):
        self.closed = True


class RecordingCircuit(HealthyCircuit):
    def __init__(self, clock, *, queue_seconds=0, probe=False):
        self.clock_source = clock
        self.queue_seconds = queue_seconds
        self.probe = probe
        self.failures = []
        self.successes = []

    def acquire(self, origin):
        self.clock_source.now += self.queue_seconds
        decision = super().acquire(origin)
        if self.probe:
            return type(decision)(permit=MiniAppCircuitPermit(probe_token="fixture-probe"))
        return decision

    def record_failure(self, *args):
        self.failures.append(args)

    def record_success(self, *args):
        self.successes.append(args)


async def inline_blocking(function, *args, **kwargs):
    return function(*args)


class WorldBossSeptember17Tests(unittest.TestCase):
    def request_with_clock(self, clock, connection, circuit, *, path=WINDOW_PATH, timeout=1):
        async def run():
            monitor = WorldBossMonitor(FakeActor(), "xiaohao", monotonic=clock.monotonic)
            client = _PersistentWorldBossJsonClient("https://fixture.invalid")
            monitor._json_clients[client.origin] = client
            trace = {}
            try:
                with patch("world_boss_features.time.monotonic", clock.monotonic), \
                        patch("miniapp_beast._MINIAPP_CIRCUIT", circuit), \
                        patch("miniapp_beast._run_blocking", inline_blocking), \
                        patch.object(client, "_new_connection", return_value=connection):
                    try:
                        result = await monitor._request(
                            client.origin, path, {}, timeout=timeout, trace=trace,
                        )
                    except MiniAppReadDeadlineError as exc:
                        return exc, trace, len(client._idle)
                    return result, trace, len(client._idle)
            finally:
                await monitor.stop()

        return asyncio.run(run())

    def test_expired_queue_budget_does_not_start_another_window_read(self):
        # The round recorded a 2,005 ms maximum pre-request delay despite a
        # one-second read timeout. Do not spend another network timeout after it.
        for probe in (False, True):
            with self.subTest(probe=probe):
                clock = RevealClock()
                connection = BudgetConnection(clock)
                circuit = RecordingCircuit(clock, queue_seconds=2.005, probe=probe)
                result, trace, pooled = self.request_with_clock(clock, connection, circuit)
                self.assertIsInstance(result, MiniAppReadDeadlineError)
                self.assertEqual(result.code, "boss_window_poll_timeout")
                self.assertEqual(result.details["deadline_phase"], "queue")
                self.assertEqual(connection.requests, [])
                self.assertEqual(pooled, 0)
                self.assertEqual(len(circuit.failures), int(probe))
                self.assertEqual(circuit.successes, [])
                self.assertEqual(trace["attempts"][0]["http_status"], 0)
                self.assertEqual(trace["attempts"][0]["deadline_phase"], "queue")

    def test_request_headers_and_body_share_the_remaining_read_budget(self):
        clock = RevealClock()
        start = clock.now
        # All four individual waits fit the old one-second socket timeout.
        # Together they exceed the poll's deadline and would block the cursor.
        connection = BudgetConnection(
            clock, request_seconds=.30, header_seconds=.30, body_seconds=(.25, .25),
        )
        circuit = RecordingCircuit(clock)
        result, trace, pooled = self.request_with_clock(clock, connection, circuit)
        self.assertIsInstance(result, MiniAppReadDeadlineError)
        self.assertEqual(result.details["deadline_phase"], "body")
        self.assertEqual(trace["attempts"][0]["deadline_phase"], "body")
        self.assertAlmostEqual(clock.now - start, 1, places=6)
        self.assertEqual(connection.requests, [WINDOW_PATH])
        self.assertTrue(connection.closed)
        self.assertEqual(pooled, 0, "A partially read socket cannot return to the pool")
        self.assertEqual(circuit.failures, [])
        self.assertEqual(circuit.successes, [])

    def test_queue_delay_reduces_the_socket_budget_without_adding_requests(self):
        clock = RevealClock()
        start = clock.now
        connection = BudgetConnection(clock, header_seconds=.4)
        circuit = RecordingCircuit(clock, queue_seconds=.7)
        result, trace, pooled = self.request_with_clock(clock, connection, circuit)
        self.assertIsInstance(result, MiniAppReadDeadlineError)
        self.assertEqual(result.details["deadline_phase"], "headers")
        self.assertAlmostEqual(clock.now - start, 1, places=6)
        self.assertEqual(connection.requests, [WINDOW_PATH])
        self.assertEqual(pooled, 0)

    def test_successful_read_retains_pooling_and_normal_health_receipt(self):
        clock = RevealClock()
        connection = BudgetConnection(
            clock, request_seconds=.1, header_seconds=.2, body_seconds=(.1, .1),
        )
        circuit = RecordingCircuit(clock, queue_seconds=.1)
        result, trace, pooled = self.request_with_clock(clock, connection, circuit)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(pooled, 1)
        self.assertEqual(len(circuit.successes), 1)
        self.assertEqual(circuit.failures, [])
        self.assertEqual(connection.requests, [WINDOW_PATH])
        self.assertAlmostEqual(trace["attempts"][0]["queue_delay_ms"], 100)

    def test_action_request_keeps_its_existing_timeout_and_is_not_replayed(self):
        clock = RevealClock()
        start = clock.now
        connection = BudgetConnection(clock, header_seconds=1.4)
        circuit = RecordingCircuit(clock, queue_seconds=.3)
        result, trace, pooled = self.request_with_clock(
            clock, connection, circuit,
            path="/api/miniapp/xianxia-world-boss/charge-start", timeout=5,
        )
        self.assertEqual(result, {"ok": True})
        self.assertAlmostEqual(clock.now - start, 1.7, places=6)
        self.assertEqual(len(connection.requests), 1)
        self.assertEqual(pooled, 1)

    def test_real_http_body_modes_and_slow_response_obey_the_budget(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                mode = payload["mode"]
                requests.append((mode, self.client_address))
                body = b'{"ok": true}'
                if mode == "slow":
                    time.sleep(.65)
                self.send_response(200)
                if mode == "chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(body) + (2 if mode == "truncated" else 0)))
                if mode == "close":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                elif mode == "truncated":
                    self.close_connection = True
                self.end_headers()
                if mode == "slow":
                    time.sleep(.65)
                try:
                    if mode == "chunked":
                        self.wfile.write(b"6\r\n" + body[:6] + b"\r\n")
                        self.wfile.write(b"6\r\n" + body[6:] + b"\r\n0\r\n\r\n")
                    else:
                        self.wfile.write(body)
                except OSError:
                    pass  # A timed-out reader has already closed its socket.

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        client = _PersistentWorldBossJsonClient(origin)
        try:
            for mode in ("normal", "chunked", "close"):
                with self.subTest(mode=mode):
                    result = client.post_sync(
                        origin, WINDOW_PATH, {"mode": mode}, 1,
                        deadline_monotonic=time.monotonic() + 1,
                    )
                    self.assertEqual(result, {"ok": True})
            self.assertEqual(len({address for _, address in requests}), 1)
            self.assertEqual(client._idle, [])
            with self.assertRaises(MiniAppReadDeadlineError) as raised:
                client.post_sync(
                    origin, WINDOW_PATH, {"mode": "slow"}, 1,
                    deadline_monotonic=time.monotonic() + 1,
                )
            self.assertEqual(raised.exception.details["deadline_phase"], "body")
            with self.assertRaises(MiniAppBeastError) as truncated:
                client.post_sync(
                    origin, WINDOW_PATH, {"mode": "truncated"}, 1,
                    deadline_monotonic=time.monotonic() + 1,
                )
            self.assertEqual(truncated.exception.code, "api_unreachable")
            self.assertEqual(truncated.exception.details["transport_error"], "incompleteread")
            self.assertEqual([mode for mode, _ in requests], ["normal", "chunked", "close", "slow", "truncated"])
            self.assertEqual(client._idle, [])
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            worker.join(3)

    def test_finished_round_keeps_window_request_attempts_without_secrets(self):
        async def run():
            clock = RevealClock()
            window = {"id": "fixture-window", "centerMs": 3000, "hitMs": 620, "perfectMs": 210}

            async def post(origin, path, payload, timeout):
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "fixture-ticket"}
                if path.endswith("/hit"):
                    return {"hit": {"damageYi": 1, "perfect": True, "deltaMs": 0}}
                self.assertTrue(path.endswith("/finish"))
                return {"result": {"hits": 1, "perfects": 1, "score": 100}}

            async def reveal(entry, init, session, challenge, start, queue, log, count, **kwargs):
                log.append({
                    "status": "error", "error": "boss_window_poll_timeout",
                    "request": {"path": "/window", "attempts": [{
                        "ok": False, "deadline_phase": "body",
                        "sessionToken": "must-not-appear",
                    }], "read_budget_ms": 1000},
                })
                log.append({
                    "status": "revealed", "sequence": 1, "window_id": window["id"],
                    "lead_ms": 1881,
                    "request": {"path": "/window", "attempts": [{
                        "ok": True, "network_duration_ms": 184, "queue_delay_ms": 1,
                        "bookkeeping_ms": 2, "sessionToken": "must-not-appear",
                    }], "total_duration_ms": 188},
                })
                await queue.put(window)
                await queue.put(None)

            monitor = build_monitor(clock, post)
            monitor._reveal_windows = reveal
            try:
                result = await monitor._fight(
                    extract_world_boss_entry(DummyMessage()), "init", "session",
                    {"challenge": {"challengeId": "fixture", "windowCount": 1, "windows": []}},
                )
                diagnostics = result["diagnostics"]
                rows = diagnostics["window_reveal"]["log"]
                self.assertEqual(rows[0]["request"]["attempts"][0]["deadline_phase"], "body")
                self.assertEqual(rows[0]["request"]["read_budget_ms"], 1000)
                attempt = rows[1]["request"]["attempts"][0]
                self.assertIsInstance(attempt, dict)
                self.assertEqual(attempt["network_duration_ms"], 184)
                self.assertEqual(attempt["queue_delay_ms"], 1)
                self.assertNotIn("must-not-appear", json.dumps(diagnostics))
                self.assertEqual(result["hit_count"], 1)
            finally:
                await monitor.stop()

        asyncio.run(run())
