import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniapp_beast import MiniAppBeastError
from world_boss_features import WorldBossMonitor


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

    def test_windows_dict_payload_rejected(self):
        # windows 字段不是 list 时安全失败（带诊断），不抛 TypeError
        challenge = {"challengeId": "ch-456", "windows": {"a": 1}}
        with self.assertRaises(MiniAppBeastError) as ctx:
            WorldBossMonitor._windows(challenge)
        self.assertEqual(ctx.exception.code, "boss_windows_invalid")
        self.assertIsInstance(getattr(ctx.exception, "details", None), dict)

    def test_new_time_ms_field_accepted(self):
        challenge = {
            "challengeId": "ch-789",
            "attacks": [
                {"attackId": "a1", "timeMs": 1000, "durationMs": 200},
                {"attackId": "a2", "timestampMs": 3000, "durationMs": 200},
            ],
        }
        windows = WorldBossMonitor._windows(challenge)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["centerMs"], 1000)
        self.assertEqual(windows[1]["centerMs"], 3000)


if __name__ == "__main__":
    unittest.main()
