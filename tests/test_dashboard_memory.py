"""Memory bounds and behavior of the lightweight Dashboard and log maintenance."""
import asyncio
from datetime import datetime, timedelta
import gc
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote
import weakref

from fastapi.testclient import TestClient

import dashboard_server as dashboard
import log_utils


class DashboardLogMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(patch.object(dashboard, "CONFIG_DIR", str(self.directory)))
        self.path = self.directory / "cultivator.log"

    @staticmethod
    def entry(index, *, bot=True):
        return (
            f"2026-09-14 09:00:00,000 [INFO] IN [.探寻裂缝 reply {index}] "
            f"[TG sender={'bot' if bot else 'human'} chat=-100123456 msg={index} attention=reply] "
            f"韩天尊(@hantianzun24_bot):\n测试回执 {index}：修为 +7\n"
        )

    def write_entries(self, count, *, alternate=False):
        with self.path.open("w", encoding="utf-8") as handle:
            for index in range(count):
                handle.write(self.entry(index, bot=not alternate or index % 2 == 0))

    def test_filtered_pages_keep_exact_counts_and_do_not_overlap(self):
        self.write_entries(137, alternate=True)
        expected = [self.entry(index).rstrip() for index in range(0, 137, 2)]
        pages = []
        before = None
        while True:
            page = dashboard.get_log_page("main", before=before, limit=20, sender="bot", q="测试")
            self.assertEqual(page["total"], 137)
            self.assertEqual(page["matched"], 69)
            pages.insert(0, page["entries"])
            if not page["has_more"]:
                break
            self.assertLess(page["next_before"], before if before is not None else 70)
            before = page["next_before"]
        self.assertEqual([entry for page in pages for entry in page], expected)
        self.assertEqual(dashboard.get_log_page("main", before=0, sender="bot")["entries"], [])
        self.assertEqual(dashboard.get_log_page("main", before=-2, sender="bot")["entries"], [])
        latest = dashboard.get_log_page("main", before=10000, limit=20, sender="bot")
        self.assertEqual(latest["entries"], expected[-20:])

    def test_stream_retains_command_context_across_entries(self):
        self.path.write_text(
            "2026-09-14 09:00:00,000 [INFO] OUT [无咎子]: .探寻裂缝\n"
            "2026-09-14 09:00:01,000 [INFO] IN [.探寻裂缝 reply 12] 韩天尊(@hantianzun24_bot):\n探索完成\n",
            encoding="utf-8",
        )
        entries = list(dashboard.iter_account_log_entries("main"))
        self.assertEqual(entries[-1]["identity"], "无咎子")
        page = dashboard.get_log_page("main", tag=".探寻裂缝", kind="in")
        self.assertEqual(len(page["entries"]), 1)
        self.assertIn("探索完成", page["content"])

    def test_read_snapshot_does_not_include_later_appends(self):
        self.write_entries(2)
        entries = dashboard.iter_account_log_entries("main")
        first = next(entries)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self.entry(2))
        self.assertEqual(len([first, *entries]), 2)
        self.assertEqual(len(list(dashboard.iter_account_log_entries("main"))), 3)

    def test_empty_missing_unicode_and_multiline_logs(self):
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(dashboard.get_log_page("main", q="测试")["matched"], 0)
        self.path.write_text(self.entry(1) + "补充内容 😀\n", encoding="utf-8")
        page = dashboard.get_log_page("main", sender="bot", q="😀")
        self.assertEqual(page["matched"], 1)
        self.assertIn("补充内容 😀", page["content"])
        self.path.unlink()
        missing = dashboard.get_log_page("main", q="测试")
        self.assertFalse(missing["has_more"])
        self.assertIn("不存在", missing["content"])

    def test_log_tag_counts_agree_with_visible_records(self):
        self.write_entries(4, alternate=True)
        tags = dashboard.get_log_tags("main")
        counts = {row["tag"]: row["count"] for row in tags["tags"]}
        self.assertEqual(counts[".探寻裂缝"], 2)
        self.assertEqual(counts[dashboard.OTHER_LOG_TAG], 2)

    def test_filter_memory_stays_bounded_as_history_grows(self):
        peaks = []
        for count in (800, 4800):
            self.write_entries(count)
            gc.collect()
            tracemalloc.start()
            try:
                page = dashboard.get_log_page("main", q="测试", sender="bot", limit=20)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(page["matched"], count)
            self.assertEqual(len(page["entries"]), 20)
            peaks.append(peak)
        self.assertLess(peaks[1], 2 * 1024 * 1024)
        self.assertLess(peaks[1], peaks[0] + 512 * 1024)


class DashboardStatusMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(patch.object(dashboard, "CONFIG_DIR", str(self.directory)))
        self.enterContext(patch.dict(dashboard.app.dependency_overrides, {dashboard.authenticate: lambda: "test"}))
        for name in ("STATUS_CACHE", "LOG_PAGE_CACHE", "COMMAND_RECORD_CACHE",
                     "COMMAND_RECORD_ENDPOINT_CACHE", "DAILY_REWARD_ENDPOINT_CACHE",
                     "MESSAGE_HEALTH_CACHE", "RESOURCE_STATS_CACHE"):
            self.enterContext(patch.object(dashboard, name, {}))
        for name, value in (("account_process_pids", []), ("account_runtime_info", {}),
                            ("build_command_panels", []), ("account_profile_usernames", {}),
                            ("dashboard_runtime_info", {})):
            self.enterContext(patch.object(dashboard, name, new=lambda *args, _result=value, **kwargs: _result))
        self.enterContext(patch.object(dashboard, "FISHING_AUTOMATION_ENABLED", False))

    def test_status_uses_state_without_reading_or_recreating_cultivation_statistics(self):
        states = []
        class AccountState(dict):
            pass
        def get_state(name):
            state = AccountState(miniapp_current_exp=0, miniapp_total_exp=100, level="元婴初期",
                                 history=[{"text": "已同步资料" * 50} for _ in range(40)])
            states.append(weakref.ref(state))
            return state
        original_open = open
        def guarded_open(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and (
                str(path).endswith(".log") or Path(path).name == "cultivation_stats_cache.json"
            ):
                raise AssertionError("The status endpoint must not read cultivation logs/cache")
            return original_open(path, *args, **kwargs)
        with patch.object(dashboard, "get_state", side_effect=get_state), patch("builtins.open", side_effect=guarded_open):
            with TestClient(dashboard.app) as client:
                response = client.get("/api/status")
                self.assertEqual(response.status_code, 200)
                account = response.json()["accounts"]["main"]
                self.assertEqual(account["state"]["miniapp_current_exp"], 0)
                self.assertNotIn("cultivation", account)
                self.assertIsInstance(dashboard.STATUS_CACHE["data"], bytes)
                gc.collect()
                self.assertTrue(all(ref() is None for ref in states))
                second = client.get("/api/status")
                self.assertEqual(response.content, second.content)
                self.assertEqual(len(states), 4)
        self.assertFalse((self.directory / "cultivation_stats_cache.json").exists())

    def test_idle_caches_expire_and_busy_build_is_not_blocked(self):
        dashboard.STATUS_CACHE.update(at=1, data=b"old")
        dashboard.LOG_PAGE_CACHE.update(old={"at": 1, "data": {}}, recent={"at": 99, "data": {}})
        dashboard.STATUS_LOCK.acquire()
        try:
            dashboard.clear_expired_dashboard_caches(100)
            self.assertEqual(dashboard.STATUS_CACHE["data"], b"old")
            self.assertEqual(list(dashboard.LOG_PAGE_CACHE), ["recent"])
        finally:
            dashboard.STATUS_LOCK.release()
        dashboard.clear_expired_dashboard_caches(100)
        self.assertEqual(dashboard.STATUS_CACHE, {})

    def test_lifespan_clears_expired_responses_without_another_request(self):
        dashboard.STATUS_CACHE.update(at=1, data=b"old")
        with patch.object(dashboard, "DASHBOARD_CACHE_SWEEP_SECONDS", 0.01):
            with TestClient(dashboard.app):
                deadline = time.monotonic() + 2
                while dashboard.STATUS_CACHE and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(dashboard.STATUS_CACHE, {})

    def test_command_records_stream_rows_and_keep_deduplication(self):
        path = self.directory / dashboard.MESSAGE_EVENTS_DB_FILE
        original_connect = sqlite3.connect
        with original_connect(path) as conn:
            conn.execute("CREATE TABLE command_ledger (account, chat_id, command_msg_id, identity, command, source, sent_at)")
            conn.executemany(
                "INSERT INTO command_ledger VALUES ('main', ?, ?, '主魂', '.探寻裂缝', ?, ?)",
                [(1, 1, "manual", "2026-09-14 09:00:00"),
                 (1, 1, "auto", "2026-09-14 09:00:30"),
                 (1, 2, "auto", "2026-09-14 09:01:00"),
                 (2, 1, "auto", "2026-09-14 09:02:00"),
                 (2, 2, "auto", "invalid")],
            )
        conn.close()
        class Cursor(sqlite3.Cursor):
            def fetchall(self):
                raise AssertionError("Command history must not be materialized")
        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                return self.cursor(factory=Cursor).execute(sql, parameters)
        def connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=Connection)
        with patch.object(dashboard.sqlite3, "connect", side_effect=connect):
            result = dashboard.build_account_command_records("main")
        self.assertEqual(result["error"], "")
        row, = result["records"]
        self.assertEqual((row["count"], row["manual_count"], row["auto_count"]), (3, 1, 2))
        self.assertEqual(row["last_interval_seconds"], 60)
        self.assertEqual(len(row["recent_times"]), 3)


class DashboardDependencyTests(unittest.TestCase):
    def test_fresh_dashboard_does_not_load_telegram_protocol(self):
        script = "import sys,dashboard_server; assert not any(n=='telethon' or n.startswith('telethon.') for n in sys.modules)"
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Dashboard profile checks")
    def test_profile_uses_synced_values_without_daily_log_estimates(self):
        html = Path("dashboard.html").read_text(encoding="utf-8")
        names = ("escapeHtml", "formatNumber", "firstProfileText", "finiteProfileNumber",
                 "dashboardIdentityProfile", "renderAccountProfile")
        functions = []
        for name in names:
            match = re.search(r"^        function " + name + r"\([\s\S]+?^        }", html, re.MULTILINE)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        script = "const assert=require('node:assert/strict');\n" + "\n".join(functions) + r'''
const state={miniapp_current_exp:0, miniapp_total_exp:100, current_exp:90, total_exp:100,
             miniapp_cultivation_level:'元婴初期', miniapp_spirit_root:'木灵根', miniapp_sect_name:'天星宗'};
const rendered=renderAccountProfile(dashboardIdentityProfile(state,'主魂',state));
for(const text of ['0 / 100','元婴初期','木灵根','天星宗']) assert.ok(rendered.includes(text),text);
assert.ok(!/今日修为|修为统计|统计到/.test(rendered));
assert.ok(renderAccountProfile({}).includes('修为未同步'));
assert.ok(!renderAccountProfile({level:'<script>x</script>'}).includes('<script>'));
'''
        result = subprocess.run([shutil.which("node"), "-"], input=script,
                                text=True, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("acc.cultivation", html)
        self.assertNotIn("查看修为明细", html)


class LazyTelegramRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_webview_refresh_still_builds_a_real_telegram_request(self):
        from telethon import types
        from telethon.tl.functions.messages import RequestMainWebViewRequest
        from miniapp_beast import request_webview_init_data
        requests = []
        init_data = "auth_date=1&user=test"
        class Client:
            async def get_entity(self, name):
                return types.User(id=1, access_hash=2, bot=True)
            async def get_input_entity(self, entity):
                return types.InputPeerUser(user_id=1, access_hash=2)
            async def __call__(self, request):
                requests.append(request)
                return SimpleNamespace(url="https://example.invalid/#tgWebAppData=" + quote(init_data))
        self.assertEqual(await request_webview_init_data(Client(), "bot", "entry"), init_data)
        self.assertIsInstance(requests[0], RequestMainWebViewRequest)
        self.assertEqual(requests[0].start_param, "entry")


class LogPruneMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "worker.log"

    def test_prune_keeps_multiline_records_and_open_handler_can_append(self):
        old = (datetime.now() - timedelta(days=8)).strftime(log_utils.TIME_FORMAT)
        now = datetime.now().strftime(log_utils.TIME_FORMAT)
        self.path.write_text(f"{old},000 [INFO] OUT .旧指令\n旧正文\n"
                             f"{now},000 [INFO] ordinary noise\n"
                             f"{now},000 [INFO] IN [reply 1] 回复\n保留正文 😀\n"
                             f"{now},000 [WARNING] 保留告警\n", encoding="utf-8")
        logger = logging.getLogger("tests.dashboard.memory.prune")
        handler = logging.FileHandler(self.path, encoding="utf-8")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(handler.close)
        inode = self.path.stat().st_ino
        log_utils.prune_log_file(self.path)
        logger.warning("after prune")
        handler.flush()
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("旧正文", text)
        self.assertNotIn("ordinary noise", text)
        self.assertIn("保留正文 😀", text)
        self.assertIn("保留告警", text)
        self.assertIn("after prune", text)
        self.assertEqual(inode, self.path.stat().st_ino)

    def test_pruning_does_not_keep_the_log_in_memory(self):
        now = datetime.now().strftime(log_utils.TIME_FORMAT)
        with self.path.open("w", encoding="utf-8") as handle:
            handle.write("2000-01-01 00:00:00,000 [INFO] OUT .过期\n")
            for _ in range(4000):
                handle.write(f"{now},000 [INFO] IN [reply] " + "正文" * 120 + "\n")
        tracemalloc.start()
        try:
            log_utils.prune_log_file(self.path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)
        with self.path.open(encoding="utf-8") as handle:
            self.assertEqual(sum(1 for _ in handle), 4000)


if __name__ == "__main__":
    unittest.main()
