import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sky_bottle_features import (
    SkyBottleMixin,
    SKY_BOTTLE_CONDENSE_COMMAND,
    SKY_BOTTLE_NURTURE_COMMAND,
    sky_bottle_default_state,
)


class _Actor(SkyBottleMixin):
    def __init__(self):
        self.state = {}
        self.expected_username = "Weeguu"
        self.saved = 0
        self.sent = []

    def parse_wait_time(self, text, find_min=False, line_identifier=None):
        # 与 intelligent_cultivator.Cultivator.parse_wait_time 同构的最小实现：
        # 清理 markdown 后按 小时/分钟/秒 汇总
        import re as _re
        if not text:
            return -1
        clean = text.replace("**", "").replace(" ", "")
        total = 0
        h = _re.search(r"(\d+)(?:小时|h)", clean)
        m = _re.search(r"(\d+)(?:分钟|分|m)", clean)
        s = _re.search(r"(\d+)(?:秒|s)", clean)
        if h:
            total += int(h.group(1)) * 3600
        if m:
            total += int(m.group(1)) * 60
        if s:
            total += int(s.group(1))
        return total if (h or m or s) else -1

    def save_state(self):
        self.saved += 1

    async def send_and_wait_feedback(self, command, **kwargs):
        self.sent.append(command)
        resp = MagicMock()
        resp.text = self.next_response
        return resp


class SkyBottleCondenseTests(unittest.TestCase):
    def _run(self, actor, coro):
        return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)

    def test_condense_success_sets_state(self):
        actor = _Actor()
        actor.next_response = (
            "**【掌天瓶·凝液】**\n你默运法诀，引来一缕月华沉入瓶中。\n"
            "当前绿液：**1/1**\n此液已可留待后续药园、星台等法门施用。"
        )
        wait = self._run(actor, actor.sky_bottle_condense())
        state = actor.get_sky_bottle_state()
        self.assertEqual(state["liquid_count"], 1)
        self.assertEqual(state["last_status"], "condense_success")
        self.assertTrue(state["next_condense_time"])
        self.assertEqual(wait, 5)

    def test_condense_cooldown_parsed(self):
        actor = _Actor()
        actor.next_response = "瓶中月华尚未再度圆满，请在 **2小时49分钟24秒** 后再试。"
        wait = self._run(actor, actor.sky_bottle_condense())
        state = actor.get_sky_bottle_state()
        self.assertEqual(state["last_status"], "condense_cooldown")
        # 2h49m24s = 10164s + 300s buffer
        self.assertEqual(wait, 10164 + 300)
        self.assertTrue(state["next_condense_time"])

    def test_condense_heaven_ban_disables(self):
        actor = _Actor()
        actor.next_response = "【天道禁制】 这只【掌天瓶】的魂印缺失或不属于你，已被冻结。"
        actor.sky_bottle_notify_user = AsyncMock()
        wait = self._run(actor, actor.sky_bottle_condense())
        state = actor.get_sky_bottle_state()
        self.assertEqual(state["disabled_reason"], "天道禁制（魂印冻结）")
        self.assertFalse(actor.sky_bottle_enabled())
        actor.sky_bottle_notify_user.assert_awaited_once()

    def test_nurture_success_clears_embryo(self):
        actor = _Actor()
        state = actor.get_sky_bottle_state()
        state["has_tree_embryo"] = True
        state["liquid_count"] = 1
        actor.next_response = (
            "**【掌天瓶·养树】**\n你将灵眼树胚封入玉盒。\n"
            "你消耗了 【灵眼树胚】x1 与 **1** 滴掌天绿液，最终炼成了 **【一截灵眼之树】**！\n"
            "当前绿液：**0/1**"
        )
        wait = self._run(actor, actor.sky_bottle_nurture())
        state = actor.get_sky_bottle_state()
        self.assertFalse(state["has_tree_embryo"])
        self.assertEqual(state["liquid_count"], 0)
        self.assertEqual(state["last_status"], "nurture_success")

    def test_nurture_skipped_without_embryo(self):
        actor = _Actor()
        result = self._run(actor, actor.sky_bottle_nurture())
        self.assertIsNone(result)
        self.assertEqual(actor.sent, [])

    def test_embryo_passive_capture(self):
        actor = _Actor()
        text = "树胚机缘：**@Weeguu** 获得 **【灵眼树胚】x1**。\n树胚可用 `.掌天瓶 养树` 培养成 **【一截灵眼之树】**。"
        self.assertTrue(actor.sky_bottle_set_embryo(text))
        state = actor.get_sky_bottle_state()
        self.assertTrue(state["has_tree_embryo"])

    def test_embryo_capture_ignores_other_user(self):
        actor = _Actor()
        text = "树胚机缘：**@saime428** 获得 **【灵眼树胚】x1**。"
        self.assertFalse(actor.sky_bottle_set_embryo(text))
        state = actor.get_sky_bottle_state()
        self.assertFalse(state["has_tree_embryo"])

    def test_tick_condenses_when_due(self):
        actor = _Actor()
        actor.next_response = "**【掌天瓶·凝液】**\n当前绿液：**1/1**"
        wait = self._run(actor, actor.sky_bottle_tick())
        self.assertEqual(actor.sent, [SKY_BOTTLE_CONDENSE_COMMAND])
        state = actor.get_sky_bottle_state()
        self.assertEqual(state["liquid_count"], 1)

    def test_tick_nurture_first_with_embryo(self):
        actor = _Actor()
        state = actor.get_sky_bottle_state()
        state["has_tree_embryo"] = True
        state["liquid_count"] = 1
        actor.next_response = "**【掌天瓶·养树】**\n炼成了 **【一截灵眼之树】**！\n当前绿液：**0/1**"
        wait = self._run(actor, actor.sky_bottle_tick())
        self.assertEqual(actor.sent, [SKY_BOTTLE_NURTURE_COMMAND])

    def test_manual_response_sync(self):
        actor = _Actor()
        actor.next_response = ""
        ok = actor.record_sky_bottle_manual_response(
            SKY_BOTTLE_CONDENSE_COMMAND,
            "**【掌天瓶·凝液】**\n当前绿液：**1/1**",
        )
        self.assertTrue(ok)
        state = actor.get_sky_bottle_state()
        self.assertEqual(state["liquid_count"], 1)
        self.assertEqual(state["last_status"], "condense_success")


if __name__ == "__main__":
    unittest.main()
