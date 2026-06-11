import unittest

from concubine_features import ConcubineMixin, concubine_default_state, parse_duration_seconds, seconds_until
from cultivator_xiaohao import CultivatorXiaoHao
from dashboard_server import parse_inventory_items_from_text, parse_resource_changes_from_text
from intelligent_cultivator import Cultivator
from log_utils import parse_cultivation_delta_text, parse_cultivation_profile_text
from sub_cultivator import SubCultivator


class DummyConcubine(ConcubineMixin):
    account_key = "main"
    avatars = []

    def __init__(self):
        self.state = concubine_default_state()

    def save_state(self):
        return None

    def parse_wait_time(self, text, *args, **kwargs):
        return parse_duration_seconds(text)


class ParserFixtureTests(unittest.TestCase):
    def test_cultivation_profile_from_spirit_root_reply(self):
        text = """
**@Lvdoumiao**** 的天命玉牒**
────────────────
**宗门**: 【星宫】
**灵根**: 真灵根(土木)
**修为**: 12,443 / 30,000
**丹毒**: 0 点
"""
        profile = parse_cultivation_profile_text(text)

        self.assertEqual(profile["spirit_root"], "真灵根(土木)")
        self.assertEqual(profile["current_exp"], 12443)
        self.assertEqual(profile["total_exp"], 30000)

    def test_cultivation_delta_gain_and_loss(self):
        self.assertEqual(
            parse_cultivation_delta_text("野外历练结束，获得修为 **+336**，采得灵草若干。"),
            [336],
        )
        self.assertEqual(
            parse_cultivation_delta_text("闯塔失败，道心受挫，修为倒退了 **1,200** 点。"),
            [-1200],
        )

    def test_wait_time_line_identifier_and_minimum(self):
        actor = Cultivator.__new__(Cultivator)

        self.assertEqual(actor.parse_wait_time("登阶冷却：1小时2分钟3秒", line_identifier="登阶冷却"), 3723)
        self.assertEqual(
            actor.parse_wait_time("入梦寻图冷却：8小时\n共历心劫冷却：9小时", find_min=True),
            8 * 3600,
        )

    def test_beast_roster_parser_ignores_return_title_and_preserves_injury(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        roster = """
【灵兽伙伴们】
- 六翼(一阶)(受伤，预计还需 1小时2分钟)
  - 种类: 天鹏
  - 经验: 12
  - 战力: 500
  - 体力: 45
- 青蛟(一阶)(休息中)
  - 种类: 蛟龙
  - 经验: 8
  - 战力: 420
  - 体力: 60
"""
        beasts = actor.parse_beasts_info(roster)

        self.assertEqual([b["full_name"] for b in beasts], ["六翼 (一阶)", "青蛟 (一阶)"])
        self.assertIn("受伤", beasts[0]["status"])
        self.assertEqual(beasts[0]["status_cd"], 3720)
        self.assertEqual(actor.parse_beasts_info("【灵兽归来】\n体力恢复若干。"), [])

    def test_beast_candidate_protects_low_stamina_focus_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "六翼", "species": "天鹏", "status": "休息中", "power": 900, "exp": 10, "stamina": 45},
            {"full_name": "青蛟", "species": "蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 60},
        ]

        candidates = actor.beast_action_candidates("abyss", cache, 30)

        self.assertEqual([b["full_name"] for b in candidates], ["青蛟"])

    def test_concubine_status_blocks_chain_during_active_voyage(self):
        actor = DummyConcubine()
        status = """
【道心侍妾】
远航状态：进行中，剩余 1小时
入梦寻图冷却：可施展
共历心劫冷却：2小时10分钟
天机代卜冷却：无
侍妾远航冷却：进行中，剩余 1小时
"""

        self.assertTrue(actor.parse_concubine_status(status))
        self.assertTrue(actor.state["concubine_voyage_active"])
        self.assertGreater(seconds_until(actor.state["next_dream_map_time"]), 50 * 60)
        self.assertGreater(seconds_until(actor.state["next_heart_trial_time"]), 50 * 60)

    def test_formation_success_and_pending_fixtures(self):
        actor = SubCultivator.__new__(SubCultivator)

        self.assertTrue(actor.is_formation_pending("【周天星斗大阵-启】正在布设大阵，尚需 2 位道友助阵。"))
        self.assertTrue(actor.is_formation_success("【周天星斗大阵-成】大阵已成，星辉流转。"))

    def test_resource_and_inventory_parsers(self):
        changes = parse_resource_changes_from_text(
            "传功玉简已记录！获得了 **30** 点贡献。\n"
            "收集完成，获得了【星辰精华】x2。\n"
            "你消耗了 **100** 点灵石。"
        )
        compact = {(item["name"], item["amount"]) for item in changes}

        self.assertIn(("贡献", 30), compact)
        self.assertIn(("星辰精华", 2), compact)
        self.assertIn(("灵石", -100), compact)

        inventory = parse_inventory_items_from_text("【储物袋】\n【养魂木】x3\n灵石：1200")
        self.assertIn({"name": "养魂木", "amount": 3}, inventory)
        self.assertIn({"name": "灵石", "amount": 1200}, inventory)


if __name__ == "__main__":
    unittest.main()
