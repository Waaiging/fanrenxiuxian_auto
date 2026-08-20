import unittest
import logging
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient

import dashboard_server
from log_utils import CommandLogFilter


class DashboardProcessTests(unittest.TestCase):
    def test_dashboard_session_is_signed_and_expires(self):
        with patch.object(dashboard_server, "DASHBOARD_PASSWORD", "test-password"), patch.object(
            dashboard_server, "DASHBOARD_SESSION_SECRET", "test-session-secret"
        ), patch.object(dashboard_server, "DASHBOARD_SESSION_DAYS", 180):
            token = dashboard_server.create_dashboard_session("admin", now=1_000)

            self.assertEqual(dashboard_server.dashboard_session_user(token, now=1_001), "admin")
            self.assertEqual(dashboard_server.dashboard_session_user(token + "x", now=1_001), "")
            self.assertEqual(
                dashboard_server.dashboard_session_user(token, now=1_000 + 180 * 86400),
                "",
            )

    def test_dashboard_login_next_path_rejects_external_redirects(self):
        self.assertEqual(dashboard_server._safe_next_path("/api/status"), "/api/status")
        self.assertEqual(dashboard_server._safe_next_path("https://example.com"), "/")
        self.assertEqual(dashboard_server._safe_next_path("//example.com"), "/")

    def test_dashboard_browser_login_persists_until_logout(self):
        with patch.object(dashboard_server, "DASHBOARD_PASSWORD", "test-password"), patch.object(
            dashboard_server, "DASHBOARD_SESSION_SECRET", "test-session-secret"
        ), patch.object(dashboard_server, "DASHBOARD_COOKIE_SECURE", False):
            with TestClient(dashboard_server.app, follow_redirects=False) as client:
                anonymous = client.get("/")
                self.assertEqual(anonymous.status_code, 303)
                self.assertEqual(anonymous.headers["location"], "/login")

                rejected = client.post(
                    "/login",
                    json={"username": "admin", "password": "wrong", "next_path": "/"},
                )
                self.assertEqual(rejected.status_code, 401)

                accepted = client.post(
                    "/login",
                    json={"username": "admin", "password": "test-password", "next_path": "/"},
                )
                self.assertEqual(accepted.status_code, 303)
                self.assertIn(dashboard_server.DASHBOARD_SESSION_COOKIE, accepted.headers["set-cookie"])
                self.assertEqual(client.get("/").status_code, 200)

                logged_out = client.post("/logout")
                self.assertEqual(logged_out.status_code, 303)
                self.assertEqual(client.get("/").status_code, 303)

    def test_dashboard_api_keeps_basic_auth_compatibility(self):
        with patch.object(dashboard_server, "DASHBOARD_PASSWORD", "test-password"):
            with TestClient(dashboard_server.app) as client:
                response = client.get("/api/red-packets", auth=("admin", "test-password"))
                self.assertEqual(response.status_code, 200)

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

    def test_fishing_result_is_visible_and_tagged_in_runtime_logs(self):
        raw = (
            "2026-08-20 12:10:00,000 [INFO] IN [Mini App | 寻真子]:\n"
            "Mini App fishing [灵眼寒潭 | 妖血饵 | 妖腥窝]: "
            "提竿成功：【银须灵鲢】 灵鱼 2.15斤"
        )
        entry = {"text": raw, "lines": raw.splitlines()}

        self.assertTrue(dashboard_server.is_dashboard_visible_log_entry(entry))
        self.assertEqual(
            dashboard_server.extract_log_entry_tags(entry),
            {dashboard_server.FISHING_LOG_TAG},
        )
        decorated = dashboard_server.decorate_log_entries([entry])[0]
        self.assertEqual(decorated["identity"], "")
        self.assertEqual(decorated["tags"], {dashboard_server.FISHING_LOG_TAG})
        self.assertEqual(
            dashboard_server.filter_log_entries(
                [decorated],
                tag=dashboard_server.FISHING_LOG_TAG,
            ),
            [decorated],
        )

    def test_fishing_summary_is_tagged_in_runtime_logs(self):
        raw = (
            "2026-08-20 12:20:00,000 [INFO] IN [Mini App | 主魂]:\n"
            "灵溪垂钓汇总（共 10 竿）\n合计：成功 9/10 竿"
        )
        entry = {"text": raw, "lines": raw.splitlines()}

        self.assertTrue(dashboard_server.is_dashboard_visible_log_entry(entry))
        self.assertEqual(
            dashboard_server.extract_log_entry_tags(entry),
            {dashboard_server.FISHING_LOG_TAG},
        )

    def test_fishing_tag_does_not_leak_to_following_runtime_entry(self):
        fishing = {
            "text": "2026-08-20 12:20:00,000 [INFO] IN [Mini App | 主魂]:\n"
            "灵溪垂钓汇总（共 10 竿）",
            "lines": [
                "2026-08-20 12:20:00,000 [INFO] IN [Mini App | 主魂]:",
                "灵溪垂钓汇总（共 10 竿）",
            ],
        }
        following = {
            "text": "2026-08-20 12:20:01,000 [INFO] IN [Mini App | 主魂]:\n"
            "普通状态回复",
            "lines": [
                "2026-08-20 12:20:01,000 [INFO] IN [Mini App | 主魂]:",
                "普通状态回复",
            ],
        }
        decorated = dashboard_server.decorate_log_entries([fishing, following])
        self.assertEqual(decorated[0]["tags"], {dashboard_server.FISHING_LOG_TAG})
        self.assertNotIn(dashboard_server.FISHING_LOG_TAG, decorated[1]["tags"])


if __name__ == "__main__":
    unittest.main()
