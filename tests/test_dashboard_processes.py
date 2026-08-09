import unittest
import logging
from types import SimpleNamespace
from unittest.mock import patch

import dashboard_server
from log_utils import CommandLogFilter


class DashboardProcessTests(unittest.TestCase):
    def test_restricted_process_matches_only_its_account(self):
        xiaohao = "/home/ubuntu/deploy/venv/bin/python red_packet_account.py --account xiaohao"
        waaiging = "/home/ubuntu/deploy/venv/bin/python red_packet_account.py --account=waaiging"

        self.assertTrue(dashboard_server.account_process_command_matches("xiaohao", xiaohao))
        self.assertFalse(dashboard_server.account_process_command_matches("waaiging", xiaohao))
        self.assertTrue(dashboard_server.account_process_command_matches("waaiging", waaiging))
        self.assertFalse(dashboard_server.account_process_command_matches("xiaohao", waaiging))

    def test_restricted_process_keeps_legacy_script_compatibility(self):
        self.assertTrue(
            dashboard_server.account_process_command_matches(
                "xiaohao",
                "/home/ubuntu/deploy/venv/bin/python cultivator_xiaohao.py",
            )
        )
        self.assertTrue(
            dashboard_server.account_process_command_matches(
                "waaiging",
                "/home/ubuntu/deploy/venv/bin/python cultivator_waaiging.py",
            )
        )

    def test_account_process_pids_filters_shared_worker_by_account(self):
        output = "\n".join(
            [
                "101 /home/ubuntu/deploy/venv/bin/python red_packet_account.py --account xiaohao",
                "102 /home/ubuntu/deploy/venv/bin/python red_packet_account.py --account waaiging",
                "103 /home/ubuntu/deploy/venv/bin/python cultivator_xiaohao.py",
                "104 tmux new-session python red_packet_account.py --account xiaohao",
            ]
        )
        completed = SimpleNamespace(returncode=0, stdout=output)

        with patch.object(dashboard_server.subprocess, "run", return_value=completed):
            self.assertEqual(dashboard_server.account_process_pids("xiaohao"), [101, 103])
            self.assertEqual(dashboard_server.account_process_pids("waaiging"), [102])

    def test_tianji_total_summary_is_visible_without_per_round_transport_logs(self):
        summary = "2026-08-08 12:00:00,000 [INFO] 刷天机值完成：总数 20"
        entry = {"text": summary, "lines": [summary]}

        self.assertTrue(dashboard_server.is_dashboard_visible_log_entry(entry))

        routine = "2026-08-08 12:00:00,000 [INFO] routine scheduler message"
        routine_entry = {"text": routine, "lines": [routine]}
        self.assertFalse(dashboard_server.is_dashboard_visible_log_entry(routine_entry))

        record = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "刷天机值完成：总数 %s", (20,), None
        )
        self.assertTrue(CommandLogFilter().filter(record))

    def test_edited_bot_reply_is_visible_in_dashboard_logs(self):
        historical = (
            "2026-08-08 12:47:00,943 [INFO] "
            "🔵 IN [edited 756018] 韩天尊(@xlqlcy_bot):\n【探寻成功】"
        )
        historical_entry = {
            "text": historical,
            "lines": historical.splitlines(),
        }
        attributed = (
            "2026-08-08 12:47:00,943 [INFO] "
            "🔵 IN [.探寻裂缝 edited 756018] 韩天尊(@xlqlcy_bot):\n【探寻成功】"
        )
        attributed_entry = {
            "text": attributed,
            "lines": attributed.splitlines(),
        }

        self.assertTrue(dashboard_server.is_dashboard_visible_log_entry(historical_entry))
        self.assertTrue(dashboard_server.is_dashboard_visible_log_entry(attributed_entry))


if __name__ == "__main__":
    unittest.main()
