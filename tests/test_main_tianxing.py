import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from intelligent_cultivator import Cultivator


class MainTianxingTests(unittest.TestCase):
    @staticmethod
    def actor():
        actor = Cultivator.__new__(Cultivator)
        actor.sect_name = "天星宗"
        actor.identity_sect_names = {"主魂": "天星宗"}
        actor.state = {
            "last_destiny_date": "",
            "next_tianxing_destiny_retry_time": "",
            "last_destiny_observation_date": "",
            "tianxing_destiny_options": [],
            "tianxing_destiny_options_date": "",
        }
        actor.active_atomic_task = None
        actor.save_state = lambda: None
        actor.response_text = lambda response: str(response or "")
        actor.dashboard_command_paused = lambda command, identity: False
        actor.daily_one_shot_should_defer = lambda *args, **kwargs: False
        return actor

    def test_main_meditation_uses_tianxing_prefix(self):
        actor = self.actor()
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            if command == ".观命":
                return "今日可定命的命星：紫微、贪狼、太阴"
            if command == ".定命 紫微":
                return "今日命轨定在紫微"
            return "执行成功"

        actor.send_and_wait_feedback = send

        async def run():
            with patch("intelligent_cultivator.asyncio.sleep", new=AsyncMock()):
                return await actor.send_main_meditation_settlement()

        self.assertEqual(asyncio.run(run()), "执行成功")
        self.assertEqual(sent, [".观命", ".定命 紫微", ".推命 闭关", ".闭关修炼"])

    def test_main_meditation_continues_when_tianxing_prefix_is_pending(self):
        actor = self.actor()
        actor.state.update(
            {
                "last_destiny_observation_date": datetime.now().strftime("%Y-%m-%d"),
                "tianxing_destiny_options": ["紫微", "贪狼", "太阴"],
                "tianxing_destiny_options_date": datetime.now().strftime("%Y-%m-%d"),
                "last_destiny_date": datetime.now().strftime("%Y-%m-%d"),
                "last_destiny_choice": "紫微",
            }
        )
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            if command == ".推命 闭关":
                return "你已有一道关于【探索】的推命尚未应验，还需等待 5 分钟。"
            return "闭关成功，获得修为，需要调息 10 分钟"

        actor.send_and_wait_feedback = send

        async def run():
            with patch("intelligent_cultivator.asyncio.sleep", new=AsyncMock()):
                return await actor.send_main_meditation_settlement()

        self.assertIn("闭关成功", asyncio.run(run()))
        self.assertEqual(sent, [".推命 闭关", ".闭关修炼"])

    def test_main_meditation_defers_on_standalone_prefix_cooldown(self):
        actor = self.actor()
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state.update(
            {
                "last_destiny_observation_date": today,
                "tianxing_destiny_options": ["紫微", "贪狼", "太阴"],
                "tianxing_destiny_options_date": today,
                "last_destiny_date": today,
                "last_destiny_choice": "紫微",
            }
        )
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            return "司命盘尚在冷却，还需等待 5 分钟。"

        actor.send_and_wait_feedback = send

        async def run():
            with patch("intelligent_cultivator.asyncio.sleep", new=AsyncMock()):
                return await actor.send_main_meditation_settlement()

        self.assertIsNone(asyncio.run(run()))
        self.assertEqual(sent, [".推命 闭关"])
        self.assertTrue(actor.state.get("next_meditation_retry_time"))
        self.assertEqual(actor.tianxing_prefix_wait_seconds("还需等待 5 分钟"), 300)
        self.assertEqual(
            actor.tianxing_prefix_wait_seconds(
                "已有一道关于【探索】的推命尚未应验，还需等待 5 分钟"
            ),
            0,
        )

    def test_fate_meditation_counts_immediate_post_pill_cultivation(self):
        actor = self.actor()
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        actor.is_running = True
        actor.state.update(
            {
                "last_destiny_date": datetime.now().strftime("%Y-%m-%d"),
                "last_destiny_choice": "紫微",
                "last_destiny_observation_date": datetime.now().strftime("%Y-%m-%d"),
                "tianxing_destiny_options": ["紫微", "贪狼", "太阴"],
                "tianxing_destiny_options_date": datetime.now().strftime("%Y-%m-%d"),
                "tianxing_fate_success_count": 1,
                "tianxing_meditation_prepared_mode": "fate",
                "tianxing_meditation_prepared_switch_id": "test-fate-switch",
            }
        )
        actor.tianxing_meditation_mode = lambda: "fate"
        actor._wait_for_main_identity = AsyncMock()
        actor.ensure_tianxing_destiny_for_action = AsyncMock(return_value=True)
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            if command == ".推命 闭关":
                return "你已有一道关于【探索】的推命尚未应验，还需等待 5 分钟。"
            if command == ".服用 合气丹1":
                return "成功服用合气丹1"
            return "闭关成功，获得修为，需要调息 10 分钟"

        actor.send_and_wait_feedback = send

        async def stop_after_sleep(*args, **kwargs):
            actor.is_running = False

        with patch("intelligent_cultivator.tianxing_settings", return_value={
            "meditation_mode": "fate",
            "meditation_switch_id": "test-fate-switch",
            "use_heqi_pill": True,
        }), patch("intelligent_cultivator.asyncio.sleep", new=stop_after_sleep):
            asyncio.run(actor.run_tianxing_fate_meditation_loop())

        self.assertEqual(
            sent,
            [".推命 闭关", ".闭关修炼", ".服用 合气丹1", ".闭关修炼"],
        )
        self.assertEqual(actor.state["tianxing_fate_success_count"], 3)

    def test_switch_to_fate_forces_exit_and_consumes_heqi_pill(self):
        actor = self.actor()
        actor.state.update(
            {
                "in_deep_meditation": True,
                "deep_meditation_end_time": "2099-01-01 00:00:00",
                "deep_meditation_guard_until": "2099-01-01 00:01:00",
                "tianxing_meditation_prepared_mode": "deep",
                "tianxing_meditation_prepared_switch_id": "old-switch",
            }
        )
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            return {
                ".查看闭关": "你正在深度闭关，预计还需 5小时 即可功成圆满。",
                ".强行出关": "你已强行出关，当前深度闭关已经结束。",
                ".服用 合气丹1": "成功服用合气丹1。",
            }[command]

        actor.send_and_wait_feedback = send
        with patch(
            "intelligent_cultivator.tianxing_settings",
            return_value={"meditation_mode": "fate", "meditation_switch_id": "new-fate-switch"},
        ), patch("intelligent_cultivator.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(asyncio.run(actor._prepare_tianxing_fate_mode()))

        self.assertEqual(sent, [".查看闭关", ".强行出关", ".服用 合气丹1"])
        self.assertFalse(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], "")
        self.assertEqual(actor.state["tianxing_meditation_prepared_mode"], "fate")
        self.assertEqual(
            actor.state["tianxing_meditation_prepared_switch_id"], "new-fate-switch"
        )

    def test_switch_to_deep_checks_status_then_starts_deep_meditation(self):
        actor = self.actor()
        actor.state.update(
            {
                "in_deep_meditation": False,
                "tianxing_meditation_prepared_mode": "fate",
                "tianxing_meditation_prepared_switch_id": "old-switch",
            }
        )
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            if command == ".查看闭关":
                return "你并未处于深度闭关之中。"
            return "你已进入深度闭关状态，神魂将自行吐纳 8小时。"

        actor.send_and_wait_feedback = send
        with patch(
            "intelligent_cultivator.tianxing_settings",
            return_value={"meditation_mode": "deep", "meditation_switch_id": "new-deep-switch"},
        ), patch("intelligent_cultivator.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(asyncio.run(actor._prepare_tianxing_deep_mode()))

        self.assertEqual(sent, [".查看闭关", ".深度闭关"])
        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["tianxing_meditation_prepared_mode"], "deep")
        self.assertEqual(
            actor.state["tianxing_meditation_prepared_switch_id"], "new-deep-switch"
        )

    def test_main_daily_destiny_records_only_confirmed_choice(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 7, 12, 0, 0)

        actor = self.actor()
        sent = []
        responses = [
            "【观命结果】今日可定下的命星如下：【太阴】。",
            "你将今日命轨定在【太阴】。",
        ]

        async def send(command, **kwargs):
            sent.append(command)
            return responses.pop(0)

        actor.send_and_wait_feedback = send
        with patch("intelligent_cultivator.datetime", FixedDatetime), patch(
            "common_command_features.datetime", FixedDatetime,
        ), patch(
            "intelligent_cultivator.asyncio.sleep",
            new=AsyncMock(),
        ):
            self.assertTrue(asyncio.run(actor._main_tianxing_destiny_check()))

        self.assertEqual(sent, [".观命"])
        self.assertEqual(actor.state["last_destiny_observation_date"], "2026-08-07")
        self.assertEqual(actor.state["tianxing_destiny_options"], ["太阴"])
        self.assertEqual(actor.state.get("last_destiny_choice", ""), "")

    def test_tianji_skill_discovers_all_tianxing_identities(self):
        actor = self.actor()
        actor.avatars = ["无咎子", "缘生子"]
        actor.avatar_nicknames = {}
        actor.identity_sect_names = {
            "主魂": "天星宗",
            "无咎子": "天星宗",
            "缘生子": "阴罗宗",
        }
        actor.state["avatars"] = {}
        self.assertEqual(actor.tianxing_identity_names(), ["主魂", "无咎子"])

        actor.ensure_tianxing_destiny_for_action = AsyncMock(return_value=True)
        calls = []

        class FakeTransport:
            async def command(self, command, identity, log_operation=True):
                calls.append((command, identity, log_operation))
                return SimpleNamespace(
                    payload={
                        "actionResult": {
                            "ok": False,
                            "message": "你拨动司命盘，为【炼制】推下一段命数。此推命将在 8 小时内生效；若你先去做别路之事，便会平添一层逆命劫。",
                        }
                    },
                    text="你拨动司命盘，为【炼制】推下一段命数。此推命将在 8 小时内生效；若你先去做别路之事，便会平添一层逆命劫。",
                )

            async def forge_treasure(
                self, identity, target_item_id, times=1, log_operation=True
            ):
                calls.append(
                    ("forge", identity, target_item_id, times, log_operation)
                )
                return {"actionResult": {"ok": True, "message": "玄铁剑炼制完成"}}

        with patch("intelligent_cultivator.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(
                asyncio.run(
                    actor._run_tianxing_tianji_identity_round(
                        "无咎子", 1, "round-1", FakeTransport()
                    )
                )
            )

        self.assertEqual(calls, [
            (".推命 炼制", "无咎子", False),
            ("forge", "无咎子", "treasure_001", 1, False),
        ])
        self.assertEqual(actor.get_avatar_state("无咎子")["tianxing_tianji_completed"], 1)
        self.assertEqual(actor.state.get("tianxing_tianji_completed", 0), 0)

    def test_tianji_summary_logs_total_once_after_all_identities_complete(self):
        actor = self.actor()
        actor.avatars = ["无咎子"]
        actor.avatar_nicknames = {}
        actor.state.update(
            {
                "tianxing_tianji_round_id": "round-1",
                "tianxing_tianji_completed": 2,
                "avatars": {
                    "无咎子": {
                        "tianxing_tianji_round_id": "round-1",
                        "tianxing_tianji_completed": 2,
                    }
                },
            }
        )

        with patch("intelligent_cultivator.log") as logger:
            self.assertTrue(
                actor._summarize_tianxing_tianji_round_if_complete(
                    ["主魂", "无咎子"], 2, "round-1"
                )
            )
            self.assertFalse(
                actor._summarize_tianxing_tianji_round_if_complete(
                    ["主魂", "无咎子"], 2, "round-1"
                )
            )

        logger.info.assert_called_once_with("刷天机值完成：总数 %s", 4)
        self.assertEqual(actor.state["tianxing_tianji_summary_total"], 4)

    def test_main_rift_uses_both_tianxing_exploration_prefixes(self):
        actor = self.actor()
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            if command == ".观命":
                return "贪狼、紫微、太阴"
            if command == ".定命 贪狼":
                return "今日命轨定在贪狼"
            if command in {".推命 探索", ".改命 探索"}:
                return "你已有一道关于【闭关】的推命尚未应验，还需等待 5 分钟。"
            return "执行成功"

        actor.send_and_wait_feedback = send
        plan = Mock(
            command=".探寻裂缝",
            timeout=120,
            max_retries=0,
            force_identity_check=False,
            return_response_msg=False,
        )
        with patch("common_command_features.asyncio.sleep", new=AsyncMock()):
            asyncio.run(actor.send_rift_search_plan(plan, "主魂"))

        self.assertEqual(
            sent,
            [".观命", ".定命 贪狼", ".推命 探索", ".改命 探索", ".探寻裂缝"],
        )

    def test_main_rift_defers_on_standalone_prefix_cooldown(self):
        actor = self.actor()
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state.update(
            {
                "last_destiny_observation_date": today,
                "tianxing_destiny_options": ["贪狼", "紫微", "太阴"],
                "tianxing_destiny_options_date": today,
                "last_destiny_date": today,
                "last_destiny_choice": "贪狼",
            }
        )
        sent = []

        async def send(command, **kwargs):
            sent.append(command)
            return "司命盘尚在冷却，还需等待 5 分钟。"

        actor.send_and_wait_feedback = send
        plan = Mock(
            command=".探寻裂缝",
            timeout=120,
            max_retries=0,
            force_identity_check=False,
            return_response_msg=False,
            next_key="next_rift_search_time",
        )
        with patch("common_command_features.asyncio.sleep", new=AsyncMock()):
            result = asyncio.run(actor.send_rift_search_plan(plan, "主魂"))

        self.assertIsNone(result)
        self.assertEqual(sent, [".推命 探索"])
        self.assertTrue(actor.state.get("next_rift_search_time"))


if __name__ == "__main__":
    unittest.main()
