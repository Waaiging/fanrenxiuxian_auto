import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import automation_settings as settings
from automation_command_controls import CommandControlPaused, NANGONGQUE_BOSS
from miniapp_beast import MiniAppBeastError
from nangongque_boss import (
    JOIN_SECONDS, NangongqueBossMonitor, NangongqueHTTP, NangongqueRoom,
    drain_operation, extract_nangongque_entry,
)
from nangongque_strategy import NangongqueStrategy, RoomInput, hazard_clearance
from world_boss_recovery import WorldBossRecoveryStore
from world_boss_turnstile import WorldBossTurnstileBroker
from world_boss_browser import AutomaticTurnstileWorker, NativeTurnstileBrowser


TOKEN = "nqb_fixture_public"
ORIGIN = "https://asc.aiopenai.app"
ANNOUNCEMENT = "【世界通告｜月殿血誓开启】\n掩月宗月殿禁制异动，南宫阙血契残影已现。"


class Clock:
    def __init__(self):
        self.value = 2000000000.0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds
        await asyncio.sleep(0)


def message(clock, *, url=None, sender="hantianzun24_bot", chat_id=-1002083016447, message_id=100):
    return SimpleNamespace(
        id=message_id, chat_id=chat_id, date=datetime.fromtimestamp(clock(), timezone.utc),
        raw_text=ANNOUNCEMENT,
        buttons=[[SimpleNamespace(text="进入月殿战场", url=url or f"https://t.me/hantianzun24_bot?startapp={TOKEN}")]],
        get_sender=AsyncMock(return_value=SimpleNamespace(username=sender)),
    )


def room_state(*, seq=1, phase=1, hp=100, status="fighting", claimed=False):
    data = {"ok": True, "sessionToken": "private-session", "roomId": "room-1", "playerId": "42",
            "stateSeq": seq, "serverTimeMs": 2000000000000 + seq * 240, "inputAckSeq": max(0, seq - 1),
            "room": {"status": status, "humanCount": 10, "roomLimit": 20},
            "boss": {"x": 50, "y": 24, "phase": phase, "hp": 1000000, "maxHp": 1000000},
            "players": [{"id": "42", "self": True, "x": 50, "y": 62, "r": 2.2, "hp": hp,
                         "maxHp": 100, "attackCdMs": 0, "dodgeCdMs": 0, "mechCdMs": 0}],
            "hazards": [], "mechanics": {"swordThreads": 7, "mirrorLock": 6, "bloodBane": 20,
                                          "wanGuard": 80, "ambush": 0}}
    if status in {"cleared", "failed"}:
        data["settlement"] = {"success": status == "cleared", "claimed": claimed, "grade": "甲",
                              "score": 710, "rank": 3, "cultivation": 5000, "stones": 200,
                              "materials": [{"name": "剑丝", "quantity": 2}], "automationReview": True,
                              "sessionToken": "must-never-be-in-public-state"}
    return data


class FixtureGame:
    """Synthetic server fixture following the published client wire format."""
    def __init__(self, *, claim_timeout=False):
        self.calls = []
        self.inputs = 0
        self.claimed = False
        self.claim_timeout = claim_timeout

    async def post(self, origin, path, body, timeout):
        operation = path.rsplit("/", 1)[-1]
        self.calls.append((operation, copy.deepcopy(body)))
        if operation == "start":
            return room_state()
        if operation == "input":
            self.inputs += 1
        if operation == "claim":
            self.claimed = True
            if self.claim_timeout:
                self.claim_timeout = False
                raise MiniAppBeastError("timeouterror")
            return {"ok": True, "settlement": {"claimed": True}}
        return room_state(seq=self.inputs + 2, status="cleared" if self.inputs else "fighting", claimed=self.claimed)


class NangongqueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.clock = Clock()
        self.actor = SimpleNamespace(
            client=SimpleNamespace(add_event_handler=Mock(), remove_event_handler=Mock(), get_messages=AsyncMock(return_value=[])),
            config={}, mc={}, state={}, state_file=str(self.base / "state.json"),
            my_info=SimpleNamespace(username="Owner", first_name="Owner", id=42),
            avatars=[], identity_usernames={"主魂": ["Owner"]}, save_state=Mock(),
            dashboard_command_paused=Mock(return_value=False),
        )
        self.entry = extract_nangongque_entry(message(self.clock))
        self.store = WorldBossRecoveryStore(self.base / "private", "main", clock=self.clock,
                                            stages={"joining", "fighting", "finish_pending", "terminal"})
        self.broker = WorldBossTurnstileBroker(self.base / "verification", clock=self.clock)

    def checkpoint(self, stage="fighting"):
        return {"version": 1, "account": "main", "entry": asdict(self.entry), "identity": "主魂",
                "expires_epoch": self.clock() + 1800, "stage": stage, "last_seq": 0,
                "sessionToken": "private-session", "roomId": "room-1", "playerId": "42"}

    def monitor(self, **kwargs):
        return NangongqueBossMonitor(self.actor, "main", broker=self.broker, store=self.store,
                                     sleep=self.clock.sleep, epoch=self.clock, **kwargs)

    async def room(self, game=None, checkpoint=None, **kwargs):
        game = game or FixtureGame()
        http = NangongqueHTTP(ORIGIN, "fixture", post_json=game.post)
        self.addAsyncCleanup(http.close)
        room = NangongqueRoom(checkpoint or self.checkpoint(), self.store, http,
                             sleep=self.clock.sleep, clock=self.clock, epoch=self.clock,
                             enable_socket=False, **kwargs)
        return room, game

    async def test_dynamic_entry_source_and_activity_are_checked(self):
        self.assertIsNotNone(self.entry)
        self.assertEqual(self.entry.origin, ORIGIN)
        self.assertNotIn(TOKEN, repr(self.entry))
        for bad in ("https://evil.example/hantianzun24_bot?startapp=" + TOKEN,
                    "https://t.me/hantianzun24_bot?startapp=qyz_fixture",
                    "https://t.me.evil.example/hantianzun24_bot?startapp=" + TOKEN):
            with self.subTest(bad=bad):
                self.assertIsNone(extract_nangongque_entry(message(self.clock, url=bad)))
        self.assertIsNone(extract_nangongque_entry(message(self.clock), sender_username="hantianzun32_bot"))
        wrong = message(self.clock)
        wrong.buttons[0][0].text = "查看战况"
        self.assertIsNone(extract_nangongque_entry(wrong))
        wrong.buttons[0][0].text = "进入月殿战场"
        wrong.raw_text = "【世界通告｜月殿血誓结算】 南宫阙"
        self.assertIsNone(extract_nangongque_entry(wrong))

    async def test_only_configured_chats_and_trusted_bot_are_queued(self):
        monitor = self.monitor()
        monitor.target_chats = [2083016447, 1680975844]
        monitor._run_entry = AsyncMock()
        self.assertFalse(await monitor.process_message(message(self.clock, sender="untrusted_bot")))
        self.assertFalse(await monitor.process_message(message(self.clock, chat_id=-100999)))
        self.assertTrue(await monitor.process_message(message(self.clock)))
        self.assertFalse(await monitor.process_message(message(self.clock), source="edited"))
        # Same event copied to the other game group is still one participation.
        self.assertFalse(await monitor.process_message(message(self.clock, chat_id=-1001680975844)))
        await asyncio.gather(*monitor.tasks)
        monitor._run_entry.assert_awaited_once()

    async def test_old_edit_does_not_reopen_registration(self):
        monitor = self.monitor()
        old = message(self.clock)
        self.clock.value += JOIN_SECONDS + 1
        old.edit_date = datetime.fromtimestamp(self.clock(), timezone.utc)
        monitor._run_entry = AsyncMock()
        self.assertFalse(await monitor.process_message(old, source="edited"))
        monitor._run_entry.assert_not_called()

    async def test_install_covers_new_edited_both_chats_and_recovers_private_room(self):
        self.store.save(self.checkpoint())
        monitor = self.monitor()
        monitor._run_entry = AsyncMock()
        with patch("nangongque_boss.resolve_actor_target_chats", AsyncMock(return_value=[2083016447, 1680975844])):
            self.assertTrue(await monitor.install())
            await asyncio.sleep(0)
        self.assertTrue(self.actor.state["nangongque_boss_monitor_active"])
        self.assertEqual(self.actor.client.add_event_handler.call_count, 2)
        self.assertEqual(self.actor.client.get_messages.await_count, 2)
        monitor._run_entry.assert_awaited_once()
        await monitor.stop()
        self.assertFalse(self.actor.state["nangongque_boss_monitor_active"])
        self.assertEqual(self.actor.client.remove_event_handler.call_count, 2)

    async def test_terminal_private_receipt_blocks_another_process(self):
        checkpoint = self.checkpoint("terminal")
        checkpoint["result"] = {"status": "completed", "identity": "主魂", "settlement": {"claimed": True}}
        self.store.save(checkpoint)
        monitor = self.monitor(post_json=AsyncMock())
        await monitor._run_entry(self.entry, "主魂")
        monitor.post_json.assert_not_called()
        self.assertTrue(self.actor.state["nangongque_boss_events"][-1]["terminal"])

    async def test_complete_monitor_joins_original_room_claims_and_redacts(self):
        game = FixtureGame()
        monitor = self.monitor(post_json=game.post, transport=SimpleNamespace(player_id=lambda _: 42),
                               room_factory=partial(NangongqueRoom, enable_socket=False))
        with patch("nangongque_boss.request_webview_init_data", AsyncMock(return_value="private-init-data")):
            await monitor._run_entry(self.entry, "主魂")
        record = self.actor.state["nangongque_boss_events"][-1]
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["identity_results"][0]["settlement"]["claimed"])
        self.assertTrue(record["identity_results"][0]["settlement"]["automationReview"])
        self.assertEqual([op for op, _ in game.calls], ["start", "state", "input", "claim"])
        public = json.dumps(self.actor.state)
        for secret in (TOKEN, "private-init-data", "private-session", "must-never-be-in-public-state"):
            self.assertNotIn(secret, public)
        persisted = self.store.load(self.entry.fingerprint)
        self.assertEqual(persisted["stage"], "terminal")
        self.assertEqual(persisted["sessionToken"], "")

    async def test_pause_after_auth_prevents_join(self):
        monitor = self.monitor()
        http = SimpleNamespace(request=AsyncMock())

        async def auth(*_):
            self.actor.dashboard_command_paused.return_value = True
            return "private-init-data"
        with patch("nangongque_boss.request_webview_init_data", auth):
            with self.assertRaises(CommandControlPaused):
                await monitor._join(self.entry, "主魂", self.checkpoint("joining"), http)
        http.request.assert_not_called()

    async def test_cancellation_after_claim_keeps_confirmed_completion(self):
        game = FixtureGame()
        def factory(checkpoint, store, http, **kwargs):
            async def run():
                checkpoint.update(stage="terminal", sessionToken="", result={"status": "completed", "settlement": {"claimed": True}})
                store.save(checkpoint)
                raise asyncio.CancelledError
            return SimpleNamespace(run=run)
        monitor = self.monitor(post_json=game.post, transport=SimpleNamespace(player_id=lambda _: 42), room_factory=factory)
        with patch("nangongque_boss.request_webview_init_data", AsyncMock(return_value="private-init-data")):
            with self.assertRaises(asyncio.CancelledError):
                await monitor._run_entry(self.entry, "主魂")
        record = self.actor.state["nangongque_boss_events"][-1]
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["identity_results"][0]["settlement"]["claimed"])

    async def test_identity_choice_never_falls_back_to_other_avatar(self):
        self.actor.avatars = ["MyAvatar"]
        monitor = self.monitor()
        http = SimpleNamespace(request=AsyncMock(return_value={"ok": True, "needsIdentitySelection": True,
            "identityChoices": [{"playerId": -4, "source": "avatar", "displayName": "MyAvatar"}]}))
        with patch("nangongque_boss.request_webview_init_data", AsyncMock(return_value="private-init-data")):
            with self.assertRaisesRegex(MiniAppBeastError, "nangongque_identity_missing"):
                await monitor._join(self.entry, "主魂", self.checkpoint("joining"), http)
        self.assertEqual(http.request.await_count, 1)

    async def test_turnstile_uses_nangongque_activity_and_actual_join_ack(self):
        game = FixtureGame()
        calls = []

        async def start(operation, payload, **_):
            calls.append(copy.deepcopy(payload))
            if "turnstileToken" not in payload:
                raise MiniAppBeastError("turnstile_required", 403)
            return room_state()

        original = self.broker.create_request
        def create(**kwargs):
            result = original(**kwargs)
            self.broker.submit_token(result["request_id"], "real-callback-fixture")
            return result
        monitor = self.monitor(transport=SimpleNamespace(player_id=lambda _: 42))
        with patch.object(self.broker, "create_request", side_effect=create), \
             patch("nangongque_boss.request_webview_init_data", AsyncMock(return_value="private-init-data")):
            await monitor._join(self.entry, "主魂", self.checkpoint("joining"), SimpleNamespace(request=start))
        self.assertEqual(len(calls), 2)
        self.assertNotIn("turnstileToken", calls[0])
        self.assertEqual(calls[1]["turnstileToken"], "real-callback-fixture")
        request = self.broker.list_requests(include_finished=True)[0]
        self.assertEqual(request["activity"], "nangongque")
        self.assertEqual(request["status"], "accepted")
        self.assertFalse(any((self.base / "verification").glob("token_*.txt")))

    async def test_verification_never_extends_sixty_second_entry_deadline(self):
        monitor = self.monitor()
        self.clock.value += 52
        with self.assertRaises(MiniAppBeastError):
            await monitor._verification(self.entry, "主魂")
        self.assertLessEqual(self.clock(), self.entry.notice_epoch + 60.01)
        request = self.broker.list_requests(include_finished=True)[0]
        self.assertEqual(request["status"], "cancelled")

    async def test_http_referer_and_operation_allowlist(self):
        http = NangongqueHTTP(ORIGIN, "fixture", post_json=AsyncMock())
        try:
            self.assertEqual(http.client.referer_path, "/miniapp/xianxia-nangongque-boss")
            with self.assertRaises(MiniAppBeastError):
                await http.request("unknown", {})
            http.post_json.assert_not_called()
        finally:
            await http.close()

    async def test_room_uses_real_input_shape_and_server_settlement(self):
        room, game = await self.room()
        result = await room.run()
        self.assertEqual(result["settlement"]["score"], 710)
        op, body = next(item for item in game.calls if item[0] == "input")
        self.assertEqual(set(body), {"sessionToken", "roomId", "playerId", "input"})
        self.assertEqual(set(body["input"]), {"seq", "time", "moveX", "moveY", "action", "compact"})
        self.assertLessEqual(math.hypot(body["input"]["moveX"], body["input"]["moveY"]), 1.00001)
        self.assertEqual(body["input"]["action"], "mechanic")
        self.assertEqual(result["diagnostics"]["requested_actions"]["mechanic"], 1)

    async def test_lost_claim_response_recovers_receipt_without_duplicate_claim(self):
        room, game = await self.room(FixtureGame(claim_timeout=True))
        with self.assertRaisesRegex(MiniAppBeastError, "timeouterror"):
            await room.run()
        saved = self.store.load(self.entry.fingerprint)
        self.assertEqual(saved["stage"], "finish_pending")
        resumed, _ = await self.room(game, saved)
        result = await resumed.run()
        self.assertTrue(result["settlement"]["claimed"])
        self.assertEqual([op for op, _ in game.calls].count("claim"), 1)
        self.assertEqual([op for op, _ in game.calls].count("input"), 1)

    async def test_input_failure_reserves_sequence_and_restart_does_not_replay(self):
        game = FixtureGame()
        room, _ = await self.room(game)
        room.accept(room_state())
        failing = AsyncMock(side_effect=MiniAppBeastError("timeouterror"))
        room.http.request = failing
        with self.assertRaises(MiniAppBeastError):
            await room.send_input(RoomInput())
        saved = self.store.load(self.entry.fingerprint)
        self.assertEqual(saved["last_seq"], 1)
        resumed, game = await self.room(game, saved)
        await resumed.run()
        sent = next(body for op, body in game.calls if op == "input")
        self.assertGreater(sent["input"]["seq"], 1)
        self.assertEqual(game.calls[0][0], "state")

    async def test_cancellation_drains_inflight_input_and_checkpoint(self):
        room, _ = await self.room()
        room.accept(room_state())
        entered, release = asyncio.Event(), asyncio.Event()

        async def request(*_):
            entered.set()
            await release.wait()
            return room_state(seq=2)
        room.http.request = request
        task = asyncio.create_task(drain_operation(room.send_input(RoomInput())))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.store.load(self.entry.fingerprint)["last_seq"], 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(room.diag["input_responses"], 1)

    async def test_checkpoint_delay_rechecks_live_state_before_sending(self):
        room, game = await self.room()
        room.accept(room_state())
        async def delayed_save():
            self.clock.value += 2
        room.save = delayed_save
        await room.send_input(RoomInput(action="attack"))
        body = game.calls[-1][1]["input"]
        self.assertEqual((body["action"], body["moveX"], body["moveY"]), ("", 0, 0))

    async def test_terminal_snapshot_during_checkpoint_prevents_new_input(self):
        room, game = await self.room()
        room.accept(room_state())
        async def finish_during_save():
            room.accept(room_state(seq=3, status="cleared"))
        room.save = finish_during_save
        await room.send_input(RoomInput(action="attack"))
        self.assertEqual(game.calls, [])

    async def test_out_of_order_snapshot_and_cross_room_response_are_rejected(self):
        room, _ = await self.room()
        room.accept(room_state(seq=10, phase=3))
        self.assertFalse(room.accept(room_state(seq=9, phase=1)))
        self.assertEqual(room.snapshot["boss"]["phase"], 3)
        wrong = room_state(seq=11)
        wrong["roomId"] = "another-room"
        with self.assertRaises(MiniAppBeastError):
            room.accept(wrong)

    async def test_compact_ack_does_not_refresh_hazard_age(self):
        room, _ = await self.room()
        room.accept(room_state())
        self.clock.value += 1
        room.accept({"ok": True, "compact": True, "stateSeq": 2, "inputAckSeq": 12,
                     "serverTimeMs": 2000000001000, "self": {"hp": 25}})
        self.assertEqual(room.snapshot["players"][0]["hp"], 25)
        self.assertEqual(self.clock() - room.full_received, 1)
        self.assertEqual(room.sequence, 12)

    async def test_private_expiry_and_qingyuanzi_recovery_format_stay_independent(self):
        self.store.save(self.checkpoint("terminal"))
        self.assertIsNotNone(self.store.load(self.entry.fingerprint))
        old_store = WorldBossRecoveryStore(self.base / "qyz", "main", clock=self.clock)
        old_store.save(self.checkpoint("terminal"))
        self.assertIsNone(old_store.load(self.entry.fingerprint))
        self.clock.value += 1801
        self.assertEqual(self.store.list_pending(), [])
        self.assertFalse(list((self.base / "private").glob("*.json")))

    async def test_incapacitated_player_waits_for_room_settlement_without_input(self):
        room, game = await self.room()
        polls = 0
        original = game.post

        async def post(origin, path, body, timeout):
            nonlocal polls
            if path.endswith("/state"):
                polls += 1
                game.calls.append(("state", body))
                return room_state(seq=polls, hp=0, status="failed" if polls > 1 else "fighting")
            return await original(origin, path, body, timeout)
        room.http.post_json = post
        result = await room.run()
        self.assertFalse(result["settlement"]["success"])
        self.assertTrue(result["settlement"]["claimed"])
        self.assertNotIn("input", [op for op, _ in game.calls])

    async def test_websocket_uses_ticket_and_applies_direct_state_messages(self):
        room, _ = await self.room()
        socket = Mock()
        socket.recv.side_effect = [json.dumps(room_state(seq=5, phase=2)), ""]
        factory = Mock(return_value=socket)
        room.socket_factory = factory
        room.http.request = AsyncMock(return_value={"ok": True, "ticket": "private-ws-ticket"})
        task = asyncio.create_task(room._socket_loop())
        try:
            for _ in range(50):
                if room.snapshot.get("stateSeq") == 5:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(room.snapshot["boss"]["phase"], 2)
            url = factory.call_args.args[0]
            self.assertEqual(url, "wss://asc.aiopenai.app/ws/miniapp/xianxia-nangongque-boss/state?ticket=private-ws-ticket")
            self.assertNotIn("initData", url)
            self.assertNotIn("private-ws-ticket", json.dumps(room.diag))
        finally:
            room.closed = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        socket.close.assert_called()

    async def test_pause_after_taking_browser_token_cancels_request_without_join(self):
        monitor = self.monitor(transport=SimpleNamespace(player_id=lambda _: 42))
        http = SimpleNamespace(request=AsyncMock(side_effect=MiniAppBeastError("turnstile_required")))
        create, take = self.broker.create_request, self.broker.take_token
        def submit(**kwargs):
            result = create(**kwargs)
            self.broker.submit_token(result["request_id"], "callback-fixture")
            return result
        def pause_after_take(request_id):
            token = take(request_id)
            self.actor.dashboard_command_paused.return_value = True
            return token
        with patch.object(self.broker, "create_request", side_effect=submit), \
             patch.object(self.broker, "take_token", side_effect=pause_after_take), \
             patch("nangongque_boss.request_webview_init_data", AsyncMock(return_value="private-init-data")):
            with self.assertRaises(CommandControlPaused):
                await monitor._join(self.entry, "主魂", self.checkpoint("joining"), http)
        self.assertEqual(http.request.await_count, 1)
        self.assertEqual(self.broker.list_requests(include_finished=True)[0]["status"], "cancelled")

    async def test_compact_receipt_cannot_replace_self_with_another_player(self):
        room, _ = await self.room()
        room.accept(room_state())
        with self.assertRaises(MiniAppBeastError):
            room.accept({"ok": True, "compact": True, "stateSeq": 2, "self": {"id": "other", "hp": 200}})
        self.assertEqual(room.snapshot["players"][0]["id"], "42")


class NangongqueStrategyTests(unittest.TestCase):
    def test_four_mechanics_use_confirmed_phase_and_position(self):
        cases = [(1, 50, 62, "sword_threads"), (2, 50, 47, "mirror"),
                 (3, 50, 66, "blood_bane"), (4, 50, 84, "guard_wan")]
        for phase, x, y, reason in cases:
            with self.subTest(phase=phase):
                snapshot = room_state(phase=phase)
                snapshot["players"][0].update(x=x, y=y)
                snapshot["mechanics"]["wanGuard"] = 40
                intent = NangongqueStrategy().decide(snapshot, "42", 100)
                self.assertEqual((intent.action, intent.reason), ("mechanic", reason))

    def test_does_not_press_mechanic_outside_its_zone(self):
        for phase in range(1, 5):
            snapshot = room_state(phase=phase)
            snapshot["players"][0].update(x=10, y=90)
            intent = NangongqueStrategy().decide(snapshot, "42", 100)
            self.assertNotEqual(intent.action, "mechanic")
            self.assertGreater(math.hypot(intent.move_x, intent.move_y), 0)

    def test_waits_for_server_cooldowns_and_local_floor(self):
        strategy = NangongqueStrategy()
        snapshot = room_state()
        snapshot["players"][0].update(attackCdMs=50, dodgeCdMs=100, mechCdMs=100)
        self.assertEqual(strategy.decide(snapshot, "42", 100).action, "")
        snapshot["players"][0].update(attackCdMs=0, mechCdMs=0)
        intent = strategy.decide(snapshot, "42", 100)
        strategy.sent(intent, 100)
        self.assertNotEqual(strategy.decide(snapshot, "42", 100.2).action, "mechanic")
        self.assertEqual(strategy.decide(snapshot, "42", 101.6).action, "mechanic")

    def test_imminent_line_or_cone_uses_normal_dodge_input(self):
        hazards = [
            {"type": "line", "x1": 4, "y1": 62, "x2": 96, "y2": 62, "width": 4.4},
            {"type": "blood", "x1": 50, "y1": 24, "x2": 50, "y2": 85, "width": 5.6},
            {"type": "cone", "x": 50, "y": 26, "angle": math.pi / 2, "spread": .38, "range": 72},
        ]
        for hazard in hazards:
            with self.subTest(kind=hazard["type"]):
                hazard.update(warn=.15, life=.6)
                snapshot = room_state()
                snapshot["hazards"] = [hazard]
                intent = NangongqueStrategy().decide(snapshot, "42", 100)
                self.assertEqual(intent.action, "dodge")
                self.assertGreater(math.hypot(intent.move_x, intent.move_y), .5)
                x, y = 50 + intent.move_x * 7.2, 62 + intent.move_y * 7.2
                self.assertGreater(hazard_clearance(hazard, x, y), hazard_clearance(hazard, 50, 62))

    def test_warning_time_accounts_for_snapshot_age(self):
        snapshot = room_state()
        snapshot["hazards"] = [{"type": "line", "x1": 4, "y1": 62, "x2": 96, "y2": 62,
                                "width": 4.4, "warn": 1.1, "life": 1.4}]
        self.assertEqual(NangongqueStrategy().decide(snapshot, "42", 100, snapshot_age=.9).action, "dodge")

    def test_no_actions_for_joining_dead_missing_self_stale_or_terminal(self):
        snapshot = room_state(status="joining")
        self.assertEqual(NangongqueStrategy().decide(snapshot, "42", 100).action, "")
        for state in (room_state(hp=0), room_state(status="cleared"), room_state(status="failed")):
            self.assertFalse(NangongqueStrategy().decide(state, "42", 100).send)
        snapshot = room_state()
        snapshot["players"][0].update(id="someone-else", self=False)
        self.assertFalse(NangongqueStrategy().decide(snapshot, "42", 100).send)
        self.assertFalse(NangongqueStrategy().decide(room_state(), "42", 100, snapshot_age=2).send)


class NangongqueSettingsAndBrowserTests(unittest.TestCase):
    def test_independent_participants_normalization_and_one_identity_per_account(self):
        defaults = settings.default_automation_settings()
        self.assertEqual(defaults["nangongque_boss"]["participants"],
                         [f"{account}|主魂" for account in ("main", "sub", "xiaohao", "waaiging")])
        normalized = settings.normalize_automation_settings({"world_boss": {"participants": []},
            "nangongque_boss": {"participants": ["main|主魂", "main|无咎子", "invalid"]}})
        self.assertEqual(normalized["world_boss"]["participants"], [])
        self.assertEqual(normalized["nangongque_boss"]["participants"], ["main|主魂"])
        self.assertEqual(settings.nangongque_boss_identities_for_account("sub", normalized), [])

    def test_legacy_settings_save_preserves_new_participants(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settings, "AUTOMATION_SETTINGS_FILE", Path(directory) / "settings.json"):
            saved = settings.save_automation_settings(world_boss_participants=["main|主魂"], mulan_support_mode="护阵",
                                                     nangongque_boss_participants=[])
            saved = settings.save_automation_settings(world_boss_participants=[], mulan_support_mode="护阵")
            self.assertEqual(saved["nangongque_boss"]["participants"], [])
            for bad in (["main|主魂", "main|无咎子"], ["wrong|wrong"], "main|主魂"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    settings.save_automation_settings(world_boss_participants=[], mulan_support_mode="护阵",
                                                      nangongque_boss_participants=bad)

    def test_browser_switches_page_and_waits_for_matching_action(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = NativeTurnstileBrowser(directory)
            browser.start = Mock()
            browser.command = Mock()
            browser.evaluate = Mock(return_value=True)
            browser.prepare()
            browser.prepare(activity="nangongque")
            browser.prepare(activity="nangongque")
            browser.prepare()
            paths = [call.args[1]["url"] for call in browser.command.call_args_list if call.args[0] == "Page.navigate"]
            self.assertEqual(paths, [ORIGIN + "/miniapp/xianxia-world-boss",
                                     ORIGIN + "/miniapp/xianxia-nangongque-boss", ORIGIN + "/miniapp/xianxia-world-boss"])
            conditions = [call.args[0] for call in browser.evaluate.call_args_list]
            self.assertIn("nangongque_world_boss_start", conditions[1])
            self.assertIn("location.pathname", conditions[1])

    def test_shared_browser_queue_passes_activity_and_prioritizes_short_deadline(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            broker = WorldBossTurnstileBroker(directory, clock=clock)
            base = dict(event_fingerprint="a" * 64, message_id=1, account="main", identity="主魂", challenge_id="", origin=ORIGIN)
            broker.create_request(**base, ttl_seconds=180)
            request = broker.create_request(**base, activity="nangongque", ttl_seconds=50)
            browser = SimpleNamespace(verify=Mock(return_value="callback-fixture"), close=Mock())
            worker = AutomaticTurnstileWorker(broker, browser, clock=clock)
            self.assertTrue(worker.run_once())
            self.assertEqual(browser.verify.call_args.kwargs["activity"], "nangongque")
            self.assertEqual(broker.get_request(request["request_id"])["status"], "submitted")
            with self.assertRaises(ValueError):
                broker.create_request(**base, activity="untrusted-page")


if __name__ == "__main__":
    unittest.main()
