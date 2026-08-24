import asyncio
import unittest
from unittest.mock import patch

from common_command_features import CommonCommandMixin
from automation_settings import current_xiaohao_taiyi_identity
from wind_thunder_features import wind_thunder_send, wind_thunder_target_cooldown


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
        return "完成"

    def save_state(self):
        self.saved += 1


class RequestedFeatureUpdatesTests(unittest.TestCase):
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

    def test_wind_thunder_cooldowns_match_requested_minutes_exactly(self):
        self.assertEqual(wind_thunder_target_cooldown(".寻觅灵兽", 6 * 3600), 252 * 60)
        self.assertEqual(wind_thunder_target_cooldown(".问道", 12 * 3600), 504 * 60)
        self.assertEqual(wind_thunder_target_cooldown(".探寻裂缝", 12 * 3600), 540 * 60)


if __name__ == "__main__":
    unittest.main()
