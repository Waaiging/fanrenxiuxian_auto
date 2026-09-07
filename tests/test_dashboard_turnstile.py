import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import dashboard_server


class DashboardTurnstileApiTests(unittest.TestCase):
    def _client(self):
        return TestClient(dashboard_server.app)

    def test_browser_diagnostics_require_auth_and_accept_only_request_metadata(self):
        with patch.object(dashboard_server, 'DASHBOARD_PASSWORD', 'test-password'), patch.object(
            dashboard_server, 'record_world_boss_turnstile_browser_event',
            return_value={'request_id':'request-id-fixture-1234','browser_error_code':'600010'},
        ) as record:
            with self._client() as client:
                payload = {'request_id':'request-id-fixture-1234','event':'widget_error','error_code':'600010'}
                self.assertEqual(client.post('/api/world-boss/turnstile/browser-event',json=payload).status_code, 401)
                result = client.post('/api/world-boss/turnstile/browser-event',json=payload,auth=('admin','test-password'))
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.json()['success'])
                record.assert_called_once_with('request-id-fixture-1234','widget_error','600010')

    def test_list_requires_auth_and_never_returns_token(self):
        public_row = {
            "request_id": "request-id-fixture-1234",
            "account": "main",
            "identity": "主魂",
            "status": "submitted",
            "token_available": True,
        }
        with patch.object(dashboard_server, "DASHBOARD_PASSWORD", "test-password"), patch.object(
            dashboard_server,
            "list_world_boss_turnstile_requests",
            return_value=[public_row],
        ):
            with self._client() as client:
                unauthorized = client.get("/api/world-boss/turnstile")
                self.assertEqual(unauthorized.status_code, 401)

                response = client.get(
                    "/api/world-boss/turnstile",
                    auth=("admin", "test-password"),
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["requests"], [public_row])
                self.assertNotIn("turnstile-fixture-token", response.text)

    def test_submit_returns_safe_metadata_and_maps_duplicate_error(self):
        submitted = {
            "request_id": "request-id-fixture-1234",
            "status": "submitted",
            "token_available": True,
        }
        with patch.object(dashboard_server, "DASHBOARD_PASSWORD", "test-password"), patch.object(
            dashboard_server,
            "submit_world_boss_turnstile_token",
            return_value=submitted,
        ) as submit:
            with self._client() as client:
                response = client.post(
                    "/api/world-boss/turnstile",
                    auth=("admin", "test-password"),
                    json={
                        "request_id": "request-id-fixture-1234",
                        "token": "turnstile-fixture-token",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"success": True, "request": submitted})
                submit.assert_called_once_with(
                    "request-id-fixture-1234", "turnstile-fixture-token"
                )
                self.assertNotIn("turnstile-fixture-token", response.text)

        from world_boss_turnstile import TurnstileRequestError

        with patch.object(dashboard_server, "DASHBOARD_PASSWORD", "test-password"), patch.object(
            dashboard_server,
            "submit_world_boss_turnstile_token",
            side_effect=TurnstileRequestError("turnstile_request_already_submitted"),
        ):
            with self._client() as client:
                response = client.post(
                    "/api/world-boss/turnstile",
                    auth=("admin", "test-password"),
                    json={"request_id": "request-id-fixture-1234", "token": "token"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"success": False, "msg": "该验证请求已经提交过令牌"})


if __name__ == "__main__":
    unittest.main()
