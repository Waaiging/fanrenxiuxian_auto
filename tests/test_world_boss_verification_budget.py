"""Registration prewarm and bounded shared-browser waits after the Sep 14 incident."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error

from websocket import WebSocketTimeoutException

from miniapp_beast import MiniAppBeastError
from tests.test_world_boss_features import DummyMessage, FakeActor
from world_boss_browser import AutomaticTurnstileWorker, BrowserVerificationError, NativeTurnstileBrowser, ORIGIN, find_chrome
from world_boss_features import WorldBossMonitor, extract_world_boss_entry
from world_boss_turnstile import BROWSER_WARMUP_SECONDS, WorldBossTurnstileBroker


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0, seconds)


def request(broker, account='main'):
    return broker.create_request(event_fingerprint='f' * 64, message_id=123,
                                 account=account, identity='主魂', challenge_id='fixture', origin=ORIGIN)


class WarmingBrowser:
    def __init__(self, clock, *, fail_first=False, warmup_error=False):
        self.clock = clock
        self.prepared = 0
        self.budgets = []
        self.closed = 0
        self.fail_first = fail_first
        self.warmup_error = warmup_error

    def prepare(self, origin, *, timeout, still_pending):
        if not still_pending():
            raise BrowserVerificationError('verification_request_finished')
        self.prepared += 1
        self.clock.sleep(min(3, timeout))
        if self.warmup_error:
            raise BrowserVerificationError('browser_connection_timeout')

    def verify(self, origin, *, timeout, on_event, still_pending):
        self.budgets.append(timeout)
        if self.fail_first and len(self.budgets) == 1:
            self.clock.sleep(timeout)
            on_event('widget_timeout')
            raise BrowserVerificationError('turnstile_browser_timeout')
        self.clock.sleep(min(10, timeout))
        on_event('token_generated')
        return 'one-shot-fixture-' + str(len(self.budgets))

    def close(self):
        self.closed += 1


class RegistrationWarmupTests(unittest.TestCase):
    def test_four_notices_share_one_fixed_deadline_and_cannot_reopen_used_warmup(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            brokers = [WorldBossTurnstileBroker(directory, clock=clock) for _ in range(4)]
            for broker in brokers:
                self.assertTrue(broker.request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1000))
            first = brokers[0].get_warmup()
            self.assertEqual(first['expires_epoch'], 1000 + BROWSER_WARMUP_SECONDS)
            clock.sleep(40)
            brokers[1].request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1040)
            self.assertEqual(brokers[0].get_warmup(), first)
            brokers[2].finish_warmup('f' * 64)
            self.assertFalse(brokers[3].request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1040))
            self.assertIsNone(brokers[0].get_warmup())
            self.assertEqual(brokers[0].list_requests(), [])
            self.assertFalse(list(Path(directory).glob('token_*')))

    def test_old_future_invalid_and_untrusted_requests_do_not_launch_a_warmup(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            for notice in (0, 910, 1006, float('nan'), float('inf'), None):
                self.assertFalse(broker.request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=notice))
            self.assertFalse(broker.request_warmup(event_fingerprint='f' * 64, origin='https://other.example', notice_epoch=1000))
            self.assertFalse(broker.request_warmup(event_fingerprint='../../fixture', origin=ORIGIN, notice_epoch=1000))
            self.assertIsNone(broker.get_warmup())

    def test_ready_browser_survives_registration_then_closes_when_queue_finishes(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            broker.request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1000)
            browser = WarmingBrowser(clock)
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            self.assertFalse(worker.run_once())
            clock.sleep(40)
            self.assertFalse(worker.run_once())
            self.assertEqual(browser.prepared, 1)
            self.assertEqual(browser.closed, 0)
            self.assertEqual(browser.budgets, [])
            request(broker)
            self.assertTrue(worker.run_once())
            self.assertEqual(browser.closed, 1)
            self.assertIsNone(broker.get_warmup())
            worker.run_once()
            self.assertEqual(browser.prepared, 1)

    def test_unused_warmup_expires_and_new_event_can_prepare_again(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            browser = WarmingBrowser(clock)
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            broker.request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1000)
            worker.run_once()
            clock.sleep(100)
            worker.run_once()
            self.assertEqual(browser.closed, 1)
            broker.request_warmup(event_fingerprint='a' * 64, origin=ORIGIN, notice_epoch=clock())
            worker.run_once()
            self.assertEqual(browser.prepared, 2)

    def test_warmup_failures_are_bounded_and_do_not_block_real_requests(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            browser = WarmingBrowser(clock, warmup_error=True)
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            broker.request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1000)
            for _ in range(5):
                worker.run_once()
                clock.sleep(6)
            self.assertEqual(browser.prepared, 2)
            row = request(broker)
            worker.run_once()
            self.assertEqual(broker.get_request(row['request_id'])['status'], 'submitted')

    def test_restarting_the_verifier_does_not_reset_prewarm_attempts(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            broker.request_warmup(event_fingerprint='f' * 64, origin=ORIGIN, notice_epoch=1000)
            browser = WarmingBrowser(clock, warmup_error=True)
            for _ in range(4):
                AutomaticTurnstileWorker(broker, browser, clock=clock).run_once()
            self.assertEqual(browser.prepared, 2)
            self.assertEqual(broker.get_warmup()['attempts'], 2)

    def test_monitor_requests_warmup_before_waiting_for_telegram_authentication(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                broker = WorldBossTurnstileBroker(directory)
                actor = FakeActor()
                actor.state_file = str(Path(directory) / 'state.json')
                monitor = WorldBossMonitor(actor, 'main', turnstile_broker=broker)
                entry = replace(extract_world_boss_entry(DummyMessage()), notice_epoch=time.time())

                async def authenticate(*args):
                    self.assertEqual(broker.get_warmup()['event_fingerprint'], entry.fingerprint)
                    self.assertEqual(broker.list_requests(), [])
                    raise MiniAppBeastError('fixture_auth_stopped')

                with patch('world_boss_features.request_webview_init_data', side_effect=authenticate), \
                        patch('world_boss_features.require_enabled'):
                    with self.assertRaisesRegex(MiniAppBeastError, 'fixture_auth_stopped'):
                        await monitor._participate(entry)
                await monitor.stop()
        asyncio.run(run())


class SharedQueueBudgetTests(unittest.TestCase):
    def test_busy_browser_is_reused_for_the_next_account(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            for account in ('main', 'sub'):
                request(broker, account)
            browser = WarmingBrowser(clock)
            browser.verify = Mock(side_effect=BrowserVerificationError('browser_protocol_timeout'))
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            worker.run_once()
            worker.run_once()
            self.assertEqual(browser.verify.call_count, 2)
            self.assertEqual(browser.closed, 0)

    def test_slow_first_account_leaves_three_first_attempts_before_retry(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            rows = [request(broker, account) for account in ('main', 'sub', 'xiaohao', 'waaiging')]
            browser = WarmingBrowser(clock, fail_first=True)
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            for _ in rows:
                worker.run_once()
            self.assertEqual(browser.budgets, [20, 20, 20, 20])
            self.assertEqual(clock() - 1000, 50)
            states = [broker.get_request(row['request_id']) for row in rows]
            self.assertEqual([row['browser_attempts'][0]['attempt'] for row in states], [1] * 4)
            self.assertCountEqual([row['status'] for row in states], ['pending', 'submitted', 'submitted', 'submitted'])
            failed = next(row for row in states if row['status'] == 'pending')['browser_attempts'][0]
            self.assertEqual(failed['error'], 'turnstile_browser_timeout')
            self.assertEqual(failed['duration_ms'], 20000)
            self.assertEqual(failed['budget_ms'], 20000)
            worker.run_once()
            self.assertEqual(browser.budgets[-1], 35)
            self.assertNotIn('one-shot-fixture', json.dumps(broker.list_requests()))

    def test_request_expiry_limits_a_retry_budget(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock, request_ttl_seconds=30)
            row = request(broker)
            browser = WarmingBrowser(clock, fail_first=True)
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            worker.run_once()
            clock.sleep(6)
            worker.run_once()
            self.assertEqual(browser.budgets, [20, 4])
            self.assertFalse(list(Path(directory).glob('token_*')))
            self.assertEqual(broker.get_request(row['request_id'])['status'], 'expired')

    def test_closed_battle_keeps_browser_failure_details(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                class FailedBroker(WorldBossTurnstileBroker):
                    def create_request(self, **kwargs):
                        row = super().create_request(**kwargs)
                        self.record_browser_event(row['request_id'], 'browser_starting', source='automatic')
                        self.record_browser_attempt(row['request_id'], attempt=1, duration_ms=20000,
                                                    budget_ms=20000, error='browser_connection_timeout')
                        return row

                broker = FailedBroker(directory)
                monitor = WorldBossMonitor(FakeActor(), 'main', turnstile_broker=broker)
                with patch.object(monitor, '_boss_stop_requested', return_value=True):
                    with self.assertRaises(MiniAppBeastError) as caught:
                        await monitor._wait_for_turnstile_token(extract_world_boss_entry(DummyMessage()), '主魂', 'challenge')
                details = caught.exception.details
                self.assertTrue(details['turnstile_request_id'])
                self.assertEqual(details['browser']['browser_attempts'][0]['error'], 'browser_connection_timeout')
                self.assertEqual(broker.get_request(details['turnstile_request_id'])['status'], 'cancelled')
                await monitor.stop()
        asyncio.run(run())

    def test_late_diagnostic_cannot_overwrite_cancelled_status_or_expose_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            row = request(broker)
            broker.cancel(row['request_id'], reason='boss_event_closed')
            for _ in range(5):
                broker.record_browser_attempt(row['request_id'], attempt=2, duration_ms=50,
                                            budget_ms=20000, error='private-token-fixture', cf_code='private-token')
            saved = broker.get_request(row['request_id'])
            self.assertEqual(saved['status'], 'cancelled')
            self.assertEqual(len(saved['browser_attempts']), 3)
            self.assertNotIn('private-token', json.dumps(saved))

    def test_closed_verification_survives_full_failure_result_sanitization(self):
        async def run():
            stopped, calls = [False], []
            with tempfile.TemporaryDirectory() as directory:
                class FailedBroker(WorldBossTurnstileBroker):
                    def create_request(self, **kwargs):
                        row = super().create_request(**kwargs)
                        self.record_browser_attempt(row['request_id'], attempt=1, duration_ms=20000,
                                                    budget_ms=20000, error='browser_connection_timeout')
                        stopped[0] = True
                        return row

                async def post(origin, path, payload, timeout):
                    calls.append(path)
                    raise MiniAppBeastError('turnstile_required', 403)

                actor = FakeActor()
                actor.state_file = str(Path(directory) / 'state.json')
                monitor = WorldBossMonitor(actor, 'main', post_json=post, turnstile_broker=FailedBroker(directory))
                entry = extract_world_boss_entry(DummyMessage())

                async def participate(*args, **kwargs):
                    return await monitor._fight(entry, 'private-init-fixture', 'private-session-fixture',
                                                {'challenge': {'challengeId': 'fixture'}})

                with patch.object(monitor, '_participate', side_effect=participate), \
                        patch.object(monitor, '_boss_stop_requested', side_effect=lambda: stopped[0]):
                    outcome = await monitor._run_identity(entry, '主魂', 'private-init-fixture')
                self.assertEqual(outcome['status'], 'event_closed')
                browser = outcome['diagnostics']['failure']['browser']
                self.assertEqual(browser['browser_attempts'][0]['error'], 'browser_connection_timeout')
                self.assertEqual(calls, ['/api/miniapp/xianxia-world-boss/begin'])
                self.assertNotIn('private-init-fixture', json.dumps(outcome))
                self.assertNotIn('private-session-fixture', json.dumps(outcome))
                await monitor.stop()
        asyncio.run(run())


class NativeBrowserDeadlineTests(unittest.TestCase):
    def test_installed_native_binary_is_preferred_to_snap_and_its_shell_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / '.cache/ms-playwright/chromium-1217/chrome-linux64/chrome'
            native.parent.mkdir(parents=True)
            native.write_bytes(b'fixture executable')
            wrapper = root / 'chromium-browser'
            wrapper.write_bytes(b'#!/bin/sh\nexec /snap/bin/chromium "$@"\n')
            paths = {'chromium': '/snap/bin/chromium', 'chromium-browser': str(wrapper)}
            with patch('world_boss_browser.shutil.which', side_effect=paths.get), \
                    patch('world_boss_browser.Path.home', return_value=root), \
                    patch('world_boss_browser.Path.is_file', lambda path: str(path) in {str(native), str(wrapper)}):
                self.assertEqual(find_chrome(), str(native.resolve()))
                self.assertEqual(find_chrome(str(wrapper)), str(wrapper.resolve()))

    def test_native_system_browser_still_takes_priority_over_cached_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / 'google-chrome'
            native.write_bytes(b'fixture executable')
            with patch('world_boss_browser.shutil.which', side_effect=lambda name: str(native) if name == 'google-chrome' else None):
                self.assertEqual(find_chrome(), str(native))

    def test_cancelled_request_never_launches_a_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            with patch('world_boss_browser.subprocess.Popen') as launch:
                with self.assertRaisesRegex(BrowserVerificationError, 'verification_request_finished'):
                    browser.start(still_pending=lambda: False)
                launch.assert_not_called()

    def test_cold_start_http_polling_respects_whole_attempt_budget(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            process = Mock()
            process.poll.return_value = None

            def missing(url, *, timeout):
                clock.sleep(timeout)
                raise urllib.error.URLError('fixture unavailable')

            with patch('world_boss_browser.time.monotonic', clock), \
                    patch('world_boss_browser.time.sleep', clock.sleep), \
                    patch('world_boss_browser.find_chrome', return_value='fixture-chrome'), \
                    patch('world_boss_browser.subprocess.Popen', return_value=process), \
                    patch('world_boss_browser.os.environ', {'DISPLAY': ':fixture'}), \
                    patch('world_boss_browser.urllib.request.urlopen', side_effect=missing), \
                    patch.object(browser, 'close') as close:
                with self.assertRaisesRegex(BrowserVerificationError, 'browser_connection_timeout'):
                    browser.start(timeout=2)
                self.assertAlmostEqual(clock() - 1000, 2)
                close.assert_called_once()

    def test_page_load_checks_cancel_before_widget_or_token_creation(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            browser.start = Mock()
            browser.command = Mock()
            browser.evaluate = Mock(return_value=False)
            with patch('world_boss_browser.time.monotonic', clock), patch('world_boss_browser.time.sleep', clock.sleep):
                with self.assertRaisesRegex(BrowserVerificationError, 'verification_request_finished'):
                    browser.prepare(timeout=20, still_pending=lambda: clock() < 1000.5)
            self.assertAlmostEqual(clock() - 1000, .5)
            self.assertTrue(all('Boolean(window.turnstile' in call.args[0] for call in browser.evaluate.call_args_list))

    def test_protocol_receives_use_remaining_budget_and_notice_cancellation(self):
        for cancel in (False, True):
            clock = Clock()
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as directory:
                browser = NativeTurnstileBrowser(directory)
                timeout = [None]
                browser.connection = Mock()
                browser.connection.settimeout.side_effect = lambda value: timeout.__setitem__(0, value)

                def receive():
                    clock.sleep(timeout[0])
                    raise WebSocketTimeoutException('fixture timeout')

                browser.connection.recv.side_effect = receive
                with patch('world_boss_browser.time.monotonic', clock):
                    expected = 'verification_request_finished' if cancel else 'browser_protocol_timeout'
                    with self.assertRaisesRegex(BrowserVerificationError, expected):
                        browser.command('Runtime.evaluate', deadline=clock() + 2.5,
                                        still_pending=(lambda: clock() < 1000.9) if cancel else None)
                self.assertAlmostEqual(clock() - 1000, 1 if cancel else 2.5)

    def test_prepare_and_verify_do_not_each_get_a_fresh_timeout(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            browser.prepare = Mock(side_effect=lambda *args, **kwargs: clock.sleep(.4))
            timeout = [None]
            browser.connection = Mock()
            browser.connection.settimeout.side_effect = lambda value: timeout.__setitem__(0, value)

            def receive():
                clock.sleep(timeout[0])
                raise WebSocketTimeoutException('fixture timeout')

            browser.connection.recv.side_effect = receive
            with patch('world_boss_browser.time.monotonic', clock):
                with self.assertRaisesRegex(BrowserVerificationError, 'browser_protocol_timeout'):
                    browser.verify(timeout=.5)
            self.assertAlmostEqual(clock() - 1000, .5)

    def test_cleanup_terminates_owned_group_after_launcher_has_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            browser.process = Mock(pid=123456)
            browser.process.poll.return_value = 0
            with patch('world_boss_browser.os.name', 'posix'), \
                    patch('world_boss_browser.os.killpg', create=True) as kill_group:
                browser.close()
            kill_group.assert_called_once_with(123456, signal.SIGTERM)
            self.assertIsNone(browser.process)


if __name__ == '__main__':
    unittest.main()
