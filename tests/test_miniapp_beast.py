import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from miniapp_beast import (
    MiniAppBeastError,
    fetch_miniapp_beast_snapshot,
    miniapp_entry_start_param,
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
        "isActive": False,
    }],
}


class MiniAppBeastTests(unittest.TestCase):
    def test_entry_and_roster_normalization(self):
        entry = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
        self.assertEqual(miniapp_entry_start_param(entry), "df_fixture")
        beasts = normalize_spirit_beast_roster(ROSTER_PAYLOAD)
        self.assertEqual(beasts[0]["full_name"], "大圣")
        self.assertEqual(beasts[0]["species"], "3阶金瞳妖猴")
        self.assertEqual(beasts[0]["stamina"], 55)

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
