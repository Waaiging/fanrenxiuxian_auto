import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wind_thunder_features as wt
import automation_settings
from wind_thunder_features import (
    WIND_THUNDER_HOLD_SECONDS,
    WIND_THUNDER_PLANNING_WINDOW_SECONDS,
    WIND_THUNDER_SCHEDULE_KEYS,
    _next_due_command,
    wind_thunder_send,
)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class _Actor:
    """Minimal actor for wind-thunder session tests."""

    def __init__(self, account="main", identity="主魂"):
        self.account_key = account
        self.state_file = f"state_{account}.json"
        self.state = {}
        self.avatar_states = {}
        self.avatars = ["无咎子"] if identity != "主魂" else []
        self.identity_for_timed_command = identity
        self.saved = 0
        self.sent = []
        # responses keyed by command prefix
        self.responses = {}

    def identity_state_for_timed_command(self, identity):
        if identity == "主魂":
            return self.state
        return self.avatar_states.setdefault(identity, {})

    def get_avatar_state(self, identity):
        return self.avatar_states.setdefault(identity, {})

    def save_state(self):
        self.saved += 1

    async def send_and_wait_feedback(self, command, **kwargs):
        self.sent.append(command)
        return self.responses.get(command, "")

    async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
        self.sent.append((identity, command))
        return self.responses.get(command, "")


def _run(coro):
    return asyncio.run(coro)


def _enabled_actor(identity="主魂", account="main"):
    return _Actor(account=account, identity=identity)


class WindThunderFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        file_patch = patch.object(
            automation_settings, "AUTOMATION_SETTINGS_FILE", Path(directory.name) / "settings.json"
        )
        file_patch.start()
        self.addCleanup(file_patch.stop)
        automation_settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="护阵",
            wind_thunder_enabled=True,
            wind_thunder_participants=["main|主魂", "main|无咎子"],
        )


class NextDueCommandTests(WindThunderFixture):
    """Planning check: which accelerated command is due within the window."""

    def setUp(self):
        super().setUp()
        self.actor = _enabled_actor()

    def test_upcoming_rift_keeps_session(self):
        due = datetime.now() + timedelta(minutes=20)
        self.actor.state["next_rift_search_time"] = due.strftime(TIME_FORMAT)
        self.assertEqual(_next_due_command(self.actor, "主魂"), (".探寻裂缝", due.replace(microsecond=0)))

    def test_overdue_rift_keeps_session(self):
        due = datetime.now() - timedelta(minutes=5)
        self.actor.state["next_rift_search_time"] = due.strftime(TIME_FORMAT)
        self.assertEqual(_next_due_command(self.actor, "主魂"), (".探寻裂缝", due.replace(microsecond=0)))

    def test_far_future_schedule_released(self):
        due = datetime.now() + timedelta(hours=3)
        self.actor.state["next_rift_search_time"] = due.strftime(TIME_FORMAT)
        self.assertEqual(_next_due_command(self.actor, "主魂")[0], "")

    def test_missing_schedule_released(self):
        self.assertEqual(_next_due_command(self.actor, "主魂")[0], "")

    def test_ask_dao_and_beast_keys_covered(self):
        now = datetime.now()
        self.actor.state["next_ask_dao_time"] = (now + timedelta(minutes=10)).strftime(TIME_FORMAT)
        self.assertEqual(_next_due_command(self.actor, "主魂")[0], ".问道")
        self.actor.state["next_hunt_time"] = (now + timedelta(minutes=2)).strftime(TIME_FORMAT)
        self.assertEqual(_next_due_command(self.actor, "主魂")[0], ".寻觅灵兽")

    def test_avatar_state_used_for_avatar_identity(self):
        actor = _enabled_actor(identity="无咎子")
        due = datetime.now() + timedelta(minutes=15)
        actor.avatar_states["无咎子"] = {"next_rift_search_time": due.strftime(TIME_FORMAT)}
        self.assertEqual(_next_due_command(actor, "无咎子"), (".探寻裂缝", due.replace(microsecond=0)))


class CleanupPlanningTests(WindThunderFixture):
    """The cleanup timer defers when commands are coming within 30 minutes."""

    def setUp(self):
        super().setUp()
        self.actor = _enabled_actor()
        self.actor.responses = {
            ".散念 风雷翅": "你已散去对【风雷翅】的祭炼联系，此宝自法宝谱中除名。",
            ".上架至万宝阁 风雷翅": "你已将【风雷翅】郑重地放置在万宝阁的展台上。",
        }

    def test_defers_when_command_due_soon(self):
        actor = self.actor
        actor.state["wind_thunder_equipped"] = True
        actor.state["wind_thunder_cleanup_due_at"] = datetime.now().strftime(TIME_FORMAT)
        actor.state["next_rift_search_time"] = (
            datetime.now() + timedelta(minutes=25)
        ).strftime(TIME_FORMAT)
        ok = _run(wt._cleanup(actor, "主魂"))
        self.assertTrue(ok)
        # No equip teardown commands sent: session extended instead.
        self.assertNotIn(".散念 风雷翅", [c for c in actor.sent if isinstance(c, str)])
        self.assertEqual(
            actor.state.get("wind_thunder_last_defer_reason"), "upcoming:.探寻裂缝"
        )
        # Cleanup due pushed forward again.
        due = datetime.strptime(actor.state["wind_thunder_cleanup_due_at"], TIME_FORMAT)
        self.assertGreater(due, datetime.now())

    def test_teardown_when_nothing_coming(self):
        actor = self.actor
        actor.state["wind_thunder_equipped"] = True
        actor.state["wind_thunder_cleanup_due_at"] = datetime.now().strftime(TIME_FORMAT)
        ok = _run(wt._cleanup(actor, "主魂"))
        self.assertTrue(ok)
        self.assertIn(".散念 风雷翅", actor.sent)
        self.assertIn(".上架至万宝阁 风雷翅", actor.sent)
        self.assertFalse(actor.state.get("wind_thunder_equipped"))
        self.assertEqual(actor.state.get("wind_thunder_cleanup_due_at"), "")

    def test_list_failure_backs_off_without_repeat(self):
        actor = self.actor
        actor.responses[".上架至万宝阁 风雷翅"] = "此宝似与储物袋中的因果牵连过深，本次放置失败，请稍后再试。"
        actor.state["wind_thunder_equipped"] = True
        actor.state["wind_thunder_cleanup_due_at"] = datetime.now().strftime(TIME_FORMAT)
        ok = _run(wt._cleanup(actor, "主魂"))
        self.assertFalse(ok)
        # Exactly one listing attempt in this pass; no blind retries.
        self.assertEqual(
            [c for c in actor.sent if c == ".上架至万宝阁 风雷翅"].count(".上架至万宝阁 风雷翅"), 1
        )
        self.assertFalse(actor.state.get("wind_thunder_equipped"))
        self.assertTrue(actor.state.get("wind_thunder_list_pending"))
        self.assertEqual(actor.state.get("wind_thunder_last_cleanup_error"), "list_failed")
        due = datetime.strptime(actor.state["wind_thunder_cleanup_due_at"], TIME_FORMAT)
        self.assertGreater(due, datetime.now())

    def test_san_nian_unconfirmed_backs_off(self):
        actor = self.actor
        actor.responses[".散念 风雷翅"] = ""  # timeout / empty
        actor.state["wind_thunder_equipped"] = True
        actor.state["wind_thunder_cleanup_due_at"] = datetime.now().strftime(TIME_FORMAT)
        ok = _run(wt._cleanup(actor, "主魂"))
        self.assertFalse(ok)
        # Must NOT blindly continue to listing after unconfirmed 散念.
        self.assertNotIn(".上架至万宝阁 风雷翅", actor.sent)
        self.assertTrue(actor.state.get("wind_thunder_equipped"))
        self.assertEqual(
            actor.state.get("wind_thunder_last_cleanup_error"), "san_nian_unconfirmed"
        )

    def test_manual_listing_failure_preserves_pending_recovery(self):
        actor = self.actor
        actor.state["wind_thunder_list_pending"] = True
        wt.sync_wind_thunder_manual_response(
            actor, "主魂", ".上架至万宝阁 风雷翅", "【风雷翅】本次放置失败，请稍后再试。"
        )
        self.assertTrue(actor.state["wind_thunder_list_pending"])
        self.assertNotIn("wind_thunder_last_cleanup_time", actor.state)

    def test_cleanup_uses_its_own_backoff_without_implicit_send_retries(self):
        actor = self.actor
        actor.state["wind_thunder_equipped"] = True
        actor.send_and_wait_feedback = AsyncMock(side_effect=[
            actor.responses[".散念 风雷翅"], actor.responses[".上架至万宝阁 风雷翅"],
        ])
        self.assertTrue(_run(wt._cleanup(actor, "主魂")))
        for call in actor.send_and_wait_feedback.await_args_list:
            self.assertIs(call.kwargs["retry_on_timeout"], False)

    def test_cleanup_reaches_real_main_soul_senders_on_all_accounts(self):
        import intelligent_cultivator as main
        import sub_cultivator as sub
        import cultivator_xiaohao as xiaohao
        from cultivator_waaiging import WaaigingCultivator

        for account, module, cls in (
            ("main", main, main.Cultivator), ("sub", sub, sub.SubCultivator),
            ("xiaohao", xiaohao, xiaohao.CultivatorXiaoHao), ("waaiging", main, WaaigingCultivator),
        ):
            with self.subTest(account=account):
                async def verify():
                    actor = cls.__new__(cls)
                    actor.account_key = account
                    actor.state = {"wind_thunder_equipped": True}
                    actor.avatars = []
                    actor._current_identity = "主魂"
                    actor._main_confirmed = True
                    actor.avatar_send_lock = asyncio.Lock()
                    actor.pause_event = asyncio.Event()
                    actor.pause_event.set()
                    actor.should_wait_for_atomic_task = lambda command: False
                    actor.wait_while_identity_paused = AsyncMock(return_value=True)
                    actor.time_critical_defer_wait = lambda *args, **kwargs: -1
                    actor.save_state = lambda: None
                    actor._send_and_wait_feedback_raw = AsyncMock(side_effect=[
                        self.actor.responses[".散念 风雷翅"], self.actor.responses[".上架至万宝阁 风雷翅"],
                    ])
                    with patch.object(wt, "wind_thunder_enabled", return_value=True), patch.object(
                        module, "wait_for_bot_activity_before_send", new=AsyncMock(return_value=True)
                    ):
                        self.assertTrue(await wt._cleanup(actor, "主魂"))
                    calls = actor._send_and_wait_feedback_raw.await_args_list
                    self.assertEqual([call.args[0] for call in calls], [".散念 风雷翅", ".上架至万宝阁 风雷翅"])
                    self.assertTrue(all(call.kwargs["retry_on_timeout"] is False for call in calls))
                    self.assertFalse(actor.state["wind_thunder_equipped"])
                    self.assertFalse(actor.state["wind_thunder_list_pending"])
                _run(verify())

    def test_cleanup_exception_is_logged_and_notified_without_losing_exposure(self):
        actor = self.actor
        actor.state["wind_thunder_equipped"] = True
        actor.send_and_wait_feedback = AsyncMock(side_effect=TypeError("incompatible sender"))
        with patch.object(wt, "log") as logger, patch.object(wt, "_wind_thunder_notify") as notify:
            self.assertFalse(_run(wt._cleanup(actor, "主魂")))
            self.assertFalse(_run(wt._cleanup(actor, "主魂")))
        self.assertTrue(actor.state["wind_thunder_equipped"])
        self.assertEqual(actor.state["wind_thunder_last_cleanup_error"], "typeerror")
        self.assertIn("incompatible sender", actor.state["wind_thunder_last_cleanup_detail"])
        self.assertEqual(logger.error.call_count, 2)
        notify.assert_called_once()

    def test_restart_retries_previous_interface_error_immediately(self):
        actor = self.actor
        actor.state.update(wind_thunder_equipped=True, wind_thunder_last_cleanup_error="typeerror",
                           wind_thunder_cleanup_due_at=(datetime.now() + timedelta(minutes=5)).strftime(TIME_FORMAT))
        with patch.object(wt, "_schedule_cleanup") as schedule:
            wt.recover_wind_thunder_sessions(actor)
        schedule.assert_called_once_with(actor, "主魂")
        self.assertLessEqual(datetime.strptime(actor.state["wind_thunder_cleanup_due_at"], TIME_FORMAT), datetime.now())


class WindThunderSendTests(WindThunderFixture):
    """Equip-execute flow keeps one cycle per burst of commands."""

    def setUp(self):
        super().setUp()
        self.actor = _enabled_actor()

    def test_second_command_within_window_reuses_equipped_item(self):
        actor = self.actor
        actor.state["wind_thunder_equipped"] = True
        actor.state["wind_thunder_equipped_at"] = datetime.now().strftime(TIME_FORMAT)
        actor.state["wind_thunder_cleanup_due_at"] = (
            datetime.now() + timedelta(minutes=25)
        ).strftime(TIME_FORMAT)
        sent = []

        async def sender():
            sent.append("ran")
            return "ok"

        result = _run(wind_thunder_send(actor, "主魂", ".探寻裂缝", sender))
        self.assertEqual(result, "ok")
        self.assertEqual(sent, ["ran"])
        # No re-equip cycle.
        self.assertEqual(actor.sent, [])

    def test_command_after_window_re_equips_once(self):
        actor = self.actor
        # Equipped long ago; window expired.
        actor.state["wind_thunder_equipped"] = True
        actor.state["wind_thunder_equipped_at"] = (
            datetime.now() - timedelta(hours=2)
        ).strftime(TIME_FORMAT)
        actor.state["wind_thunder_cleanup_due_at"] = (
            datetime.now() - timedelta(hours=1)
        ).strftime(TIME_FORMAT)
        actor.responses = {
            ".散念 风雷翅": "你已散去对【风雷翅】的祭炼联系，此宝自法宝谱中除名。",
            ".上架至万宝阁 风雷翅": "你已将【风雷翅】郑重地放置在万宝阁的展台上。",
            ".从万宝阁取下 风雷翅": "你已将【风雷翅】从万宝阁收回储物袋。",
            ".装备 风雷翅": "你已祭出【风雷翅】。",
        }

        async def sender():
            return "ran"

        result = _run(wind_thunder_send(actor, "主魂", ".问道", sender))
        self.assertEqual(result, "ran")
        # An expired equipment snapshot is revalidated by one equip request.
        self.assertEqual(actor.sent.count(".散念 风雷翅"), 0)
        self.assertEqual(actor.sent.count(".上架至万宝阁 风雷翅"), 0)
        self.assertEqual(actor.sent.count(".从万宝阁取下 风雷翅"), 1)
        self.assertEqual(actor.sent.count(".装备 风雷翅"), 1)


class ExposureStateTests(WindThunderFixture):
    """Hunt-exposure protections: list-pending lock and escalating backoff."""

    def setUp(self):
        super().setUp()
        self.actor = _enabled_actor()
        self.actor.responses = {
            ".散念 风雷翅": "你已散去对【风雷翅】的祭炼联系，此宝自法宝谱中除名。",
            ".上架至万宝阁 风雷翅": "你已将【风雷翅】郑重地放置在万宝阁的展台上。",
        }

    def test_pending_listing_blocks_re_equip(self):
        actor = self.actor
        actor.state["wind_thunder_list_pending"] = True

        async def sender():
            return "plain"

        result = _run(wind_thunder_send(actor, "主魂", ".探寻裂缝", sender))
        self.assertEqual(result, "plain")
        # No equip cycle at all while listing is pending.
        self.assertEqual(actor.sent, [])

    def test_cleanup_retry_listing_skips_san_nian(self):
        actor = self.actor
        actor.state["wind_thunder_list_pending"] = True
        actor.state["wind_thunder_equipped"] = False
        ok = _run(wt._cleanup(actor, "主魂"))
        self.assertTrue(ok)
        # Direct listing retry; no 散念 re-send.
        self.assertNotIn(".散念 风雷翅", actor.sent)
        self.assertIn(".上架至万宝阁 风雷翅", actor.sent)
        self.assertFalse(actor.state.get("wind_thunder_list_pending"))

    def test_escalating_backoff(self):
        state = {}
        self.assertEqual(wt._listing_backoff_seconds(state), 5 * 60)
        state["wind_thunder_list_fail_count"] = 1
        self.assertEqual(wt._listing_backoff_seconds(state), 5 * 60)
        state["wind_thunder_list_fail_count"] = 2
        self.assertEqual(wt._listing_backoff_seconds(state), 15 * 60)
        state["wind_thunder_list_fail_count"] = 3
        self.assertEqual(wt._listing_backoff_seconds(state), 30 * 60)
        state["wind_thunder_list_fail_count"] = 5
        self.assertEqual(wt._listing_backoff_seconds(state), 30 * 60)

    def test_retry_listing_failure_notifies_and_sets_pending(self):
        actor = self.actor
        actor.responses[".上架至万宝阁 风雷翅"] = "此宝似与储物袋中的因果牵连过深，本次放置失败，请稍后再试。"
        actor.state["wind_thunder_list_pending"] = True
        notified = []
        with patch.object(wt, "_wind_thunder_notify", side_effect=lambda a, m: notified.append(m)):
            ok = _run(wt._retry_listing(actor, "主魂", actor.state))
        self.assertFalse(ok)
        self.assertTrue(actor.state.get("wind_thunder_list_pending"))
        self.assertEqual(actor.state.get("wind_thunder_list_fail_count"), 1)
        self.assertTrue(notified)
        self.assertIn("追杀暴露", notified[0])
        # Backoff is the first-tier 5 minutes.
        due = datetime.strptime(actor.state["wind_thunder_cleanup_due_at"], TIME_FORMAT)
        self.assertGreater(due - datetime.now(), timedelta(minutes=4))

    def test_unconfirmed_listing_retains_pending_state_and_retries(self):
        for reply in (None, "", "请稍后再试", "上架失败，风雷翅仍在储物袋"):
            with self.subTest(reply=reply):
                actor = self.actor
                actor.state = {"wind_thunder_equipped": True}
                actor.responses[".上架至万宝阁 风雷翅"] = reply
                self.assertFalse(_run(wt._cleanup(actor, "主魂")))
                self.assertFalse(actor.state["wind_thunder_equipped"])
                self.assertTrue(actor.state["wind_thunder_list_pending"])
                self.assertEqual(actor.state["wind_thunder_last_cleanup_error"], "list_unconfirmed")
                self.assertNotIn("wind_thunder_last_cleanup_time", actor.state)
                self.assertGreater(datetime.strptime(actor.state["wind_thunder_cleanup_due_at"], TIME_FORMAT), datetime.now())

    def test_restart_restores_pending_listing_for_enabled_identity(self):
        async def scenario():
            actor = self.actor
            actor.state = {"wind_thunder_list_pending": True, "wind_thunder_equipped": False}
            wt.recover_wind_thunder_sessions(actor)
            await actor._wind_thunder_cleanup_tasks["主魂"]
            return actor

        actor = _run(scenario())
        self.assertEqual(actor.sent, [".上架至万宝阁 风雷翅"])
        self.assertFalse(actor.state["wind_thunder_list_pending"])

    def test_cleanup_waits_for_inflight_command(self):
        async def scenario():
            actor = self.actor
            actor.state = {
                "wind_thunder_equipped": True,
                "wind_thunder_equipped_at": datetime.now().strftime(TIME_FORMAT),
                "wind_thunder_cleanup_due_at": (datetime.now() + timedelta(minutes=5)).strftime(TIME_FORMAT),
            }
            started, release = asyncio.Event(), asyncio.Event()

            async def sender():
                started.set()
                await release.wait()
                return "ok"

            command = asyncio.create_task(wind_thunder_send(actor, "主魂", ".问道", sender))
            await started.wait()
            cleanup = asyncio.create_task(wt._cleanup(actor, "主魂"))
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done())
            self.assertEqual(actor.sent, [])
            release.set()
            self.assertEqual(await command, "ok")
            self.assertTrue(await cleanup)
            self.assertEqual(actor.sent, [".散念 风雷翅", ".上架至万宝阁 风雷翅"])

        _run(scenario())


class DisabledIdentityTests(WindThunderFixture):
    def select(self, participants, enabled=True):
        automation_settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="护阵",
            wind_thunder_enabled=enabled, wind_thunder_participants=participants,
        )

    def avatar(self, equipped=False, pending=True):
        actor = _enabled_actor(identity="无咎子")
        actor.avatar_states["无咎子"] = {
            "wind_thunder_equipped": equipped,
            "wind_thunder_equipped_at": "2026-08-30 01:03:55",
            "wind_thunder_list_pending": pending,
            "wind_thunder_list_fail_count": 5,
            "wind_thunder_cleanup_due_at": datetime.now().strftime(TIME_FORMAT),
        }
        actor.responses = {
            ".散念 风雷翅": "你已散去对【风雷翅】的祭炼联系，此宝自法宝谱中除名。",
            ".上架至万宝阁 风雷翅": "你已将【风雷翅】郑重地放置在万宝阁的展台上。",
        }
        return actor

    def test_restart_skips_unselected_avatar_without_clearing_equipment_facts(self):
        self.select(["sub|主魂"])
        for equipped, pending in ((False, True), (True, False)):
            with self.subTest(equipped=equipped, pending=pending):
                actor = self.avatar(equipped, pending)
                with patch.object(wt, "_wind_thunder_notify") as notify:
                    wt.recover_wind_thunder_sessions(actor)
                state = actor.avatar_states["无咎子"]
                self.assertEqual(actor.sent, [])
                self.assertEqual(getattr(actor, "_wind_thunder_cleanup_tasks", {}), {})
                self.assertEqual(state["wind_thunder_cleanup_due_at"], "")
                self.assertEqual(state["wind_thunder_last_defer_reason"], "disabled")
                self.assertEqual(state["wind_thunder_equipped"], equipped)
                self.assertEqual(state["wind_thunder_list_pending"], pending)
                self.assertEqual(state["wind_thunder_list_fail_count"], 5)
                self.assertNotIn("wind_thunder_last_cleanup_time", state)
                notify.assert_not_called()

    def test_global_off_blocks_cleanup_for_a_selected_identity(self):
        self.select(["main|无咎子"], enabled=False)
        actor = self.avatar(equipped=True, pending=False)
        self.assertFalse(_run(wt._cleanup(actor, "无咎子")))
        self.assertEqual(actor.sent, [])
        self.assertTrue(actor.avatar_states["无咎子"]["wind_thunder_equipped"])

    def test_selected_sub_identity_still_lists_normally(self):
        self.select(["sub|主魂"])
        actor = _enabled_actor(account="sub")
        actor.state["wind_thunder_list_pending"] = True
        actor.responses = self.avatar().responses
        self.assertTrue(_run(wt._cleanup(actor, "主魂")))
        self.assertEqual(actor.sent, [".上架至万宝阁 风雷翅"])
        self.assertFalse(actor.state["wind_thunder_list_pending"])

    def test_disabling_cancels_sleeping_retry_and_does_not_rearm_it(self):
        async def scenario():
            actor = self.avatar()
            state = actor.avatar_states["无咎子"]
            state["wind_thunder_cleanup_due_at"] = (
                datetime.now() + timedelta(minutes=30)
            ).strftime(TIME_FORMAT)
            wt._schedule_cleanup(actor, "无咎子")
            timer = actor._wind_thunder_cleanup_tasks["无咎子"]
            await asyncio.sleep(0)
            self.select(["sub|主魂"])
            wt._schedule_cleanup(actor, "无咎子")
            await asyncio.gather(timer, return_exceptions=True)
            self.assertTrue(timer.cancelled())
            self.assertFalse(await wt._retry_listing(actor, "无咎子", state))
            wt.recover_wind_thunder_sessions(actor)
            self.assertEqual(actor._wind_thunder_cleanup_tasks, {})
            self.assertEqual(actor.sent, [])
            self.assertEqual(state["wind_thunder_list_fail_count"], 5)

        _run(scenario())

    def test_disable_while_waiting_for_session_lock_blocks_both_entry_points(self):
        async def scenario(operation):
            self.select(["main|无咎子"])
            actor = self.avatar(equipped=True, pending=False)
            sender = AsyncMock(return_value="plain")
            lock = wt._session_lock(actor)
            await lock.acquire()
            coroutine = (wt._cleanup(actor, "无咎子") if operation == "cleanup" else
                         wt.wind_thunder_send(actor, "无咎子", ".探寻裂缝", sender))
            task = asyncio.create_task(coroutine)
            await asyncio.sleep(0)
            self.select(["sub|主魂"])
            lock.release()
            result = await task
            self.assertEqual(actor.sent, [])
            if operation == "cleanup":
                self.assertFalse(result)
                sender.assert_not_awaited()
            else:
                self.assertEqual(result, "plain")
                sender.assert_awaited_once()

        for operation in ("cleanup", "command"):
            with self.subTest(operation=operation):
                _run(scenario(operation))

    def test_disable_during_san_nian_records_reply_without_followup_listing(self):
        actor = self.avatar(equipped=True, pending=False)

        async def response(identity, command, **kwargs):
            actor.sent.append((identity, command))
            self.select(["sub|主魂"])
            return actor.responses[command]

        actor.send_and_wait_feedback_identity = response
        with patch.object(wt, "_wind_thunder_notify") as notify:
            self.assertFalse(_run(wt._cleanup(actor, "无咎子")))
        self.assertEqual(actor.sent, [("无咎子", ".散念 风雷翅")])
        state = actor.avatar_states["无咎子"]
        self.assertFalse(state["wind_thunder_equipped"])
        self.assertTrue(state["wind_thunder_list_pending"])
        self.assertEqual(state["wind_thunder_cleanup_due_at"], "")
        notify.assert_not_called()

    def test_disable_during_failed_listing_does_not_count_retry_or_alert(self):
        actor = self.avatar()

        async def response(identity, command, **kwargs):
            actor.sent.append((identity, command))
            self.select(["sub|主魂"])
            return "此宝似与储物袋中的因果牵连过深，本次放置失败，请稍后再试。"

        actor.send_and_wait_feedback_identity = response
        state = actor.avatar_states["无咎子"]
        with patch.object(wt, "_wind_thunder_notify") as notify:
            self.assertFalse(_run(wt._retry_listing(actor, "无咎子", state)))
        self.assertEqual(actor.sent, [("无咎子", ".上架至万宝阁 风雷翅")])
        self.assertEqual(state["wind_thunder_list_fail_count"], 5)
        self.assertEqual(state["wind_thunder_cleanup_due_at"], "")
        notify.assert_not_called()

    def test_disabled_stale_session_does_not_block_normal_command(self):
        self.select(["sub|主魂"])
        actor = self.avatar()
        sender = AsyncMock(return_value="plain")
        with patch.object(wt, "_wind_thunder_notify") as notify:
            result = _run(wt.wind_thunder_send(actor, "无咎子", ".探寻裂缝", sender))
        self.assertEqual(result, "plain")
        sender.assert_awaited_once()
        self.assertEqual(actor.sent, [])
        notify.assert_not_called()

    def test_disable_during_equipment_setup_stops_subsequent_operations(self):
        for disable_after in (".从万宝阁取下 风雷翅", ".装备 风雷翅"):
            with self.subTest(disable_after=disable_after):
                self.select(["main|无咎子"])
                actor = self.avatar(equipped=False, pending=False)

                async def response(identity, command, **kwargs):
                    actor.sent.append((identity, command))
                    if command == disable_after:
                        self.select(["sub|主魂"])
                    return "你已祭出【风雷翅】。" if command == ".装备 风雷翅" else "已取回【风雷翅】。"

                actor.send_and_wait_feedback_identity = response
                sender = AsyncMock(return_value="plain")
                with patch.object(wt, "_wind_thunder_notify") as notify:
                    self.assertEqual(_run(wt.wind_thunder_send(actor, "无咎子", ".探寻裂缝", sender)), "plain")
                expected = [("无咎子", ".从万宝阁取下 风雷翅")]
                if disable_after == ".装备 风雷翅":
                    expected.append(("无咎子", ".装备 风雷翅"))
                self.assertEqual(actor.sent, expected)
                self.assertEqual(actor.avatar_states["无咎子"]["wind_thunder_cleanup_due_at"], "")
                self.assertEqual(actor.avatar_states["无咎子"]["wind_thunder_equipped"], disable_after == ".装备 风雷翅")
                sender.assert_awaited_once()
                notify.assert_not_called()

    def test_reenabled_pending_session_resumes_listing_without_re_equipping(self):
        async def scenario():
            actor = self.avatar()
            self.select(["sub|主魂"])
            wt.recover_wind_thunder_sessions(actor)
            self.select(["main|无咎子", "sub|主魂"])
            sender = AsyncMock(return_value="plain")
            self.assertEqual(await wt.wind_thunder_send(actor, "无咎子", ".探寻裂缝", sender), "plain")
            await actor._wind_thunder_cleanup_tasks["无咎子"]
            self.assertEqual(actor.sent, [("无咎子", ".上架至万宝阁 风雷翅")])
            self.assertFalse(actor.avatar_states["无咎子"]["wind_thunder_list_pending"])
            sender.assert_awaited_once()

        _run(scenario())

    def test_manual_listing_confirmation_still_updates_disabled_identity(self):
        self.select(["sub|主魂"])
        actor = self.avatar()
        wt.recover_wind_thunder_sessions(actor)
        self.assertTrue(wt.sync_wind_thunder_manual_response(
            actor, "无咎子", ".上架至万宝阁 风雷翅", actor.responses[".上架至万宝阁 风雷翅"]
        ))
        state = actor.avatar_states["无咎子"]
        self.assertFalse(state["wind_thunder_list_pending"])
        self.assertEqual(state["wind_thunder_list_fail_count"], 0)
        self.assertEqual(state["wind_thunder_cleanup_due_at"], "")
        self.assertEqual(actor.sent, [])


if __name__ == "__main__":
    unittest.main()
