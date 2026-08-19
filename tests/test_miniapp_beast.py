import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from miniapp_beast import (
    DEFAULT_CIRCUIT_BACKOFF_SECONDS,
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    MiniAppTransportCircuitBreaker,
    _post_json,
    fetch_miniapp_beast_snapshot,
    miniapp_entry_start_param,
    miniapp_upstream_failure,
    normalize_spirit_beast_roster,
    read_cached_spirit_token,
    read_refresh_request,
    write_cached_spirit_token,
    write_refresh_request,
)


ROSTER_PAYLOAD = {
    "ok": True,
    "player": {"daoName": "冥狂客", "sect": "万灵宗"},
    "beasts": [{
        "id": 2439,
        "name": "大圣",
        "beastType": "金瞳妖猴",
        "tier": 3,
        "level": 40,
        "status": "休息中",
        "stamina": 55,
        "combatPower": 396,
        "experience": 18,
        "canExpedition": True,
        "canExploreAbyss": True,
        "isActive": False,
    }],
}


class MiniAppBeastTests(unittest.TestCase):
    def test_default_circuit_uses_long_backoff_after_repeated_server_failures(self):
        self.assertEqual(
            DEFAULT_CIRCUIT_BACKOFF_SECONDS,
            (15 * 60, 30 * 60, 60 * 60, 3 * 60 * 60, 6 * 60 * 60),
        )

    def test_upstream_failure_classifier_covers_transport_and_http_5xx(self):
        self.assertTrue(miniapp_upstream_failure(MiniAppBeastError("invalid_json")))
        self.assertTrue(miniapp_upstream_failure(MiniAppBeastError("maintenance", 503)))
        self.assertTrue(miniapp_upstream_failure(TimeoutError()))
        self.assertFalse(miniapp_upstream_failure(MiniAppBeastError("bad_request", 400)))

    def test_shared_circuit_opens_extends_and_recovers_only_on_transitions(self):
        class Logger:
            def __init__(self):
                self.errors = []
                self.warnings = []

            def error(self, message, *args):
                self.errors.append(message % args if args else message)

            def warning(self, message, *args):
                self.warnings.append(message % args if args else message)

        clock = [1000.0]
        logger = Logger()
        origin = "https://asc.aiopenai.app"
        with tempfile.TemporaryDirectory() as tmpdir:
            breaker = MiniAppTransportCircuitBreaker(
                os.path.join(tmpdir, "health.json"),
                failure_threshold=2,
                backoff_seconds=(10, 20, 40),
                probe_lease_seconds=5,
                clock=lambda: clock[0],
                logger=logger,
            )

            first = breaker.acquire(origin)
            self.assertIsNotNone(first.permit)
            breaker.record_failure(origin, first.permit, "invalid_json")
            self.assertEqual(logger.errors, [])

            second = breaker.acquire(origin)
            breaker.record_failure(origin, second.permit, "timeouterror")
            state = breaker.snapshot(origin)
            self.assertEqual(state["status"], "open")
            self.assertEqual(state["backoff_level"], 1)
            self.assertEqual(len(logger.errors), 1)

            blocked = breaker.acquire(origin)
            self.assertIsNone(blocked.permit)
            self.assertEqual(int(blocked.wait_seconds), 10)
            self.assertEqual(len(logger.errors), 1)

            clock[0] += 10
            probe = breaker.acquire(origin)
            self.assertTrue(probe.permit.is_probe)
            competing = breaker.acquire(origin)
            self.assertIsNone(competing.permit)
            self.assertTrue(competing.half_open)
            breaker.record_failure(origin, probe.permit, "invalid_json")
            state = breaker.snapshot(origin)
            self.assertEqual(state["status"], "open")
            self.assertEqual(state["backoff_level"], 2)
            self.assertEqual(len(logger.errors), 2)

            clock[0] += 20
            recovery_probe = breaker.acquire(origin)
            self.assertTrue(recovery_probe.permit.is_probe)
            breaker.record_success(origin, recovery_probe.permit)
            state = breaker.snapshot(origin)
            self.assertEqual(state["status"], "closed")
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertEqual(len(logger.errors), 2)
            self.assertEqual(len(logger.warnings), 1)

    def test_post_json_skips_http_while_circuit_is_open_then_probes_once(self):
        clock = [2000.0]
        origin = "https://asc.aiopenai.app"
        with tempfile.TemporaryDirectory() as tmpdir:
            breaker = MiniAppTransportCircuitBreaker(
                os.path.join(tmpdir, "health.json"),
                failure_threshold=1,
                backoff_seconds=(10,),
                probe_lease_seconds=5,
                clock=lambda: clock[0],
                logger=Mock(),
            )
            post = Mock(side_effect=[MiniAppBeastError("invalid_json"), {"ok": True}])
            with (
                patch("miniapp_beast._MINIAPP_CIRCUIT", breaker),
                patch("miniapp_beast._post_json_sync", post),
            ):
                with self.assertRaises(MiniAppCircuitOpenError) as opened:
                    asyncio.run(_post_json(origin, "/start", {}, 5))
                self.assertGreaterEqual(opened.exception.retry_after, 9)
                self.assertEqual(post.call_count, 1)

                with self.assertRaises(MiniAppCircuitOpenError) as blocked:
                    asyncio.run(_post_json(origin, "/details", {}, 5))
                self.assertGreaterEqual(blocked.exception.retry_after, 9)
                self.assertEqual(post.call_count, 1)

                clock[0] += 10
                result = asyncio.run(_post_json(origin, "/details", {}, 5))
                self.assertEqual(result, {"ok": True})
                self.assertEqual(post.call_count, 2)
                self.assertEqual(breaker.snapshot(origin)["status"], "closed")

    def test_entry_and_roster_normalization(self):
        entry = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
        self.assertEqual(miniapp_entry_start_param(entry), "df_fixture")
        beasts = normalize_spirit_beast_roster(ROSTER_PAYLOAD)
        self.assertEqual(beasts[0]["full_name"], "大圣")
        self.assertEqual(beasts[0]["species"], "3阶金瞳妖猴")
        self.assertEqual(beasts[0]["stamina"], 55)
        self.assertTrue(beasts[0]["can_explore_abyss"])

    def test_invalid_entry_is_rejected(self):
        with self.assertRaises(MiniAppBeastError):
            miniapp_entry_start_param("https://t.me/fanrenxiuxian_bot")

    def test_fetch_renews_spirit_token_then_reads_roster(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append(path)
            if path.endswith("/xianxia-dwelling/start"):
                return {"ok": True}
            if path.endswith("/xianxia-dwelling/external"):
                return {"ok": True, "url": "/miniapp/xianxia-spirit-beast?startapp=spiritbeast_fixture"}
            if path.endswith("/xianxia-spirit-beast/start"):
                return ROSTER_PAYLOAD
            self.fail(path)

        with patch("miniapp_beast.request_webview_init_data", new=AsyncMock(return_value="signed")):
            snapshot = asyncio.run(fetch_miniapp_beast_snapshot(
                object(),
                "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
                post_json=post_json,
            ))
        self.assertEqual(snapshot["spirit_token"], "spiritbeast_fixture")
        self.assertEqual(snapshot["beasts"][0]["full_name"], "大圣")
        self.assertEqual(calls, [
            "/api/miniapp/xianxia-dwelling/start",
            "/api/miniapp/xianxia-dwelling/external",
            "/api/miniapp/xianxia-spirit-beast/start",
        ])

    def test_cached_spirit_token_skips_external_exchange(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append(path)
            return ROSTER_PAYLOAD

        with patch("miniapp_beast.request_webview_init_data", new=AsyncMock(return_value="signed")):
            snapshot = asyncio.run(fetch_miniapp_beast_snapshot(
                object(),
                "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
                cached_spirit_token="spiritbeast_cached",
                post_json=post_json,
            ))
        self.assertEqual(snapshot["spirit_token"], "spiritbeast_cached")
        self.assertEqual(calls, ["/api/miniapp/xianxia-spirit-beast/start"])

    def test_dashboard_refresh_request_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            written = write_refresh_request(tmpdir, requested_by="tester")
            loaded = read_refresh_request(tmpdir)
            self.assertEqual(loaded["request_id"], written["request_id"])
            self.assertEqual(loaded["requested_by"], "tester")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "miniapp_beast_refresh_request.json")))

    def test_spirit_token_cache_is_bound_to_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entry = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
            write_cached_spirit_token(tmpdir, entry, "spiritbeast_cached")
            self.assertEqual(read_cached_spirit_token(tmpdir, entry), "spiritbeast_cached")
            self.assertEqual(read_cached_spirit_token(tmpdir, entry + "2"), "")


if __name__ == "__main__":
    unittest.main()
