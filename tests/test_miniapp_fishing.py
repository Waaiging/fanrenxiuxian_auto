import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import dashboard_server
import miniapp_fishing
from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import MiniAppDwellingTransport
from miniapp_fishing import (
    MiniAppFishingAutomation,
    build_fishing_proof,
    fishing_result_summary,
    fishing_start_wait,
    request_miniapp_fishing_force_retry,
)


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=dwelling_fixture"


def shop_payload(*, bait_count=0, active_chum=None, chum_remaining_today=1):
    return {
        "shop": {
            "castActive": False,
            "activeChum": active_chum,
            "ponds": [
                {
                    "key": "qingxi",
                    "name": "青溪浅滩",
                    "unlocked": True,
                    "requiredExp": 0,
                    "currentExp": 408,
                }
            ],
            "baits": [
                {
                    "key": "demon_blood",
                    "itemId": "item_fishing_bait_demon_blood",
                    "name": "妖血饵",
                    "count": bait_count,
                    "unlocked": True,
                    "cost": [{"name": "灵石", "qty": 220, "owned": 9999}],
                }
            ],
            "chums": [
                {
                    "key": "demon",
                    "name": "妖腥窝",
                    "uses": 6,
                    "dailyLimit": 1,
                    "usedToday": 1 - min(1, chum_remaining_today),
                    "remainingToday": chum_remaining_today,
                    "affordable": bait_count >= 2,
                    "cost": [{"name": "妖血饵", "qty": 2, "owned": bait_count}],
                }
            ],
        }
    }


class MiniAppFishingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.global_patch = patch.object(
            miniapp_fishing,
            "MINIAPP_FISHING_GLOBAL_FILE",
            Path(self.tempdir.name) / "miniapp_fishing_global.json",
        )
        self.global_patch.start()

    def tearDown(self):
        self.global_patch.stop()
        self.tempdir.cleanup()

    def test_proof_keeps_multiple_challenges_stable_and_scores_full_marks(self):
        for seed, power, low, high, minimum in [
            ("preview", 2.4, 41, 67, 4200),
            ("fish-a", 1.7, 38, 64, 5200),
            ("鱼讯甲", 3.2, 46, 72, 6000),
        ]:
            built = build_fishing_proof(
                {
                    "challengeId": seed,
                    "fishSeed": seed,
                    "fishPower": power,
                    "targetLow": low,
                    "targetHigh": high,
                    "minDurationMs": minimum,
                    "maxDurationMs": 70000,
                }
            )
            self.assertEqual(built["predicted_score"], 100)
            self.assertEqual(built["stability"], 1.0)
            self.assertEqual(built["danger_ms"], 0)
            self.assertEqual(built["slack_ms"], 0)
            self.assertGreaterEqual(built["proof"]["durationMs"], minimum)
            self.assertLess(built["proof"]["durationMs"], 20000)
            self.assertGreater(len(built["proof"]["events"]), 1)
            self.assertLess(len(built["proof"]["events"]), 40)

    def test_configured_start_time_waits_only_until_today_target(self):
        wait, target = fishing_start_wait("06:30", datetime(2026, 8, 1, 6, 0, 0))
        self.assertEqual(wait, 1800)
        self.assertEqual(target, "2026-08-01 06:30:00")
        self.assertEqual(
            fishing_start_wait("06:30", datetime(2026, 8, 1, 6, 31, 0))[0],
            0,
        )
        self.assertEqual(fishing_start_wait("", datetime(2026, 8, 1, 6, 0, 0)), (0, ""))

    def test_run_loop_does_not_drive_fishing_before_start_time(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}
                self.is_running = True

            def save_state(self):
                pass

        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "main",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        worker.settings = lambda: {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "auto",
            "start_time": "23:59",
        }
        worker._drive_once = AsyncMock(return_value=3)

        async def stop_after_one_cycle(_seconds):
            actor.is_running = False

        with patch(
            "miniapp_fishing.fishing_start_wait",
            return_value=(600, "2026-08-01 23:59:00"),
        ), patch(
            "miniapp_fishing.miniapp_circuit_preflight",
            side_effect=AssertionError("waiting-start state must not probe upstream"),
        ), patch(
            "miniapp_fishing.asyncio.sleep",
            new=AsyncMock(side_effect=stop_after_one_cycle),
        ):
            asyncio.run(worker.run_loop())

        worker._drive_once.assert_not_awaited()
        self.assertEqual(actor.state["miniapp_fishing_status"], "waiting_start")
        self.assertEqual(
            actor.state["miniapp_fishing_next_run_time"],
            "2026-08-01 23:59:00",
        )

    def test_disabled_fishing_does_not_report_upstream_outage(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}
                self.is_running = True

            def save_state(self):
                pass

        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(origin="https://asc.aiopenai.app"),
            "main",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        worker.settings = lambda: {
            "enabled": False,
            "participants": ["main|主魂"],
            "rod_owner": "auto",
            "start_time": "",
        }
        worker._drive_once = AsyncMock(return_value=3)

        async def stop_after_one_cycle(_wait, _settings):
            actor.is_running = False

        worker._sleep_until_next_cycle = AsyncMock(side_effect=stop_after_one_cycle)
        with patch(
            "miniapp_fishing.miniapp_circuit_preflight",
            side_effect=AssertionError("disabled fishing must not probe upstream"),
        ):
            asyncio.run(worker.run_loop())

        worker._drive_once.assert_not_awaited()
        self.assertEqual(actor.state["miniapp_fishing_status"], "paused")
        self.assertEqual(actor.state["miniapp_fishing_last_error"], "")

    def test_legacy_lock_file_does_not_block_os_managed_state_lock(self):
        lock_path = miniapp_fishing.MINIAPP_FISHING_GLOBAL_FILE.with_name(
            "miniapp_fishing_global.json.lock"
        )
        lock_path.write_text("999999999", encoding="ascii")

        runtime = miniapp_fishing._update_global_state(
            lambda data: data.update(status="ready")
        )

        self.assertEqual(runtime["status"], "ready")
        self.assertTrue(lock_path.exists())

    def test_global_lock_timeout_keeps_scheduler_alive(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}
                self.is_running = True

            def save_state(self):
                pass

        actor = Actor()
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        worker = MiniAppFishingAutomation(actor, SimpleNamespace(), "main", logger)
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "auto",
            "rod": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
            "start_time": "",
        }
        worker.settings = lambda: settings
        worker._clear_irrelevant_local_statuses = Mock()
        worker._apply_force_retry_request = Mock(return_value={})
        worker._drive_once = AsyncMock(
            side_effect=miniapp_fishing.MiniAppFishingGlobalStateBusy()
        )

        async def stop_after_retry(_wait, _settings, **_kwargs):
            actor.is_running = False

        worker._sleep_until_next_cycle = AsyncMock(side_effect=stop_after_retry)
        asyncio.run(worker.run_loop())

        self.assertEqual(actor.state["miniapp_fishing_status"], "paused_state")
        self.assertEqual(
            actor.state["miniapp_fishing_last_error"],
            "miniapp_fishing_global_lock_timeout",
        )
        worker._sleep_until_next_cycle.assert_awaited_once_with(
            miniapp_fishing.FISHING_GLOBAL_LOCK_RETRY_SECONDS[0],
            settings,
            uncapped=True,
        )
        logger.error.assert_not_called()
        logger.warning.assert_called_once()

    def test_repeated_global_lock_timeouts_escalate_instead_of_retrying_every_minute(self):
        actor = SimpleNamespace(
            state={},
            config={},
            is_running=True,
            save_state=lambda: None,
        )
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "main",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "auto",
            "rod": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
            "start_time": "",
        }
        worker.settings = lambda: settings
        worker._clear_irrelevant_local_statuses = Mock()
        worker._apply_force_retry_request = Mock(return_value={})
        worker._drive_once = AsyncMock(
            side_effect=miniapp_fishing.MiniAppFishingGlobalStateBusy()
        )
        waits = []

        async def record_wait(wait, _settings, **kwargs):
            waits.append((wait, kwargs))
            if len(waits) == 3:
                actor.is_running = False

        worker._sleep_until_next_cycle = AsyncMock(side_effect=record_wait)
        asyncio.run(worker.run_loop())

        self.assertEqual(
            waits,
            [
                (5 * 60, {"uncapped": True}),
                (15 * 60, {"uncapped": True}),
                (60 * 60, {"uncapped": True}),
            ],
        )
        self.assertEqual(worker._drive_once.await_count, 3)

    def test_unselected_xiaohao_clears_stale_status_without_driving(self):
        class Actor:
            def __init__(self):
                self.state = {
                    "miniapp_fishing_status": "waiting_resources",
                    "miniapp_fishing_last_error": "fishing_bait_unaffordable",
                    "miniapp_fishing_next_run_time": "2026-08-06 15:46:00",
                    "avatars": {
                        identity: {
                            "miniapp_fishing_status": "waiting",
                            "miniapp_fishing_last_error": "stale",
                            "miniapp_fishing_next_run_time": "2026-08-06 16:00:00",
                        }
                        for identity in miniapp_fishing.ACCOUNT_IDENTITIES["xiaohao"]
                        if identity != "主魂"
                    },
                }
                self.config = {}
                self.is_running = True

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                pass

        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "xiaohao",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        worker.settings = lambda: {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod": "auto",
            "rod_owner": "main|主魂",
            "start_time": "",
        }
        worker._drive_once = AsyncMock(return_value=3)

        async def stop_after_one_cycle(_seconds):
            actor.is_running = False

        with patch(
            "miniapp_fishing.asyncio.sleep",
            new=AsyncMock(side_effect=stop_after_one_cycle),
        ):
            asyncio.run(worker.run_loop())

        worker._drive_once.assert_not_awaited()
        local_states = [actor.state, *actor.state["avatars"].values()]
        for state in local_states:
            self.assertEqual(state["miniapp_fishing_status"], "not_selected")
            self.assertEqual(state["miniapp_fishing_last_error"], "")
            self.assertEqual(state["miniapp_fishing_next_run_time"], "")

    def test_reconcile_removes_unselected_xiaohao_scan_but_preserves_history(self):
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod": "auto",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }
        data = miniapp_fishing._global_default_state()
        data.update(
            participants=["main|主魂", "xiaohao|主魂"],
            current_key="xiaohao|主魂",
            rod_holder="xiaohao|主魂",
            rod_name="银竹钓竿",
            rod_holder_source="scan",
            configured_rod="auto",
            status="transferring",
        )
        data["completed_today"] = {
            "main|主魂": False,
            "xiaohao|主魂": True,
        }
        data["round_records"] = {
            "main|主魂": [],
            "xiaohao|主魂": [{"id": "stale-round"}],
        }
        data["summary_emitted_ids"] = {
            "xiaohao|主魂": ["stale-round"],
        }
        data["scans"] = {
            "main|主魂": {"has_rod": False},
            "xiaohao|主魂": {
                "has_rod": True,
                "rod_name": "银竹钓竿",
            },
        }
        data["transfer"] = {
            "id": "stale-transfer",
            "status": "listed",
            "from": "xiaohao|主魂",
            "to": "main|主魂",
        }

        miniapp_fishing._reconcile_global_state(data, settings)

        self.assertEqual(data["participants"], ["main|主魂"])
        self.assertEqual(data["current_key"], "main|主魂")
        self.assertEqual(data["rod_holder"], "")
        self.assertEqual(data["rod_name"], "")
        self.assertNotIn("xiaohao|主魂", data["scans"])
        self.assertTrue(data["completed_today"]["xiaohao|主魂"])
        self.assertEqual(data["round_records"]["xiaohao|主魂"], [{"id": "stale-round"}])
        self.assertEqual(data["summary_emitted_ids"]["xiaohao|主魂"], ["stale-round"])
        self.assertEqual(data["transfer"], {})
        self.assertEqual(
            data["last_transfer"]["status"],
            "cancelled_participant_removed",
        )

    def test_reconcile_partial_settings_does_not_reopen_finished_identity(self):
        settings_one = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod": "auto",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }
        settings_full = {
            **settings_one,
            "participants": ["main|主魂", "xiaohao|主魂"],
        }
        data = miniapp_fishing._global_default_state()
        data.update(
            participants=["main|主魂", "xiaohao|主魂"],
            completed_today={
                "main|主魂": "2026-08-15 03:00:00",
                "xiaohao|主魂": "2026-08-15 03:10:00",
            },
            current_key="",
        )

        miniapp_fishing._reconcile_global_state(data, settings_one)
        self.assertIn("xiaohao|主魂", data["completed_today"])
        miniapp_fishing._reconcile_global_state(data, settings_full)

        self.assertEqual(data["completed_today"]["xiaohao|主魂"], "2026-08-15 03:10:00")
        self.assertEqual(data["current_key"], "")
        self.assertEqual(data["status"], "daily_done")

    def test_sub_dao_name_migration_preserves_completion_without_force_retry(self):
        completed_at = "2026-08-14 01:49:14"
        data = miniapp_fishing._global_default_state()
        data.update(
            participants=["sub|缘生子"],
            current_key="",
            completed_today={"sub|缘生子": completed_at},
            force_retry={},
            force_retry_request_id="manual-request-already-finished",
        )
        settings = {
            "enabled": True,
            "participants": ["sub|竹和生"],
            "rod": "auto",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        miniapp_fishing._migrate_global_participant_keys(data)
        miniapp_fishing._reconcile_global_state(data, settings)

        current_identity = miniapp_fishing.canonical_automation_identity(
            "sub", "竹和生"
        )
        self.assertEqual(data["participants"], [f"sub|{current_identity}"])
        self.assertEqual(
            data["completed_today"], {f"sub|{current_identity}": completed_at}
        )
        self.assertEqual(data["current_key"], "")
        self.assertEqual(data["force_retry"], {})
        self.assertEqual(data["force_retry_request_id"], "manual-request-already-finished")
        self.assertEqual(
            miniapp_fishing.fishing_participant_label("sub|缘生子"),
            f"副号｜{current_identity}",
        )

    def test_reconcile_changed_fishing_options_preserves_completion_without_force_retry(self):
        completed_at = miniapp_fishing._now_text()
        data = miniapp_fishing._global_default_state()
        data.update(
            participants=["main|主魂"],
            current_key="",
            configured_rod="银竹钓竿",
            completed_today={"main|主魂": completed_at},
            force_retry={},
            force_retry_request_id="last-manual-request",
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod": "金雷竹钓竿",
            "rod_owner": "auto",
            "pond": "hantan",
            "bait": "spirit_worm",
            "chum": "grass",
        }

        miniapp_fishing._reconcile_global_state(data, settings)

        self.assertEqual(data["completed_today"], {"main|主魂": completed_at})
        self.assertEqual(data["current_key"], "")
        self.assertEqual(data["status"], "daily_done")
        self.assertEqual(data["force_retry"], {})
        self.assertEqual(data["force_retry_request_id"], "last-manual-request")

    def test_saving_automation_settings_does_not_request_fishing_force_retry(self):
        with patch(
            "dashboard_server.save_automation_settings",
            return_value={"updated_by": "tester"},
        ), patch(
            "dashboard_server.request_miniapp_fishing_force_retry",
        ) as force_retry:
            result = asyncio.run(dashboard_server.automation_settings_control(
                {
                    "world_boss_participants": [],
                    "mulan_support_mode": "护阵",
                    "miniapp_fishing": {"enabled": True},
                    "tianxing": {"meditation_mode": "deep"},
                },
                username="tester",
            ))

        self.assertTrue(result["success"])
        force_retry.assert_not_called()

    def test_result_summary_contains_server_score_and_fish(self):
        summary = fishing_result_summary(
            {
                "result": {
                    "grade": "甲等",
                    "score": 100,
                    "quality_bonus": 0.32,
                    "details": {"stability": 1},
                }
            },
            {
                "result": {
                    "ready": True,
                    "caught": True,
                    "fish": {"name": "银须灵鲢", "weight": 1.23},
                    "rarityLabel": "灵鱼",
                    "expGain": 4,
                    "bonusLoot": [{"name": "灵石", "qty": 8}],
                }
            },
        )
        self.assertIn("甲等 100分", summary)
        self.assertIn("稳定 100%", summary)
        self.assertIn("银须灵鲢", summary)
        self.assertIn("灵石x8", summary)

    def test_pond_matched_bait_overrides_incompatible_global_preference(self):
        shop = {
            "baits": [
                {"key": "plain", "unlocked": True},
                {"key": "spirit_worm", "unlocked": True},
                {"key": "demon_blood", "unlocked": True},
            ]
        }

        self.assertEqual(
            miniapp_fishing.fishing_bait_key_for_pond(
                shop,
                "qingxi",
                "demon_blood",
            ),
            ("plain", "pond_match"),
        )
        self.assertEqual(
            miniapp_fishing.fishing_bait_key_for_pond(
                shop,
                "hantan",
                "demon_blood",
            ),
            ("spirit_worm", "pond_match"),
        )
        self.assertEqual(
            miniapp_fishing.fishing_bait_key_for_pond(
                shop,
                "luanxing",
                "demon_blood",
            ),
            ("demon_blood", "configured"),
        )

    def test_pond_matched_bait_falls_back_to_configured_when_recommendation_locked(self):
        shop = {
            "baits": [
                {"key": "plain", "unlocked": False},
                {"key": "demon_blood", "unlocked": True},
            ]
        }

        self.assertEqual(
            miniapp_fishing.fishing_bait_key_for_pond(
                shop,
                "qingxi",
                "demon_blood",
            ),
            ("demon_blood", "configured"),
        )

    def test_lobby_buys_ten_selected_baits_before_casting(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {"miniapp_beast": {"fishing_bait_purchase_quantity": 1}}

            def save_state(self):
                pass

        transport = SimpleNamespace(
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_lobby",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {
                                "itemId": "item_fishing_rod_basic",
                                "name": "青竹钓竿",
                            },
                        }
                    },
                )
            ),
            fishing_shop=AsyncMock(return_value=shop_payload(bait_count=0)),
            fishing_buy_bait=AsyncMock(return_value=shop_payload(bait_count=10)),
            fishing_next_cast=AsyncMock(return_value=("fish_cast", {"token": "fish_cast"})),
            fishing_start=AsyncMock(
                return_value=(
                    "fish_cast",
                    {
                        "session": {
                            "phase": "waiting",
                            "serverNow": 1_000,
                            "biteAt": 31_000,
                        }
                    },
                )
            ),
        )
        worker = MiniAppFishingAutomation(Actor(), transport, "main", SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        ))
        wait = asyncio.run(
            worker.run_cycle(
                {
                    "enabled": True,
                    "pond": "qingxi",
                    "bait": "demon_blood",
                    "chum": "none",
                }
            )
        )
        self.assertEqual(wait, 31)
        transport.fishing_buy_bait.assert_awaited_once_with(
            "主魂",
            "fish_lobby",
            "demon_blood",
            10,
            [{"name": "灵石", "qty": 220, "owned": 9999}],
            log_operation=False,
        )

    def test_locked_configured_pond_falls_back_to_unlocked_pond(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}

            def save_state(self):
                pass

        shop = shop_payload(bait_count=1)
        shop["shop"]["ponds"] = [
            {
                "key": "hantan",
                "name": "灵眼寒潭",
                "unlocked": False,
                "requiredExp": 1000,
                "currentExp": 0,
            },
            {
                "key": "qingxi",
                "name": "青溪浅滩",
                "unlocked": True,
                "requiredExp": 0,
                "currentExp": 408,
            },
        ]
        transport = SimpleNamespace(
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_lobby",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {"itemId": "item_fishing_rod_basic", "name": "青竹钓竿"},
                        }
                    },
                )
            ),
            fishing_shop=AsyncMock(return_value=shop),
            fishing_next_cast=AsyncMock(return_value=("fish_cast", {"token": "fish_cast"})),
            fishing_start=AsyncMock(
                return_value=(
                    "fish_cast",
                    {"session": {"phase": "waiting", "serverNow": 1000, "biteAt": 31000}},
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            Actor(),
            transport,
            "xiaohao",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )

        wait = asyncio.run(
            worker.run_cycle(
                {"enabled": True, "pond": "hantan", "bait": "demon_blood", "chum": "none"}
            )
        )

        self.assertEqual(wait, 31)
        transport.fishing_next_cast.assert_awaited_once_with(
            "主魂",
            "fish_lobby",
            "qingxi",
            "item_fishing_bait_demon_blood",
            log_operation=False,
        )
        self.assertEqual(worker.actor.state["miniapp_fishing_pond_fallback_to"], "qingxi")

    def test_auto_pond_selects_highest_unlocked_tier_for_identity(self):
        actor = SimpleNamespace(state={}, config={}, save_state=lambda: None)
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "xiaohao",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        pond, key = worker._resolve_pond(
            "问心子",
            {
                "ponds": [
                    {"key": "qingxi", "unlocked": True, "requiredExp": 0, "currentExp": 399},
                    {"key": "hantan", "unlocked": True, "requiredExp": 399, "currentExp": 399},
                    {"key": "luanxing", "unlocked": False, "requiredExp": 2400, "currentExp": 399},
                ]
            },
            "auto",
        )
        self.assertEqual(key, "hantan")
        self.assertEqual(pond["key"], "hantan")

    def test_unaffordable_configured_bait_falls_back_to_most_available_bait(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}

            def save_state(self):
                pass

        initial = shop_payload(bait_count=0)
        initial["shop"]["baits"] = [
            {
                "key": "plain",
                "itemId": "item_fishing_bait_plain",
                "name": "凡饵",
                "count": 0,
                "unlocked": True,
                "cost": [{"name": "灵石", "qty": 12, "owned": 125}],
            },
            {
                "key": "spirit_worm",
                "itemId": "item_fishing_bait_spirit_worm",
                "name": "灵虫饵",
                "count": 0,
                "unlocked": True,
                "cost": [
                    {"name": "灵石", "qty": 90, "owned": 125},
                    {"name": "凝血草", "qty": 2, "owned": 123},
                ],
            },
            {
                "key": "demon_blood",
                "itemId": "item_fishing_bait_demon_blood",
                "name": "妖血饵",
                "count": 0,
                "unlocked": True,
                "cost": [
                    {"name": "灵石", "qty": 220, "owned": 125},
                    {"name": "一阶妖丹", "qty": 1, "owned": 187},
                ],
            },
        ]
        purchased = shop_payload(bait_count=0)
        purchased["shop"]["baits"] = [dict(item) for item in initial["shop"]["baits"]]
        purchased["shop"]["baits"][0]["count"] = 10
        transport = SimpleNamespace(
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_lobby",
                    {"session": {"phase": "lobby", "rod": {"name": "银竹钓竿"}}},
                )
            ),
            fishing_shop=AsyncMock(return_value=initial),
            fishing_buy_bait=AsyncMock(return_value=purchased),
            fishing_next_cast=AsyncMock(return_value=("fish_cast", {"token": "fish_cast"})),
            fishing_start=AsyncMock(
                return_value=(
                    "fish_cast",
                    {"session": {"phase": "waiting", "serverNow": 1000, "biteAt": 31000}},
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            Actor(),
            transport,
            "xiaohao",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )

        wait = asyncio.run(
            worker.run_cycle(
                {"enabled": True, "pond": "qingxi", "bait": "demon_blood", "chum": "none"}
            )
        )

        self.assertEqual(wait, 31)
        transport.fishing_buy_bait.assert_awaited_once_with(
            "主魂",
            "fish_lobby",
            "plain",
            10,
            [{"name": "灵石", "qty": 12, "owned": 125}],
            log_operation=False,
        )
        transport.fishing_next_cast.assert_awaited_once_with(
            "主魂",
            "fish_lobby",
            "qingxi",
            "item_fishing_bait_plain",
            log_operation=False,
        )
        self.assertEqual(worker.actor.state["miniapp_fishing_bait_key"], "plain")
        self.assertEqual(
            worker.actor.state["miniapp_fishing_configured_bait_key"],
            "demon_blood",
        )
        self.assertEqual(
            worker.actor.state["miniapp_fishing_bait_selection_reason"],
            "pond_match",
        )

    def test_lobby_purchase_is_limited_by_available_cost_materials(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}

            def save_state(self):
                pass

        initial = shop_payload(bait_count=0)
        initial["shop"]["baits"][0]["cost"] = [
            {"name": "灵石", "qty": 220, "owned": 9999},
            {"name": "一阶妖丹", "qty": 1, "owned": 2},
        ]
        purchased = shop_payload(bait_count=2)
        transport = SimpleNamespace(
            fishing_entry=AsyncMock(
                return_value=("fish_lobby", {"session": {"phase": "lobby", "rod": {"name": "银竹钓竿"}}})
            ),
            fishing_shop=AsyncMock(return_value=initial),
            fishing_buy_bait=AsyncMock(return_value=purchased),
            fishing_next_cast=AsyncMock(return_value=("fish_cast", {"token": "fish_cast"})),
            fishing_start=AsyncMock(return_value=("fish_cast", {"session": {"phase": "waiting", "serverNow": 1000, "biteAt": 31000}})),
        )
        worker = MiniAppFishingAutomation(
            Actor(), transport, "xiaohao", SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        )
        asyncio.run(worker.run_cycle({"enabled": True, "pond": "qingxi", "bait": "demon_blood", "chum": "none"}))
        transport.fishing_buy_bait.assert_awaited_once()
        self.assertEqual(transport.fishing_buy_bait.await_args.args[3], 2)
        transport.fishing_next_cast.assert_awaited_once_with(
            "主魂",
            "fish_lobby",
            "qingxi",
            "item_fishing_bait_demon_blood",
            log_operation=False,
        )

    def test_exhausted_configured_chum_continues_without_chum(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}

            def save_state(self):
                pass

        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        transport = SimpleNamespace(fishing_apply_chum=AsyncMock())
        worker = MiniAppFishingAutomation(Actor(), transport, "sub", logger)
        shop = miniapp_fishing.fishing_shop(
            shop_payload(bait_count=10, chum_remaining_today=0)
        )

        result = asyncio.run(worker._ensure_chum("主魂", "fish_token", shop, "demon"))
        second = asyncio.run(worker._ensure_chum("主魂", "fish_token", shop, "demon"))

        self.assertIs(result, shop)
        self.assertIs(second, shop)
        transport.fishing_apply_chum.assert_not_awaited()
        self.assertEqual(
            worker.actor.state["miniapp_fishing_chum_fallback_reason"],
            "daily_limit",
        )
        self.assertIn(
            "继续不打窝",
            worker.actor.state["miniapp_fishing_chum_fallback_detail"],
        )
        logger.info.assert_called_once()

    def test_existing_active_chum_is_used_even_when_reapply_limit_is_zero(self):
        actor = SimpleNamespace(state={}, config={}, save_state=lambda: None)
        transport = SimpleNamespace(fishing_apply_chum=AsyncMock())
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "sub",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        shop = miniapp_fishing.fishing_shop(
            shop_payload(
                bait_count=10,
                active_chum={"key": "demon", "name": "妖腥窝", "remaining": 1},
                chum_remaining_today=0,
            )
        )

        result = asyncio.run(worker._ensure_chum("主魂", "fish_token", shop, "demon"))

        self.assertIs(result, shop)
        transport.fishing_apply_chum.assert_not_awaited()
        self.assertNotIn("miniapp_fishing_chum_fallback_reason", actor.state)

    def test_server_side_chum_limit_race_also_falls_back_without_error(self):
        actor = SimpleNamespace(state={}, config={}, save_state=lambda: None)
        refreshed_payload = shop_payload(bait_count=10, chum_remaining_today=1)
        transport = SimpleNamespace(
            fishing_shop=AsyncMock(return_value=refreshed_payload),
            fishing_apply_chum=AsyncMock(
                side_effect=MiniAppBeastError("fishing_chum_daily_limit")
            ),
        )
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "sub",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        shop = miniapp_fishing.fishing_shop(refreshed_payload)

        result = asyncio.run(worker._ensure_chum("主魂", "fish_token", shop, "demon"))

        self.assertEqual(result["activeChum"], None)
        transport.fishing_apply_chum.assert_awaited_once()
        self.assertEqual(
            actor.state["miniapp_fishing_chum_fallback_reason"],
            "daily_limit",
        )

    def test_unaffordable_configured_chum_falls_back_to_no_chum(self):
        actor = SimpleNamespace(
            state={},
            config={"notify_target": "8219248252"},
            client=SimpleNamespace(send_message=AsyncMock()),
            save_state=lambda: None,
        )
        transport = SimpleNamespace(fishing_shop=AsyncMock(), fishing_apply_chum=AsyncMock())
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "xiaohao",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        payload = shop_payload(bait_count=10, chum_remaining_today=1)
        payload["shop"]["chums"][0]["affordable"] = False
        payload["shop"]["chums"][0]["cost"][0]["owned"] = 0
        shop = miniapp_fishing.fishing_shop(payload)

        result = asyncio.run(worker._ensure_chum("主魂", "fish_token", shop, "demon"))

        self.assertIs(result, shop)
        transport.fishing_apply_chum.assert_not_awaited()
        self.assertEqual(actor.state["miniapp_fishing_chum_fallback_reason"], "unaffordable")
        actor.client.send_message.assert_awaited_once()
        self.assertEqual(actor.client.send_message.await_args.args[0], "@Waaiging")
        self.assertIn("材料不足", actor.client.send_message.await_args.args[1])

    def test_material_notice_failure_retries_at_most_hourly(self):
        notice_time = (datetime.now() - timedelta(minutes=10)).strftime(
            miniapp_fishing.TIME_FORMAT
        )
        actor = SimpleNamespace(
            state={
                "miniapp_fishing_material_notice_date": datetime.now().strftime("%Y-%m-%d"),
                "miniapp_fishing_material_notice_key": "鱼饵|妖血饵",
                "miniapp_fishing_material_notice_time": notice_time,
                "miniapp_fishing_material_notice_sent": False,
            },
            config={},
            client=SimpleNamespace(send_message=AsyncMock()),
            save_state=lambda: None,
        )
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "xiaohao",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )

        asyncio.run(
            worker._notify_material_shortage(
                "主魂",
                kind="鱼饵",
                name="妖血饵",
                shortages=["灵石 0/220"],
            )
        )

        actor.client.send_message.assert_not_awaited()

    def test_material_notice_logs_send_failure_only_once_per_day(self):
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        actor = SimpleNamespace(
            state={},
            config={},
            client=SimpleNamespace(
                send_message=AsyncMock(side_effect=RuntimeError("Too many requests"))
            ),
            save_state=lambda: None,
        )
        worker = MiniAppFishingAutomation(actor, SimpleNamespace(), "xiaohao", logger)

        async def notify():
            await worker._notify_material_shortage(
                "主魂",
                kind="鱼饵",
                name="妖血饵",
                shortages=["灵石 0/220"],
            )

        asyncio.run(notify())
        actor.state["miniapp_fishing_material_notice_time"] = (
            datetime.now() - timedelta(hours=2)
        ).strftime(miniapp_fishing.TIME_FORMAT)
        asyncio.run(notify())

        self.assertEqual(actor.client.send_message.await_count, 2)
        logger.warning.assert_called_once()

    def test_transport_uses_fishing_external_token_and_scoped_endpoints(self):
        calls = []

        async def post_json(origin, path, payload, timeout):
            calls.append((path, dict(payload)))
            if path.endswith("/xianxia-dwelling/external"):
                return {"url": "/miniapp/xianxia-fishing?startapp=fish_fixture"}
            if path.endswith("/xianxia-fishing/start"):
                return {"session": {"phase": "lobby"}}
            if path.endswith("/xianxia-fishing/shop"):
                return shop_payload(bait_count=3)
            if path.endswith("/xianxia-fishing/next"):
                return {"token": "fish_next"}
            if path.endswith("/xianxia-fishing/finish"):
                return {"result": {"grade": "甲等", "score": 100}}
            raise AssertionError(path)

        transport = MiniAppDwellingTransport(object(), ENTRY, post_json=post_json)
        transport.init_data = "signed"
        transport.start_payload = {"ok": True}
        transport.identity_player_ids = {"主魂": 100}

        async def exercise():
            token, payload = await transport.fishing_entry("主魂")
            self.assertEqual(token, "fish_fixture")
            self.assertEqual(payload["session"]["phase"], "lobby")
            await transport.fishing_shop("主魂", token)
            next_token, _ = await transport.fishing_next_cast(
                "主魂",
                token,
                "qingxi",
                "item_fishing_bait_demon_blood",
                log_operation=False,
            )
            self.assertEqual(next_token, "fish_next")
            await transport.fishing_finish(
                "主魂",
                next_token,
                {
                    "mode": "xianxiaFishingV2",
                    "challengeId": "challenge-1",
                    "durationMs": 9000,
                    "events": [],
                },
                log_operation=False,
            )

        asyncio.run(exercise())
        external = next(call for call in calls if call[0].endswith("/external"))
        self.assertEqual(external[1]["action"], "fishing")
        self.assertEqual(external[1]["playerId"], 100)
        finish = next(call for call in calls if call[0].endswith("/finish"))
        self.assertEqual(finish[1]["token"], "fish_next")
        self.assertEqual(finish[1]["fishingProof"]["challengeId"], "challenge-1")

    def test_completed_rounds_emit_one_numbered_daily_summary(self):
        class Actor:
            def __init__(self):
                self.state = {
                    "miniapp_fishing_chum": "不打窝",
                    "miniapp_fishing_pending_purchases": [
                        {"name": "妖血饵", "quantity": 10}
                    ],
                }
                self.config = {}

            def save_state(self):
                pass

        def challenge(name):
            return {
                "challengeId": name,
                "fishSeed": name,
                "fishPower": 1.7,
                "targetLow": 41,
                "targetHigh": 68,
                "minDurationMs": 5200,
                "maxDurationMs": 70000,
            }

        transport = SimpleNamespace(
            fishing_entry=AsyncMock(side_effect=[
                (
                    "fish_cast_1",
                    {
                        "session": {
                            "phase": "bite",
                            "pond": {"name": "青溪浅滩"},
                            "bait": {"name": "妖血饵"},
                        },
                        "challenge": challenge("summary-fixture-1"),
                    },
                ),
                (
                    "fish_cast_2",
                    {
                        "session": {
                            "phase": "bite",
                            "pond": {"name": "青溪浅滩"},
                            "bait": {"name": "妖血饵"},
                        },
                        "challenge": challenge("summary-fixture-2"),
                    },
                ),
            ]),
            fishing_shop=AsyncMock(return_value=shop_payload(bait_count=1)),
            fishing_finish=AsyncMock(side_effect=[
                {
                    "result": {
                        "grade": "甲等",
                        "score": 100,
                        "quality_bonus": 0.32,
                        "details": {"stability": 1},
                    }
                },
                {
                    "result": {
                        "grade": "甲等",
                        "score": 98,
                        "quality_bonus": 0.28,
                        "details": {"stability": 0.96},
                    }
                },
            ]),
            fishing_result=AsyncMock(side_effect=[
                {
                    "result": {
                        "ready": True,
                        "caught": True,
                        "fish": {"name": "银须灵鲢", "weight": 1.23},
                        "rarityLabel": "灵鱼",
                        "expGain": 4,
                        "bonusLoot": [],
                    }
                },
                {
                    "result": {
                        "ready": True,
                        "caught": True,
                        "fish": {"name": "赤尾火鲤", "weight": 3.45},
                        "rarityLabel": "妖鱼",
                        "expGain": 8,
                        "bonusLoot": [{"name": "灵石", "qty": 6}],
                    }
                },
            ]),
        )
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        worker = MiniAppFishingAutomation(Actor(), transport, "main", logger)

        with patch("miniapp_fishing.asyncio.sleep", new=AsyncMock()):
            asyncio.run(worker.run_cycle({"enabled": True}))
            asyncio.run(worker.run_cycle({"enabled": True}))

        # Individual casts stay in the state/journal; Dashboard receives one
        # aggregate entry when the caller explicitly emits the daily summary.
        self.assertEqual(logger.info.call_count, 0)
        self.assertTrue(worker._emit_daily_summary("主魂"))
        self.assertEqual(logger.info.call_count, 1)
        combined = " ".join(str(part) for part in logger.info.call_args.args)
        self.assertIn("灵溪垂钓汇总", combined)
        self.assertIn("共 2 竿", combined)
        self.assertIn("自动购饵 妖血饵x10", combined)
        self.assertIn("甲等 100分", combined)
        self.assertIn("银须灵鲢", combined)
        self.assertIn("赤尾火鲤", combined)
        self.assertIn("1. ", combined)
        self.assertIn("2. ", combined)
        self.assertIn("合计：成功 2/2 竿", combined)
        self.assertIn("总重 4.68斤", combined)
        self.assertIn("钓术经验 +12", combined)
        self.assertNotIn("灵溪垂钓开竿", combined)
        self.assertNotIn("灵溪垂钓自动收线", combined)
        self.assertEqual(worker.actor.state["miniapp_fishing_pending_purchases"], [])
        self.assertEqual(len(worker.actor.state["miniapp_fishing_round_records"]), 2)
        self.assertFalse(worker._emit_daily_summary("主魂"))
        self.assertEqual(logger.info.call_count, 1)

    def test_unready_result_blocks_next_cast_until_it_is_recorded(self):
        class Actor:
            def __init__(self):
                self.state = {"miniapp_fishing_chum": "不打窝"}
                self.config = {}

            def save_state(self):
                pass

        challenge = {
            "challengeId": "delayed-result-1",
            "fishSeed": "delayed-result-1",
            "fishPower": 1.7,
            "targetLow": 41,
            "targetHigh": 68,
            "minDurationMs": 5200,
            "maxDurationMs": 70000,
        }
        transport = SimpleNamespace(
            fishing_entry=AsyncMock(return_value=(
                "fish_delayed",
                {
                    "session": {
                        "phase": "bite",
                        "pond": {"name": "青溪浅滩"},
                        "bait": {"name": "妖血饵"},
                    },
                    "challenge": challenge,
                },
            )),
            fishing_shop=AsyncMock(return_value=shop_payload(bait_count=1)),
            fishing_finish=AsyncMock(return_value={
                "result": {
                    "grade": "甲等",
                    "score": 100,
                    "quality_bonus": 0.32,
                    "details": {"stability": 1},
                }
            }),
            fishing_result=AsyncMock(side_effect=[
                {"result": {"ready": False}},
                {
                    "result": {
                        "ready": True,
                        "caught": True,
                        "fish": {"name": "银须灵鲢", "weight": 1.23},
                        "rarityLabel": "灵鱼",
                        "expGain": 4,
                        "bonusLoot": [],
                    }
                },
            ]),
        )
        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "main",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )

        with (
            patch("miniapp_fishing.DEFAULT_RESULT_ATTEMPTS", 1),
            patch("miniapp_fishing.asyncio.sleep", new=AsyncMock()),
        ):
            self.assertEqual(asyncio.run(worker.run_cycle({"enabled": True})), 30)
            self.assertTrue(actor.state["miniapp_fishing_pending_result"])
            self.assertNotIn("miniapp_fishing_round_records", actor.state)
            self.assertEqual(asyncio.run(worker.run_cycle({"enabled": True})), 3)

        transport.fishing_entry.assert_awaited_once()
        self.assertEqual(transport.fishing_result.await_count, 2)
        self.assertEqual(len(actor.state["miniapp_fishing_round_records"]), 1)
        self.assertEqual(
            actor.state["miniapp_fishing_round_records"][0]["id"],
            "delayed-result-1",
        )
        self.assertEqual(actor.state["miniapp_fishing_pending_result"], {})

    def test_pending_result_resumes_after_worker_restart_before_opening_next_cast(self):
        class Actor:
            def __init__(self):
                self.state = {
                    "miniapp_fishing_pending_result": {
                        "id": "restart-delayed-result",
                        "finished_at": "2026-08-20 10:00:00",
                        "pond": "青溪浅滩",
                        "bait": "妖血饵",
                        "chum": "不打窝",
                        "purchases": [],
                        "finish_result": {
                            "grade": "甲等",
                            "score": 100,
                            "quality_bonus": 0.32,
                            "details": {"stability": 1},
                        },
                    }
                }
                self.config = {}

            def save_state(self):
                pass

        transport = SimpleNamespace(
            fishing_entry=AsyncMock(return_value=(
                "fish_fresh_after_restart",
                {"session": {"phase": "lobby"}},
            )),
            fishing_result=AsyncMock(return_value={
                "result": {
                    "ready": True,
                    "caught": True,
                    "fish": {"name": "赤尾火鲤", "weight": 3.21},
                    "rarityLabel": "妖鱼",
                    "expGain": 8,
                    "bonusLoot": [],
                }
            }),
            fishing_shop=AsyncMock(),
            fishing_next_cast=AsyncMock(),
        )
        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "main",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )

        self.assertEqual(asyncio.run(worker.run_cycle({"enabled": True})), 3)

        transport.fishing_entry.assert_awaited_once()
        transport.fishing_result.assert_awaited_once_with(
            "主魂",
            "fish_fresh_after_restart",
        )
        transport.fishing_shop.assert_not_awaited()
        transport.fishing_next_cast.assert_not_awaited()
        self.assertEqual(len(actor.state["miniapp_fishing_round_records"]), 1)
        self.assertEqual(actor.state["miniapp_fishing_pending_result"], {})

    def test_stale_pending_result_scope_is_discarded_after_restart(self):
        class Actor:
            def __init__(self):
                self.state = {
                    "miniapp_fishing_pending_result": {
                        "id": "stale-result-token",
                        "finished_at": "2026-08-20 10:00:00",
                        "pond": "青溪浅滩",
                        "bait": "妖血饵",
                        "chum": "不打窝",
                        "purchases": [],
                        "finish_result": {"grade": "甲等", "score": 100},
                    }
                }
                self.config = {}

            def save_state(self):
                pass

        transport = SimpleNamespace(
            fishing_entry=AsyncMock(return_value=(
                "fish_new_scope",
                {
                    "session": {
                        "phase": "waiting",
                        "biteAt": 2_000,
                        "serverNow": 1_000,
                    }
                },
            )),
            fishing_result=AsyncMock(
                side_effect=MiniAppBeastError("fishing_token_scope")
            ),
            fishing_shop=AsyncMock(return_value=shop_payload(bait_count=1)),
            fishing_next_cast=AsyncMock(),
        )
        actor = Actor()
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        worker = MiniAppFishingAutomation(actor, transport, "main", logger)

        self.assertGreater(asyncio.run(worker.run_cycle({"enabled": True})), 0)

        transport.fishing_result.assert_awaited_once_with("主魂", "fish_new_scope")
        transport.fishing_shop.assert_awaited_once_with("主魂", "fish_new_scope")
        transport.fishing_next_cast.assert_not_awaited()
        self.assertEqual(actor.state["miniapp_fishing_pending_result"], {})
        self.assertEqual(
            actor.state["miniapp_fishing_unrecorded_result_id"],
            "stale-result-token",
        )
        self.assertEqual(
            actor.state["miniapp_fishing_unrecorded_result_reason"],
            "fishing_token_scope",
        )
        logger.warning.assert_called_once()

    def test_tenth_completed_round_emits_summary_without_waiting_for_rejection(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}

            def save_state(self):
                pass

        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        worker = MiniAppFishingAutomation(Actor(), SimpleNamespace(), "main", logger)
        for index in range(10):
            worker._append_round_summary(
                "主魂",
                record_id=f"limit-round-{index}",
                pond="青溪浅滩",
                bait="妖血饵",
                chum="不打窝",
                purchases=[],
                summary=f"提竿成功：【灵鱼{index}】",
                caught=True,
                weight=1,
                exp_gain=1,
                bonus_loot=[],
            )

        self.assertTrue(worker._emit_daily_summary_at_cast_limit("主魂"))
        summary = str(logger.info.call_args.args[-1])
        self.assertIn("共 10 竿", summary)
        self.assertIn("灵鱼9", summary)
        self.assertFalse(worker._emit_daily_summary_at_cast_limit("主魂"))

    def test_round_journal_recovers_summary_after_actor_state_is_lost(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}

            def save_state(self):
                pass

        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        first_worker = MiniAppFishingAutomation(Actor(), SimpleNamespace(), "sub", logger)
        for index, fish in enumerate(("青鳞小鲫", "赤尾火鲤"), 1):
            first_worker._append_round_summary(
                "主魂",
                record_id=f"restart-round-{index}",
                pond="青溪浅滩",
                bait="妖血饵",
                chum="妖腥窝",
                purchases=[],
                summary=f"提竿成功：【{fish}】",
                caught=True,
                weight=float(index),
                exp_gain=index * 2,
                bonus_loot=[],
            )

        restarted_actor = Actor()
        restarted_worker = MiniAppFishingAutomation(
            restarted_actor,
            SimpleNamespace(),
            "sub",
            logger,
        )

        self.assertTrue(
            restarted_worker._emit_daily_summary(
                "主魂",
                daily_limit_reached=True,
            )
        )
        summary = str(logger.info.call_args.args[-1])
        self.assertIn("本次记录 2 竿", summary)
        self.assertIn("服务端今日竿数已尽", summary)
        self.assertIn("青鳞小鲫", summary)
        self.assertIn("赤尾火鲤", summary)
        self.assertEqual(
            len(restarted_actor.state["miniapp_fishing_round_records"]),
            2,
        )
        self.assertFalse(
            restarted_worker._emit_daily_summary(
                "主魂",
                daily_limit_reached=True,
            )
        )
        runtime = miniapp_fishing._load_global_state()
        self.assertEqual(runtime["version"], 3)
        self.assertEqual(
            runtime["summary_emitted_ids"]["sub|主魂"],
            ["restart-round-1", "restart-round-2"],
        )

    def test_daily_limit_is_recorded_without_error_traceback(self):
        class Actor:
            def __init__(self):
                self.state = {
                    "miniapp_fishing_summary_date": miniapp_fishing._today_text(),
                    "miniapp_fishing_summary_emitted_count": 0,
                    "miniapp_fishing_round_records": [
                        {
                            "id": "daily-limit-summary",
                            "pond": "青溪浅滩",
                            "bait": "妖血饵",
                            "chum": "妖腥窝",
                            "purchases": [],
                            "summary": "评分 甲等 100分，提竿成功：【银须灵鲢】 灵鱼 2.58斤",
                            "caught": True,
                            "weight": 2.58,
                            "exp_gain": 4,
                            "bonus_loot": [],
                        }
                    ],
                }
                self.config = {}
                self.is_running = True

            def save_state(self):
                pass

        actor = Actor()
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        worker = MiniAppFishingAutomation(actor, SimpleNamespace(), "main", logger)
        worker.settings = lambda: {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "auto",
        }
        worker._drive_once = AsyncMock(
            side_effect=MiniAppBeastError("fishing_daily_limit_reached")
        )

        async def stop_after_one_cycle(_seconds):
            actor.is_running = False

        with patch("miniapp_fishing.asyncio.sleep", new=AsyncMock(side_effect=stop_after_one_cycle)):
            asyncio.run(worker.run_loop())

        self.assertEqual(logger.info.call_count, 1)
        self.assertIn("灵溪垂钓汇总", str(logger.info.call_args.args[-1]))
        self.assertIn("银须灵鲢", str(logger.info.call_args.args[-1]))
        self.assertIn("本次记录 1 竿", str(logger.info.call_args.args[-1]))
        self.assertIn("服务端今日竿数已尽", str(logger.info.call_args.args[-1]))
        logger.error.assert_not_called()
        self.assertEqual(actor.state["miniapp_fishing_status"], "daily_done")
        self.assertEqual(actor.state["miniapp_fishing_summary_emitted_count"], 1)

    def test_dashboard_daily_limit_uses_configured_bait_and_done_status(self):
        state = {
            "miniapp_fishing_status": "daily_done",
            "miniapp_fishing_last_error": "fishing_daily_limit_reached",
            "miniapp_fishing_pond": "青溪浅滩",
            "miniapp_fishing_bait": "凡饵",
            "miniapp_fishing_chum": "不打窝",
            "miniapp_fishing_last_grade": "甲等",
            "miniapp_fishing_last_score": 100,
        }
        with patch(
            "dashboard_server.miniapp_fishing_settings",
            return_value={
                "enabled": True,
                "participants": ["main|主魂"],
                "rod_owner": "auto",
                "pond": "qingxi",
                "bait": "demon_blood",
                "chum": "none",
            },
        ), patch(
            "dashboard_server.miniapp_fishing_global_snapshot",
            return_value={
                "status": "daily_done",
                "participant_labels": ["主号｜主魂"],
                "current_label": "",
                "rod_holder_label": "主号｜主魂",
                "detail": "所选身份今日垂钓均已完成",
                "transfer": {},
            },
        ):
            row = dashboard_server.miniapp_fishing_command(state)

        self.assertEqual(row["status"], "今日竿数已尽")
        self.assertEqual(row["tone"], "done")
        self.assertIn("妖血饵", row["detail"])
        self.assertIn("上竿 凡饵", row["detail"])
        self.assertNotIn("fishing_daily_limit_reached", row["detail"])

    def test_sub_account_worker_is_supported(self):
        for account in ("sub", "xiaohao", "waaiging"):
            with self.subTest(account=account):
                worker = MiniAppFishingAutomation(
                    SimpleNamespace(config={}, state={}, save_state=lambda: None),
                    SimpleNamespace(),
                    account,
                    SimpleNamespace(warning=lambda *args, **kwargs: None),
                )
                self.assertTrue(worker.supported)

    def test_manual_holder_is_scanned_first_and_verified(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.avatars = ["厚土"]
                self.config = {}

            def get_avatar_state(self, identity):
                return self.state.setdefault("avatars", {}).setdefault(identity, {})

            def save_state(self):
                pass

        async def fishing_entry(identity):
            if identity == "主魂":
                return "fish_sub", {
                    "session": {
                        "phase": "lobby",
                        "rod": {"itemId": "rod_silver", "name": "银竹钓竿"},
                    }
                }
            raise MiniAppBeastError("fishing_rod_missing")

        transport = SimpleNamespace(
            identity_player_ids={"主魂": 1, "厚土": 2},
            fishing_entry=AsyncMock(side_effect=fishing_entry),
        )
        worker = MiniAppFishingAutomation(
            Actor(),
            transport,
            "sub",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "sub|主魂",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        asyncio.run(worker._scan_local(settings, force=True))
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertEqual(transport.fishing_entry.await_args_list[0].args, ("主魂",))
        self.assertEqual(runtime["rod_holder"], "sub|主魂")
        self.assertEqual(runtime["rod_holder_source"], "manual")
        self.assertEqual(runtime["scans"]["sub|主魂"]["rod_name"], "银竹钓竿")

    def test_cross_account_transfer_lists_purchases_and_verifies(self):
        class Actor:
            def __init__(self, response):
                self.state = {}
                self.config = {}
                self.avatars = []
                self.send_fishing_command = AsyncMock(return_value=response)

            def save_state(self):
                pass

        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "sub|主魂",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(
                current_key="main|主魂",
                rod_holder="sub|主魂",
                rod_holder_source="manual",
                rod_holder_verified_at=miniapp_fishing._now_text(),
            )
            data["scans"] = {
                "sub|主魂": {
                    "has_rod": True,
                    "rod_name": "银竹钓竿",
                    "definitive": True,
                    "phase": "lobby",
                    "active": False,
                    "updated_at": miniapp_fishing._now_text(),
                }
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        main_actor = Actor(
            "**上架成功！**\n你已将 **【凝血草】x1** 上架至万宝楼。\n**挂单ID**: 24474"
        )
        sub_actor = Actor("**交易成功！**\n你成功购得 **【凝血草】x1**！")
        main_transport = SimpleNamespace(
            identity_player_ids={"主魂": 1},
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_main",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {"itemId": "rod_silver", "name": "银竹钓竿"},
                        }
                    },
                )
            ),
        )
        main = MiniAppFishingAutomation(
            main_actor,
            main_transport,
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        sub = MiniAppFishingAutomation(
            sub_actor,
            SimpleNamespace(identity_player_ids={"主魂": 2}),
            "sub",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )

        self.assertTrue(
            asyncio.run(main._create_listing(settings, "sub|主魂", "main|主魂"))
        )
        main_actor.send_fishing_command.assert_awaited_once_with(
            "主魂",
            ".上架 凝血草 换 银竹钓竿1",
            timeout=90,
        )
        transfer = miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"]
        self.assertEqual(transfer["listing_id"], "24474")

        self.assertTrue(asyncio.run(sub._purchase_listing(settings, transfer)))
        sub_actor.send_fishing_command.assert_awaited_once_with(
            "主魂",
            ".购买 24474",
            timeout=90,
        )
        purchased = miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"]
        self.assertEqual(purchased["status"], "purchased")

        self.assertEqual(asyncio.run(main._handle_transfer(settings, purchased)), 2)
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)
        self.assertEqual(runtime["rod_holder"], "main|主魂")
        self.assertEqual(runtime["transfer"], {})
        self.assertEqual(runtime["last_transfer"]["status"], "verified")

    def test_specific_rod_ignores_a_different_detected_rod(self):
        actor = SimpleNamespace(state={}, config={}, save_state=lambda: None)
        transport = SimpleNamespace(
            identity_player_ids={"主魂": 1},
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_main",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {"itemId": "rod_gold", "name": "金竹钓竿"},
                        }
                    },
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod": "金雷竹钓竿",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        info = asyncio.run(worker._scan_identity("主魂", settings))
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertTrue(info["has_any_rod"])
        self.assertFalse(info["has_rod"])
        self.assertEqual(info["rod_name"], "金竹钓竿")
        self.assertEqual(runtime["rod_holder"], "")

    def test_scan_keeps_pending_result_identity_active_when_server_reports_lobby(self):
        actor = SimpleNamespace(
            state={"miniapp_fishing_pending_result": {"id": "pending-1"}},
            config={},
            save_state=lambda: None,
        )
        transport = SimpleNamespace(
            identity_player_ids={"主魂": 1},
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_pending",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {"itemId": "rod_silver", "name": "银竹钓竿"},
                        }
                    },
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod": "auto",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        info = asyncio.run(worker._scan_identity("主魂", settings))

        self.assertTrue(info["active"])
        self.assertEqual(info["phase"], "settling")

    def test_auto_rod_uses_detected_type_for_transfer_listing(self):
        actor = SimpleNamespace(
            state={},
            config={},
            save_state=lambda: None,
            send_fishing_command=AsyncMock(
                return_value=(
                    "**上架成功！**\n你已将 **【凝血草】x1** 上架至万宝楼。\n"
                    "**挂单ID**: 31415"
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(identity_player_ids={"主魂": 1}),
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod": "auto",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(current_key="main|主魂", rod_holder="sub|主魂", rod_name="金雷竹钓竿")
            data["scans"] = {
                "sub|主魂": {
                    "has_rod": True,
                    "rod_name": "金雷竹钓竿",
                    "definitive": True,
                    "updated_at": miniapp_fishing._now_text(),
                }
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        created = asyncio.run(worker._create_listing(settings, "sub|主魂", "main|主魂"))
        transfer = miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"]

        self.assertTrue(created)
        actor.send_fishing_command.assert_awaited_once_with(
            "主魂",
            ".上架 凝血草 换 金雷竹钓竿1",
            timeout=90,
        )
        self.assertEqual(transfer["rod_name"], "金雷竹钓竿")

    def test_legacy_hardcoded_transfer_is_stopped_when_actual_rod_differs(self):
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod": "auto",
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(rod_holder="main|主魂", configured_rod="")
            data["scans"] = {
                "main|主魂": {
                    "has_rod": True,
                    "rod_name": "青竹钓竿",
                    "definitive": True,
                    "updated_at": miniapp_fishing._now_text(),
                }
            }
            data["transfer"] = {
                "id": "legacy-silver",
                "status": "purchase_unknown",
                "from": "main|主魂",
                "to": "sub|主魂",
                "listing_id": "24765",
                "response": "每件售价: 【银竹钓竿】x1",
            }

        miniapp_fishing._update_global_state(seed)
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertEqual(runtime["configured_rod"], "auto")
        self.assertEqual(runtime["rod_name"], "青竹钓竿")
        self.assertEqual(runtime["transfer"], {})
        self.assertEqual(runtime["last_transfer"]["status"], "superseded_rod_mismatch")
        self.assertIn("停止错误重试", runtime["detail"])

    def test_active_round_blocks_transfer_listing(self):
        actor = SimpleNamespace(
            state={},
            config={},
            save_state=lambda: None,
            send_fishing_command=AsyncMock(),
        )
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(identity_player_ids={}),
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        worker._scan_local = AsyncMock()
        worker._scan_started = True
        settings = {
            "enabled": True,
            "participants": ["main|主魂"],
            "rod_owner": "sub|主魂",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(current_key="main|主魂", rod_holder="sub|主魂")
            data["scans"] = {
                "sub|主魂": {
                    "has_rod": True,
                    "definitive": True,
                    "phase": "waiting",
                    "active": True,
                    "updated_at": miniapp_fishing._now_text(),
                }
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        self.assertEqual(asyncio.run(worker._drive_once(settings)), 5)
        actor.send_fishing_command.assert_not_awaited()
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)
        self.assertEqual(runtime["status"], "active_round")
        self.assertEqual(runtime["transfer"], {})

    def test_failed_transfer_waits_until_scheduled_retry(self):
        actor = SimpleNamespace(state={}, config={}, save_state=lambda: None)
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(identity_player_ids={}),
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }
        retry_at = (datetime.now() + timedelta(seconds=95)).strftime(miniapp_fishing.TIME_FORMAT)

        def seed(data):
            data.update(current_key="main|主魂", rod_holder="sub|主魂")
            data["transfer"] = {
                "id": "transfer-retry-wait",
                "status": "listing_unknown",
                "from": "sub|主魂",
                "to": "main|主魂",
                "updated_at": miniapp_fishing._now_text(),
                "next_retry_at": retry_at,
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        transfer = miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"]

        wait = asyncio.run(worker._handle_transfer(settings, transfer))
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertGreaterEqual(wait, 90)
        self.assertLessEqual(wait, 95)
        self.assertEqual(runtime["status"], "transfer_retry_wait")
        self.assertIn("自动补跑", runtime["detail"])
        self.assertEqual(runtime["transfer"]["next_retry_at"], retry_at)

    def test_due_listing_unknown_recreates_listing_hourly(self):
        class Actor:
            def __init__(self, response):
                self.state = {}
                self.config = {}
                self.avatars = []
                self.send_fishing_command = AsyncMock(return_value=response)

            def save_state(self):
                pass

        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|寻真子"],
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(current_key="main|主魂", rod_holder="sub|寻真子")
            data["transfer"] = {
                "id": "transfer-recreate",
                "status": "listing_unknown",
                "from": "sub|寻真子",
                "to": "main|主魂",
                "updated_at": "2026-08-06 03:45:44",
                "next_retry_at": "2026-08-06 04:45:44",
            }
            data["scans"] = {
                "sub|寻真子": {
                    "account": "sub",
                    "identity": "寻真子",
                    "has_rod": True,
                    "rod_name": "银竹钓竿",
                    "phase": "lobby",
                    "active": False,
                    "challenge": False,
                    "definitive": True,
                    "error": "",
                    "updated_at": miniapp_fishing._now_text(),
                }
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        actor = Actor("**上架成功！**\n你已将 **【凝血草】x1** 上架至万宝楼。\n**挂单ID**: 29999")
        transport = SimpleNamespace(
            identity_player_ids={"主魂": 1},
            fishing_entry=AsyncMock(side_effect=MiniAppBeastError("fishing_rod_missing")),
        )
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        transfer = miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"]

        wait = asyncio.run(worker._handle_transfer(settings, transfer))
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertEqual(wait, 5)
        actor.send_fishing_command.assert_awaited_once_with(
            "主魂",
            ".上架 凝血草 换 银竹钓竿1",
            timeout=90,
        )
        self.assertEqual(runtime["transfer"]["id"], "transfer-recreate")
        self.assertEqual(runtime["transfer"]["status"], "listed")
        self.assertEqual(runtime["transfer"]["listing_id"], "29999")
        self.assertEqual(runtime["transfer"]["from"], "sub|寻真子")

    def test_due_purchase_unknown_retries_purchase_hourly(self):
        class Actor:
            def __init__(self, response):
                self.state = {}
                self.config = {}
                self.avatars = []
                self.send_fishing_command = AsyncMock(return_value=response)

            def save_state(self):
                pass

        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(current_key="main|主魂", rod_holder="sub|主魂")
            data["transfer"] = {
                "id": "transfer-purchase-retry",
                "status": "purchase_unknown",
                "from": "sub|主魂",
                "to": "main|主魂",
                "listing_id": "24474",
                "updated_at": "2026-08-06 03:45:44",
                "next_retry_at": "2026-08-06 04:45:44",
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        actor = Actor("**交易成功！**\n你成功购得 **【凝血草】x1**！")
        transport = SimpleNamespace(
            identity_player_ids={"主魂": 2},
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_sub",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {"itemId": "rod_silver", "name": "银竹钓竿"},
                        }
                    },
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            actor,
            transport,
            "sub",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        transfer = miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"]

        wait = asyncio.run(worker._handle_transfer(settings, transfer))
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertEqual(wait, 5)
        actor.send_fishing_command.assert_awaited_once_with(
            "主魂",
            ".购买 24474",
            timeout=90,
        )
        self.assertEqual(runtime["transfer"]["status"], "purchased")
        self.assertEqual(runtime["status"], "verifying_transfer")

    def test_empty_purchase_response_retries_in_one_minute(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}
                self.avatars = []
                self.send_fishing_command = AsyncMock(return_value="")

            def save_state(self):
                pass

        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(current_key="main|主魂", rod_holder="sub|主魂")
            data["transfer"] = {
                "id": "transfer-empty-purchase-response",
                "status": "listed",
                "from": "sub|主魂",
                "to": "main|主魂",
                "listing_id": "24474",
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(identity_player_ids={}),
            "sub",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )

        before = datetime.now()
        self.assertFalse(
            asyncio.run(
                worker._purchase_listing(
                    settings,
                    miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"],
                )
            )
        )
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)
        retry_at = datetime.strptime(
            runtime["transfer"]["next_retry_at"], miniapp_fishing.TIME_FORMAT
        )

        self.assertEqual(runtime["transfer"]["status"], "purchase_unknown")
        self.assertGreaterEqual((retry_at - before).total_seconds(), 59)
        self.assertLessEqual((retry_at - before).total_seconds(), 61)

    def test_unrelated_worker_keeps_failed_transfer_retry_short(self):
        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂", "xiaohao|主魂"],
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data.update(current_key="sub|主魂", rod_holder="main|主魂")
            data["transfer"] = {
                "id": "transfer-unrelated-worker",
                "status": "purchase_unknown",
                "from": "main|主魂",
                "to": "sub|主魂",
                "listing_id": "24474",
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        worker = MiniAppFishingAutomation(
            SimpleNamespace(state={}, config={}, avatars=[], save_state=lambda: None),
            SimpleNamespace(identity_player_ids={}),
            "xiaohao",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )

        before = datetime.now()
        wait = asyncio.run(
            worker._resume_failed_transfer(
                settings,
                miniapp_fishing.miniapp_fishing_global_snapshot(settings)["transfer"],
            )
        )
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)
        retry_at = datetime.strptime(
            runtime["transfer"]["next_retry_at"], miniapp_fishing.TIME_FORMAT
        )

        self.assertEqual(wait, miniapp_fishing.FISHING_TRANSFER_RETRY_SECONDS)
        self.assertGreaterEqual((retry_at - before).total_seconds(), 59)
        self.assertLessEqual((retry_at - before).total_seconds(), 61)

    def test_wrong_cached_holder_recovers_with_verified_holder_on_same_listing(self):
        class Actor:
            def __init__(self):
                self.state = {"avatars": {"无咎子": {}}}
                self.config = {}
                self.avatars = ["无咎子"]

            def get_avatar_state(self, identity):
                return self.state["avatars"][identity]

            def save_state(self):
                pass

        settings = {
            "enabled": True,
            "participants": ["sub|主魂"],
            "rod_owner": "main|主魂",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }

        def seed(data):
            data["rod_holder"] = ""
            data["transfer"] = {
                "id": "transfer-1",
                "status": "purchase_failed",
                "failure_code": "missing_required_rod",
                "from": "main|主魂",
                "to": "sub|主魂",
                "listing_id": "24474",
                "updated_at": miniapp_fishing._now_text(),
            }

        miniapp_fishing._update_global_state(seed, settings=settings)
        transport = SimpleNamespace(
            identity_player_ids={"无咎子": 2},
            fishing_entry=AsyncMock(
                return_value=(
                    "fish_holder",
                    {
                        "session": {
                            "phase": "lobby",
                            "rod": {"itemId": "rod_silver", "name": "银竹钓竿"},
                        }
                    },
                )
            ),
        )
        worker = MiniAppFishingAutomation(
            Actor(),
            transport,
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )

        asyncio.run(worker._scan_identity("无咎子", settings))
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)

        self.assertEqual(runtime["rod_holder"], "main|无咎子")
        self.assertEqual(runtime["transfer"]["from"], "main|无咎子")
        self.assertEqual(runtime["transfer"]["status"], "listed")
        self.assertEqual(runtime["transfer"]["listing_id"], "24474")

    def test_completed_round_keeps_current_identity_until_daily_limit(self):
        worker = MiniAppFishingAutomation(
            SimpleNamespace(config={}, state={}, save_state=lambda: None),
            SimpleNamespace(),
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod_owner": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
        }
        miniapp_fishing._update_global_state(
            lambda data: data.update(current_key="main|主魂", rod_holder="main|主魂"),
            settings=settings,
        )

        runtime = worker._complete_round(settings, "main|主魂")

        self.assertEqual(runtime["current_key"], "main|主魂")
        self.assertEqual(runtime["last_round"]["participant"], "main|主魂")
        self.assertEqual(runtime["status"], "fishing")
        self.assertIn("继续垂钓", runtime["detail"])

        runtime = worker._mark_daily_done(settings, "main|主魂")
        self.assertEqual(runtime["current_key"], "sub|主魂")
        self.assertIn("今日竿数已尽", runtime["detail"])

    def test_force_retry_ignores_completion_once_then_restores_it(self):
        settings = {
            "enabled": True,
            "participants": ["main|主魂", "sub|主魂"],
            "rod_owner": "auto",
            "rod": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
            "start_time": "",
        }
        completed_at = miniapp_fishing._now_text()
        miniapp_fishing._update_global_state(
            lambda data: data.update(
                completed_today={"main|主魂": completed_at, "sub|主魂": completed_at},
                current_key="",
                status="daily_done",
            ),
            settings=settings,
        )

        runtime = request_miniapp_fishing_force_retry(settings, requested_by="test")

        self.assertEqual(runtime["current_key"], "main|主魂")
        self.assertEqual(runtime["completed_today"], {})
        self.assertEqual(
            runtime["force_retry"]["pending"],
            ["main|主魂", "sub|主魂"],
        )

        worker = MiniAppFishingAutomation(
            SimpleNamespace(config={}, state={}, save_state=lambda: None),
            SimpleNamespace(),
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        runtime = worker._complete_round(settings, "main|主魂")
        self.assertEqual(runtime["current_key"], "sub|主魂")
        self.assertEqual(runtime["force_retry"]["pending"], ["sub|主魂"])

        runtime = worker._mark_daily_done(settings, "sub|主魂")
        self.assertEqual(runtime["force_retry"], {})
        self.assertEqual(runtime["current_key"], "")
        self.assertEqual(runtime["status"], "daily_done")
        self.assertEqual(runtime["completed_today"]["main|主魂"], completed_at)
        self.assertIn("sub|主魂", runtime["completed_today"])
        self.assertEqual(
            runtime["last_force_retry"]["attempted"],
            ["main|主魂", "sub|主魂"],
        )

    def test_force_retry_rejects_disabled_automation(self):
        with self.assertRaisesRegex(ValueError, "Mini App fishing is disabled"):
            request_miniapp_fishing_force_retry(
                {
                    "enabled": False,
                    "participants": ["main|主魂"],
                    "rod_owner": "auto",
                    "rod": "auto",
                    "pond": "qingxi",
                    "bait": "demon_blood",
                    "chum": "none",
                    "start_time": "",
                }
            )

    def test_force_retry_wakes_a_sleeping_worker(self):
        actor = SimpleNamespace(is_running=True, config={}, state={}, save_state=lambda: None)
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "main",
            SimpleNamespace(warning=lambda *args, **kwargs: None),
        )
        worker._force_retry_request_id = "old"

        async def mark_request(_seconds):
            miniapp_fishing._update_global_state(
                lambda data: data.update(force_retry_request_id="new")
            )

        with patch("miniapp_fishing.asyncio.sleep", new=AsyncMock(side_effect=mark_request)) as sleep:
            asyncio.run(worker._sleep_until_next_cycle(300, {}))

        self.assertEqual(sleep.await_count, 1)

    def test_force_retry_error_advances_without_normal_backoff(self):
        class Actor:
            def __init__(self):
                self.state = {}
                self.config = {}
                self.is_running = True

            def save_state(self):
                pass

        actor = Actor()
        worker = MiniAppFishingAutomation(
            actor,
            SimpleNamespace(),
            "main",
            SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        )
        settings = {
            "enabled": True,
            "participants": ["main|主魂", "main|无咎子"],
            "rod_owner": "auto",
            "rod": "auto",
            "pond": "qingxi",
            "bait": "demon_blood",
            "chum": "none",
            "start_time": "",
        }
        worker.settings = lambda: settings
        worker._clear_irrelevant_local_statuses = Mock()
        worker._apply_force_retry_request = Mock(
            return_value={"pending": ["main|主魂", "main|无咎子"]}
        )
        worker._drive_once = AsyncMock(
            side_effect=MiniAppBeastError("fishing_bait_unaffordable")
        )
        worker._force_retry_request_id = "request"
        request_miniapp_fishing_force_retry(settings, requested_by="test")

        waits = []

        async def stop_after_wait(wait, _settings):
            waits.append(wait)
            actor.is_running = False

        worker._sleep_until_next_cycle = AsyncMock(side_effect=stop_after_wait)
        asyncio.run(worker.run_loop())

        self.assertEqual(waits, [1])
        runtime = miniapp_fishing.miniapp_fishing_global_snapshot(settings)
        self.assertEqual(runtime["force_retry"]["pending"], ["main|无咎子"])

    def test_dashboard_force_retry_endpoint_returns_runtime(self):
        expected = {"status": "force_retry", "force_retry": {"pending": ["main|主魂"]}}
        with patch(
            "dashboard_server.miniapp_fishing_settings",
            return_value={"enabled": True, "participants": ["main|主魂"]},
        ), patch(
            "dashboard_server.request_miniapp_fishing_force_retry",
            return_value=expected,
        ) as request:
            result = asyncio.run(
                dashboard_server.automation_fishing_force_retry(username="tester")
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["runtime"], expected)
        request.assert_called_once_with(
            {"enabled": True, "participants": ["main|主魂"]},
            requested_by="tester",
        )


if __name__ == "__main__":
    unittest.main()
