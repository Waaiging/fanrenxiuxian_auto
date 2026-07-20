import json
import os
import tempfile
import unittest
from unittest.mock import patch

import intelligent_cultivator
from dashboard_server import build_command_panels
from main_beast_features import (
    ABYSS_CD_SECONDS,
    BORDER_PATROL_CD_SECONDS,
    HUNT_CD_SECONDS,
    INTERACTION_CD_SECONDS,
    MainBeastMixin,
    beast_seconds_until,
    main_beast_default_state,
    main_beast_feedback_candidate,
)


ROSTER = """
@Weeguu 的灵兽伙伴们：

- 大圣 (休息中)
  - 种类: 六阶金刚巨猿
  - 品阶: 6阶, 等级: 100
  - 经验: 1200 / 2000
  - 战力: 59150
  - 灵性: 心情 100 | 体力: 35

- 铁甲龟 (休息中)
  - 种类: 二阶玄甲龟
  - 品阶: 2阶, 等级: 20
  - 经验: 200 / 2000
  - 战力: 850
  - 灵性: 心情 90 | 体力: 70

- 灵狐 (休息中)
  - 种类: 一阶灵狐
  - 品阶: 1阶, 等级: 1
  - 经验: 0 / 100
  - 战力: 20
  - 灵性: 心情 80 | 体力: 80
"""


class DummyMainBeast(MainBeastMixin):
    def __init__(self):
        self.state = main_beast_default_state()
        self.saved = 0

    def save_state(self):
        self.saved += 1

    def parse_wait_time(self, text, *args, **kwargs):
        import re
        clean = str(text or "").replace("**", "")
        h = re.search(r"(\d+)小时", clean)
        m = re.search(r"(\d+)分钟", clean)
        s = re.search(r"(\d+)秒", clean)
        if not any((h, m, s)):
            return -1
        return (int(h.group(1)) * 3600 if h else 0) + (int(m.group(1)) * 60 if m else 0) + (int(s.group(1)) if s else 0)


class MainBeastFeatureTests(unittest.TestCase):
    def setUp(self):
        self.actor = DummyMainBeast()
        self.assertTrue(self.actor.record_main_beast_roster(ROSTER, source="fixture"))

    def test_roster_parser_and_dynamic_focus(self):
        self.assertEqual(len(self.actor.state["beasts_cache"]), 3)
        self.assertEqual(self.actor.state["best_beast_name"], "大圣")
        self.assertEqual(self.actor.state["best_beast_stamina"], 35)

    def test_abyss_protects_low_stamina_focus_and_uses_tier_one(self):
        candidates = self.actor.main_beast_candidates("abyss")
        self.assertEqual([item["full_name"] for item in candidates], ["灵狐"])
        self.actor.main_beast_by_name("大圣")["stamina"] = 80
        candidates = self.actor.main_beast_candidates("abyss")
        self.assertEqual(candidates[0]["full_name"], "大圣")

    def test_patrol_excludes_focus_and_prefers_healthy_fallback(self):
        candidates = self.actor.main_beast_candidates("patrol")
        self.assertNotIn("大圣", [item["full_name"] for item in candidates])
        self.assertEqual(candidates[0]["full_name"], "灵狐")

    def test_manual_responses_record_real_cooldowns(self):
        self.assertTrue(self.actor.record_manual_beast_command_response(
            ".寻觅灵兽",
            "驯化成功！你与【灵狐】成功建立了契约。",
        ))
        self.assertGreaterEqual(beast_seconds_until(self.actor.state["next_hunt_time"]), HUNT_CD_SECONDS - 2)

        self.assertTrue(self.actor.record_manual_beast_command_response(
            ".灵兽互动 大圣 安抚",
            "【灵兽互动】灵契放松。心情 +17，羁绊 +6，忠诚 +5，体力 +10。",
        ))
        self.assertGreaterEqual(beast_seconds_until(self.actor.state["next_beast_interaction_time"]), INTERACTION_CD_SECONDS - 2)

        self.assertTrue(self.actor.record_manual_beast_command_response(
            ".灵兽巡边 铁甲龟 袭营",
            "【灵兽巡边】消耗体力 24，预计 75分钟 后归来。",
        ))
        self.assertGreaterEqual(beast_seconds_until(self.actor.state["next_beast_border_patrol_time"]), BORDER_PATROL_CD_SECONDS - 2)

        self.assertTrue(self.actor.record_manual_beast_command_response(
            ".探渊 灵狐",
            "险胜！灵兽【灵狐】成功击败了对手，获得战利品，需要休养1小时。",
        ))
        self.assertGreaterEqual(beast_seconds_until(self.actor.state["next_abyss_time"]), ABYSS_CD_SECONDS - 2)

    def test_pasture_and_return_update_cached_statuses(self):
        response = """【万兽奔腾】\n大圣、铁甲龟 等2只灵兽 欢快地冲入了万兽谷！\n它们将在 4 小时后自动归来。"""
        self.assertTrue(self.actor.record_main_pasture_response(response))
        self.assertEqual(self.actor.main_beast_by_name("大圣")["status"], "放养中")
        self.assertTrue(self.actor.record_main_pasture_return(
            "【灵兽归来】道友 @Weeguu，你放养的 2 只灵兽已归来。\n• 【大圣】：体力恢复 29"
        ))
        self.assertEqual(self.actor.main_beast_by_name("大圣")["status"], "休息中")

    def test_feedback_matching_covers_requested_commands(self):
        fixtures = {
            ".我的灵兽": "@Weeguu 的灵兽伙伴们",
            ".寻觅灵兽": "驯化成功！",
            ".探渊 灵狐": "灵兽正在与渊中妖兽搏杀",
            ".一键放养": "【万兽奔腾】",
            ".灵兽互动 大圣": "【灵兽互动】",
            ".灵兽巡边 铁甲龟 袭营": "【灵兽巡边】预计75分钟归来",
        }
        for command, text in fixtures.items():
            self.assertTrue(main_beast_feedback_candidate(command, text), command)

    def test_dashboard_main_panel_is_wanling_and_has_requested_entries(self):
        state = {**main_beast_default_state(), "done": [], "avatars": {}}
        panel = build_command_panels("main", state)[0]
        commands = {item["command"] for item in panel["commands"]}
        self.assertTrue({
            ".寻觅灵兽", ".探渊 <灵兽>", ".一键放养",
            ".灵兽互动 <重点灵兽>", ".灵兽巡边 <灵兽> 袭营",
        }.issubset(commands))
        self.assertNotIn(".登天阶", commands)
        self.assertNotIn(".引九天罡风", commands)

    def test_main_account_initializes_wanling_runtime_and_migrates_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state_main.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({"sect_name": "凌霄宫", "avatars": {}}, handle, ensure_ascii=False)
            config = {
                "api_id": 1,
                "api_hash": "fixture",
                "monitor": {"xiaohao_visibility_control": False},
            }
            with patch.object(intelligent_cultivator, "STATE_FILE", state_file), patch.object(
                intelligent_cultivator, "load_config", return_value=config
            ), patch.object(intelligent_cultivator, "TelegramClient", return_value=object()):
                actor = intelligent_cultivator.Cultivator(session_name="fixture")

            self.assertEqual(actor.sect_name, "万灵宗")
            self.assertEqual(actor.identity_sect_names["主魂"], "万灵宗")
            self.assertFalse(actor.lingxiao_enabled)
            self.assertFalse(actor.enable_spirit_tree)
            self.assertTrue(actor.enable_main_beasts)
            self.assertEqual(actor.state["sect_name"], "万灵宗")
            self.assertIn("next_abyss_time", actor.state)
            self.assertIn("next_beast_border_patrol_time", actor.state)


if __name__ == "__main__":
    unittest.main()
