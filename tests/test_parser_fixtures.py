import asyncio
import unittest
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import concubine_features
import cultivator_xiaohao
import dashboard_server
import intelligent_cultivator
import log_utils
import star_gazing_collector
import sub_cultivator
from common_command_features import CommonCommandMixin, now_str, seconds_until as common_seconds_until
from concubine_features import ConcubineMixin, concubine_default_state, parse_duration_seconds, seconds_until
from cultivator_xiaohao import CultivatorXiaoHao
from dashboard_server import build_command_panels, outgoing_log_command_full, parse_inventory_items_from_text, parse_resource_changes_from_text, resource_text_matches_identity
from intelligent_cultivator import Cultivator
from log_utils import parse_cultivation_delta_text, parse_cultivation_profile_text
from sub_cultivator import SubCultivator


class DummyConcubine(ConcubineMixin):
    account_key = "main"
    avatars = []

    def __init__(self):
        self.state = concubine_default_state()

    def save_state(self):
        return None

    def parse_wait_time(self, text, *args, **kwargs):
        return parse_duration_seconds(text)


class DummyCommon(CommonCommandMixin):
    def __init__(self):
        self.state = {}

    def save_state(self):
        return None

    def parse_wait_time(self, text, *args, **kwargs):
        return parse_duration_seconds(text)


class DummyAvatarCommon(DummyCommon):
    avatars = ["缘生子"]

    def __init__(self):
        self.state = {"avatars": {"缘生子": {}}}

    def get_avatar_state(self, identity):
        return self.state.setdefault("avatars", {}).setdefault(identity, {})

    def set_avatar_state(self, identity, key, value):
        self.get_avatar_state(identity)[key] = value

    def update_avatar_states(self, identity, values):
        self.get_avatar_state(identity).update(values)


class DummyMessage:
    def __init__(self, msg_id, chat_id=-100123456, text="", reply_to_msg_id=None, out=False):
        self.id = msg_id
        self.chat_id = chat_id
        self.text = text
        self.out = out
        self.sender_id = 999 if out else 888
        self.reply_to = None
        if reply_to_msg_id is not None:
            self.reply_to = type("ReplyTo", (), {"reply_to_msg_id": reply_to_msg_id})()


class DummyManualActor:
    state_file = "state_main.json"
    target_chat_id = -100123456


class ParserFixtureTests(unittest.TestCase):
    def test_han_soul_choice_targets_main_and_replies_once(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command, kwargs))
            return ""

        actor.send_and_wait_feedback_identity = fake_send
        msg = DummyMessage(7101, text="")
        text = """
@Waaiging! 你感到一股无法抗拒的意志锁定了你的神魂！
你必须在 180 分钟 内做出抉择：
1. 回复本消息 .献上魂魄 (高风险，高回报)
2. 回复本消息 .收敛气息 (低风险，低回报)
"""

        self.assertEqual(log_utils.han_soul_choice_target_identity(actor, msg, text), "主魂")
        self.assertTrue(asyncio.run(log_utils.maybe_handle_han_soul_choice(actor, msg, text)))
        self.assertTrue(asyncio.run(log_utils.maybe_handle_han_soul_choice(actor, msg, text)))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0:2], ("主魂", ".献上魂魄"))
        self.assertEqual(sent[0][2]["reply_to"], 7101)
        self.assertTrue(sent[0][2]["suppress_no_response_alert"])

    def test_han_soul_choice_targets_avatar_identity(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command, kwargs))
            return ""

        actor.send_and_wait_feedback_identity = fake_send
        msg = DummyMessage(7102, text="")
        text = """
@kulipabp! 你感到一股无法抗拒的意志锁定了你的神魂！
你必须在 180 分钟 内做出抉择：
1. 回复本消息 .献上魂魄 (高风险，高回报)
2. 回复本消息 .收敛气息 (低风险，低回报)
"""

        self.assertEqual(log_utils.han_soul_choice_target_identity(actor, msg, text), "缘生子")
        self.assertTrue(asyncio.run(log_utils.maybe_handle_han_soul_choice(actor, msg, text)))
        self.assertEqual(sent[0][0:2], ("缘生子", ".献上魂魄"))
        self.assertEqual(sent[0][2]["reply_to"], 7102)

    def test_star_gazing_final_report_clears_pending_shift(self):
        actor = SubCultivator.__new__(SubCultivator)
        manifest = actor.current_star_report_manifest_dt()
        manifest_key = manifest.strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "pending_star_shift_target_time": manifest_key,
            "pending_star_shift_msg_id": 123,
            "pending_star_gazing_manifest_time": manifest_key,
        }
        actor.save_state = lambda: None
        actor.star_shift_task = None
        actor.star_gazing_task = None

        text = """
**【天机阁快报 - 封魔裂隙回响】**
天机演化结果: 裂隙闭合前，你伸手一抓，竟摄出一枚令牌！
"""

        self.assertTrue(actor.record_star_gazing_final_report_if_needed(
            DummyMessage(7201),
            text,
            source="fixture",
        ))
        self.assertEqual(actor.state["last_star_gazing_report_manifest_time"], manifest_key)
        self.assertEqual(actor.state["pending_star_shift_target_time"], "")
        self.assertEqual(actor.state["pending_star_shift_msg_id"], 0)

    def test_star_gazing_final_report_blocks_avatar_shift_all_accounts(self):
        async def run_case(cls, avatar):
            actor = cls.__new__(cls)
            target_dt = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
            target_key = target_dt.strftime("%Y-%m-%d %H:%M:%S")
            actor.state = {
                "last_star_gazing_report_manifest_time": target_key,
                "avatars": {avatar: {"last_star_shift_date": ""}},
            }
            actor.save_state = lambda: None
            actor.get_avatar_state = lambda name: actor.state["avatars"].setdefault(name, {})
            actor.active_atomic_task = None
            sent = []

            async def fake_send(*args, **kwargs):
                sent.append((args, kwargs))
                return DummyMessage(7202)

            actor.send_and_wait_feedback_identity = fake_send
            await actor.avatar_schedule_star_shift(
                avatar,
                reply_msg_id=7200,
                target_dt=target_dt,
                gazing_date=target_dt.strftime("%Y-%m-%d"),
            )
            return sent

        cases = [
            (Cultivator, "素缘子"),
            (SubCultivator, "寻真子"),
            (CultivatorXiaoHao, "素心子"),
        ]
        for cls, avatar in cases:
            with self.subTest(cls=cls.__name__, avatar=avatar):
                self.assertEqual(asyncio.run(run_case(cls, avatar)), [])

    def test_star_gazing_boundary_notice_targets_next_manifest_all_accounts(self):
        now = datetime(2026, 6, 15, 0, 0, 10)
        expected_manifest = datetime(2026, 6, 15, 3, 0, 0)
        expected_send = datetime(2026, 6, 15, 2, 59, 0)

        for cls in (Cultivator, SubCultivator, CultivatorXiaoHao):
            actor = cls.__new__(cls)
            manifest_dt = actor.star_gazing_target_for_opportunity(now)
            send_dt, immediate_shift, gazing_date = actor.star_gazing_schedule_plan(now, manifest_dt)

            self.assertEqual(manifest_dt, expected_manifest)
            self.assertEqual(send_dt, expected_send)
            self.assertFalse(immediate_shift)
            self.assertEqual(gazing_date, "2026-06-15")

    def test_star_gazing_too_late_to_observe_skips_to_next_round_all_accounts(self):
        now = datetime(2026, 6, 15, 2, 59, 1)
        expected_manifest = datetime(2026, 6, 15, 6, 0, 0)

        for cls in (Cultivator, SubCultivator, CultivatorXiaoHao):
            actor = cls.__new__(cls)
            manifest_dt = actor.star_gazing_target_for_opportunity(now)
            send_dt, immediate_shift, _ = actor.star_gazing_schedule_plan(now, manifest_dt)

            self.assertEqual(manifest_dt, expected_manifest)
            self.assertLessEqual(send_dt, manifest_dt - timedelta(seconds=60))
            self.assertFalse(immediate_shift)

    def test_star_gazing_collector_manifest_notice_targets_next_boundary(self):
        msg = DummyMessage(7301, text="")
        msg.date = datetime(2026, 6, 14, 16, 0, 0, tzinfo=timezone.utc)
        text = """
**【星盘显化】**
@foo 闭目凝神，仰观天象。
当前天命所归: @bar
【Good - 星辰异象】
"""

        record = star_gazing_collector.build_star_gazing_event_record("main", msg, text)

        self.assertEqual(record["event_kind"], "manifest")
        self.assertEqual(record["message_boundary_time"], "2026-06-15 00:00:00")
        self.assertEqual(record["target_manifest_time"], "2026-06-15 03:00:00")

    def test_sub_star_gazing_account_date_does_not_block_other_avatars(self):
        async def run_case():
            today = datetime.now().strftime("%Y-%m-%d")
            actor = SubCultivator.__new__(SubCultivator)
            actor.mc = {}
            actor.avatars = ["厚土", "缘生子", "寻真子"]
            actor.avatar_nicknames = {"厚土": "", "缘生子": "", "寻真子": ""}
            actor.state = {
                "last_gazing_date": today,
                "last_gazing_time": now_str(),
                "star_gazing_avatar_index": 0,
                "avatars": {
                    "厚土": {"last_gazing_date": ""},
                    "缘生子": {"last_gazing_date": ""},
                    "寻真子": {"last_gazing_date": today},
                },
            }
            actor.star_gazing_lock = asyncio.Lock()
            actor.star_gazing_task = None
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.schedule_star_gazing_simple = fake_schedule
            sender = SimpleNamespace(username="hantianzzzzzz_bot")
            text = "【Good - 星辰异象】"

            handled = await actor.maybe_handle_star_gazing_opportunity(
                DummyMessage(7302),
                text,
                sender,
            )
            await asyncio.sleep(0)
            return handled, scheduled, actor.state

        handled, scheduled, state = asyncio.run(run_case())

        self.assertTrue(handled)
        self.assertEqual(state["star_gazing_claimed_avatar"], "厚土")
        self.assertTrue(state["pending_star_gazing_target_time"])
        self.assertEqual(len(scheduled), 1)

    def test_main_command_sends_immediately_after_identity_switch_all_accounts(self):
        async def run_case(actor_cls, actor_module):
            actor = actor_cls.__new__(actor_cls)
            actor.avatars = ["缘生子"]
            actor.state = {
                "current_identity": "缘生子",
                "next_star_gazing_time": now_str(),
            }
            actor.save_state = lambda: None
            actor.current_identity = "缘生子"
            actor._main_confirmed = False
            actor.avatar_send_lock = asyncio.Lock()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.active_atomic_task = None
            actor.dashboard_command_paused = lambda *args, **kwargs: False
            actor.wait_while_identity_paused = lambda *args, **kwargs: asyncio.sleep(0, result=True)
            actor.check_and_record_switch_ban = lambda *args, **kwargs: False
            actor.apply_switch_guard_backoff = lambda *args, **kwargs: False
            sent = []

            async def fake_raw(message, *args, **kwargs):
                sent.append(message)
                if message == ".切换 主魂":
                    return "你已收回神通，神念重归主魂肉身。"
                return DummyMessage(7401, text="ok")

            actor._send_and_wait_feedback_raw = fake_raw
            old_precheck = actor_module.command_send_precheck
            actor_module.command_send_precheck = lambda *args, **kwargs: True
            try:
                await asyncio.wait_for(
                    actor.send_and_wait_feedback(
                        ".深度闭关",
                        timeout=5,
                        max_retries=0,
                        force_identity_check=True,
                    ),
                    timeout=1,
                )
            finally:
                actor_module.command_send_precheck = old_precheck
            return sent

        cases = [
            (Cultivator, intelligent_cultivator),
            (SubCultivator, sub_cultivator),
            (CultivatorXiaoHao, cultivator_xiaohao),
        ]
        for actor_cls, actor_module in cases:
            with self.subTest(actor=actor_cls.__name__):
                self.assertEqual(
                    asyncio.run(run_case(actor_cls, actor_module)),
                    [".切换 主魂", ".深度闭关"],
                )

    def test_dashboard_outgoing_parser_handles_literal_newline_manual_entries(self):
        entry = {
            "lines": [
                r"2026-06-12 17:24:52,585 [INFO] OUT [manual 10269126 | 主魂]:\n.灵树灌溉"
            ]
        }

        self.assertEqual(outgoing_log_command_full(entry), ".灵树灌溉")

    def test_dashboard_log_page_uses_tail_cursor_for_default_view(self):
        old_config_dir = dashboard_server.CONFIG_DIR
        old_initial_bytes = dashboard_server.LOG_TAIL_INITIAL_BYTES
        old_max_bytes = dashboard_server.LOG_TAIL_MAX_BYTES
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dashboard_server.CONFIG_DIR = tmp
                dashboard_server.LOG_TAIL_INITIAL_BYTES = 128
                dashboard_server.LOG_TAIL_MAX_BYTES = 2048
                lines = []
                for idx in range(45):
                    lines.append(
                        f"2026-06-12 17:{idx:02d}:00,000 [INFO] OUT [manual {idx} | 主魂]:\n"
                        f".测试{idx}\n"
                        f"line {idx}"
                    )
                with open(os.path.join(tmp, "cultivator.log"), "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))

                first = dashboard_server.get_log_page("main", limit=20)

                self.assertTrue(first.get("partial"))
                self.assertEqual(first.get("cursor_mode"), "byte")
                self.assertTrue(first.get("has_more"))
                self.assertEqual(len(first.get("entries") or []), 20)
                self.assertIn(".测试44", first["entries"][-1])
                self.assertIsNone(first.get("total"))

                older = dashboard_server.get_log_page("main", before=first.get("next_before"), limit=20)

                self.assertTrue(older.get("partial"))
                self.assertEqual(len(older.get("entries") or []), 20)
                self.assertIn(".测试24", older["entries"][-1])
        finally:
            dashboard_server.CONFIG_DIR = old_config_dir
            dashboard_server.LOG_TAIL_INITIAL_BYTES = old_initial_bytes
            dashboard_server.LOG_TAIL_MAX_BYTES = old_max_bytes

    def test_dashboard_command_records_use_effective_replies_only(self):
        old_config_dir = dashboard_server.CONFIG_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dashboard_server.CONFIG_DIR = tmp
                dashboard_server.COMMAND_RECORD_CACHE.clear()
                db_path = os.path.join(tmp, dashboard_server.MESSAGE_EVENTS_DB_FILE)
                conn = sqlite3.connect(db_path)
                try:
                    conn.executescript("""
                        CREATE TABLE command_ledger (
                            account TEXT NOT NULL,
                            chat_id INTEGER,
                            command_msg_id INTEGER NOT NULL,
                            command TEXT NOT NULL,
                            identity TEXT NOT NULL,
                            source TEXT NOT NULL,
                            reply_to_msg_id INTEGER,
                            status TEXT NOT NULL,
                            sent_at TEXT NOT NULL,
                            response_msg_id INTEGER,
                            response_hash TEXT,
                            response_at TEXT,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(account, chat_id, command_msg_id)
                        );
                        CREATE TABLE message_events (
                            account TEXT NOT NULL,
                            event_kind TEXT NOT NULL,
                            direction TEXT NOT NULL,
                            chat_id INTEGER,
                            msg_id INTEGER,
                            reply_to_msg_id INTEGER,
                            sender_id INTEGER,
                            sender_username TEXT,
                            sender_name TEXT,
                            is_out INTEGER NOT NULL DEFAULT 0,
                            is_game_bot INTEGER NOT NULL DEFAULT 0,
                            identity TEXT,
                            command TEXT,
                            text TEXT,
                            text_hash TEXT NOT NULL,
                            created_at TEXT NOT NULL
                        );
                    """)
                    rows = [
                        (1001, ".灵树灌溉", "auto", "2026-06-14 09:49:32", 2001, "2026-06-14 09:49:33",
                         "**【🌿 灵树灌溉】**\n你注入了: **木行** 灵气\n🌳 **成熟度**: 10.94% -> **11.05%**"),
                        (1002, ".灵树灌溉", "auto", "2026-06-14 09:50:28", 2002, "2026-06-14 09:50:29",
                         "地脉灵气尚未恢复，请在 **1小时59分钟55秒** 后再来灌溉。"),
                        (1003, ".灵树灌溉", "manual", "2026-06-14 11:49:32", 2003, "2026-06-14 11:49:34",
                         "**【🌿 灵树灌溉】**\n你注入了: **水行** 灵气\n🌳 **成熟度**: 11.05% -> **11.16%**"),
                    ]
                    for cmd_msg, command, source, sent_at, resp_msg, response_at, text in rows:
                        conn.execute(
                            """
                            INSERT INTO command_ledger (
                                account, chat_id, command_msg_id, command, identity, source,
                                status, sent_at, response_msg_id, response_at, updated_at
                            ) VALUES ('main', 1, ?, ?, '主魂', ?, 'matched', ?, ?, ?, ?)
                            """,
                            (cmd_msg, command, source, sent_at, resp_msg, response_at, response_at),
                        )
                        conn.execute(
                            """
                            INSERT INTO message_events (
                                account, event_kind, direction, chat_id, msg_id, is_game_bot,
                                identity, command, text, text_hash, created_at
                            ) VALUES ('main', 'new', 'bot_in', 1, ?, 1, '主魂', ?, ?, '', ?)
                            """,
                            (resp_msg, command, text, response_at),
                        )
                    conn.commit()
                finally:
                    conn.close()

                records = dashboard_server.build_account_command_records("main")["records"]
                row = next(item for item in records if item["command"] == ".灵树灌溉")

                self.assertEqual(row["count"], 2)
                self.assertEqual(row["auto_count"], 1)
                self.assertEqual(row["manual_count"], 1)
                self.assertEqual(row["last_time"], "2026-06-14 11:49:34")
                self.assertEqual(row["recent_times"], ["2026-06-14 09:49:33", "2026-06-14 11:49:34"])
        finally:
            dashboard_server.CONFIG_DIR = old_config_dir
            dashboard_server.COMMAND_RECORD_CACHE.clear()

    def test_cultivation_profile_from_spirit_root_reply(self):
        text = """
**@Lvdoumiao**** 的天命玉牒**
────────────────
**宗门**: 【星宫】
**灵根**: 真灵根(土木)
**修为**: 12,443 / 30,000
**丹毒**: 0 点
"""
        profile = parse_cultivation_profile_text(text)

        self.assertEqual(profile["spirit_root"], "真灵根(土木)")
        self.assertEqual(profile["current_exp"], 12443)
        self.assertEqual(profile["total_exp"], 30000)

    def test_direct_avatar_meditation_profile_accepts_breakthrough_cap(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {
            "avatars": {
                "缘生子": {
                    "level": "结丹后期",
                    "current_exp": 199922,
                    "total_exp": 200000,
                }
            }
        }
        actor.save_state = lambda: None

        text = """
[Avatar: 缘生子]
**【闭关成功】**
本次闭关，你的修为最终增加了 **576** 点。

当前境界: 元婴初期
当前修为: **36030 / 500000**

你感到一阵疲惫，需要打坐调息 **10** 分钟方可再次闭关。
"""

        self.assertTrue(log_utils.record_cultivation_profile_from_text(
            actor,
            text,
            identity="缘生子",
            source="identity .闭关修炼",
        ))
        state = actor.state["avatars"]["缘生子"]
        self.assertEqual(state["level"], "元婴初期")
        self.assertEqual(state["cultivation_level"], "元婴初期")
        self.assertEqual(state["current_exp"], 36030)
        self.assertEqual(state["total_exp"], 500000)

    def test_edited_cultivation_delta_uses_single_avatar_username(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor.state = {
            "avatars": {
                "缘生子": {
                    "current_exp": 36030,
                    "total_exp": 500000,
                }
            }
        }
        actor.save_state = lambda: None
        msg = DummyMessage(10300752)
        text = """
**【野外历练 · 灵机暗藏】**
@kulipabp 在山涧残阵旁避开妖兽踪迹，采得一份机缘。
获得修为 **+6335**，获得 **【养魂木】x1**。
"""

        self.assertTrue(log_utils.is_relevant_game_bot_edited_message(actor, msg, text))
        self.assertTrue(log_utils.record_edited_cultivation_state_if_needed(actor, msg, text=text))
        self.assertEqual(actor.state["avatars"]["缘生子"]["current_exp"], 42365)

    def test_cultivation_delta_gain_and_loss(self):
        self.assertEqual(
            parse_cultivation_delta_text("野外历练结束，获得修为 **+336**，采得灵草若干。"),
            [336],
        )
        self.assertEqual(
            parse_cultivation_delta_text("闯塔失败，道心受挫，修为倒退了 **1,200** 点。"),
            [-1200],
        )

    def test_wait_time_line_identifier_and_minimum(self):
        actor = Cultivator.__new__(Cultivator)

        self.assertEqual(actor.parse_wait_time("登阶冷却：1小时2分钟3秒", line_identifier="登阶冷却"), 3723)
        self.assertEqual(
            actor.parse_wait_time("入梦寻图冷却：8小时\n共历心劫冷却：9小时", find_min=True),
            8 * 3600,
        )

    def test_divination_cooldown_reply_matches_command_family(self):
        text = "天机链路尚未重铸，请在 **7小时20分钟15秒** 后再试。"

        self.assertEqual(log_utils.text_response_family(text), "divination")
        self.assertTrue(log_utils.feedback_response_matches_command(".天机代卜", text))
        self.assertFalse(log_utils.feedback_response_conflicts(".天机代卜", text))

    def test_beast_roster_parser_ignores_return_title_and_preserves_injury(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        roster = """
【灵兽伙伴们】
- 六翼(一阶)(受伤，预计还需 1小时2分钟)
  - 种类: 天鹏
  - 经验: 12
  - 战力: 500
  - 体力: 45
- 青蛟(一阶)(休息中)
  - 种类: 蛟龙
  - 经验: 8
  - 战力: 420
  - 体力: 60
"""
        beasts = actor.parse_beasts_info(roster)

        self.assertEqual([b["full_name"] for b in beasts], ["六翼 (一阶)", "青蛟 (一阶)"])
        self.assertIn("受伤", beasts[0]["status"])
        self.assertEqual(beasts[0]["status_cd"], 3720)
        self.assertEqual(actor.parse_beasts_info("【灵兽归来】\n体力恢复若干。"), [])

    def test_beast_candidate_protects_low_stamina_focus_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "六翼", "species": "天鹏", "status": "休息中", "power": 900, "exp": 10, "stamina": 45},
            {"full_name": "青蛟", "species": "蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 60},
        ]

        candidates = actor.beast_action_candidates("abyss", cache, 30)

        self.assertEqual([b["full_name"] for b in candidates], ["青蛟"])

    def test_beast_steal_prefers_mahuateng_then_fallback(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 90},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 100},
        ]

        self.assertEqual(actor.select_beast_for_steal(cache)["full_name"], "麻花藤")

        cache[2]["status"] = "受伤"
        self.assertEqual(actor.select_beast_for_steal(cache)["full_name"], "青蛟")

    def test_pastured_beasts_remain_action_candidates(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "放养中", "power": 4096, "exp": 0, "stamina": 34},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "放养中", "power": 31, "exp": 0, "stamina": 100},
            {"full_name": "吞金兽", "species": "一阶噬金虫", "status": "放养中", "power": 28, "exp": 207, "stamina": 100},
        ]

        self.assertTrue(actor.can_attempt_steal_status("放养中"))
        self.assertTrue(actor.can_attempt_abyss_status("放养中"))
        self.assertTrue(actor.should_rest_before_abyss("放养中"))
        self.assertEqual(actor.select_beast_for_steal(cache)["full_name"], "麻花藤")
        self.assertEqual(actor.abyss_candidate_beasts(cache)[0]["full_name"], "麻花藤")

    def test_xiaohao_main_soul_pause_does_not_make_main_impending(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["问心子"]
        actor.state = {
            "main_soul_pause_until": (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
            "main_soul_pause_reason": "肉体破碎/元婴虚弱",
            "identity_pauses": {},
        }
        actor.save_state = lambda: None
        actor.custom_command_impending_wait = lambda identity: -1

        self.assertGreater(actor.main_soul_pause_seconds(), 0)
        self.assertEqual(actor.get_identity_impending_command_wait("主魂"), 999999)

    def test_yuanying_rebirth_pending_reply_pauses_and_clears_guard(self):
        actor = DummyCommon()
        actor.state = {"identity_pauses": {}, "next_field_training_time": ""}
        actor._command_send_guard = {
            ".查看闭关": {"times": [1.0], "blocked_until": 9999.0},
            ".野外历练 谨慎": {"times": [1.0], "blocked_until": 9999.0},
            ".野外历练 谨慎 (缘生子)": {"times": [1.0], "blocked_until": 9999.0},
        }
        actor._last_command_guard_block = {
            "key": ".野外历练 谨慎",
            "wait": 3600,
            "blocked_until": 9999.0,
            "reason": "command_guard",
            "identity": "主魂",
            "at": 1.0,
        }

        self.assertTrue(log_utils.feedback_response_matches_command(
            ".查看闭关", "*灵气稀薄，元婴欲夺舍却无力*"
        ))
        self.assertTrue(actor.record_identity_yuanying_recovery_from_text(
            "主魂", "*残婴飘荡，灵气稀薄，神通难展，欲夺舍重生*", source="fixture"
        ))

        self.assertGreater(actor.identity_pause_seconds("主魂"), 0)
        self.assertEqual(actor.state["main_soul_pause_reason"], "元婴虚弱/待夺舍重生")
        self.assertNotIn(".查看闭关", actor._command_send_guard)
        self.assertNotIn(".野外历练 谨慎", actor._command_send_guard)
        self.assertIn(".野外历练 谨慎 (缘生子)", actor._command_send_guard)
        self.assertGreater(common_seconds_until(actor.state["next_field_training_time"]), 0)

    def test_yuanying_rebirth_success_clears_pause_and_stale_guard(self):
        actor = DummyCommon()
        future = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "identity_pauses": {"主魂": {"until": future, "reason": "肉体破碎/元婴虚弱"}},
            "main_soul_pause_until": future,
            "main_soul_pause_reason": "肉体破碎/元婴虚弱",
            "yuanying_out_active": True,
            "yuanying_out_end_time": future,
            "last_field_training_time": "2026-06-14 10:00:00",
            "next_field_training_time": future,
        }
        actor._command_send_guard = {
            ".野外历练 谨慎": {"times": [1.0], "blocked_until": 9999.0},
        }

        text = "先前肉身陨落的 **@Gamling33** (原道号：竹平子)，其元婴已成功夺舍重生！"
        self.assertTrue(actor.record_identity_yuanying_recovery_from_text("主魂", text, source="manual .重生 2"))

        self.assertEqual(actor.identity_pause_seconds("主魂"), 0)
        self.assertEqual(actor.state["main_soul_pause_until"], "")
        self.assertFalse(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["yuanying_out_end_time"], "")
        self.assertNotIn(".野外历练 谨慎", actor._command_send_guard)
        self.assertLessEqual(common_seconds_until(actor.state["next_field_training_time"]), 1)

    def test_identity_pause_blocks_only_that_identity(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["问心子"]
        actor.state = {
            "identity_pauses": {
                "问心子": {
                    "until": (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": "肉体破碎/元婴虚弱",
                }
            },
            "avatars": {
                "问心子": {
                    "next_field_training_time": "",
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                }
            },
        }
        actor.save_state = lambda: None
        actor.custom_command_impending_wait = lambda identity: -1
        actor.get_avatar_state = lambda identity: actor.state["avatars"][identity]

        self.assertEqual(actor.get_identity_impending_command_wait("问心子"), 999999)
        self.assertNotEqual(actor.get_identity_impending_command_wait("主魂"), 999999)

    def test_dashboard_marks_only_paused_identity_panel(self):
        pause_until = (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
        state = {
            "identity_pauses": {"缘生子": {"until": pause_until, "reason": "肉体破碎/元婴虚弱"}},
            "avatars": {"缘生子": {}, "素心子": {}},
        }
        panels = build_command_panels("xiaohao", state)
        by_identity = {panel["identity"]: panel for panel in panels}

        self.assertEqual(by_identity["缘生子"]["commands"][0]["status"], "元婴虚弱暂停")
        self.assertNotEqual(by_identity["素心子"]["commands"][0]["status"], "元婴虚弱暂停")

    def test_steal_recalls_pastured_candidate_before_deploy(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "放养中", "power": 31, "exp": 0, "stamina": 100},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽休息 麻花藤":
                return "已将灵兽【麻花藤】召回休息。"
            if command == ".灵兽出战 麻花藤":
                return "已将灵兽【麻花藤】设为出战状态。"
            if command == ".灵兽偷菜":
                return "灵兽偷菜成功，获得【灵石】x1。"
            return ""

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_steal_with_candidate()))
        self.assertEqual(sent, [".灵兽休息 麻花藤", ".灵兽出战 麻花藤", ".灵兽偷菜"])
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "出战中")
        self.assertTrue(actor.state.get("next_steal_time"))

    def test_steal_pending_target_lock_counts_as_accepted(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "保龄球", "species": "一阶灵兽", "status": "出战中", "power": 120, "exp": 0, "stamina": 100},
            ],
        }
        actor.save_state = lambda: None
        sent = []
        pending_text = "灵兽已锁定目标：**@q** 的药园，其中一块灵田种着**【清灵草】**！\n正在准备动手..."

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return pending_text

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_steal_with_candidate()))
        self.assertEqual(sent, [".灵兽偷菜"])
        self.assertTrue(actor.state.get("next_steal_time"))

    def test_manual_steal_pending_target_lock_updates_cooldown(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        actor.save_state = lambda: None
        text = "灵兽已锁定目标：**@q** 的药园，其中一块灵田种着**【清灵草】**！\n正在准备动手..."

        self.assertTrue(actor.record_manual_beast_command_response(".灵兽偷菜", text))
        self.assertTrue(actor.state.get("next_steal_time"))

    def test_abyss_prefers_focus_beast_when_healthy(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "青蛟", "species": "一阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 100},
            {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
        ]

        self.assertEqual(actor.abyss_candidate_beasts(cache)[0]["full_name"], "六翼")

    def test_recent_profile_fallback_requires_username(self):
        actor = Cultivator.__new__(Cultivator)
        actor._recent_cultivation_profile_commands = None

        log_utils.record_recent_profile_command(actor, 1000, ".闭关修炼", "素缘子")

        text = """
**【闭关失败】**
当前境界: 炼气一层
当前修为: **0 / 100**
你感到一阵疲惫，需要打坐调息 **10** 分钟方可再次闭关。
"""

        self.assertEqual(log_utils.recent_profile_identity_for_text(actor, text, msg_id=1010), "")

    def test_meditation_feedback_rejects_other_identity_summary_for_main(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {"current_identity": "主魂"}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        event = asyncio.Event()
        actor.feedback_events = {1000: event}
        actor.feedback_commands = {1000: ".查看闭关"}
        actor.feedback_identities = {1000: "主魂"}
        actor.feedback_senders = {1000: 777}
        actor.last_feedback_text = {}
        actor.last_feedback_msg = {}
        msg = DummyMessage(1001)
        text = """
📜 **修士 ****@kulipabp**** 深度闭关总结**
**【深度闭关总结】**
本次深度闭关，你的修为最终变化了 **15246** 点！
"""

        self.assertFalse(log_utils.match_pending_feedback_by_id(actor, 1000, msg, text))
        self.assertFalse(event.is_set())

    def test_force_exit_restart_sends_deep_meditation_directly(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["素缘子"]
        actor.avatar_nicknames = {"素缘子": "老爱同学"}
        actor.state = {
            "avatars": {
                "素缘子": {
                    "in_deep_meditation": False,
                    "deep_meditation_end_time": "",
                    "meditation_restart_pending": True,
                    "meditation_restart_mode": "deep_only",
                    "next_meditation_time": "",
                    "nickname": "老爱同学",
                }
            }
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "你已进入深度闭关状态，神魂将自行吐纳 **8** 小时。"

        actor.send_and_wait_feedback_identity = fake_send

        self.assertTrue(asyncio.run(actor.restart_avatar_deep_meditation_direct("素缘子", "test")))
        self.assertEqual(sent, [("素缘子", ".深度闭关")])
        state = actor.get_avatar_state("素缘子")
        self.assertTrue(state["in_deep_meditation"])
        self.assertFalse(state["meditation_restart_pending"])
        self.assertEqual(state["meditation_restart_mode"], "")

    def test_meditation_cultivation_rest_does_not_defer_deep_restart(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {"avatars": {"缘生子": {"next_meditation_time": "", "next_meditation_retry_time": ""}}}
        actor.save_state = lambda: None

        retry = actor.defer_meditation_after_cultivation_cooldown(
            "缘生子",
            "你感到一阵疲惫，需要打坐调息 **14** 分钟方可再次闭关。",
            "fixture .闭关修炼",
        )

        self.assertEqual(retry, 0)
        state = actor.get_avatar_state("缘生子")
        self.assertEqual(state["next_meditation_time"], "")
        self.assertEqual(state["next_meditation_retry_time"], "")

    def test_main_deep_active_without_timer_does_not_reuse_stale_end_time(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "in_deep_meditation": False,
            "deep_meditation_end_time": "2099-01-01 00:00:00",
            "next_meditation_time": "2099-01-01 00:00:00",
            "next_meditation_retry_time": "2099-01-01 00:00:00",
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return ""

        actor.send_and_wait_feedback = fake_send

        ok = asyncio.run(actor.sync_main_deep_meditation_start(
            "你已在深度闭关之中。",
            "fixture",
        ))

        self.assertTrue(ok)
        self.assertEqual(sent, [".查看闭关"])
        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], "")
        self.assertEqual(actor.state["next_meditation_time"], "")
        self.assertEqual(actor.state["next_meditation_retry_time"], "")

    def test_unowned_passive_exit_text_does_not_clear_main_meditation(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子", "缘生子", "素缘子"]
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": {"waaiging"}}
        actor.command_avatar_map = {}
        actor._current_identity = "主魂"
        actor.my_info = SimpleNamespace(username="Waaiging", first_name="")
        actor.notify_users = []
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "in_deep_meditation": True,
            "deep_meditation_end_time": future,
        }
        actor.save_state = lambda: None
        event_set = []
        actor.meditation_state_event = SimpleNamespace(set=lambda: event_set.append(True))

        msg = SimpleNamespace(
            id=123,
            text="✨ **天道感应**：检测到 @Lvdoumiao 功成圆满，神魂正在归位...",
            sender_id=999,
            reply_to=None,
        )
        actor.maybe_record_avatar_passive_states(msg)

        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], future)
        self.assertEqual(event_set, [])

    def test_unowned_passive_ongoing_text_does_not_overwrite_main_meditation(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子", "缘生子", "素缘子"]
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": {"waaiging"}}
        actor.command_avatar_map = {}
        actor._current_identity = "主魂"
        actor.my_info = SimpleNamespace(username="Waaiging", first_name="")
        actor.notify_users = []
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "in_deep_meditation": True,
            "deep_meditation_end_time": future,
            "next_meditation_time": "",
        }
        actor.save_state = lambda: None
        event_set = []
        actor.meditation_state_event = SimpleNamespace(set=lambda: event_set.append(True))

        msg = SimpleNamespace(
            id=125,
            text="你正在深度闭关，预计还需 **5小时42分钟17秒** 即可功成圆满。",
            sender_id=999,
            reply_to=None,
        )
        actor.maybe_record_avatar_passive_states(msg)

        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], future)
        self.assertEqual(actor.state["next_meditation_time"], "")
        self.assertEqual(event_set, [])

    def test_owned_passive_settlement_clears_main_meditation(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子", "缘生子", "素缘子"]
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": {"waaiging"}}
        actor.command_avatar_map = {}
        actor._current_identity = "主魂"
        actor.my_info = SimpleNamespace(username="Waaiging", first_name="")
        actor.notify_users = []
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "in_deep_meditation": True,
            "deep_meditation_end_time": future,
        }
        actor.save_state = lambda: None
        event_set = []
        actor.meditation_state_event = SimpleNamespace(set=lambda: event_set.append(True))

        msg = SimpleNamespace(
            id=124,
            text="📜 **修士 ****@Waaiging**** 深度闭关总结**\n本次深度闭关，你的修为最终变化了 **23973** 点！",
            sender_id=999,
            reply_to=None,
        )
        actor.maybe_record_avatar_passive_states(msg)

        self.assertFalse(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], "")
        self.assertEqual(event_set, [True])

    def test_meditation_defer_ignores_ordinary_cultivation_time(self):
        actor = DummyCommon()
        sooner = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        later = (datetime.now() + timedelta(minutes=11)).strftime("%Y-%m-%d %H:%M:%S")

        self.assertEqual(actor.meditation_defer_until({
            "next_meditation_retry_time": sooner,
            "next_meditation_time": later,
        }), sooner)

    def test_avatar_attention_ignores_ordinary_cultivation_time(self):
        future = (datetime.now() + timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S")
        cases = [
            (Cultivator, "缘生子"),
            (SubCultivator, "厚土"),
            (CultivatorXiaoHao, "素心子"),
        ]

        for cls, avatar in cases:
            actor = cls.__new__(cls)
            actor.avatars = [avatar]
            actor.avatar_nicknames = {avatar: ""}
            actor.state = {
                "avatars": {
                    avatar: {
                        "next_meditation_time": future,
                        "next_meditation_retry_time": "",
                        "meditation_restart_pending": True,
                    }
                }
            }
            actor.save_state = lambda: None

            self.assertTrue(actor.avatar_meditation_needs_attention(avatar), cls.__name__)

    def test_xiaohao_avatar_settle_starts_deep_after_cultivation_rest(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        actor.state = {"avatars": {"素心子": {}}}
        actor.save_state = lambda: None
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            if command == ".查看闭关":
                return "你并未处于深度闭关之中。"
            if command == ".闭关修炼":
                return "你感到一阵疲惫，需要打坐调息 **12** 分钟方可再次闭关。"
            if command == ".深度闭关":
                return "你已进入深度闭关状态，神魂将自行吐纳 **8小时**。"
            return "unexpected"

        actor.send_and_wait_feedback_identity = fake_send

        wait = asyncio.run(actor._avatar_settle_and_start_deep("素心子"))

        self.assertEqual(sent, [("素心子", ".查看闭关"), ("素心子", ".闭关修炼"), ("素心子", ".深度闭关")])
        self.assertEqual(wait, 300)
        state = actor.get_avatar_state("素心子")
        self.assertTrue(state["in_deep_meditation"])
        self.assertFalse(state["meditation_restart_pending"])
        self.assertEqual(state["next_meditation_retry_time"], "")
        self.assertTrue(state["deep_meditation_guard_until"])

    def test_xiaohao_avatar_meditation_guard_suppresses_polluted_check_only(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "avatars": {
                "素心子": {
                    "in_deep_meditation": False,
                    "deep_meditation_end_time": "",
                    "deep_meditation_guard_until": future,
                    "meditation_restart_pending": True,
                    "next_meditation_retry_time": "",
                }
            }
        }
        actor.save_state = lambda: None

        self.assertFalse(actor.avatar_meditation_needs_attention("素心子"))

        actor.mark_avatar_meditation_restart_pending("素心子", "passive settlement")
        state = actor.get_avatar_state("素心子")
        self.assertEqual(state["deep_meditation_guard_until"], future)
        self.assertFalse(actor.avatar_meditation_needs_attention("素心子"))

        main_state = {"deep_meditation_end_time": future, "deep_meditation_guard_until": ""}
        self.assertTrue(actor.ensure_meditation_guard_from_end_time(main_state))
        self.assertEqual(main_state["deep_meditation_guard_until"], future)
        self.assertTrue(actor.meditation_guard_active_for_state(main_state))

    def test_xiaohao_avatar_guard_skips_early_meditation_check_send(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.active_atomic_task = None
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "avatars": {
                "素心子": {
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": future,
                    "deep_meditation_guard_until": future,
                }
            }
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_raw(command, *args, **kwargs):
            sent.append(command)
            return "sent"

        actor._send_and_wait_feedback_raw = fake_raw

        resp = asyncio.run(actor.send_and_wait_feedback_identity("素心子", ".查看闭关"))

        self.assertEqual(sent, [])
        self.assertIn("正在深度闭关", resp)
        self.assertIn("预计还需", resp)

    def test_xiaohao_main_guard_skips_early_meditation_check_send(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = []
        actor.active_atomic_task = None
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "in_deep_meditation": True,
            "deep_meditation_end_time": future,
            "deep_meditation_guard_until": future,
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_raw(command, *args, **kwargs):
            sent.append(command)
            return "sent"

        actor._send_and_wait_feedback_raw = fake_raw

        resp = asyncio.run(actor.send_and_wait_feedback(".查看闭关"))

        self.assertEqual(sent, [])
        self.assertIn("正在深度闭关", resp)
        self.assertIn("预计还需", resp)

    def test_pasture_precheck_rests_focus_beast_instead_of_deploying(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "出战中", "power": 4096, "exp": 0, "stamina": 34},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 100},
            ],
            "best_beast_name": "六翼",
            "best_beast_status": "出战中",
            "best_beast_stamina": 34,
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return "已将灵兽【六翼】召回休息。"

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.ensure_focus_beast_ready_for_pasture()))
        self.assertEqual(sent, [".灵兽休息 六翼"])
        self.assertEqual(actor.get_cached_beast_by_name("六翼")["status"], "休息中")

    def test_concubine_status_blocks_chain_during_active_voyage(self):
        actor = DummyConcubine()
        status = """
【道心侍妾】
远航状态：进行中，剩余 1小时
入梦寻图冷却：可施展
共历心劫冷却：2小时10分钟
天机代卜冷却：无
侍妾远航冷却：进行中，剩余 1小时
"""

        self.assertTrue(actor.parse_concubine_status(status))
        self.assertTrue(actor.state["concubine_voyage_active"])
        self.assertGreater(seconds_until(actor.state["next_dream_map_time"]), 50 * 60)
        self.assertGreater(seconds_until(actor.state["next_heart_trial_time"]), 50 * 60)

    def test_trusted_avatar_concubine_name_migrates_for_any_identity(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.account_key = "sub"
        actor.avatars = ["寻真子"]
        actor.state = {"avatars": {"寻真子": {"concubine_name": "若兰"}}}
        actor.save_state = lambda: None

        status = """
[Avatar: 寻真子]
**你的道心侍妾: 【辛如音】** (状态: 随行中)

**【第二期机缘】**
- 入梦寻图冷却: 479分钟
- 共历心劫冷却: 可施展
- 天机代卜冷却: 719分钟
"""

        self.assertTrue(actor.concubine_status_matches_identity(status, "寻真子"))
        self.assertEqual(actor.state["avatars"]["寻真子"]["concubine_name"], "辛如音")
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            return DummyMessage(2201, text=status)

        actor.send_and_wait_feedback_identity = fake_send

        self.assertTrue(asyncio.run(actor.sync_avatar_heart_trial_cooldown_after_failure(
            "寻真子",
            "fixture anchor lost",
        )))
        self.assertEqual(sent, [("寻真子", ".我的侍妾")])
        self.assertGreater(common_seconds_until(actor.state["avatars"]["寻真子"]["next_heart_trial_time"]), 500)

    def test_main_concubine_name_migrates_from_trusted_unmarked_status(self):
        actor = DummyConcubine()
        actor.state["concubine_name"] = "慕沛灵"
        status = """
**你的道心侍妾: 【南宫婉】** (状态: 随行中)

**【第二期机缘】**
- 入梦寻图冷却: 479分钟
- 共历心劫冷却: 可施展
- 天机代卜冷却: 719分钟
"""

        self.assertTrue(actor.concubine_status_matches_identity(status, "主魂"))
        self.assertEqual(actor.state["concubine_name"], "南宫婉")
        self.assertEqual(actor.state["last_concubine_status_mismatch"], "")

    def test_main_concubine_name_rejects_avatar_marked_status(self):
        actor = DummyConcubine()
        actor.state["concubine_name"] = "慕沛灵"
        status = """
[Avatar: 缘生子]
**你的道心侍妾: 【瑶光】** (状态: 随行中)

**【第二期机缘】**
- 入梦寻图冷却: 479分钟
- 共历心劫冷却: 可施展
- 天机代卜冷却: 719分钟
"""

        self.assertFalse(actor.concubine_status_matches_identity(status, "主魂"))
        self.assertEqual(actor.state["concubine_name"], "慕沛灵")
        self.assertIn("got=瑶光", actor.state["last_concubine_status_mismatch"])

    def test_main_heart_trial_anchor_lost_syncs_cooldown_without_unknown_alert(self):
        actor = DummyConcubine()
        ready_status = """
【道心侍妾】
侍妾：慕沛灵
入梦寻图冷却：1小时
共历心劫冷却：无
天机代卜冷却：1小时
侍妾远航冷却：无
"""
        cooldown_status = """
【道心侍妾】
侍妾：慕沛灵
入梦寻图冷却：1小时
共历心劫冷却：1小时10分钟
天机代卜冷却：1小时
侍妾远航冷却：无
"""
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if len(sent) == 1:
                return DummyMessage(2001, text=ready_status)
            if command == ".共历心劫":
                return DummyMessage(2002, text="心劫锚点已散，需重新引动天劫。")
            return DummyMessage(2003, text=cooldown_status)

        actor.send_and_wait_feedback = fake_send
        alerts = []
        old_notify = concubine_features.notify_unrecognized_response
        concubine_features.notify_unrecognized_response = lambda *args, **kwargs: alerts.append(args)
        try:
            asyncio.run(actor.execute_heart_trial())
        finally:
            concubine_features.notify_unrecognized_response = old_notify

        self.assertEqual(sent, [".我的侍妾", ".共历心劫", ".我的侍妾"])
        self.assertEqual(alerts, [])
        self.assertGreater(seconds_until(actor.state["next_heart_trial_time"]), 60 * 60)

    def test_main_avatar_heart_trial_anchor_lost_syncs_identity_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        status = """
【道心侍妾】
侍妾：瑶光
入梦寻图冷却：1小时
共历心劫冷却：2小时5分钟
天机代卜冷却：1小时
侍妾远航冷却：无
"""
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            return DummyMessage(2101, text=status)

        actor.send_and_wait_feedback_identity = fake_send

        self.assertTrue(asyncio.run(actor.sync_avatar_heart_trial_cooldown_after_failure(
            "缘生子",
            "fixture anchor lost",
        )))
        self.assertEqual(sent, [("缘生子", ".我的侍妾")])
        self.assertGreater(common_seconds_until(actor.state["avatars"]["缘生子"]["next_heart_trial_time"]), 2 * 3600)

    def test_formation_success_and_pending_fixtures(self):
        actor = SubCultivator.__new__(SubCultivator)

        self.assertTrue(actor.is_formation_pending("【周天星斗大阵-启】正在布设大阵，尚需 2 位道友助阵。"))
        self.assertTrue(actor.is_formation_success("【周天星斗大阵-成】大阵已成，星辉流转。"))

    def test_main_formation_assist_allows_meditation_without_force_exit(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["素缘子"]
        actor.avatar_nicknames = {}
        actor.state = {"avatars": {"素缘子": {"in_deep_meditation": True}}}
        actor.save_state = lambda: None
        sent = []

        async def fake_send(*args, **kwargs):
            sent.append(args)
            return "should not send"

        actor.send_and_wait_feedback_identity = fake_send

        self.assertTrue(asyncio.run(actor.prepare_avatar_for_formation_assist("素缘子")))
        self.assertEqual(sent, [])

    def test_xiaohao_formation_assist_allows_meditation_without_force_exit(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.state = {"avatars": {"素心子": {"in_deep_meditation": True}}}
        actor.save_state = lambda: None
        sent = []

        async def fake_send(*args, **kwargs):
            sent.append(args)
            return "should not send"

        actor.send_and_wait_feedback_identity = fake_send

        self.assertTrue(asyncio.run(actor.prepare_avatar_for_formation_assist("素心子")))
        self.assertEqual(sent, [])

    def test_sub_formation_assist_skips_cooldown_and_allows_meditation(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatars = ["厚土", "缘生子", "寻真子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"ding303": "寻真子"}
        actor.state = {
            "avatars": {
                "厚土": {"next_formation_time": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")},
                "缘生子": {"in_deep_meditation": True, "deep_meditation_end_time": (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")},
                "寻真子": {},
            }
        }
        actor.save_state = lambda: None

        self.assertEqual(actor.formation_invite_actor_identity("@Ding303 正在布设大阵"), "寻真子")
        self.assertFalse(asyncio.run(actor.prepare_avatar_for_formation_assist("厚土")))
        self.assertTrue(asyncio.run(actor.prepare_avatar_for_formation_assist("缘生子")))

    def test_global_spirit_tree_mature_status_from_untracked_reply_is_accepted(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        scheduled = []
        actor.schedule_spirit_tree_harvest_once = lambda reason="mature", identity="主魂": scheduled.append((identity, reason))
        msg = DummyMessage(10212367, reply_to_msg_id=10212365)
        text = """
**【落云宗 · 灵眼之树】**
✨ **状态**: 成熟采摘期
⏳ **剩余**: 23小时38分钟46秒
🍎 **果实**: **极品 (万年灵木)**
"""

        self.assertTrue(actor.maybe_record_spirit_tree_passive_message(msg, text, source="fixture"))
        self.assertEqual(actor.state["spirit_tree_status"], "成熟采摘期")
        self.assertTrue(actor.state["spirit_tree_harvest_pending"])
        self.assertEqual(actor.state["avatars"]["缘生子"], {})
        self.assertEqual(scheduled, [("主魂", "fixture")])

    def test_spirit_tree_state_is_shared_for_luoyun_world_event(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        scheduled = []
        actor.schedule_spirit_tree_harvest_once = lambda reason="mature", identity="缘生子": scheduled.append((identity, reason))
        msg = DummyMessage(10222367, reply_to_msg_id=10222365)
        text = """
**【落云宗 · 灵眼之树】**
✨ **状态**: 成熟采摘期
⏳ **剩余**: 23小时38分钟46秒
🍎 **果实**: **极品 (万年灵木)**
"""

        self.assertTrue(actor.maybe_record_spirit_tree_passive_message(
            msg, text, source="fixture", identity="主魂"
        ))
        self.assertEqual(actor.state["spirit_tree_status"], "成熟采摘期")
        self.assertTrue(actor.state["spirit_tree_harvest_pending"])
        self.assertNotEqual(actor.state["avatars"]["缘生子"].get("spirit_tree_status"), "成熟采摘期")
        self.assertEqual(scheduled, [("主魂", "fixture")])

    def test_spirit_tree_guide_text_does_not_trigger_harvest(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        scheduled = []
        actor.schedule_spirit_tree_harvest_once = lambda reason="mature", identity="主魂": scheduled.append((identity, reason))
        text = """
**你所属的宗门: 【落云宗】**
**灵眼之树要诀**:
`.灵树状态` 查看本轮走向；采摘期开启后再用 `.采摘灵果` 收取本轮机缘。
"""

        self.assertFalse(actor.spirit_tree_text_indicates_mature(text))
        self.assertFalse(actor.maybe_record_spirit_tree_passive_message(None, text, source="fixture", identity="主魂"))
        self.assertEqual(scheduled, [])

    def test_avatar_defaults_do_not_recreate_spirit_tree_state(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {"avatars": {}}
        actor.save_state = lambda: None

        actor.ensure_avatar_states()

        avatar_state = actor.state["avatars"]["缘生子"]
        self.assertFalse(any("spirit_tree" in key for key in avatar_state))

    def test_spirit_tree_no_irrigation_reply_preserves_mature_until(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        mature_until = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": mature_until,
            "next_spirit_tree_irrigation_time": mature_until,
            "spirit_tree_harvest_pending": False,
            "spirit_tree_harvested_in_mature_period": True,
            "spirit_tree_harvest_attempted_in_mature_period": True,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.maybe_record_spirit_tree_passive_message(
            None,
            "灵眼之树已然成熟或正遭劫难，此刻无需灌溉，静待或守护即可。",
            source="fixture",
            identity="主魂",
        ))
        self.assertEqual(actor.state["spirit_tree_status"], "成熟采摘期")
        self.assertEqual(actor.state["spirit_tree_mature_until"], mature_until)
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], mature_until)

    def test_spirit_tree_harvest_lock_blocks_repeat_sends_for_48_hours(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_spirit_tree_irrigation_time": (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "spirit_tree_harvest_pending": True,
            "spirit_tree_harvested_in_mature_period": False,
            "spirit_tree_harvest_attempted_in_mature_period": False,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.active_atomic_task = None
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command, kwargs))
            return "灵果尚未成熟，或采摘期已过。"

        async def run_once_then_retry():
            actor.startup_done = asyncio.Event()
            actor.startup_done.set()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.send_and_wait_feedback_identity = fake_send
            await actor.execute_spirit_tree_harvest_once("fixture", identity="主魂")
            actor.state["spirit_tree_status"] = "成熟采摘期"
            actor.state["spirit_tree_mature_until"] = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
            actor.state["spirit_tree_harvest_attempted_in_mature_period"] = False
            await actor.execute_spirit_tree_harvest_once("fixture retry", identity="主魂")

        started = datetime.now()
        asyncio.run(run_once_then_retry())

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0:2], ("主魂", ".采摘灵果"))
        self.assertEqual(sent[0][2].get("max_retries"), 0)
        lock_until = datetime.strptime(actor.state["next_spirit_tree_harvest_time"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(lock_until, started + timedelta(hours=47, minutes=59))
        self.assertLessEqual(lock_until, datetime.now() + timedelta(hours=48, seconds=5))

    def test_spirit_tree_harvest_reject_preserves_irrigation_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        future_irrigation = (datetime.now() + timedelta(hours=1, minutes=51, seconds=23)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_spirit_tree_irrigation_time": future_irrigation,
            "spirit_tree_harvest_attempted_in_mature_period": True,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.record_spirit_tree_harvest_response(
            "灵果尚未成熟，或采摘期已过。",
            identity="主魂",
        ))
        self.assertEqual(actor.state["spirit_tree_status"], "灌溉期")
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], future_irrigation)
        self.assertTrue(actor.state["spirit_tree_harvest_attempted_in_mature_period"])

    def test_spirit_tree_harvest_no_contribution_is_known_reject(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.record_spirit_tree_harvest_response(
            "你未曾为灵树灌溉分毫，无功不受禄。",
            identity="主魂",
        ))
        self.assertEqual(actor.state["spirit_tree_status"], "灌溉期")
        self.assertFalse(actor.state["spirit_tree_harvest_pending"])

    def test_spirit_tree_no_irrigation_followup_status_refreshes_mature_timer(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        old_until = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": old_until,
            "next_spirit_tree_irrigation_time": old_until,
            "spirit_tree_harvest_pending": False,
            "spirit_tree_harvested_in_mature_period": False,
            "spirit_tree_harvest_attempted_in_mature_period": True,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.dashboard_command_paused = lambda command, identity="": False
        scheduled = []
        actor.schedule_spirit_tree_harvest_once = lambda reason="mature", identity="主魂": scheduled.append((identity, reason))
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            if command == ".灵树灌溉":
                return "灵眼之树已然成熟或正遭劫难，此刻无需灌溉，静待或守护即可。"
            if command == ".灵树状态":
                return """
**【落云宗 · 灵眼之树】**
✨ **状态**: 成熟采摘期
⏳ **剩余**: 23小时38分钟46秒
🍎 **果实**: **极品 (万年灵木)**
"""
            return ""

        actor.send_and_wait_feedback_identity = fake_send

        asyncio.run(actor._identity_spirit_tree_irrigation_check("主魂"))

        self.assertEqual(sent, [("主魂", ".灵树灌溉"), ("主魂", ".灵树状态")])
        self.assertEqual(actor.state["spirit_tree_status"], "成熟采摘期")
        refreshed_until = datetime.strptime(actor.state["spirit_tree_mature_until"], "%Y-%m-%d %H:%M:%S")
        self.assertGreater(refreshed_until, datetime.now() + timedelta(hours=23))
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], actor.state["spirit_tree_mature_until"])
        self.assertTrue(actor.state["spirit_tree_harvest_attempted_in_mature_period"])
        self.assertFalse(actor.state["spirit_tree_harvest_pending"])
        self.assertEqual(scheduled, [])

    def test_spirit_tree_irrigation_cooldown_reply_sets_exact_retry_time(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.dashboard_command_paused = lambda command, identity="": False
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "地脉灵气尚未恢复，请在 **1小时51分钟23秒** 后再来灌溉。"

        actor.send_and_wait_feedback_identity = fake_send

        started = datetime.now()
        asyncio.run(actor._identity_spirit_tree_irrigation_check("主魂"))

        self.assertEqual(sent, [("主魂", ".灵树灌溉")])
        self.assertEqual(actor.state["spirit_tree_status"], "灌溉期")
        retry_at = datetime.strptime(actor.state["next_spirit_tree_irrigation_time"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(retry_at, started + timedelta(seconds=6680))
        self.assertLessEqual(retry_at, datetime.now() + timedelta(seconds=6685))
        self.assertEqual(
            actor.state["spirit_tree_irrigation_times"]["主魂"],
            actor.state["next_spirit_tree_irrigation_time"],
        )

    def test_main_spirit_tree_irrigation_sends_second_safety_attempt(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.dashboard_command_paused = lambda command, identity="": False
        sent = []
        responses = [
            """
**【🌿 灵树灌溉】**
当前环境: 生机萎靡 (需 木/森/草)
你注入了: **木行** 灵气
------------------------------
🌳 **成熟度**: 10.94% -> **11.05%**
""",
            "地脉灵气尚未恢复，请在 **1小时59分钟55秒** 后再来灌溉。",
        ]

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command, kwargs.get("max_retries"), kwargs.get("suppress_no_response_alert")))
            return responses[len(sent) - 1]

        actor.send_and_wait_feedback_identity = fake_send

        started = datetime.now()
        asyncio.run(actor._identity_spirit_tree_irrigation_check("主魂"))

        self.assertEqual(
            [(identity, command) for identity, command, _retries, _suppress in sent],
            [("主魂", ".灵树灌溉"), ("主魂", ".灵树灌溉")],
        )
        self.assertEqual([item[2] for item in sent], [0, 0])
        self.assertEqual([item[3] for item in sent], [True, True])
        retry_at = datetime.strptime(actor.state["next_spirit_tree_irrigation_time"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(retry_at, started + timedelta(seconds=7190))
        self.assertLessEqual(retry_at, datetime.now() + timedelta(seconds=7200))

    def test_spirit_tree_irrigation_cooldowns_are_per_identity(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        main_time = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": main_time,
            "spirit_tree_irrigation_times": {"主魂": main_time},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.dashboard_command_paused = lambda command, identity="": False
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "地脉灵气尚未恢复，请在 **1小时51分钟23秒** 后再来灌溉。"

        actor.send_and_wait_feedback_identity = fake_send

        started = datetime.now()
        asyncio.run(actor._identity_spirit_tree_irrigation_check("缘生子"))

        self.assertEqual(sent, [("缘生子", ".灵树灌溉")])
        self.assertEqual(actor.state["spirit_tree_irrigation_times"]["主魂"], main_time)
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], main_time)
        avatar_retry = datetime.strptime(actor.state["spirit_tree_irrigation_times"]["缘生子"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(avatar_retry, started + timedelta(seconds=6680))
        self.assertLessEqual(avatar_retry, datetime.now() + timedelta(seconds=6685))
        self.assertFalse(any("spirit_tree" in key for key in actor.state["avatars"]["缘生子"]))

    def test_spirit_tree_success_cooldown_uses_command_sent_time(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        sent_at = (datetime.now() - timedelta(seconds=43)).strftime("%Y-%m-%d %H:%M:%S")
        success_text = """
**【🌿 灵树灌溉】**
当前环境: 生机萎靡 (需 木/森/草)
你注入了: **木行** 灵气
------------------------------
🌳 **成熟度**: 10.94% -> **11.05%**
"""

        self.assertTrue(actor.record_spirit_tree_irrigation_state(
            success_text,
            source="fixture",
            identity="主魂",
            success_base_time=sent_at,
        ))

        retry_at = datetime.strptime(actor.state["spirit_tree_irrigation_times"]["主魂"], "%Y-%m-%d %H:%M:%S")
        expected = datetime.strptime(sent_at, "%Y-%m-%d %H:%M:%S") + timedelta(hours=2)
        self.assertEqual(retry_at, expected)

    def test_spirit_tree_irrigation_is_time_critical_for_deferral(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        due = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {"缘生子": due},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": False

        self.assertTrue(actor.time_critical_identity_command(".灵树灌溉"))
        self.assertLess(actor.time_critical_identity_wait("缘生子", exclude_command=".抚摸法宝 青竹蜂云剑（神雷版）"), 31)
        self.assertEqual(actor.time_critical_identity_wait("缘生子", exclude_command=".灵树灌溉"), -1)

    def test_spirit_tree_wait_uses_switch_lead_for_other_identity(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        due = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {"缘生子": due},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        actor.current_identity = "主魂"
        other_identity_wait = actor.spirit_tree_next_wait_seconds("缘生子")
        actor.current_identity = "缘生子"
        same_identity_wait = actor.spirit_tree_next_wait_seconds("缘生子")

        self.assertLess(other_identity_wait, same_identity_wait)
        self.assertGreaterEqual(same_identity_wait - other_identity_wait, 2)
        self.assertLess(other_identity_wait, 30)

    def test_spirit_tree_irrigation_check_uses_switch_lead_window(self):
        async def run_with_identity(current_identity):
            actor = Cultivator.__new__(Cultivator)
            actor.avatars = ["缘生子"]
            actor.avatar_nicknames = {}
            due = (datetime.now() + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
            actor.state = {
                "spirit_tree_status": "灌溉期",
                "next_spirit_tree_irrigation_time": "",
                "spirit_tree_irrigation_times": {"缘生子": due},
                "avatars": {"缘生子": {}},
            }
            actor.save_state = lambda: None
            actor.current_identity = current_identity
            actor.identity_pause_seconds = lambda identity: 0
            actor.dashboard_command_paused = lambda command, identity="": False
            sent = []

            async def fake_send(identity, command, *args, **kwargs):
                sent.append((identity, command))
                return "地脉灵气尚未恢复，请在 **1秒** 后再来灌溉。"

            actor.send_and_wait_feedback_identity = fake_send
            await actor._identity_spirit_tree_irrigation_check("缘生子")
            return sent

        self.assertEqual(
            asyncio.run(run_with_identity("主魂")),
            [("缘生子", ".灵树灌溉")],
        )
        self.assertEqual(asyncio.run(run_with_identity("缘生子")), [])

    def test_avatar_min_cd_uses_spirit_tree_switch_lead_without_sixty_second_floor(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_features = {"缘生子": {"spirit_tree_irrigation": True}}
        due = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {"缘生子": due},
            "avatars": {"缘生子": {"deep_meditation_end_time": "2099-01-01 00:00:00"}},
        }
        actor.save_state = lambda: None
        actor.current_identity = "主魂"
        actor.identity_pause_seconds = lambda identity: 0
        actor.avatar_meditation_needs_attention = lambda avatar: False
        actor.dashboard_command_paused = lambda command, identity="": False

        wait = asyncio.run(actor._get_avatar_min_cd_seconds())

        self.assertLess(wait, 30)
        self.assertGreaterEqual(wait, 25)

    def test_untrusted_spirit_tree_irrigation_reply_does_not_move_main_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        main_time = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": main_time,
            "spirit_tree_irrigation_times": {"主魂": main_time},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        text = """
**【🌿 灵树灌溉】**
当前环境: 生机萎靡 (需 木/森/草)
你注入了: **木行** 灵气
------------------------------
🌳 **成熟度**: 10.94% -> **11.05%**
"""

        self.assertFalse(actor.maybe_record_spirit_tree_passive_message(
            DummyMessage(10278232),
            text,
            source="new message",
        ))
        self.assertEqual(actor.state["spirit_tree_irrigation_times"]["主魂"], main_time)
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], main_time)

    def test_unknown_spirit_tree_irrigation_text_preserves_state(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        mature_until = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": mature_until,
            "next_spirit_tree_irrigation_time": mature_until,
            "spirit_tree_irrigation_times": {"主魂": mature_until},
            "spirit_tree_harvest_pending": True,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor._unrecognized_response_alerts = {}
        text = "灵树灌溉可与 `.灵树状态` 配合查看，不是本次灌溉结果。"

        self.assertFalse(actor.record_spirit_tree_irrigation_state(text, source="fixture", identity="主魂"))
        self.assertEqual(actor.state["spirit_tree_status"], "成熟采摘期")
        self.assertEqual(actor.state["spirit_tree_mature_until"], mature_until)
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], mature_until)
        self.assertEqual(actor.state["spirit_tree_irrigation_times"]["主魂"], mature_until)
        self.assertTrue(actor.state["spirit_tree_harvest_pending"])

    def test_tracked_spirit_tree_irrigation_reply_sets_identity_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor._manual_command_ids = [10269126]
        actor._manual_command_texts = {10269126: ".灵树灌溉"}
        actor._manual_command_identities = {10269126: "主魂"}
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        text = """
**【🌿 灵树灌溉】**
当前环境: 生机萎靡 (需 木/森/草)
你注入了: **木行** 灵气
------------------------------
🌳 **成熟度**: 0.77% -> **0.88%**
"""

        started = datetime.now()
        self.assertTrue(actor.maybe_record_spirit_tree_passive_message(
            DummyMessage(10269127, reply_to_msg_id=10269126),
            text,
            source="manual .灵树灌溉",
        ))
        retry_at = datetime.strptime(actor.state["spirit_tree_irrigation_times"]["主魂"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(retry_at, started + timedelta(hours=1, minutes=59, seconds=55))
        self.assertLessEqual(retry_at, datetime.now() + timedelta(hours=2, seconds=5))
        self.assertEqual(
            actor.state["next_spirit_tree_irrigation_time"],
            actor.state["spirit_tree_irrigation_times"]["主魂"],
        )

    def test_spirit_tree_status_text_does_not_overwrite_irrigation_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        main_time = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": main_time,
            "spirit_tree_irrigation_times": {"主魂": main_time},
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        text = """
**【落云宗 · 灵眼之树】**
🌿 **环境**: 生机萎靡 (需 木/森/草)
🌲 **进度**:
🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 8.81%
🔄 **阶段**: 1 / 4
📊 **倾向**: 木:3672 水:1896 火:136
👤 **你的当前状态**: 88 点
🌰 **奉养灵树**: 可用 `.奉养灵树` 献枝 (1/1) | 当前会优先走献枝
🧪 **长期资粮**: 凝液 木髓 19/12 | 常规炼枝 木髓 19/24 + 灵液 0/1 | 纯木髓炼枝 19/36
"""

        self.assertTrue(actor.maybe_record_spirit_tree_passive_message(
            None,
            text,
            source="manual .灵树状态",
            identity="主魂",
        ))
        self.assertEqual(actor.state["spirit_tree_irrigation_times"]["主魂"], main_time)
        self.assertEqual(actor.state["next_spirit_tree_irrigation_time"], main_time)

    def test_spirit_tree_harvest_pending_text_waits_for_edited_result(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {"spirit_tree_harvest_pending": False, "avatars": {"缘生子": {}}}
        actor.save_state = lambda: None

        self.assertFalse(actor.record_spirit_tree_harvest_response("你来到灵眼之树下，拿出宗门贡献令，核对天道榜单..."))
        self.assertFalse(actor.state.get("spirit_tree_harvested_in_mature_period", False))
        self.assertTrue(actor.record_spirit_tree_harvest_response("**【灵果入腹 · 造化自生】**\n你摘下一枚**【万年灵木果】**...\n💪 **修为增长**: +28000"))
        self.assertTrue(actor.state["spirit_tree_harvested_in_mature_period"])

    def test_resource_and_inventory_parsers(self):
        changes = parse_resource_changes_from_text(
            "传功玉简已记录！获得了 **30** 点贡献。\n"
            "收集完成，获得了【星辰精华】x2。\n"
            "你消耗了 **100** 点灵石。"
        )
        compact = {(item["name"], item["amount"]) for item in changes}

        self.assertIn(("贡献", 30), compact)
        self.assertIn(("星辰精华", 2), compact)
        self.assertIn(("灵石", -100), compact)

        inventory = parse_inventory_items_from_text("【储物袋】\n【养魂木】x3\n灵石：1200")
        self.assertIn({"name": "养魂木", "amount": 3}, inventory)
        self.assertIn({"name": "灵石", "amount": 1200}, inventory)

        self.assertEqual(
            parse_inventory_items_from_text("道友 @cupaopao 购得 **【幸运符】x1**，物资已发放到储物袋！"),
            [],
        )

    def test_resource_parser_skips_status_counters(self):
        changes = parse_resource_changes_from_text(
            "【天机前兆】本次心劫入场消耗降低，首轮评分+1。\n"
            "🌱 养树底蕴 +10\n"
            "- 体力 -20 点\n"
            "心情 +5、羁绊 +6，你获得 10 点宗门贡献！\n"
            "- 灵石 +98\n"
            "获得【玄铁剑图纸】！\n"
            "闭关奇遇：8 次"
        )
        compact = {(item["name"], item["amount"]) for item in changes}

        self.assertIn(("灵石", 98), compact)
        self.assertIn(("玄铁剑图纸", 1), compact)
        self.assertIn(("宗门贡献", 10), compact)
        self.assertNotIn(("首轮评分", 1), compact)
        self.assertNotIn(("养树底蕴", 10), compact)
        self.assertNotIn(("体力", -20), compact)
        self.assertNotIn(("奇遇", 8), compact)
        self.assertNotIn(("心情", 5), compact)
        self.assertNotIn(("羁绊", 6), compact)

    def test_resource_stats_rejects_other_user_mentions(self):
        self.assertTrue(resource_text_matches_identity(
            "xiaohao",
            "素心子",
            "@hajiimiii 获得修为 **+2548**，获得 **【三级妖丹】x1**。",
        ))
        self.assertFalse(resource_text_matches_identity(
            "main",
            "主魂",
            "@hajiimiii 获得修为 **+2548**，获得 **【三级妖丹】x1**。",
        ))

    def test_field_training_retry_preserves_confirmed_cooldown(self):
        actor = DummyCommon()
        actor.state = {
            "last_field_training_time": now_str(),
            "next_field_training_time": "",
        }

        actor.record_field_training_response("", context="test")

        self.assertGreater(common_seconds_until(actor.state["next_field_training_time"]), 110 * 60)

    def test_field_training_missing_response_retries_immediately_without_confirmed_cooldown(self):
        actor = DummyCommon()
        actor.state = {
            "last_field_training_time": "",
            "next_field_training_time": "",
        }

        actor.record_field_training_response("", context="test")

        self.assertLessEqual(common_seconds_until(actor.state["next_field_training_time"]), 1)

    def test_avatar_field_training_missing_response_retries_immediately(self):
        actor = DummyAvatarCommon()

        actor.record_identity_field_training_response("缘生子", "", context="test")

        next_time = actor.state["avatars"]["缘生子"]["next_field_training_time"]
        self.assertLessEqual(common_seconds_until(next_time), 1)

    def test_yuanying_active_and_settlement_do_not_short_retry(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "last_yuanying_out_time": "",
            "next_yuanying_out_time": "",
            "yuanying_out_end_time": "",
            "yuanying_out_active": False,
        }

        self.assertTrue(actor.record_yuanying_out_active_response(
            "你的元婴正在执行“元神出窍”任务，无法分身。请先使用 `.元婴归窍` 将其召回。",
            source="test",
        ))
        wait = (datetime.strptime(actor.state["next_yuanying_out_time"], "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds()
        self.assertGreater(wait, 50 * 60)
        self.assertLess(wait, 70 * 60)

        actor.state = {
            "last_yuanying_out_time": "",
            "next_yuanying_out_time": "",
            "yuanying_out_end_time": "",
            "yuanying_out_active": False,
        }
        self.assertTrue(actor.record_yuanying_out_settlement_response(
            "✨ **元神回响**：感应到 @Waaiging 的元婴已神游归来，正在清点收获...",
            source=".元婴出窍 response",
        ))
        wait = (datetime.strptime(actor.state["next_yuanying_out_time"], "%Y-%m-%d %H:%M:%S") - datetime.now()).total_seconds()
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertGreater(wait, 7 * 3600)

    def test_sub_main_yuanying_retreat_command_and_settlement_retry(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.state = {
            "last_yuanying_out_time": "",
            "next_yuanying_out_time": "",
            "yuanying_out_end_time": "",
            "yuanying_out_active": False,
        }
        actor.save_state = lambda: None

        self.assertEqual(actor.yuanying_command_for_identity("主魂"), ".元婴闭关")
        self.assertEqual(actor.yuanying_command_for_identity("厚土"), ".元婴出窍")
        self.assertEqual(log_utils.command_response_family(".元婴闭关"), ".元婴出窍")
        self.assertTrue(log_utils.feedback_response_matches_command(
            ".元婴闭关",
            "【元婴闭关结算】闭关结束，清点收获。",
        ))

        self.assertTrue(actor.record_yuanying_out_start_response(
            "你催动元婴闭关秘法，将在 **8小时** 后出关，下一次发言时将自动结算收获。",
        ))
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertGreater(common_seconds_until(actor.state["next_yuanying_out_time"]), 7 * 3600)

        actor.state["last_yuanying_out_time"] = "2020-01-01 00:00:00"
        self.assertFalse(actor.record_yuanying_out_start_response(
            "【元婴闭关结算】元婴闭关结束，获得修为 +1000。",
        ))
        self.assertFalse(actor.state["yuanying_out_active"])
        self.assertLessEqual(common_seconds_until(actor.state["next_yuanying_out_time"]), 10)

        actor.state["last_yuanying_out_time"] = "2020-01-01 00:00:00"
        self.assertTrue(actor.record_yuanying_out_settlement_response(
            "【元婴闭关结算】元婴闭关结束，获得修为 +2000。",
            source="passive 主魂",
        ))
        self.assertFalse(actor.state["yuanying_out_active"])
        self.assertLessEqual(common_seconds_until(actor.state["next_yuanying_out_time"]), 10)

    def test_sub_main_yuanying_retreat_passive_settlement_requires_main_reply(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatars = ["厚土", "缘生子", "寻真子"]
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": {"gamling33"}}
        actor.command_avatar_map = {}
        actor._current_identity = "主魂"
        actor.my_info = SimpleNamespace(username="Gamling33", first_name="")
        actor.notify_users = []
        actor.target_chat_id = -100123456
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "yuanying_out_active": True,
            "yuanying_out_end_time": future,
            "next_yuanying_out_time": future,
            "last_yuanying_out_time": "2020-01-01 00:00:00",
            "in_deep_meditation": True,
            "deep_meditation_end_time": future,
            "avatars": {},
        }
        actor.save_state = lambda: None

        text = "@Gamling33\n【元婴闭关结算】元婴闭关结束，获得修为 +2000。"
        actor.maybe_record_avatar_passive_states(DummyMessage(4101, text=text))

        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["next_yuanying_out_time"], future)
        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], future)

        actor.command_avatar_map = {4001: "主魂"}
        actor.maybe_record_avatar_passive_states(DummyMessage(4102, text=text, reply_to_msg_id=4001))

        self.assertFalse(actor.state["yuanying_out_active"])
        self.assertLessEqual(common_seconds_until(actor.state["next_yuanying_out_time"]), 10)
        self.assertTrue(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], future)

    def test_lingxiao_avatar_yuanying_and_rift_use_avatar_state(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子"]
        actor.avatar_nicknames = {"无咎子": "天星雷总"}
        actor.state = {
            "next_yuanying_out_time": "",
            "next_rift_search_time": "",
            "avatars": {"无咎子": {}},
        }
        actor.save_state = lambda: None
        actor.ensure_avatar_states()

        self.assertTrue(actor.record_yuanying_out_start_response(
            "你心念一动，元婴出窍，消失在天际，将在外云游 **8小时**。",
            identity="无咎子",
        ))
        self.assertEqual(actor.state["next_yuanying_out_time"], "")
        self.assertTrue(actor.state["avatars"]["无咎子"]["yuanying_out_active"])
        self.assertTrue(actor.state["avatars"]["无咎子"]["next_yuanying_out_time"])

        self.assertTrue(actor.record_identity_fixed_cd_command_response(
            "无咎子",
            "你运转全身法力，撕开一道漆黑的空间裂缝，将元婴送入其中探寻机缘。",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))
        self.assertEqual(actor.state["next_rift_search_time"], "")
        self.assertTrue(actor.state["avatars"]["无咎子"]["next_rift_search_time"])

    def test_sub_avatar_yuanying_and_rift_do_not_overwrite_main_state(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatars = ["厚土"]
        actor.avatar_nicknames = {"厚土": ""}
        main_yuanying = "2099-01-01 00:00:00"
        main_rift = "2099-01-02 00:00:00"
        actor.state = {
            "next_yuanying_out_time": main_yuanying,
            "next_rift_search_time": main_rift,
            "avatars": {"厚土": {}},
        }
        actor.save_state = lambda: None
        actor.ensure_avatar_states()

        self.assertTrue(actor.record_yuanying_out_start_response(
            "你心念一动，元婴出窍，消失在天际，将在外云游 **8小时**。",
            identity="厚土",
        ))
        self.assertEqual(actor.state["next_yuanying_out_time"], main_yuanying)
        self.assertTrue(actor.state["avatars"]["厚土"]["yuanying_out_active"])
        self.assertTrue(actor.state["avatars"]["厚土"]["next_yuanying_out_time"])

        self.assertFalse(actor.record_identity_fixed_cd_command_response(
            "厚土",
            "此处空间波动尚未平复，请在 **1小时2分钟3秒** 后再来探寻裂缝。",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))
        self.assertEqual(actor.state["next_rift_search_time"], main_rift)
        self.assertTrue(actor.state["avatars"]["厚土"]["next_rift_search_time"])

    def test_manual_star_reply_uses_avatar_identity(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatars = ["厚土"]
        actor.avatar_nicknames = {"厚土": ""}
        actor.state_file = "state_sub.json"
        actor.target_chat_id = -100123456
        actor.my_info = None
        actor.state = {"avatars": {"厚土": {"next_star_collect_time": "2099-01-01 00:00:00"}}}
        actor.save_state = lambda: None
        actor._manual_command_ids = [3001]
        actor._manual_command_texts = {3001: ".安抚星辰"}
        actor._manual_command_identities = {3001: "厚土"}
        actor.ensure_avatar_states()

        reply = DummyMessage(3002, reply_to_msg_id=3001)
        processed = asyncio.run(log_utils.record_manual_command_reply_state_if_needed(
            actor,
            reply,
            "你成功安抚了引星盘中的狂暴星力。",
        ))

        self.assertTrue(processed)
        self.assertTrue(actor.state["avatars"]["厚土"]["last_star_appease_time"])
        self.assertNotIn("last_calm_time", actor.state)

    def test_xiaohao_avatar_yuanying_and_rift_do_not_overwrite_main_state(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        main_yuanying = "2099-01-01 00:00:00"
        main_rift = "2099-01-02 00:00:00"
        actor.state = {
            "next_yuanying_out_time": main_yuanying,
            "next_rift_search_time": main_rift,
            "avatars": {"素心子": {}},
        }
        actor.save_state = lambda: None
        actor.ensure_avatar_states()

        self.assertTrue(actor.record_yuanying_out_start_response(
            "你心念一动，元婴出窍，消失在天际，将在外云游 **8小时**。",
            identity="素心子",
        ))
        self.assertEqual(actor.state["next_yuanying_out_time"], main_yuanying)
        self.assertTrue(actor.state["avatars"]["素心子"]["yuanying_out_active"])
        self.assertTrue(actor.state["avatars"]["素心子"]["next_yuanying_out_time"])

        self.assertFalse(actor.record_identity_fixed_cd_command_response(
            "素心子",
            "此处空间波动尚未平复，请在 **1小时2分钟3秒** 后再来探寻裂缝。",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))
        self.assertEqual(actor.state["next_rift_search_time"], main_rift)
        self.assertTrue(actor.state["avatars"]["素心子"]["next_rift_search_time"])

    def test_cloud_stairs_reward_progress_text_counts_as_success(self):
        text = """
你踏入云海深处，偶得一缕天外机缘，袖中顿时多了几样灵材。
本次获得 **157** 点修为、**80** 点宗门贡献。
当前云阶进度: **8 / 12**，罡风淬体: **12 / 12**。
额外收获: 【金精矿】x2
"""
        actor = Cultivator.__new__(Cultivator)
        actor.state = {"cloud_stairs_progress": "7 / 12 阶"}
        actor.save_state = lambda: None
        actor._main_confirmed = True

        self.assertTrue(actor.record_cloud_stairs_response(text))
        self.assertEqual(actor.state["cloud_stairs_progress"], "8 / 12 阶")
        self.assertTrue(actor.state["next_stairs_time"])

        xiaohao = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        xiaohao.avatars = ["问心子"]
        xiaohao.state = {"avatars": {"问心子": {"cloud_stairs_progress": "7 / 12 阶"}}}
        xiaohao.save_state = lambda: None

        self.assertTrue(xiaohao.record_avatar_cloud_stairs_response("问心子", text))
        self.assertEqual(xiaohao.state["avatars"]["问心子"]["cloud_stairs_progress"], "8 / 12 阶")
        self.assertTrue(xiaohao.state["avatars"]["问心子"]["next_stairs_time"])

    def test_xiaohao_no_such_beast_removes_stale_cache(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "stamina": 100},
                {"full_name": "青蛟", "species": "一阶蛟龙", "status": "休息中", "power": 420, "stamina": 90},
            ],
            "best_beast_name": "麻花藤",
            "best_beast_power": 31,
            "best_beast_status": "休息中",
            "best_beast_stamina": 100,
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.handle_no_such_beast_response(
            "麻花藤",
            "你没有名为“麻花藤”的灵兽。",
            "fixture",
        ))
        self.assertEqual([b["full_name"] for b in actor.state["beasts_cache"]], ["青蛟"])
        self.assertEqual(actor.state["best_beast_name"], "")
        self.assertTrue(actor.state["next_beast_status_check_time"])

    def test_xiaohao_no_such_beast_removes_non_best_cache_only(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "保龄球", "species": "一阶灵兽", "status": "放养中", "power": 120, "stamina": 100},
                {"full_name": "青蛟", "species": "一阶蛟龙", "status": "休息中", "power": 420, "stamina": 90},
            ],
            "best_beast_name": "青蛟",
            "best_beast_power": 420,
            "best_beast_status": "休息中",
            "best_beast_stamina": 90,
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.handle_no_such_beast_response(
            "保龄球",
            "你没有名为“保龄球”的灵兽。",
            "steal recall",
        ))
        self.assertEqual([b["full_name"] for b in actor.state["beasts_cache"]], ["青蛟"])
        self.assertEqual(actor.state["best_beast_name"], "青蛟")
        self.assertTrue(actor.state["next_beast_status_check_time"])

    def test_avatar_dream_active_voyage_defers_without_unknown_alert(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["问心子"]
        actor.state = {"avatars": {"问心子": {"next_dream_map_time": ""}}}
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": False

        async def fake_send(identity, command, **kwargs):
            return None, "侍妾仍在远航途中，暂无法与你同梦寻图。", False

        actor._send_concubine_identity_command = fake_send
        alerts = []
        old_notify = concubine_features.notify_unrecognized_response
        concubine_features.notify_unrecognized_response = lambda *args, **kwargs: alerts.append(args)
        try:
            self.assertFalse(asyncio.run(actor.execute_avatar_concubine_direct("问心子", "dream")))
        finally:
            concubine_features.notify_unrecognized_response = old_notify

        a_state = actor.state["avatars"]["问心子"]
        self.assertTrue(a_state["concubine_voyage_active"])
        self.assertTrue(a_state["next_dream_map_time"])
        self.assertEqual(alerts, [])

    def test_formation_invite_watchlist_includes_sub_avatars(self):
        for username in ("Crayonxxin", "Lvdoumiao", "Ding303"):
            invite = f"""
**【周天星斗大阵-启】**
【星宫】弟子 @{username} 正在布设大阵，尚需 **1** 位同门相助！
请其他星宫弟子在 **60秒** 内回复此消息使用 `.助阵`！
"""
            main = Cultivator.__new__(Cultivator)
            main.avatar_usernames = {}
            self.assertTrue(main.is_target_formation_invite(invite))

            xiaohao = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            xiaohao.avatar_usernames = {}
            self.assertTrue(xiaohao.is_target_formation_invite(invite))

    def test_dashboard_shows_wujiuzi_yuanying_and_rift(self):
        panels = build_command_panels("main", {"avatars": {"无咎子": {}}})
        wujiuzi = next(panel for panel in panels if panel.get("identity") == "无咎子")
        commands = {row.get("command") for row in wujiuzi.get("commands", [])}

        self.assertIn(".元婴出窍", commands)
        self.assertIn(".探寻裂缝", commands)

    def test_dashboard_shows_main_yuanshengzi_yuanying_and_rift(self):
        panels = build_command_panels("main", {"avatars": {"缘生子": {}}})
        yuanshengzi = next(panel for panel in panels if panel.get("identity") == "缘生子")
        commands = {row.get("command") for row in yuanshengzi.get("commands", [])}

        self.assertIn(".元婴出窍", commands)
        self.assertIn(".探寻裂缝", commands)

    def test_dashboard_avatar_field_training_commands_use_current_mode(self):
        panels = build_command_panels("main", {"avatars": {"无咎子": {}, "缘生子": {}, "素缘子": {}}})
        by_identity = {panel.get("identity"): panel for panel in panels}

        self.assertIn(".野外历练 深入", {row.get("command") for row in by_identity["无咎子"].get("commands", [])})
        self.assertIn(".改命 探索", {row.get("command") for row in by_identity["无咎子"].get("commands", [])})
        self.assertIn(".野外历练", {row.get("command") for row in by_identity["缘生子"].get("commands", [])})
        self.assertIn(".野外历练", {row.get("command") for row in by_identity["素缘子"].get("commands", [])})
        self.assertNotIn(".野外历练 谨慎", {row.get("command") for row in by_identity["缘生子"].get("commands", [])})

        sub_panels = build_command_panels("sub", {"avatars": {"厚土": {}}})
        sub_avatar = next(panel for panel in sub_panels if panel.get("identity") == "厚土")
        self.assertIn(".野外历练", {row.get("command") for row in sub_avatar.get("commands", [])})

        xiaohao_panels = build_command_panels("xiaohao", {"avatars": {"问心子": {}}})
        xiaohao_avatar = next(panel for panel in xiaohao_panels if panel.get("identity") == "问心子")
        self.assertIn(".野外历练", {row.get("command") for row in xiaohao_avatar.get("commands", [])})

    def test_main_avatar_field_training_sends_bare_command_except_wujiuzi(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.avatar_features = {"缘生子": {"training_cmd": ".野外历练", "training_level": ""}}
        actor.state = {"avatars": {"缘生子": {"next_field_training_time": "", "last_field_training_time": ""}}}
        actor.save_state = lambda: None
        actor.avatar_meditation_needs_attention = lambda avatar: False
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            return "**【野外历练 · 灵机暗藏】**\n本次获得 **157** 点修为。"

        actor.send_and_wait_feedback_identity = fake_send

        asyncio.run(actor._avatar_field_training_check("缘生子"))

        self.assertEqual(sent, [("缘生子", ".野外历练")])
        self.assertTrue(actor.state["avatars"]["缘生子"]["next_field_training_time"])

    def test_main_wujiuzi_field_training_sends_destiny_change_before_deep_training(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子"]
        actor.avatar_nicknames = {"无咎子": "天星雷总"}
        actor.avatar_features = {
            "无咎子": {
                "meditation_prefix": ".推命",
                "training_prefix_commands": [".推命 探索", ".改命 探索"],
                "training_cmd": ".野外历练",
                "training_level": "深入",
            }
        }
        actor.state = {"avatars": {"无咎子": {"next_field_training_time": "", "last_field_training_time": ""}}}
        actor.save_state = lambda: None
        actor.avatar_meditation_needs_attention = lambda avatar: False
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            return "**【野外历练 · 灵机暗藏】**\n本次获得 **157** 点修为。"

        async def fake_sleep(seconds):
            return None

        actor.send_and_wait_feedback_identity = fake_send

        with patch.object(intelligent_cultivator.asyncio, "sleep", fake_sleep):
            asyncio.run(actor._avatar_field_training_check("无咎子"))

        self.assertEqual(sent, [
            ("无咎子", ".推命 探索"),
            ("无咎子", ".改命 探索"),
            ("无咎子", ".野外历练 深入"),
        ])
        self.assertTrue(actor.state["avatars"]["无咎子"]["next_field_training_time"])

    def test_rift_beast_defeat_counts_as_consumed_attempt(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {
            "next_rift_search_time": "",
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.ensure_avatar_states()

        self.assertTrue(actor.record_identity_fixed_cd_command_response(
            "缘生子",
            "**【不敌败退】**\n时空异兽神通诡异，你最终不敌败退，元婴险些崩溃！\n你身受重创，修为倒退了 **67** 点！",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))
        self.assertEqual(actor.state["next_rift_search_time"], "")
        self.assertGreater(common_seconds_until(actor.state["avatars"]["缘生子"]["next_rift_search_time"]), 11 * 3600)

    def test_rift_beast_defeat_matches_feedback_family(self):
        text = "**【不敌败退】**\n时空异兽神通诡异，你最终不敌败退，元婴险些崩溃！\n你身受重创，修为倒退了 **67** 点！"

        self.assertEqual(log_utils.text_response_family(text), "rift")
        self.assertTrue(log_utils.feedback_response_matches_command(".探寻裂缝", text))

    def test_dashboard_shows_main_soul_luoyun_spirit_tree_commands(self):
        panels = build_command_panels("main", {"avatars": {}})
        main_panel = next(panel for panel in panels if panel.get("identity") == "主魂")
        commands = {row.get("command") for row in main_panel.get("commands", [])}

        self.assertIn(".灵树状态", commands)
        self.assertIn(".灵树灌溉", commands)
        self.assertIn(".协同守山", commands)
        self.assertFalse({
            ".借天门势",
            ".天阶状态",
            ".登天阶",
            ".引九天罡风",
            ".问心台",
        } & commands)

    def test_nurture_spirit_loop_respects_dashboard_pause(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {"next_nurture_spirit_time": ""}
        actor.is_running = True
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        sent = []

        async def fake_wait_for_main():
            return None

        async def fake_wait_for_control_change(timeout):
            actor.is_running = False
            return True

        async def fake_send(*args, **kwargs):
            sent.append(args)
            return ""

        actor._wait_for_main_identity = fake_wait_for_main
        actor.dashboard_command_paused = (
            lambda command, identity: command == intelligent_cultivator.NURTURE_SPIRIT_COMMAND
        )
        actor.wait_for_dashboard_command_control_change = fake_wait_for_control_change
        actor.send_and_wait_feedback = fake_send

        asyncio.run(actor.run_nurture_spirit_loop())
        self.assertEqual(sent, [])

    def test_dashboard_uses_identity_spirit_tree_irrigation_times(self):
        state = {
            "spirit_tree_status": "灌溉期",
            "next_spirit_tree_irrigation_time": "2026-06-12 19:24:56",
            "spirit_tree_irrigation_times": {
                "主魂": "2026-06-12 19:24:56",
                "缘生子": "2026-06-12 19:25:03",
            },
            "avatars": {"缘生子": {}},
        }
        panels = build_command_panels("main", state)
        main_panel = next(panel for panel in panels if panel.get("identity") == "主魂")
        yuanshengzi = next(panel for panel in panels if panel.get("identity") == "缘生子")
        main_row = next(row for row in main_panel.get("commands", []) if row.get("command") == ".灵树灌溉")
        avatar_row = next(row for row in yuanshengzi.get("commands", []) if row.get("command") == ".灵树灌溉")

        self.assertEqual(main_row.get("at"), "2026-06-12 19:24:56")
        self.assertEqual(avatar_row.get("at"), "2026-06-12 19:25:03")

    def test_manual_reply_falls_back_to_command_ledger_after_memory_loss(self):
        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log_utils.MESSAGE_EVENTS_DB_FILE = os.path.join(tmp, "message_events.sqlite3")
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
                actor = DummyManualActor()
                command_msg = DummyMessage(1001, text=".我的灵根", out=True)
                reply_msg = DummyMessage(1002, text="天命玉牒", reply_to_msg_id=1001)

                self.assertTrue(
                    log_utils.record_command_sent(
                        actor,
                        command_msg,
                        ".我的灵根",
                        identity="无咎子",
                        source="manual",
                    )
                )

                self.assertTrue(log_utils.is_reply_to_manual_command(actor, reply_msg))
                self.assertEqual(log_utils.manual_command_text_for_reply(actor, reply_msg), ".我的灵根")
                self.assertEqual(log_utils.manual_command_identity_for_reply(actor, reply_msg), "无咎子")
        finally:
            log_utils.MESSAGE_EVENTS_DB_FILE = old_db
            log_utils._MESSAGE_EVENTS_SCHEMA_READY = False


if __name__ == "__main__":
    unittest.main()
