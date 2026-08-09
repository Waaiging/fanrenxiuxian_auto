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

    def test_reconcile_removes_unselected_xiaohao_from_shared_state(self):
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
        self.assertNotIn("xiaohao|主魂", data["completed_today"])
        self.assertNotIn("xiaohao|主魂", data["round_records"])
        self.assertNotIn("xiaohao|主魂", data["summary_emitted_ids"])
        self.assertEqual(data["transfer"], {})
        self.assertEqual(
            data["last_transfer"]["status"],
            "cancelled_participant_removed",
        )

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


if __name__ == "__main__":
    unittest.main()
