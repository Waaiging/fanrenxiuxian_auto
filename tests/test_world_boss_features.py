import asyncio
import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from world_boss_features import (
    WORLD_BOSS_HOLD_MS,
    WORLD_BOSS_STANCE,
    WorldBossMonitor,
    extract_world_boss_entry,
    select_identity_choice,
    select_main_identity_choice,
)


class DummyMessage:
    def __init__(self, *, message_id=580302, sender_username="hantianzun32_bot", url=None):
        self.id = message_id
        self.raw_text = "━━━━━━━━━━━━━━━\n【世界通告｜真仙试锋开启】\nBoss 资料"
        self.text = self.raw_text
        self.date = datetime.now(timezone.utc)
        self._sender = SimpleNamespace(username=sender_username, first_name="韩天尊")
        url = url or "https://t.me/hantianzun32_bot?startapp=qyz_fixture_token"
        raw = SimpleNamespace(url=url)
        self.buttons = [[SimpleNamespace(text="进入真仙战场", url=url, button=raw)]]

    async def get_sender(self):
        return self._sender


class FakeClient:
    def __init__(self):
        self.handlers = []

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback):
        self.handlers = [item for item in self.handlers if item[0] is not callback]

    async def get_messages(self, target, **kwargs):
        return []


class FakeActor:
    def __init__(self, *, avatars=None, identity_usernames=None):
        self.client = FakeClient()
        self.config = {}
        self.mc = {}
        self.state = {}
        self.state_file = "state_fixture.json"
        self.target_chat_id = 2083016447
        self.avatars = list(avatars or [])
        self.identity_usernames = identity_usernames or {"主魂": ["Waaiging"]}
        self.my_info = SimpleNamespace(username="Waaiging", first_name="Waaiging")
        self.saved = 0

    def save_state(self):
        self.saved += 1


class FakeTransport:
    def __init__(self, player_id=42, error=None, player_ids=None):
        self._player_id = player_id
        self.player_ids = dict(player_ids or {})
        self.error = error
        self.initialized = 0

    async def initialize(self):
        self.initialized += 1
        if self.error:
            raise self.error

    def player_id(self, identity):
        if self.error:
            raise self.error
        if self.player_ids:
            return self.player_ids[identity]
        if identity != "主魂":
            raise AssertionError(identity)
        return self._player_id


class WorldBossFeatureTests(unittest.TestCase):
    def test_account_offsets_are_all_early_and_staggered(self):
        window = {"perfectMs": 210}
        offsets = {
            account: WorldBossMonitor(FakeActor(), account)._hit_offset_ms(window)
            for account in ("main", "sub", "xiaohao", "waaiging")
        }

        self.assertEqual(
            offsets,
            {"main": -160, "sub": -120, "xiaohao": -80, "waaiging": -40},
        )

    def test_extracts_only_matching_trusted_entry_shape(self):
        message = DummyMessage()
        entry = extract_world_boss_entry(message, sender_username="hantianzun32_bot")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.message_id, 580302)
        self.assertEqual(entry.bot_username, "hantianzun32_bot")
        self.assertEqual(entry.origin, "https://asc.aiopenai.app")
        self.assertNotIn("qyz_fixture_token", repr(entry))

        self.assertIsNone(
            extract_world_boss_entry(message, sender_username="hantianzun31_bot")
        )
        message.buttons[0][0].text = "查看战况"
        self.assertIsNone(extract_world_boss_entry(message))

    def test_rejects_non_qyz_or_non_telegram_button(self):
        wrong_token = DummyMessage(
            url="https://t.me/hantianzun32_bot?startapp=df_fixture_token"
        )
        wrong_origin = DummyMessage(
            url="https://example.com/hantianzun32_bot?startapp=qyz_fixture_token"
        )
        self.assertIsNone(extract_world_boss_entry(wrong_token))
        self.assertIsNone(extract_world_boss_entry(wrong_origin))

    def test_selects_personal_main_identity_instead_of_avatar(self):
        actor = FakeActor(avatars=["缘生子"])
        choices = [
            {"playerId": 7, "source": "avatar", "displayName": "缘生子"},
            {"playerId": 42, "source": "personal", "displayName": "Waaiging"},
        ]
        self.assertEqual(select_main_identity_choice(actor, choices), 42)

    def test_refuses_ambiguous_identity_choices(self):
        actor = FakeActor(avatars=["缘生子"])
        choices = [
            {"playerId": 7, "source": "avatar", "displayName": "缘生子"},
            {"playerId": 8, "source": "avatar", "displayName": "厚土"},
        ]
        self.assertIsNone(select_main_identity_choice(actor, choices))

    def test_selects_requested_avatar_identity(self):
        actor = FakeActor(
            avatars=["无咎子", "缘生子"],
            identity_usernames={
                "主魂": ["Waaiging"],
                "无咎子": ["WuxingLinggen"],
                "缘生子": ["Kulipabp"],
            },
        )
        choices = [
            {"playerId": 7, "source": "avatar", "sourceLabel": "缘生子", "displayName": "Kulipabp"},
            {"playerId": 8, "source": "avatar", "sourceLabel": "无咎子", "displayName": "WuxingLinggen"},
            {"playerId": 42, "source": "personal", "displayName": "Waaiging"},
        ]
        self.assertEqual(select_identity_choice(actor, choices, "无咎子"), 8)
        self.assertEqual(select_identity_choice(actor, choices, "缘生子"), 7)

    def test_full_fight_uses_main_player_and_expected_proof(self):
        async def run():
            actor = FakeActor(avatars=["缘生子"])
            calls = []

            async def post_json(origin, path, payload, timeout):
                calls.append((path, payload))
                if path.endswith("/start"):
                    return {
                        "sessionToken": "session_fixture",
                        "boss": {"actionsUsed": 0, "actionsRemaining": 1},
                        "player": {
                            "label": "剑修",
                            "root": "异灵根(风)",
                            "maxHp": 188,
                            "attackBonus": 1.08,
                        },
                        "challenge": {
                            "challengeId": "challenge_fixture",
                            "windows": [
                                {"id": "w1", "centerMs": 0, "hitMs": 1, "perfectMs": 1}
                            ],
                        },
                    }
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/hit"):
                    return {"hit": {"damageYi": 123}}
                if path.endswith("/finish"):
                    return {"result": {"grade": "甲等", "score": 100, "player_hp": 188}}
                raise AssertionError(path)

            monitor = WorldBossMonitor(
                actor,
                "main",
                logger=logging.getLogger("world-boss-test"),
                transport=FakeTransport(42),
                post_json=post_json,
                sleep=AsyncMock(),
                monotonic=lambda: 100.0,
                finish_grace_seconds=0,
            )
            entry = extract_world_boss_entry(DummyMessage())
            with patch(
                "world_boss_features.request_webview_init_data",
                new=AsyncMock(return_value="signed_init_data"),
            ):
                outcome = await monitor._participate(entry)

            self.assertEqual(outcome["grade"], "甲等")
            self.assertEqual(outcome["hit_count"], 1)
            self.assertEqual(WORLD_BOSS_HOLD_MS, 1200)
            self.assertEqual(outcome["damage_yi_total"], 123)
            self.assertEqual(outcome["damage_yi_average"], 123)
            self.assertEqual(outcome["damage_yi_hit_count"], 1)
            self.assertEqual(outcome["damage_yi_hits"], [123])
            self.assertEqual(outcome["perfect_count"], 1)
            self.assertEqual(outcome["local_perfect_count"], 1)
            diagnostics = outcome["diagnostics"]
            self.assertEqual(diagnostics["version"], 1)
            self.assertEqual(diagnostics["player"]["attackBonus"], 1.08)
            self.assertEqual(diagnostics["hits"][0]["server_status"], "accepted")
            self.assertEqual(diagnostics["hits"][0]["account_offset_ms"], 0)
            serialized = str(diagnostics)
            self.assertNotIn("signed_init_data", serialized)
            self.assertNotIn("session_fixture", serialized)
            self.assertEqual(
                [path.rsplit("/", 1)[-1] for path, _ in calls],
                ["start", "begin", "hit", "finish"],
            )
            self.assertEqual(calls[0][1]["playerId"], 42)
            self.assertEqual(calls[2][1]["holdMs"], WORLD_BOSS_HOLD_MS)
            proof = calls[3][1]["bossProof"]
            self.assertEqual(proof["mode"], "qyz_focus_burst_v2")
            self.assertEqual(proof["stance"], WORLD_BOSS_STANCE)
            self.assertEqual(proof["playerHp"], 188)
            self.assertEqual(proof["clientStats"]["perfects"], 1)
            self.assertEqual(
                proof["actions"],
                [{"t": 0, "holdMs": WORLD_BOSS_HOLD_MS, "stance": WORLD_BOSS_STANCE}],
            )

        asyncio.run(run())

    def test_outcome_summary_includes_total_average_and_per_hit_damage(self):
        summary = WorldBossMonitor._outcome_summary({
            "grade": "甲等",
            "score": 100,
            "player_hp": 84,
            "hit_count": 3,
            "perfect_count": 3,
            "failed_hit_count": 0,
            "window_count": 3,
            "damage_yi_total": 150_000_000,
            "damage_yi_average": 75_000_000,
            "damage_yi_hit_count": 2,
            "damage_yi_hits": [100_000_000, 50_000_000, 0],
        })

        self.assertIn("甲等 100分；命中 3/3，完美 3，余血 84", summary)
        self.assertIn("伤害合计 1.50亿亿", summary)
        self.assertIn("有效 2/3", summary)
        self.assertIn("均击 7500.00万亿", summary)
        self.assertIn("逐击 [1=1.00亿亿, 2=5000.00万亿, 3=0亿]", summary)

    def test_realtime_hit_uses_actual_elapsed_time_and_account_stagger(self):
        async def run():
            clock = [100.0]
            calls = []

            async def sleep(seconds):
                clock[0] += seconds

            async def post_json(origin, path, payload, timeout):
                calls.append((path, dict(payload)))
                return {"hit": {"damageYi": 9}}

            monitor = WorldBossMonitor(
                FakeActor(),
                "main",
                post_json=post_json,
                sleep=sleep,
                monotonic=lambda: clock[0],
            )
            result = await monitor._hit_window(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                {
                    "id": "w1",
                    "centerMs": 1000,
                    "hitMs": 460,
                    "perfectMs": 150,
                },
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["perfect"])
            elapsed = calls[0][1]["elapsedMs"]
            self.assertGreaterEqual(elapsed, 855)
            self.assertLessEqual(elapsed, 865)
            self.assertEqual(result["action"]["t"], elapsed)

        asyncio.run(run())

    def test_rejected_realtime_hit_records_server_timing_and_details(self):
        async def run():
            clock = [100.0]

            async def sleep(seconds):
                clock[0] += seconds

            async def post_json(origin, path, payload, timeout):
                clock[0] += 0.48
                error = MiniAppBeastError("boss_hit_outside_window", 409)
                error.details = {
                    "error": "boss_hit_outside_window",
                    "serverElapsedMs": 1520,
                    "sessionToken": "must_not_persist",
                }
                raise error

            monitor = WorldBossMonitor(
                FakeActor(),
                "xiaohao",
                post_json=post_json,
                sleep=sleep,
                monotonic=lambda: clock[0],
            )
            result = await monitor._hit_window(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                "challenge_fixture",
                100.0,
                {
                    "id": "w14",
                    "centerMs": 1000,
                    "hitMs": 460,
                    "perfectMs": 150,
                },
                14,
            )

            self.assertFalse(result["ok"])
            self.assertTrue(result["perfect"])
            self.assertFalse(result["accepted_perfect"])
            diagnostic = result["diagnostic"]
            self.assertEqual(diagnostic["sequence"], 14)
            self.assertEqual(diagnostic["account_offset_ms"], -70)
            self.assertEqual(diagnostic["actual_elapsed_ms"], 930)
            self.assertEqual(diagnostic["signed_delta_ms"], -70)
            self.assertEqual(diagnostic["request"]["total_duration_ms"], 480)
            self.assertEqual(diagnostic["http_status"], 409)
            self.assertEqual(
                diagnostic["server_details"]["serverElapsedMs"],
                1520,
            )
            self.assertNotIn("sessionToken", str(diagnostic))

        asyncio.run(run())

    def test_failed_report_is_local_perfect_but_not_confirmed_perfect(self):
        async def run():
            calls = []

            async def post_json(origin, path, payload, timeout):
                calls.append((path, payload))
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/finish"):
                    return {
                        "result": {
                            "grade": "甲等",
                            "score": 100,
                            "player_hp": 84,
                        }
                    }
                raise AssertionError(path)

            monitor = WorldBossMonitor(
                FakeActor(),
                "xiaohao",
                post_json=post_json,
                sleep=AsyncMock(),
                monotonic=lambda: 100.0,
                finish_grace_seconds=0,
            )
            accepted = {
                "action": {"t": 1000, "holdMs": 1200, "stance": "强攻"},
                "ok": True,
                "matched": True,
                "perfect": True,
                "accepted_perfect": True,
                "damage": 10,
                "diagnostic": {"sequence": 1},
            }
            rejected = {
                "action": {"t": 2000, "holdMs": 1200, "stance": "强攻"},
                "ok": False,
                "matched": True,
                "perfect": True,
                "accepted_perfect": False,
                "damage": 0,
                "error": "boss_hit_outside_window",
                "diagnostic": {
                    "sequence": 2,
                    "error": "boss_hit_outside_window",
                },
            }
            monitor._hit_window = AsyncMock(side_effect=[accepted, rejected])
            outcome = await monitor._fight(
                extract_world_boss_entry(DummyMessage()),
                "signed_init_data",
                "session_fixture",
                {
                    "player": {"maxHp": 100},
                    "boss": {"phase": 1},
                    "challenge": {
                        "challengeId": "challenge_fixture",
                        "windows": [
                            {"id": "w1", "centerMs": 1000, "hitMs": 460, "perfectMs": 150},
                            {"id": "w2", "centerMs": 2000, "hitMs": 460, "perfectMs": 150},
                        ],
                    },
                },
            )

            self.assertEqual(outcome["hit_count"], 1)
            self.assertEqual(outcome["perfect_count"], 1)
            self.assertEqual(outcome["local_matched_count"], 2)
            self.assertEqual(outcome["local_perfect_count"], 2)
            finish_payload = next(
                payload for path, payload in calls if path.endswith("/finish")
            )
            self.assertEqual(
                finish_payload["bossProof"]["clientStats"]["perfects"],
                2,
            )
            summary = monitor._outcome_summary(outcome)
            self.assertIn("完美 1", summary)
            self.assertIn("本地判定 2，服务端确认 1", summary)

        asyncio.run(run())

    def test_default_requests_reuse_one_persistent_client_per_origin(self):
        async def run():
            fake_client = SimpleNamespace(
                post=AsyncMock(return_value={"ok": True}),
                close=lambda: None,
            )
            monitor = WorldBossMonitor(FakeActor(), "main")
            with patch(
                "world_boss_features._PersistentWorldBossJsonClient",
                return_value=fake_client,
            ) as client_type:
                await monitor._request("https://asc.aiopenai.app", "/one", {})
                await monitor._request("https://asc.aiopenai.app", "/two", {})

            client_type.assert_called_once_with("https://asc.aiopenai.app")
            self.assertEqual(fake_client.post.await_count, 2)

        asyncio.run(run())

    def test_identity_fallback_uses_personal_event_choice(self):
        async def run():
            actor = FakeActor(avatars=["缘生子"])
            starts = []

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/start"):
                    starts.append(payload["playerId"])
                    if len(starts) == 1:
                        return {
                            "needsIdentitySelection": True,
                            "identityChoices": [
                                {"playerId": 7, "source": "avatar", "displayName": "缘生子"},
                                {"playerId": 42, "source": "personal", "displayName": "Waaiging"},
                            ],
                        }
                    return {
                        "sessionToken": "session_fixture",
                        "boss": {"actionsUsed": 0, "actionsRemaining": 1},
                        "player": {"maxHp": 100},
                        "challenge": {
                            "challengeId": "challenge_fixture",
                            "windows": [
                                {"id": "w1", "centerMs": 0, "hitMs": 1, "perfectMs": 1}
                            ],
                        },
                    }
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/hit"):
                    return {"hit": {"damageYi": 1}}
                if path.endswith("/finish"):
                    return {"result": {"grade": "甲等", "score": 100, "player_hp": 100}}
                raise AssertionError(path)

            monitor = WorldBossMonitor(
                actor,
                "sub",
                transport=FakeTransport(error=MiniAppBeastError("hash_mismatch")),
                post_json=post_json,
                sleep=AsyncMock(),
                monotonic=lambda: 100.0,
                finish_grace_seconds=0,
            )
            entry = extract_world_boss_entry(DummyMessage())
            with patch(
                "world_boss_features.request_webview_init_data",
                new=AsyncMock(return_value="signed_init_data"),
            ):
                await monitor._participate(entry)
            self.assertEqual(starts, ["", 42])

        asyncio.run(run())

    def test_selected_avatar_uses_its_fixed_player_id(self):
        async def run():
            actor = FakeActor(avatars=["无咎子"])
            monitor = WorldBossMonitor(
                actor,
                "main",
                transport=FakeTransport(player_ids={"主魂": 42, "无咎子": 88}),
            )
            entry = extract_world_boss_entry(DummyMessage())
            payload = {"challenge": {"challengeId": "challenge_fixture"}}
            monitor._wait_for_challenge = AsyncMock(return_value=("session_fixture", payload))
            monitor._fight = AsyncMock(return_value={"grade": "甲等"})

            outcome = await monitor._participate(
                entry,
                identity="无咎子",
                init_data="signed_init_data",
            )

            self.assertEqual(outcome["grade"], "甲等")
            monitor._wait_for_challenge.assert_awaited_once_with(
                entry,
                "signed_init_data",
                88,
                identity="无咎子",
            )

        asyncio.run(run())

    def test_dashboard_disabled_account_does_not_queue_event(self):
        async def run():
            actor = FakeActor(avatars=["缘生子"])
            monitor = WorldBossMonitor(actor, "main")
            with (
                patch("world_boss_features.is_game_bot_sender", return_value=True),
                patch("world_boss_features.world_boss_identities_for_account", return_value=[]),
            ):
                queued = await monitor.process_message(DummyMessage())
            self.assertFalse(queued)
            self.assertFalse(monitor._tasks)
            self.assertEqual(actor.state.get("world_boss_events"), None)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
