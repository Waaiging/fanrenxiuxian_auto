import asyncio
import copy
import logging
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import command_feedback as feedback
import duel_features as duel


class DuelScheduleFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "duel_state.json"
        self.now = datetime(2026, 9, 12, 9, 0)
        for name, value in {
            "DUEL_STATE_FILE": str(self.path),
            "DUEL_LOCK_FILE": str(self.path.with_suffix(".lock")),
            "DUEL_DB_FILE": str(self.path.with_suffix(".sqlite3")),
            "XIAOHAO_STATE_FILE": str(self.path.parent / "xiaohao.json"),
            "duel_now": lambda: self.now,
        }.items():
            self.patch(duel, name, value)
        duel.load_duel_state(write_back=True)
        duel.set_duel_control(False, duel.DUEL_ROTATION_QUEUE_KEY)

    def patch(self, module, name, value):
        patched = patch.object(module, name, value)
        patched.start()
        self.addCleanup(patched.stop)
        return value

    def configure(self, start="09:00", end="10:00", target="ExternalAlpha", enabled=True):
        return duel.configure_duel_multi_plan(
            "main", "无咎子", [{"username": target, "count": 3}],
            enabled=enabled, target_switch_enabled=target == "ding303",
            schedule={"enabled": True, "start": start, "end": end},
        )

    def settle(self, reservation):
        duel.finish_duel_reservation(reservation, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })


class DuelScheduleTests(DuelScheduleFixture, unittest.TestCase):
    def test_daytime_window_includes_start_and_excludes_end(self):
        multi = self.configure()["multi"]
        for instant, active, next_start, ends_at in (
            ("2026-09-12 08:59:59", False, "2026-09-12 09:00:00", ""),
            ("2026-09-12 09:00:00", True, "", "2026-09-12 10:00:00"),
            ("2026-09-12 09:59:59", True, "", "2026-09-12 10:00:00"),
            ("2026-09-12 10:00:00", False, "2026-09-13 09:00:00", ""),
        ):
            with self.subTest(instant=instant):
                result = duel.duel_multi_schedule_status(multi, datetime.fromisoformat(instant))
                self.assertEqual((result["active"], result["next_start_at"], result["ends_at"]),
                                 (active, next_start, ends_at))

    def test_overnight_window_repeats_across_dates(self):
        multi = self.configure("22:00", "02:00")["multi"]
        for instant, active, next_start, ends_at in (
            ("2026-09-12 21:59:59", False, "2026-09-12 22:00:00", ""),
            ("2026-09-12 22:00:00", True, "", "2026-09-13 02:00:00"),
            ("2026-09-13 00:00:00", True, "", "2026-09-13 02:00:00"),
            ("2026-09-13 01:59:59", True, "", "2026-09-13 02:00:00"),
            ("2026-09-13 02:00:00", False, "2026-09-13 22:00:00", ""),
            ("2026-09-14 22:00:00", True, "", "2026-09-15 02:00:00"),
        ):
            with self.subTest(instant=instant):
                result = duel.duel_multi_schedule_status(multi, datetime.fromisoformat(instant))
                self.assertEqual((result["active"], result["next_start_at"], result["ends_at"]),
                                 (active, next_start, ends_at))

    def test_aware_times_are_interpreted_in_beijing(self):
        multi = self.configure()["multi"]
        for zone in (timezone.utc, timezone(timedelta(hours=-7)), timezone(timedelta(hours=9))):
            instant = datetime(2026, 9, 12, 1, tzinfo=timezone.utc).astimezone(zone)
            result = duel.duel_multi_schedule_status(multi, instant)
            self.assertTrue(result["active"])
            self.assertEqual(result["timezone"], "Asia/Shanghai")
            self.assertEqual(result["ends_at"], "2026-09-12 10:00:00")
            self.assertFalse(duel.duel_multi_schedule_status(multi, instant + timedelta(hours=1))["active"])

    def test_invalid_updates_do_not_change_saved_plan(self):
        self.configure()
        before = self.path.read_bytes()
        for invalid in (
            None, [], "09:00-10:00", {"enabled": "false"},
            {"enabled": True, "start": "24:00", "end": "10:00"},
            {"enabled": True, "start": "9:00", "end": "10:00"},
            {"enabled": True, "start": "09:00", "end": "10:60"},
            {"enabled": True, "start": "09:00", "end": "09:00"},
            {"enabled": True, "start": "", "end": "10:00"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                duel.set_duel_multi_schedule(invalid)
            self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_saved_enabled_schedule_blocks_sends(self):
        state = self.configure()
        for invalid in ("bad", {"enabled": True}, {"enabled": True, "start": "22:00", "end": "22:00"}):
            with self.subTest(invalid=invalid):
                state["multi"]["schedule"] = invalid
                duel._atomic_write_json(duel.DUEL_STATE_FILE, state)
                self.assertIsNone(duel.reserve_duel_for_account("main"))
                self.assertFalse(duel._duel_multi_send_allowed())
                self.assertTrue(duel.duel_dashboard_payload()["multi"]["schedule"]["error"])

    def test_old_plans_remain_unrestricted(self):
        state = self.configure()
        state["multi"].pop("schedule")
        duel._atomic_write_json(duel.DUEL_STATE_FILE, state)
        self.now = self.now.replace(hour=20)
        restored = duel.load_duel_state(write_back=True)
        self.assertFalse(restored["multi"]["schedule"]["enabled"])
        self.assertEqual(duel.reserve_duel_for_account("main")["queue_key"], "multi")

    def test_saving_schedule_preserves_progress_cooldowns_and_controls(self):
        self.configure()
        self.settle(duel.reserve_duel_for_account("main"))
        before = duel.load_duel_state()
        schedule = {"enabled": True, "start": "22:00", "end": "02:00"}
        after = duel.set_duel_multi_schedule(schedule)
        expected = copy.deepcopy(before)
        expected["multi"]["schedule"] = schedule
        self.assertEqual(after, expected)
        self.assertEqual(after["multi"]["targets"][0]["attempts"], 1)
        self.assertEqual(after["multi"]["daily_remaining"], 9)

    def test_schedule_survives_pause_restart_daily_reset_and_plan_replacement(self):
        self.configure()
        self.settle(duel.reserve_duel_for_account("main"))
        duel.set_duel_multi_control(False)
        duel.set_duel_multi_control(True)
        self.now += timedelta(days=1)
        restored = duel.load_duel_state(write_back=True)
        self.assertEqual(restored["multi"]["schedule"], {"enabled": True, "start": "09:00", "end": "10:00"})
        self.assertEqual(restored["multi"]["targets"][0]["attempts"], 1)
        self.assertEqual(restored["multi"]["daily_remaining"], 10)
        replacement = duel.configure_duel_multi_plan("sub", "厚土", [{"username": "NewTarget", "count": 2}])
        self.assertEqual(replacement["multi"]["schedule"], restored["multi"]["schedule"])

    def test_reservation_waits_for_opening_and_next_day(self):
        self.configure()
        self.now = self.now.replace(hour=8, minute=59, second=59)
        self.assertIsNone(duel.reserve_duel_for_account("main"))
        self.now += timedelta(seconds=1)
        self.settle(duel.reserve_duel_for_account("main"))
        self.now = self.now.replace(hour=10)
        self.assertIsNone(duel.reserve_duel_for_account("main"))
        self.now = (self.now + timedelta(days=1)).replace(hour=9)
        self.assertEqual(duel.reserve_duel_for_account("main")["queue_key"], "multi")

    def test_multi_window_does_not_block_rotation(self):
        self.configure()
        self.now = self.now.replace(hour=8)
        duel.set_duel_control(True, duel.DUEL_ROTATION_QUEUE_KEY)
        duel.set_duel_participant_control(True, "main|主魂", "RotationTarget")
        reservation = duel.reserve_duel_for_account("main")
        self.assertEqual(reservation["queue_key"], duel.DUEL_ROTATION_QUEUE_KEY)
        self.assertEqual(reservation["target_username"], "RotationTarget")

    def test_dashboard_next_run_combines_schedule_and_cooldown(self):
        state = self.configure()
        self.now = self.now.replace(hour=8)
        for next_at, expected in (
            ("", "2026-09-12 09:00:00"),
            ("2026-09-12 09:06:00", "2026-09-12 09:06:00"),
            ("2026-09-12 10:00:00", "2026-09-13 09:00:00"),
        ):
            with self.subTest(next_at=next_at):
                state["multi"]["next_at"] = next_at
                duel._atomic_write_json(duel.DUEL_STATE_FILE, state)
                payload = duel.duel_dashboard_payload()["multi"]
                self.assertFalse(payload["schedule"]["active"])
                self.assertEqual(payload["next_at"], expected)

    def test_overnight_result_keeps_its_reservation_at_midnight(self):
        self.now = self.now.replace(hour=23, minute=59, second=50)
        self.configure("22:00", "02:00")
        reservation = duel.reserve_duel_for_account("main")
        self.now += timedelta(seconds=20)
        restored = duel.load_duel_state(write_back=True)
        self.assertEqual(restored["multi"]["in_flight"]["run_id"], reservation["run_id"])
        self.settle(reservation)
        multi = duel.load_duel_state()["multi"]
        self.assertEqual(multi["targets"][0]["attempts"], 1)
        self.assertEqual(multi["targets"][0]["remaining"], 2)
        self.assertEqual(multi["in_flight"], {})

    def test_target_preparation_cannot_start_after_close(self):
        self.now = self.now.replace(minute=59, second=59)
        self.configure(target="ding303")
        self.assertIsNone(duel.reserve_duel_for_account("main"))
        self.assertTrue(duel.load_duel_state()["multi"]["preparation"])
        self.now += timedelta(seconds=1)
        self.assertIsNone(duel.claim_duel_target_preparation("sub"))
        self.assertIsNone(duel.reserve_duel_for_account("main"))
        self.assertEqual(duel.load_duel_state()["multi"]["preparation"], {})

    def test_ready_target_is_released_at_close_but_inflight_target_is_held(self):
        self.now = self.now.replace(minute=59, second=58)
        self.configure(target="ding303")
        duel.reserve_duel_for_account("main")
        claim = duel.claim_duel_target_preparation("sub")
        duel.finish_duel_target_preparation(claim, True, reply_to_msg_id=501)
        self.now += timedelta(seconds=2)
        self.assertFalse(duel.duel_target_preparation_held(claim))
        self.now -= timedelta(seconds=1)
        reservation = duel.reserve_duel_for_account("main")
        self.now += timedelta(seconds=1)
        self.assertTrue(duel.duel_target_preparation_held(claim))
        self.settle(reservation)
        self.assertFalse(duel.duel_target_preparation_held(claim))

    def test_unsent_reservation_does_not_consume_progress_or_target_cooldown(self):
        self.now = self.now.replace(minute=59, second=59)
        self.configure()
        reservation = duel.reserve_duel_for_account("main")
        self.now += timedelta(seconds=1)
        actor = duel.DuelMixin()
        record = self.patch(duel, "record_duel_event", MagicMock())
        result = asyncio.run(actor.execute_duel_reservation(reservation))
        self.assertEqual(result["status"], "schedule_wait")
        state = duel.load_duel_state()
        self.assertEqual(state["multi"]["targets"][0]["attempts"], 0)
        self.assertEqual(state["multi"]["daily_remaining"], 10)
        self.assertEqual(state["multi"]["cursor"], 0)
        self.assertEqual(state["multi"]["in_flight"], {})
        self.assertEqual(state["target_next_at"], {})
        record.assert_not_called()

    def test_cancellation_does_not_erase_a_cooldown_extended_by_rotation(self):
        self.configure()
        reservation = duel.reserve_duel_for_account("main")
        state = duel.load_duel_state()
        extended = "2026-09-13 09:00:00"
        state["target_next_at"]["externalalpha"] = extended
        duel._atomic_write_json(duel.DUEL_STATE_FILE, state)
        duel.finish_duel_reservation(reservation, {"status": "schedule_wait", "outcome": "等待每日斗法时段"})
        self.assertEqual(duel.load_duel_state()["target_next_at"]["externalalpha"], extended)

    def test_schedule_api_round_trip_is_independent_of_plan_progress(self):
        import dashboard_server as dashboard
        from fastapi.testclient import TestClient

        self.configure()
        self.settle(duel.reserve_duel_for_account("main"))
        previous_overrides = dict(dashboard.app.dependency_overrides)
        try:
            dashboard.app.dependency_overrides[dashboard.authenticate] = lambda: "test-admin"
            with TestClient(dashboard.app) as client:
                schedule = {"enabled": True, "start": "22:00", "end": "02:00"}
                response = client.post("/api/duels/multi", json={"schedule": schedule})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["success"])
                self.assertEqual(response.json()["schedule"]["start"], "22:00")
                saved = client.get("/api/duels").json()["multi"]
                self.assertEqual(saved["total_completed"], 1)
                self.assertEqual(saved["schedule"]["end"], "02:00")
                self.assertTrue(saved["enabled"])
                before = self.path.read_bytes()
                invalid = client.post("/api/duels/multi", json={"schedule": {**schedule, "end": "22:00"}}).json()
                self.assertFalse(invalid["success"])
                self.assertIn("不能相同", invalid["msg"])
                self.assertEqual(self.path.read_bytes(), before)
        finally:
            dashboard.app.dependency_overrides.clear()
            dashboard.app.dependency_overrides.update(previous_overrides)


class FeedbackDuelActor(duel.DuelMixin):
    def __init__(self):
        self.account_key = "main"
        self.current_identity = "无咎子"
        self.avatars = ["无咎子", "寻真子"]
        self.cmd_lock = asyncio.Lock()
        self.target_chat_id = -1001
        self.topic_id = None
        self.state = {}
        self.feedback_events = {}
        self.feedback_commands = {}
        self.feedback_sent_ts = {}
        self.last_feedback_text = {}
        self.last_feedback_msg = {}
        self.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(
            id=101, sender_id=999, chat_id=-1001,
        )))
        self.entered = asyncio.Event()

    async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
        self.entered.set()
        kwargs.pop("force_identity_check", None)
        return await feedback.send_and_wait_feedback_common(self, logging.getLogger(__name__), command, **kwargs)

    async def prepare_identity_for_time_critical_command(self, identity, **kwargs):
        response = await self.send_and_wait_feedback_identity(identity, f".切换 {identity}", delete_after=False)
        if response:
            self.current_identity = identity
        return bool(response), getattr(self, "last_sent_id", None)


class DuelScheduleDispatchTests(DuelScheduleFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.now = self.now.replace(minute=59, second=59)
        self.configure()
        self.actor = FeedbackDuelActor()
        for name in (
            "command_send_allowed", "record_command_sent", "record_recent_profile_command",
            "record_bot_response", "record_bot_no_response", "record_cultivation_profile_from_text",
            "record_cultivation_delta_from_text", "_record_timed_response_guard", "_record_repeated_response_guard",
        ):
            self.patch(feedback, name, MagicMock(return_value=True))
        self.patch(feedback, "wait_for_bot_activity_before_send", AsyncMock(return_value=True))
        self.patch(feedback, "log_incoming_message", AsyncMock())
        self.patch(feedback, "send_text_alert", AsyncMock())
        self.patch(feedback, "NO_RESPONSE_TIMEOUT_SECONDS", 0.01)
        self.patch(feedback, "NO_RESPONSE_RETRY_COUNT", 0)
        self.patch(feedback, "record_telegram_send_success", AsyncMock())
        self.patch(feedback.asyncio, "sleep", AsyncMock())
        self.record = self.patch(duel, "record_duel_event", MagicMock())
        text = "胜者：@wuxinglinggen\n败者：@ExternalAlpha\n胜负已分！\n今日神念：9/10"
        self.patch(duel, "wait_for_duel_result", AsyncMock(return_value=(SimpleNamespace(id=102), text)))

    async def test_command_waiting_for_lock_is_blocked_at_closing_time(self):
        reservation = duel.reserve_duel_for_account("main")
        await self.actor.cmd_lock.acquire()
        task = asyncio.create_task(self.actor.execute_duel_reservation(reservation))
        try:
            await self.actor.entered.wait()
            self.now += timedelta(seconds=1)
            self.actor.cmd_lock.release()
            result = await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(result["status"], "schedule_wait")
        self.actor.client.send_message.assert_not_awaited()
        self.record.assert_not_called()

    async def test_bot_activity_wait_rechecks_current_schedule(self):
        reservation = duel.reserve_duel_for_account("main")

        async def wait(*args):
            duel.set_duel_multi_schedule({"enabled": True, "start": "22:00", "end": "02:00"})
            return True

        self.patch(feedback, "wait_for_bot_activity_before_send", wait)
        result = await self.actor.execute_duel_reservation(reservation)
        self.assertEqual(result["status"], "schedule_wait")
        self.actor.client.send_message.assert_not_awaited()
        self.assertEqual(duel.load_duel_state()["multi"]["targets"][0]["remaining"], 3)

    async def test_target_switch_waiting_for_bot_cannot_send_after_close(self):
        self.configure(target="ding303")
        duel.reserve_duel_for_account("main")
        claim = duel.claim_duel_target_preparation("sub")
        self.actor.account_key = "sub"

        async def wait(*args):
            self.now += timedelta(seconds=1)
            return True

        self.patch(feedback, "wait_for_bot_activity_before_send", wait)
        success, detail, message_id = await self.actor.prepare_duel_target_identity(claim)
        self.assertFalse(success)
        self.assertEqual(detail, "等待每日斗法时段")
        self.assertIsNone(message_id)
        self.actor.client.send_message.assert_not_awaited()

    async def test_retry_is_blocked_after_close_but_already_sent_duel_settles(self):
        self.patch(feedback, "NO_RESPONSE_RETRY_COUNT", 1)

        async def sent(*args, **kwargs):
            self.now += timedelta(seconds=1)

        self.patch(feedback, "record_telegram_send_success", sent)
        reservation = duel.reserve_duel_for_account("main")
        result = await self.actor.execute_duel_reservation(reservation)
        self.assertEqual(result["status"], "settled")
        self.actor.client.send_message.assert_awaited_once()
        self.record.assert_called_once()
        self.assertEqual(self.record.call_args.kwargs["command_msg_id"], 101)
        self.assertEqual(duel.load_duel_state()["multi"]["targets"][0]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
