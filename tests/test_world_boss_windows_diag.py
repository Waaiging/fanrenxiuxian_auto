import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniapp_beast import MiniAppBeastError
from world_boss_features import WorldBossMonitor, extract_world_boss_entry
from tests.test_world_boss_features import DummyMessage, FakeActor


class WindowsDiagnosticsTests(unittest.TestCase):
    """_windows 解析失败时必须携带 challenge 快照（诊断增强）。"""

    def test_invalid_windows_error_carries_challenge_snapshot(self):
        challenge = {
            "challengeId": "ch-123",
            "unknownFormat": [{"foo": 1}],
        }
        try:
            WorldBossMonitor._windows(challenge)
        except MiniAppBeastError as exc:
            self.assertEqual(exc.code, "boss_windows_invalid")
            details = getattr(exc, "details", None)
            self.assertIsInstance(details, dict)
            snapshot = details.get("challenge")
            self.assertIsInstance(snapshot, dict)
            self.assertEqual(snapshot.get("challengeId"), "ch-123")
            self.assertIn("unknownFormat", snapshot)
        else:
            self.fail("expected boss_windows_invalid")

    def test_new_format_falls_back_to_begin_response_windows(self):
        """2026-08-26+ format: challenge attacks carry no timing; /begin does."""
        challenge = {
            "challengeId": "NEWFMT",
            "mode": "qyz_world_boss_v1",
            "windowCount": 3,
            "windows": [],
            "attacks": [
                {"id": "a_1", "name": "青竹蜂云剑", "tone": "fire", "width": 58.0},
                {"id": "a_2", "name": "七焰扇", "tone": "sword", "width": 66.0},
                {"id": "a_3", "name": "阴罗幡火", "tone": "soul", "width": 74.0},
            ],
        }
        begin_response = {
            "startsInMs": 1500,
            "windows": [
                {"id": "w_1", "centerMs": 1800, "hitMs": 600, "perfectMs": 200},
                {"id": "w_2", "centerMs": 7400, "hitMs": 600, "perfectMs": 200},
                {"id": "w_3", "centerMs": 13000, "hitMs": 600, "perfectMs": 200},
            ],
        }
        # Challenge alone fails; begin response parses.
        with self.assertRaises(MiniAppBeastError):
            WorldBossMonitor._windows(challenge)
        self.assertEqual(len(WorldBossMonitor._windows(begin_response)), 3)

    def test_both_formats_fail_attaches_diagnostics(self):
        challenge = {
            "challengeId": "BADFMT",
            "windows": [],
            "attacks": [{"id": "a_1", "name": "X", "width": 58.0}],
        }
        sync = {"startsInMs": 1000, "windows": []}

        now = [1_000.0]
        async def sleep(seconds):
            now[0] += seconds
        async def fail_request(origin, path, payload, timeout):
            return sync
        monitor = WorldBossMonitor(FakeActor(), "main", post_json=fail_request,
                                   monotonic=lambda: now[0], sleep=sleep)

        async def go():
            with self.assertRaises(MiniAppBeastError) as ctx:
                await monitor._fight(
                    entry=extract_world_boss_entry(DummyMessage()),
                    init_data="init",
                    session_token="tok",
                    payload={"challenge": challenge},
                )
            await monitor.stop()
            return ctx.exception

        exc = asyncio.run(go())
        self.assertEqual(exc.code, "boss_windows_invalid")
        self.assertIn("challenge", exc.details)
        self.assertIn("begin_response", exc.details)


if __name__ == "__main__":
    unittest.main()
