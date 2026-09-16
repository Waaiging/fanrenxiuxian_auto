"""Offline regressions for the September 16 round; no real game requests."""
import asyncio
import unittest

from miniapp_beast import MiniAppBeastError
from tests.test_world_boss_features import DummyMessage
from tests.test_world_boss_reveal_protocol import RevealClock, build_monitor
from world_boss_features import _diagnostic_value, extract_world_boss_entry


class WorldBossSeptember16Tests(unittest.TestCase):
    def test_late_reveal_preserves_real_hold_when_only_extra_margin_does_not_fit(self):
        async def run(outbound_ms):
            # Main #3: reveal at 14612 ms, checkpoint 5 ms, charge reply 189 ms.
            # deltaMs is unsigned, so replay both arrival directions consistent
            # with the saved reply. The server computes hold and arrival itself.
            clock = RevealClock(114.612)
            calls, observed = [], {}
            issued_ms = 14977 + outbound_ms - 363.6

            async def sleep(seconds):
                clock.now += max(0, seconds) + (0.001 if seconds > 0 else 0)

            async def save():
                clock.now += 0.005

            async def request(origin, path, payload, timeout):
                calls.append(path.rsplit('/', 1)[-1])
                if path.endswith('/charge-start'):
                    observed['pressed'] = clock.now
                    clock.now += 0.189
                    return {'chargeTicket': 'fixture-ticket'}
                self.assertTrue(path.endswith('/hit'))
                observed['released'] = clock.now
                observed['reported_hold'] = payload['holdMs']
                arrived_ms = (clock.now - 100) * 1000 + outbound_ms
                hold_ms = arrived_ms - issued_ms
                delta_ms = abs(arrived_ms - 15107)
                clock.now += 0.190
                return {'hit': {'damageYi': 1, 'deltaMs': delta_ms, 'holdMs': hold_ms,
                                'perfect': delta_ms <= 210 and 520 <= hold_ms <= 1250}}

            monitor = build_monitor(clock, request)
            monitor.sleep = sleep
            monitor._save_checkpoint = save
            monitor._checkpoint = {'claimed_window_ids': []}
            monitor._drift_ms, monitor._drift_samples = 15.18, 1
            try:
                result = await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), 'init-fixture', 'session-fixture',
                    'challenge-fixture', 100,
                    {'id': 'w3-fixture', 'centerMs': 15107, 'hitMs': 620, 'perfectMs': 210},
                    3, 96,
                )
                self.assertTrue(result['accepted_perfect'], result['diagnostic'])
                self.assertEqual(calls, ['charge-start', 'hit'])
                self.assertEqual(observed['reported_hold'],
                                 round((observed['released'] - observed['pressed']) * 1000))
                self.assertGreaterEqual(result['diagnostic']['server_hold_ms'], 520)
                self.assertLessEqual(result['diagnostic']['server_hit']['deltaMs'], 210)
                charge = result['diagnostic']['charge']
                self.assertTrue(charge['hold_margin_reduced'])
                self.assertGreaterEqual(result['diagnostic']['target_ms'], charge['required_hold_target_ms'])
                self.assertLessEqual(result['diagnostic']['target_ms'], charge['perfect_deadline_ms'])
            finally:
                await monitor.stop()

        for outbound in (114.4, 145.6):
            with self.subTest(outbound_ms=outbound):
                asyncio.run(run(outbound))

    def test_unreachable_minimum_does_not_delay_or_fabricate_a_hold(self):
        async def run():
            clock = RevealClock(114.617)
            observed = {}

            async def request(origin, path, payload, timeout):
                if path.endswith('/charge-start'):
                    observed['pressed'] = clock.now
                    clock.now += 0.250  # Too late to fit even the required hold.
                    return {'chargeTicket': 'fixture-ticket'}
                self.assertTrue(path.endswith('/hit'))
                observed['released'], observed['payload'] = clock.now, payload
                return {'hit': {'damageYi': 1, 'perfect': False, 'holdMs': 310, 'deltaMs': 15}}

            monitor = build_monitor(clock, request)
            monitor._drift_ms, monitor._drift_samples = 15.18, 1
            try:
                result = await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), 'init-fixture', 'session-fixture',
                    'challenge-fixture', 100,
                    {'id': 'w3-fixture', 'centerMs': 15107, 'hitMs': 620, 'perfectMs': 210},
                    3, 96,
                )
                diag = result['diagnostic']
                self.assertEqual(diag['target_ms'], diag['ideal_target_ms'])
                self.assertFalse(result['accepted_perfect'])
                self.assertLess(observed['payload']['holdMs'], 520)
                self.assertEqual(observed['payload']['holdMs'],
                                 round((observed['released'] - observed['pressed']) * 1000))
            finally:
                await monitor.stop()

        asyncio.run(run())

    def test_poll_diagnostics_distinguish_request_time_from_delayed_wakeup(self):
        async def run():
            clock, calls = RevealClock(), []
            sleeps = 0

            async def sleep(seconds):
                nonlocal sleeps
                sleeps += 1
                clock.now += seconds + (4.984 if sleeps == 1 else 0)

            async def request(origin, path, payload, **kwargs):
                calls.append((clock.now, payload['afterWindowId']))
                duration = 3.144 if len(calls) == 1 else 0.200
                clock.now += duration
                kwargs['trace'].update(attempts=[{
                    'network_duration_ms': 300 if len(calls) == 1 else 190,
                    'queue_delay_ms': 569 if len(calls) == 1 else 1,
                    'bookkeeping_ms': 2275 if len(calls) == 1 else 9,
                }], total_duration_ms=round(duration * 1000))
                if len(calls) < 3:
                    raise MiniAppBeastError('boss_window_not_ready', 409)
                return {'window': {'id': 'w1', 'centerMs': 12000, 'hitMs': 620, 'perfectMs': 210},
                        'done': True}

            monitor = build_monitor(clock, None)
            monitor.sleep, monitor._request = sleep, request
            queue, log = asyncio.Queue(), []
            try:
                await monitor._reveal_windows(
                    extract_world_boss_entry(DummyMessage()), 'init-fixture', 'session-fixture',
                    'challenge-fixture', 100, queue, log, 1,
                )
                self.assertEqual([cursor for _, cursor in calls], ['', '', ''])
                self.assertAlmostEqual(calls[2][0] - calls[1][0], .360, places=6)
                polling = _diagnostic_value(log)[0]['polling']
                self.assertEqual(polling['max_poll_gap_ms'], 8178)
                self.assertEqual(polling['max_gap_request_ms'], 3144)
                self.assertEqual(polling['max_gap_sleep_ms'], 5034)
                self.assertEqual(polling['max_gap_sleep_lateness_ms'], 4984)
                self.assertEqual(polling['max_gap_processing_ms'], 0)
                self.assertEqual(polling['max_sleep_lateness_ms'], 4984)
                self.assertEqual(polling['max_request_network_ms'], 300)
                self.assertEqual(polling['slow_poll_count'], 1)
                self.assertEqual((await queue.get())['id'], 'w1')
                self.assertIsNone(await queue.get())
            finally:
                await monitor.stop()

        asyncio.run(run())
