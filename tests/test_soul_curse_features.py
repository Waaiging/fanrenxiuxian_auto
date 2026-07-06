import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import soul_curse_features
from soul_curse_features import (
    SOUL_CURSE_ASSISTANTS,
    SOUL_CURSE_IDENTIFY_COMMAND,
    SOUL_CURSE_INFER_COMMAND,
    SOUL_CURSE_PROTECT_COMMAND,
    SOUL_CURSE_PUBLISH_COMMAND,
    SOUL_CURSE_PUBLISHERS,
    SOUL_CURSE_STRIP_COMMAND,
    SOUL_CURSE_SUPPRESS_COMMAND,
    SoulCurseMixin,
    parse_soul_curse_action,
    parse_soul_curse_protect,
    parse_soul_curse_publish,
)


class _NoopAtomic:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummySoulCurseActor(SoulCurseMixin):
    def __init__(self, account_key, main_responses=None, identity_responses=None):
        self.account_key = account_key
        self.state = {"avatars": {"缘生子": {}}}
        self.avatars = ["缘生子"]
        self.main_responses = {key: list(value) for key, value in (main_responses or {}).items()}
        self.identity_responses = {key: list(value) for key, value in (identity_responses or {}).items()}
        self.main_sent = []
        self.identity_sent = []
        self.saved = 0

    def save_state(self):
        self.saved += 1

    def get_avatar_state(self, identity):
        return self.state.setdefault("avatars", {}).setdefault(identity, {})

    def identity_pause_seconds(self, identity):
        return 0

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
        self.assertEqual(no_contract["status"], "no_contract")
        self.assertEqual(not_ready["status"], "source_not_ready")


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

        asyncio.run(actor.soul_curse_run_publisher_chain(SOUL_CURSE_PUBLISHERS["main"]))

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
        self.assertEqual(actor.state["avatars"]["缘生子"]["soul_curse_assist"]["strip_commission_id"], "19")

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
        self.assertEqual(data["xiaohao"]["assistant_account"], "sub")
        self.assertEqual(data["xiaohao"]["status"], "pending_accept")
        self.assertEqual(actor.identity_sent, [])

    def test_sub_yinluo_consumes_shared_xiaohao_commission(self):
        actor = DummySoulCurseActor(
            "sub",
            identity_responses={
                ("缘生子", ".接取解咒委托 20"): ["**【咒契协定已成】** 阴罗宗弟子 @Lvdoumiao 已接取 @TitanCreeper 的解咒委托。"],
                ("缘生子", f"{SOUL_CURSE_IDENTIFY_COMMAND} @TitanCreeper"): ["**【阴罗辨咒】** 咒源 +27。"],
                ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @TitanCreeper"): ["**【借幡镇魂】** 魂封 -11，月魄 +1。"],
                ("缘生子", f"{SOUL_CURSE_STRIP_COMMAND} @TitanCreeper"): ["**【剥离咒源成功】** 获得【阴罗残咒】x1。"],
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
                        "assistant_identity": "缘生子",
                        "status": "pending_accept",
                    }
                }, f)
            with patch.object(soul_curse_features, "SOUL_CURSE_SHARED_FILE", shared_file):
                asyncio.run(actor.soul_curse_shared_assist_tick(SOUL_CURSE_ASSISTANTS["sub"]))
                with open(shared_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

        self.assertEqual(actor.identity_sent, [
            ("缘生子", ".接取解咒委托 20"),
            ("缘生子", f"{SOUL_CURSE_IDENTIFY_COMMAND} @TitanCreeper"),
            ("缘生子", f"{SOUL_CURSE_SUPPRESS_COMMAND} @TitanCreeper"),
            ("缘生子", f"{SOUL_CURSE_STRIP_COMMAND} @TitanCreeper"),
        ])
        self.assertEqual(data["xiaohao"]["status"], "completed")
        self.assertEqual(actor.state["avatars"]["缘生子"]["soul_curse_assist"]["strip_commission_id"], "20")


if __name__ == "__main__":
    unittest.main()
