import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from miniapp_beast import MiniAppBeastError, MiniAppCircuitDecision, MiniAppCircuitPermit, _post_json_sync
from tests.test_world_boss_features import DummyMessage, FakeActor
from world_boss_features import WorldBossMonitor, _PersistentWorldBossJsonClient, extract_world_boss_entry


class HealthyCircuit:
    clock = staticmethod(time.time)

    def acquire(self, origin):
        return MiniAppCircuitDecision(permit=MiniAppCircuitPermit())

    def snapshot(self, origin):
        return {"status": "closed"}

    def record_failure(self, *args):
        pass

    def record_success(self, *args):
        pass

    def force_recover(self, *args):
        pass


@contextmanager
def local_server():
    waiting = threading.Event()
    release = threading.Event()
    connections = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            connections.append(self.client_address)
            if self.path == "/slow":
                waiting.set()
                release.wait(3)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", connections, waiting, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


class WorldBossTransportTests(unittest.TestCase):
    def test_connections_are_reused_without_blocking_a_concurrent_hit(self):
        with local_server() as (origin, connections, waiting, release):
            client = _PersistentWorldBossJsonClient(origin)
            try:
                client.post_sync(origin, "/one", {}, 5)
                client.post_sync(origin, "/two", {}, 5)
                self.assertEqual(connections[0], connections[1])
                with ThreadPoolExecutor(max_workers=2) as executor:
                    slow = executor.submit(client.post_sync, origin, "/slow", {}, 5)
                    self.assertTrue(waiting.wait(2))
                    try:
                        hit = executor.submit(client.post_sync, origin, "/hit", {}, 5)
                        self.assertEqual(hit.result(timeout=2), {"ok": True})
                        self.assertFalse(slow.done())
                    finally:
                        release.set()
                    slow.result(timeout=2)
                self.assertEqual(len(set(connections)), 2)
            finally:
                client.close()

    def test_lost_post_response_is_not_replayed_by_connection_pool(self):
        client = _PersistentWorldBossJsonClient("https://fixture.invalid")
        connection = Mock(sock=None)
        connection.getresponse.side_effect = http.client.RemoteDisconnected()
        with patch.object(client, "_new_connection", return_value=connection):
            with self.assertRaises(MiniAppBeastError) as raised:
                client.post_sync(client.origin, "/begin", {"turnstileToken": "one-shot"}, 5)
        self.assertEqual(raised.exception.code, "api_unreachable")
        self.assertEqual(raised.exception.details, {"transport_error": "remotedisconnected"})
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()
        client.close()

    def test_entry_recovers_from_a_stale_connection_using_its_retry_policy(self):
        async def run():
            monitor = WorldBossMonitor(FakeActor(), "main")
            entry = extract_world_boss_entry(DummyMessage())
            client = _PersistentWorldBossJsonClient(entry.origin)
            stale, fresh = Mock(sock=None), Mock(sock=None)
            stale.getresponse.side_effect = http.client.RemoteDisconnected()
            response = Mock(status=200, will_close=False)
            response.read.return_value = b'{"ok": true}'
            fresh.getresponse.return_value = response
            client._idle.append((time.monotonic(), stale))
            monitor._json_clients[entry.origin] = client
            try:
                with patch("miniapp_beast._MINIAPP_CIRCUIT", HealthyCircuit()), \
                     patch.object(client, "_new_connection", return_value=fresh):
                    self.assertEqual(await monitor._start_request(entry, "fixture", "fixture", 42),
                                     {"ok": True})
                self.assertEqual(stale.request.call_count, 1)
                stale.close.assert_called_once()
                self.assertEqual(fresh.request.call_count, 1)
            finally:
                await monitor.stop()
        asyncio.run(run())

    def test_default_executor_congestion_does_not_delay_boss_transport(self):
        async def run():
            loop = asyncio.get_running_loop()
            loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
            occupied, release = threading.Event(), threading.Event()
            def block():
                occupied.set()
                release.wait(3)
            background = asyncio.create_task(asyncio.to_thread(block))
            while not occupied.is_set():
                await asyncio.sleep(0.001)
            monitor = WorldBossMonitor(FakeActor(), "main")
            try:
                with patch("miniapp_beast._MINIAPP_CIRCUIT", HealthyCircuit()), \
                     patch.object(_PersistentWorldBossJsonClient, "post_sync", return_value={"ok": True}):
                    result = await asyncio.wait_for(monitor._request(
                        "https://fixture.invalid", "/hit", {}, time_critical=False,
                    ), timeout=0.5)
                    self.assertEqual(result, {"ok": True})
                    self.assertFalse(background.done())
            finally:
                release.set()
                await background
                await monitor.stop()
        asyncio.run(run())

    def test_network_timing_excludes_health_bookkeeping(self):
        async def run():
            class SlowReceiptCircuit(HealthyCircuit):
                def record_success(self, *args):
                    time.sleep(0.15)
            monitor = WorldBossMonitor(FakeActor(), "main")
            trace = {}
            try:
                with patch("miniapp_beast._MINIAPP_CIRCUIT", SlowReceiptCircuit()), \
                     patch.object(_PersistentWorldBossJsonClient, "post_sync", return_value={"ok": True}):
                    await monitor._request("https://fixture.invalid", "/begin", {}, trace=trace)
                attempt = trace["attempts"][0]
                self.assertGreaterEqual(attempt["bookkeeping_ms"], 140)
                self.assertLess(attempt["network_duration_ms"], 100)
                self.assertLess(trace["response_received_monotonic"], time.monotonic() - 0.1)
            finally:
                await monitor.stop()
        asyncio.run(run())

    def test_real_http_error_preserves_timing_but_excludes_credentials(self):
        body = {"ok": False, "error": "boss_hit_outside_window", "serverElapsedMs": 1520,
                "sessionToken": "private", "nested": {"holdMs": 1000, "chargeTicket": "private"}}
        error = urllib.error.HTTPError("https://fixture.invalid", 409, "fixture", {},
                                       io.BytesIO(json.dumps(body).encode()))
        with patch("miniapp_beast.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(MiniAppBeastError) as raised:
                _post_json_sync("https://fixture.invalid", "/hit", {}, 5)
        self.assertEqual(raised.exception.details["serverElapsedMs"], 1520)
        self.assertEqual(raised.exception.details["nested"], {"holdMs": 1000})
        self.assertNotIn("private", json.dumps(raised.exception.details))

    def test_drift_converges_to_full_delay_correction_across_sixteen_hits(self):
        async def run():
            now, center = [100.0], [0]
            errors = []
            async def sleep(seconds): now[0] += max(0, seconds)
            async def post(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    now[0] += 0.1
                    return {"chargeTicket": "fixture"}
                if path.endswith("/hit"):
                    signed = round((now[0] - 100) * 1000 + 50 - center[0])
                    errors.append(signed)
                    now[0] += 0.1
                    return {"hit": {"deltaMs": abs(signed), "perfect": abs(signed) <= 100,
                                    "holdMs": 1000, "damageYi": 1}}
                raise AssertionError(path)
            monitor = WorldBossMonitor(FakeActor(), "main", post_json=post, sleep=sleep,
                                       monotonic=lambda: now[0])
            perfects = 0
            for index in range(16):
                center[0] = 3000 + 5000 * index
                result = await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), "fixture", "fixture", "fixture", 100.0,
                    {"id": str(index), "centerMs": center[0], "hitMs": 620, "perfectMs": 100},
                    index + 1, request_lead_ms=250,
                )
                perfects += result["accepted_perfect"]
            self.assertLessEqual(monitor._drift_lead_ms(), -180)
            self.assertLessEqual(abs(errors[-1]), 40)
            self.assertGreaterEqual(perfects, 12)
            await monitor.stop()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
