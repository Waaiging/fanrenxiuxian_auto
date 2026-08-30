import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import dashboard_server
import soul_curse_features
from automation_settings import SUB_YINLUO_IDENTITY
from soul_curse_features import (
    SOUL_CURSE_IDENTIFY_COMMAND,
    SOUL_CURSE_INFER_COMMAND,
    SOUL_CURSE_PROTECT_COMMAND,
    SOUL_CURSE_PUBLISH_COMMAND,
    SOUL_CURSE_PUBLISHERS,
    SOUL_CURSE_SHARED_ASSISTANTS,
    SOUL_CURSE_STRIP_COMMAND,
    SOUL_CURSE_SUPPRESS_COMMAND,
    SoulCurseMixin,
    parse_soul_curse_action,
    parse_soul_curse_protect,
    parse_soul_curse_publish,
)
from yinluo_features import YINLUO_CONVERT_COMMAND, parse_yinluo_convert


class _NoopAtomic:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummySoulCurseActor(SoulCurseMixin):
    def __init__(self, account_key, main_responses=None, identity_responses=None):
        self.account_key = account_key
        assistant_identity = SUB_YINLUO_IDENTITY if account_key == "sub" else "缘生子"
        self.state = {"avatars": {assistant_identity: {}}}
        self.avatars = [assistant_identity]
        self.main_responses = {key: list(value) for key, value in (main_responses or {}).items()}
        self.identity_responses = {key: list(value) for key, value in (identity_responses or {}).items()}
        self.main_sent = []
        self.identity_sent = []
        self.saved = 0
        self.yinluo_states = {}

    def save_state(self):
        self.saved += 1

    def get_avatar_state(self, identity):
        return self.state.setdefault("avatars", {}).setdefault(identity, {})

    def identity_pause_seconds(self, identity):
        return 0

    def soul_curse_identity_enabled(self, account=None, identity="主魂"):
        # 流程测试：身份开关默认视为开启（开关行为由专门测试覆盖）
        return True

    def dashboard_command_paused(self, command, identity=""):
        return False

    def common_atomic_task(self, label):
        return _NoopAtomic()

    async def send_and_wait_feedback(self, command, **kwargs):
        self.main_sent.append(command)
        values = self.main_responses.get(command, [])
        return values.pop(0) if values else ""

    async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
        self.identity_sent.append((identity, command))
        values = self.identity_responses.get((identity, command), [])
        return values.pop(0) if values else ""

    def get_yinluo_state(self, identity):
        return self.yinluo_states.setdefault(identity, {"last_status": "", "next_action_at": ""})

    async def yinluo_convert_sha(self, identity):
        self.identity_sent.append((identity, YINLUO_CONVERT_COMMAND))
        values = self.identity_responses.get((identity, YINLUO_CONVERT_COMMAND), [])
        text = values.pop(0) if values else ""
        parsed = parse_yinluo_convert(text)
        state = self.get_yinluo_state(identity)
        if parsed.get("status") == "success":
            state["last_status"] = "converted"
            state["next_action_at"] = ""
            return True
        if parsed.get("status") == "pending":
            state["last_status"] = "convert_pending"
            state["next_action_at"] = soul_curse_features.add_seconds_str(soul_curse_features.now_str(), 60)
            return True
        wait = max(60, int(parsed.get("cooldown_seconds") or 3600))
        state["last_status"] = "convert_failed"
        state["next_action_at"] = soul_curse_features.add_seconds_str(soul_curse_features.now_str(), wait)
        return False


class SoulCurseParserTests(unittest.TestCase):
    def test_publish_parser_reads_new_and_existing_commission_ids(self):
        new = parse_soul_curse_publish("**【解咒委托已发布】**\n委托 ID：**19**\n报酬：**1** 灵石")
        existing = parse_soul_curse_publish("你已有进行中的解咒委托（ID: 8），不可重复发布。")
        self.assertEqual(new["status"], "success")
        self.assertEqual(new["commission_id"], "19")
        self.assertEqual(existing["status"], "existing")
        self.assertEqual(existing["commission_id"], "8")

    def test_cooldown_parsers_read_observed_replies(self):
        protect = parse_soul_curse_protect("神魂护持不可过密，请在 **5小时59分钟49秒** 后再试。")
        identify = parse_soul_curse_action("冷却 **4** 小时，请在 **3小时56分钟1秒** 后再试。", "identify")
        self.assertEqual(protect["status"], "cooldown")
        self.assertEqual(protect["cooldown_seconds"], 5 * 3600 + 59 * 60 + 49)
        self.assertEqual(identify["status"], "cooldown")
        self.assertEqual(identify["cooldown_seconds"], 3 * 3600 + 56 * 60 + 1)

    def test_action_parser_recognizes_failures(self):
        no_contract = parse_soul_curse_action("你与对方没有有效的咒契协定。需先由对方发布委托，再由你接取。", "strip")
        not_ready = parse_soul_curse_action("咒源尚未辨明，需先通过 `.推演封魂咒` 或 `.辨认咒纹` 将咒源推进到 50 以上。", "strip")
        sha_not_enough = parse_soul_curse_action("煞气不足，无法施展借幡镇魂。", "suppress")
        self.assertEqual(no_contract["status"], "no_contract")
        self.assertEqual(not_ready["status"], "source_not_ready")
        self.assertEqual(sha_not_enough["status"], "sha_not_enough")

    def test_dashboard_publisher_rows_use_completed_chain_cooldown(self):
        chain_ready = soul_curse_features.add_seconds_str(soul_curse_features.now_str(), 8 * 3600)
        old_single_ready = soul_curse_features.add_seconds_str(soul_curse_features.now_str(), -60)
        rows = {
            row["command"]: row
            for row in dashboard_server.soul_curse_publisher_commands({
                "soul_curse": {
                    "chain_stage": "",
                    "next_chain_time": chain_ready,
                    "next_protect_time": old_single_ready,
                    "next_action_at": chain_ready,
                    "commission_id": "22",
                    "commission_status": "completed",
                }
            })
        }

        for command in (SOUL_CURSE_INFER_COMMAND, SOUL_CURSE_PROTECT_COMMAND, SOUL_CURSE_PUBLISH_COMMAND):
            self.assertEqual(rows[command]["tone"], "cooldown")
            self.assertEqual(rows[command]["status"], "整链冷却")
            self.assertGreater(rows[command]["next_seconds"], 7 * 3600)


class SoulCurseFlowTests(unittest.TestCase):
    def test_main_chain_publishes_and_local_yinluo_completes_commission(self):
        actor = DummySoulCurseActor(
            "main",
            main_responses={
                SOUL_CURSE_INFER_COMMAND: ["**【推演封魂咒】** 咒源 +18。"],
                SOUL_CURSE_PROTECT_COMMAND: ["**【护持神魂】** 魂封 -10，月魄 +1。"],
                SOUL_CURSE_PUBLISH_COMMAND: ["**【解咒委托已发布】**\n委托 ID：**19**\n报酬：**1** 灵石"],
            },
            identity_responses={
                ("缘生子", ".接取解咒委托 19"): ["**【咒契协定已成】** 阴罗宗弟子 @kulipabp 已接取 @Weeguu 的解咒委托。"],
                ("缘生子", f"{SOUL_CURSE_IDENTIFY_COMMAND} @Weeguu"): ["**【阴罗辨咒】** 咒源 +27。"],
                ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @Weeguu"): ["**【借幡镇魂】** 魂封 -11，月魄 +1。"],
                ("缘生子", f"{SOUL_CURSE_STRIP_COMMAND} @Weeguu"): ["**【剥离咒源成功】** 获得【阴罗残咒】x1。"],
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_file = os.path.join(tmpdir, "soul_curse_commissions.json")
            with patch.object(soul_curse_features, "SOUL_CURSE_SHARED_FILE", shared_file):
                asyncio.run(actor.soul_curse_run_publisher_chain(SOUL_CURSE_PUBLISHERS["main"]))
                # 共享池设计：发布后同进程的阴罗身份竞争认领并完成接取链
                for profile in actor.soul_curse_shared_assistant_profiles():
                    asyncio.run(actor.soul_curse_shared_assist_tick(profile))

        self.assertEqual(actor.main_sent, [
            SOUL_CURSE_INFER_COMMAND,
            SOUL_CURSE_PROTECT_COMMAND,
            SOUL_CURSE_PUBLISH_COMMAND,
        ])
        self.assertEqual(actor.identity_sent, [
            ("缘生子", ".接取解咒委托 19"),
            ("缘生子", f"{SOUL_CURSE_IDENTIFY_COMMAND} @Weeguu"),
            ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @Weeguu"),
            ("缘生子", f"{SOUL_CURSE_STRIP_COMMAND} @Weeguu"),
        ])
        self.assertEqual(actor.state["soul_curse"]["commission_id"], "19")
        self.assertEqual(actor.state["soul_curse"]["commission_status"], "completed")
        self.assertEqual(actor.state["avatars"]["缘生子"]["soul_curse_assist"]["strip_commission_id"], "19")
        assist = actor.state["avatars"]["缘生子"]["soul_curse_assist"]
        self.assertEqual(assist["next_identify_time"], assist["next_strip_time"])
        self.assertEqual(assist["next_suppress_time"], assist["next_strip_time"])

    def test_sub_chain_publishes_and_local_yinluo_completes_commission(self):
        actor = DummySoulCurseActor(
            "sub",
            main_responses={
                SOUL_CURSE_INFER_COMMAND: ["**【推演封魂咒】** 咒源 +18。"],
                SOUL_CURSE_PROTECT_COMMAND: ["**【护持神魂】** 魂封 -10，月魄 +1。"],
                SOUL_CURSE_PUBLISH_COMMAND: ["**【解咒委托已发布】**\n委托 ID：**21**\n报酬：**1** 灵石"],
            },
            identity_responses={
                (SUB_YINLUO_IDENTITY, ".接取解咒委托 21"): ["**【咒契协定已成】** 阴罗宗弟子已接取 @Gamling33 的解咒委托。"],
                (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_IDENTIFY_COMMAND} @Gamling33"): ["**【阴罗辨咒】** 咒源 +27。"],
                (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_SUPPRESS_COMMAND} @Gamling33"): ["**【借幡镇魂】** 魂封 -11，月魄 +1。"],
                (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_STRIP_COMMAND} @Gamling33"): ["**【剥离咒源成功】** 获得【阴罗残咒】x1。"],
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_file = os.path.join(tmpdir, "soul_curse_commissions.json")
            with patch.object(soul_curse_features, "SOUL_CURSE_SHARED_FILE", shared_file):
                asyncio.run(actor.soul_curse_run_publisher_chain(SOUL_CURSE_PUBLISHERS["sub"]))
                # 共享池设计：发布后同进程的阴罗身份竞争认领并完成接取链
                for profile in actor.soul_curse_shared_assistant_profiles():
                    asyncio.run(actor.soul_curse_shared_assist_tick(profile))

        self.assertEqual(actor.main_sent, [
            SOUL_CURSE_INFER_COMMAND,
            SOUL_CURSE_PROTECT_COMMAND,
            SOUL_CURSE_PUBLISH_COMMAND,
        ])
        self.assertEqual(actor.identity_sent, [
            (SUB_YINLUO_IDENTITY, ".接取解咒委托 21"),
            (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_IDENTIFY_COMMAND} @Gamling33"),
            (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_SUPPRESS_COMMAND} @Gamling33"),
            (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_STRIP_COMMAND} @Gamling33"),
        ])
        self.assertEqual(actor.state["soul_curse"]["commission_status"], "completed")
        local_assist = actor.get_soul_curse_assist_state(SUB_YINLUO_IDENTITY, "sub")
        self.assertEqual(local_assist["strip_commission_id"], "21")
        self.assertEqual(local_assist["target_username"], "@Gamling33")

    def test_yinluo_action_replenishes_sha_and_retries_same_step(self):
        actor = DummySoulCurseActor(
            "main",
            identity_responses={
                ("缘生子", ".接取解咒委托 31"): ["**【咒契协定已成】** 阴罗宗弟子已接取 @Weeguu 的解咒委托。"],
                ("缘生子", f"{SOUL_CURSE_IDENTIFY_COMMAND} @Weeguu"): ["**【阴罗辨咒】** 咒源 +27。"],
                ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @Weeguu"): [
                    "煞气不足，无法施展借幡镇魂。",
                    "**【借幡镇魂】** 魂封 -11，月魄 +1。",
                ],
                ("缘生子", YINLUO_CONVERT_COMMAND): ["煞气池增加了 **10000** 点。"],
                ("缘生子", f"{SOUL_CURSE_STRIP_COMMAND} @Weeguu"): ["**【剥离咒源成功】** 获得【阴罗残咒】x1。"],
            },
        )
        actor.state["soul_curse"] = {
            "commission_id": "31",
            "commission_status": "success",
            "commission_target": "@Weeguu",
        }

        asyncio.run(actor.soul_curse_process_assist_commission({
            "owner_account": "main",
            "commission_id": "31",
            "target_username": "@Weeguu",
            "assistant_identity": "缘生子",
        }, source="local"))

        self.assertEqual(actor.identity_sent, [
            ("缘生子", ".接取解咒委托 31"),
            ("缘生子", f"{SOUL_CURSE_IDENTIFY_COMMAND} @Weeguu"),
            ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @Weeguu"),
            ("缘生子", YINLUO_CONVERT_COMMAND),
            ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @Weeguu"),
            ("缘生子", f"{SOUL_CURSE_STRIP_COMMAND} @Weeguu"),
        ])
        assist = actor.state["avatars"]["缘生子"]["soul_curse_assist"]
        self.assertEqual(assist["suppress_commission_id"], "31")
        self.assertEqual(assist["strip_commission_id"], "31")
        self.assertEqual(actor.state["soul_curse"]["commission_status"], "completed")

    def test_xiaohao_publish_writes_shared_commission_for_sub(self):
        actor = DummySoulCurseActor(
            "xiaohao",
            main_responses={
                SOUL_CURSE_INFER_COMMAND: ["**【推演封魂咒】** 咒源 +18。"],
                SOUL_CURSE_PROTECT_COMMAND: ["**【护持神魂】** 魂封 -10，月魄 +1。"],
                SOUL_CURSE_PUBLISH_COMMAND: ["**【解咒委托已发布】**\n委托 ID：**20**\n报酬：**1** 灵石"],
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_file = os.path.join(tmpdir, "soul_curse_commissions.json")
            with patch.object(soul_curse_features, "SOUL_CURSE_SHARED_FILE", shared_file):
                asyncio.run(actor.soul_curse_run_publisher_chain(SOUL_CURSE_PUBLISHERS["xiaohao"]))
                with open(shared_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

        self.assertEqual(data["xiaohao"]["commission_id"], "20")
        self.assertEqual(data["xiaohao"]["target_username"], "@TitanCreeper")
        # 新设计：发布时认领留空，由就绪的阴罗身份按冷却竞争认领
        self.assertEqual(data["xiaohao"]["assistant_account"], "")
        self.assertEqual(data["xiaohao"]["status"], "pending_accept")
        self.assertEqual(actor.identity_sent, [])

    def test_sub_yinluo_consumes_shared_xiaohao_commission(self):
        actor = DummySoulCurseActor(
            "sub",
            identity_responses={
                (SUB_YINLUO_IDENTITY, ".接取解咒委托 20"): ["**【咒契协定已成】** 阴罗宗弟子 @Lvdoumiao 已接取 @TitanCreeper 的解咒委托。"],
                (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_IDENTIFY_COMMAND} @TitanCreeper"): ["**【阴罗辨咒】** 咒源 +27。"],
                (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_SUPPRESS_COMMAND} @TitanCreeper"): ["**【借幡镇魂】** 魂封 -11，月魄 +1。"],
                (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_STRIP_COMMAND} @TitanCreeper"): ["**【剥离咒源成功】** 获得【阴罗残咒】x1。"],
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_file = os.path.join(tmpdir, "soul_curse_commissions.json")
            with open(shared_file, "w", encoding="utf-8") as f:
                json.dump({
                    "xiaohao": {
                        "owner_account": "xiaohao",
                        "commission_id": "20",
                        "target_username": "@TitanCreeper",
                        "assistant_account": "sub",
                        "assistant_identity": SUB_YINLUO_IDENTITY,
                        "status": "pending_accept",
                    }
                }, f)
            with patch.object(soul_curse_features, "SOUL_CURSE_SHARED_FILE", shared_file):
                for profile in actor.soul_curse_shared_assistant_profiles():
                    asyncio.run(actor.soul_curse_shared_assist_tick(profile))
                with open(shared_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

        self.assertEqual(actor.identity_sent, [
            (SUB_YINLUO_IDENTITY, ".接取解咒委托 20"),
            (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_IDENTIFY_COMMAND} @TitanCreeper"),
            (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_SUPPRESS_COMMAND} @TitanCreeper"),
            (SUB_YINLUO_IDENTITY, f"{SOUL_CURSE_STRIP_COMMAND} @TitanCreeper"),
        ])
        self.assertEqual(data["xiaohao"]["status"], "completed")
        shared_assist = actor.get_soul_curse_assist_state(SUB_YINLUO_IDENTITY, "xiaohao")
        self.assertEqual(shared_assist["strip_commission_id"], "20")
        self.assertEqual(shared_assist["target_username"], "@TitanCreeper")

    def test_sub_migrates_legacy_xiaohao_assist_without_overwriting_local_chain(self):
        actor = DummySoulCurseActor("sub")
        actor.state["avatars"][SUB_YINLUO_IDENTITY]["soul_curse_assist"] = {
            "owner_account": "xiaohao",
            "target_username": "@TitanCreeper",
            "commission_id": "424",
            "strip_commission_id": "424",
            "status": "completed",
        }

        local = actor.get_soul_curse_assist_state(SUB_YINLUO_IDENTITY, "sub")
        shared = actor.get_soul_curse_assist_state(SUB_YINLUO_IDENTITY, "xiaohao")

        self.assertEqual(local["owner_account"], "sub")
        self.assertEqual(local["commission_id"], "")
        self.assertEqual(shared["owner_account"], "xiaohao")
        self.assertEqual(shared["commission_id"], "424")
        self.assertEqual(shared["strip_commission_id"], "424")

        local["commission_id"] = "425"
        self.assertEqual(
            actor.soul_curse_assistant_profile_for_command(SUB_YINLUO_IDENTITY, ".接取解咒委托 425")["owner_account"],
            "sub",
        )
        self.assertEqual(
            actor.soul_curse_assistant_profile_for_command(SUB_YINLUO_IDENTITY, ".剥离咒源 @TitanCreeper")["owner_account"],
            "xiaohao",
        )


if __name__ == "__main__":
    unittest.main()
