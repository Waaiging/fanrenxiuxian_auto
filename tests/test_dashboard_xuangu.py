from collections import OrderedDict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import automation_settings as settings
import dashboard_server as dashboard
import xuangu_quiz_features as quiz


class XuanguDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for module, name, value in (
            (quiz, "STATE_FILE", Path(self.temp.name) / "quiz.json"),
            (settings, "AUTOMATION_SETTINGS_FILE", Path(self.temp.name) / "settings.json"),
            (dashboard, "DASHBOARD_PASSWORD", "test-password"),
            (dashboard, "USER_NAMES", ("quiz-admin",)),
            (dashboard, "LOGIN_FAILURES", OrderedDict()),
        ):
            patcher = patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.question = quiz.Question("玄骨考校", "player", "新加入的题目？",
                                      {"A": "甲", "B": "乙", "C": "丙", "D": "丁"})
        pending = {"key": self.question.key, "event_type": self.question.kind,
                   "question": self.question.text, "options": self.question.options,
                   "option_versions": [self.question.options], "status": "unknown"}
        quiz._update(lambda data: data["pending"].update({self.question.key: pending}))
        self.client = TestClient(dashboard.app)
        self.addCleanup(self.client.close)
        self.auth = ("quiz-admin", "test-password")

    def test_answers_require_authentication_and_only_accept_observed_options(self):
        payload = {"question_key": self.question.key, "answer": "丙"}
        before = quiz._state()
        self.assertEqual(self.client.post("/api/xuangu-quiz/answers", json=payload).status_code, 401)
        self.assertEqual(quiz._state(), before)
        for invalid in ({**payload, "answer": "不存在的答案"}, {**payload, "question_key": "invalid"},
                        {**payload, "question_key": "0" * 24}, {**payload, "answer": ["丙"]}):
            with self.subTest(payload=invalid):
                response = self.client.post("/api/xuangu-quiz/answers", json=invalid, auth=self.auth)
                self.assertFalse(response.json()["success"])
                self.assertEqual(quiz._state(), before)

    def test_confirmed_answer_is_persisted_and_pending_list_refreshes(self):
        response = self.client.post("/api/xuangu-quiz/answers",
                                   json={"question_key": self.question.key, "answer": "丙"}, auth=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["runtime"]["pending_count"], 0)
        self.assertEqual(response.json()["runtime"]["known_count"], 15)
        self.assertEqual(quiz.answer_for(self.question), "C")
        self.assertEqual(quiz._state()["answers"][self.question.key]["confirmed_by"], "quiz-admin")

    def test_settings_api_enables_selected_identity_without_changing_wings(self):
        current = settings.default_automation_settings()
        current["wind_thunder"] = {"enabled": True, "participants": ["sub|主魂"]}
        settings.AUTOMATION_SETTINGS_FILE.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        payload = {"world_boss_participants": current["world_boss"]["participants"],
                   "mulan_support_mode": current["mulan_support"]["mode"],
                   "xuangu_quiz": {"enabled": True, "participants": ["main|主魂"]}}
        response = self.client.post("/api/automation-settings", json=payload, auth=self.auth)
        self.assertTrue(response.json()["success"], response.text)
        self.assertTrue(settings.xuangu_quiz_enabled("main", "主魂"))
        self.assertFalse(settings.xuangu_quiz_enabled("sub", "主魂"))
        self.assertEqual(settings.load_automation_settings()["wind_thunder"], current["wind_thunder"])
        response = self.client.get("/api/automation-settings", auth=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["xuangu_quiz"]["enabled"])
        self.assertEqual(response.json()["xuangu_quiz"]["runtime"]["known_count"], 14)


if __name__ == "__main__":
    unittest.main()
