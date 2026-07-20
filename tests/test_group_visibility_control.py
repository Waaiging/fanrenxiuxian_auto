import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace

from group_visibility_control import (
    TelegramGroupXiaohaoController,
    normalize_telegram_chat_id,
    telegram_chat_ids_match,
    telegram_group_visibility,
    telegram_write_permission_status,
    telegram_update_targets_chat,
    xiaohao_write_restriction_retry_wait,
)


class FakeLogger:
    def __init__(self):
        self.records = []

    def _record(self, level, message, *args, **kwargs):
        self.records.append((level, message % args if args else message))

    def info(self, message, *args, **kwargs):
        self._record("info", message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        self._record("warning", message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        self._record("error", message, *args, **kwargs)


class FakeClient:
    def __init__(self, entity):
        self.entity = entity
        self.requests = []

    async def get_entity(self, target):
        self.requests.append(target)
        return self.entity


class FakeProcessManager:
    def __init__(self):
        self.desired = []

    async def ensure_running(self, desired_running):
        self.desired.append(desired_running)
        return "started" if desired_running else "stopped"


class GroupVisibilityControlTests(unittest.TestCase):
    def test_primary_or_active_username_means_public(self):
        self.assertEqual(
            telegram_group_visibility(SimpleNamespace(username="public_group", usernames=[])),
            "public",
        )
        self.assertEqual(
            telegram_group_visibility(SimpleNamespace(
                username=None,
                usernames=[SimpleNamespace(username="alias", active=True)],
            )),
            "public",
        )

    def test_no_active_username_means_private(self):
        entity = SimpleNamespace(
            username=None,
            usernames=[SimpleNamespace(username="old_alias", active=False)],
        )
        self.assertEqual(telegram_group_visibility(entity), "private")

    def test_broadcast_channel_is_not_treated_as_group(self):
        entity = SimpleNamespace(broadcast=True, megagroup=False, username="news")
        self.assertEqual(telegram_group_visibility(entity), "unsupported")

    def test_chat_id_matching_accepts_minus_100_form(self):
        self.assertEqual(normalize_telegram_chat_id(-1002083016447), 2083016447)
        self.assertTrue(telegram_chat_ids_match(-1002083016447, 2083016447))
        self.assertFalse(telegram_chat_ids_match(-1002083016447, 2083016448))

    def test_write_permission_status_detects_block(self):
        self.assertEqual(
            telegram_write_permission_status(SimpleNamespace(is_banned=True, send_messages=True)),
            ("blocked", "participant is banned"),
        )
        self.assertEqual(
            telegram_write_permission_status(SimpleNamespace(is_banned=False, send_messages=False)),
            ("blocked", "send_messages is false"),
        )
        self.assertEqual(
            telegram_write_permission_status(SimpleNamespace(is_banned=False, send_messages=True)),
            ("allowed", "group permissions allow messages"),
        )

    def test_write_restriction_retry_wait_uses_retry_at(self):
        state = {
            "telegram_send_protection_stop": {
                "reason": "write_restricted",
                "retry_at": "2026-07-17 12:15:00",
            }
        }
        wait = xiaohao_write_restriction_retry_wait(
            state,
            now=datetime(2026, 7, 17, 12, 10, 0),
        )
        self.assertEqual(wait, 300)

    def test_only_target_channel_updates_trigger_immediate_check(self):
        UpdateChannel = type("UpdateChannel", (), {})
        update = UpdateChannel()
        update.channel_id = 2083016447
        self.assertTrue(telegram_update_targets_chat(update, -1002083016447))
        update.channel_id = 2083016448
        self.assertFalse(telegram_update_targets_chat(update, -1002083016447))

    def test_controller_starts_for_private_and_stops_for_public(self):
        logger = FakeLogger()
        manager = FakeProcessManager()
        state = {}
        saves = []
        client = FakeClient(SimpleNamespace(username=None, usernames=[]))
        controller = TelegramGroupXiaohaoController(
            client,
            2083016447,
            manager,
            logger,
            state=state,
            save_state=lambda: saves.append(dict(state)),
        )

        self.assertEqual(asyncio.run(controller.check_once("fixture private")), "private")
        client.entity = SimpleNamespace(username="public_group", usernames=[])
        self.assertEqual(asyncio.run(controller.check_once("fixture public")), "public")

        self.assertEqual(manager.desired, [True, False])
        self.assertEqual(state["target_group_visibility"], "public")
        self.assertEqual(state["xiaohao_visibility_last_action"], "stopped")
        self.assertEqual(len(saves), 2)

    def test_controller_uses_account_specific_action_keys(self):
        state = {}
        controller = TelegramGroupXiaohaoController(
            FakeClient(SimpleNamespace(username=None, usernames=[])),
            2083016447,
            FakeProcessManager(),
            FakeLogger(),
            state=state,
            account_label="Waaiging",
            state_prefix="waaiging",
        )

        self.assertEqual(asyncio.run(controller.check_once("fixture")), "private")
        self.assertEqual(state["waaiging_visibility_last_action"], "started")
        self.assertNotIn("xiaohao_visibility_last_action", state)


if __name__ == "__main__":
    unittest.main()
