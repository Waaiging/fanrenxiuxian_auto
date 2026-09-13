"""Clock-offset regressions from the 2026-09-13 round; no live game requests."""

import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from miniapp_beast import MiniAppBeastError
from tests.test_world_boss_features import DummyMessage, FakeActor
from tests.test_world_boss_reveal_protocol import RevealClock, build_monitor
from world_boss_features import WorldBossMonitor, extract_world_boss_entry
from world_boss_timing import BattleClockProbe


class BattleClockProbeTests(unittest.TestCase):
    def observation(self, probe, window, delta=270, sent=-15, **changes):
        values = dict(delta_ms=delta, sent_offset_ms=sent, rtt_ms=190,
                      wake_lateness_ms=1, perfect_ms=210, hit_ms=620,
                      strict_direction=False)
        values.update(changes)
        return probe.observe(window, **values)

    def armed_probe(self):
        probe = BattleClockProbe()
        self.observation(probe, 'baseline-1')
        self.observation(probe, 'baseline-2')
        self.assertEqual(probe.status, 'armed')
        return probe

    def test_both_signs_need_two_shifted_confirmations(self):
        for direction, shifted_delta, expected in (('early', 190, -255), ('late', 350, 285)):
            with self.subTest(direction=direction):
                probe = self.armed_probe()
                self.assertEqual(probe.plan('shift-1', 620), 80)
                first = self.observation(probe, 'shift-1', delta=shifted_delta, sent=65)
                self.assertEqual(first['status'], 'confirming')
                self.assertIsNone(probe.offset_ms)
                self.assertEqual(probe.plan('shift-2', 620), 80)
                second = self.observation(probe, 'shift-2', delta=shifted_delta, sent=65)
                self.assertEqual(second['direction'], direction)
                self.assertEqual(probe.offset_ms, expected)
                self.assertEqual(probe.plan('later', 620), 0)

    def test_normal_perfects_or_known_directions_never_arm(self):
        for changes in ({'delta_ms': 30}, {'strict_direction': True}):
            probe = BattleClockProbe()
            for i in range(16):
                self.observation(probe, str(i), **changes)
                self.assertEqual(probe.plan('next-' + str(i), 620), 0)
            self.assertIsNone(probe.offset_ms)

    def test_probe_preserves_hit_margin_and_has_two_window_limit(self):
        probe = self.armed_probe()
        self.assertEqual(probe.plan('narrow', 400), 0)
        self.assertEqual(probe.plan('shift-1', 620), 80)
        self.assertEqual(probe.plan('shift-1', 620), 80)
        self.assertEqual(probe.plan('shift-2', 620), 80)
        self.assertEqual(probe.plan('shift-3', 620), 0)

    def test_spikes_missing_data_and_unstable_shift_do_not_change_clock(self):
        changes = ({'rtt_ms': 900}, {'delta_ms': float('nan')}, {'delta_ms': True},
                   {'wake_lateness_ms': 876}, {'sent_offset_ms': 0},
                   {'delta_ms': 270}, {'rtt_ms': 260})
        for change in changes:
            with self.subTest(change=change):
                probe = self.armed_probe()
                probe.plan('shift', 620)
                values = dict(delta_ms=350, sent_offset_ms=65)
                values.update(change)
                result = self.observation(probe, 'shift', **values)
                self.assertEqual(result['status'], 'rejected')
                self.assertIsNone(probe.offset_ms)
                self.assertEqual(probe.plan('retry', 620), 0)

    def test_conflicting_or_duplicate_confirmations_are_not_accepted(self):
        probe = self.armed_probe()
        probe.plan('first', 620)
        self.observation(probe, 'first', delta=350, sent=65)
        self.assertIsNone(self.observation(probe, 'first', delta=350, sent=65))
        self.assertIsNone(probe.offset_ms)
        probe.plan('second', 620)
        self.assertEqual(self.observation(probe, 'second', delta=190, sent=65)['status'], 'rejected')
        self.assertIsNone(probe.offset_ms)

    def test_stale_or_noisy_baselines_cannot_arm(self):
        probe = BattleClockProbe()
        for i, delta in enumerate((230, 380, 240, 360)):
            self.observation(probe, str(i), delta=delta)
            self.assertEqual(probe.plan('next-' + str(i), 620), 0)


class WorldBossClockAlignmentTests(unittest.TestCase):
    def test_sub_round_model_resolves_either_possible_clock_direction(self):
        # Captured (center, reveal, sent, charge-sent, charge RTT, hit RTT,
        # unsigned server delta, server hold). The server did not report a sign.
        # Both possible clock hypotheses are replayed; neither is claimed as
        # the observed production direction. Modeled arrivals ignore elapsedMs.
        rows = (
            (1818, 0, 1730, 750, 980, 187, 279.8, 190.5),
            (8303, 6341, 8200, 6986, 486, 190, 268.2, 946.1),
            (13564, 11769, 13457, 12246, 300, 183, 257.1, 1174.1),
            (21114, 19097, 21007, 19796, 190, 182, 257.0, 1208.0),
            (30059, 28090, 29951, 28741, 191, 189, 261.5, 1212.7),
            (36975, 35193, 36867, 35690, 179, 179, 254.9, 1176.2),
            (44763, 42999, 44655, 43539, 192, 178, 253.7, 1110.8),
            (48179, 46115, 48072, 46994, 193, 179, 255.2, 1071.6),
            (55093, 53201, 54985, 53934, 181, 193, 259.5, 1053.1),
            (59527, 57764, 59419, 58386, 202, 183, 255.9, 1032.9),
            (63082, 61240, 62974, 61952, 181, 222, 295.5, 1061.5),
            (67038, 65241, 66930, 65916, 197, 184, 258.4, 1007.7),
            (73831, 71994, 73722, 72714, 191, 184, 258.2, 1003.8),
            (78083, 76198, 77975, 76968, 185, 191, 263.3, 1010.0),
            (81722, 79754, 81614, 80610, 185, 182, 257.1, 1001.4),
            (86068, 84312, 85961, 84956, 182, 181, 256.6, 1002.6),
        )

        async def replay(sign, enable_probe):
            clock, state, results, calls = RevealClock(), {}, [], []
            clock_offset = sign * 274

            async def post(origin, path, payload, timeout):
                sequence = int(payload['windowId'])
                center, _, sent, charge, charge_rtt, hit_rtt, delta, hold = rows[sequence - 1]
                hit_out = center + sign * delta - clock_offset - sent
                charge_out = sent + hit_out - hold - charge
                self.assertTrue(0 <= hit_out <= hit_rtt)
                self.assertTrue(0 <= charge_out <= charge_rtt)
                calls.append((sequence, path.rsplit('/', 1)[-1]))
                if path.endswith('/charge-start'):
                    state[sequence] = (clock.now + charge_out / 1000, clock.now)
                    clock.now += charge_rtt / 1000
                    return {'chargeTicket': 'fixture-ticket'}
                self.assertTrue(path.endswith('/hit'))
                arrived = clock.now + hit_out / 1000
                actual_delta = abs((arrived - 100) * 1000 + clock_offset - center)
                actual_hold = (arrived - state[sequence][0]) * 1000
                self.assertEqual(payload['holdMs'], round((clock.now - state[sequence][1]) * 1000))
                clock.now += hit_rtt / 1000
                return {'hit': {'perfect': actual_delta <= 210 and 520 <= actual_hold <= 1250,
                                'deltaMs': actual_delta, 'holdMs': actual_hold, 'damageYi': 1}}

            monitor = build_monitor(clock, post, account='sub')
            if not enable_probe:
                monitor._clock_probe.plan = lambda *args: 0
                monitor._clock_probe.observe = lambda *args, **kwargs: None
            for sequence, row in enumerate(rows, 1):
                clock.now = max(clock.now, 100 + row[1] / 1000)
                results.append(await monitor._hit_window(
                    extract_world_boss_entry(DummyMessage()), 'init', 'session', 'challenge', 100,
                    {'id': str(sequence), 'centerMs': row[0], 'hitMs': 620, 'perfectMs': 210}, sequence, 94,
                ))
            self.assertEqual(len(calls), 32, 'Calibration may not add extra game requests')
            self.assertEqual(len(set(calls)), 32, 'Each action is used once')
            summary = monitor._clock_probe.summary()
            self.assertTrue(all(result['ok'] for result in results))
            count = sum(result['accepted_perfect'] for result in results)
            await monitor.stop()
            return count, summary, results

        for sign in (-1, 1):
            with self.subTest(sign=sign):
                before, _, _ = asyncio.run(replay(sign, False))
                after, summary, results = asyncio.run(replay(sign, True))
                self.assertEqual(before, 0)
                self.assertGreaterEqual(after, 11)
                self.assertEqual(summary['status'], 'confirmed')
                self.assertEqual(summary['shifted_window_count'], 2)
                self.assertTrue(all(result['accepted_perfect'] for result in results[-8:]))
                print({'replay': '2026-09-13-sub', 'direction_hypothesis': sign,
                       'before_perfects': before, 'after_perfects': after, 'clock_probe': summary})

    def test_summary_reports_per_hit_reductions_even_when_finish_recovers(self):
        hits = [{'server_status': 'accepted', 'server_hit': {'automationDamageMultiplier': value}}
                for value in (1, .18, .55, .55, 1, None, True, float('nan'))]
        hits.append({'server_status': 'rejected', 'server_hit': {'automationDamageMultiplier': .01}})
        summary = WorldBossMonitor._timing_diagnostic_summary({'diagnostics': {
            'hits': hits, 'finish': {'server_result': {'automation_risk': {'damageMultiplier': 1.0}}},
        }})
        self.assertIn('服务端减伤 3 击，最低倍率 0.18', summary)

    def test_alignment_is_used_for_direction_and_reset_for_next_round(self):
        monitor = WorldBossMonitor(FakeActor(), 'sub')
        monitor._arrival_clock_offset_ms = 274
        # After correcting the schedule, local time is still in the old frame.
        result = monitor._record_drift(16, center_ms=10000, sent_elapsed_ms=9616,
                                      request_completed_elapsed_ms=9806, request_lead_ms=94)
        self.assertEqual(result['direction'], 'ambiguous')
        self.assertFalse(result['update_applied'])
        monitor._reset_drift()
        self.assertEqual(monitor._arrival_clock_offset_ms, 0)
        self.assertEqual(monitor._clock_probe.status, 'idle')

    def test_blocked_marker_read_cannot_block_two_strike_timers(self):
        async def run(directory):
            started, release = threading.Event(), threading.Event()
            threads = {}
            monitor = WorldBossMonitor(FakeActor(), 'main')
            monitor._boss_defeat_marker = Path(directory) / 'marker.json'

            def slow_read(marker):
                threads['reader'] = threading.get_ident()
                started.set()
                release.wait(3)
                return ''

            async def battle():
                threads['clock'] = threading.get_ident()
                while not started.is_set():
                    await asyncio.sleep(.005)
                now = time.monotonic()
                result = await asyncio.wait_for(asyncio.gather(
                    monitor._sleep_until(now + .02), monitor._sleep_until(now + .04),
                ), timeout=.7)
                self.assertFalse(release.is_set())
                return result

            try:
                with patch.object(monitor, '_read_boss_marker', side_effect=slow_read) as reader, \
                        patch.object(monitor, '_fight', side_effect=battle):
                    self.assertEqual(await monitor._dispatch_fight(), [True, True])
                    self.assertEqual(reader.call_count, 1)
                self.assertNotEqual(threads['reader'], threads['clock'])
                self.assertIsNone(monitor._boss_marker_task)
            finally:
                release.set()
                await monitor.stop()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(directory))

    def test_background_marker_wakes_waiters_and_prevents_new_battle(self):
        async def run(directory):
            entry = extract_world_boss_entry(DummyMessage())
            actor = FakeActor()
            actor.state_file = str(Path(directory) / 'state.json')
            winner, waiting = WorldBossMonitor(actor, 'main'), WorldBossMonitor(actor, 'sub')
            winner._prepare_boss_lifecycle(entry)
            waiting._prepare_boss_lifecycle(entry)

            async def battle():
                asyncio.get_running_loop().call_later(.03, winner._mark_boss_defeated)
                return await asyncio.wait_for(waiting._sleep_until(time.monotonic() + 5), 1)

            try:
                with patch.object(waiting, '_fight', side_effect=battle):
                    self.assertFalse(await waiting._dispatch_fight())
                fresh = WorldBossMonitor(actor, 'sub')
                try:
                    with patch.object(fresh, '_request', side_effect=AssertionError('No new battle')) as request:
                        with self.assertRaises(MiniAppBeastError) as caught:
                            await fresh._dispatch_fight(entry, 'init', 'session', {'challenge': {}})
                        self.assertEqual(caught.exception.code, 'boss_event_closed')
                        request.assert_not_called()
                finally:
                    await fresh.stop()
            finally:
                await waiting.stop()
                await winner.stop()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(directory))

    def test_consecutive_battles_can_wait_on_different_isolated_loops(self):
        async def run(directory):
            monitor = WorldBossMonitor(FakeActor(), 'main')
            monitor._boss_defeat_marker = Path(directory) / 'marker.json'
            async def battle():
                return await monitor._sleep_until(time.monotonic() + .03)
            try:
                with patch.object(monitor, '_fight', side_effect=battle):
                    self.assertTrue(await monitor._dispatch_fight())
                    self.assertTrue(await monitor._dispatch_fight())
            finally:
                await monitor.stop()
        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(run(directory))
