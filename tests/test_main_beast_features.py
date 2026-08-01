import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import intelligent_cultivator
import dashboard_server
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
        self.config = {}
        self.client = object()
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

    def test_patrol_uses_dashboard_route_and_keeps_automatic_beast_selection(self):
        self.actor.dashboard_command_option = lambda *args, **kwargs: "斥候"
        self.actor.update_main_beast_cache = AsyncMock(return_value=True)
        self.actor.normalize_main_beast_for_action = AsyncMock(return_value=True)
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            return "灵兽【灵狐】领命前往边境巡行，执行【斥候】。"

        self.actor.send_and_wait_feedback = send

        self.assertTrue(asyncio.run(self.actor.execute_main_beast_patrol()))
        self.assertEqual(sent, [".灵兽巡边 灵狐 斥候"])
        self.assertEqual(self.actor.state["beast_border_patrol_name"], "灵狐")
        self.assertEqual(self.actor.state["beast_border_patrol_mode"], "斥候")

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
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            dashboard_server, "CONFIG_DIR", tmpdir
        ):
            panel = build_command_panels("main", state)[0]
        commands = {item["command"] for item in panel["commands"]}
        self.assertTrue({
            ".寻觅灵兽",
            "miniapp:spirit-beast-contract", "miniapp:spirit-beast-abyss",
            ".灵兽巡边 <灵兽> 袭营",
        }.issubset(commands))
        self.assertNotIn(".探渊 <灵兽>", commands)
        self.assertNotIn(".一键放养", commands)
        self.assertNotIn(".灵兽互动 <重点灵兽>", commands)
        self.assertNotIn(".登天阶", commands)
        self.assertNotIn(".引九天罡风", commands)
        self.assertNotIn(".我的灵兽", commands)
        miniapp = next(item for item in panel["commands"] if item["command"] == "miniapp:spirit-beast")
        self.assertEqual(miniapp["dashboard_action"], "miniapp-beast-refresh")
        patrol = next(item for item in panel["commands"] if item.get("control_key") == ".灵兽巡边 *")
        self.assertEqual(patrol["patrol_mode_options"], ["斥候", "护粮", "袭营"])
        self.assertEqual(patrol["patrol_mode_value"], "袭营")

    def test_miniapp_sync_replaces_deprecated_roster_command(self):
        self.actor.config = {
            "miniapp_beast": {
                "enabled": True,
                "entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
            }
        }
        snapshot = {
            "spirit_token": "spiritbeast_fixture",
            "player": {"daoName": "测试"},
            "beasts": [{
                "id": 1,
                "full_name": "新灵狐",
                "status": "休息中",
                "species": "1阶灵狐",
                "tier": 1,
                "level": 4,
                "power": 30,
                "stamina": 92,
                "exp": 12,
            }],
        }
        with patch("main_beast_features.fetch_miniapp_beast_snapshot", new=AsyncMock(return_value=snapshot)) as fetch:
            self.assertTrue(asyncio.run(self.actor.update_main_beast_cache(force=True)))
        fetch.assert_awaited_once()
        self.assertEqual(self.actor.state["beasts_cache"][0]["full_name"], "新灵狐")
        self.assertEqual(self.actor.state["beast_roster_last_source"], "miniapp")
        self.assertEqual(self.actor.state["beast_miniapp_last_error"], "")
        self.assertEqual(self.actor.state["beast_roster_auto_query_count"], 0)

    def test_automatic_miniapp_sync_is_limited_to_twice_per_day(self):
        self.actor.config = {
            "miniapp_beast": {
                "enabled": True,
                "entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
                "refresh_seconds": 1800,
            }
        }
        snapshot = {
            "spirit_token": "spiritbeast_fixture",
            "player": {},
            "beasts": [{
                "full_name": "新灵狐",
                "status": "休息中",
                "species": "1阶灵狐",
                "tier": 1,
                "power": 30,
                "stamina": 92,
            }],
        }
        fetch = AsyncMock(return_value=snapshot)
        with patch("main_beast_features.fetch_miniapp_beast_snapshot", new=fetch):
            self.actor.state["next_beast_status_check_time"] = ""
            self.assertTrue(asyncio.run(self.actor.update_main_beast_cache()))
            self.actor.state["next_beast_status_check_time"] = ""
            self.assertTrue(asyncio.run(self.actor.update_main_beast_cache()))
            self.actor.state["next_beast_status_check_time"] = ""
            self.assertTrue(asyncio.run(self.actor.update_main_beast_cache()))

        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(self.actor.state["beast_roster_auto_query_count"], 2)
        reset = datetime.strptime(
            self.actor.state["next_beast_status_check_time"],
            "%Y-%m-%d %H:%M:%S",
        )
        self.assertEqual((reset.hour, reset.minute), (0, 5))

    def test_automatic_miniapp_quota_resets_on_a_new_day(self):
        self.actor.config = {
            "miniapp_beast": {
                "enabled": True,
                "entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
            }
        }
        self.actor.state.update({
            "beast_roster_auto_query_date": "2000-01-01",
            "beast_roster_auto_query_count": 2,
            "next_beast_status_check_time": "",
        })
        snapshot = {
            "spirit_token": "spiritbeast_fixture",
            "player": {},
            "beasts": [{
                "full_name": "新灵狐",
                "status": "休息中",
                "species": "1阶灵狐",
                "tier": 1,
                "power": 30,
                "stamina": 92,
            }],
        }
        with patch(
            "main_beast_features.fetch_miniapp_beast_snapshot",
            new=AsyncMock(return_value=snapshot),
        ) as fetch:
            self.assertTrue(asyncio.run(self.actor.update_main_beast_cache()))
        fetch.assert_awaited_once()
        self.assertEqual(self.actor.state["beast_roster_auto_query_count"], 1)

    def test_failed_miniapp_sync_never_reuses_stale_roster_for_actions(self):
        self.actor.config = {
            "miniapp_beast": {
                "enabled": True,
                "entry_url": "https://t.me/fanrenxiuxian_bot?startapp=df_fixture",
            }
        }
        self.actor.state.update({
            "beasts_cache": [{"full_name": "旧灵兽", "stamina": 100}],
            "beast_roster_last_source": "miniapp",
            "beast_miniapp_last_error": "external_action_unavailable",
            "next_beast_status_check_time": "2999-01-01 00:00:00",
        })
        with patch("main_beast_features.fetch_miniapp_beast_snapshot", new=AsyncMock()) as fetch:
            self.assertFalse(asyncio.run(self.actor.update_main_beast_cache()))
        fetch.assert_not_awaited()

    def test_disabled_miniapp_never_falls_back_to_cached_roster(self):
        self.actor.config = {}
        self.actor.state.update({
            "beasts_cache": [{"full_name": "旧灵兽", "stamina": 100}],
            "next_beast_status_check_time": "2999-01-01 00:00:00",
        })
        self.assertFalse(asyncio.run(self.actor.update_main_beast_cache()))
        self.assertEqual(self.actor.state["beast_miniapp_last_error"], "miniapp_not_configured")

    def test_deprecated_roster_deadline_is_not_priority_work_for_main(self):
        actor = intelligent_cultivator.Cultivator.__new__(intelligent_cultivator.Cultivator)
        actor.state = {"next_beast_status_check_time": "2000-01-01 00:00:00"}
        actor.avatars = []
        actor.identity_pause_seconds = lambda identity: 0
        actor.state_time_command_paused = lambda key, identity="": False
        self.assertFalse(actor.priority_due_work_summary())

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
