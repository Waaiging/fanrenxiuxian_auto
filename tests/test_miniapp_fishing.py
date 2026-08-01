import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniapp_dwelling import MiniAppDwellingTransport
from miniapp_fishing import (
    MiniAppFishingAutomation,
    build_fishing_proof,
    fishing_result_summary,
)


ENTRY = "https://t.me/fanrenxiuxian_bot?startapp=dwelling_fixture"


def shop_payload(*, bait_count=0, active_chum=None):
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
                    "usedToday": 0,
                    "remainingToday": 1,
                    "affordable": bait_count >= 2,
                    "cost": [{"name": "妖血饵", "qty": 2, "owned": bait_count}],
                }
            ],
        }
    }


class MiniAppFishingTests(unittest.TestCase):
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

    def test_lobby_buys_missing_selected_bait_before_casting(self):
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
            fishing_buy_bait=AsyncMock(return_value=shop_payload(bait_count=1)),
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
            "主魂", "fish_lobby", "demon_blood", 1
        )
        transport.fishing_next_cast.assert_awaited_once_with(
            "主魂",
            "fish_lobby",
            "qingxi",
            "item_fishing_bait_demon_blood",
            log_operation=False,
        )

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


if __name__ == "__main__":
    unittest.main()
