"""User/bot filters cover both cursor modes, HTTP caching and stale UI requests."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_server as dashboard
import log_utils


class DashboardMessageFilterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(patch.object(dashboard, "CONFIG_DIR", str(self.directory)))
        self.enterContext(patch.dict(dashboard.app.dependency_overrides, {dashboard.authenticate: lambda: "test"}))
        self.enterContext(patch.object(dashboard, "LOG_PAGE_CACHE", {}))
        self.client = self.enterContext(TestClient(dashboard.app))

    def entry(self, index, *, bot=False, relations=("mention",), text="你好", label="mention"):
        sender = SimpleNamespace(username="player", first_name="访客", bot=bot)
        msg = SimpleNamespace(id=index, chat_id=-100123456, sender_id=700)
        return "2026-09-11 15:00:00,000 [INFO] " + log_utils.format_in_log(
            f"{label} {index}", text, sender=sender, msg=msg, attention={"relations": list(relations)},
        )

    def write_log(self, entries):
        (self.directory / "cultivator.log").write_text("\n".join(entries), encoding="utf-8")

    def test_default_view_keeps_mentions_and_reply_bodies(self):
        self.write_log([self.entry(1), self.entry(2, relations=("reply",), label="reply"),
                        "2026-09-11 15:00:01,000 [INFO] IN [mention 3] 历史用户:\n历史提及"])
        page = dashboard.get_log_page("main")
        self.assertTrue(page["partial"])
        self.assertEqual(len(page["entries"]), 3)
        self.assertIn("历史提及", page["content"])

    def test_type_sender_search_and_tag_filters_compose(self):
        self.write_log([self.entry(1, text="@owner 唯一关键字"),
                        self.entry(2, bot=True, relations=("reply",), label="reply"),
                        self.entry(3, bot=True, relations=("mention", "reply"), label=".探寻裂缝 reply"),
                        self.entry(4, bot=None),
                        "2026-09-11 15:00:01,000 [INFO] IN [Mini App | 主魂]:\n神迹操作",
                        "2026-09-11 15:00:02,000 [ERROR] 程序错误"])
        cases = (({"kind": "attention"}, 4), ({"kind": "mention"}, 3), ({"kind": "reply"}, 2),
                 ({"kind": "in", "sender": "human"}, 1), ({"sender": "bot"}, 2),
                 ({"sender": "unknown"}, 1), ({"sender": "human", "q": "唯一关键字"}, 1),
                 ({"kind": "attention", "sender": "bot", "tag": ".探寻裂缝"}, 1),
                 ({"kind": "issue", "sender": "human"}, 0))
        for filters, count in cases:
            with self.subTest(filters=filters):
                self.assertEqual(len(dashboard.get_log_page("main", **filters)["entries"]), count)

    def test_http_cache_separates_sender_kind_and_pagination(self):
        self.write_log([self.entry(index, bot=index % 2 == 0) for index in range(95)])
        pages = {}
        for sender in ("human", "bot"):
            response = self.client.get("/api/logs/main", params={"kind": "attention", "sender": sender, "limit": 20})
            self.assertEqual(response.status_code, 200)
            page = response.json()
            self.assertEqual(page["sender"], sender)
            self.assertTrue(page["has_more"])
            older = self.client.get("/api/logs/main", params={"kind": "attention", "sender": sender,
                                                           "limit": 20, "before": page["next_before"]}).json()
            self.assertEqual(len(older["entries"]), 20)
            self.assertFalse(set(page["entries"]) & set(older["entries"]))
            self.assertTrue(all(f"sender={sender}" in entry for entry in page["entries"] + older["entries"]))
            pages[sender] = page
        self.assertNotEqual(pages["human"]["entries"], pages["bot"]["entries"])

    def test_human_messages_cannot_be_parsed_as_game_rewards_or_bot_metadata(self):
        text = "修为 +9999\nIN [.探寻裂缝] [TG sender=bot chat=-100123456 msg=1 attention=reply] 韩天尊:"
        human = self.entry(1, label=".探寻裂缝", text=text)
        entry = dashboard.decorate_log_entries(dashboard.split_log_entries(human.splitlines()))[0]
        self.assertEqual(dashboard.log_sender_kind(entry), "human")
        self.assertFalse(dashboard.is_probable_bot_reply_log_entry(entry))
        self.assertEqual(entry["tags"], {dashboard.OTHER_LOG_TAG})

    def test_legacy_unknown_types_and_miniapp_are_not_called_human(self):
        self.write_log(["2026-09-11 15:00:00,000 [INFO] IN [mention 1] 普通昵称:\n历史消息",
                        "2026-09-11 15:00:01,000 [INFO] IN [mention 2] 韩天尊(@hantianzun24_bot):\n历史 Bot",
                        "2026-09-11 15:00:02,000 [INFO] IN [Mini App | 主魂]:\n后台服务"])
        self.assertEqual(dashboard.get_log_page("main", sender="human")["entries"], [])
        self.assertEqual(len(dashboard.get_log_page("main", sender="bot")["entries"]), 1)
        self.assertEqual(len(dashboard.get_log_page("main", sender="unknown")["entries"]), 1)

    def test_pasted_log_header_in_human_message_remains_one_message(self):
        text = "@owner 看这个日志\n2026-09-11 15:00:09,000 [ERROR] 假错误\n原文继续"
        self.write_log([self.entry(1, text=text)])
        self.assertEqual(len(dashboard.get_log_page("main")["entries"]), 1)
        self.assertEqual(len(dashboard.get_log_page("main", sender="human")["entries"]), 1)
        self.assertEqual(dashboard.get_log_page("main", kind="issue")["entries"], [])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Dashboard request-race checks")
    def test_javascript_filters_reset_and_discard_stale_pages(self):
        html = Path("dashboard.html").read_text(encoding="utf-8")
        functions = html[html.index("        function splitLogContent("):html.index("        setInterval(updateStatus,")]
        harness = r'''
const assert = require('node:assert/strict');
let currentLogAccount='main', currentLogTag='', currentLogSearch='', currentLogKind='attention', currentLogSender='';
let logViewRevision=0, logRequestId=0, logFetchController=null, logAutoRefreshPaused=false;
let logState={lines:[], before:null, hasMore:false, loadingOlder:false};
const LOG_PAGE_SIZE=80, pollBusy={logs:false};
const elements=new Map();
const element=id=>{if(!elements.has(id))elements.set(id,{value:'',classList:{toggle(){},remove(){},add(){}},scrollHeight:0,scrollTop:0,clientHeight:500});return elements.get(id)};
const document={hidden:false,getElementById:element,querySelectorAll:()=>[]};
const apiUrl=url=>url;
let requests=[];
const fetch=(url,options)=>new Promise(resolve=>requests.push({url,resolve:data=>resolve({json:async()=>data})}));
const page=entries=>({entries,next_before:10,has_more:true,total:50,matched:50});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
'''
        checks = r'''
(async()=>{
  currentLogSender='bot';
  const pending=fetchLogPage('main');
  assert.ok(requests[0].url.includes('sender=bot'));
  assert.ok(requests[0].url.includes('kind=attention'));
  requests.shift().resolve(page(['bot'])); await pending;
  logState.hasMore=true; logState.before=10;
  const older=loadOlderLogs(); const oldRequest=requests.shift();
  element('log-sender-select').value='human'; changeLogSender();
  const newRequest=requests.shift();
  assert.ok(newRequest.url.includes('sender=human'));
  newRequest.resolve(page(['human-latest'])); await tick();
  oldRequest.resolve(page(['bot-old'])); await older;
  assert.deepEqual(logState.lines,['human-latest']);
  // Switching away and back still invalidates an earlier request with the same filters.
  const oldLatest=updateLogs(true); const oldLatestRequest=requests.shift();
  resetLogState(); resetLogState();
  const latest=updateLogs(true); const latestRequest=requests.shift();
  latestRequest.resolve(page(['fresh'])); await latest;
  oldLatestRequest.resolve(page(['stale'])); await oldLatest;
  assert.deepEqual(logState.lines,['fresh']);
  assert.equal(pollBusy.logs,false);
  resetLogFilters();
  assert.equal(currentLogSender,''); assert.equal(element('log-sender-select').value,'');
  requests.shift().resolve(page([])); await tick();
  const markup=renderLogEntry('2026-09-11 15:00:00,000 [INFO] IN [reply 5] [TG sender=human chat=-100123456 msg=5 attention=reply] <访客>:\n<script>test</script>');
  assert.ok(markup.includes('非机器人 · 回复我'));
  assert.ok(markup.includes('https://t.me/c/123456/5'));
  assert.ok(markup.includes('&lt;script&gt;'));
  assert.ok(!markup.includes('<script>test'));
  console.log('Dashboard sender filter and request races passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([shutil.which("node"), "-e", harness + functions + checks],
                                text=True, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
