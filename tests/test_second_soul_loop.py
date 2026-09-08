"""第二元神修炼循环测试。

这些用例直接驱动 `CommonCommandMixin.run_second_soul_loop`，而不是只测试
`command_feedback` 里的解析函数。回归重点：`send_and_wait_feedback` 默认返回
纯字符串，早期实现用 `getattr(response, "text", "")` 读取，永远得到空串，
导致"无法分心修炼"分支从未触发、冷却一律顺延 24 小时。
"""

import asyncio
import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import common_command_features
from common_command_features import (
    SECOND_SOUL_COOLDOWN_BUFFER_SECONDS,
    SECOND_SOUL_INTERVAL_SECONDS,
    SECOND_SOUL_RECHECK_SECONDS,
    SECOND_SOUL_STATUS_COMMAND,
    SECOND_SOUL_TRAIN_COMMAND,
    TIME_FORMAT,
    CommonCommandMixin,
)

# 线上实际回复文本（取自 VPS 日志）
BUSY_REPLY = "你的第二元神正在(修炼中)，无法分心修炼。"
STATUS_REPLY = (
    "**【你的第二元神：金之元神】**\n"
    "**状态**: 修炼中 (剩余: 14小时11分钟36秒)\n"
    "**等级**: 3\n"
)
STATUS_REMAINING = 14 * 3600 + 11 * 60 + 36  # 51096
SUCCESS_REPLY = "你的第二元神开始修炼，24小时后可再次修炼。"


class _StubActor(CommonCommandMixin):
    """只提供 run_second_soul_loop 所需的最小运行时。"""

    def __init__(self, responses, state=None):
        self.state = dict(state or {})
        self.startup_done = asyncio.Event()
        self.startup_done.set()
        self._responses = list(responses)
        self.sent = []
        self.save_calls = 0
        # 只跑一轮循环体，随后退出
        self._running = iter([True, False])

    @property
    def is_running(self):
        return next(self._running, False)

    def save_state(self):
        self.save_calls += 1

    def common_command_logger(self):
        return logging.getLogger("second-soul-test")

    async def send_and_wait_feedback(self, command, **kwargs):
        self.sent.append(command)
        # 真实实现返回纯字符串，这里保持一致
        return self._responses.pop(0) if self._responses else ""


class SecondSoulLoopTests(unittest.TestCase):
    def _run(self, actor):
        with patch.object(common_command_features.asyncio, "sleep", new=AsyncMock()):
            asyncio.run(actor.run_second_soul_loop())

    def _assert_next_within(self, actor, expected_seconds, tolerance=60):
        recorded = datetime.strptime(actor.state["next_second_soul_time"], TIME_FORMAT)
        expected = datetime.now() + timedelta(seconds=expected_seconds)
        delta = abs((recorded - expected).total_seconds())
        self.assertLessEqual(
            delta,
            tolerance,
            f"next_second_soul_time={recorded} 偏离期望 {expected} 达 {delta}s",
        )

    def test_busy_reply_schedules_from_parsed_cooldown(self):
        """收到"无法分心修炼"后应查询 .第二元神 并按真实剩余时间排程。"""
        actor = _StubActor([BUSY_REPLY, STATUS_REPLY])
        self._run(actor)

        self.assertEqual(actor.sent, [SECOND_SOUL_TRAIN_COMMAND, SECOND_SOUL_STATUS_COMMAND])
        self._assert_next_within(
            actor, STATUS_REMAINING + SECOND_SOUL_COOLDOWN_BUFFER_SECONDS
        )
        # 关键回归：绝不能顺延满 24 小时
        self.assertLess(
            datetime.strptime(actor.state["next_second_soul_time"], TIME_FORMAT),
            datetime.now() + timedelta(seconds=SECOND_SOUL_INTERVAL_SECONDS),
        )
        self.assertTrue(actor.save_calls)

    def test_busy_reply_with_unparsable_status_schedules_short_recheck(self):
        actor = _StubActor([BUSY_REPLY, "查询失败，请稍后再试。"])
        self._run(actor)

        self.assertEqual(actor.sent, [SECOND_SOUL_TRAIN_COMMAND, SECOND_SOUL_STATUS_COMMAND])
        self._assert_next_within(actor, SECOND_SOUL_RECHECK_SECONDS)

    def test_successful_training_skips_status_query(self):
        actor = _StubActor([SUCCESS_REPLY])
        self._run(actor)

        self.assertEqual(actor.sent, [SECOND_SOUL_TRAIN_COMMAND])
        self._assert_next_within(actor, SECOND_SOUL_INTERVAL_SECONDS)

    def test_empty_response_schedules_full_interval(self):
        actor = _StubActor([""])
        self._run(actor)

        self.assertEqual(actor.sent, [SECOND_SOUL_TRAIN_COMMAND])
        self._assert_next_within(actor, SECOND_SOUL_INTERVAL_SECONDS)

    def test_future_schedule_waits_without_sending(self):
        """未到期时只等待，不应发出任何指令。"""
        future = (datetime.now() + timedelta(hours=5)).strftime(TIME_FORMAT)
        actor = _StubActor([BUSY_REPLY], state={"next_second_soul_time": future})
        self._run(actor)

        self.assertEqual(actor.sent, [])
        self.assertEqual(actor.state["next_second_soul_time"], future)

    def test_corrupt_schedule_is_repaired_without_sending(self):
        """时间戳损坏时先修复排程再等待。

        注意与"字段缺失"的差异：缺失会立即发送，损坏则重置为 +24h 后等待。
        这是原实现的既有行为，本次重构刻意保留，未作变更。
        """
        actor = _StubActor([SUCCESS_REPLY], state={"next_second_soul_time": "not-a-time"})
        self._run(actor)

        self.assertEqual(actor.sent, [])
        self._assert_next_within(actor, SECOND_SOUL_INTERVAL_SECONDS)

    def test_missing_schedule_sends_immediately(self):
        actor = _StubActor([SUCCESS_REPLY], state={})
        self._run(actor)

        self.assertEqual(actor.sent, [SECOND_SOUL_TRAIN_COMMAND])
        self._assert_next_within(actor, SECOND_SOUL_INTERVAL_SECONDS)

    def test_message_object_response_is_also_handled(self):
        """Bot 有时返回消息对象；两种形态都要能识别忙碌状态。"""

        class _Msg:
            text = BUSY_REPLY

        class _StatusMsg:
            text = STATUS_REPLY

        actor = _StubActor([_Msg(), _StatusMsg()])
        self._run(actor)

        self.assertEqual(actor.sent, [SECOND_SOUL_TRAIN_COMMAND, SECOND_SOUL_STATUS_COMMAND])
        self._assert_next_within(
            actor, STATUS_REMAINING + SECOND_SOUL_COOLDOWN_BUFFER_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
