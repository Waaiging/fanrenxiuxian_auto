import copy
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_server as dashboard
from dashboard_command_catalog import (
    apply_command_classifications,
    classify_command,
    command_catalog_payload,
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 8, 15, 30, tzinfo=tz)


class CommandClassificationTests(unittest.TestCase):
    def test_confirmed_categories_ignore_old_group_and_account_assumptions(self):
        for command, category, subcategory in (
            (".探寻裂缝", "realm", "元婴及以上"),
            (".元婴出窍", "realm", "元婴及以上"),
            (".双修 温养", "sect", "合欢宗"),
            (".问道", "sect", "元婴宗"),
            (".元婴闭关", "sect", "元婴宗"),
            (".改换星移 @someone", "sect", "星宫"),
            (".化功为煞 10000", "sect", "阴罗宗"),
            ("miniapp:spirit-beast:xiaohao", "sect", "万灵宗"),
            ("miniapp:journey-deep", "general", "游历"),
            ("miniapp:fishing", "general", "垂钓"),
            (".掌天瓶 凝液", "general", "法宝"),
        ):
            with self.subTest(command=command):
                result = classify_command(command, group="旧分类", custom=True)
                self.assertEqual((result["category"], result["subcategory"]), (category, subcategory))
                self.assertFalse(result["pending"])

    def test_unknown_limits_and_similar_commands_remain_pending(self):
        for command in (".元神修炼", ".第二元神", ".启阵", ".助阵", ".双修 其他", ".观星测试"):
            with self.subTest(command=command):
                result = classify_command(command, group="星宫")
                self.assertEqual(result["subcategory"], "归属待确认")
                self.assertTrue(result["pending"])
        self.assertEqual(classify_command(".未知指令", custom=True)["subcategory"], "自定义")

    def test_classification_is_the_only_panel_change(self):
        panel = {"identity": "主魂", "commands": [{
            "command": ".探寻裂缝", "label": "探寻裂缝", "group": "通用",
            "control_key": ".探寻裂缝", "control_disabled": True,
            "at": "2026-09-09 01:02:03", "next_seconds": 456,
            "schedule_type": "cooldown", "execution_channel": "group",
            "status": "已暂停", "tone": "paused", "actionable": True,
        }]}
        original = copy.deepcopy(panel)
        result = apply_command_classifications(panel)
        self.assertEqual(result["commands"][0].pop("classification")["category"], "realm")
        self.assertEqual(result, original)

    def test_real_panel_builders_preserve_all_existing_fields(self):
        state = {
            "sect_name": "天星宗", "done": [],
            "miniapp_route_active": True,
            "next_rift_search_time": "2026-09-09 01:02:03",
            "avatars": {"无咎子": {}, "素缘子": {}, "厚土": {}, "问心子": {}, "素心子": {}},
        }
        original = copy.deepcopy(state)
        controls = {"main": {"主魂": {".探寻裂缝": {"disabled": True}}}}
        custom = {"waaiging": {"主魂": [{
            "id": "custom-dual", "command": ".双修 温养", "label": "温养", "group": "自定义",
            "schedule_enabled": True, "interval_minutes": 60,
        }]}}
        with patch.object(dashboard, "datetime", FixedDateTime), \
                patch.object(dashboard, "load_command_controls", return_value=controls), \
                patch.object(dashboard, "load_custom_commands", return_value=custom):
            for account in ("main", "sub", "xiaohao", "waaiging"):
                with self.subTest(account=account):
                    with patch.object(dashboard, "apply_command_classifications", side_effect=lambda panel: panel):
                        before = dashboard.build_command_panels(account, copy.deepcopy(state))
                    after = dashboard.build_command_panels(account, state)
                    for panel in after:
                        for row in panel["commands"]:
                            self.assertIn("classification", row)
                            row.pop("classification")
                    self.assertEqual(after, before)
        self.assertEqual(state, original)

    def test_catalog_has_unique_ids_no_schedules_and_fresh_payloads(self):
        payload = command_catalog_payload()
        self.assertEqual(len({item["id"] for item in payload["entries"]}), len(payload["entries"]))
        for item in payload["entries"]:
            self.assertNotIn("schedule_enabled", item)
            self.assertNotIn("control_key", item)
            self.assertNotIn("next_seconds", item)
            category = next(group for group in payload["categories"] if group["id"] == item["classification"]["category"])
            self.assertIn(item["classification"]["subcategory"], category["subcategories"])
        by_id = {item["id"]: item for item in payload["entries"]}
        self.assertEqual(by_id["heart-trial"]["lifecycle"], "opt_in")
        self.assertEqual(by_id["concubine-search"]["lifecycle"], "disabled")
        self.assertEqual(by_id["legacy-pagoda"]["lifecycle"], "retired")
        self.assertEqual(by_id["beast-seek"]["channel"], "Mini App")
        payload["entries"][0]["classification"]["category"] = "changed"
        self.assertEqual(command_catalog_payload()["entries"][0]["classification"]["category"], "sect")

    def test_catalog_endpoint_is_authenticated_and_read_only(self):
        with patch.object(dashboard, "DASHBOARD_PASSWORD", "test-password"), TestClient(dashboard.app) as client:
            self.assertEqual(client.get("/api/command-catalog").status_code, 401)
            with patch.dict(dashboard.app.dependency_overrides, {dashboard.authenticate: lambda: "test"}):
                response = client.get("/api/command-catalog")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), command_catalog_payload())
                self.assertEqual(client.post("/api/command-catalog", json={}).status_code, 405)


class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inline = "src" not in dict(attrs)

    def handle_endtag(self, tag):
        if tag == "script":
            self.inline = False

    def handle_data(self, data):
        if self.inline:
            self.scripts.append(data)


class DashboardScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript syntax validation")
    def test_inline_scripts_parse(self):
        parser = InlineScripts()
        parser.feed((Path(__file__).resolve().parents[1] / "dashboard.html").read_text(encoding="utf-8"))
        self.assertTrue(parser.scripts)
        for script in parser.scripts:
            result = subprocess.run(
                ["node", "-e", "new (require('node:vm').Script)(require('node:fs').readFileSync(0, 'utf8'))"],
                input=script, text=True, encoding="utf-8", capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
