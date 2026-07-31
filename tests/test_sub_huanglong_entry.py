import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sub_cultivator
from sub_cultivator import SubCultivator


class _Event:
    def __init__(self, message, sender):
        self.message = message
        self._sender = sender

    async def get_sender(self):
        return self._sender


class SubHuanglongEntryTests(unittest.TestCase):
    def test_sub_identity_sect_mapping_includes_luoyun_xunzhenzi(self):
        with patch.object(
            sub_cultivator,
            "load_config",
            return_value={"api_id": 1, "api_hash": "fixture", "monitor": {}},
        ), patch.object(sub_cultivator, "TelegramClient"), patch.object(
            SubCultivator, "load_state", return_value={}
        ), patch.object(SubCultivator, "save_state"):
            actor = SubCultivator(session_name="fixture_sub_huanglong")

        self.assertEqual(
            actor.identity_sect_names,
            {
                "主魂": "元婴宗",
                "厚土": "星宫",
                "缘生子": "阴罗宗",
                "寻真子": "落云宗",
            },
        )
        self.assertEqual(actor.huanglong_identities_for_sect("落云宗"), ["寻真子"])

    def test_sub_new_message_routes_huanglong_report_to_common_trigger(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.target_chat_id = -100123456
        actor.watch_bot = "fanrenxiuxian_bot"
        actor.feedback_events = {}
        actor.state = {}
        actor.record_star_gazing_final_report_if_needed = Mock()
        actor.record_star_shift_attempt_if_needed = Mock()
        actor.maybe_record_main_yuanying_retreat_settlement_reply = Mock(return_value=False)
        actor.update_identity_passively = Mock()
        actor.maybe_record_avatar_passive_states = Mock()
        actor.maybe_record_fishing_rod_message = AsyncMock()
        actor.maybe_alert_low_price_tianleizhu = AsyncMock()
        actor.should_send_keyword_alert = Mock(return_value=False)
        actor.maybe_record_field_training_passive = Mock()
        actor.maybe_handle_sect_war_message = Mock(return_value=True)

        report = (
            "【黄龙山轮值军报】\n"
            "今日黄龙山前线轮值宗门为【落云宗】。\n"
            "轮值宗门弟子可在 14:00 前使用 .报名黄龙山 报名。"
        )
        message = SimpleNamespace(
            id=578380,
            text=report,
            sender_id=777,
            chat_id=actor.target_chat_id,
            reply_to=None,
        )
        sender = SimpleNamespace(id=777, username="fanrenxiuxian_bot")
        event = _Event(message, sender)

        with patch.object(sub_cultivator, "log_mention_if_needed"), patch.object(
            sub_cultivator, "record_message_event"
        ), patch.object(sub_cultivator, "record_star_gazing_event"), patch.object(
            sub_cultivator, "log_manual_outgoing_if_needed", return_value=False
        ), patch.object(
            sub_cultivator, "maybe_handle_han_soul_choice", AsyncMock(return_value=False)
        ), patch.object(
            sub_cultivator, "is_game_bot_sender", return_value=True
        ), patch.object(sub_cultivator, "record_game_bot_activity"), patch.object(
            sub_cultivator, "is_reply_to_manual_command", return_value=False
        ), patch.object(
            sub_cultivator,
            "record_manual_command_reply_state_if_needed",
            AsyncMock(return_value=False),
        ), patch.object(
            sub_cultivator, "handle_anti_bot_challenge", AsyncMock(return_value=False)
        ), patch.object(sub_cultivator, "is_auto_reply_followup", return_value=True):
            asyncio.run(actor.handle_game_response(event))

        actor.maybe_handle_sect_war_message.assert_called_once_with(message, report, sender)


if __name__ == "__main__":
    unittest.main()
