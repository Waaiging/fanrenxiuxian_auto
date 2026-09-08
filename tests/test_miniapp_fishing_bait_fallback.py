import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError
from miniapp_fishing import MiniAppFishingAutomation


class FishingBaitFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_worker()

    def make_worker(self, account="waaiging"):
        self.shop = {
            "baits": [
                {
                    "key": key,
                    "name": name,
                    "itemId": "item_fishing_bait_" + key,
                    "count": 0,
                    "unlocked": True,
                    "cost": [{"name": "灵石", "qty": price, "owned": 10000}],
                }
                for key, name, price in (
                    ("plain", "凡饵", 12),
                    ("spirit_rice", "灵米饵", 40),
                    ("spirit_worm", "灵虫饵", 90),
                    ("demon_blood", "妖血饵", 220),
                    ("moon", "月华饵", 650),
                )
            ],
            "ponds": [{"key": "qingxi", "name": "青溪浅滩", "unlocked": True}],
            "chums": [],
        }
        self.failures = {}

        async def buy(identity, token, key, quantity, cost, **kwargs):
            if key in self.failures:
                raise self.failures[key]
            self.bait(key)["count"] += quantity
            return {"shop": deepcopy(self.shop)}

        self.transport = SimpleNamespace(
            fishing_buy_bait=AsyncMock(side_effect=buy),
            fishing_next_cast=AsyncMock(return_value=("fish_cast", {})),
            fishing_start=AsyncMock(return_value=(
                "fish_cast", {"session": {"serverNow": 1000, "biteAt": 31000}}
            )),
        )
        actor = SimpleNamespace(state={}, config={}, save_state=Mock())
        self.worker = MiniAppFishingAutomation(actor, self.transport, account, Mock())
        self.worker._notify_material_shortage = AsyncMock()

    def bait(self, key):
        return next(item for item in self.shop["baits"] if item["key"] == key)

    def unaffordable(self, *keys):
        for key in keys:
            self.bait(key)["cost"][0]["owned"] = 0

    def purchased_keys(self):
        return [call.args[2] for call in self.transport.fishing_buy_bait.await_args_list]

    async def ensure(self, key="demon_blood"):
        return await self.worker._ensure_cast_bait("主魂", "fish_lobby", self.shop, key)

    async def test_demon_blood_shortage_never_buys_moon_for_any_account(self):
        for account in ("main", "sub", "xiaohao", "waaiging"):
            with self.subTest(account=account):
                self.make_worker(account)
                self.unaffordable("demon_blood")

                _, bait, key = await self.ensure()

                self.assertEqual((key, bait["name"]), ("spirit_worm", "灵虫饵"))
                self.assertEqual(self.purchased_keys(), ["spirit_worm"])
                self.worker._notify_material_shortage.assert_not_awaited()

    async def test_existing_moon_stock_does_not_override_the_ceiling(self):
        self.unaffordable("demon_blood")
        self.bait("moon")["count"] = 99

        _, _, key = await self.ensure()

        self.assertEqual(key, "spirit_worm")
        self.assertEqual(self.purchased_keys(), ["spirit_worm"])
        self.assertEqual(self.bait("moon")["count"], 99)

    async def test_nearest_lower_tier_wins_even_when_only_one_is_affordable(self):
        self.unaffordable("demon_blood")
        self.bait("spirit_worm")["cost"][0]["owned"] = 90

        _, bait, key = await self.ensure()

        self.assertEqual(key, "spirit_worm")
        self.assertEqual(bait["count"], 1)
        self.assertEqual(self.transport.fishing_buy_bait.await_args.args[3], 1)

    async def test_configured_bait_is_used_from_stock_before_any_purchase(self):
        self.unaffordable("demon_blood")
        self.bait("demon_blood")["count"] = 1
        self.bait("moon")["count"] = 99

        _, _, key = await self.ensure()

        self.assertEqual(key, "demon_blood")
        self.transport.fishing_buy_bait.assert_not_awaited()
        self.assertNotIn("miniapp_fishing_bait_fallback_to", self.worker.actor.state)

    async def test_configured_bait_purchase_takes_precedence_over_other_stock(self):
        self.bait("moon")["count"] = 99
        self.bait("spirit_worm")["count"] = 10

        _, _, key = await self.ensure()

        self.assertEqual(key, "demon_blood")
        self.assertEqual(self.purchased_keys(), ["demon_blood"])

    async def test_stock_of_nearest_lower_tier_avoids_another_purchase(self):
        self.unaffordable("demon_blood", "spirit_worm")
        self.bait("spirit_worm")["count"] = 1

        _, _, key = await self.ensure()

        self.assertEqual(key, "spirit_worm")
        self.transport.fishing_buy_bait.assert_not_awaited()

    async def test_confirmed_purchase_rejections_continue_down_to_plain_once_each(self):
        self.failures = {
            "demon_blood": MiniAppBeastError("fishing_bait_unaffordable"),
            "spirit_worm": MiniAppBeastError("fishing_bait_unaffordable"),
            "spirit_rice": MiniAppBeastError("fishing_bait_level_low"),
        }

        _, _, key = await self.ensure()

        self.assertEqual(key, "plain")
        self.assertEqual(self.purchased_keys(), ["demon_blood", "spirit_worm", "spirit_rice", "plain"])
        self.assertEqual(self.worker.actor.state["miniapp_fishing_pending_purchases"],
                         [{"name": "凡饵", "quantity": 10}])

    async def test_catalog_order_and_unknown_tiers_do_not_choose_the_fallback(self):
        self.unaffordable("demon_blood")
        by_key = {item["key"]: item for item in self.shop["baits"]}
        self.shop["baits"] = [by_key[key] for key in
                              ("spirit_worm", "demon_blood", "plain", "spirit_rice", "moon")]
        self.shop["baits"].append(dict(by_key["moon"], key="future_bait", count=99))

        _, _, key = await self.ensure()

        self.assertEqual(key, "spirit_worm")
        self.assertEqual(self.purchased_keys(), ["spirit_worm"])

    async def test_locked_and_missing_lower_tiers_are_skipped(self):
        self.bait("demon_blood")["unlocked"] = False
        self.bait("spirit_worm")["unlocked"] = False
        self.shop["baits"].remove(self.bait("spirit_rice"))

        _, _, key = await self.ensure()

        self.assertEqual(key, "plain")
        self.assertEqual(self.purchased_keys(), ["plain"])

    async def test_missing_configured_item_uses_its_known_lower_tiers(self):
        self.shop["baits"].remove(self.bait("demon_blood"))

        _, _, key = await self.ensure()

        self.assertEqual(key, "spirit_worm")
        self.assertEqual(self.purchased_keys(), ["spirit_worm"])

    async def test_each_configured_tier_has_its_own_ceiling(self):
        for configured, expected in (("moon", "demon_blood"),
                                     ("spirit_worm", "spirit_rice"),
                                     ("spirit_rice", "plain")):
            with self.subTest(configured=configured):
                self.make_worker()
                self.unaffordable(configured)

                _, _, key = await self.ensure(configured)

                self.assertEqual(key, expected)
                self.assertEqual(self.purchased_keys(), [expected])

    async def test_lowest_tier_shortage_never_upgrades(self):
        self.unaffordable("plain")

        with self.assertRaisesRegex(MiniAppBeastError, "fishing_bait_unaffordable"):
            await self.ensure("plain")

        self.transport.fishing_buy_bait.assert_not_awaited()
        self.worker._notify_material_shortage.assert_awaited_once()

    async def test_all_lower_tiers_unavailable_stop_even_with_moon_in_stock(self):
        self.unaffordable("demon_blood", "spirit_worm", "spirit_rice", "plain")
        self.bait("moon")["count"] = 99

        with self.assertRaisesRegex(MiniAppBeastError, "fishing_bait_unaffordable"):
            await self.ensure()

        self.transport.fishing_buy_bait.assert_not_awaited()
        self.worker._notify_material_shortage.assert_awaited_once()
        self.assertNotIn("miniapp_fishing_bait_fallback_to", self.worker.actor.state)

    async def test_unknown_configured_tier_does_not_guess_a_replacement(self):
        with self.assertRaisesRegex(MiniAppBeastError, "fishing_bait_invalid"):
            await self.ensure("unknown")

        self.transport.fishing_buy_bait.assert_not_awaited()

    async def test_transport_and_uncertain_errors_stop_at_the_failing_tier(self):
        for failing_key in ("demon_blood", "spirit_worm"):
            for error in (
                TimeoutError(), MiniAppCircuitOpenError(120), asyncio.CancelledError(),
                *(MiniAppBeastError(code) for code in (
                    "external_action_rate_limited", "fishing_auth_refreshed",
                    "invalid_json", "fishing_shop_cost_missing", "fishing_bait_missing",
                )),
            ):
                with self.subTest(key=failing_key, error=type(error).__name__, code=str(error)):
                    self.make_worker()
                    if failing_key == "spirit_worm":
                        self.unaffordable("demon_blood")
                    self.failures[failing_key] = error

                    with self.assertRaises(type(error)) as caught:
                        await self.ensure()

                    self.assertIs(caught.exception, error)
                    self.assertEqual(self.purchased_keys(), [failing_key])
                    self.assertNotIn("miniapp_fishing_bait_fallback_to", self.worker.actor.state)

    async def test_unconfirmed_purchase_response_does_not_buy_another_bait(self):
        self.transport.fishing_buy_bait.side_effect = None
        self.transport.fishing_buy_bait.return_value = {"ok": True}

        with self.assertRaisesRegex(MiniAppBeastError, "fishing_bait_missing"):
            await self.ensure()

        self.assertEqual(self.purchased_keys(), ["demon_blood"])
        self.assertNotIn("miniapp_fishing_pending_purchases", self.worker.actor.state)

    async def test_next_cast_returns_to_configured_bait_when_materials_recover(self):
        settings = {"pond": "qingxi", "bait": "demon_blood", "chum": "none"}
        original_settings = dict(settings)
        self.unaffordable("demon_blood")

        await self.worker._start_cast("主魂", "fish_lobby", self.shop, settings)

        self.assertEqual(self.transport.fishing_next_cast.await_args.args[3],
                         "item_fishing_bait_spirit_worm")
        self.assertEqual(self.worker.actor.state["miniapp_fishing_bait_selection_reason"], "fallback")
        self.assertEqual(self.worker.actor.state["miniapp_fishing_configured_bait_key"], "demon_blood")
        self.assertEqual(self.worker.actor.state["miniapp_fishing_bait_fallback_to"], "spirit_worm")
        self.bait("demon_blood")["cost"][0]["owned"] = 10000

        await self.worker._start_cast("主魂", "fish_lobby", self.shop, settings)

        self.assertEqual(self.transport.fishing_next_cast.await_args.args[3],
                         "item_fishing_bait_demon_blood")
        self.assertEqual(self.worker.actor.state["miniapp_fishing_bait_selection_reason"], "configured")
        self.assertEqual(self.purchased_keys(), ["spirit_worm", "demon_blood"])
        self.assertEqual(settings, original_settings)


if __name__ == "__main__":
    unittest.main()
