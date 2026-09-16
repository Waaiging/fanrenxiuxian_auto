from collections import OrderedDict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import automation_settings as settings
import dashboard_server as dashboard
from dashboard_command_catalog import classify_command


class NangongqueDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for module, name, value in (
            (settings, "AUTOMATION_SETTINGS_FILE", Path(self.temp.name) / "settings.json"),
            (dashboard, "DASHBOARD_PASSWORD", "fixture-password"),
            (dashboard, "USER_NAMES", ("fixture-admin",)),
            (dashboard, "LOGIN_FAILURES", OrderedDict()),
        ):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.current = settings.default_automation_settings()
        settings.AUTOMATION_SETTINGS_FILE.write_text(json.dumps(self.current), encoding="utf-8")
        self.client = TestClient(dashboard.app)
        self.addCleanup(self.client.close)
        self.auth = ("fixture-admin", "fixture-password")

    def payload(self, participants):
        return {"world_boss_participants": self.current["world_boss"]["participants"],
                "mulan_support_mode": "护阵", "nangongque_boss_participants": participants}

    def test_auth_required_and_identity_selection_round_trips_independently(self):
        self.assertEqual(self.client.post("/api/automation-settings", json=self.payload([])).status_code, 401)
        response = self.client.post("/api/automation-settings", json=self.payload(["sub|主魂"]), auth=self.auth)
        self.assertTrue(response.json()["success"], response.text)
        value = settings.load_automation_settings()
        self.assertEqual(value["world_boss"], self.current["world_boss"])
        self.assertEqual(value["wind_thunder"], self.current["wind_thunder"])
        self.assertEqual(value["nangongque_boss"]["participants"], ["sub|主魂"])
        response = self.client.get("/api/automation-settings", auth=self.auth)
        data = response.json()
        self.assertEqual(data["nangongque_boss"]["selected_count"], 1)
        selected = [i["key"] for a in data["nangongque_boss"]["accounts"] for i in a["identities"] if i["selected"]]
        self.assertEqual(selected, ["sub|主魂"])

    def test_invalid_selection_does_not_change_saved_settings(self):
        before = settings.AUTOMATION_SETTINGS_FILE.read_bytes()
        for selection in ("invalid", ["main|主魂", "main|无咎子"], ["main|unknown"]):
            with self.subTest(selection=selection):
                response = self.client.post("/api/automation-settings", json=self.payload(selection), auth=self.auth)
                self.assertFalse(response.json()["success"])
                self.assertEqual(settings.AUTOMATION_SETTINGS_FILE.read_bytes(), before)

    def test_command_catalog_exposes_separate_world_event_control(self):
        classification = classify_command("miniapp:nangongque-boss")
        self.assertEqual(classification["subcategory"], "世界活动")
        self.assertFalse(classification["pending"])


if __name__ == "__main__":
    unittest.main()
