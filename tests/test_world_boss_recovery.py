import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError
from miniapp_dwelling import MiniAppDwellingTransport
from tests.test_world_boss_features import DummyMessage, FakeActor, FakeTransport
from world_boss_features import WorldBossMonitor, WORLD_BOSS_IDENTITY, _ProcessLease, extract_world_boss_entry
from world_boss_recovery import WorldBossRecoveryStore


class WorldBossRecoveryTests(unittest.TestCase):
    def test_cached_identity_does_not_initialize_a_blocked_dwelling(self):
        async def run():
            actor = FakeActor()
            transport = MiniAppDwellingTransport(actor.client, "https://t.me/fixture_bot?startapp=fixture")
            transport.identity_player_ids = {WORLD_BOSS_IDENTITY: 42}
            transport.initialize = AsyncMock(side_effect=MiniAppCircuitOpenError(900))
            monitor = WorldBossMonitor(actor, "main", transport=transport)
            self.assertEqual(await monitor._identity_player_id(WORLD_BOSS_IDENTITY), 42)
            transport.initialize.assert_not_awaited()
            await monitor.stop()
        asyncio.run(run())

    def test_blocked_optional_identity_lookup_allows_event_selection(self):
        async def run():
            monitor = WorldBossMonitor(
                FakeActor(), "main", transport=FakeTransport(error=MiniAppCircuitOpenError(900)),
            )
            self.assertIsNone(await monitor._identity_player_id(WORLD_BOSS_IDENTITY))
            await monitor.stop()
        asyncio.run(run())

    def test_temporary_pause_retries_within_event_deadline(self):
        async def run():
            now = [100.0]
            delays = []
            async def sleep(seconds):
                delays.append(seconds)
                now[0] += seconds
            monitor = WorldBossMonitor(FakeActor(), "main", sleep=sleep, monotonic=lambda: now[0])
            entry = extract_world_boss_entry(DummyMessage())
            statuses = iter(("paused_upstream", "completed"))
            async def attempt(*args):
                monitor._record(entry, next(statuses), error="")
            monitor._run_entry_once = AsyncMock(side_effect=attempt)
            await monitor._run_entry(entry, [WORLD_BOSS_IDENTITY])
            self.assertEqual(monitor._run_entry_once.await_count, 2)
            self.assertEqual(delays, [30.0])
            self.assertEqual(monitor._event_status(entry.fingerprint), "completed")
            await monitor.stop()
        asyncio.run(run())

    def test_retry_budget_does_not_extend_when_event_is_updated(self):
        async def run():
            now = [100.0]
            async def sleep(seconds): now[0] += seconds
            message = DummyMessage()
            message.date = datetime.now(timezone.utc) - timedelta(seconds=899)
            entry = extract_world_boss_entry(message)
            monitor = WorldBossMonitor(FakeActor(), "main", sleep=sleep, monotonic=lambda: now[0])
            async def attempt(*args): monitor._record(entry, "paused_upstream", error="")
            monitor._run_entry_once = AsyncMock(side_effect=attempt)
            await monitor._run_entry(entry, [WORLD_BOSS_IDENTITY])
            self.assertEqual(monitor._run_entry_once.await_count, 1)
            await monitor.stop()
        asyncio.run(run())

    def test_startup_fetches_unfinished_notice_outside_latest_messages(self):
        async def run():
            actor = FakeActor()
            message = DummyMessage()
            message.chat_id = 42
            message.date = datetime.now(timezone.utc) - timedelta(seconds=180)
            entry = extract_world_boss_entry(message)
            monitor = WorldBossMonitor(actor, "main")
            monitor._record(entry, "running")
            actor.client.get_messages = AsyncMock(side_effect=[[], message])
            monitor._run_entry = AsyncMock()
            with patch("world_boss_features.resolve_actor_target_chats", new=AsyncMock(return_value=[42])), \
                 patch("world_boss_features.is_game_bot_sender", return_value=True), \
                 patch("world_boss_features.world_boss_identities_for_account", return_value=[WORLD_BOSS_IDENTITY]):
                await monitor.install()
                await asyncio.gather(*monitor._tasks)
            self.assertEqual(actor.client.get_messages.await_args.kwargs, {"ids": message.id})
            monitor._run_entry.assert_awaited_once()
            await monitor.stop()
        asyncio.run(run())

    def test_fresh_worker_rejects_unseen_old_notice(self):
        async def run():
            message = DummyMessage()
            message.date = datetime.now(timezone.utc) - timedelta(seconds=180)
            monitor = WorldBossMonitor(FakeActor(), "main")
            self.assertFalse(await monitor.process_message(message, source="startup"))
            await monitor.stop()
        asyncio.run(run())

    def test_finish_timeout_preserves_battle_and_restart_only_retries_finish(self):
        async def run():
            now = [100.0]
            calls = []
            async def sleep(seconds): now[0] += max(0, seconds)
            async def post(origin, path, payload, timeout):
                calls.append(path.rsplit("/", 1)[-1])
                if path.endswith("/start"):
                    return {"sessionToken": "private-session", "challenge": {
                        "challengeId": "fixture", "windows": [
                            {"id": "w1", "centerMs": 3000, "hitMs": 620, "perfectMs": 210},
                        ],
                    }}
                if path.endswith("/begin"): return {"startsInMs": 0}
                if path.endswith("/charge-start"): return {"chargeTicket": "private-ticket"}
                if path.endswith("/hit"): return {"hit": {"damageYi": 123, "perfect": True}}
                if path.endswith("/finish"): raise MiniAppBeastError("api_timeout")
                raise AssertionError(path)
            actor = FakeActor()
            entry = extract_world_boss_entry(DummyMessage())
            monitor = WorldBossMonitor(actor, "main", transport=FakeTransport(), post_json=post,
                                       sleep=sleep, monotonic=lambda: now[0], finish_grace_seconds=0)
            result = await monitor._run_identity(entry, WORLD_BOSS_IDENTITY, "private-init")
            self.assertEqual(result["status"], "finish_pending")
            self.assertEqual(result["hit_count"], 1)
            self.assertEqual(result["damage_yi_total"], 123)
            self.assertEqual(len(result["diagnostics"]["hits"]), 1)
            monitor._record(entry, "finish_pending", identity_results=[result])
            public = json.dumps(actor.state)
            for secret in ("private-session", "private-ticket", "private-init", entry.token):
                self.assertNotIn(secret, public)
            checkpoint = monitor.recovery_store.load(entry.fingerprint)
            self.assertEqual(checkpoint["stage"], "finish_pending")
            await monitor.stop()

            finish = AsyncMock(return_value={"result": {"score": 100, "grade": "A"}})
            resumed = WorldBossMonitor(actor, "main", post_json=finish)
            with patch("world_boss_features.request_webview_init_data", new=AsyncMock()) as webview:
                await resumed._run_entry(entry, [WORLD_BOSS_IDENTITY])
                webview.assert_not_awaited()
            self.assertEqual(finish.await_count, 1)
            self.assertTrue(finish.await_args.args[1].endswith("/finish"))
            self.assertEqual(finish.await_args.args[2]["bossProof"], checkpoint["proof"])
            self.assertEqual(actor.state["world_boss_events"][0]["status"], "completed")
            self.assertIsNone(resumed.recovery_store.load(entry.fingerprint))
            await resumed.stop()
        asyncio.run(run())

    def test_interrupted_fight_reuses_completed_hits_and_does_not_replay_unknown_hit(self):
        async def run():
            now = [100.0]
            hits = []
            async def sleep(seconds): now[0] += max(0, seconds)
            async def post(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    self.assertEqual(payload["windowId"], "w3")
                    return {"chargeTicket": "fixture"}
                if path.endswith("/hit"):
                    hits.append(payload["windowId"])
                    return {"hit": {"damageYi": 10, "perfect": True}}
                if path.endswith("/finish"):
                    self.assertEqual(len(payload["bossProof"]["actions"]), 2)
                    return {"result": {"score": 90}}
                raise AssertionError("restarted battle must not start or begin again")
            actor = FakeActor()
            entry = extract_world_boss_entry(DummyMessage())
            monitor = WorldBossMonitor(actor, "main", post_json=post, sleep=sleep,
                                       monotonic=lambda: now[0], finish_grace_seconds=0)
            windows = [{"id": f"w{i}", "centerMs": i * 3000, "hitMs": 620, "perfectMs": 210}
                       for i in range(1, 4)]
            checkpoint = {
                "version": 1, "account": "main", "identity": WORLD_BOSS_IDENTITY,
                "entry": asdict(entry), "expires_epoch": time.time() + 900,
                "init_data": "fixture", "session_token": "fixture", "stage": "fighting",
                "payload": {"challenge": {"challengeId": "fixture", "windows": windows}},
                "windows": windows, "claimed_window_ids": ["w1", "w2"],
                "hit_results": {"w1": {"action": {"t": 2980, "holdMs": 1000}, "ok": True,
                                       "matched": True, "perfect": True, "accepted_perfect": True,
                                       "damage": 123, "diagnostic": {"window_id": "w1"}}},
                "battle_start_epoch": time.time(), "sync": {}, "begin_trace": {},
                "round_trip": 0, "starts_in": 0, "reveal_log": [], "reveal_mode": False,
            }
            monitor.recovery_store.save(checkpoint)
            result = await monitor._participate(entry)
            self.assertEqual(hits, ["w3"])
            self.assertEqual(result["damage_yi_total"], 133)
            self.assertEqual(result["hit_count"], 2)
            self.assertEqual(result["skipped_hit_count"], 1)
            await monitor.stop()
        asyncio.run(run())

    def test_private_checkpoint_expiry_and_account_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            store = WorldBossRecoveryStore(Path(directory), "main", clock=lambda: now[0])
            entry = extract_world_boss_entry(DummyMessage())
            store.save({"version": 1, "account": "main", "entry": asdict(entry),
                        "stage": "fighting", "expires_epoch": 1010})
            self.assertEqual(len(store.list_pending()), 1)
            other = WorldBossRecoveryStore(Path(directory), "sub", clock=lambda: now[0])
            self.assertEqual(other.list_pending(), [])
            if os.name != "nt":
                self.assertEqual(store._path(entry.fingerprint).stat().st_mode & 0o777, 0o600)
            now[0] = 1011
            self.assertEqual(store.list_pending(), [])
            self.assertFalse(store._path(entry.fingerprint).exists())

    def test_checkpoint_write_finishes_before_cancellation_releases_lock(self):
        async def run():
            entered, release = threading.Event(), threading.Event()
            monitor = WorldBossMonitor(FakeActor(), "main")
            monitor._checkpoint = {"sequence": 1}
            saved = []
            def save(value):
                entered.set()
                release.wait(3)
                saved.append(value)
            with patch.object(monitor.recovery_store, "save", side_effect=save):
                task = asyncio.create_task(monitor._save_checkpoint())
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertTrue(monitor._checkpoint_lock.locked())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(saved, [{"sequence": 1}])
            self.assertFalse(monitor._checkpoint_lock.locked())
            await monitor.stop()
        asyncio.run(run())

    def test_account_lease_excludes_another_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account.lock"
            first, second = _ProcessLease(path), _ProcessLease(path)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
                first.release()
                self.assertTrue(second.acquire())
            finally:
                first.release()
                second.release()


if __name__ == "__main__":
    unittest.main()
