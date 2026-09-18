import asyncio
import json
import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from dashboard_server import is_dashboard_visible_log_entry, split_log_entries
from log_utils import CommandLogFilter
from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import MiniAppDwellingTransport
from miniapp_journey import MiniAppTianxingJourney
from tests.test_miniapp_journey import ENTRY, START, FakeActor, FakeLogger, SequenceTransport, journey_payload


class JourneySummaryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 18, 0, 30)
        clock_patch = patch("miniapp_journey.datetime", wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock_patch.stop)

    def runner(self, *, account="main", transport=None, logger=None, state=None):
        actor = FakeActor(account, avatars=["无咎子"], sects={"无咎子": "天星宗"})
        if state is not None:
            actor.state = json.loads(json.dumps(state))
        runner = MiniAppTianxingJourney(
            actor, transport or SequenceTransport(daily_limit=8), account, logger or FakeLogger(),
        )
        runner.configured_identities = lambda: ["无咎子"]
        runner.runtime_enabled = lambda: True
        return runner

    def advance(self, runner, attempts, identity="无咎子"):
        for _ in range(attempts):
            performed, _ = asyncio.run(runner.run_identity(identity))
            self.assertTrue(performed)
            self.now += timedelta(seconds=5)
            self.clock.now.return_value = self.now

    def command_messages(self, logger):
        log_filter = CommandLogFilter()
        return [message for message in logger.info_messages if log_filter.filter(
            logging.LogRecord("journey", logging.INFO, __file__, 1, message, (), None),
        )]

    def test_eight_real_transport_results_form_one_visible_log_entry(self):
        calls = []
        count = 0
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            nonlocal count
            calls.append(path)
            if path.endswith("/start"):
                return START
            if path.endswith("/details"):
                return journey_payload(count, daily_limit=8)
            if path.endswith("/command-center"):
                return {"ok": True, "actionResult": {"ok": True, "rawMessage": "探索命格已改定"}}
            if path.endswith("/journey"):
                count += 1
                return journey_payload(count, daily_limit=8, message=f"第 {count} 次历练\n修为 +{100 + count}")
            self.fail(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, logger=logger, post_json=post_json)
        runner = self.runner(transport=transport, logger=logger)
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            self.advance(runner, 7)
            self.assertEqual(self.command_messages(logger), [])
            self.advance(runner, 1)
            self.assertFalse(asyncio.run(runner.run_once())[0])

        self.assertEqual(count, 8)
        self.assertEqual(sum(path.endswith("/command-center") for path in calls), 8)
        messages = self.command_messages(logger)
        self.assertEqual(len(messages), 1)
        summary = messages[0]
        self.assertIn("IN [Mini App | 无咎子]:\n野外历练汇总", summary)
        self.assertIn("今日完成 8/8 次", summary)
        for index in range(1, 9):
            self.assertIn(f"第 {index} 次历练 修为 +{100 + index}", summary)
        self.assertEqual(summary.count("改命探索：探索命格已改定"), 8)
        self.assertEqual(len(runner.actor.rewards), 8)
        self.assertTrue(runner.actor.saved_state["avatars"]["无咎子"]["miniapp_journey_summary"]["emitted"])

        record = logging.LogRecord("journey", logging.INFO, __file__, 1, summary, (), None)
        self.assertTrue(CommandLogFilter().filter(record))
        raw = "2026-09-18 00:30:35 [INFO] " + summary + "\n"
        entries = split_log_entries(raw.splitlines(keepends=True))
        self.assertEqual(len(entries), 1)
        self.assertTrue(is_dashboard_visible_log_entry(entries[0]))

        # Other callers sharing this transport retain their normal command logs.
        with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
            asyncio.run(transport.command(".观命", identity="无咎子"))
        messages = self.command_messages(logger)
        self.assertEqual(len(messages), 3)
        self.assertIn("OUT [Mini App | 无咎子]", messages[1])

    def test_partial_journal_survives_restart_and_summary_is_not_repeated(self):
        runner = self.runner()
        self.advance(runner, 3)
        self.assertEqual(runner.log.info_messages, [])
        restored = self.runner(transport=runner.transport, state=runner.actor.saved_state)
        self.advance(restored, 5)
        self.assertEqual(len(restored.log.info_messages), 1)
        summary = restored.log.info_messages[0]
        for index in range(1, 9):
            self.assertIn(f"第 {index} 次奖励", summary)

        again = self.runner(transport=runner.transport, state=restored.actor.saved_state)
        again.actor.get_avatar_state("无咎子")["miniapp_journey_next_run_time"] = ""
        self.assertFalse(asyncio.run(again.run_once())[0])
        self.assertEqual(again.log.info_messages, [])
        self.assertEqual(len([call for call in runner.transport.calls if call[0] == "journey"]), 8)

    def test_success_before_failed_read_remains_in_the_final_summary(self):
        runner = self.runner()
        transport = runner.transport
        original_snapshot = transport.journey_snapshot
        transport.journey_snapshot = AsyncMock(side_effect=[
            journey_payload(0, daily_limit=8), MiniAppBeastError("read_failed"),
        ])
        self.assertEqual(asyncio.run(runner.run_once()), (False, runner.retry_seconds))
        self.assertEqual(len(runner.actor.saved_state["avatars"]["无咎子"]["miniapp_journey_summary"]["records"]), 1)
        self.assertEqual(runner.log.info_messages, [])
        self.assertIn("read_failed", runner.log.error_messages[0])

        transport.journey_snapshot = original_snapshot
        restored = self.runner(transport=transport, state=runner.actor.saved_state)
        self.now += timedelta(seconds=runner.retry_seconds)
        self.clock.now.return_value = self.now
        self.advance(restored, 7)
        self.assertEqual(len(restored.log.info_messages), 1)
        self.assertIn("第 1 次奖励", restored.log.info_messages[0])
        self.assertEqual(transport.counts["无咎子"], 8)

    def test_eighth_success_emits_even_if_its_followup_read_fails(self):
        runner = self.runner()
        self.advance(runner, 7)
        runner.transport.journey_snapshot = AsyncMock(side_effect=[
            journey_payload(7, daily_limit=8), MiniAppBeastError("read_failed"),
        ])
        asyncio.run(runner.run_once())
        self.assertEqual(len(runner.log.info_messages), 1)
        self.assertIn("今日完成 8/8 次", runner.log.info_messages[0])
        self.assertIn("第 8 次奖励", runner.log.info_messages[0])
        batch = runner.actor.saved_state["avatars"]["无咎子"]["miniapp_journey_summary"]
        self.assertEqual(len(batch["records"]), 8)
        self.assertTrue(batch["emitted"])

    def test_confirmed_last_action_without_counter_still_finishes_summary(self):
        runner = self.runner()
        self.advance(runner, 7)
        runner.transport.journey_action = AsyncMock(return_value={
            "ok": True, "actionResult": {"ok": True, "rawMessage": "最后一次奖励"},
        })
        runner.transport.journey_snapshot = AsyncMock(side_effect=[
            journey_payload(7, daily_limit=8), MiniAppBeastError("read_failed"),
        ])
        asyncio.run(runner.run_once())
        self.assertEqual(len(runner.log.info_messages), 1)
        self.assertIn("今日完成 8/8 次", runner.log.info_messages[0])
        self.assertIn("最后一次奖励", runner.log.info_messages[0])

    def test_preexisting_server_attempts_are_reported_without_inventing_details(self):
        runner = self.runner(transport=SequenceTransport(count=5, daily_limit=8))
        self.advance(runner, 3)
        self.assertEqual(len(runner.log.info_messages), 1)
        summary = runner.log.info_messages[0]
        self.assertIn("今日完成 8/8 次", summary)
        self.assertIn("本条记录 3 次；其余 5 次无本地明细", summary)
        self.assertNotIn("第 1 次奖励", summary)
        self.assertIn("第 8 次奖励", summary)

    def test_already_exhausted_quota_without_journal_does_not_fabricate_a_summary(self):
        runner = self.runner(transport=SequenceTransport(count=8, daily_limit=8))
        self.assertFalse(asyncio.run(runner.run_once())[0])
        self.assertEqual(runner.log.info_messages, [])
        self.assertFalse(any(call[0] == "journey" for call in runner.transport.calls))

    def test_daily_rollover_flushes_partial_results_and_starts_a_separate_batch(self):
        runner = self.runner()
        self.advance(runner, 3)
        self.now = datetime(2026, 9, 19, 0, 30)
        self.clock.now.return_value = self.now
        runner.transport.counts.clear()
        self.advance(runner, 8)
        self.assertEqual(len(runner.log.info_messages), 2)
        previous, current = runner.log.info_messages
        self.assertIn("2026-09-18 · 今日完成 3/8 次，跨日汇总", previous)
        self.assertIn("2026-09-19 · 今日完成 8/8 次", current)
        self.assertEqual(len(runner.actor.get_avatar_state("无咎子")["miniapp_journey_summary"]["records"]), 8)

    def test_accounts_and_identities_keep_separate_journals(self):
        main = self.runner(transport=SequenceTransport(daily_limit=2))
        sub = self.runner(account="sub", transport=SequenceTransport(daily_limit=2))
        self.advance(main, 1, "主魂")
        self.advance(main, 1)
        self.advance(sub, 1)
        self.assertEqual(main.log.info_messages, [])
        self.assertEqual(sub.log.info_messages, [])

        self.advance(main, 1)
        self.assertEqual(len(main.log.info_messages), 1)
        self.assertTrue(main.log.info_messages[0].startswith("IN [Mini App | 无咎子]:"))
        self.assertFalse(main.actor.state["miniapp_journey_summary"]["complete"])
        self.assertEqual(len(sub.actor.get_avatar_state("无咎子")["miniapp_journey_summary"]["records"]), 1)
        self.advance(main, 1, "主魂")
        self.advance(sub, 1)
        self.assertEqual(len(main.log.info_messages), 2)
        self.assertTrue(main.log.info_messages[1].startswith("IN [Mini App | 主魂]:"))
        self.assertEqual(len(sub.log.info_messages), 1)

    def test_prefix_failure_is_immediate_and_does_not_count_as_an_attempt(self):
        runner = self.runner(transport=SequenceTransport(prefix_ok=False, daily_limit=8))
        self.assertFalse(asyncio.run(runner.run_once())[0])
        self.assertEqual(runner.log.info_messages, [])
        self.assertIn("命格改定失败", runner.log.error_messages[0])
        self.assertNotIn("miniapp_journey_summary", runner.actor.get_avatar_state("无咎子"))
        self.assertFalse(any(call[0] == "journey" for call in runner.transport.calls))

    def test_failed_action_receipt_does_not_count_as_a_success(self):
        runner = self.runner()
        runner.transport.journey_action = AsyncMock(return_value={
            "ok": True, "actionResult": {"ok": False, "rawMessage": "历练未开始"},
        })
        self.assertFalse(asyncio.run(runner.run_once())[0])
        self.assertEqual(runner.log.info_messages, [])
        self.assertIn("历练未开始", runner.log.error_messages[0])
        self.assertNotIn("miniapp_journey_summary", runner.actor.get_avatar_state("无咎子"))
        self.assertEqual(runner.actor.rewards, [])

    def test_quiet_transport_still_logs_action_errors(self):
        logger = FakeLogger()

        async def post_json(origin, path, payload, timeout):
            if path.endswith("/start"):
                return START
            if path.endswith("/command-center"):
                return {"ok": True, "actionResult": {"ok": True, "rawMessage": "探索命格已改定"}}
            raise MiniAppBeastError("journey_network_failed")

        for combined in (False, True):
            with self.subTest(combined=combined):
                transport = MiniAppDwellingTransport(object(), ENTRY, logger=logger, post_json=post_json)
                action = transport.journey_with_destiny_prefix if combined else transport.journey_action
                with patch("miniapp_dwelling.request_webview_init_data", new=AsyncMock(return_value="signed")):
                    with self.assertRaises(MiniAppBeastError):
                        asyncio.run(action("无咎子", log_operation=False))
        self.assertEqual(self.command_messages(logger), [])
        self.assertEqual(len(logger.error_messages), 2)
        self.assertTrue(all("journey_network_failed" in message for message in logger.error_messages))

    def test_fallback_prefix_and_action_are_quiet(self):
        runner = self.runner()
        runner.transport.journey_with_destiny_prefix = None
        self.advance(runner, 8)
        self.assertEqual(runner.transport.log_operations, [False] * 16)
        self.assertEqual(len(runner.log.info_messages), 1)

    def test_pause_keeps_the_partial_journal_without_consuming_more_quota(self):
        runner = self.runner()
        self.advance(runner, 3)
        before = json.dumps(runner.actor.state, sort_keys=True)
        with patch("miniapp_journey.command_paused", return_value=True):
            self.assertEqual(asyncio.run(runner.run_identity("无咎子")), (False, 60))
        self.assertEqual(json.dumps(runner.actor.state, sort_keys=True), before)
        self.assertEqual(runner.transport.counts["无咎子"], 3)
        self.assertEqual(runner.log.info_messages, [])


if __name__ == "__main__":
    unittest.main()
