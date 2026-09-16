import asyncio
import atexit
import logging
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniapp_beast import MiniAppBeastError
from world_boss_features import (
    WORLD_BOSS_DRIFT_MAX_MS,
    WORLD_BOSS_DRIFT_MIN_MS,
    WORLD_BOSS_HOLD_MAX_MS,
    WORLD_BOSS_HOLD_MIN_MS,
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
        self._directory = tempfile.TemporaryDirectory()
        atexit.register(self._directory.cleanup)
        self.state_file = str(Path(self._directory.name) / "state_fixture.json")
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
            {"main": -20, "sub": -15, "xiaohao": -10, "waaiging": -5},
        )

    def test_tight_attack_width_uses_safe_arrival_offsets(self):
        window = {"perfectMs": 58}

        offsets = {
            account: WorldBossMonitor(FakeActor(), account)._hit_offset_ms(window)
            for account in ("main", "sub", "xiaohao", "waaiging")
        }

        self.assertEqual(
            offsets,
            {"main": -20, "sub": -15, "xiaohao": -10, "waaiging": -5},
        )

    def test_attack_format_uses_impact_and_width(self):
        windows = WorldBossMonitor._windows(
            {
                "attacks": [
                    {"id": "a_1", "impactMs": 2100, "width": 58},
                    {"id": "a_2", "impactMs": 7813, "width": 66},
                ]
            }
        )

        self.assertEqual(
            windows,
            [
                {
                    "id": "a_1",
                    "centerMs": 2100,
                    "impactMs": 2100,
                    "dangerStartMs": 0,
                    "dangerEndMs": 0,
                    "hitMs": 460,
                    "perfectMs": 58,
                },
                {
                    "id": "a_2",
                    "centerMs": 7813,
                    "impactMs": 7813,
                    "dangerStartMs": 0,
                    "dangerEndMs": 0,
                    "hitMs": 460,
                    "perfectMs": 66,
                },
            ],
        )

    def test_attack_format_uses_rotated_danger_window(self):
        windows = WorldBossMonitor._windows(
            {
                "attacks": [
                    {
                        "id": "a_1",
                        "impactMs": 2100,
                        "dangerStart": -27.0,
                        "dangerEnd": 31.0,
                    }
                ]
            }
        )

        self.assertEqual(
            windows,
            [
                {
                    "id": "a_1",
                    "centerMs": 2102,
                    "impactMs": 2100,
                    "dangerStartMs": -27,
                    "dangerEndMs": 31,
                    "hitMs": 58,
                    "perfectMs": 58,
                }
            ],
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
                                {
                                    "id": "w1",
                                    "centerMs": 2000,
                                    "hitMs": 460,
                                    "perfectMs": 150,
                                }
                            ],
                        },
                    }
                if path.endswith("/begin"):
                    return {"startsInMs": 0}
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "charge_fixture"}
                if path.endswith("/hit"):
                    return {"hit": {"damageYi": 123}}
                if path.endswith("/finish"):
                    return {"result": {"grade": "甲等", "score": 100, "player_hp": 188}}
                raise AssertionError(path)

            clock = [100.0]

            async def sleep(seconds):
                clock[0] += max(0.0, seconds)

            monitor = WorldBossMonitor(
                actor,
                "main",
                logger=logging.getLogger("world-boss-test"),
                transport=FakeTransport(42),
                post_json=post_json,
                sleep=sleep,
                monotonic=lambda: clock[0],
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
            # The server judges its own measurement of the hold, which differed
            # from ours by -714..+493ms on 2026-09-01, so what matters is that the
            # target keeps headroom inside the legal band rather than its exact
            # value. Aiming at the ceiling made 20 of 57 server-side holds illegal.
            self.assertGreater(WORLD_BOSS_HOLD_MS, WORLD_BOSS_HOLD_MIN_MS)
            self.assertLess(WORLD_BOSS_HOLD_MS, WORLD_BOSS_HOLD_MAX_MS)
            self.assertGreaterEqual(
                WORLD_BOSS_HOLD_MAX_MS - WORLD_BOSS_HOLD_MS, 200
            )
            self.assertEqual(outcome["damage_yi_total"], 123)
            self.assertEqual(outcome["damage_yi_average"], 123)
            self.assertEqual(outcome["damage_yi_hit_count"], 1)
            self.assertEqual(outcome["damage_yi_hits"], [123])
            self.assertEqual(outcome["perfect_count"], 1)
            self.assertEqual(outcome["local_perfect_count"], 1)
            diagnostics = outcome["diagnostics"]
            self.assertEqual(diagnostics["version"], 7)
            self.assertEqual(diagnostics["player"]["attackBonus"], 1.08)
            self.assertEqual(diagnostics["hits"][0]["server_status"], "accepted")
            # main sits at slot -4 and the stagger scales with the perfect window.
            self.assertEqual(diagnostics["hits"][0]["account_offset_ms"], -20)
            serialized = str(diagnostics)
            self.assertNotIn("signed_init_data", serialized)
            self.assertNotIn("session_fixture", serialized)
            self.assertEqual(
                [path.rsplit("/", 1)[-1] for path, _ in calls],
                ["start", "begin", "charge-start", "hit", "finish"],
            )
            self.assertEqual(calls[0][1]["playerId"], 42)
            self.assertEqual(calls[2][1]["windowId"], "w1")
            self.assertEqual(calls[3][1]["chargeTicket"], "charge_fixture")
            proof = calls[4][1]["bossProof"]
            self.assertEqual(proof["mode"], "qyz_focus_burst_v2")
            self.assertEqual(proof["stance"], WORLD_BOSS_STANCE)
            self.assertEqual(proof["playerHp"], 188)
            self.assertEqual(proof["clientStats"]["perfects"], 1)
            self.assertEqual(
                proof["actions"],
                [{"t": 1980, "holdMs": WORLD_BOSS_HOLD_MS, "stance": WORLD_BOSS_STANCE}],
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
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "charge_fixture"}
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
                    "centerMs": 2000,
                    "hitMs": 460,
                    "perfectMs": 150,
                },
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["perfect"])
            self.assertEqual(
                [path.rsplit("/", 1)[-1] for path, _ in calls],
                ["charge-start", "hit"],
            )
            elapsed = calls[1][1]["elapsedMs"]
            self.assertGreaterEqual(elapsed, 1975)
            self.assertLessEqual(elapsed, 1985)
            self.assertEqual(result["action"]["t"], elapsed)
            # Charging one hold ahead of the strike reports the real hold length.
            self.assertEqual(calls[1][1]["holdMs"], WORLD_BOSS_HOLD_MS)
            self.assertEqual(result["diagnostic"]["charge"]["granted"], True)

        asyncio.run(run())

    def test_realtime_hit_compensates_begin_round_trip(self):
        async def run():
            clock = [100.0]
            calls = []

            async def sleep(seconds):
                clock[0] += seconds

            async def post_json(origin, path, payload, timeout):
                calls.append((path, dict(payload)))
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "charge_fixture"}
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
                    "centerMs": 2000,
                    "hitMs": 460,
                    "perfectMs": 150,
                },
                1,
                100,
            )

            self.assertTrue(result["ok"])
            self.assertTrue(result["perfect"])
            self.assertEqual(calls[1][1]["elapsedMs"], 1980)
            self.assertEqual(result["diagnostic"]["sent_elapsed_ms"], 1880)
            self.assertEqual(result["diagnostic"]["actual_elapsed_ms"], 1980)
            self.assertEqual(result["diagnostic"]["signed_delta_ms"], -20)

        asyncio.run(run())

    def test_server_delta_calibrates_lead_once_per_battle(self):
        """The server's own deltaMs sets the extra lead, and only the first sample.

        deltaMs is unsigned, so a running sum would diverge: after the correction
        overshoots, the strike lands early, deltaMs grows again, and each further
        sample would push it earlier still.
        """
        monitor = WorldBossMonitor(FakeActor(), "main")
        self.assertEqual(monitor._drift_lead_ms(), 0)

        monitor._record_drift(160)
        self.assertEqual(monitor._drift_lead_ms(), 80)

        # Later samples are ignored for the rest of the battle.
        monitor._record_drift(400)
        monitor._record_drift(4)
        self.assertEqual(monitor._drift_lead_ms(), 80)

        # A new battle starts uncorrected: RTT is not stable across events.
        monitor._reset_drift()
        self.assertEqual(monitor._drift_lead_ms(), 0)

    def test_drift_ignores_unusable_server_samples(self):
        monitor = WorldBossMonitor(FakeActor(), "main")
        for sample in (None, "", "abc", 0, -5, 99999):
            monitor._record_drift(sample)
            self.assertEqual(monitor._drift_lead_ms(), 0)
        # A sample above the cap is clamped, not discarded: the account furthest
        # off most needs the lead.
        monitor._record_drift(2 * WORLD_BOSS_DRIFT_MAX_MS + 100)
        self.assertEqual(monitor._drift_lead_ms(), WORLD_BOSS_DRIFT_MAX_MS)
        monitor._reset_drift()
        monitor._record_drift(200)
        self.assertEqual(monitor._drift_lead_ms(), 100)
        # A wild reading is a broken measurement and is still refused.
        monitor._reset_drift()
        monitor._record_drift(99999)
        self.assertEqual(monitor._drift_lead_ms(), 0)

    def test_contextual_drift_uses_direction_and_signed_lead(self):
        early_monitor = WorldBossMonitor(FakeActor(), "main")
        early = early_monitor._record_drift(
            120,
            center_ms=1000,
            sent_elapsed_ms=860,
            request_completed_elapsed_ms=930,
            request_lead_ms=160,
        )
        self.assertEqual(early["direction"], "early")
        self.assertLess(early_monitor._drift_lead_ms(), 0)
        self.assertGreaterEqual(early_monitor._drift_lead_ms(), WORLD_BOSS_DRIFT_MIN_MS)

        late_monitor = WorldBossMonitor(FakeActor(), "main")
        late = late_monitor._record_drift(
            120,
            center_ms=1000,
            sent_elapsed_ms=1080,
            request_completed_elapsed_ms=1150,
            request_lead_ms=0,
        )
        self.assertEqual(late["direction"], "late")
        self.assertGreater(late_monitor._drift_lead_ms(), 0)

    def test_contextual_drift_skips_ambiguous_candidates(self):
        monitor = WorldBossMonitor(FakeActor(), "main")
        inference = monitor._record_drift(
            100,
            center_ms=1000,
            sent_elapsed_ms=850,
            request_completed_elapsed_ms=1150,
            request_lead_ms=0,
        )
        self.assertEqual(inference["direction"], "ambiguous")
        self.assertFalse(inference["update_applied"])
        self.assertEqual(monitor._drift_samples, 0)
        self.assertEqual(monitor._drift_lead_ms(), 0)

    def test_contextual_drift_downweights_long_http_intervals(self):
        normal = WorldBossMonitor(FakeActor(), "main")
        normal_result = normal._record_drift(
            200,
            center_ms=1000,
            sent_elapsed_ms=1150,
            request_completed_elapsed_ms=1250,
            request_lead_ms=0,
        )
        slow = WorldBossMonitor(FakeActor(), "main")
        slow_result = slow._record_drift(
            200,
            center_ms=1000,
            sent_elapsed_ms=1150,
            request_completed_elapsed_ms=2050,
            request_lead_ms=0,
        )
        self.assertEqual(normal_result["direction"], "late")
        self.assertEqual(slow_result["direction"], "late")
        self.assertLess(slow_result["gain"], normal_result["gain"])
        self.assertLess(slow._drift_lead_ms(), normal._drift_lead_ms())

    def test_contextual_drift_long_outlier_does_not_replace_short_history(self):
        monitor = WorldBossMonitor(FakeActor(), "main")
        first = monitor._record_drift(
            120,
            center_ms=1000,
            sent_elapsed_ms=1080,
            request_completed_elapsed_ms=1150,
        )
        before = monitor._drift_lead_ms()
        # The late candidate is unique, but the 900ms response interval is a
        # low-confidence observation and should not replace the normal sample in
        # the weighted median.
        second = monitor._record_drift(
            200,
            center_ms=1000,
            sent_elapsed_ms=1150,
            request_completed_elapsed_ms=2050,
        )
        self.assertTrue(first["update_applied"])
        self.assertTrue(second["update_applied"])
        self.assertLess(second["gain"], first["gain"])
        self.assertEqual(second["robust_sample_ms"], first["clamped_sample_drift_ms"])
        self.assertGreater(monitor._drift_lead_ms(), before)

    def test_server_hold_feedback_adjusts_only_the_next_charge_plan(self):
        monitor = WorldBossMonitor(FakeActor(), "main")
        self.assertEqual(monitor._planned_hold_ms(), WORLD_BOSS_HOLD_MS)

        # A server hold 200ms longer than the local press-to-release duration
        # should make the next charge shorter, while retaining legal headroom.
        sample = monitor._record_hold_skew(1200, WORLD_BOSS_HOLD_MS)
        self.assertEqual(sample, 200)
        self.assertLess(monitor._planned_hold_ms(), WORLD_BOSS_HOLD_MS)
        self.assertGreaterEqual(monitor._planned_hold_ms(), WORLD_BOSS_HOLD_MIN_MS)
        self.assertLessEqual(monitor._planned_hold_ms(), WORLD_BOSS_HOLD_MAX_MS)

        monitor._reset_hold_skew()
        self.assertEqual(monitor._planned_hold_ms(), WORLD_BOSS_HOLD_MS)

    def test_server_hold_feedback_ignores_late_reveal_boundary_samples(self):
        monitor = WorldBossMonitor(FakeActor(), "main")
        # 520ms is the emergency minimum used when a window is revealed late;
        # it should not drag the normal plan toward a misleading correction.
        self.assertIsNone(monitor._record_hold_skew(900, WORLD_BOSS_HOLD_MIN_MS))
        self.assertEqual(monitor._planned_hold_ms(), WORLD_BOSS_HOLD_MS)

    def test_calibrated_drift_leads_the_next_strike(self):
        async def run():
            clock = [100.0]
            sent = []

            async def sleep(seconds):
                clock[0] += seconds

            async def post_json(origin, path, payload, timeout):
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "charge_fixture"}
                sent.append(dict(payload))
                return {"hit": {"damageYi": 1, "deltaMs": 160}}

            monitor = WorldBossMonitor(
                FakeActor(),
                "main",
                post_json=post_json,
                sleep=sleep,
                monotonic=lambda: clock[0],
            )
            entry = extract_world_boss_entry(DummyMessage())
            window = {"id": "w1", "centerMs": 4000, "hitMs": 620, "perfectMs": 210}
            first = await monitor._hit_window(
                entry, "d", "t", "c", 100.0, dict(window), 1, 0
            )
            self.assertEqual(first["diagnostic"]["drift_lead_ms"], 0)
            self.assertEqual(monitor._drift_lead_ms(), 80)

            clock[0] = 100.0
            second = await monitor._hit_window(
                entry, "d", "t", "c", 100.0, dict(window), 2, 0
            )
            # 80ms earlier than the uncorrected strike, and reported as the arrival
            # the server is predicted to stamp.
            self.assertEqual(second["diagnostic"]["drift_lead_ms"], 80)
            self.assertEqual(
                second["diagnostic"]["sent_elapsed_ms"],
                first["diagnostic"]["sent_elapsed_ms"] - 80,
            )
            self.assertEqual(sent[-1]["elapsedMs"], sent[0]["elapsedMs"])

        asyncio.run(run())

    def test_rejected_realtime_hit_records_server_timing_and_details(self):
        async def run():
            clock = [100.0]

            async def sleep(seconds):
                clock[0] += seconds

            async def post_json(origin, path, payload, timeout):
                clock[0] += 0.48
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "charge_fixture"}
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
            self.assertEqual(diagnostic["account_offset_ms"], -10)
            self.assertEqual(diagnostic["actual_elapsed_ms"], 990)
            self.assertEqual(diagnostic["signed_delta_ms"], -10)
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
                "action": {"t": 1000, "holdMs": WORLD_BOSS_HOLD_MS, "stance": "强攻"},
                "ok": True,
                "matched": True,
                "perfect": True,
                "accepted_perfect": True,
                "damage": 10,
                "diagnostic": {"sequence": 1},
            }
            rejected = {
                "action": {"t": 2000, "holdMs": WORLD_BOSS_HOLD_MS, "stance": "强攻"},
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

    def test_default_requests_use_shared_circuit_breaker_transport(self):
        async def run():
            monitor = WorldBossMonitor(FakeActor(), "main")
            with patch(
                "world_boss_features._post_json",
                new=AsyncMock(return_value={"ok": True}),
            ) as post_json:
                await monitor._request("https://asc.aiopenai.app", "/one", {})
                await monitor._request("https://asc.aiopenai.app", "/two", {})

            self.assertEqual(post_json.await_count, 2)
            self.assertEqual(post_json.await_args_list[0].args[:2], (
                "https://asc.aiopenai.app",
                "/one",
            ))
            self.assertEqual(post_json.await_args_list[1].args[:2], (
                "https://asc.aiopenai.app",
                "/two",
            ))

    def test_world_boss_requests_bypass_open_shared_circuit(self):
        async def run():
            monitor = WorldBossMonitor(FakeActor(), "main")
            with patch(
                "world_boss_features._post_json",
                new=AsyncMock(return_value={"ok": True}),
            ) as post_json:
                await monitor._request(
                    "https://asc.aiopenai.app",
                    "/api/miniapp/xianxia-world-boss/start",
                    {},
                )

            self.assertEqual(post_json.await_count, 1)
            self.assertTrue(post_json.await_args.kwargs["time_critical"])

        asyncio.run(run())

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

    def test_boss_hp_response_marks_local_lifecycle_stop(self):
        async def run():
            calls = []
            clock = [100.0]

            async def sleep(seconds):
                clock[0] += max(0.0, float(seconds))

            async def post_json(origin, path, payload, timeout):
                calls.append(path.rsplit("/", 1)[-1])
                if path.endswith("/charge-start"):
                    return {"chargeTicket": "charge_fixture"}
                if path.endswith("/hit"):
                    return {
                        "hit": {
                            "bossHp": 0,
                            "deltaMs": 4,
                            "perfect": True,
                            "damageYi": 10,
                        }
                    }
                raise AssertionError(path)

            actor = FakeActor()
            with tempfile.TemporaryDirectory() as directory:
                actor.state_file = f"{directory}/state.json"
                monitor = WorldBossMonitor(
                    actor,
                    "main",
                    post_json=post_json,
                    sleep=sleep,
                    monotonic=lambda: clock[0],
                )
                entry = extract_world_boss_entry(DummyMessage())
                monitor._prepare_boss_lifecycle(entry)
                result = await monitor._hit_window(
                    entry,
                    "signed_init_data",
                    "session_fixture",
                    "challenge_fixture",
                    100.0,
                    {
                        "id": "w1",
                        "centerMs": 3000,
                        "hitMs": 460,
                        "perfectMs": 150,
                    },
                    1,
                )
                self.assertTrue(result["ok"])
                self.assertTrue(monitor._boss_defeated.is_set())
                self.assertEqual(calls, ["charge-start", "hit"])

                skipped = await monitor._hit_window(
                    entry,
                    "signed_init_data",
                    "session_fixture",
                    "challenge_fixture",
                    100.0,
                    {
                        "id": "w2",
                        "centerMs": 6000,
                        "hitMs": 460,
                        "perfectMs": 150,
                    },
                    2,
                )
                self.assertTrue(skipped["skipped"])
                self.assertEqual(skipped["error"], "boss_defeated_local")
                self.assertEqual(calls, ["charge-start", "hit"])

        asyncio.run(run())

    def test_boss_defeat_marker_is_shared_between_monitors(self):
        entry = extract_world_boss_entry(DummyMessage())
        with tempfile.TemporaryDirectory() as directory:
            actor1 = FakeActor()
            actor2 = FakeActor()
            actor1.state_file = f"{directory}/state_main.json"
            actor2.state_file = f"{directory}/state_sub.json"
            first = WorldBossMonitor(actor1, "main")
            second = WorldBossMonitor(actor2, "sub")
            first._prepare_boss_lifecycle(entry)
            second._prepare_boss_lifecycle(entry)
            self.assertFalse(second._boss_stop_requested())
            first._mark_boss_defeated("boss_defeated", 0)
            self.assertTrue(second._boss_stop_requested())
            self.assertEqual(second._boss_defeat_reason, "boss_defeated")

    def test_shared_defeat_before_first_window_is_event_closed_not_invalid(self):
        async def run():
            entry = extract_world_boss_entry(DummyMessage())
            with tempfile.TemporaryDirectory() as directory:
                winner_actor = FakeActor()
                loser_actor = FakeActor()
                winner_actor.state_file = f"{directory}/state_main.json"
                loser_actor.state_file = f"{directory}/state_sub.json"
                winner = WorldBossMonitor(winner_actor, "main")
                calls = []

                async def post_json(origin, path, payload, timeout):
                    calls.append(path)
                    raise AssertionError("a stopped battle must not call the API")

                loser = WorldBossMonitor(
                    loser_actor,
                    "sub",
                    post_json=post_json,
                    sleep=AsyncMock(),
                    monotonic=lambda: 100.0,
                    finish_grace_seconds=0,
                )
                winner._prepare_boss_lifecycle(entry)
                winner._mark_boss_defeated("boss_defeated", 0)

                with self.assertRaises(MiniAppBeastError) as raised:
                    await loser._fight(
                        entry,
                        "signed_init_data",
                        "session_fixture",
                        {
                            "player": {"maxHp": 100},
                            "boss": {"phase": 1},
                            "challenge": {"challengeId": "challenge_fixture"},
                        },
                    )
                self.assertEqual(raised.exception.code, "boss_event_closed")
                self.assertNotIn("boss_windows_invalid", str(raised.exception))
                self.assertEqual(calls, [])

        asyncio.run(run())

    def test_concurrent_window_wait_wakes_when_sibling_marks_defeat(self):
        async def run():
            entry = extract_world_boss_entry(DummyMessage())
            with tempfile.TemporaryDirectory() as directory:
                winner_actor = FakeActor()
                waiting_actor = FakeActor()
                winner_actor.state_file = f"{directory}/state_main.json"
                waiting_actor.state_file = f"{directory}/state_sub.json"
                winner = WorldBossMonitor(winner_actor, "main")
                waiting = WorldBossMonitor(waiting_actor, "sub")
                winner._prepare_boss_lifecycle(entry)
                waiting._prepare_boss_lifecycle(entry)

                async def mark_after_scheduler_turn():
                    await asyncio.sleep(0.05)
                    winner._mark_boss_defeated("boss_defeated", 0)

                battle_start = time.monotonic()
                waiting_task = asyncio.create_task(
                    waiting._hit_window(
                        entry,
                        "signed_init_data",
                        "session_fixture",
                        "challenge_fixture",
                        battle_start,
                        {
                            "id": "w1",
                            "centerMs": 5000,
                            "hitMs": 460,
                            "perfectMs": 150,
                        },
                        1,
                    )
                )
                marker_task = asyncio.create_task(mark_after_scheduler_turn())
                result, _ = await asyncio.gather(waiting_task, marker_task)
                self.assertTrue(result["skipped"])
                self.assertEqual(result["error"], "boss_defeated_local")
                self.assertTrue(waiting._boss_defeated.is_set())

        asyncio.run(run())

    def test_skipped_windows_do_not_require_a_proof_action(self):
        async def run():
            actor = FakeActor()
            with tempfile.TemporaryDirectory() as directory:
                actor.state_file = f"{directory}/state.json"
                monitor = WorldBossMonitor(
                    actor,
                    "main",
                    post_json=None,
                    sleep=AsyncMock(),
                    monotonic=lambda: 100.0,
                    finish_grace_seconds=0,
                )
                accepted = {
                    "action": {"t": 1000, "holdMs": WORLD_BOSS_HOLD_MS, "stance": "强攻"},
                    "ok": True,
                    "matched": True,
                    "perfect": True,
                    "accepted_perfect": True,
                    "damage": 10,
                    "diagnostic": {"sequence": 1},
                }
                skipped = monitor._skipped_hit_result(
                    {"id": "w2", "centerMs": 2000, "hitMs": 460, "perfectMs": 150},
                    2,
                )
                monitor._hit_window = AsyncMock(side_effect=[accepted, skipped])
                calls = []

                async def post_json(origin, path, payload, timeout):
                    calls.append((path, payload))
                    if path.endswith("/begin"):
                        return {"startsInMs": 0}
                    if path.endswith("/finish"):
                        return {"result": {"grade": "甲等", "score": 100, "player_hp": 100}}
                    raise AssertionError(path)

                monitor.post_json = post_json
                entry = extract_world_boss_entry(DummyMessage())
                outcome = await monitor._fight(
                    entry,
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
                self.assertEqual(outcome["failed_hit_count"], 0)
                self.assertEqual(outcome["skipped_hit_count"], 1)
                self.assertEqual(
                    next(payload for path, payload in calls if path.endswith("/finish"))["bossProof"]["actions"],
                    [accepted["action"]],
                )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
