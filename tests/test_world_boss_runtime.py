"""Regressions for real local stalls and incomplete settlements on 2026-09-12."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from miniapp_beast import MiniAppBeastError
from tests.test_world_boss_features import DummyMessage, FakeActor
from tests.test_world_boss_reveal_protocol import RevealClock, build_monitor
from world_boss_features import WorldBossMonitor, extract_world_boss_entry
from world_boss_runtime import run_isolated_battle


class WorldBossRuntimeTests(unittest.TestCase):
    def test_production_dispatch_uses_the_isolated_clock(self):
        async def run():
            owner = threading.get_ident()
            monitor = WorldBossMonitor(FakeActor(), 'main')
            async def fight():
                return {'thread': threading.get_ident()}
            with patch.object(monitor, '_fight', side_effect=fight):
                result = await monitor._dispatch_fight()
            self.assertNotEqual(result['thread'], owner)
            await monitor.stop()
        asyncio.run(run())

    def test_local_stall_keeps_cursor_and_fetches_following_window(self):
        async def run():
            clock, cursors = RevealClock(), []

            async def post(origin, path, payload, timeout):
                cursors.append(payload['afterWindowId'])
                index = len(cursors)
                return {'window': {'id': f'w{index}', 'centerMs': index * 14000,
                                   'hitMs': 620, 'perfectMs': 210},
                        'windowCount': 16, 'done': index == 2}

            monitor = build_monitor(clock, post)
            async def stalled_sleep(seconds):
                clock.now += 21 if len(cursors) == 1 else seconds
            monitor.sleep = stalled_sleep
            queue, log = asyncio.Queue(), []
            await monitor._reveal_windows(extract_world_boss_entry(DummyMessage()),
                                          'init', 'session', 'challenge', 100, queue, log, 16)
            self.assertEqual(cursors, ['', 'w1'])
            self.assertEqual([row['window_id'] for row in log if row['status'] == 'revealed'], ['w1', 'w2'])
            self.assertTrue(any(row['status'] == 'stalled' for row in log))
            self.assertEqual(log[-1]['reason'], 'server_done')
            await monitor.stop()
        asyncio.run(run())

    def test_missing_windows_still_have_a_server_duration_deadline(self):
        async def run():
            clock = RevealClock()
            async def post(*args):
                raise MiniAppBeastError('boss_window_not_ready', 409)
            monitor = build_monitor(clock, post)
            log = []
            await monitor._reveal_windows(extract_world_boss_entry(DummyMessage()),
                                          'init', 'session', 'challenge', 100, asyncio.Queue(), log, 16,
                                          max_duration_ms=8000)
            self.assertEqual(log[-1]['reason'], 'battle_deadline')
            self.assertGreaterEqual(clock.now, 107)
            self.assertLess(clock.now, 108)
            await monitor.stop()
        asyncio.run(run())

    def test_exhausted_read_errors_do_not_turn_into_a_partial_finish(self):
        async def run():
            clock = RevealClock()
            async def post(*args):
                raise MiniAppBeastError('bad_response', 502)
            monitor = build_monitor(clock, post)
            log = []
            with self.assertRaises(MiniAppBeastError) as caught:
                await monitor._reveal_windows(extract_world_boss_entry(DummyMessage()),
                                              'init', 'session', 'challenge', 100, asyncio.Queue(), log, 16)
            self.assertEqual(caught.exception.code, 'boss_window_fetch_failed')
            self.assertEqual(log[-1]['reason'], 'error_budget_exhausted')
            await monitor.stop()
        asyncio.run(run())

    def test_checkpoint_latency_is_absorbed_before_charge_starts(self):
        async def run():
            clock, state = RevealClock(), {}
            monitor = None
            async def post(origin, path, payload, timeout):
                self.assertTrue(state.get('saved'), 'No action before its durable reservation')
                if path.endswith('/charge-start'):
                    state['charge_at'] = clock.now
                    state['ticket_at'] = clock.now + .1
                    clock.now += .2
                    return {'chargeTicket': 'fixture-ticket'}
                self.assertTrue(path.endswith('/hit'))
                arrived = clock.now + .1
                hold = (arrived - state['ticket_at']) * 1000
                delta = abs((arrived - 100) * 1000 - 2000)
                clock.now += .2
                return {'hit': {'perfect': 520 <= hold <= 1250 and delta <= 210,
                                'holdMs': hold, 'deltaMs': delta, 'damageYi': 1}}
            monitor = build_monitor(clock, post)
            monitor._checkpoint = {'claimed_window_ids': []}
            async def save():
                self.assertEqual(monitor._checkpoint['claimed_window_ids'], ['w1'])
                clock.now += .6
                state['saved'] = True
            monitor._save_checkpoint = save
            hit = await monitor._hit_window(extract_world_boss_entry(DummyMessage()),
                                             'init', 'session', 'challenge', 100,
                                             {'id': 'w1', 'centerMs': 2000, 'hitMs': 620, 'perfectMs': 210},
                                             1, 100)
            self.assertTrue(hit['accepted_perfect'])
            self.assertAlmostEqual(state['charge_at'], 100.88, places=2)
            self.assertEqual(hit['diagnostic']['charge']['checkpoint_duration_ms'], 600)
            await monitor.stop()
        asyncio.run(run())

    def test_long_checkpoint_stall_does_not_send_an_already_expired_charge(self):
        async def run():
            clock, calls = RevealClock(), []
            async def post(*args):
                calls.append(args)
                raise AssertionError('Expired window must not generate an action')
            monitor = build_monitor(clock, post)
            monitor._checkpoint = {'claimed_window_ids': []}
            async def save():
                clock.now += 25
            monitor._save_checkpoint = save
            hit = await monitor._hit_window(extract_world_boss_entry(DummyMessage()),
                                             'init', 'session', 'challenge', 100,
                                             {'id': 'w1', 'centerMs': 2000, 'hitMs': 620, 'perfectMs': 210}, 1, 100)
            self.assertEqual(calls, [])
            self.assertEqual(hit['error'], 'local_window_missed')
            self.assertIsNone(hit['action'])
            self.assertEqual(hit['diagnostic']['charge']['checkpoint_duration_ms'], 25000)
            await monitor.stop()
        asyncio.run(run())

    def test_owner_loop_blocking_does_not_block_battle_timer(self):
        async def run():
            started, fired = threading.Event(), threading.Event()
            owner = threading.get_ident()
            async def battle():
                started.set()
                await asyncio.sleep(.03)
                fired.set()
                return threading.get_ident()
            task = asyncio.create_task(run_isolated_battle(battle, account='fixture'))
            while not started.is_set():
                await asyncio.sleep(.001)
            time.sleep(.15)  # A synchronous handler blocks the Telegram loop.
            self.assertTrue(fired.is_set())
            self.assertNotEqual(await task, owner)
        asyncio.run(run())

    def test_cancel_waits_for_battle_cleanup_even_after_second_cancel(self):
        async def run():
            started, cleaning, release, cleaned = (threading.Event() for _ in range(4))
            async def battle():
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await asyncio.to_thread(release.wait, 2)
                    cleaned.set()
            task = asyncio.create_task(run_isolated_battle(battle, account='fixture'))
            try:
                while not started.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                while not cleaning.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cleaned.is_set())
        asyncio.run(run())

    def fixture(self, duration=56053):
        clock = RevealClock()
        monitor = build_monitor(clock, AsyncMock())
        entry = extract_world_boss_entry(DummyMessage())
        epoch = time.time() - duration / 1000
        checkpoint = {
            'version': 1, 'account': 'main', 'identity': '主魂', 'entry': asdict(entry),
            'stage': 'finish_pending', 'expires_epoch': time.time() + 600,
            'battle_start_epoch': epoch, 'init_data': 'old-init', 'session_token': 'original-session',
            'payload': {'challenge': {'challengeId': 'original-challenge', 'minDurationMs': 75000,
                                      'maxDurationMs': 312000}},
            'proof': {'durationMs': duration, 'actions': [{'t': 2000, 'holdMs': 1000}]},
            'outcome': {'diagnostics': {}, 'hit_count': 2, 'perfect_count': 1, 'player_hp': 100},
        }
        monitor._checkpoint = checkpoint
        monitor._finish_clock = (epoch, clock.now - duration / 1000)
        monitor.recovery_store.delete = Mock()
        return clock, monitor, entry, checkpoint

    def test_short_finish_waits_real_time_and_preserves_actual_actions(self):
        async def run():
            clock, monitor, entry, checkpoint = self.fixture()
            actions, sent = deepcopy(checkpoint['proof']['actions']), []
            async def post(origin, path, payload, timeout):
                self.assertTrue(path.endswith('/finish'))
                sent.append(deepcopy(payload['bossProof']))
                return {'result': {'score': 100}}
            monitor.post_json = post
            result = await monitor._submit_finish(entry, checkpoint)
            self.assertEqual(len(sent), 1)
            elapsed = int((clock.now - monitor._finish_clock[1]) * 1000)
            self.assertEqual(sent[0]['durationMs'], elapsed)
            self.assertGreaterEqual(elapsed, 75000)
            self.assertLess(elapsed, 75200)
            self.assertEqual(sent[0]['actions'], actions)
            self.assertEqual(result['hit_count'], 2)
            await monitor.stop()
        asyncio.run(run())

    def test_rejected_duration_is_recomputed_before_retry_and_retries_are_bounded(self):
        async def run():
            clock, monitor, entry, checkpoint = self.fixture(80000)
            sent = []
            async def post(origin, path, payload, timeout):
                sent.append(payload['bossProof']['durationMs'])
                raise MiniAppBeastError('boss_duration_too_short', 400)
            monitor.post_json = post
            with self.assertRaises(MiniAppBeastError) as first:
                await monitor._submit_finish(entry, checkpoint)
            self.assertTrue(first.exception.world_boss_retryable)
            clock.now += 30
            with self.assertRaises(MiniAppBeastError) as second:
                await monitor._submit_finish(entry, checkpoint)
            self.assertFalse(second.exception.world_boss_retryable)
            self.assertEqual(sent, [80000, 110000])
            self.assertIsNone(monitor._checkpoint)
            monitor.recovery_store.delete.assert_called_once_with(entry.fingerprint)
            await monitor.stop()
        asyncio.run(run())

    def test_expired_auth_refreshes_only_auth_and_reuses_original_finish(self):
        async def run():
            clock, monitor, entry, checkpoint = self.fixture(80000)
            original_proof, calls = deepcopy(checkpoint['proof']), []
            async def post(origin, path, payload, timeout):
                calls.append((path, deepcopy(payload)))
                self.assertTrue(path.endswith('/finish'))
                if payload['initData'] == 'old-init':
                    raise MiniAppBeastError('auth_date_expired', 401)
                return {'result': {'score': 100}}
            monitor.post_json = post
            with self.assertRaises(MiniAppBeastError):
                await monitor._submit_finish(entry, checkpoint)
            self.assertTrue(checkpoint['refresh_init_data'])
            with patch.object(monitor.recovery_store, 'load', return_value=checkpoint), patch(
                'world_boss_features.request_webview_init_data', new=AsyncMock(return_value='fresh-init'),
            ) as auth:
                result = await monitor._participate(entry)
            auth.assert_awaited_once_with(monitor.client, entry.bot_username, entry.token)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1]['bossProof'], original_proof)
            self.assertEqual(calls[1][1]['token'], 'original-session')
            self.assertEqual(result['score'], 100)
            self.assertNotIn('fresh-init', json.dumps(result))
            await monitor.stop()
        asyncio.run(run())

    def test_expired_finish_cannot_pad_duration_or_send_an_action(self):
        async def run():
            _, monitor, entry, checkpoint = self.fixture()
            checkpoint['expires_epoch'] = time.time() + 1
            with self.assertRaises(MiniAppBeastError) as caught:
                await monitor._submit_finish(entry, checkpoint)
            self.assertEqual(caught.exception.code, 'boss_finish_expired')
            self.assertFalse(caught.exception.world_boss_retryable)
            self.assertEqual(checkpoint['proof']['durationMs'], 56053)
            monitor.post_json.assert_not_awaited()
            await monitor.stop()
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
