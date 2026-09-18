import asyncio
import copy
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dashboard_server import build_command_panels
from miniapp_dwelling import MiniAppCommandResponse, MiniAppDwellingTransport
from miniapp_journey import JOURNEY_INTERVAL_SECONDS, MiniAppTianxingJourney, journey_counter
from miniapp_beast import MiniAppBeastError
from restricted_miniapp_worker import RestrictedMiniAppWorker


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
START = {
    "ok": True,
    "identity": {
        "selectedPlayerId": 100,
        "choices": [
            {
                "playerId": 100,
                "daoName": "主号道名",
                "username": "MainUser",
                "source": "personal",
            },
            {
                "playerId": -201,
                "daoName": "无咎子",
                "username": "wuxinglinggen",
                "source": "bound_character",
            },
        ],
    },
}


def journey_payload(count=0, *, available=True, remaining_seconds=0, message="", daily_limit=None):
    payload = {
        "ok": True,
        "account": {
            "journey": {
                "wildExperience": {
                    "available": available,
                    "dailyCount": count,
                    "remainingSeconds": remaining_seconds,
                    "modes": [{"key": "deep", "label": "深入"}],
                }
            }
        },
    }
    if daily_limit is not None:
        payload["account"]["journey"]["wildExperience"].update(
            dailyLimit=daily_limit, dailyRemaining=max(0, daily_limit - count),
        )
    if message:
        payload["actionResult"] = {"ok": True, "rawMessage": message}
    return payload


class FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message, *args, **kwargs):
        self.info_messages.append(message % args if args else message)

    def warning(self, *args, **kwargs):
        pass

    def error(self, message, *args, **kwargs):
        self.error_messages.append(message % args if args else message)

    def critical(self, *args, **kwargs):
        pass


class FakeActor:
    def __init__(self, account="main", avatars=None, sects=None):
        self.account_key = account
        self.client = object()
        self.config = {
            "miniapp_beast": {
                "entry_url": ENTRY,
                "journey_action_delay_seconds": 0,
            }
        }
        self.avatars = list(avatars or [])
        self.state = {"avatars": {name: {} for name in self.avatars}}
        self.identity_sect_names = dict(sects or {})
        self.state["identity_sect_names"] = dict(self.identity_sect_names)
        self.saved = 0
        self.rewards = []
        self.is_running = True
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self.pause_event = asyncio.Event()
        self.pause_event.set()

    def get_avatar_state(self, identity):
        return self.state["avatars"][identity]

    def save_state(self):
        self.saved += 1
        self.saved_state = copy.deepcopy(self.state)

    def identity_pause_seconds(self, identity):
        return 0

    def record_daily_reward_event(self, identity, command, text, **kwargs):
        self.rewards.append((identity, command, text, kwargs))
        return True


class SequenceTransport:
    def __init__(self, count=0, *, prefix_ok=True, available=True, remaining_seconds=0, daily_limit=None):
        self.identity_player_ids = {"无咎子": -201, "主魂": 100}
        self.count = count
        self.prefix_ok = prefix_ok
        self.available = available
        self.remaining_seconds = remaining_seconds
        self.daily_limit = daily_limit
        self.calls = []
        self.log_operations = []
        self.counts = {}

    async def initialize(self):
        self.calls.append(("initialize",))
        return START

    async def journey_snapshot(self, identity):
        self.calls.append(("snapshot", identity))
        available = self.available
        return journey_payload(
            self.counts.get(identity, self.count),
            available=available,
            remaining_seconds=self.remaining_seconds,
            daily_limit=self.daily_limit,
        )

    async def command(self, command, identity="主魂", log_operation=True):
        self.calls.append(("command", identity, command))
        self.log_operations.append(log_operation)
        payload = {
            "ok": True,
            "actionResult": {
                "ok": self.prefix_ok,
                "rawMessage": "探索命格已改定" if self.prefix_ok else "命格改定失败",
            },
        }
        return MiniAppCommandResponse(payload["actionResult"]["rawMessage"], payload)

    async def journey_action(self, identity, mode="deep", log_operation=True):
        self.calls.append(("journey", identity, mode))
        self.log_operations.append(log_operation)
        count = self.counts.get(identity, self.count) + 1
        self.counts[identity] = count
        return journey_payload(
            count,
            available=self.available,
            message=f"深入历练完成，第 {count} 次奖励",
            daily_limit=self.daily_limit,
        )

    async def journey_with_destiny_prefix(
        self,
        identity,
        prefix_command=".改命 探索",
        mode="deep",
        log_operation=True,
    ):
        prefix = await self.command(prefix_command, identity=identity, log_operation=log_operation)
        if not self.prefix_ok:
            return prefix, None
        return prefix, await self.journey_action(identity, mode=mode, log_operation=log_operation)


class MiniAppJourneyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 0, 30)
        clock_patch = patch("miniapp_journey.datetime", wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock_patch.stop)

    def runner(self, **transport_options):
        actor = FakeActor("main", avatars=["无咎子"], sects={"无咎子": "天星宗"})
        transport = SequenceTransport(**transport_options)
        runner = MiniAppTianxingJourney(actor, transport, "main", FakeLogger())
        runner.configured_identities = lambda: ["无咎子"]
        return actor, transport, runner

    def test_counter_uses_server_limit_without_a_local_two_attempt_cap(self):
        payload = journey_payload(0)
        wild = payload["account"]["journey"]["wildExperience"]
        wild.update({"dailyLimit": 3, "dailyRemaining": 3})

        counter = journey_counter(payload)

        self.assertEqual(counter["daily_limit"], 3)
        self.assertEqual(counter["daily_remaining"], 3)

    def test_transport_uses_journey_endpoint_and_deep_mode(self):
        calls = []
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/start"):
                return START
            if path.endswith("/details"):
                return journey_payload(0, daily_limit=2)
            if path.endswith("/command-center"):
                return {
                    "ok": True,
                    "actionResult": {"ok": True, "rawMessage": "探索命格已改定"},
                }
            if path.endswith("/journey"):
                return journey_payload(1, message="深入历练完成")
            self.fail(path)

        transport = MiniAppDwellingTransport(
            object(),
            ENTRY,
            logger=logger,
            post_json=post_json,
        )
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            snapshot = asyncio.run(transport.journey_snapshot("无咎子"))
            prefix, result = asyncio.run(
                transport.journey_with_destiny_prefix("无咎子", mode="deep")
            )

        self.assertEqual(journey_counter(snapshot)["daily_remaining"], 2)
        action = next(call for call in calls if call[0].endswith("/journey"))
        self.assertEqual(action[1]["playerId"], -201)
        self.assertEqual(action[1]["action"], "wild_experience")
        self.assertEqual(action[1]["mode"], "deep")
        self.assertEqual(prefix.text, "探索命格已改定")
        self.assertEqual(result["actionResult"]["rawMessage"], "深入历练完成")
        combined = "\n".join(logger.info_messages)
        self.assertNotIn("同步洞府详情", combined)
        self.assertIn("OUT [Mini App | 无咎子]:\n游历·野外历练（深入）", combined)
        self.assertIn("IN [Mini App | 无咎子]:\n游历·野外历练（深入） -> 深入历练完成", combined)

    def test_only_explicit_dashboard_identities_are_selected(self):
        main_actor = FakeActor(
            "main",
            avatars=["无咎子", "其他天星"],
            sects={"主魂": "天星宗", "无咎子": "天星宗", "其他天星": "天星宗"},
        )
        main_transport = SequenceTransport()
        main_transport.identity_player_ids["其他天星"] = -202
        main_runner = MiniAppTianxingJourney(main_actor, main_transport, "main", FakeLogger())

        waaiging_actor = FakeActor("waaiging", sects={"主魂": "天星宗"})
        waaiging_runner = MiniAppTianxingJourney(
            waaiging_actor,
            SequenceTransport(),
            "waaiging",
            FakeLogger(),
        )
        sub_actor = FakeActor("sub", avatars=["无咎子"], sects={"无咎子": "星宫"})
        sub_runner = MiniAppTianxingJourney(sub_actor, SequenceTransport(), "sub", FakeLogger())

        self.assertEqual(main_runner.identities(), ["主魂", "无咎子"])
        self.assertEqual(waaiging_runner.identities(), ["主魂"])
        self.assertEqual(sub_runner.identities(), [])
        self.assertTrue(sub_runner.supported)
        self.assertTrue(sub_runner.enabled)

    def test_dashboard_selection_can_enable_non_tianxing_identity(self):
        sub_actor = FakeActor("sub", avatars=["厚土"], sects={"厚土": "星宫"})
        sub_transport = SequenceTransport()
        sub_transport.identity_player_ids["厚土"] = -301
        sub_runner = MiniAppTianxingJourney(
            sub_actor,
            sub_transport,
            "sub",
            FakeLogger(),
        )

        with patch(
            "miniapp_journey.miniapp_journey_identities_for_account",
            return_value=["厚土"],
        ):
            self.assertEqual(sub_runner.identities(), ["厚土"])

    def test_non_tianxing_identity_skips_destiny_prefix(self):
        actor = FakeActor("sub", avatars=["厚土"], sects={"厚土": "星宫"})
        transport = SequenceTransport(count=0)
        transport.identity_player_ids["厚土"] = -301
        runner = MiniAppTianxingJourney(actor, transport, "sub", FakeLogger())

        with patch(
            "miniapp_journey.miniapp_journey_identities_for_account",
            return_value=["厚土"],
        ):
            complete, retry = asyncio.run(
                runner.run_once(self.now)
            )

        self.assertTrue(complete)
        self.assertEqual(retry, 5)
        self.assertEqual(
            [call for call in transport.calls if call[0] in {"command", "journey"}],
            [
                ("journey", "厚土", "deep"),
            ],
        )
        state = actor.state["avatars"]["厚土"]
        self.assertEqual(state["miniapp_journey_last_prefix"], "")

    def test_consecutive_attempts_each_have_a_confirmed_destiny_prefix(self):
        actor = FakeActor("main", avatars=["无咎子"], sects={"无咎子": "天星宗"})
        transport = SequenceTransport(count=0)
        runner = MiniAppTianxingJourney(actor, transport, "main", FakeLogger())
        runner.configured_identities = lambda: ["无咎子"]

        complete, retry = asyncio.run(
            runner.run_once(self.now)
        )

        self.assertTrue(complete)
        self.assertEqual(retry, 5)
        # A premature retry performs neither the prefix nor the action.
        first_calls = list(transport.calls)
        self.clock.now.return_value = self.now + timedelta(seconds=2)
        self.assertEqual(asyncio.run(runner.run_identity("无咎子")), (False, 3))
        self.assertEqual(transport.calls, first_calls)
        self.clock.now.return_value = self.now + timedelta(seconds=5)
        self.assertEqual(asyncio.run(runner.run_once()), (True, 5))
        actions = [call for call in transport.calls if call[0] in {"command", "journey"}]
        self.assertEqual(
            actions,
            [
                ("command", "无咎子", ".改命 探索"),
                ("journey", "无咎子", "deep"),
                ("command", "无咎子", ".改命 探索"),
                ("journey", "无咎子", "deep"),
            ],
        )
        state = actor.state["avatars"]["无咎子"]
        self.assertEqual(state["miniapp_journey_daily_count"], 2)
        self.assertEqual(state["miniapp_journey_last_date"], "2026-09-17")
        self.assertEqual(len(actor.rewards), 2)

    def test_used_daily_counter_does_not_prevent_a_due_action(self):
        actor = FakeActor("main", avatars=["无咎子"], sects={"无咎子": "天星宗"})
        transport = SequenceTransport(count=5)
        runner = MiniAppTianxingJourney(actor, transport, "main", FakeLogger())
        runner.configured_identities = lambda: ["无咎子"]

        complete, _ = asyncio.run(
            runner.run_once(self.now)
        )

        self.assertTrue(complete)
        self.assertEqual(
            [call[0] for call in transport.calls if call[0] in {"command", "journey"}],
            ["command", "journey"],
        )

    def test_prefix_failure_blocks_the_deep_click(self):
        actor = FakeActor("main", avatars=["无咎子"], sects={"无咎子": "天星宗"})
        transport = SequenceTransport(prefix_ok=False)
        runner = MiniAppTianxingJourney(actor, transport, "main", FakeLogger())
        runner.configured_identities = lambda: ["无咎子"]

        complete, retry = asyncio.run(
            runner.run_once(self.now)
        )

        self.assertFalse(complete)
        self.assertEqual(retry, runner.retry_seconds)
        self.assertFalse(any(call[0] == "journey" for call in transport.calls))
        self.assertEqual(
            actor.state["avatars"]["无咎子"]["miniapp_journey_last_error"],
            "journey_destiny_prefix_failed",
        )

    def test_server_cooldown_sets_exact_retry_without_clicking(self):
        actor = FakeActor("main", avatars=["无咎子"], sects={"无咎子": "天星宗"})
        transport = SequenceTransport(count=1, available=False, remaining_seconds=1234)
        runner = MiniAppTianxingJourney(actor, transport, "main", FakeLogger())

        complete, retry = asyncio.run(
            runner.run_once(self.now)
        )

        self.assertFalse(complete)
        self.assertEqual(retry, 1234)
        self.assertFalse(any(call[0] in {"command", "journey"} for call in transport.calls))

    def test_server_cooldown_without_daily_quota_supports_repeated_actions(self):
        counter = journey_counter(journey_payload(9))
        self.assertTrue(counter["available"])
        self.assertIsNone(counter["daily_remaining"])
        self.assertEqual(counter["daily_count"], 9)
        self.assertEqual(counter["daily_limit"], 0)


    def test_ready_at_without_remaining_seconds_is_respected(self):
        payload = journey_payload(4)
        payload["account"]["journey"]["wildExperience"]["readyAt"] = (
            self.now + timedelta(seconds=1234, milliseconds=100)
        ).isoformat()
        counter = journey_counter(payload, now=self.now)
        self.assertFalse(counter["available"])
        self.assertEqual(counter["remaining_seconds"], 1235)

    def test_old_daily_completion_and_tomorrow_schedule_are_migrated(self):
        actor, transport, runner = self.runner(count=2)
        state = actor.state["avatars"]["无咎子"]
        state.update(
            miniapp_journey_last_date="2026-09-17",
            miniapp_journey_daily_count=2,
            miniapp_journey_daily_limit=2,
            miniapp_journey_last_time="2026-09-16 21:00:00",
            miniapp_journey_next_run_time="2026-09-18 07:00:00",
        )
        self.assertEqual(asyncio.run(runner.run_once()), (True, 5))
        self.assertEqual([call for call in transport.calls if call[0] == "journey"], [("journey", "无咎子", "deep")])
        self.assertEqual(state["miniapp_journey_next_run_time"], "2026-09-17 00:30:05")

    def test_migration_discards_the_obsolete_three_hour_cooldown(self):
        actor, transport, runner = self.runner()
        state = actor.state["avatars"]["无咎子"]
        state.update(miniapp_journey_last_time="2026-09-16 23:30:00", miniapp_journey_next_run_time="2026-09-17 07:00:00")
        self.assertEqual(asyncio.run(runner.run_once()), (True, 5))
        self.assertEqual(state["miniapp_journey_next_run_time"], "2026-09-17 00:30:05")
        self.assertEqual(len([call for call in transport.calls if call[0] == "journey"]), 1)

    def test_restart_preserves_a_server_cooldown(self):
        actor, transport, runner = self.runner(available=False, remaining_seconds=1234)
        self.assertEqual(asyncio.run(runner.run_once()), (False, 1234))
        due = actor.state["avatars"]["无咎子"]["miniapp_journey_next_run_time"]
        restored = MiniAppTianxingJourney(actor, transport, "main", FakeLogger())
        restored.configured_identities = runner.configured_identities
        transport.calls.clear()
        self.clock.now.return_value = self.now + timedelta(seconds=100)
        self.assertEqual(asyncio.run(restored.run_once()), (False, 1134))
        self.assertEqual(actor.state["avatars"]["无咎子"]["miniapp_journey_next_run_time"], due)
        self.assertFalse(any(call[0] == "snapshot" for call in transport.calls))

    def test_success_is_persisted_even_when_followup_snapshot_fails(self):
        actor, transport, runner = self.runner()
        transport.journey_snapshot = AsyncMock(side_effect=[journey_payload(), MiniAppBeastError("read_failed")])
        _, retry = asyncio.run(runner.run_once())
        self.assertEqual(retry, runner.retry_seconds)
        state = actor.saved_state["avatars"]["无咎子"]
        self.assertEqual(state["miniapp_journey_next_run_time"], "2026-09-17 00:45:00")
        self.assertEqual(state["miniapp_journey_last_time"], "2026-09-17 00:30:00")
        self.assertEqual(state["miniapp_journey_summary"]["records"][0]["result"], "深入历练完成，第 1 次奖励")
        self.clock.now.return_value = self.now + timedelta(minutes=14)
        self.assertEqual(asyncio.run(runner.run_identity("无咎子")), (False, 60))
        self.assertEqual(len([call for call in transport.calls if call[0] == "journey"]), 1)

    def test_success_uses_longer_server_cooldown(self):
        actor, transport, runner = self.runner()
        transport.journey_snapshot = AsyncMock(side_effect=[journey_payload(), journey_payload(1, available=False, remaining_seconds=14400)])
        self.assertEqual(asyncio.run(runner.run_once()), (True, 14400))
        self.assertEqual(actor.state["avatars"]["无咎子"]["miniapp_journey_next_run_time"], "2026-09-17 04:30:00")

    def test_explicit_server_quota_waits_for_reset(self):
        actor, transport, runner = self.runner()
        payload = journey_payload(3, daily_limit=3)
        payload["account"]["journey"]["wildExperience"]["resetAt"] = "2026-09-17 02:00:00"
        transport.journey_snapshot = AsyncMock(return_value=payload)
        self.assertEqual(asyncio.run(runner.run_once()), (False, 5400))
        self.assertFalse(any(call[0] == "journey" for call in transport.calls))

    def test_race_at_action_reads_server_cooldown_instead_of_retrying_action(self):
        actor, transport, runner = self.runner()
        transport.journey_action = AsyncMock(side_effect=MiniAppBeastError("wild_experience_unavailable"))
        transport.journey_snapshot = AsyncMock(side_effect=[journey_payload(), journey_payload(1, available=False, remaining_seconds=321)])
        self.assertEqual(asyncio.run(runner.run_once()), (False, 321))
        transport.journey_action.assert_awaited_once()
        self.assertEqual(actor.state["avatars"]["无咎子"]["miniapp_journey_last_error"], "")

    def test_main_and_avatar_keep_separate_deadlines(self):
        actor, transport, runner = self.runner()
        runner.configured_identities = lambda: ["主魂", "无咎子"]
        actor.state.update(miniapp_journey_ready_at="2026-09-17 02:00:00")
        actor.state["avatars"]["无咎子"]["miniapp_journey_ready_at"] = "2026-09-17 01:00:00"
        self.assertEqual(asyncio.run(runner.run_once()), (False, 1800))
        runner._record_next_schedule(self.now + timedelta(seconds=1800), runner.identities())
        self.assertEqual(actor.state["miniapp_journey_next_run_time"], "2026-09-17 02:00:00")
        self.assertEqual(actor.state["avatars"]["无咎子"]["miniapp_journey_next_run_time"], "2026-09-17 01:00:00")
        self.assertEqual(actor.state["miniapp_journey_next_cycle_time"], "2026-09-17 01:00:00")

    def test_loop_runs_overnight_and_reloads_selection_before_cooldown_expires(self):
        actor, transport, runner = self.runner()
        runner.run_once = AsyncMock(return_value=(True, JOURNEY_INTERVAL_SECONDS))
        async def stop_after_sleep(seconds):
            self.assertLessEqual(seconds, 60)
            actor.is_running = False
        with patch("miniapp_journey.asyncio.sleep", side_effect=stop_after_sleep):
            asyncio.run(runner.run_loop())
        runner.run_once.assert_awaited_once()


    def test_dashboard_adds_journey_to_all_three_scoped_panels(self):
        main_state = {
            "sect_name": "天星宗",
            "identity_sect_names": {"主魂": "天星宗", "无咎子": "天星宗"},
            "miniapp_journey_daily_count": 0,
            "miniapp_journey_daily_limit": 2,
            "avatars": {
                "无咎子": {"miniapp_journey_daily_count": 1, "miniapp_journey_daily_limit": 2},
                "缘生子": {},
            }
        }
        main_panels = {panel["identity"]: panel for panel in build_command_panels("main", main_state)}
        waaiging_panels = build_command_panels("waaiging", {"sect_name": "天星宗"})

        self.assertIn(
            "miniapp:journey-deep",
            {row["command"] for row in main_panels["无咎子"]["commands"]},
        )
        main_commands = {row["command"] for row in main_panels["主魂"]["commands"]}
        self.assertIn("miniapp:journey-deep", main_commands)
        self.assertIn(".推命 闭关", main_commands)
        self.assertIn(".观命", main_commands)
        unselected = next(row for row in main_panels["缘生子"]["commands"]
                          if row["command"] == "miniapp:journey-deep")
        self.assertTrue(unselected["controllable"])
        self.assertFalse(unselected["actionable"])
        self.assertNotIn("next_seconds", unselected)
        waaiging_row = next(
            row
            for row in waaiging_panels[0]["commands"]
            if row["command"] == "miniapp:journey-deep"
        )
        self.assertEqual(waaiging_row["execution_channel"], "miniapp")
        self.assertIn("天星宗先推命/改命", waaiging_row["detail"])

    def test_dashboard_adds_journey_to_newly_selected_sub_identity(self):
        state = {
            "avatars": {
                "厚土": {
                    "miniapp_journey_daily_count": 0,
                    "miniapp_journey_daily_limit": 2,
                }
            }
        }
        with patch(
            "dashboard_server.miniapp_journey_identities_for_account",
            return_value=["厚土"],
        ):
            panels = {
                panel["identity"]: panel
                for panel in build_command_panels("sub", state)
            }

        self.assertIn(
            "miniapp:journey-deep",
            {row["command"] for row in panels["厚土"]["commands"]},
        )

    def test_restricted_waaiging_worker_starts_journey_loop(self):
        class Actor(FakeActor):
            def __init__(self):
                super().__init__("waaiging", sects={"主魂": "天星宗"})
                self.config["miniapp_beast"].update({
                    "pagoda_daily_enabled": False,
                    "hunt_daily_enabled": False,
                })
                self.config["restricted_miniapp"] = {"enabled": True}
                self._current_identity = ""
                self._main_confirmed = False

            async def send_and_wait_feedback(self, *args, **kwargs):
                return None

            async def send_and_wait_feedback_identity(self, *args, **kwargs):
                return None

            async def run_custom_command_loop(self):
                return None

            async def run_meditation_timer(self):
                return None

            async def run_yuanying_out_loop(self):
                return None

            async def run_tianxing_destiny_loop(self):
                return None

            run_sect_daily_loop = run_tianxing_destiny_loop
            run_tianxing_tianji_grind_loop = run_tianxing_destiny_loop

        actor = Actor()
        worker = RestrictedMiniAppWorker(actor, "waaiging", logger=FakeLogger())
        worker.transport.identity_player_ids = {"主魂": 100}
        worker.transport.initialize = AsyncMock(return_value=START)
        worker.sync_all_details = AsyncMock()
        spawned = []

        def capture(name, coroutine):
            spawned.append(name)
            coroutine.close()
            return SimpleNamespace(cancel=lambda: None)

        worker._spawn = capture
        asyncio.run(worker.start())

        self.assertIn("journey", spawned)


if __name__ == "__main__":
    unittest.main()
