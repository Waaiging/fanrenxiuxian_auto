import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import command_feedback
import intelligent_cultivator
from command_modules import DEFAULT_WAAIGING_FIELD_TRAINING_COMMAND
from cultivator_waaiging import WaaigingCultivator
from dashboard_server import build_command_panels
from miniapp_command_routing import MiniAppCommandRouter


class WaaigingAccountTests(unittest.TestCase):
    def test_profile_without_saved_sect_waits_for_membership_and_has_no_avatar_tasks(self):
        def fake_base_init(actor, session_name):
            actor.mc = {}
            actor.state = {}
            actor.xiaohao_visibility_control_enabled = True
            actor.save_state = lambda: None

        with patch.object(intelligent_cultivator, "configure_runtime_files"), patch.object(
            intelligent_cultivator.Cultivator,
            "__init__",
            fake_base_init,
        ):
            actor = WaaigingCultivator()

        self.assertEqual(actor.account_key, "waaiging")
        self.assertEqual(actor.expected_username, "Waaiging")
        self.assertEqual(actor.field_training_command, DEFAULT_WAAIGING_FIELD_TRAINING_COMMAND)
        self.assertEqual(actor.field_training_plan("主魂").command, ".野外历练 深入")
        self.assertEqual(actor.identity_sect_names, {"主魂": ""})
        self.assertEqual(actor.avatars, [])
        self.assertFalse(actor.enable_avatar_tasks)
        self.assertFalse(actor.enable_miniapp_star_palace)
        self.assertFalse(actor.enable_main_beasts)
        self.assertFalse(actor.enable_treasure_touch)
        self.assertFalse(actor.enable_nurture_spirit)
        # 化神境（2026-09-07）：Waaiging 启用小世界三件套（显灵/安抚信徒/神迹布道）。
        self.assertTrue(actor.enable_small_world)
        self.assertTrue(actor.enable_node_search)
        self.assertTrue(actor.telegram_write_restriction_retry_enabled)
        self.assertEqual(actor.account_sect_name(), "")
        config = {"enabled": True, "mode": "daily", "switch_id": "own-switch", "use_heqi_pill": False}
        with patch("meditation_features.meditation_identity_settings", return_value=config) as selected:
            self.assertEqual(actor.identity_meditation_mode(), "daily")
            selected.assert_called_with("waaiging", "主魂")
        self.assertNotIn("tianxing_meditation_prepared_mode", actor.state)

    def test_stale_star_palace_assignment_is_cleared_for_single_soul_account(self):
        saved = []

        def fake_base_init(actor, session_name):
            actor.mc = {}
            actor.state = {
                "sect_join_confirmed": True,
                "star_gazing_assigned_avatar": "素缘子",
                "star_gazing_claimed_avatar": "素缘子",
                "pending_star_shift_msg_id": 123,
                "miniapp_star_farm_identities": ["素缘子"],
            }
            actor.xiaohao_visibility_control_enabled = True
            actor.save_state = lambda: saved.append(dict(actor.state))

        with patch.object(intelligent_cultivator, "configure_runtime_files"), patch.object(
            intelligent_cultivator.Cultivator,
            "__init__",
            fake_base_init,
        ):
            actor = WaaigingCultivator()

        self.assertEqual(actor.state["star_gazing_assigned_avatar"], "")
        self.assertEqual(actor.state["star_gazing_claimed_avatar"], "")
        self.assertEqual(actor.state["pending_star_shift_msg_id"], 0)
        self.assertEqual(actor.state["miniapp_star_farm_identities"], [])
        self.assertTrue(saved)

    def test_constructor_preserves_confirmed_sect_without_legacy_join_flag(self):
        for source in ("identity_sect_names", "miniapp_sect_name", "sect_name"):
            with self.subTest(source=source):
                def fake_base_init(actor, session_name):
                    actor.mc = {}
                    actor.state = {source: {"主魂": "合欢宗"} if source == "identity_sect_names" else "合欢宗"}
                    actor.save_state = lambda: None

                with patch.object(intelligent_cultivator, "configure_runtime_files"), patch.object(
                    intelligent_cultivator.Cultivator, "__init__", fake_base_init,
                ):
                    actor = WaaigingCultivator()
                self.assertEqual(actor.account_sect_name(), "合欢宗")
                self.assertEqual(actor.identity_sect_names, {"主魂": "合欢宗"})
                self.assertEqual(actor.state["sect_name"], "合欢宗")
                self.assertFalse(actor.state["sect_join_confirmed"])

    def test_identity_sect_name_ignores_stale_non_tianxing_mapping(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        actor.state = {"sect_join_confirmed": True, "sect_name": "天星宗"}
        actor.sect_name = "天星宗"
        actor.identity_sect_names = {"主魂": "星宫", "素缘子": "星宫"}

        self.assertEqual(actor.identity_sect_name("主魂"), "天星宗")
        self.assertEqual(actor.identity_sect_name("素缘子"), "")

    def test_identity_guard_blocks_nonexistent_avatar(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        sent = []

        async def base_send(_actor, identity, message, *args, **kwargs):
            sent.append((identity, message))
            return "ok"

        with patch.object(
            intelligent_cultivator.Cultivator,
            "send_and_wait_feedback_identity",
            base_send,
        ):
            self.assertIsNone(asyncio.run(actor.send_and_wait_feedback_identity("素缘子", ".观星")))
            self.assertEqual(
                asyncio.run(actor.send_and_wait_feedback_identity("主魂", ".查看闭关")),
                "ok",
            )

        self.assertEqual(sent, [("主魂", ".查看闭关")])

    def test_time_critical_pre_switch_guard_blocks_nonexistent_avatar(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        sent = []

        async def base_prepare(_actor, identity, *args, **kwargs):
            sent.append(identity)
            return True

        with patch.object(
            intelligent_cultivator.Cultivator,
            "prepare_identity_for_time_critical_command",
            base_prepare,
        ):
            self.assertFalse(
                asyncio.run(
                    actor.prepare_identity_for_time_critical_command("素缘子", ".观星")
                )
            )
            self.assertTrue(
                asyncio.run(
                    actor.prepare_identity_for_time_critical_command("主魂", ".查看闭关")
                )
            )

        self.assertEqual(sent, ["主魂"])

    def test_router_does_not_start_star_palace_worker_for_waaiging(self):
        actor = SimpleNamespace(
            account_key="waaiging",
            enable_miniapp_star_palace=False,
            is_running=True,
            avatars=[],
            save_state=lambda: None,
        )
        router = MiniAppCommandRouter.__new__(MiniAppCommandRouter)
        router.actor = actor
        router.start_background_tasks = True
        router.enabled = True
        router._star_palace_tasks = {}
        router.log = intelligent_cultivator.log
        router.routable_identities = lambda: ["主魂"]
        router.star_farm_identities = lambda identities=None: ["主魂"]

        router._reconcile_star_palace_tasks(["主魂"])

        self.assertEqual(router._star_palace_tasks, {})

    def test_stale_avatars_are_removed_and_persisted(self):
        saved = []

        def fake_base_init(actor, session_name):
            actor.mc = {}
            actor.state = {
                "avatars": {"无咎子": {}},
                "next_small_world_time": "2026-07-22 03:00:00",
                "next_small_world_calamity_time": "2026-07-22 03:05:00",
                "small_world_calamity_pending": True,
                "next_miracle_preach_time": "2026-07-22 03:10:00",
            }
            actor.xiaohao_visibility_control_enabled = True
            actor.save_state = lambda: saved.append(dict(actor.state))

        with patch.object(intelligent_cultivator, "configure_runtime_files"), patch.object(
            intelligent_cultivator.Cultivator,
            "__init__",
            fake_base_init,
        ):
            actor = WaaigingCultivator()

        self.assertNotIn("avatars", actor.state)
        self.assertTrue(saved)
        self.assertNotIn("avatars", saved[-1])
        # 化神境启用后小世界排期保留，不清空（无缝续跑）。
        self.assertEqual(saved[-1]["next_small_world_time"], "2026-07-22 03:00:00")
        self.assertEqual(saved[-1]["next_small_world_calamity_time"], "2026-07-22 03:05:00")
        self.assertTrue(saved[-1]["small_world_calamity_pending"])
        self.assertEqual(saved[-1]["next_miracle_preach_time"], "2026-07-22 03:10:00")

    def test_small_world_scheduler_tasks_are_not_registered_for_waaiging(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        actor.enable_small_world = False
        registered = []
        actor.create_scheduler_task = lambda name, factory: registered.append(name)

        actor.start_small_world_scheduler_tasks()

        self.assertEqual(registered, [])

    def test_small_world_scheduler_tasks_remain_registered_for_main(self):
        actor = intelligent_cultivator.Cultivator.__new__(intelligent_cultivator.Cultivator)
        actor.enable_small_world = True
        registered = []
        actor.create_scheduler_task = lambda name, factory: registered.append(name)

        actor.start_small_world_scheduler_tasks()

        self.assertEqual(registered, ["small_world", "small_world_calamity", "miracle_preach"])

    def test_restricted_account_specs_support_both_accounts(self):
        actor = intelligent_cultivator.Cultivator.__new__(intelligent_cultivator.Cultivator)
        actor.manage_restricted_accounts = True
        actor.xiaohao_visibility_control_enabled = True
        actor.xiaohao_visibility_poll_seconds = 60
        actor.mc = {
            "restricted_accounts": [
                {
                    "key": "xiaohao",
                    "script": "cultivator_xiaohao.py",
                    "tmux_target": "xiuxian:2",
                    "state_file": "state_xiaohao.json",
                },
                {
                    "key": "waaiging",
                    "script": "cultivator_waaiging.py",
                    "tmux_target": "xiuxian:3",
                    "state_file": "state_waaiging.json",
                },
            ]
        }

        specs = actor.restricted_account_specs()
        self.assertEqual([item["key"] for item in specs], ["xiaohao", "waaiging"])
        self.assertEqual(specs[1]["tmux_target"], "xiuxian:3")

    def test_dashboard_has_only_main_soul_commands(self):
        panels = build_command_panels("waaiging", {"done": [], "avatars": {}})
        self.assertEqual([panel["identity"] for panel in panels], ["主魂"])
        commands = {item["command"] for item in panels[0]["commands"]}
        self.assertNotIn(".推命 探索", commands)
        self.assertNotIn(".改命 探索", commands)
        self.assertIn(".探寻裂缝", commands)
        self.assertNotIn(".野外历练 深入", commands)
        self.assertNotIn(".野外历练", commands)
        self.assertFalse(any(command.startswith(".抚摸法宝") for command in commands))
        # 化神境（2026-09-07）：小世界三件套 + 搜寻节点已启用，Dashboard 应展示。
        self.assertIn(".小世界", commands)
        self.assertIn(".显灵", commands)
        self.assertIn(".安抚信徒", commands)
        self.assertIn(".神迹 布道", commands)
        self.assertIn(".搜寻节点", commands)

    def test_sect_join_cooldown_and_success_are_persisted(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        not_before = intelligent_cultivator.add_seconds_str(
            intelligent_cultivator.now_str(),
            24 * 3600,
        )
        actor.state = {
            "done": [".宗门点卯"],
            "sect_join_not_before": not_before,
        }
        actor.save_state = lambda: None
        actor.parse_wait_time = lambda text: 10 * 3600

        status = actor.record_sect_join_response(
            "你因叛出宗门，身负天道烙印，各大宗门在 10 小时内都不会接纳你。"
        )
        self.assertEqual(status, "cooldown")
        self.assertEqual(actor.state["sect_join_status"], "cooldown")
        self.assertEqual(actor.state["next_sect_join_time"], not_before)
        self.assertFalse(actor.state.get("sect_join_confirmed", False))

        status = actor.record_sect_join_response(
            "恭喜 @Waaiging 道友，你已成功拜入【天星宗】，成为本门弟子！"
        )
        self.assertEqual(status, "joined")
        self.assertTrue(actor.state["sect_join_confirmed"])
        self.assertEqual(actor.state["next_sect_join_time"], "")
        self.assertEqual(actor.state["sect_join_not_before"], "")
        self.assertNotIn(".宗门点卯", actor.state["done"])

    def test_write_restriction_gets_retry_time_for_waaiging(self):
        class Actor:
            account_key = "waaiging"
            current_identity = "主魂"
            telegram_write_restriction_retry_enabled = True
            telegram_send_protection_retry_seconds = 15 * 60

            def __init__(self):
                self.state = {}
                self.is_running = True

            def save_state(self):
                return None

        actor = Actor()
        with patch.object(command_feedback, "send_text_alert", AsyncMock(return_value=True)):
            handled = asyncio.run(
                command_feedback._handle_telegram_send_protection(
                    actor,
                    ".宗门点卯",
                    RuntimeError("CHAT_WRITE_FORBIDDEN"),
                )
            )

        self.assertTrue(handled)
        self.assertFalse(actor.is_running)
        self.assertTrue(actor.state["telegram_send_protection_stop"]["retry_at"])

    def test_switch_replies_are_excluded_from_repeated_response_guard(self):
        actor = SimpleNamespace(current_identity="寻真子")
        response = "切换成功！你的神念已附着在【寻真子】之上。"

        for _ in range(5):
            self.assertFalse(
                command_feedback._record_repeated_response_guard(
                    actor,
                    ".切换 寻真子",
                    response,
                    identity="寻真子",
                )
            )

    def test_removed_tower_and_spirit_tree_commands_are_globally_retired(self):
        for command in (
            ".闯塔",
            ".借天门势",
            ".灵树灌溉",
            ".灵树状态",
            ".采摘灵果",
            ".协同守山",
            ".推命 探索",
            ".改命 探索",
            ".灵兽休息 六翼",
        ):
            self.assertTrue(command_feedback.is_retired_auto_command(command))
        self.assertTrue(command_feedback.is_retired_auto_command(".宗门点卯"))

    def test_watchdog_ignores_disabled_waaiging_treasure_tasks(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        actor.enable_treasure_touch = False
        actor.enable_nurture_spirit = False
        actor.enable_small_world = False
        stale = [
            ("next_treasure_touch_time", ".抚摸法宝 玄天斩灵剑", "2026-07-19 00:00:00", 3600),
            ("next_nurture_spirit_time", ".温养器灵 斩灵", "2026-07-19 00:00:00", 3600),
            ("next_small_world_time", ".小世界", "2026-07-22 00:00:00", 3600),
            ("next_miracle_preach_time", ".神迹 布道", "2026-07-22 00:00:00", 3600),
            ("next_rift_search_time", ".探寻裂缝", "2026-07-19 00:00:00", 3600),
        ]

        with patch.object(
            intelligent_cultivator.Cultivator,
            "stale_scheduler_due_items",
            return_value=stale,
        ):
            self.assertEqual(actor.stale_scheduler_due_items(), [stale[-1]])

    def test_tianxing_rift_restores_exploration_prefixes_and_meditation_prefix_remains(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state = {
            "sect_join_confirmed": True,
            "last_destiny_observation_date": today,
            "tianxing_destiny_options_date": today,
            "tianxing_destiny_options": ["贪狼"],
            "last_destiny_date": today,
            "last_destiny_choice": "贪狼",
        }
        actor.sect_name = "天星宗"
        actor.identity_sect_names = {"主魂": "天星宗"}
        actor.active_atomic_task = None
        sent = []

        async def fake_base_send(_actor, command, *args, **kwargs):
            sent.append(command)
            return "执行成功"

        async def fake_sleep(_seconds):
            return None

        rift_plan = SimpleNamespace(
            command=".探寻裂缝",
            timeout=120,
            max_retries=0,
            force_identity_check=False,
            return_response_msg=False,
        )
        with patch.object(
            intelligent_cultivator.Cultivator,
            "send_and_wait_feedback",
            fake_base_send,
        ), patch.object(asyncio, "sleep", fake_sleep):
            asyncio.run(actor.send_and_wait_feedback(".野外历练 深入"))
            asyncio.run(actor.send_main_meditation_settlement())
            asyncio.run(actor.send_rift_search_plan(rift_plan, "主魂"))

        self.assertEqual(
            sent,
            [
                ".野外历练 深入",
                ".推命 闭关",
                ".闭关修炼",
                ".推命 探索",
                ".改命 探索",
                ".探寻裂缝",
            ],
        )

    def test_tianxing_rift_prefix_scope_bypasses_retired_policy_only_while_active(self):
        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        actor._tianxing_rift_prefix_command = ""
        self.assertTrue(command_feedback.is_retired_auto_command(".推命 探索", actor=actor))

        actor._tianxing_rift_prefix_command = ".推命 探索"
        self.assertFalse(command_feedback.is_retired_auto_command(".推命 探索", actor=actor))
        self.assertTrue(command_feedback.is_retired_auto_command(".改命 探索", actor=actor))

    def test_tianxing_daily_destiny_records_only_confirmed_choice(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 19, 12, 0, 0)

        actor = WaaigingCultivator.__new__(WaaigingCultivator)
        actor.state = {
            "sect_join_confirmed": True,
            "last_destiny_date": "",
            "identity_sect_names": {"主魂": "天星宗"},
            "next_tianxing_destiny_retry_time": "",
        }
        actor.active_atomic_task = None
        actor.dashboard_command_paused = lambda command, identity: False
        actor.daily_one_shot_should_defer = lambda *args, **kwargs: False
        actor.response_text = lambda response: str(response or "")
        actor.save_state = lambda: None
        sent = []
        responses = ["【观命结果】今日可定下的命星如下：【太阴】。"]

        async def fake_send(command, **kwargs):
            sent.append(command)
            return responses.pop(0)

        async def fake_sleep(_seconds):
            return None

        actor.send_and_wait_feedback = fake_send
        with patch("cultivator_waaiging.datetime", FixedDatetime), patch(
            "common_command_features.datetime", FixedDatetime
        ), patch.object(asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor._tianxing_destiny_check()))

        self.assertEqual(sent, [".观命"])
        self.assertEqual(actor.state["last_destiny_observation_date"], "2026-07-19")
        self.assertEqual(actor.state["tianxing_destiny_options"], ["太阴"])
        self.assertEqual(actor.state.get("last_destiny_date", ""), "")
        self.assertEqual(actor.state.get("last_destiny_choice", ""), "")


if __name__ == "__main__":
    unittest.main()
