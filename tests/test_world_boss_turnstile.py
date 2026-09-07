import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from miniapp_beast import MiniAppBeastError
from world_boss_features import WorldBossMonitor, extract_world_boss_entry
from world_boss_turnstile import WorldBossTurnstileBroker, TurnstileRequestError

from tests.test_world_boss_features import DummyMessage, FakeActor


class _ImmediateTokenBroker(WorldBossTurnstileBroker):
    def __init__(self, directory):
        super().__init__(directory)
        self.created = []

    def create_request(self, **kwargs):
        request = super().create_request(**kwargs)
        self.created.append(request)
        self.submit_token(request["request_id"], "browser-token-fixture")
        return request


class _SequencedTokenBroker(WorldBossTurnstileBroker):
    def __init__(self, directory):
        super().__init__(directory)
        self.tokens = []

    def create_request(self, **kwargs):
        request = super().create_request(**kwargs)
        token = f"browser-token-{len(self.tokens) + 1}"
        self.tokens.append(token)
        self.submit_token(request["request_id"], token)
        return request


class WorldBossTurnstileBrokerTests(unittest.TestCase):
    def test_browser_wait_is_excluded_from_battle_clock_and_request_lead(self):
        async def run():
            now = [100.0]

            async def sleep(seconds):
                now[0] += max(0, seconds)

            class SlowBrowserBroker(_ImmediateTokenBroker):
                def create_request(self, **kwargs):
                    now[0] += 30.0
                    return super().create_request(**kwargs)

                def record_result(self, *args, **kwargs):
                    result = super().record_result(*args, **kwargs)
                    now[0] += 2.0
                    return result

            async def post_json(origin, path, payload, timeout):
                if path.endswith('/begin'):
                    now[0] += 0.2
                    if not payload.get('turnstileToken'):
                        raise MiniAppBeastError('turnstile_required', 403)
                    return {'startsInMs': 1500}
                if path.endswith('/finish'):
                    return {'result': {'score': 100}}
                raise AssertionError(path)

            with tempfile.TemporaryDirectory() as directory:
                broker = SlowBrowserBroker(directory)
                actor = FakeActor()
                actor.state_file = str(Path(directory) / 'state.json')
                monitor = WorldBossMonitor(
                    actor, 'main', post_json=post_json,
                    turnstile_broker=broker, sleep=sleep,
                    monotonic=lambda: now[0], finish_grace_seconds=0,
                )
                monitor._hit_window = AsyncMock(return_value={
                    'action': {'t': 1000, 'holdMs': 1000, 'stance': '强攻'},
                    'ok': True, 'matched': True, 'perfect': True,
                    'accepted_perfect': True, 'damage': 10, 'diagnostic': {},
                })
                result = await monitor._fight(
                    extract_world_boss_entry(DummyMessage()),
                    'signed-init-data', 'session',
                    {'challenge': {
                        'challengeId': 'challenge',
                        'windows': [{'id': 'w1', 'centerMs': 1000, 'hitMs': 460, 'perfectMs': 150}],
                    }},
                )
                sync = result['diagnostics']['clock_sync']
                self.assertEqual(sync['round_trip_ms'], 200)
                self.assertEqual(sync['applied_wait_ms'], 1400)
                self.assertEqual(monitor._hit_window.await_args.args[-1], 100)
                self.assertAlmostEqual(monitor._hit_window.await_args.args[4], 131.8)
                self.assertGreaterEqual(sync['request']['total_duration_ms'], 30_000)
                self.assertEqual(broker.list_requests(include_finished=True)[0]['status'], 'accepted')

        asyncio.run(run())

    def test_live_cf_validation_delays_do_not_make_valid_strikes_arrive_early(self):
        # Timings observed in the first automatic four-account battle. /begin
        # includes Siteverify work before the server starts its 1499 ms timer.
        cases = (
            ('waaiging', 237, 3128), ('main', 274, 1575),
            ('sub', 142, 823), ('xiaohao', 889, 479),
        )

        async def run(account, token_free_ms, verified_ms):
            now = [100.0]
            server_start = [None]
            accepted = []
            transport_ms = min(token_free_ms, verified_ms)

            async def sleep(seconds):
                now[0] += max(0, seconds)

            async def post_json(origin, path, payload, timeout):
                if path.endswith('/begin'):
                    if not payload.get('turnstileToken'):
                        now[0] += token_free_ms / 1000
                        raise MiniAppBeastError('turnstile_failed', 403)
                    now[0] += verified_ms / 1000
                    server_start[0] = now[0] + 1.499 - transport_ms / 2000
                    return {'startsInMs': 1499}
                if path.endswith('/charge-start'):
                    now[0] += transport_ms / 1000
                    return {'chargeTicket': 'clock-fixture-ticket'}
                if path.endswith('/hit'):
                    arrived = (now[0] - server_start[0]) * 1000 + transport_ms / 2
                    now[0] += transport_ms / 1000
                    delta = abs(arrived - 2500)
                    if delta > 620:
                        raise MiniAppBeastError('boss_hit_outside_window', 400)
                    accepted.append(arrived)
                    return {'hit': {'perfect': delta <= 210, 'deltaMs': delta, 'damageYi': 10}}
                if path.endswith('/finish'):
                    return {'result': {'score': 100 if accepted else 0}}
                raise AssertionError(path)

            with tempfile.TemporaryDirectory() as directory:
                actor = FakeActor()
                actor.state_file = str(Path(directory) / 'state.json')
                monitor = WorldBossMonitor(
                    actor, account, post_json=post_json, monotonic=lambda: now[0],
                    sleep=sleep, turnstile_broker=_ImmediateTokenBroker(directory),
                    finish_grace_seconds=0,
                )
                result = await monitor._fight(
                    extract_world_boss_entry(DummyMessage()), 'init-fixture', 'session-fixture',
                    {'challenge': {'challengeId': 'clock-fixture',
                     'windows': [{'id': 'w1', 'centerMs': 2500, 'hitMs': 620, 'perfectMs': 210}]}},
                )
                self.assertEqual(result['score'], 100)
                self.assertEqual(len(accepted), 1)
                self.assertLessEqual(abs(accepted[0] - 2500), 210)
                sync = result['diagnostics']['clock_sync']
                self.assertEqual(sync['request']['successful_attempt_ms'], verified_ms)
                self.assertEqual(sync['round_trip_ms'], transport_ms)

        for case in cases:
            with self.subTest(account=case[0]):
                asyncio.run(run(*case))

    def test_cancelled_worker_removes_pending_browser_request(self):
        async def run():
            sleeping = asyncio.Event()

            async def sleep(seconds):
                sleeping.set()
                await asyncio.Event().wait()

            with tempfile.TemporaryDirectory() as directory:
                broker = WorldBossTurnstileBroker(directory)
                monitor = WorldBossMonitor(FakeActor(), 'main', turnstile_broker=broker, sleep=sleep)
                task = asyncio.create_task(monitor._wait_for_turnstile_token(
                    extract_world_boss_entry(DummyMessage()), '主魂', 'challenge',
                ))
                await sleeping.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(broker.list_requests(), [])
                self.assertEqual(broker.list_requests(include_finished=True)[0]['cancel_reason'], 'worker_cancelled')

        asyncio.run(run())

    def test_browser_diagnostics_and_rejected_begin_are_visible_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory)
            request = broker.create_request(
                event_fingerprint='a' * 64, message_id=123, account='main',
                identity='主魂', challenge_id='challenge', origin='https://asc.aiopenai.app',
            )
            request_id = request['request_id']
            broker.record_browser_event(request_id, 'widget_error', '600010')
            metadata = broker.list_requests()[0]
            self.assertEqual(metadata['browser_error_code'], '600010')
            broker.submit_token(request_id, 'private-fixture-token')
            broker.take_token(request_id)
            broker.record_result(request_id, accepted=False, error='turnstile_failed', http_status=403)
            result = broker.list_requests(include_finished=True)[0]
            self.assertEqual(result['status'], 'rejected')
            self.assertEqual(result['result_error'], 'turnstile_failed')
            self.assertEqual(result['result_http_status'], 403)
            self.assertNotIn('private-fixture-token', str(result))
            self.assertFalse(list(Path(directory).glob('token_*.txt')))

    def test_timeout_preserves_initial_begin_error_and_browser_diagnostics(self):
        async def run():
            now = [100.0]

            async def sleep(seconds):
                now[0] += seconds

            class ErrorBrowserBroker(WorldBossTurnstileBroker):
                def create_request(self, **kwargs):
                    request = super().create_request(**kwargs)
                    self.record_browser_event(request['request_id'], 'widget_error', '600010')
                    return request

            async def post_json(*args):
                raise MiniAppBeastError('turnstile_failed', 403)

            with tempfile.TemporaryDirectory() as directory:
                broker = ErrorBrowserBroker(directory)
                monitor = WorldBossMonitor(
                    FakeActor(), 'main', post_json=post_json, turnstile_broker=broker,
                    sleep=sleep, monotonic=lambda: now[0], turnstile_wait_seconds=20,
                )
                trace = {}
                with self.assertRaises(MiniAppBeastError) as ctx:
                    await monitor._begin_with_turnstile(
                        extract_world_boss_entry(DummyMessage()), '主魂',
                        'signed-init-data', 'session', 'challenge', trace=trace,
                    )
                self.assertEqual(ctx.exception.code, 'world_boss_turnstile_timeout')
                self.assertEqual(trace['attempts'][0]['error'], 'turnstile_failed')
                self.assertEqual(ctx.exception.details['browser']['browser_error_code'], '600010')

        asyncio.run(run())

    def test_token_is_one_shot_and_public_metadata_never_contains_it(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, request_ttl_seconds=60)
            request = broker.create_request(
                event_fingerprint="a" * 64,
                message_id=123,
                account="main",
                identity="主魂",
                challenge_id="challenge",
                origin="https://asc.aiopenai.app",
            )
            submitted = broker.submit_token(request["request_id"], "token-fixture")
            self.assertTrue(submitted["token_available"])
            self.assertNotIn("token-fixture", str(submitted))
            self.assertEqual(broker.take_token(request["request_id"]), "token-fixture")
            self.assertIsNone(broker.take_token(request["request_id"]))
            self.assertNotIn("token-fixture", str(broker.list_requests(include_finished=True)))

    def test_expired_request_rejects_submission(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=lambda: now[0], request_ttl_seconds=30)
            request = broker.create_request(
                event_fingerprint="b" * 64,
                message_id=1,
                account="sub",
                identity="主魂",
                challenge_id="challenge",
                origin="https://asc.aiopenai.app",
            )
            now[0] = 131.0
            with self.assertRaisesRegex(TurnstileRequestError, "expired"):
                broker.submit_token(request["request_id"], "token")

    def test_begin_retries_once_with_browser_token(self):
        async def run():
            calls = []

            async def post_json(origin, path, payload, timeout):
                calls.append((path, dict(payload)))
                if len(calls) == 1:
                    raise MiniAppBeastError("turnstile_required", 403)
                return {"startsInMs": 0}

            with tempfile.TemporaryDirectory() as directory:
                broker = _ImmediateTokenBroker(directory)
                monitor = WorldBossMonitor(
                    FakeActor(),
                    "main",
                    post_json=post_json,
                    turnstile_broker=broker,
                    turnstile_wait_seconds=20,
                )
                trace = {}
                result = await monitor._begin_with_turnstile(
                    extract_world_boss_entry(DummyMessage()),
                    "主魂",
                    "signed-init-data",
                    "session",
                    "challenge",
                    trace=trace,
                )
                self.assertEqual(result["startsInMs"], 0)
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][1]["turnstileToken"], "browser-token-fixture")
                self.assertRegex(calls[1][1]["turnstileIdempotencyKey"], r"^[0-9a-f-]{36}$")
                self.assertNotIn("browser-token-fixture", str(trace))
                self.assertEqual(len(broker.created), 1)

        asyncio.run(run())

    def test_transient_after_browser_token_uses_fresh_token_without_replay(self):
        async def run():
            calls = []

            async def post_json(origin, path, payload, timeout):
                calls.append(dict(payload))
                if len(calls) == 1:
                    raise MiniAppBeastError("turnstile_required", 403)
                if len(calls) == 2:
                    raise MiniAppBeastError("api_timeout")
                return {"startsInMs": 0}

            with tempfile.TemporaryDirectory() as directory:
                broker = _SequencedTokenBroker(directory)
                monitor = WorldBossMonitor(
                    FakeActor(),
                    "main",
                    post_json=post_json,
                    turnstile_broker=broker,
                    turnstile_wait_seconds=20,
                )
                result = await monitor._begin_with_turnstile(
                    extract_world_boss_entry(DummyMessage()),
                    "主魂",
                    "signed-init-data",
                    "session",
                    "challenge",
                )

                self.assertEqual(result["startsInMs"], 0)
                self.assertEqual(len(calls), 3)
                self.assertEqual(broker.tokens, ["browser-token-1", "browser-token-2"])
                self.assertEqual(calls[1]["turnstileToken"], "browser-token-1")
                self.assertEqual(calls[2]["turnstileToken"], "browser-token-2")
                self.assertEqual(
                    [row.get("turnstileToken") for row in calls].count("browser-token-1"),
                    1,
                )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
