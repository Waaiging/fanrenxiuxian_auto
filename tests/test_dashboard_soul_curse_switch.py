import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds


def _settings(enabled, identities):
    return {"enabled": enabled, "identities": identities}


class SoulCurseDashboardSwitchTests(unittest.TestCase):
    """封魂咒关闭时仍可配置单条指令，但不显示为可执行任务。"""

    def setUp(self):
        self.state = {
            "soul_curse": {"last_detail": "测试", "next_chain_time": "2026-01-01 00:00:00"},
            "soul_curse_assist": {"commission_id": "C1"},
        }

    def test_switch_off_keeps_individual_commands_inactive(self):
        with patch(
            "dashboard_server.soul_curse_identity_enabled_for_dashboard",
            return_value=False,
        ):
            rows = ds.soul_curse_publisher_commands(self.state, account="main", identity="主魂")
        self.assertGreater(len(rows), 1)
        self.assertIn("已关闭", rows[0].get("status", "") + rows[0].get("detail", "") + rows[0].get("name", ""))
        commands = {row["command"].split()[0] for row in rows[1:]}
        self.assertTrue({".探望南宫婉", ".推演封魂咒", ".护持神魂", ".发布解咒委托"}.issubset(commands))
        for row in rows[1:]:
            self.assertEqual(row["status"], "链路未启用")
            self.assertFalse(row["actionable"])
            self.assertIsNone(row["next_seconds"])

    def test_switch_on_rendes_full_chain(self):
        with patch(
            "dashboard_server.soul_curse_identity_enabled_for_dashboard",
            return_value=True,
        ):
            rows = ds.soul_curse_publisher_commands(self.state, account="main", identity="主魂")
        commands = [str(r.get("command", "")) for r in rows]
        self.assertTrue(any("探望南宫婉" in c for c in commands))
        self.assertTrue(any("封魂咒推演" in c or "推演" in c for c in commands))

    def test_assist_switch_off_keeps_target_commands_inactive(self):
        with patch(
            "dashboard_server.soul_curse_identity_enabled_for_dashboard",
            return_value=False,
        ):
            rows = ds.soul_curse_assist_commands(self.state, account="main", identity="厚土")
        self.assertEqual(len(rows), 5)
        self.assertIn("厚土", str(rows[0].get("label", "")))
        for row in rows[1:]:
            self.assertEqual(row["status"], "链路未启用")
            self.assertFalse(row["actionable"])
            self.assertIsNone(row["next_seconds"])

    def test_assist_switch_on_renders_chain(self):
        with patch(
            "dashboard_server.soul_curse_identity_enabled_for_dashboard",
            return_value=True,
        ):
            rows = ds.soul_curse_assist_commands(self.state, account="main", identity="厚土")
        self.assertGreater(len(rows), 1)

    def test_dashboard_switch_reads_settings_file(self):
        """settings 文件缺省（不存在）时默认关闭。"""
        self.assertFalse(
            ds.soul_curse_identity_enabled_for_dashboard("main", "主魂")
        )


if __name__ == "__main__":
    unittest.main()
