import unittest
from types import SimpleNamespace
from unittest.mock import patch

import dashboard_server


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


if __name__ == "__main__":
    unittest.main()
