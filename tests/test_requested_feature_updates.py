import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from common_command_features import CommonCommandMixin
import log_utils
import automation_settings as settings_module
from automation_settings import current_xiaohao_taiyi_identity
from wind_thunder_features import recover_wind_thunder_sessions, wind_thunder_send, wind_thunder_target_cooldown


def _state(actor, identity):
    return actor.identity_state_for_timed_command(identity)


class FeatureActor(CommonCommandMixin):
    def __init__(self, account="sub"):
        self.account_key = account
        self.state = {"identity_sect_names": {"主魂": "元婴宗"}}
        self.identity_sect_names = {"主魂": "元婴宗"}
        self.sect_name = "元婴宗"
        self.calls = []
        self.saved = 0

    async def send_and_wait_feedback(self, command, **kwargs):
        self.calls.append(command)
        if command == ".装备 风雷翅":
            return "你已祭出【风雷翅】。"
        if command == ".散念 风雷翅":
            return "你已散去对【风雷翅】的祭炼联系。"
        if command == ".上架至万宝阁 风雷翅":
            return "你已将【风雷翅】放置在万宝阁的展台上。"
        return "完成"

    def save_state(self):
        self.saved += 1


class RequestedFeatureUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "automation_settings.json"
        self.file_patch = patch.object(settings_module, "AUTOMATION_SETTINGS_FILE", self.path)
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.tempdir.cleanup()

    def test_wind_thunder_defaults_to_disabled_and_dashboard_can_enable_main_wujiu(self):
        value = settings_module.load_automation_settings()
        self.assertEqual(value["wind_thunder"], {"enabled": False, "participants": []})
        settings_module.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            wind_thunder_enabled=True,
            wind_thunder_participants=["main|无咎子"],
        )
        value = settings_module.load_automation_settings()
        self.assertTrue(value["wind_thunder"]["enabled"])
        self.assertEqual(value["wind_thunder"]["participants"], ["main|无咎子"])
        payload = settings_module.automation_dashboard_payload()
        accounts = {item["key"]: item for item in payload["wind_thunder"]["accounts"]}
        self.assertEqual(
            [item["name"] for item in accounts["main"]["identities"]],
            ["主魂", "无咎子"],
        )

    def test_sect_reply_retires_old_membership_and_enables_new_one(self):
        actor = FeatureActor()
        actor.sync_identity_sect_from_text("主魂", "已叛出宗门，斩断与【元婴宗】尘缘")
        self.assertEqual(actor.identity_sect_name("主魂"), "散修")
        actor.sync_identity_sect_from_text("主魂", "恭喜成功拜入【天星宗】")
        self.assertEqual(actor.identity_sect_name("主魂"), "天星宗")
        self.assertTrue(actor.tianxing_identity_enabled("主魂"))

    def test_wind_thunder_reuses_one_session_and_cleans_up_once(self):
        async def scenario():
            actor = FeatureActor("sub")
            settings_module.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                wind_thunder_enabled=True,
                wind_thunder_participants=["sub|主魂"],
            )
            with patch("wind_thunder_features.WIND_THUNDER_HOLD_SECONDS", 60):
                await wind_thunder_send(
                    actor,
                    "主魂",
                    ".探寻裂缝",
                    lambda: actor.send_and_wait_feedback(".探寻裂缝"),
                )
                await wind_thunder_send(
                    actor,
                    "主魂",
                    ".问道",
                    lambda: actor.send_and_wait_feedback(".问道"),
                )
                await asyncio.sleep(0.01)
            return actor.calls

        calls = asyncio.run(scenario())
        self.assertEqual(calls, [".从万宝阁取下 风雷翅", ".装备 风雷翅", ".探寻裂缝", ".问道"])

    def test_xiaohao_rebirth_name_prefers_authoritative_miniapp_avatar_field(self):
        state = {
            "avatars": {
                "缘生子": {
                    "miniapp_player_id": -1003996748766,
                    "miniapp_dao_name": "灵脉玄",
                }
            }
        }
        self.assertEqual(current_xiaohao_taiyi_identity(state), "灵脉玄")

    def test_wind_thunder_second_command_reuses_equipment_before_reassessment(self):
        async def scenario():
            actor = FeatureActor("sub")
            settings_module.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                wind_thunder_enabled=True,
                wind_thunder_participants=["sub|主魂"],
            )
            with patch("wind_thunder_features.WIND_THUNDER_HOLD_SECONDS", 60):
                await wind_thunder_send(
                    actor,
                    "主魂",
                    ".探寻裂缝",
                    lambda: actor.send_and_wait_feedback(".探寻裂缝"),
                )
                first_due = _state(actor, "主魂")["wind_thunder_cleanup_due_at"]
                await wind_thunder_send(
                    actor,
                    "主魂",
                    ".问道",
                    lambda: actor.send_and_wait_feedback(".问道"),
                )
                second_due = _state(actor, "主魂")["wind_thunder_cleanup_due_at"]
            return actor.calls, first_due, second_due

        calls, first_due, second_due = asyncio.run(scenario())
        self.assertEqual(calls.count(".装备 风雷翅"), 1)
        self.assertEqual(datetime.strptime(first_due, "%Y-%m-%d %H:%M:%S"), datetime.strptime(second_due, "%Y-%m-%d %H:%M:%S"))

    def test_wind_thunder_recovery_schedules_future_cleanup(self):
        async def scenario():
            actor = FeatureActor("sub")
            settings_module.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                wind_thunder_enabled=True,
                wind_thunder_participants=["sub|主魂"],
            )
            state = _state(actor, "主魂")
            state["wind_thunder_equipped"] = True
            state["wind_thunder_equipped_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["wind_thunder_cleanup_due_at"] = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
            recover_wind_thunder_sessions(actor)
            task = actor._wind_thunder_cleanup_tasks["主魂"]
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_market_feedback_is_not_retried_as_fishing(self):
        command = ".上架至万宝阁 风雷翅"
        self.assertEqual(log_utils.command_response_family(command), "market")
        self.assertEqual(log_utils.text_response_family("你已将【风雷翅】郑重地放置在万宝阁的展台上。"), "market")
        self.assertTrue(log_utils.feedback_response_matches_command(command, "你已将【风雷翅】郑重地放置在万宝阁的展台上。"))
    def test_wind_thunder_cooldowns_match_requested_minutes_exactly(self):
        self.assertEqual(wind_thunder_target_cooldown(".寻觅灵兽", 6 * 3600), 252 * 60)
        self.assertEqual(wind_thunder_target_cooldown(".问道", 12 * 3600), 504 * 60)
        self.assertEqual(wind_thunder_target_cooldown(".探寻裂缝", 12 * 3600), 540 * 60)


if __name__ == "__main__":
    unittest.main()
