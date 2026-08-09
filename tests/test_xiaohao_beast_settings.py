import unittest
from unittest.mock import patch

import cultivator_xiaohao


class XiaoHaoBeastSettingTests(unittest.TestCase):
    def setUp(self):
        self.actor = cultivator_xiaohao.CultivatorXiaoHao.__new__(cultivator_xiaohao.CultivatorXiaoHao)
        self.actor.state = {
            "beasts_cache": [
                {"full_name": "小玉", "status": "休息中", "power": 1184, "stamina": 100, "exp": 0, "species": "二阶噬魂兽"},
                {"full_name": "谛听", "status": "休息中", "power": 314, "stamina": 71, "exp": 0, "species": "二阶噬魂兽"},
                {"full_name": "猴哥", "status": "休息中", "power": 210, "stamina": 92, "exp": 0, "species": "二阶金瞳妖猴"},
            ]
        }
        self.actor.save_state = lambda: None
        self.actor.beast_name_matches = cultivator_xiaohao.CultivatorXiaoHao.beast_name_matches.__get__(self.actor)
        self.actor.is_valid_beast_record = cultivator_xiaohao.CultivatorXiaoHao.is_valid_beast_record.__get__(self.actor)
        self.actor.beast_stamina_value = cultivator_xiaohao.CultivatorXiaoHao.beast_stamina_value.__get__(self.actor)
        self.actor.is_injury_status = cultivator_xiaohao.CultivatorXiaoHao.is_injury_status.__get__(self.actor)
        self.actor.is_pastured_status = cultivator_xiaohao.CultivatorXiaoHao.is_pastured_status.__get__(self.actor)

    def test_abyss_range_excludes_outside_beasts(self):
        with patch.object(
            cultivator_xiaohao,
            "miniapp_beast_abyss_power_in_range",
            side_effect=lambda power, match_all_when_empty=True: 1000 <= int(power) <= 1300 if match_all_when_empty else 1000 <= int(power) <= 1300,
        ):
            abyss = self.actor.abyss_candidate_beasts()
            patrol = self.actor.border_patrol_candidate_beasts()

        self.assertEqual([item["full_name"] for item in abyss], ["小玉"])
        self.assertEqual([item["full_name"] for item in patrol], ["猴哥", "谛听"])


if __name__ == "__main__":
    unittest.main()
