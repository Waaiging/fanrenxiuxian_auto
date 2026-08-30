import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from log_utils import (
    command_response_family,
    feedback_response_matches_command,
    text_response_family,
)


class SkyBottleFeedbackFamilyTests(unittest.TestCase):
    """掌天瓶指令的反馈家族匹配（修复：不再误归 star 家族导致超时）。"""

    def test_command_family_is_sky_bottle(self):
        self.assertEqual(command_response_family(".掌天瓶 凝液"), "sky_bottle")
        self.assertEqual(command_response_family(".掌天瓶 养树"), "sky_bottle")
        self.assertEqual(command_response_family(".掌天瓶"), "sky_bottle")

    def test_command_family_star_intact(self):
        # 观星家族不受影响
        self.assertEqual(command_response_family(".牵引星辰 北斗"), "star")
        self.assertEqual(command_response_family(".观星台"), "star")

    def test_cooldown_text_family(self):
        text = "瓶中月华尚未再度圆满，请在 **5小时55分钟7秒** 后再试。"
        self.assertEqual(text_response_family(text), "sky_bottle")

    def test_condense_success_text_family(self):
        text = "**【掌天瓶·凝液】**\n你默运法诀，引来一缕月华沉入瓶中。\n当前绿液：**1/1**"
        self.assertEqual(text_response_family(text), "sky_bottle")

    def test_nurture_success_text_family(self):
        text = "**【掌天瓶·养树】**\n你消耗了 【灵眼树胚】x1 与 **1** 滴掌天绿液，最终炼成了 **【一截灵眼之树】**！"
        self.assertEqual(text_response_family(text), "sky_bottle")

    def test_positive_match(self):
        self.assertTrue(
            feedback_response_matches_command(
                ".掌天瓶 凝液", "瓶中月华尚未再度圆满，请在 **22分钟2秒** 后再试。"
            )
        )
        self.assertTrue(
            feedback_response_matches_command(
                ".掌天瓶 养树", "**【掌天瓶·养树】** 炼成了 **【一截灵眼之树】**！"
            )
        )

    def test_no_conflict_with_star(self):
        # 掌天瓶指令不会与观星文本冲突（不同家族返回 True=不冲突）
        from log_utils import feedback_response_conflicts

        self.assertFalse(
            feedback_response_conflicts(".掌天瓶 凝液", "星轨运行变化，需等待良辰。")
            or text_response_family("星轨运行变化，需等待良辰。") == "sky_bottle"
        )


if __name__ == "__main__":
    unittest.main()
