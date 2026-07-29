import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from group_visibility_control import (
    TmuxXiaohaoProcessManager,
    TelegramGroupXiaohaoController,
    is_missing_tmux_target_error,
    normalize_telegram_chat_id,
    telegram_chat_ids_match,
    telegram_group_visibility,
    telegram_write_permission_status,
    telegram_update_targets_chat,
    tmux_target_parts,
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

    def test_tmux_target_helpers_parse_and_recognize_missing_window(self):
        self.assertEqual(tmux_target_parts("xiuxian:3"), ("xiuxian", "3"))
        self.assertTrue(is_missing_tmux_target_error("can't find window: 3"))
        self.assertTrue(is_missing_tmux_target_error("no server running on /tmp/tmux"))

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

    def test_public_group_starts_red_packet_standby_when_game_script_is_stopped(self):
        manager = TmuxXiaohaoProcessManager(
            "/tmp/deploy",
            FakeLogger(),
            "/tmp/deploy/venv/bin/python",
            script="cultivator_xiaohao.py",
            tmux_target="xiuxian:2",
            fallback_script="red_packet_account.py",
            fallback_account="xiaohao",
        )
        with (
            patch.object(manager, "_ensure_tmux_target_sync", return_value=False),
            patch.object(manager, "process_pids", AsyncMock(return_value=[])),
            patch.object(manager, "fallback_process_pids", AsyncMock(return_value=[])),
            patch.object(manager, "_start_fallback", AsyncMock(return_value=True)) as start_fallback,
        ):
            action = asyncio.run(manager.ensure_running(False))

        self.assertEqual(action, "already_stopped")
        start_fallback.assert_awaited_once()

    def test_missing_tmux_window_is_created_before_respawn(self):
        manager = TmuxXiaohaoProcessManager(
            "/tmp/deploy",
            FakeLogger(),
            "/tmp/deploy/venv/bin/python",
            script="cultivator_waaiging.py",
            tmux_target="xiuxian:3",
            account_label="Waaiging",
        )
        with (
            patch.object(manager, "_tmux_session_exists_sync", return_value=True),
            patch.object(manager, "_tmux_window_exists_sync", return_value=False),
            patch.object(manager, "_run_tmux_sync") as run_tmux,
        ):
            created = manager._ensure_tmux_target_sync()

        self.assertTrue(created)
        self.assertIn(
            call([
                "new-window",
                "-d",
                "-t",
                "xiuxian:3",
                "-n",
                "Waaiging",
                "exec sleep infinity",
            ]),
            run_tmux.mock_calls,
        )

    def test_respawn_retries_if_window_disappears_after_repair(self):
        manager = TmuxXiaohaoProcessManager(
            "/tmp/deploy",
            FakeLogger(),
            "/tmp/deploy/venv/bin/python",
            tmux_target="xiuxian:3",
            account_label="Waaiging",
        )
        with (
            patch.object(manager, "_ensure_tmux_target_sync") as ensure_target,
            patch.object(
                manager,
                "_run_tmux_sync",
                side_effect=[RuntimeError("can't find window: 3"), None],
            ) as run_tmux,
        ):
            manager._respawn_window_sync("bash -lc 'exec python worker.py'")

        self.assertEqual(ensure_target.call_count, 2)
        self.assertEqual(run_tmux.call_count, 2)


if __name__ == "__main__":
    unittest.main()
