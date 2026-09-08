import base64
from collections import OrderedDict
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_server as dashboard


class DashboardAuthTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("DASHBOARD_PASSWORD", "test-password"),
            ("DASHBOARD_SESSION_SECRET", "test-signing-key"),
            ("USER_NAMES", ("admin", "道友")),
            ("LOGIN_FAILURES", OrderedDict()),
        ):
            value_patch = patch.object(dashboard, name, value)
            value_patch.start()
            self.addCleanup(value_patch.stop)

    def _signed(self, payload):
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        signature = hmac.new(b"test-signing-key", encoded.encode(), hashlib.sha256).hexdigest()
        return f"{encoded}.{signature}"

    def test_unicode_credentials_and_invalid_unicode_return_normally(self):
        with TestClient(dashboard.app) as client:
            for name, password in (("路人", "test-password"), ("admin", "错误"), ("admin", "\ud800")):
                with self.subTest(name=name, password=repr(password)):
                    response = client.post("/login", content=json.dumps({"username": name, "password": password}),
                                           headers={"Content-Type": "application/json"})
                    self.assertEqual(response.status_code, 401)
            response = client.post("/login", json={"username": "道友", "password": "test-password"},
                                   follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            token = response.cookies.get(dashboard.DASHBOARD_SESSION_COOKIE)
            self.assertEqual(dashboard.dashboard_session_user(token), "道友")
            cookie = response.headers["set-cookie"].lower()
            for attribute in ("secure", "httponly", "samesite=strict"):
                self.assertIn(attribute, cookie)

    def test_redirects_remain_local(self):
        with TestClient(dashboard.app) as client:
            for target in ("https://example.com", "//example.com", "/\\example.com", "/\t/example.com",
                           "/\r\n/example.com"):
                with self.subTest(target=target):
                    response = client.post("/login", json={"password": "test-password", "next_path": target},
                                           follow_redirects=False)
                    self.assertEqual(response.headers["location"], "/")
            response = client.post("/login", json={"password": "test-password", "next_path": "/?tab=logs"},
                                   follow_redirects=False)
            self.assertEqual(response.headers["location"], "/?tab=logs")

    def test_session_rejects_malformed_expired_and_future_payloads(self):
        valid = {"u": "admin", "iat": 100, "exp": 200}
        self.assertEqual(dashboard.dashboard_session_user(self._signed(valid), now=150), "admin")
        invalid = [[], None, "admin", {**valid, "u": []}, {**valid, "iat": 151},
                   {**valid, "exp": 150}, {**valid, "iat": True}, {**valid, "iat": "100"},
                   {**valid, "exp": 100 + dashboard.DASHBOARD_SESSION_DAYS * 86400 + 1}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(dashboard.dashboard_session_user(self._signed(payload), now=150), "")
        token = self._signed(valid)
        self.assertEqual(dashboard.dashboard_session_user(token + "0", now=150), "")
        with patch.object(dashboard, "DASHBOARD_PASSWORD", ""):
            self.assertEqual(dashboard.dashboard_session_user(token, now=150), "")

    def test_login_and_basic_auth_share_limit_and_ignore_forwarded_header(self):
        with patch.object(dashboard, "LOGIN_FAILURE_LIMIT", 2), TestClient(dashboard.app) as client:
            self.assertEqual(client.post("/login", json={"password": "wrong"}).status_code, 401)
            self.assertEqual(client.get("/api/world-boss/turnstile", auth=("admin", "wrong")).status_code, 401)
            response = client.post("/login", json={"password": "test-password"},
                                   headers={"X-Forwarded-For": "198.51.100.2"})
            self.assertEqual(response.status_code, 429)
            self.assertGreater(int(response.headers["retry-after"]), 0)
            with patch.object(dashboard, "LOGIN_FAILURE_WINDOW_SECONDS", 0):
                response = client.post("/login", json={"password": "test-password"}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(dashboard.LOGIN_FAILURES, {})

    def test_success_clears_previous_failures(self):
        with TestClient(dashboard.app) as client:
            client.post("/login", json={"password": "wrong"})
            self.assertTrue(dashboard.LOGIN_FAILURES)
            client.post("/login", json={"password": "test-password"}, follow_redirects=False)
            self.assertEqual(dashboard.LOGIN_FAILURES, {})


if __name__ == "__main__":
    unittest.main()
