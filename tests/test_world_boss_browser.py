import tempfile
import unittest
from pathlib import Path

from world_boss_browser import (
    AutomaticTurnstileWorker, BrowserVerificationError, NativeTurnstileBrowser, worker_lease,
)
from world_boss_turnstile import WorldBossTurnstileBroker, TurnstileRequestError


class FakeBrowser:
    def __init__(self, *, fail=False, before_return=None):
        self.calls = 0
        self.closed = 0
        self.fail = fail
        self.before_return = before_return

    def verify(self, origin, *, timeout=55, on_event, still_pending):
        self.calls += 1
        on_event('widget_ready')
        if self.fail:
            on_event('widget_error', '600010')
            raise BrowserVerificationError('turnstile_browser_error', '600010')
        if self.before_return:
            self.before_return()
        return 'private-browser-fixture-' + str(self.calls)

    def close(self):
        self.closed += 1


class AutomaticBrowserTests(unittest.TestCase):
    def create(self, broker, account='main'):
        return broker.create_request(
            event_fingerprint='f' * 64, message_id=321, account=account,
            identity='主魂', challenge_id='challenge', origin='https://asc.aiopenai.app',
        )

    def test_four_accounts_receive_different_one_shot_tokens_without_manual_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            requests = [self.create(broker, account) for account in ('main', 'sub', 'xiaohao', 'waaiging')]
            browser = FakeBrowser()
            worker = AutomaticTurnstileWorker(broker, browser)
            for index, _ in enumerate(requests):
                self.assertTrue(worker.run_once())
                self.assertEqual(browser.closed, int(index == len(requests) - 1))
            self.assertFalse(worker.run_once())
            self.assertEqual(browser.calls, 4)
            self.assertNotIn('private-browser-fixture', str(broker.list_requests()))
            tokens = [broker.take_token(item['request_id']) for item in requests]
            self.assertEqual(len(set(tokens)), 4)
            for item in requests:
                self.assertIsNone(broker.take_token(item['request_id']))
            self.assertFalse(list(Path(directory).glob('token_*.txt')))
            self.assertTrue(all(item['browser_source'] == 'automatic' for item in broker.list_requests(include_finished=True)))

    def test_closed_request_discards_fresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            request = self.create(broker)
            browser = FakeBrowser(before_return=lambda: broker.cancel(request['request_id'], reason='boss_event_closed'))
            AutomaticTurnstileWorker(broker, browser).run_once()
            self.assertEqual(broker.list_requests(), [])
            self.assertFalse(list(Path(directory).glob('token_*.txt')))

    def test_cf_failure_retries_are_bounded_and_keep_error_code(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            self.create(broker)
            browser = FakeBrowser(fail=True)
            worker = AutomaticTurnstileWorker(broker, browser, clock=lambda: now[0])
            self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
            now[0] += 6
            self.assertTrue(worker.run_once())
            now[0] += 6
            self.assertFalse(worker.run_once())
            self.assertEqual(browser.calls, 2)
            self.assertEqual(broker.list_requests()[0]['browser_error_code'], '600010')
            self.assertFalse(list(Path(directory).glob('token_*.txt')))

    def test_other_accounts_get_a_first_attempt_before_retry(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            requests = [self.create(broker, name) for name in ('main', 'sub')]
            browser = FakeBrowser(fail=True)
            worker = AutomaticTurnstileWorker(broker, browser, clock=lambda: now[0])
            worker.run_once()
            now[0] += 6
            worker.run_once()
            self.assertEqual([worker.attempts.get(item['request_id']) for item in requests], [1, 1])

    def test_idle_browser_is_closed(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            browser = FakeBrowser()
            worker = AutomaticTurnstileWorker(WorldBossTurnstileBroker(directory), browser, clock=lambda: now[0])
            worker.run_once()
            self.assertEqual(browser.closed, 0)
            now[0] += 21
            worker.run_once()
            self.assertEqual(browser.closed, 1)

    def test_only_one_browser_worker_can_own_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            with worker_lease(directory):
                with self.assertRaisesRegex(BrowserVerificationError, 'already_running'):
                    with worker_lease(directory):
                        self.fail('second worker acquired the browser lease')

    def test_origin_is_checked_before_browser_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            with self.assertRaisesRegex(BrowserVerificationError, 'origin_not_allowed'):
                browser.verify('https://unrelated.example')
            self.assertIsNone(browser.process)

    def test_arbitrary_browser_error_text_cannot_enter_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            request = self.create(broker)
            with self.assertRaises(TurnstileRequestError):
                broker.record_browser_event(request['request_id'], 'widget_error', 'private-token')
            self.assertNotIn('private-token', str(broker.get_request(request['request_id'])))


if __name__ == '__main__':
    unittest.main()
