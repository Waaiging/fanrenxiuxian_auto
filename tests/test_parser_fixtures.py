import asyncio
import json
import unittest
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import concubine_features
import common_command_features
import auto_reply_features
import command_feedback
import cultivator_xiaohao
import dashboard_server
import fishing_features
import intelligent_cultivator
import log_utils
import star_gazing_collector
import sub_cultivator
from command_modules import (
    ask_dao_plan,
    field_training_plan_from_features,
    nurture_spirit_plan,
    rift_search_plan,
    treasure_touch_plan,
    yuanying_out_plan,
)
from fishing_features import (
    FishingMixin,
    fishing_catch_summary,
    parse_fishing_auto_control_text,
    parse_fishing_control_text,
    parse_buy_bait,
    parse_exchange_response,
    parse_fishing_basket,
    parse_fishing_loot_lines,
    parse_fishing_start,
    parse_missing_resources,
    parse_nest_response,
    parse_rod_gift_response,
    parse_rod_response,
    parse_trade_listing_response,
    parse_trade_purchase_response,
)
from yinluo_features import (
    YINLUO_CONVERT_COMMAND,
    YINLUO_MASTER_COMMAND,
    YINLUO_SOUL,
    YinluoMixin,
    parse_yinluo_appease,
    parse_yinluo_blood_wash,
    parse_yinluo_convert,
    parse_yinluo_imprison,
    parse_yinluo_status,
    parse_yinluo_summon_shadow,
)
from common_command_features import CommonCommandMixin, add_seconds_str, dt_to_str, now_str, seconds_until as common_seconds_until
from concubine_features import ConcubineMixin, concubine_default_state, parse_duration_seconds, seconds_until
from cultivator_xiaohao import CultivatorXiaoHao
from dashboard_server import build_command_panels, outgoing_log_command_full, parse_inventory_items_from_text, parse_resource_changes_from_text, resource_text_matches_identity
from intelligent_cultivator import Cultivator
from log_utils import parse_cultivation_delta_text, parse_cultivation_profile_text
from soul_curse_features import (
    SOUL_CURSE_CO_STUDY_COMMAND,
    SOUL_CURSE_WANYING_GREETING_COMMAND,
    SoulCurseMixin,
    parse_soul_curse_co_study,
    parse_soul_curse_wanying_greeting,
)
from sub_cultivator import SubCultivator


class DummyConcubine(ConcubineMixin):
    account_key = "main"
    avatars = []

    def __init__(self):
        self.state = concubine_default_state()
        self.target_concubine_search_enabled_flag = True

    def save_state(self):
        return None

    def parse_wait_time(self, text, *args, **kwargs):
        return parse_duration_seconds(text)


class DummyAtomicConcubine(ConcubineMixin):
    def __init__(self):
        self.active_atomic_task = None

    def time_critical_identity_command(self, command):
        command = str(command or "").strip()
        return command == ".观星" or command.startswith(".观星 ") or command == ".改换星移" or command.startswith(".改换星移 ")


class DummyCommon(CommonCommandMixin):
    def __init__(self):
        self.state = {}

    def save_state(self):
        return None

    def parse_wait_time(self, text, *args, **kwargs):
        return parse_duration_seconds(text)


class IncomingTelemetryFailureActor:
    current_identity = "主魂"

    def get_identity_from_msg(self, msg):
        raise IndexError("fixture telemetry failure")


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


class DummySoulCurse(SoulCurseMixin):
    def __init__(self, account_key="main"):
        self.account_key = account_key
        self.state = {}
        self.sent = []
        self.is_running = True
        self.pause_event = asyncio.Event()
        self.pause_event.set()

    def save_state(self):
        return None

    def identity_pause_seconds(self, identity="主魂"):
        return 0

    def dashboard_command_paused(self, command, identity="主魂"):
        return False

    async def send_and_wait_feedback(self, command, **kwargs):
        self.sent.append(command)
        if command == SOUL_CURSE_WANYING_GREETING_COMMAND:
            return "婉影问安已成，南宫婉轻声回应。"
        if command == SOUL_CURSE_CO_STUDY_COMMAND:
            return "你与南宫婉同参封魂，封魂咒气息渐稳。"
        return ""


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


class FakeClearClient:
    def __init__(self, messages):
        self.messages = messages
        self.deleted = []

    async def get_me(self):
        return SimpleNamespace(id=1)

    def iter_messages(self, *args, **kwargs):
        async def gen():
            for msg in self.messages:
                yield msg
        return gen()

    async def delete_messages(self, chat_id, ids, revoke=True):
        self.deleted.extend(list(ids))


class ParserFixtureTests(unittest.TestCase):
    def test_incoming_telemetry_failure_does_not_interrupt_workflow(self):
        actor = IncomingTelemetryFailureActor()
        msg = SimpleNamespace(id=14731, sender_id=12345, text="坠魔心劫·第2轮")

        result = asyncio.run(log_utils.log_incoming_message(
            actor,
            ".稳 1/3 try 1/3 (无咎子)",
            msg.text,
            msg=msg,
        ))

        self.assertFalse(result)

    def test_managed_mentions_cover_all_twelve_identities(self):
        account_fixtures = (
            (
                "Weeguu",
                {"wuxinglinggen": "无咎子", "kulipabp": "缘生子", "OldEinstein": "素缘子"},
            ),
            (
                "Gamling33",
                {"crayonxxin": "厚土", "lvdoumiao": "缘生子", "ding303": "寻真子"},
            ),
            (
                "TitanCreeper",
                {"lianqi10000": "问心子", "hajiimiii": "素心子", "adai925": "缘生子"},
            ),
        )
        checked = 0
        for main_username, avatar_usernames in account_fixtures:
            actor = SimpleNamespace(
                my_info=SimpleNamespace(id=100 + checked, username=main_username, first_name="", last_name=""),
                identity_usernames={"主魂": [main_username]},
                avatar_usernames=avatar_usernames,
                avatars=list(avatar_usernames.values()),
            )
            identities_by_username = {main_username: "主魂", **avatar_usernames}
            for username, identity in identities_by_username.items():
                msg = DummyMessage(1000 + checked, text=f"请查看 @{username} 的消息")
                msg.mentioned = identity == "主魂"
                msg.entities = []
                self.assertEqual(
                    log_utils.managed_mention_identities(actor, msg, msg.text),
                    [identity],
                )
                checked += 1

        self.assertEqual(checked, 12)

    def test_mentions_only_logs_avatar_before_feedback_matching(self):
        actor = SimpleNamespace(
            my_info=SimpleNamespace(id=42, username="Gamling33", first_name="", last_name=""),
            identity_usernames={"主魂": ["Gamling33"]},
            avatar_usernames={"crayonxxin": "厚土"},
            avatars=["厚土"],
            current_identity="主魂",
        )
        msg = DummyMessage(2001, text="秘境提醒 @Crayonxxin 请立即处理")
        msg.mentioned = False
        msg.entities = []
        sender = SimpleNamespace(username="some_player", first_name="路人", last_name="")
        target_logger = log_utils.logging.getLogger(actor.__class__.__name__)

        with (
            patch.object(target_logger, "info") as info_mock,
            patch.object(log_utils, "record_message_event") as event_mock,
            patch.object(log_utils, "record_command_response_for_reply", return_value=False),
            patch.object(log_utils, "record_command_response_for_related_event", return_value=False),
        ):
            self.assertTrue(log_utils.log_mention_if_needed(
                actor,
                msg,
                text=msg.text,
                sender=sender,
                mentions_only=True,
            ))

        self.assertIn("[Avatar: 厚土]", info_mock.call_args.args[0])
        self.assertEqual(event_mock.call_args.kwargs["identity"], "厚土")

    def test_main_mention_is_not_relabelled_as_current_avatar(self):
        actor = SimpleNamespace(
            my_info=SimpleNamespace(id=42, username="Gamling33", first_name="", last_name=""),
            identity_usernames={"主魂": ["Gamling33"]},
            avatar_usernames={"crayonxxin": "厚土"},
            avatars=["厚土"],
            current_identity="厚土",
            get_identity_from_msg=lambda msg: None,
        )
        msg = DummyMessage(2002, text="@Gamling33 主魂请查看")
        msg.mentioned = True
        msg.entities = []

        with (
            patch.object(log_utils.logging.getLogger(actor.__class__.__name__), "info"),
            patch.object(log_utils, "record_message_event") as event_mock,
            patch.object(log_utils, "record_command_response_for_reply", return_value=False),
            patch.object(log_utils, "record_command_response_for_related_event", return_value=False),
        ):
            self.assertTrue(log_utils.log_mention_if_needed(
                actor,
                msg,
                text=msg.text,
                sender=SimpleNamespace(username="some_player"),
                mentions_only=True,
            ))

        self.assertEqual(event_mock.call_args.kwargs["identity"], "主魂")

    def test_time_critical_wait_clears_stale_star_schedule(self):
        actor = DummyAvatarCommon()
        avatar = actor.get_avatar_state("缘生子")
        stale = add_seconds_str(now_str(), -600)
        avatar["pending_star_gazing_target_time"] = stale
        avatar["next_star_gazing_time"] = stale

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(common_command_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertEqual(actor.time_critical_identity_wait("缘生子"), -1)

        self.assertEqual(avatar["pending_star_gazing_target_time"], "")
        self.assertEqual(avatar["next_star_gazing_time"], "")

    def test_time_critical_wait_keeps_recent_star_schedule_due(self):
        actor = DummyAvatarCommon()
        avatar = actor.get_avatar_state("缘生子")
        avatar["pending_star_gazing_target_time"] = add_seconds_str(now_str(), -30)

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(common_command_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertEqual(actor.time_critical_identity_wait("缘生子"), 0)

        self.assertNotEqual(avatar["pending_star_gazing_target_time"], "")

    def test_concubine_atomic_batch_blocks_regular_identity_switches(self):
        async def scenario():
            actor = DummyAtomicConcubine()
            acquired = asyncio.Event()
            release = asyncio.Event()

            async def hold_batch():
                async with concubine_features._ConcubineAtomicTask(actor, "ConcubineChain-素心子"):
                    acquired.set()
                    await release.wait()

            task = asyncio.create_task(hold_batch())
            await acquired.wait()
            try:
                self.assertTrue(actor.should_wait_for_atomic_task(".野外历练"))
                self.assertTrue(actor.should_wait_for_atomic_task(".侍妾远航 冒险"))
            finally:
                release.set()
                await task

            self.assertFalse(actor.should_wait_for_atomic_task(".野外历练"))

        asyncio.run(scenario())

    def test_concubine_atomic_batch_allows_star_gazing_interrupt(self):
        async def scenario():
            actor = DummyAtomicConcubine()
            acquired = asyncio.Event()
            release = asyncio.Event()

            async def hold_batch():
                async with concubine_features._ConcubineAtomicTask(actor, "ConcubineChain-素心子"):
                    acquired.set()
                    await release.wait()

            task = asyncio.create_task(hold_batch())
            await acquired.wait()
            try:
                self.assertFalse(actor.should_wait_for_atomic_task(".观星"))
                self.assertFalse(actor.should_wait_for_atomic_task(".改换星移 @Target"))

                actor.active_atomic_task = asyncio.current_task()
                self.assertTrue(actor.should_wait_for_atomic_task(".野外历练"))
                self.assertFalse(actor.should_wait_for_atomic_task(".观星"))
            finally:
                release.set()
                await task

        asyncio.run(scenario())

    def test_heart_trial_atomic_batch_blocks_yinluo_interrupts(self):
        async def scenario():
            actor = DummyAtomicConcubine()
            acquired = asyncio.Event()
            release = asyncio.Event()

            async def hold_batch():
                async with concubine_features._ConcubineAtomicTask(actor, "HeartTrial-素缘子"):
                    acquired.set()
                    await release.wait()

            task = asyncio.create_task(hold_batch())
            await acquired.wait()
            try:
                self.assertTrue(actor.should_wait_for_atomic_task(".切换 缘生子"))
                self.assertTrue(actor.should_wait_for_atomic_task(".召唤魔影"))
                self.assertFalse(actor.should_wait_for_atomic_task(".观星"))
            finally:
                release.set()
                await task

            self.assertFalse(actor.should_wait_for_atomic_task(".切换 缘生子"))

        asyncio.run(scenario())

    def test_meditation_restart_chain_blocks_identity_switches(self):
        async def scenario():
            class DummyMeditation(CommonCommandMixin, ConcubineMixin):
                def __init__(self):
                    self.state = {"avatars": {"素心子": {}}}
                    self.active_atomic_task = None
                    self.sent = []
                    self.checked = asyncio.Event()
                    self.release_check = asyncio.Event()

                def get_avatar_state(self, avatar):
                    return self.state.setdefault("avatars", {}).setdefault(avatar, {})

                def save_state(self):
                    pass

                def response_text(self, resp):
                    return str(resp or "")

                def parse_wait_time(self, text):
                    return 8 * 3600 if "8小时" in str(text or "") else 0

                def update_avatar_states(self, avatar, values):
                    self.get_avatar_state(avatar).update(values)

                def set_avatar_state(self, avatar, key, value):
                    self.get_avatar_state(avatar)[key] = value

                async def record_avatar_deep_meditation_start(self, avatar, response_text):
                    self.update_avatar_states(avatar, self.meditation_active_state_values("2099-01-01 00:00:00"))
                    return True

                async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                    self.sent.append((identity, command))
                    if command == ".查看闭关":
                        self.checked.set()
                        await self.release_check.wait()
                        return "你并未处于深度闭关之中。"
                    if command == ".闭关修炼":
                        return "闭关成功，本次修为增加 100 点。"
                    if command == ".深度闭关":
                        return "你已进入深度闭关状态，神魂将自行吐纳 **8小时**。"
                    raise AssertionError(f"unexpected command: {command}")

            actor = DummyMeditation()
            task = asyncio.create_task(actor.run_avatar_meditation_restart_chain("素心子"))
            await actor.checked.wait()
            self.assertTrue(actor.should_wait_for_atomic_task(".野外历练"))
            actor.release_check.set()
            result = await task
            self.assertEqual(result["status"], "started")
            self.assertEqual(actor.sent, [
                ("素心子", ".查看闭关"),
                ("素心子", ".闭关修炼"),
                ("素心子", ".深度闭关"),
            ])
            self.assertFalse(actor.should_wait_for_atomic_task(".野外历练"))

        asyncio.run(scenario())

    def test_avatar_field_training_blocks_switch_until_edited_settlement(self):
        async def scenario():
            class DummyFieldTraining(CommonCommandMixin, ConcubineMixin):
                def __init__(self):
                    self.state = {"avatars": {"缘生子": {"next_field_training_time": "", "last_field_training_time": ""}}}
                    self.active_atomic_task = None
                    self.sent = []
                    self.waiting_for_edit = asyncio.Event()
                    self.release_edit = asyncio.Event()

                def get_avatar_state(self, avatar):
                    return self.state.setdefault("avatars", {}).setdefault(avatar, {})

                def save_state(self):
                    pass

                def response_text(self, resp):
                    return getattr(resp, "text", "") if hasattr(resp, "text") else str(resp or "")

                def parse_wait_time(self, text):
                    return 0

                def update_avatar_states(self, avatar, values):
                    self.get_avatar_state(avatar).update(values)

                def set_avatar_state(self, avatar, key, value):
                    self.get_avatar_state(avatar)[key] = value

                def field_training_plan(self, avatar):
                    return field_training_plan_from_features(avatar, {"training_cmd": ".野外历练"})

                async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                    self.sent.append((identity, command))
                    return SimpleNamespace(
                        id=7001,
                        text="**【野外历练】**\n@foo 选择【均衡】策略，正向荒野深处行去...",
                    )

                async def wait_for_field_training_settlement(self, resp, identity="主魂", **kwargs):
                    self.waiting_for_edit.set()
                    await self.release_edit.wait()
                    return SimpleNamespace(
                        id=7001,
                        text="**【野外历练 · 灵机暗藏】**\n@foo 采得一份机缘，获得修为 **+157**。",
                    )

            actor = DummyFieldTraining()
            task = asyncio.create_task(actor.common_avatar_field_training_tick("缘生子"))
            await actor.waiting_for_edit.wait()
            self.assertTrue(actor.should_wait_for_atomic_task(".查看闭关"))
            actor.release_edit.set()
            self.assertEqual(await task, 5)
            self.assertEqual(actor.sent, [("缘生子", ".野外历练")])
            self.assertFalse(actor.should_wait_for_atomic_task(".查看闭关"))
            self.assertTrue(actor.get_avatar_state("缘生子")["next_field_training_time"])

        asyncio.run(scenario())

    def test_auto_exchange_wraps_exchange_with_concubine_place_and_recall(self):
        actions = []
        actor_holder = {}

        class FakeClient:
            async def get_messages(self, chat_id, ids):
                # The bot deletes the offer quickly; exchange must fall back to
                # a plain command instead of abandoning the event.
                return None

            async def send_message(self, chat_id, command, reply_to=None):
                actions.append(("direct", command, reply_to))
                sent = SimpleNamespace(
                    id=900 + len(actions), text=command, chat_id=chat_id,
                    sender_id=42, reply_to=None,
                )
                actor = actor_holder["actor"]
                loop = asyncio.get_running_loop()
                if command == ".交换 法宝":
                    settlement = SimpleNamespace(
                        id=990,
                        text=(
                            "【天机异闻·南陇侯的交易】道友 @hajiimiii 经过深思熟虑，"
                            "选择将侍妾【测试】与南陇侯交换！作为回报，南陇侯赐予法宝。"
                        ),
                        chat_id=chat_id,
                        sender_id=777,
                        reply_to=None,
                    )
                    loop.call_soon(
                        auto_reply_features._consume_exchange_settlement,
                        actor,
                        settlement,
                        settlement.text,
                        SimpleNamespace(username="fanrenxiuxian_bot"),
                    )
                elif command == ".召回侍妾":
                    reply = SimpleNamespace(
                        id=991,
                        text="你已将【测试】从藏娇阁中召回，随你一同历练。",
                        chat_id=chat_id,
                        sender_id=778,
                        reply_to=SimpleNamespace(reply_to_msg_id=sent.id),
                    )
                    loop.call_soon(
                        auto_reply_features.is_auto_reply_followup,
                        actor,
                        reply,
                        SimpleNamespace(username="fanrenxiuxian_bot"),
                    )
                return sent

        class DummyActor:
            def __init__(self):
                self.client = FakeClient()
                self.target_chat_id = -100123
                self.watch_bot = "fanrenxiuxian_bot"
                self.avatars = ["素心子"]
                self.avatar_usernames = {"hajiimiii": "素心子"}
                self.identity_usernames = {"主魂": ["TitanCreeper"]}
                self.my_info = SimpleNamespace(id=42, username="TitanCreeper")
                self.current_identity = "主魂"
                self.active_atomic_task = None
                self.state = {}
                self.save_state = lambda: None

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                actions.append((identity, command, kwargs.get("reply_to")))
                self.current_identity = identity
                return SimpleNamespace(text="你已将侍妾安置在藏娇阁中。")

        class FakeEvent:
            def __init__(self):
                self.message = SimpleNamespace(
                    id=321,
                    text="@hajiimiii！南陇侯给出选项：.交换 法宝 / .交换 功法",
                    entities=[],
                    reply_to=None,
                )

            async def get_sender(self):
                return SimpleNamespace(username="fanrenxiuxian_bot")

        delays = []

        async def fake_sleep(seconds):
            delays.append(seconds)

        async def scenario():
            actor = DummyActor()
            actor_holder["actor"] = actor
            handled = await auto_reply_features.maybe_auto_reply_exchange(actor, FakeEvent())
            await asyncio.gather(*list(actor._exchange_auto_tasks))
            return actor, handled

        with patch("auto_reply_features.asyncio.sleep", new=fake_sleep), patch.object(
            auto_reply_features, "record_command_sent", return_value=True
        ), patch.object(
            auto_reply_features, "record_command_response_for_command_id", return_value=True
        ):
            actor, handled = asyncio.run(scenario())

        self.assertTrue(handled)
        self.assertEqual(
            actions,
            [
                ("素心子", ".安置侍妾", None),
                ("direct", ".交换 法宝", None),
                ("direct", ".召回侍妾", None),
            ],
        )
        self.assertIn(5, delays)
        self.assertEqual(actor.state[auto_reply_features.EXCHANGE_STATE_KEY]["321"]["status"], "done")
        self.assertIsNone(actor.active_atomic_task)

    def test_auto_merchant_ignores_other_username(self):
        class DummyActor:
            def __init__(self):
                self.watch_bot = "fanrenxiuxian_bot"
                self.avatars = []
                self.avatar_usernames = {}
                self.identity_usernames = {"主魂": ["Waaiging"]}
                self.my_info = SimpleNamespace(id=42, username="Waaiging")

        class FakeEvent:
            def __init__(self):
                self.message = SimpleNamespace(
                    id=421,
                    text="【天机异动 · 异界商人】请 @AliceI005 速用 .查看货品 与其交易！",
                    entities=[],
                    reply_to=None,
                )

            async def get_sender(self):
                return SimpleNamespace(username="fanrenxiuxian_bot")

        handled = asyncio.run(auto_reply_features.maybe_auto_reply_exchange(DummyActor(), FakeEvent()))
        self.assertFalse(handled)

    def test_auto_merchant_buys_priority_goods_for_mentioned_identity(self):
        actions = []
        goods_text = """
【异界商人·玄天】（将在 3599 分钟后离去）
1. 尘封的储物袋 (剩余: 1)
2. 掌天瓶的仿制品 (剩余: 1)
3. 九天息壤 (剩余: 1)
使用 .购买商品 <编号> 进行交易。
"""

        class DummyActor:
            def __init__(self):
                self.watch_bot = "fanrenxiuxian_bot"
                self.avatars = []
                self.avatar_usernames = {}
                self.identity_usernames = {"主魂": ["Waaiging"]}
                self.my_info = SimpleNamespace(id=42, username="Waaiging")
                self.current_identity = "主魂"

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                actions.append((identity, command, kwargs.get("reply_to"), kwargs.get("max_retries")))
                if command == ".查看货品":
                    return SimpleNamespace(id=777, text=goods_text)
                return SimpleNamespace(id=778 + len(actions), text="购买成功，宝物已收入储物袋。")

        class FakeEvent:
            def __init__(self):
                self.message = SimpleNamespace(
                    id=422,
                    text="【天机异动 · 异界商人】请 @Waaiging 速用 .查看货品 与其交易！",
                    entities=[],
                    reply_to=None,
                )

            async def get_sender(self):
                return SimpleNamespace(username="fanrenxiuxian_bot")

        async def fake_sleep(seconds):
            return None

        actor = DummyActor()
        with tempfile.TemporaryDirectory() as tmpdir:
            merchant_state = os.path.join(tmpdir, "merchant_auto_reply_state.json")
            with patch("auto_reply_features.asyncio.sleep", new=fake_sleep), \
                    patch.object(auto_reply_features, "MERCHANT_STATE_FILE", merchant_state):
                handled = asyncio.run(auto_reply_features.maybe_auto_reply_exchange(actor, FakeEvent()))

        self.assertTrue(handled)
        self.assertEqual(actions, [
            ("主魂", ".查看货品", None, 0),
            ("主魂", ".购买商品 2", None, 0),
            ("主魂", ".购买商品 3", None, 0),
            ("主魂", ".购买商品 1", None, 0),
        ])

    def test_field_training_plan_preserves_identity_specific_prefixes(self):
        plan = field_training_plan_from_features(
            "无咎子",
            {
                "meditation_prefix": ".推命",
                "training_prefix_commands": [".推命 探索", ".改命 探索"],
                "training_cmd": ".野外历练",
                "training_level": "深入",
            },
        )
        self.assertEqual(plan.command, ".野外历练 深入")
        self.assertEqual(
            plan.all_commands(),
            [".推命 探索", ".改命 探索", ".野外历练 深入"],
        )

        default_avatar = field_training_plan_from_features("缘生子", {})
        self.assertEqual(default_avatar.all_commands(), [".野外历练"])

        main = field_training_plan_from_features("主魂", main_command=".野外历练 谨慎")
        self.assertEqual(main.all_commands(), [".野外历练 谨慎"])

    def test_yuanying_and_rift_plans_preserve_account_differences(self):
        main_yuanying = yuanying_out_plan("主魂")
        self.assertEqual(main_yuanying.command, ".元婴出窍")
        self.assertEqual(main_yuanying.next_key, "next_yuanying_out_time")
        self.assertFalse(main_yuanying.force_identity_check)

        sub_main_yuanying = yuanying_out_plan("主魂", main_command=".元婴闭关")
        self.assertEqual(sub_main_yuanying.command, ".元婴闭关")

        avatar_yuanying = yuanying_out_plan("缘生子", main_command=".元婴闭关")
        self.assertEqual(avatar_yuanying.command, ".元婴出窍")
        self.assertTrue(avatar_yuanying.force_identity_check)

        main_rift = rift_search_plan("主魂")
        self.assertEqual(main_rift.command, ".探寻裂缝")
        self.assertEqual(main_rift.last_key, "last_rift_search_time")
        self.assertEqual(main_rift.next_key, "next_rift_search_time")
        self.assertTrue(main_rift.return_response_msg)

        avatar_rift = rift_search_plan("缘生子")
        self.assertEqual(avatar_rift.command, ".探寻裂缝")
        self.assertFalse(avatar_rift.return_response_msg)

    def test_fixed_cooldown_plans_preserve_command_metadata(self):
        main_treasure = treasure_touch_plan(".抚摸法宝 玄天斩灵剑")
        self.assertEqual(main_treasure.command, ".抚摸法宝 玄天斩灵剑")
        self.assertEqual(main_treasure.last_key, "last_treasure_touch_time")
        self.assertEqual(main_treasure.next_key, "next_treasure_touch_time")
        self.assertEqual(main_treasure.timeout, 90)
        self.assertTrue(main_treasure.force_identity_check)

    def test_daily_reward_parser_and_summary_group_by_identity(self):
        actor = DummyAvatarCommon()
        text = "【元婴闭关结算】元婴闭关结束，获得修为 +2000，获得了【煞气小刀】x1。"

        self.assertTrue(actor.record_daily_reward_event("主魂", ".元婴闭关", text, source="test"))
        self.assertFalse(actor.record_daily_reward_event("主魂", ".元婴闭关", text, source="duplicate"))
        self.assertTrue(actor.record_daily_reward_event(
            "缘生子",
            ".探寻裂缝",
            "探寻裂缝成功，发现秘藏，获得【灵石】x3，宗门贡献 +5。",
            source="test",
        ))
        today = datetime.now().strftime("%Y-%m-%d")
        summary = actor.build_daily_reward_summary_text(today)

        self.assertIn("账号：DummyAvatarCommon", summary)
        self.assertIn("【主魂】", summary)
        self.assertIn("- .元婴闭关：1 次（成功 1）；修为 +2000、煞气小刀 +1", summary)
        self.assertIn("【缘生子】", summary)
        self.assertIn("- .探寻裂缝：1 次（成功 1）；宗门贡献 +5、灵石 +3", summary)

    def test_daily_reward_hooks_for_yuanying_rift_and_field_training(self):
        actor = DummyAvatarCommon()
        actor.record_yuanying_out_settlement_response(
            "【元婴归窍总结】元婴神游归来，带回了以下收获：修为 +1200。",
            identity="缘生子",
            source="fixture",
        )
        actor.record_identity_fixed_cd_command_response(
            "缘生子",
            "探寻裂缝成功，获得【空间碎片】x2。",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        )
        actor.record_identity_field_training_response(
            "缘生子",
            "【野外历练 · 灵机暗藏】本次获得：修为 +300、灵石 x4。",
            context="fixture",
        )

        today = datetime.now().strftime("%Y-%m-%d")
        summary = actor.build_daily_reward_summary_text(today)
        self.assertIn("- .元婴出窍：1 次（成功 1）；修为 +1200", summary)
        self.assertIn("- .探寻裂缝：1 次（成功 1）；空间碎片 +2", summary)
        self.assertIn("- .野外历练：1 次（成功 1）；修为 +300、灵石 +4", summary)

        sub_treasure = treasure_touch_plan(".抚摸法宝 青竹蜂云剑")
        self.assertEqual(sub_treasure.command, ".抚摸法宝 青竹蜂云剑")

        nurture = nurture_spirit_plan(".温养器灵 斩灵")
        self.assertEqual(nurture.command, ".温养器灵 斩灵")
        self.assertEqual(nurture.next_key, "next_nurture_spirit_time")
        self.assertFalse(nurture.force_identity_check)

        ask_dao = ask_dao_plan()
        self.assertEqual(ask_dao.command, ".问道")
        self.assertEqual(ask_dao.last_key, "last_ask_dao_time")
        self.assertEqual(ask_dao.next_key, "next_ask_dao_time")
        self.assertEqual(ask_dao.max_retries, 1)
        self.assertTrue(ask_dao.force_identity_check)

    def test_daily_reward_records_edited_settlements_and_field_training_failure(self):
        actor = DummyAvatarCommon()
        actor.command_avatar_map = {1001: "缘生子", 1002: "缘生子"}
        actor.current_identity = "主魂"
        actor.my_info = SimpleNamespace(username="TitanCreeper")
        pending_msg = DummyMessage(
            2001,
            text="**【野外历练】**\n@foo 选择【均衡】策略，正向荒野深处行去...",
            reply_to_msg_id=1001,
        )
        success_msg = DummyMessage(
            2001,
            text="**【野外历练 · 灵机暗藏】**\n@foo 采得一份机缘，获得修为 **+157**。",
            reply_to_msg_id=1001,
        )
        failure_msg = DummyMessage(
            2002,
            text="**【野外历练 · 妖兽遭遇】**\n@foo 负伤而归，修为折损 **67** 点。",
            reply_to_msg_id=1002,
        )

        self.assertFalse(actor.maybe_record_daily_reward_from_edited_message(pending_msg, pending_msg.text))
        self.assertTrue(actor.maybe_record_daily_reward_from_edited_message(success_msg, success_msg.text))
        self.assertTrue(actor.maybe_record_daily_reward_from_edited_message(failure_msg, failure_msg.text))

        today = datetime.now().strftime("%Y-%m-%d")
        summary = actor.build_daily_reward_summary_text(today)
        self.assertIn("- .野外历练：2 次（成功 1 / 失败 1）；修为 +90", summary)
        self.assertEqual(len(actor.state["daily_reward_events"]), 2)

    def test_daily_reward_edited_final_commands_parse_rewards(self):
        actor = DummyAvatarCommon()
        actor.current_identity = "主魂"
        actor.my_info = SimpleNamespace(username="TitanCreeper")
        cases = [
            (".探寻裂缝", "探寻裂缝成功，发现秘藏，获得【空间碎片】x2，宗门贡献 +5。"),
            (".探渊 青蛟", "青蛟自万兽渊归来，带回【兽骨】x3，获得修为 +120。"),
            (".元婴闭关", "**【元婴闭关结算】**\n元婴闭关结束，获得修为 +2000。"),
            (".元婴出窍", "**【元婴归窍总结】**\n元婴神游归来，带回了以下收获：修为 +1200。"),
        ]
        for index, (command, text) in enumerate(cases, start=1):
            msg = DummyMessage(3000 + index, text=text, reply_to_msg_id=4000 + index)
            actor.command_avatar_map = {4000 + index: "主魂"}
            actor.feedback_commands = {4000 + index: command}
            self.assertTrue(actor.maybe_record_daily_reward_from_edited_message(msg, text), command)

        today = datetime.now().strftime("%Y-%m-%d")
        summary = actor.build_daily_reward_summary_text(today)
        self.assertIn("- .探寻裂缝：1 次（成功 1）；宗门贡献 +5、空间碎片 +2", summary)
        self.assertIn("- .探渊：1 次（成功 1）；修为 +120、兽骨 +3", summary)
        self.assertIn("- .元婴闭关：1 次（成功 1）；修为 +2000", summary)
        self.assertIn("- .元婴出窍：1 次（成功 1）；修为 +1200", summary)

    def test_daily_reward_ignores_deep_meditation(self):
        actor = DummyAvatarCommon()
        text = "📜 **修士 ****@TitanCreeper**** 深度闭关总结**\n本次深度闭关，你的修为最终变化了 **15246** 点！"
        msg = DummyMessage(3100, text=text, reply_to_msg_id=4100)
        actor.command_avatar_map = {4100: "主魂"}
        actor.feedback_commands = {4100: ".深度闭关"}

        self.assertFalse(actor.maybe_record_daily_reward_from_edited_message(msg, text))
        actor.state["daily_reward_events"] = [{
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": now_str(),
            "identity": "主魂",
            "command": ".深度闭关",
            "rewards": {"修为": 15246},
            "excerpt": text,
            "clean": text,
            "final": True,
        }]

        self.assertEqual(actor.build_daily_reward_summary_text(datetime.now().strftime("%Y-%m-%d")), "")

    def test_daily_reward_records_avatar_yuanying_edited_summary_by_username(self):
        actor = DummyAvatarCommon()
        actor.avatars = ["无咎子"]
        actor.avatar_usernames = {"wuxinglinggen": "无咎子"}
        actor.my_info = SimpleNamespace(username="Waaiging")
        pending_text = "✨ **元神回响**：感应到 @wuxinglinggen 的元婴已神游归来，正在清点收获..."
        final_text = (
            "📜 **修士 ****@wuxinglinggen**** 元神归窍总结**\n"
            "你的元婴在虚空中神游八小时，带回了以下收获：\n"
            " - **【三级妖丹】x3**\n"
            " - **【养魂木】x2**\n"
            " - **【天火液丹方】x1**\n"
            "**元婴成长**:\n"
            " - 获得了 **1536** 点经验。"
        )

        self.assertFalse(actor.record_daily_reward_event("无咎子", ".元婴出窍", pending_text, source="passive"))
        self.assertTrue(actor.maybe_record_daily_reward_from_edited_message(DummyMessage(3200, text=final_text), final_text))

        today = datetime.now().strftime("%Y-%m-%d")
        events = actor.state["daily_reward_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["identity"], "无咎子")
        summary = actor.build_daily_reward_summary_text(today)
        self.assertIn("【无咎子】", summary)
        self.assertIn("三级妖丹 +3", summary)
        self.assertIn("养魂木 +2", summary)
        self.assertIn("天火液丹方 +1", summary)
        self.assertIn("经验 +1536", summary)

    def test_daily_reward_parser_counts_unquantified_bracket_rewards(self):
        actor = DummyAvatarCommon()
        rewards = actor.parse_reward_items_from_text(
            "【探寻成功】你的元婴满载而归，为你带来了：【法则碎片·木】, "
            "【法则碎片·木】, 一份意外之喜 【九转凝魂丹丹方】！"
        )

        self.assertEqual(rewards.get("法则碎片·木"), 2)
        self.assertEqual(rewards.get("九转凝魂丹丹方"), 1)
        self.assertNotIn("探寻成功", rewards)

    def test_daily_reward_parser_filters_status_titles_and_counts_treasure(self):
        actor = DummyAvatarCommon()
        rift_text = (
            "【激战得胜】\n"
            "经过一番苦战，你成功斩杀了时空异兽！\n"
            "你从其残骸中，获得了【法则碎片·空间】x1、【四级妖丹】x5，"
            "以及一件至宝：【太虚仙露】！"
        )
        stairs_text = (
            "【凌霄云阶】\n"
            "你一举踏破十二重云阶，完成了第 19 轮【周天巡天】！\n"
            "并获得 12小时 的【天门余韵】，斗法战力提升 12%。\n"
            "本次获得 296 点修为、42 点宗门贡献。\n"
            "额外收获: 【养魂木】x3、【天雷竹】x1"
        )
        collect_text = (
            "收集完成！你获得了：【天雷竹】x10, 【金精矿】x3, 【二级妖丹】x10。\n"
            "【天人感应】因与侍妾心意相通，其中有 2 次收集触发了双倍收获！"
        )
        abyss_text = (
            "胜利！\n"
            "你的灵兽【六翼】成功击败了对手！它带回了战利品：\n"
            "- 获得 291 点经验, 【三级妖丹】x1, 【蛮荒兽血】x2。"
        )

        self.assertEqual(actor.parse_reward_items_from_text(rift_text), {
            "法则碎片·空间": 1,
            "四级妖丹": 5,
            "太虚仙露": 1,
        })
        stairs_rewards = actor.parse_reward_items_from_text(stairs_text)
        self.assertEqual(stairs_rewards, {
            "修为": 296,
            "宗门贡献": 42,
            "养魂木": 3,
            "天雷竹": 1,
        })
        self.assertNotIn("周天巡天", stairs_rewards)
        self.assertNotIn("天门余韵", stairs_rewards)
        collect_rewards = actor.parse_reward_items_from_text(collect_text)
        self.assertEqual(collect_rewards, {"天雷竹": 10, "金精矿": 3, "二级妖丹": 10})
        self.assertNotIn("天人感应", collect_rewards)
        abyss_rewards = actor.parse_reward_items_from_text(abyss_text)
        self.assertEqual(abyss_rewards, {"经验": 291, "三级妖丹": 1, "蛮荒兽血": 2})
        self.assertNotIn("六翼", abyss_rewards)

    def test_daily_reward_summary_plain_text_for_xiaohao(self):
        actor = DummyAvatarCommon()
        actor.account_key = "xiaohao"
        actor.avatars = ["问心子"]
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state["daily_reward_events"] = [{
            "date": today,
            "time": "12:00:00",
            "identity": "问心子",
            "command": ".探寻裂缝",
            "clean": (
                "【激战得胜】经过一番苦战，你成功斩杀了时空异兽！"
                "你从其残骸中，获得了【法则碎片·空间】x1、【四级妖丹】x5，"
                "以及一件至宝：【太虚仙露】！"
            ),
            "rewards": {},
            "final": True,
        }]

        summary = actor.build_daily_reward_summary_text(today)

        self.assertFalse(summary.startswith("周期收益日报"))
        self.assertIn("账号：小号", summary)
        self.assertIn("【问心子】", summary)
        self.assertIn("- .探寻裂缝：1 次（成功 1）；四级妖丹 +5、太虚仙露 +1、法则碎片·空间 +1", summary)
        self.assertNotIn("\\-", summary)
        self.assertNotIn("\\+", summary)
        self.assertNotIn("激战得胜", summary)

    def test_daily_reward_summary_markdown_for_xiaohao(self):
        actor = DummyAvatarCommon()
        actor.account_key = "xiaohao"
        actor.avatars = ["问心子"]
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state["daily_reward_events"] = [{
            "date": today,
            "time": "12:00:00",
            "identity": "问心子",
            "command": ".探寻裂缝",
            "clean": (
                "【激战得胜】经过一番苦战，你成功斩杀了时空异兽！"
                "你从其残骸中，获得了【法则碎片·空间】x1、【四级妖丹】x5，"
                "以及一件至宝：【太虚仙露】！"
            ),
            "rewards": {},
            "final": True,
        }, {
            "date": today,
            "time": "13:00:00",
            "identity": "缘生子",
            "command": ".探寻裂缝",
            "clean": "【大凶·虚空噬体】肉身化为齑粉，神魂遭受重创，已陷入 6小时 的【虚弱期】！",
            "rewards": {},
            "final": True,
        }]

        summary = actor.build_daily_reward_summary_text(today, markdown=True)

        self.assertIn("*周期收益日报*", summary)
        self.assertIn("统计日期：", summary)
        self.assertIn("账号：小号", summary)
        self.assertIn("*账号明细:*", summary)
        self.assertIn("\\- *问心子*: 有效1｜结算1｜裂缝1｜成功: 1｜脱险: 0｜失败: 0", summary)
        self.assertIn("\\- *缘生子*: 有效1｜结算1｜裂缝1｜成功: 0｜脱险: 0｜失败: 1", summary)
        self.assertIn("✓裂缝｜法则碎片·空间x1｜四级妖丹x5｜太虚仙露x1", summary)
        self.assertIn("✗裂缝｜无收益", summary)
        self.assertNotIn("未解析", summary)
        self.assertNotIn("激战得胜", summary)

    def test_daily_reward_summary_send_skips_push_by_default(self):
        actor = DummyAvatarCommon()
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state["daily_reward_events"] = [{
            "date": today,
            "time": "12:00:00",
            "identity": "主魂",
            "command": ".元婴闭关",
            "clean": "【元婴闭关结算】元婴闭关结束，获得修为 +2000。",
            "rewards": {"修为": 2000},
            "final": True,
        }]

        async def fake_alert(*args, **kwargs):
            raise AssertionError("daily reward summary should not push by default")

        with patch.object(common_command_features, "send_text_alert", fake_alert):
            sent = asyncio.run(actor.send_daily_reward_summary_for_date(today))

        self.assertFalse(sent)
        self.assertEqual(actor.state["daily_reward_last_sent_date"], today)

    def test_daily_reward_summary_send_uses_markdown_v2_when_enabled(self):
        actor = DummyAvatarCommon()
        actor.config = {"daily_reward_summary_push": True}
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state["daily_reward_events"] = [{
            "date": today,
            "time": "12:00:00",
            "identity": "主魂",
            "command": ".元婴闭关",
            "clean": "【元婴闭关结算】元婴闭关结束，获得修为 +2000。",
            "rewards": {"修为": 2000},
            "final": True,
        }]
        captured = {}

        async def fake_alert(actor_arg, title, text, logger=None, parse_mode=None):
            captured["title"] = title
            captured["text"] = text
            captured["parse_mode"] = parse_mode
            return True

        with patch.object(common_command_features, "send_text_alert", fake_alert):
            sent = asyncio.run(actor.send_daily_reward_summary_for_date(today))

        self.assertTrue(sent)
        self.assertEqual(captured["title"], "周期收益日报")
        self.assertEqual(captured["parse_mode"], "MarkdownV2")
        self.assertIn("*账号明细:*", captured["text"])
        self.assertIn("修为\\+2,000", captured["text"])

    def test_daily_reward_ignores_rift_intermediate_edit(self):
        actor = DummyAvatarCommon()
        actor.command_avatar_map = {4200: "缘生子"}
        actor.feedback_commands = {4200: ".探寻裂缝"}
        pending = "元婴在无尽的虚空中穿行，成功捕获了几缕逸散的法则本源！"
        final = (
            "【探寻成功】\n"
            "你的元婴满载而归，为你带来了：【法则碎片·土】, 【法则碎片·空间】！"
        )

        self.assertFalse(actor.maybe_record_daily_reward_from_edited_message(
            DummyMessage(3300, text=pending, reply_to_msg_id=4200),
            pending,
        ))
        self.assertTrue(actor.maybe_record_daily_reward_from_edited_message(
            DummyMessage(3300, text=final, reply_to_msg_id=4200),
            final,
        ))

        events = actor.state["daily_reward_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["rewards"].get("法则碎片·土"), 1)
        self.assertEqual(events[0]["rewards"].get("法则碎片·空间"), 1)

    def test_daily_reward_rift_ignores_destiny_prefix(self):
        actor = DummyAvatarCommon()
        text = (
            "【探寻成功】\n"
            "命盘【贪狼】照命，主偏财夺势，闭关奇遇与探寻收获更盛。\n"
            "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30\n"
            "【天星偏转】 凶险偏移，珍稀显化上扬\n"
            "你的元婴满载而归，为你带来了：【法则碎片·火】, 【法则碎片·风】, 【法则碎片·金】！"
        )
        reward_text = actor.daily_reward_parse_text_for_command(".探寻裂缝", text)
        rewards = actor.parse_reward_items_from_text(reward_text)

        self.assertEqual(rewards, {
            "法则碎片·火": 1,
            "法则碎片·风": 1,
            "法则碎片·金": 1,
        })

    def test_daily_reward_records_rift_weakness_failure_without_rewards(self):
        actor = DummyAvatarCommon()
        actor.command_avatar_map = {4300: "主魂"}
        actor.feedback_commands = {4300: ".探寻裂缝"}
        text = (
            "【大凶·虚空噬体】\n"
            "你运气不佳，竟一头撞入了空间裂缝最深处的风暴核心！"
            "无可抵挡的撕裂之力瞬间将你的肉身化为齑粉！\n"
            "【元婴遁逃·虚弱】\n"
            "但你的神魂遭受重创，已陷入 6小时 的【虚弱期】！"
        )

        self.assertTrue(actor.maybe_record_daily_reward_from_edited_message(
            DummyMessage(3400, text=text, reply_to_msg_id=4300),
            text,
        ))
        today = datetime.now().strftime("%Y-%m-%d")
        summary = actor.build_daily_reward_summary_text(today)
        self.assertIn("- .探寻裂缝：1 次（失败 1）", summary)

    def test_daily_reward_field_training_beast_encounter_can_succeed(self):
        actor = DummyAvatarCommon()
        text = "【野外历练 · 妖兽遭遇】一番斗法后，妖兽伏诛。获得修为 +3213，获得【阴魂丝】x2。"
        rewards = actor.parse_reward_items_from_text(text)

        self.assertEqual(actor.daily_reward_outcome_from_text(".野外历练", text, rewards), "成功")

    def test_daily_reward_field_training_ignores_destiny_prefix(self):
        actor = DummyAvatarCommon()
        beast_text = (
            "【野外历练 · 妖兽遭遇】\n"
            "命盘【贪狼】照命，主偏财夺势。\n"
            "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30\n"
            "【改命待发】此道改命尚可维持 23小时59分钟\n"
            "【天星偏转】 趋吉偏转，材料显化上扬\n"
            "@wuxinglinggen 遭遇 裂风妖禽。\n"
            "战力对比: 你 8300822 / 妖兽 10131646，胜算 44%。\n"
            "一番斗法后，妖兽伏诛。\n"
            "获得修为 +45000，获得【四级妖丹】x1。"
        )
        rescue_text = (
            "【野外历练 · 改命脱险】\n"
            "命盘【贪狼】照命，主偏财夺势。\n"
            "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30\n"
            "【天星偏转】 趋吉偏转，材料显化上扬\n"
            "@wuxinglinggen 遭遇 幽冥鬼蛛，本已要负伤折返，司命盘却替你撬开了一线退路。\n"
            "【改命回天】你强行拨正命轨，硬从凶数中抢回一线生机。\n"
            "你虽未能尽取机缘，却仍带回了【三级妖丹】x1，且本次未损修为。"
        )
        chance_text = (
            "【野外历练 · 灵机暗藏】\n"
            "命盘【贪狼】照命，主偏财夺势。\n"
            "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30\n"
            "【天星偏转】 趋吉偏转，材料显化上扬\n"
            "@wuxinglinggen 在山涧残阵旁避开妖兽踪迹，采得一份机缘。\n"
            "获得修为 +45000，获得【四级妖丹】x1。"
        )

        beast_rewards = actor.parse_reward_items_from_text(
            actor.daily_reward_parse_text_for_command(".野外历练 深入", beast_text)
        )
        rescue_rewards = actor.parse_reward_items_from_text(
            actor.daily_reward_parse_text_for_command(".野外历练 深入", rescue_text)
        )
        chance_rewards = actor.parse_reward_items_from_text(
            actor.daily_reward_parse_text_for_command(".野外历练 深入", chance_text)
        )

        self.assertEqual(beast_rewards, {"修为": 45000, "四级妖丹": 1})
        self.assertEqual(rescue_rewards, {"三级妖丹": 1})
        self.assertEqual(chance_rewards, {"修为": 45000, "四级妖丹": 1})
        self.assertEqual(actor.daily_reward_outcome_from_text(".野外历练 深入", beast_text, beast_rewards), "成功")
        self.assertEqual(actor.daily_reward_outcome_from_text(".野外历练 深入", rescue_text, rescue_rewards), "脱险")
        self.assertNotIn("宗门贡献", beast_rewards)
        self.assertNotIn("贪狼", beast_rewards)

    def test_daily_reward_summary_reparses_old_field_training_events(self):
        actor = DummyAvatarCommon()
        today = datetime.now().strftime("%Y-%m-%d")
        text = (
            "【野外历练 · 妖兽遭遇】\n"
            "命盘【贪狼】照命。\n"
            "【推命命中】司命演算吻合，天机值 +1，宗门贡献 +30\n"
            "@wuxinglinggen 遭遇 裂风妖禽。\n"
            "一番斗法后，妖兽伏诛。\n"
            "获得修为 +45000，获得【四级妖丹】x1。"
        )
        actor.state["daily_reward_events"] = [{
            "date": today,
            "time": "12:00:00",
            "identity": "无咎子",
            "command": ".野外历练",
            "rewards": {"宗门贡献": 30, "修为": 45000, "四级妖丹": 1},
            "excerpt": text,
            "clean": text,
        }]

        summary = actor.build_daily_reward_summary_text(today)

        self.assertIn("【无咎子】", summary)
        self.assertIn("修为 +45000、四级妖丹 +1", summary)
        self.assertNotIn("宗门贡献", summary)

    def test_daily_reward_summary_drops_legacy_field_training_destiny_rewards(self):
        actor = DummyAvatarCommon()
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state["daily_reward_events"] = [{
            "date": today,
            "time": "12:00:00",
            "identity": "无咎子",
            "command": ".野外历练",
            "rewards": {"宗门贡献": 30, "三级妖丹": 1},
            "excerpt": "【野外历练 · 改命脱险】 命盘【贪狼】... 【推命命中】宗门贡献 +30 ...",
        }]

        summary = actor.build_daily_reward_summary_text(today)

        self.assertIn("三级妖丹 +1", summary)
        self.assertNotIn("宗门贡献", summary)

    def test_common_message_age_seconds_handles_timezone_and_missing_date(self):
        actor = DummyCommon()
        aware_msg = SimpleNamespace(date=datetime.now(timezone.utc) - timedelta(seconds=15))
        naive_msg = SimpleNamespace(date=datetime.utcnow() - timedelta(seconds=9))
        self.assertGreaterEqual(actor.message_age_seconds(aware_msg), 14)
        self.assertGreaterEqual(actor.message_age_seconds(naive_msg), 8)
        self.assertEqual(actor.message_age_seconds(SimpleNamespace()), 0)

    def test_common_formation_invite_and_avatar_match_helpers(self):
        actor = DummyCommon()
        actor.avatar_usernames = {"Ding303": "寻真子", "Sub_Avatar": "缘生子"}
        text = "【周天星斗大阵-启】@Ding303 正在布设大阵，尚需 2 位道友助阵。"
        self.assertEqual(actor.formation_invite_actor_username(text), "ding303")
        self.assertEqual(actor.avatar_username_for_identity("缘生子"), "sub_avatar")
        self.assertTrue(actor.formation_result_includes_avatar("大阵已成，@Sub_Avatar 已助阵。", "缘生子"))
        self.assertFalse(actor.formation_result_includes_avatar("大阵已成，@Other 已助阵。", "缘生子"))

    def test_common_star_gazing_pending_and_claim_helpers(self):
        actor = DummyCommon()
        now = datetime.now()
        actor.state = {
            "pending_star_gazing_target_time": add_seconds_str(now_str(), 60),
            "pending_star_shift_target_time": "",
            "pending_star_gazing_scheduled_time": "",
            "pending_star_gazing_manifest_time": dt_to_str(now),
            "pending_star_gazing_fate_type": "Good - 星辰异象",
            "pending_star_gazing_date": "2026-06-25",
            "star_gazing_claimed_manifest_time": dt_to_str(now),
            "star_gazing_claimed_avatar": "缘生子",
        }

        self.assertTrue(actor.common_has_pending_star_gazing_action())
        self.assertTrue(actor.common_star_gazing_claim_matches("缘生子", now))
        self.assertFalse(actor.common_star_gazing_claim_matches("厚土", now))
        self.assertTrue(actor.common_claimed_star_gazing_pending_due(
            "缘生子",
            dt_to_str(now + timedelta(seconds=1)),
            now=now,
        ))
        actor.common_clear_pending_star_gazing_schedule()
        self.assertEqual(actor.state["pending_star_gazing_target_time"], "")
        self.assertEqual(actor.state["pending_star_gazing_fate_type"], "")
        actor.common_clear_star_gazing_round_claim()
        self.assertEqual(actor.state["star_gazing_claimed_avatar"], "")
        self.assertEqual(actor.state["star_gazing_claimed_manifest_time"], "")

    def test_common_clear_stale_star_gazing_claim_cancels_old_task(self):
        class FakeTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        actor = DummyCommon()
        saved = []
        actor.save_state = lambda: saved.append(True)
        old_manifest = datetime(2026, 6, 25, 6, 0, 0)
        new_manifest = datetime(2026, 6, 25, 9, 0, 0)
        actor.state = {
            "pending_star_gazing_date": "2026-06-25",
            "pending_star_gazing_target_time": "2026-06-25 05:59:00",
            "pending_star_gazing_scheduled_time": "2026-06-25 05:59:00",
            "pending_star_gazing_manifest_time": dt_to_str(old_manifest),
            "pending_star_gazing_fate_type": "Good - 星辰异象",
            "star_gazing_claimed_manifest_time": dt_to_str(old_manifest),
            "star_gazing_claimed_avatar": "缘生子",
            "next_star_gazing_time": "2026-06-25 05:59:00",
        }
        actor.star_gazing_task = FakeTask()

        self.assertTrue(actor.common_clear_stale_star_gazing_claim_before_manifest(new_manifest))
        self.assertTrue(actor.star_gazing_task.cancelled)
        self.assertEqual(saved, [True])
        self.assertEqual(actor.state["pending_star_gazing_target_time"], "")
        self.assertEqual(actor.state["star_gazing_claimed_avatar"], "")
        self.assertEqual(actor.state["next_star_gazing_time"], "")

    def test_common_avatar_yuanying_check_uses_shared_plan_sender(self):
        class DummyTimedAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            def _repair_yuanying_out_from_last_start(self, reason="", identity="主魂"):
                return ""

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return "元婴出窍成功，云游 8 小时。"

            def record_yuanying_out_start_response(self, text, identity="主魂"):
                state = self.get_avatar_state(identity)
                state["yuanying_out_active"] = True
                state["last_yuanying_out_time"] = now_str()

        actor = DummyTimedAvatar()
        self.assertTrue(asyncio.run(actor.common_avatar_yuanying_out_check("缘生子")))
        self.assertEqual(actor.sent[0][0], "缘生子")
        self.assertEqual(actor.sent[0][1], ".元婴出窍")
        self.assertTrue(actor.sent[0][2]["force_identity_check"])
        self.assertFalse(actor.sent[0][2]["return_response_msg"])
        self.assertTrue(actor.get_avatar_state("缘生子")["yuanying_out_active"])

    def test_common_avatar_timed_check_can_require_meditation_ready(self):
        class DummyTimedAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            def avatar_meditation_needs_attention(self, avatar):
                return True

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return "OK"

        actor = DummyTimedAvatar()
        self.assertFalse(asyncio.run(actor.common_avatar_rift_search_check("缘生子", 12 * 3600, require_meditation_ready=True)))
        self.assertEqual(actor.sent, [])

    def test_tianxing_avatar_rift_sends_both_exploration_prefixes(self):
        class DummyTianxingRift(DummyCommon):
            avatars = ["无咎子"]

            def __init__(self):
                super().__init__()
                self.state = {"avatars": {"无咎子": {}}}
                self.identity_sect_names = {"无咎子": "天星宗"}
                self.sent = []

            def get_avatar_state(self, identity):
                return self.state.setdefault("avatars", {}).setdefault(identity, {})

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command))
                if command == ".探寻裂缝":
                    return "你运转全身法力，撕开一道漆黑的空间裂缝，将元婴送入其中探寻机缘。"
                return "司命演算完成。"

        async def fake_sleep(_seconds):
            return None

        actor = DummyTianxingRift()
        with patch.object(common_command_features.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.common_avatar_rift_search_check("无咎子", 12 * 3600)))

        self.assertEqual(actor.sent, [
            ("无咎子", ".推命 探索"),
            ("无咎子", ".改命 探索"),
            ("无咎子", ".探寻裂缝"),
        ])
        self.assertTrue(actor.get_avatar_state("无咎子")["next_rift_search_time"])

    def test_wujiuzi_rift_probes_wind_wing_adjusted_cooldown(self):
        class DummyWujiuziRift(DummyCommon):
            avatars = ["无咎子"]
            actual_cooldown_probe_commands = {("无咎子", ".探寻裂缝")}
            actual_cooldown_probe_delay_seconds = 0

            def __init__(self):
                super().__init__()
                self.state = {"avatars": {"无咎子": {}}}
                self.identity_sect_names = {"无咎子": "天星宗"}
                self.sent = []

            def get_avatar_state(self, identity):
                return self.state.setdefault("avatars", {}).setdefault(identity, {})

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command))
                rift_count = sum(1 for _, sent_command in self.sent if sent_command == ".探寻裂缝")
                if command == ".探寻裂缝" and rift_count == 1:
                    return "探寻裂缝成功，发现秘藏，获得【空间碎片】x2。"
                if command == ".探寻裂缝":
                    return "空间裂缝尚未稳定，请在 **4小时1分钟40秒** 后再行探寻。"
                return "司命演算完成。"

        actor = DummyWujiuziRift()
        self.assertTrue(asyncio.run(actor.common_avatar_rift_search_check("无咎子", 12 * 3600)))

        self.assertEqual(
            actor.sent,
            [
                ("无咎子", ".推命 探索"),
                ("无咎子", ".改命 探索"),
                ("无咎子", ".探寻裂缝"),
                ("无咎子", ".探寻裂缝"),
            ],
        )
        remaining = common_seconds_until(actor.get_avatar_state("无咎子")["next_rift_search_time"])
        self.assertGreater(remaining, 4 * 3600)
        self.assertLessEqual(remaining, 4 * 3600 + 2 * 60)
        calibrated_next = actor.get_avatar_state("无咎子")["next_rift_search_time"]
        self.assertFalse(asyncio.run(actor.common_avatar_rift_search_check("无咎子", 12 * 3600)))
        self.assertEqual(actor.get_avatar_state("无咎子")["next_rift_search_time"], calibrated_next)
        self.assertEqual(len(actor.sent), 4)

    def test_non_tianxing_avatar_rift_does_not_send_destiny_prefix(self):
        class DummyStarRift(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.identity_sect_names = {"缘生子": "星宫"}
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command))
                return "你运转全身法力，撕开一道漆黑的空间裂缝，将元婴送入其中探寻机缘。"

        async def fake_sleep(_seconds):
            raise AssertionError("non-Tianxing rift should not sleep for prefix")

        actor = DummyStarRift()
        with patch.object(common_command_features.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.common_avatar_rift_search_check("缘生子", 12 * 3600)))

        self.assertEqual(actor.sent, [("缘生子", ".探寻裂缝")])

    def test_sub_and_xiaohao_keyword_alerts_skip_global_boss_events(self):
        msg = SimpleNamespace(out=False, sender_id=123, mentioned=False, entities=[])
        boss_text = "【天机异象·玄骨考校】 @TitanCreeper 的答案完全正确。"
        normal_text = "【秘境传音】 @TitanCreeper 附近出现异常波动。"

        for cls in (SubCultivator, CultivatorXiaoHao):
            with self.subTest(cls=cls.__name__):
                actor = cls.__new__(cls)
                actor.my_info = None
                actor.keywords = ["天机异象", "玄骨上人", "秘境传音"]
                actor.notify_users = ["titancreeper"]

                self.assertFalse(actor.should_send_keyword_alert(msg, boss_text))
                self.assertTrue(actor.should_send_keyword_alert(msg, normal_text))

    def test_common_avatar_tower_send_does_not_trigger_mulan_support(self):
        class DummyTowerAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return "你成功通关试炼古塔第 7 层。"

        actor = DummyTowerAvatar()
        today = "2026-06-25"

        self.assertTrue(asyncio.run(actor.common_avatar_tower_send("缘生子", today=today)))
        self.assertEqual(
            [(identity, command) for identity, command, _kwargs in actor.sent],
            [("缘生子", ".闯塔")],
        )
        state = actor.get_avatar_state("缘生子")
        self.assertEqual(state["last_tower_date"], today)
        self.assertNotIn("last_mulan_support_date", state)

    def test_avatar_tower_tick_does_not_defer_to_other_overdue_work(self):
        class DummyTowerAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            def daily_one_shot_should_defer(self, identity="主魂", command="", logger=None):
                raise AssertionError("tower must not defer to unrelated overdue work")

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command))
                return "你成功通关试炼古塔第 7 层。"

        actor = DummyTowerAvatar()
        self.assertTrue(asyncio.run(actor.common_avatar_tower_tick("缘生子", min_hour=0)))
        self.assertEqual(actor.sent[0], ("缘生子", ".闯塔"))

    def test_avatar_tower_loop_does_not_defer_to_other_overdue_work(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 11, 23, 59, 50)

        class DummyTowerAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.is_running = True
                self.startup_done = asyncio.Event()
                self.startup_done.set()
                self.pause_event = asyncio.Event()
                self.pause_event.set()
                self._avatar_loop_count = 0

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            def daily_one_shot_should_defer(self, identity="主魂", command="", logger=None):
                raise AssertionError("tower loop must not defer to unrelated overdue work")

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command))
                if command == ".闯塔":
                    self.is_running = False
                return "你成功通关试炼古塔第 7 层。"

        sleeps = []

        async def no_sleep(_seconds):
            sleeps.append(_seconds)
            return None

        actor = DummyTowerAvatar()
        with patch.object(common_command_features, "datetime", FixedDateTime), patch.object(
            common_command_features.asyncio, "sleep", no_sleep
        ), patch.object(common_command_features.random, "randint", return_value=600):
            asyncio.run(actor.run_common_avatar_tower_loop("缘生子", delay_range=(10, 600)))

        self.assertEqual(actor.sent[0], ("缘生子", ".闯塔"))
        self.assertEqual(sleeps[0], 0)

    def test_mulan_support_feedback_family_matches_response(self):
        text = "【慕兰烽烟】你领了【夜袭法士营】奇袭，小胜险还，边境军功 +5。"

        self.assertEqual(
            log_utils.command_response_family(common_command_features.MULAN_SUPPORT_COMMAND),
            "mulan_support",
        )
        self.assertEqual(log_utils.text_response_family(text), "mulan_support")
        self.assertTrue(log_utils.feedback_response_matches_command(
            common_command_features.MULAN_SUPPORT_COMMAND,
            text,
        ))
        self.assertTrue(log_utils.feedback_response_conflicts(".闯塔", text))

    def test_common_avatar_tower_send_can_require_meditation_ready(self):
        class DummyTowerAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            def avatar_meditation_needs_attention(self, avatar):
                return True

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return "你成功通关试炼古塔第 7 层。"

        actor = DummyTowerAvatar()

        self.assertFalse(asyncio.run(actor.common_avatar_tower_send(
            "缘生子",
            today="2026-06-25",
            require_meditation_ready=True,
        )))
        self.assertEqual(actor.sent, [])
        self.assertNotIn("last_tower_date", actor.get_avatar_state("缘生子"))

    def test_avatar_field_training_ignores_meditation_attention(self):
        class DummyFieldAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.state["avatars"]["缘生子"].update({
                    "next_field_training_time": "",
                    "last_field_training_time": "",
                })

            def avatar_meditation_needs_attention(self, avatar):
                return True

            def field_training_plan(self, avatar):
                return field_training_plan_from_features(avatar, {"training_cmd": ".野外历练"})

            def preserve_cooldown_floor(self, *args, **kwargs):
                return ""

            async def wait_for_field_training_settlement(self, resp, avatar):
                return resp

            def response_text(self, resp):
                return str(resp or "")

            def record_identity_field_training_response(self, avatar, text, context=""):
                self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now_str(), 2 * 3600))

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command))
                return "**【野外历练 · 灵机暗藏】**\n获得修为 **+12000**。"

        actor = DummyFieldAvatar()

        wait = asyncio.run(actor.common_avatar_field_training_tick("缘生子"))

        self.assertEqual(wait, 5)
        self.assertEqual(actor.sent, [("缘生子", ".野外历练")])

    def test_avatar_star_wait_ignores_meditation_attention(self):
        actor = DummyAvatarCommon()
        actor.state["avatars"]["缘生子"].update({
            "next_star_collect_time": (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S"),
        })
        actor.avatar_meditation_needs_attention = lambda avatar: True

        self.assertEqual(actor.common_next_avatar_star_wait_seconds("缘生子"), 0)

    def test_common_avatar_tower_insufficient_cultivation_does_not_mark_done_after_failed_retry(self):
        class DummyTowerAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return "修为不足，无法继续闯塔。"

            async def handle_修为不足(self, avatar, retry_func, cooldown_key="", cooldown_hours=2):
                await retry_func()
                return False, "修为仍然不足。"

        actor = DummyTowerAvatar()

        self.assertFalse(asyncio.run(actor.common_avatar_tower_send(
            "缘生子",
            today="2026-06-25",
            handle_insufficient_cultivation=True,
        )))
        self.assertEqual([item[1] for item in actor.sent], [".闯塔", ".闯塔"])
        self.assertNotIn("last_tower_date", actor.get_avatar_state("缘生子"))

    def test_common_avatar_daily_checkin_waits_for_daily_start(self):
        class DummyDailyAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return "点卯成功。"

        actor = DummyDailyAvatar()

        self.assertFalse(asyncio.run(actor.common_avatar_daily_checkin(
            "缘生子",
            daily_start_wait_func=lambda now: 300,
        )))
        self.assertEqual(actor.sent, [])

    def test_common_avatar_daily_checkin_records_done_on_response(self):
        class DummyDailyAvatar(DummyAvatarCommon):
            def __init__(self):
                super().__init__()
                self.sent = []

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                if command == common_command_features.MULAN_SUPPORT_COMMAND:
                    return "支援慕兰奇袭成功，获得灵石x100。"
                return "宗门点卯成功。"

        actor = DummyDailyAvatar()

        self.assertTrue(asyncio.run(actor.common_avatar_daily_checkin(
            "缘生子",
            daily_start_wait_func=lambda now: 0,
        )))
        self.assertEqual(
            [item[1] for item in actor.sent],
            [".宗门点卯", common_command_features.MULAN_SUPPORT_COMMAND],
        )
        self.assertEqual(
            actor.get_avatar_state("缘生子")["last_dianmao_date"],
            datetime.now().strftime("%Y-%m-%d"),
        )
        self.assertEqual(
            actor.get_avatar_state("缘生子")["last_mulan_support_date"],
            datetime.now().strftime("%Y-%m-%d"),
        )

    def test_common_daily_tasks_main_sends_mulan_after_checkin(self):
        class DummyDailyLoop(DummyCommon):
            def __init__(self):
                super().__init__()
                self.state = {"date": "2026-06-24", "done": [], "heart_platform_date": "2026-06-24"}
                self.startup_done = asyncio.Event()
                self.startup_done.set()
                self.is_running = True
                self.sent = []

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append((command, kwargs))
                if command == common_command_features.MULAN_SUPPORT_COMMAND:
                    return "支援慕兰奇袭成功，获得灵石x100。"
                return SimpleNamespace(id=8801)

        actor = DummyDailyLoop()

        async def fake_sleep(seconds):
            if seconds >= 600:
                actor.is_running = False

        async def no_wait_main():
            return None

        with patch.object(common_command_features.asyncio, "sleep", fake_sleep):
            asyncio.run(actor.run_common_daily_tasks_loop(
                lambda now: 0,
                lambda: "07:00",
                [".宗门点卯"],
                pre_loop_func=no_wait_main,
                send_kwargs_func=lambda command: {"return_msg": True},
                mark_done_before_send=True,
                reset_heart_platform_date=True,
            ))

        self.assertEqual(
            [item[0] for item in actor.sent],
            [".宗门点卯", common_command_features.MULAN_SUPPORT_COMMAND],
        )
        self.assertIn(".宗门点卯", actor.state["done"])
        self.assertEqual(actor.state["last_dianmao_msg_id"], 8801)
        self.assertEqual(actor.state["last_dianmao_date"], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(actor.state["last_mulan_support_date"], datetime.now().strftime("%Y-%m-%d"))
        self.assertEqual(actor.state["heart_platform_date"], "")

    def test_common_daily_tasks_success_only_mode_preserves_send_kwargs(self):
        class DummyDailyLoop(DummyCommon):
            def __init__(self):
                super().__init__()
                self.state = {"date": "2026-06-24", "done": []}
                self.startup_done = asyncio.Event()
                self.startup_done.set()
                self.is_running = True
                self.sent = []

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append((command, kwargs))
                if command == common_command_features.MULAN_SUPPORT_COMMAND:
                    return "支援慕兰奇袭成功，获得灵石x100。"
                return SimpleNamespace(id=9901)

        actor = DummyDailyLoop()

        async def fake_sleep(seconds):
            if seconds >= 600:
                actor.is_running = False

        async def no_wait_main():
            return None

        with patch.object(common_command_features.asyncio, "sleep", fake_sleep):
            asyncio.run(actor.run_common_daily_tasks_loop(
                lambda now: 0,
                lambda: "07:15",
                [".宗门点卯"],
                pre_loop_func=no_wait_main,
                send_kwargs_func=lambda command: {
                    "return_sent": True,
                    "delete_after": command != ".宗门点卯",
                },
                mark_done_before_send=False,
            ))

        self.assertEqual(actor.sent[0], (".宗门点卯", {"return_sent": True, "delete_after": False}))
        self.assertEqual(actor.sent[1][0], common_command_features.MULAN_SUPPORT_COMMAND)
        self.assertEqual(actor.state["done"], [".宗门点卯"])
        self.assertEqual(actor.state["last_dianmao_msg_id"], 9901)
        self.assertEqual(actor.state["last_dianmao_date"], datetime.now().strftime("%Y-%m-%d"))

    def test_common_daily_tasks_does_not_support_after_unconfirmed_checkin(self):
        class DummyDailyLoop(DummyCommon):
            def __init__(self):
                super().__init__()
                self.state = {"date": "2026-06-24", "done": []}
                self.startup_done = asyncio.Event()
                self.startup_done.set()
                self.is_running = True
                self.sent = []

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                return None

        actor = DummyDailyLoop()

        async def fake_sleep(seconds):
            if seconds >= 600:
                actor.is_running = False

        with patch.object(common_command_features.asyncio, "sleep", fake_sleep):
            asyncio.run(actor.run_common_daily_tasks_loop(
                lambda now: 0,
                lambda: "07:00",
                [".宗门点卯"],
                send_kwargs_func=lambda command: {"return_msg": True},
                mark_done_before_send=True,
            ))

        self.assertEqual(actor.sent, [".宗门点卯"])
        self.assertEqual(actor.state["last_dianmao_date"], "")
        self.assertNotIn("last_mulan_support_date", actor.state)

    def test_common_record_sect_skill_response_updates_count(self):
        actor = DummyCommon()
        actor.state = {"sect_skill_count": 0}

        self.assertEqual(actor.common_record_sect_skill_response("今日已传功 **2/3**"), "counted")
        self.assertEqual(actor.state["sect_skill_count"], 2)

        self.assertEqual(actor.common_record_sect_skill_response("传功玉简已记录！获得了贡献。"), "counted")
        self.assertEqual(actor.state["sect_skill_count"], 3)

        actor.state["sect_skill_count"] = 1
        self.assertEqual(actor.common_record_sect_skill_response("今日次数不足，明日再来。"), "done")
        self.assertEqual(actor.state["sect_skill_count"], 3)

        actor.state["sect_skill_count"] = 1
        self.assertEqual(actor.common_record_sect_skill_response("传功失败，需回复主魂消息。"), "invalid")
        self.assertEqual(actor.state["sect_skill_count"], 1)

    def test_huanglong_report_parser_extracts_rotation_sect(self):
        actor = DummyCommon()
        text = """
【黄龙山轮值军报】
今日黄龙山前线轮值宗门为【凌霄宫】。
当前符合 结丹初期 及以上且不在黄龙山冷却中的本宗修士：28 人。

轮值宗门弟子可在 14:00 前使用 .报名黄龙山 报名，使用 .黄龙征调 查看名单。
"""

        self.assertEqual(actor.parse_huanglong_rotation_sect(text), "凌霄宫")
        self.assertEqual(actor.parse_huanglong_rotation_sect("今日轮值宗门为【凌霄宫】。"), "")

    def test_huanglong_signup_sends_once_for_matching_sect_in_window(self):
        actor = DummyCommon()
        actor.sect_name = "凌霄宫"
        sent = []

        async def fake_send(command, **kwargs):
            sent.append((command, kwargs))
            return "报名成功"

        actor.send_and_wait_feedback = fake_send
        now_dt = datetime(2026, 7, 2, 12, 30)

        self.assertTrue(asyncio.run(actor.maybe_signup_huanglong_now("凌霄宫", msg_id=1001, now_dt=now_dt)))
        self.assertFalse(asyncio.run(actor.maybe_signup_huanglong_now("凌霄宫", msg_id=1002, now_dt=now_dt)))

        self.assertEqual([item[0] for item in sent], [".报名黄龙山"])
        self.assertEqual(sent[0][1]["max_retries"], 0)
        self.assertEqual(actor.state["huanglong_signup_date"], "2026-07-02")
        self.assertEqual(actor.state["huanglong_signup_sect"], "凌霄宫")
        self.assertEqual(actor.state["huanglong_signup_status"], "responded")

    def test_huanglong_signup_skips_non_matching_sect(self):
        actor = DummyCommon()
        actor.sect_name = "万灵宗"

        async def fake_send(*args, **kwargs):
            raise AssertionError("non-matching sect should not sign up")

        actor.send_and_wait_feedback = fake_send

        self.assertFalse(asyncio.run(actor.maybe_signup_huanglong_now(
            "凌霄宫",
            msg_id=1001,
            now_dt=datetime(2026, 7, 2, 12, 30),
        )))
        self.assertEqual(actor.state["huanglong_signup_date"], "")
        self.assertEqual(actor.state["huanglong_signup_status"], "")

    def test_huanglong_signup_skips_after_window(self):
        actor = DummyCommon()
        actor.sect_name = "凌霄宫"

        async def fake_send(*args, **kwargs):
            raise AssertionError("huanglong signup must not send after 14:00")

        actor.send_and_wait_feedback = fake_send

        self.assertFalse(asyncio.run(actor.maybe_signup_huanglong_now(
            "凌霄宫",
            msg_id=1001,
            now_dt=datetime(2026, 7, 2, 14, 0),
        )))
        self.assertEqual(actor.state["huanglong_signup_date"], "2026-07-02")
        self.assertEqual(actor.state["huanglong_signup_status"], "window_closed")

    def test_huanglong_identity_sect_map_sends_matching_avatars_once(self):
        actor = DummyCommon()
        actor.sect_name = "万灵宗"
        actor.avatars = ["问心子", "素心子", "缘生子"]
        actor.identity_sect_names = {
            "主魂": "万灵宗",
            "问心子": "凌霄宫",
            "素心子": "星宫",
            "缘生子": "太一门",
        }
        sent = []

        async def fake_identity_send(identity, command, **kwargs):
            sent.append((identity, command, kwargs))
            return f"{identity}报名成功"

        actor.send_and_wait_feedback_identity = fake_identity_send
        identities = actor.huanglong_identities_for_sect("星宫")

        self.assertEqual(identities, ["素心子"])
        self.assertEqual(actor.huanglong_identities_for_sect("太一门"), ["缘生子"])
        self.assertTrue(asyncio.run(actor.maybe_signup_huanglong_identities_now(
            "星宫",
            identities,
            msg_id=2001,
            now_dt=datetime(2026, 7, 2, 12, 30),
        )))
        self.assertFalse(asyncio.run(actor.maybe_signup_huanglong_identities_now(
            "星宫",
            identities,
            msg_id=2002,
            now_dt=datetime(2026, 7, 2, 12, 45),
        )))

        self.assertEqual([(item[0], item[1]) for item in sent], [
            ("素心子", ".报名黄龙山"),
        ])
        self.assertTrue(all(item[2]["force_identity_check"] for item in sent))
        self.assertTrue(actor.huanglong_signup_record_matches("2026-07-02", "星宫", "素心子"))
        self.assertFalse(actor.huanglong_signup_record_matches("2026-07-02", "星宫", "缘生子"))

    def test_common_avatar_star_observatory_parser_extracts_remaining(self):
        actor = DummyAvatarCommon()
        text = """
【星宫 · 观星台】
总数: 2座
一号引星盘: 天雷星 - 凝聚中，剩余 1小时
二号引星盘: 空闲
"""

        info = actor.common_parse_avatar_star_observatory(text)

        self.assertTrue(info["valid"])
        self.assertEqual(info["total_count"], 2)
        self.assertEqual(info["empty_count"], 1)
        self.assertEqual(info["occupied_count"], 1)
        self.assertEqual(info["min_remaining"], 3600)
        self.assertEqual(info["stars"], ["天雷星"])

    def test_common_avatar_star_observatory_record_marks_collect_and_appease_due(self):
        class DummyStar(DummyAvatarCommon):
            def parse_avatar_star_observatory(self, text):
                return self.common_parse_avatar_star_observatory(text)

        actor = DummyStar()
        text = """
【星宫 · 观星台】
总数: 1座
一号引星盘: 天雷星 - 精华已成，星光黯淡
"""

        self.assertTrue(actor.common_record_avatar_star_observatory(
            "缘生子",
            text,
            star_target="天雷星",
            pre_appease_lead_seconds=1800,
            status_retry_seconds=600,
        ))
        state = actor.get_avatar_state("缘生子")
        self.assertEqual(state["star_observatory_summary"], "精华已成")
        self.assertEqual(state["next_star_collect_time"], state["next_star_check_time"])
        self.assertEqual(state["next_star_appease_time"], state["next_star_check_time"])
        self.assertFalse(state["star_observatory_needs_refresh"])

    def test_common_avatar_star_guard_backoff_updates_retry_fields(self):
        actor = DummyAvatarCommon()
        actor._last_command_guard_block = {
            "at": time.monotonic(),
            "key": ".观星台",
            "wait": 30,
            "blocked_until": time.monotonic() + 45,
        }

        self.assertTrue(actor.common_apply_avatar_star_guard_backoff(
            "缘生子",
            command=".观星台",
            fields=["next_star_collect_time"],
        ))
        state = actor.get_avatar_state("缘生子")
        self.assertTrue(state["star_observatory_needs_refresh"])
        self.assertEqual(state["next_star_check_time"], state["next_star_collect_time"])
        self.assertGreater(common_seconds_until(state["next_star_check_time"]), 0)

    def test_common_next_avatar_star_wait_seconds_handles_refresh_and_due_times(self):
        class DummyStar(DummyAvatarCommon):
            def avatar_meditation_needs_attention(self, avatar):
                return False

        actor = DummyStar()
        state = actor.get_avatar_state("缘生子")
        state["star_observatory_needs_refresh"] = True
        self.assertEqual(actor.common_next_avatar_star_wait_seconds("缘生子"), 0)

        state.clear()
        state["last_star_observatory_time"] = now_str()
        state["next_star_collect_time"] = add_seconds_str(now_str(), 120)
        state["next_star_attraction_time"] = add_seconds_str(now_str(), 3600)
        wait = actor.common_next_avatar_star_wait_seconds("缘生子")
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, 120)

    def test_xiaohao_taiyi_guide_response_records_success_and_cooldown(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["缘生子"]
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None

        result = actor.record_avatar_taiyi_guide_response(
            "缘生子",
            "太一门引道成功，水行道韵流转。",
        )
        state = actor.get_avatar_state("缘生子")

        self.assertEqual(result, "success")
        self.assertGreater(common_seconds_until(state["next_taiyi_guide_time"]), 11 * 3600)

        result = actor.record_avatar_taiyi_guide_response(
            "缘生子",
            "引道冷却中，请在 **3小时** 后再来引道。",
        )
        state = actor.get_avatar_state("缘生子")

        self.assertEqual(result, "cooldown")
        self.assertGreater(common_seconds_until(state["next_taiyi_guide_time"]), 2 * 3600)
        self.assertLess(common_seconds_until(state["next_taiyi_guide_time"]), 4 * 3600)

    def test_common_avatar_yuanying_rift_wait_seconds(self):
        actor = DummyAvatarCommon()
        state = actor.get_avatar_state("缘生子")

        self.assertEqual(actor.avatar_yuanying_rift_wait_seconds("缘生子"), 60)

        state["yuanying_out_active"] = True
        state["yuanying_out_end_time"] = add_seconds_str(now_str(), 120)
        state["next_rift_search_time"] = add_seconds_str(now_str(), 3600)
        wait = actor.avatar_yuanying_rift_wait_seconds("缘生子")
        self.assertGreaterEqual(wait, 60)
        self.assertLessEqual(wait, 120)

        actor.set_identity_pause("缘生子", 3600, "fixture")
        self.assertEqual(actor.avatar_yuanying_rift_wait_seconds("缘生子"), 600)

    def test_common_fixed_cd_response_records_identity_state(self):
        actor = DummyAvatarCommon()
        self.assertTrue(actor.record_identity_fixed_cd_command_response(
            "缘生子",
            "探寻裂缝成功，发现一处空间裂缝。",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))
        state = actor.get_avatar_state("缘生子")
        self.assertTrue(state["last_rift_search_time"])
        self.assertGreater(common_seconds_until(state["next_rift_search_time"]), 11 * 3600)

    def test_rift_weakness_pauses_identity_until_rebirth_success(self):
        actor = DummyAvatarCommon()
        text = "【不敌败退】肉身破碎，元婴虚弱逃遁，需夺舍重生后方可行动。"

        self.assertTrue(actor.record_identity_fixed_cd_command_response(
            "缘生子",
            text,
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))

        entry = actor.identity_pause_entry("缘生子")
        self.assertTrue(entry.get("wait_for_rebirth"))
        self.assertGreater(actor.identity_pause_seconds("缘生子"), 300 * 24 * 3600)
        self.assertEqual(actor.get_avatar_state("缘生子").get("next_rift_search_time"), "")

        success = "先前肉身陨落的修士，其元婴已成功夺舍重生！"
        self.assertTrue(actor.record_identity_yuanying_recovery_from_text("缘生子", success, source="manual .重生 2"))
        self.assertEqual(actor.identity_pause_seconds("缘生子"), 0)

    def test_common_yuanying_retreat_waits_for_settlement(self):
        class DummyRetreat(DummyCommon):
            yuanying_main_command = ".元婴闭关"

        actor = DummyRetreat()
        self.assertTrue(actor.record_yuanying_out_start_response("开始闭关，持续提供修为。"))
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["next_yuanying_out_time"], "")
        self.assertEqual(actor.state["yuanying_out_end_time"], "")

        self.assertTrue(actor.record_yuanying_out_settlement_response("元婴闭关结算，修为增加。"))
        self.assertFalse(actor.state["yuanying_out_active"])
        self.assertGreaterEqual(common_seconds_until(actor.state["next_yuanying_out_time"]), 0)
        self.assertLessEqual(common_seconds_until(actor.state["next_yuanying_out_time"]), 5)

    def test_common_yuanying_out_start_records_cooldown(self):
        actor = DummyAvatarCommon()
        self.assertTrue(actor.record_yuanying_out_start_response(
            "心念一动，元婴出窍，将在外云游 8小时。",
            identity="缘生子",
        ))
        state = actor.get_avatar_state("缘生子")
        self.assertTrue(state["yuanying_out_active"])
        self.assertGreater(common_seconds_until(state["next_yuanying_out_time"]), 7 * 3600)

    def test_common_main_yuanying_tick_sends_due_command(self):
        class DummyMainYuanying(DummyCommon):
            async def _wait_for_main_identity(self):
                return None

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                return "心念一动，元婴出窍，将在外云游 8小时。"

        actor = DummyMainYuanying()
        actor.sent = []

        wait = asyncio.run(actor.common_main_yuanying_out_tick())

        self.assertEqual(actor.sent, [".元婴出窍"])
        self.assertEqual(wait, 5)
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertGreater(common_seconds_until(actor.state["next_yuanying_out_time"]), 7 * 3600)

    def test_common_main_yuanying_active_wait_is_debug_only(self):
        class DummyMainYuanying(DummyCommon):
            async def _wait_for_main_identity(self):
                return None

        actor = DummyMainYuanying()
        actor.state.update({
            "yuanying_out_active": True,
            "yuanying_out_end_time": add_seconds_str(now_str(), 3600),
        })
        logger = SimpleNamespace(info=Mock(), debug=Mock())
        actor.common_command_logger = lambda: logger

        wait = asyncio.run(actor.common_main_yuanying_out_tick())

        self.assertGreater(wait, 3500)
        logger.info.assert_not_called()
        logger.debug.assert_called_once()

    def test_dashboard_labels_miniapp_and_group_execution_channels(self):
        state = {
            "miniapp_route_active": True,
            "done": [],
            "avatars": {},
        }

        commands = build_command_panels("main", state)[0]["commands"]
        by_command = {item["command"]: item for item in commands}

        self.assertEqual(by_command[".深度闭关"]["execution_channel"], "miniapp")
        self.assertEqual(by_command[".元婴出窍"]["execution_channel"], "miniapp")
        self.assertEqual(by_command[".探寻裂缝"]["execution_channel"], "group")
        self.assertEqual(by_command["miniapp:spirit-beast"]["execution_channel"], "miniapp")

    def test_dashboard_uses_group_marker_when_miniapp_route_is_inactive(self):
        state = {"done": [], "avatars": {}}

        commands = build_command_panels("main", state)[0]["commands"]
        deep = next(item for item in commands if item["command"] == ".深度闭关")

        self.assertEqual(deep["execution_channel"], "group")
        self.assertIn("路由未启用", deep["execution_channel_detail"])

    def test_dashboard_labels_restricted_account_commands_as_miniapp(self):
        state = {
            "restricted_miniapp_active": True,
            "done": [],
            "avatars": {},
        }

        commands = build_command_panels("xiaohao", state)[0]["commands"]
        by_command = {item["command"]: item for item in commands}

        self.assertEqual(by_command[".深度闭关"]["execution_channel"], "miniapp")
        self.assertEqual(by_command[".元婴出窍"]["execution_channel"], "miniapp")
        self.assertIn("受限模式", by_command[".深度闭关"]["execution_channel_detail"])

    def test_common_main_yuanying_retreat_tick_retries_after_settlement(self):
        class DummyRetreatTick(DummyCommon):
            yuanying_main_command = ".元婴闭关"

            async def _wait_for_main_identity(self):
                return None

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                if len(self.sent) == 1:
                    return "元婴闭关结算，修为增加。"
                return "开始闭关，持续提供修为。"

        async def no_sleep(_seconds):
            return None

        actor = DummyRetreatTick()
        actor.sent = []

        with patch("common_command_features.asyncio.sleep", no_sleep):
            wait = asyncio.run(actor.common_main_yuanying_out_tick())

        self.assertEqual(actor.sent, [".元婴闭关", ".元婴闭关"])
        self.assertEqual(wait, 5)
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["next_yuanying_out_time"], "")

    def test_common_main_rift_ignores_foreign_weakness_reply(self):
        class DummyRift(DummyCommon):
            async def _wait_for_main_identity(self):
                return None

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                return SimpleNamespace(
                    text="肉体破碎，元婴虚弱，进入虚弱期。",
                    reply_to=SimpleNamespace(reply_to_msg_id=9999),
                )

            async def stop_for_rift_weakness(self, response, identity="主魂", msg=None):
                self.stopped = True

        actor = DummyRift()
        actor.sent = []
        actor.stopped = False
        actor.last_sent_id = 1234

        wait = asyncio.run(actor.common_main_rift_search_tick(12 * 3600))

        self.assertEqual(actor.sent, [".探寻裂缝"])
        self.assertEqual(wait, 0)
        self.assertFalse(actor.stopped)

    def test_common_main_rift_probes_actual_cooldown_when_configured(self):
        class DummyRift(DummyCommon):
            actual_cooldown_probe_commands = {("主魂", ".探寻裂缝")}
            actual_cooldown_probe_delay_seconds = 0

            async def _wait_for_main_identity(self):
                return None

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append((command, kwargs.get("force_identity_check")))
                if len(self.sent) == 1:
                    return "探寻裂缝成功，发现秘藏，获得【空间碎片】x2。"
                return "空间裂缝尚未稳定，其中的空间风暴仍在肆虐。请在 **4小时1分钟40秒** 后再行探寻。"

        actor = DummyRift()
        actor.sent = []

        wait = asyncio.run(actor.common_main_rift_search_tick(12 * 3600))

        self.assertEqual(actor.sent, [(".探寻裂缝", False), (".探寻裂缝", True)])
        self.assertLess(wait, 5 * 3600)
        remaining = common_seconds_until(actor.state["next_rift_search_time"])
        self.assertGreater(remaining, 4 * 3600)
        self.assertLessEqual(remaining, 4 * 3600 + 2 * 60)
        self.assertTrue(actor.state["last_rift_search_time"])
        self.assertTrue(actor.state["last_rift_search_cooldown_probe_time"])

    def test_sub_main_rift_loop_probes_actual_cooldown_after_success(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.state = {}
        actor.is_running = True
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        actor.actual_cooldown_probe_commands = {("主魂", ".探寻裂缝")}
        actor.save_state = lambda: None
        actor.record_daily_reward_event = lambda *args, **kwargs: True
        probes = []

        async def wait_for_main():
            return None

        async def send(command, **kwargs):
            return SimpleNamespace(
                text="探寻裂缝成功，发现秘藏，获得【空间碎片】x2。",
            )

        async def probe(plan, identity="主魂"):
            probes.append((plan.command, identity))
            actor.is_running = False
            return True

        async def no_sleep(_seconds):
            return None

        actor._wait_for_main_identity = wait_for_main
        actor.send_and_wait_feedback = send
        actor.probe_rift_actual_cooldown = probe

        with patch("sub_cultivator.asyncio.sleep", no_sleep):
            asyncio.run(actor.run_rift_search_loop())

        self.assertEqual(probes, [(".探寻裂缝", "主魂")])

    def test_rift_success_can_record_explicit_shortened_cooldown(self):
        actor = DummyCommon()

        self.assertTrue(actor.record_identity_fixed_cd_command_response(
            "主魂",
            "探寻裂缝成功，发现秘藏，获得【空间碎片】x2。\n"
            "风雷翅灵光流转，下次探寻裂缝冷却缩短为 **4小时1分钟40秒**。",
            ".探寻裂缝",
            "last_rift_search_time",
            "next_rift_search_time",
            12 * 3600,
        ))

        remaining = common_seconds_until(actor.state["next_rift_search_time"])
        self.assertGreater(remaining, 4 * 3600)
        self.assertLessEqual(remaining, 4 * 3600 + 2 * 60)

    def test_common_treasure_touch_response_records_success_and_failure(self):
        actor = DummyCommon()
        actor.treasure_touch_command = ".抚摸法宝 青竹蜂云剑"
        actor._main_confirmed = True

        self.assertTrue(actor.record_treasure_touch_response("器灵微微颤动，与你默契提升。"))
        self.assertTrue(actor.state["last_treasure_touch_time"])
        self.assertGreater(common_seconds_until(actor.state["next_treasure_touch_time"]), 3600)

        self.assertFalse(actor.record_treasure_touch_response("没有这件拥有器灵的法宝。"))
        self.assertFalse(actor._main_confirmed)
        self.assertTrue(actor.state["last_treasure_touch_error"])

    def test_wujiuzi_treasure_touch_uses_avatar_two_hour_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子"]
        actor.avatar_nicknames = {"无咎子": "天星雷总"}
        actor.avatar_features = {
            "无咎子": {"treasure_touch_command": ".抚摸法宝 风雷翅"},
        }
        actor.state = {
            "last_treasure_touch_time": "",
            "next_treasure_touch_time": "",
            "avatars": {"无咎子": {}},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        sent = []

        async def send(identity, command, **kwargs):
            sent.append((identity, command))
            return "风雷翅器灵微微颤动，与你默契提升。"

        actor.send_and_wait_feedback_identity = send

        self.assertTrue(asyncio.run(actor._avatar_treasure_touch_check("无咎子")))
        self.assertEqual(sent, [("无咎子", ".抚摸法宝 风雷翅")])
        avatar_state = actor.state["avatars"]["无咎子"]
        remaining = common_seconds_until(avatar_state["next_treasure_touch_time"])
        self.assertGreater(remaining, 115 * 60)
        self.assertLessEqual(remaining, 2 * 3600)
        self.assertEqual(actor.state["next_treasure_touch_time"], "")

    def test_common_ask_dao_response_records_success_and_cooldown(self):
        actor = DummyCommon()

        with patch.object(common_command_features, "record_daily_reward_event_log", lambda *args, **kwargs: True):
            self.assertTrue(actor.record_ask_dao_response("你于元婴宗问道参悟，获得大道感悟。"))
        self.assertTrue(actor.state["last_ask_dao_time"])
        self.assertGreater(common_seconds_until(actor.state["next_ask_dao_time"]), 11 * 3600)
        events = actor.state.get("daily_reward_events") or []
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["command"], ".问道")
        self.assertEqual(events[0]["identity"], "主魂")
        self.assertEqual(events[0]["rewards"].get("感悟"), 1)
        summary = actor.build_daily_reward_summary_text(datetime.now().strftime("%Y-%m-%d"), markdown=True)
        self.assertIn("问道1", summary)
        self.assertIn("✓问道｜感悟\\+1", summary)

        self.assertTrue(actor.record_ask_dao_response("问道尚在冷却，请在 10分钟 后再试。"))
        self.assertLessEqual(common_seconds_until(actor.state["next_ask_dao_time"]), 10 * 60)

    def test_common_ask_dao_success_can_record_explicit_shortened_cooldown(self):
        actor = DummyCommon()

        with patch.object(common_command_features, "record_daily_reward_event_log", lambda *args, **kwargs: True):
            self.assertTrue(actor.record_ask_dao_response(
                "你消耗了 **1000** 点修为，虔诚地向宗门长老问道。\n"
                "**【问道得宝】**\n"
                "你获得大道感悟。\n"
                "风雷翅灵光流转，下次问道冷却缩短为 **8小时23分钟44秒**。"
            ))

        remaining = common_seconds_until(actor.state["next_ask_dao_time"])
        self.assertGreater(remaining, 8 * 3600)
        self.assertLessEqual(remaining, 8 * 3600 + 24 * 60)
        self.assertTrue(actor.state["last_ask_dao_time"])

    def test_common_ask_dao_tick_sends_due_command(self):
        class DummyAskDao(DummyCommon):
            async def _wait_for_main_identity(self):
                return None

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                return "问道参悟成功，道韵萦绕。"

        actor = DummyAskDao()
        actor.sent = []

        wait = asyncio.run(actor.common_ask_dao_tick())

        self.assertEqual(actor.sent, [".问道"])
        self.assertEqual(wait, 5)
        self.assertTrue(actor.state["last_ask_dao_time"])

    def test_common_ask_dao_tick_probes_actual_cooldown_when_configured(self):
        class DummyAskDao(DummyCommon):
            actual_cooldown_probe_commands = {("主魂", ".问道")}
            actual_cooldown_probe_delay_seconds = 0

            async def _wait_for_main_identity(self):
                return None

            def dashboard_command_paused(self, command, identity="主魂"):
                return False

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                if len(self.sent) == 1:
                    return "你消耗了 **1000** 点修为，虔诚地向宗门长老问道。\n**【问道得宝】**\n你获得大道感悟。"
                return "天机不可频繁窥探，请在 **8小时23分钟44秒** 后再来问道。"

        actor = DummyAskDao()
        actor.sent = []

        wait = asyncio.run(actor.common_ask_dao_tick())

        self.assertEqual(actor.sent, [".问道", ".问道"])
        self.assertEqual(wait, 5)
        remaining = common_seconds_until(actor.state["next_ask_dao_time"])
        self.assertGreater(remaining, 8 * 3600)
        self.assertLessEqual(remaining, 8 * 3600 + 24 * 60)

    def test_fishing_active_round_blocks_switch_until_raise(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}

            def save_state(self):
                pass

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing["active"] = True
        fishing["active_due_at"] = add_seconds_str(now_str(), 30)

        wait = actor.fishing_active_switch_wait("主魂", target_identity="问心子", command=".登天阶")
        self.assertGreaterEqual(wait, 30)
        self.assertEqual(
            actor.fishing_active_switch_wait("主魂", target_identity="主魂", command=".登天阶"),
            0,
        )
        self.assertEqual(
            actor.fishing_active_switch_wait("主魂", target_identity="问心子", command=".提竿"),
            0,
        )

    def test_fishing_overdue_switch_guard_is_disabled(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []
                self.last_sent_id = 100

            def save_state(self):
                pass

            async def _send_and_wait_feedback_raw(self, command, **kwargs):
                self.commands.append(command)
                self.last_sent_id += 1
                return "**【提竿成功】**\n水下灵光一翻，竟是一尾 **【银须灵鲢】**！"

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "active": True,
            "active_due_at": add_seconds_str(now_str(), -5),
            "today_count": 3,
            "daily_limit": 5,
        })

        wait = asyncio.run(actor.fishing_switch_wait_or_raise_due(
            "主魂",
            target_identity="问心子",
            command=".登天阶",
        ))

        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(wait, 0)
        self.assertEqual(actor.commands, [])
        self.assertTrue(fishing["active"])
        self.assertEqual(fishing["today_count"], 3)

    def test_fishing_yields_to_overdue_same_identity_command(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}

            def save_state(self):
                pass

            def fishing_command_is_enabled(self, identity):
                return True

            def identity_pause_seconds(self, identity):
                return 0

            def fishing_other_identity_impending_wait(self, identity):
                return "", -1

            def fishing_impending_wait(self, identity):
                return 0

            async def fishing_ensure_daily_bait(self, identity):
                raise AssertionError("fishing should yield before buying bait")

            async def fishing_try_nest(self, identity):
                raise AssertionError("fishing should yield before nesting")

            async def fishing_start_round(self, identity):
                raise AssertionError("fishing should yield before starting a round")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "rod_owned": True,
            "today_count": 4,
            "daily_limit": 5,
            "next_action_at": "",
            "active": False,
        })

        wait = asyncio.run(actor.fishing_tick("主魂"))

        self.assertEqual(wait, 10)
        self.assertEqual(fishing["last_status"], "yielding")
        self.assertIn("0秒", fishing["last_detail"])

    def test_fishing_ignores_overdue_yuanying_state_for_disabled_avatar(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["素缘子"]
        actor.avatar_features = {"素缘子": {"formation_assist": True}}
        actor.state = {
            "avatars": {
                "素缘子": {
                    "next_yuanying_out_time": "2026-06-24 11:58:06",
                    "yuanying_out_active": True,
                    "yuanying_out_end_time": "2026-06-24 11:58:06",
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": add_seconds_str(now_str(), 4 * 3600),
                }
            },
            "fishing": {},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.custom_command_impending_wait = lambda identity: -1
        actor.concubine_voyage_enabled = lambda identity: False
        actor.concubine_voyage_auto_start_enabled = lambda identity: False

        other_identity, wait = actor.fishing_other_identity_impending_wait("主魂")

        self.assertEqual(other_identity, "")
        self.assertEqual(wait, -1)

    def test_fishing_ignores_heart_platform_daily_ready_before_fallback(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = []
        actor.state = {
            "next_heart_time": "2026-06-26 00:05:00",
            "heart_platform_date": "2026-06-25",
            "done": [".宗门点卯", ".闯塔"],
            "fishing": {},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.custom_command_impending_wait = lambda identity: -1
        actor.concubine_voyage_enabled = lambda identity: False
        actor.concubine_voyage_auto_start_enabled = lambda identity: False
        actor.is_heart_platform_fallback_due = lambda today: False

        self.assertEqual(actor.fishing_impending_wait_for_identity("主魂"), -1)

    def test_xiaohao_impending_ignores_heart_platform_before_fallback(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["问心子"]
        actor.state = {
            "avatars": {
                "问心子": {
                    "next_heart_platform_time": "2026-06-26 00:05:00",
                    "heart_platform_date": "2026-06-25",
                    "last_dianmao_date": datetime.now().strftime("%Y-%m-%d"),
                },
            },
            "fishing": {},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.custom_command_impending_wait = lambda identity: -1
        actor.concubine_voyage_auto_start_enabled = lambda identity: False
        actor.state_time_command_paused = lambda key, identity: False
        actor.is_heart_platform_fallback_due = lambda today: False

        self.assertEqual(actor.fishing_impending_wait_for_identity("问心子"), 999999)

    def test_xiaohao_impending_ignores_expired_force_exit_after_formation(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.state = {"avatars": {"素心子": {}}}
        actor.state_time_command_paused = lambda key, identity: False
        actor.concubine_voyage_auto_start_enabled = lambda identity: False
        actor.dashboard_command_paused = lambda command, identity: False
        force_exit_time = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        active_until = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        wait = actor._state_impending_command_wait({
            "next_force_exit_time": force_exit_time,
            "formation_active_until": active_until,
            "in_deep_meditation": True,
            "deep_meditation_end_time": "",
            "last_dianmao_date": datetime.now().strftime("%Y-%m-%d"),
        }, identity="素心子")

        self.assertEqual(wait, 999999)

    def test_daily_one_shot_defers_behind_priority_due_work(self):
        class DummyDailyPriority(CommonCommandMixin):
            avatars = []

            def __init__(self, state):
                self.state = state

            def identity_pause_seconds(self, identity):
                return 0

            def state_time_command_paused(self, key, identity=""):
                return False

            def dashboard_command_paused(self, command, identity=""):
                return False

        due_at = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor = DummyDailyPriority({"next_field_training_time": due_at})

        self.assertTrue(actor.daily_one_shot_should_defer("主魂", ".宗门点卯"))

        actor = DummyDailyPriority({"done": []})
        self.assertFalse(actor.daily_one_shot_should_defer("主魂", ".宗门点卯"))

    def test_main_impending_does_not_promote_daily_when_priority_due(self):
        actor = Cultivator.__new__(Cultivator)
        due_at = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {"next_field_training_time": due_at}
        actor.avatars = []
        actor.avatar_features = {}
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False
        actor.custom_command_impending_wait = lambda identity: -1

        with patch.object(intelligent_cultivator, "seconds_until_daily_task_start", lambda now: 0):
            wait = actor._state_impending_command_wait({"done": []}, identity="主魂")

        self.assertEqual(wait, -1)

    def test_fishing_parsers_cover_core_flow(self):
        basket = parse_fishing_basket(
            "**【鱼篓】**\n"
            "鱼竿：**青竹钓竿（每日 5 竿）**\n"
            "钓术：**Lv.0 凡竿**（43熟练度）\n"
            "今日竿数：**4/5**\n"
            "当前窝料：**灵草窝**（剩余 2 竿）\n\n"
            "**鱼饵**\n- 妖血饵 x6\n- 灵虫饵 x3\n\n"
            "**鱼获**\n- 青鳞小鲫 x7\n"
        )
        self.assertTrue(basket["matched"])
        self.assertTrue(basket["rod_owned"])
        self.assertEqual(basket["today_count"], 4)
        self.assertEqual(basket["daily_limit"], 5)
        self.assertEqual(basket["current_nest"], "灵草窝")
        self.assertEqual(basket["current_nest_remaining"], 2)
        self.assertEqual(basket["baits"]["灵虫饵"], 3)

        start = parse_fishing_start(
            "**【灵溪垂钓】**\n"
            "你挂上 **【灵米饵】**，抛竿入水，敛息坐定。\n"
            "预计 **32秒** 内会有鱼讯。\n"
            "鱼讯倒计时：**31秒**"
        )
        self.assertEqual(start["status"], "started")
        self.assertEqual(start["bait"], "灵米饵")
        self.assertEqual(start["wait_seconds"], 32)

        buy = parse_buy_bait("**【渔具铺】**\n你购得 **【灵米饵】x20**。")
        self.assertEqual(buy["status"], "success")
        self.assertEqual(buy["count"], 20)

        missing = parse_missing_resources("打窝失败，资源不足：灵米饵x3, 凝血草x5。")
        self.assertEqual(missing, [
            {"name": "灵米饵", "count": 3},
            {"name": "凝血草", "count": 5},
        ])

        buy_missing = parse_buy_bait("鱼饵购买失败，资源不足：凝血草x23。")
        self.assertEqual(buy_missing["status"], "insufficient_resource")
        self.assertEqual(buy_missing["missing_resources"], [{"name": "凝血草", "count": 23}])

        exchange = parse_exchange_response("**兑换成功！**\n你消耗了 **100** 点贡献，获得了【凝血草】x5，已放入你的储物袋。")
        self.assertEqual(exchange["status"], "success")
        self.assertEqual(exchange["material"], "凝血草")
        self.assertEqual(exchange["count"], 5)

        nest = parse_nest_response("打窝失败，资源不足：灵米饵x3, 凝血草x5。")
        self.assertEqual(nest["status"], "missing_resource")
        self.assertEqual(nest["missing_name"], "灵米饵")
        self.assertEqual(nest["missing_count"], 3)
        self.assertEqual(nest["missing_resources"], [
            {"name": "灵米饵", "count": 3},
            {"name": "凝血草", "count": 5},
        ])

        active_nest = parse_nest_response("你已打下【灵草窝】，还可影响 **1** 竿，不可重复叠加。")
        self.assertEqual(active_nest["status"], "already_active")
        self.assertEqual(active_nest["nest"], "灵草窝")
        self.assertEqual(active_nest["remaining"], 1)
        self.assertTrue(log_utils.feedback_response_matches_command(
            ".打窝 灵草窝",
            "你已打下【灵草窝】，还可影响 **1** 竿，不可重复叠加。",
        ))

        rod = parse_rod_response("**【提竿成功】**\n水下灵光一翻，竟是一尾 **【银须灵鲢】**！")
        self.assertEqual(rod["status"], "success")
        self.assertEqual(rod["catch"], "银须灵鲢")

        lucky_rod = parse_rod_response(
            "【提竿成功】\n"
            "@Yidao2250 在 青溪浅滩 猛然提竿，灵线绷成一道银弧。\n"
            "水下灵光一翻，竟是一尾【银须灵鲢】！\n\n"
            "品阶：灵鱼\n"
            "- 伴生机缘：【煞气小刀】x1\n\n"
            "鱼获已入鱼篓，可用 .开鱼 银须灵鲢 查看鱼腹机缘。"
        )
        self.assertEqual(lucky_rod["status"], "success")
        self.assertEqual(lucky_rod["catch"], "银须灵鲢")
        self.assertEqual(lucky_rod["loot"], {"煞气小刀": 1})
        self.assertEqual(parse_fishing_loot_lines("- 伴生机缘：【煞气小刀】x1"), {"煞气小刀": 1})

        listing = parse_trade_listing_response("上架成功，挂单ID：23733。")
        self.assertEqual(listing["status"], "success")
        self.assertEqual(listing["listing_id"], "23733")
        self.assertEqual(parse_trade_purchase_response("购买成功，获得了【青竹钓竿】x1。")["status"], "success")
        self.assertEqual(
            parse_trade_purchase_response("购买失败！你还缺少：【青竹钓竿】x1。")["status"],
            "missing_required_rod",
        )
        self.assertEqual(parse_rod_gift_response("赠送成功！你将【青竹钓竿】x1赠予了对方。")["status"], "success")

    def test_fishing_control_text_is_bare_and_limited(self):
        self.assertEqual(parse_fishing_control_text("钓鱼 灵米饵"), "灵米饵")
        self.assertEqual(parse_fishing_control_text(" 钓鱼   灵虫饵 "), "灵虫饵")
        self.assertEqual(parse_fishing_control_text(".钓鱼 灵米饵"), "")
        self.assertEqual(parse_fishing_control_text("钓鱼 凡饵"), "凡饵")
        self.assertEqual(parse_fishing_control_text("钓鱼 妖血饵"), "妖血饵")
        self.assertEqual(parse_fishing_auto_control_text("全自动钓鱼 灵米饵"), "灵米饵")
        self.assertEqual(parse_fishing_auto_control_text("全自动钓鱼 妖血饵"), "妖血饵")
        self.assertEqual(parse_fishing_auto_control_text(".全自动钓鱼 灵米饵"), "")

    def test_fishing_chat_control_enables_selected_bait(self):
        class DummyFishing(FishingMixin):
            account_key = "xiaohao"

            def __init__(self):
                self.state = {"fishing": {}}
                self.pause_admins = {8325841058}
                self.current_identity = "主魂"
                self.saved = 0

            def save_state(self):
                self.saved += 1

        actor = DummyFishing()
        msg = SimpleNamespace(sender_id=8325841058, out=False, id=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertFalse(asyncio.run(
                    actor.maybe_handle_fishing_control_message(msg, "钓鱼 灵虫饵")
                ))
                self.assertFalse(os.path.exists(controls_path))

        fishing = actor.get_fishing_state("主魂")
        self.assertNotEqual(fishing.get("preferred_bait"), "灵虫饵")
        self.assertEqual(actor.saved, 0)

    def test_fishing_preferred_bait_drives_buy_and_start(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {"preferred_bait": "灵虫饵"}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".买鱼饵 灵虫饵 5":
                    return "**【渔具铺】**\n你购得 **【灵虫饵】x5**。"
                if command == ".钓鱼 灵虫饵":
                    return (
                        "**【灵溪垂钓】**\n"
                        "你挂上 **【灵虫饵】**，抛竿入水，敛息坐定。\n"
                        "预计 **30秒** 内会有鱼讯。"
                    )
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing["last_sync_date"] = datetime.now().strftime("%Y-%m-%d")
        fishing["daily_limit"] = 5
        self.assertTrue(asyncio.run(actor.fishing_ensure_daily_bait("主魂")))
        self.assertTrue(asyncio.run(actor.fishing_start_round("主魂")))
        self.assertEqual(actor.commands, [".买鱼饵 灵虫饵 5", ".钓鱼 灵虫饵"])
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["active_bait"], "灵虫饵")
        self.assertEqual(fishing["baits"]["灵虫饵"], 4)

    def test_fishing_existing_nest_reply_syncs_without_incrementing_count(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".打窝 灵草窝":
                    return "你已打下【灵草窝】，还可影响 **1** 竿，不可重复叠加。"
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing["last_sync_date"] = datetime.now().strftime("%Y-%m-%d")
        fishing["nest_counts"] = {}

        self.assertTrue(asyncio.run(actor.fishing_try_nest("主魂")))
        self.assertEqual(actor.commands, [".打窝 灵草窝"])
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["current_nest"], "灵草窝")
        self.assertEqual(fishing["current_nest_remaining"], 1)
        self.assertEqual(fishing["nest_counts"]["灵草窝"], 1)

    def test_fishing_missing_nest_bait_retry_does_not_skip_nest(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".打窝 灵草窝":
                    return "打窝失败，资源不足：灵米饵x3。"
                if command == ".买鱼饵 灵米饵 3":
                    return ""
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing["nest_counts"] = {}

        self.assertFalse(asyncio.run(actor.fishing_try_nest("主魂")))
        self.assertEqual(actor.commands, [".打窝 灵草窝", ".买鱼饵 灵米饵 3"])
        self.assertNotEqual(
            actor.get_fishing_state("主魂").get("nest_blocked", {}).get("灵草窝"),
            datetime.now().strftime("%Y-%m-%d"),
        )
        self.assertEqual(actor.fishing_next_nest("主魂"), "灵草窝")

    def test_fishing_missing_nest_bait_and_blood_grass_retry_same_nest(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []
                self.attempts = 0

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".打窝 灵草窝":
                    self.attempts += 1
                    if self.attempts == 1:
                        return "打窝失败，资源不足：灵米饵x3, 凝血草x5。"
                    return "**【打窝已成】**\n你在水脉交汇处撒下 **【灵草窝】**，接下来 **4** 竿会受其牵引。"
                if command == ".买鱼饵 灵米饵 3":
                    return "**【渔具铺】**\n你购得 **【灵米饵】x3**。"
                if command == ".兑换 凝血草*5":
                    return "**兑换成功！**\n你消耗了 **100** 点贡献，获得了【凝血草】x5，已放入你的储物袋。"
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        actor.get_fishing_state("主魂")["last_sync_date"] = datetime.now().strftime("%Y-%m-%d")
        self.assertTrue(asyncio.run(actor.fishing_try_nest("主魂")))
        self.assertEqual(actor.commands, [
            ".打窝 灵草窝",
            ".买鱼饵 灵米饵 3",
            ".兑换 凝血草*5",
            ".打窝 灵草窝",
        ])
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["current_nest"], "灵草窝")
        self.assertNotEqual(
            fishing.get("nest_blocked", {}).get("灵草窝"),
            datetime.now().strftime("%Y-%m-%d"),
        )

    def test_fishing_buy_bait_exchanges_blood_grass_then_retries(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []
                self.buy_attempts = 0

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".买鱼饵 灵虫饵 20":
                    self.buy_attempts += 1
                    if self.buy_attempts == 1:
                        return "鱼饵购买失败，资源不足：凝血草x23。"
                    return "**【渔具铺】**\n你购得 **【灵虫饵】x20**。"
                if command == ".兑换 凝血草*23":
                    return "**兑换成功！**\n你消耗了 **460** 点贡献，获得了【凝血草】x23，已放入你的储物袋。"
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        self.assertTrue(asyncio.run(actor.fishing_buy_bait("主魂", "灵虫饵", 20)))
        self.assertEqual(actor.commands, [
            ".买鱼饵 灵虫饵 20",
            ".兑换 凝血草*23",
            ".买鱼饵 灵虫饵 20",
        ])
        self.assertEqual(actor.get_fishing_state("主魂")["baits"]["灵虫饵"], 20)

    def test_fishing_auto_transfer_rod_uses_listing_then_purchase(self):
        class DummyFishing(FishingMixin):
            account_key = "main"
            avatars = ["缘生子"]

            def __init__(self):
                self.state = {"fishing": {}, "avatars": {"缘生子": {}}}
                self.commands = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append((identity, command))
                if identity == "缘生子" and command == ".上架 凝血草 换 青竹钓竿*1":
                    return "上架成功，挂单ID：23733。"
                if identity == "主魂" and command == ".购买 23733":
                    return "购买成功，获得了【青竹钓竿】x1。"
                raise AssertionError(f"unexpected command: {identity} {command}")

        actor = DummyFishing()
        actor.get_fishing_state("主魂")["rod_owned"] = True
        actor.get_fishing_state("缘生子")["rod_owned"] = False

        self.assertTrue(asyncio.run(actor.fishing_auto_transfer_rod("主魂", "缘生子")))
        self.assertEqual(actor.commands, [
            ("缘生子", ".上架 凝血草 换 青竹钓竿*1"),
            ("主魂", ".购买 23733"),
        ])
        self.assertFalse(actor.get_fishing_state("主魂")["rod_owned"])
        self.assertTrue(actor.get_fishing_state("缘生子")["rod_owned"])
        self.assertEqual(actor.get_fishing_auto_state()["rod_holder"], "缘生子")

    def test_fishing_auto_transfer_rod_missing_rod_invalidates_stale_holder(self):
        class DummyFishing(FishingMixin):
            account_key = "xiaohao"
            avatars = ["问心子", "素心子"]

            def __init__(self):
                self.state = {
                    "fishing": {},
                    "fishing_auto": {"rod_holder": "主魂"},
                    "avatars": {"问心子": {}, "素心子": {}},
                }
                self.commands = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append((identity, command))
                if identity == "素心子" and command == ".上架 凝血草 换 青竹钓竿*1":
                    return "上架成功，挂单ID：23842。"
                if identity == "主魂" and command == ".购买 23842":
                    return "购买失败！你还缺少：【青竹钓竿】x1。"
                raise AssertionError(f"unexpected command: {identity} {command}")

        actor = DummyFishing()
        actor.get_fishing_state("主魂")["rod_owned"] = True
        actor.get_fishing_state("问心子")["rod_owned"] = True
        actor.get_fishing_state("素心子")["rod_owned"] = False

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "rod_holder": {"account": "xiaohao", "identity": "主魂"},
                    "transfer": {},
                }, f, ensure_ascii=False)
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertFalse(asyncio.run(actor.fishing_auto_transfer_rod("主魂", "素心子")))
                global_state = fishing_features._load_fishing_auto_global_state()

        self.assertEqual(actor.commands, [
            ("素心子", ".上架 凝血草 换 青竹钓竿*1"),
            ("主魂", ".购买 23842"),
        ])
        self.assertFalse(actor.get_fishing_state("主魂")["rod_owned"])
        self.assertTrue(actor.get_fishing_state("问心子")["rod_owned"])
        self.assertEqual(actor.get_fishing_auto_state()["rod_holder"], "")
        self.assertEqual(global_state["rod_holder"], {})
        self.assertEqual(asyncio.run(actor.fishing_auto_find_rod_holder(scan=False)), "问心子")

    def test_fishing_auto_transfer_rod_to_main_uses_reply_gift(self):
        class FakeClient:
            async def get_messages(self, chat_id, limit=200):
                return [SimpleNamespace(id=9101, sender_id=8219248252)]

        class DummyFishing(FishingMixin):
            account_key = "main"
            avatars = ["缘生子"]
            target_chat_id = -100123456

            def __init__(self):
                self.state = {"fishing": {}, "fishing_auto": {}, "avatars": {"缘生子": {}}}
                self.commands = []
                self.client = FakeClient()

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60, **kwargs):
                self.commands.append((identity, command, kwargs.get("reply_to")))
                if identity == "缘生子" and command == ".赠送 青竹钓竿*1" and kwargs.get("reply_to") == 9101:
                    return "赠送成功！你将【青竹钓竿】x1赠予了对方。"
                raise AssertionError(f"unexpected command: {identity} {command} {kwargs}")

        actor = DummyFishing()
        actor.get_fishing_state("缘生子")["rod_owned"] = True
        actor.get_fishing_state("主魂")["rod_owned"] = False

        self.assertTrue(asyncio.run(actor.fishing_auto_transfer_rod("缘生子", "主魂")))
        self.assertEqual(actor.commands, [("缘生子", ".赠送 青竹钓竿*1", 9101)])
        self.assertFalse(actor.get_fishing_state("缘生子")["rod_owned"])
        self.assertTrue(actor.get_fishing_state("主魂")["rod_owned"])
        self.assertEqual(actor.get_fishing_auto_state()["rod_holder"], "主魂")

    def test_fishing_auto_cross_account_transfer_listing_purchase_and_adopt(self):
        class DummyFishing(FishingMixin):
            def __init__(self, account_key, avatars):
                self.account_key = account_key
                self.avatars = avatars
                self.state = {
                    "fishing": {},
                    "fishing_auto": {},
                    "avatars": {avatar: {} for avatar in avatars},
                }
                self.commands = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append((identity, command))
                if self.account_key == "sub" and identity == "厚土" and command == ".上架 凝血草 换 青竹钓竿*1":
                    return "上架成功，挂单ID：23733。"
                if self.account_key == "main" and identity == "主魂" and command == ".购买 23733":
                    return "购买成功，获得了【青竹钓竿】x1。"
                raise AssertionError(f"unexpected command: {self.account_key} {identity} {command}")

        main = DummyFishing("main", ["无咎子"])
        sub = DummyFishing("sub", ["厚土"])
        main.get_fishing_state("主魂")["rod_owned"] = True
        sub.get_fishing_state("厚土")["rod_owned"] = False
        holder = {"account": "main", "identity": "主魂"}
        target = {"account": "sub", "identity": "厚土"}

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertTrue(asyncio.run(sub.fishing_auto_publish_global_listing(holder, target)))
                transfer = fishing_features._load_fishing_auto_global_state()["transfer"]
                self.assertEqual(transfer["status"], "listed")
                self.assertEqual(transfer["listing_id"], "23733")

                self.assertTrue(asyncio.run(main.fishing_auto_handle_global_purchase()))
                global_state = fishing_features._load_fishing_auto_global_state()
                self.assertEqual(global_state["transfer"]["status"], "purchased")
                self.assertFalse(main.get_fishing_state("主魂")["rod_owned"])

                self.assertTrue(sub.fishing_auto_adopt_purchased_global_rod(target))
                global_state = fishing_features._load_fishing_auto_global_state()
                self.assertEqual(global_state["transfer"], {})
                self.assertEqual(global_state["rod_holder"]["account"], "sub")
                self.assertEqual(global_state["rod_holder"]["identity"], "厚土")
                self.assertTrue(sub.get_fishing_state("厚土")["rod_owned"])

        self.assertEqual(sub.commands, [("厚土", ".上架 凝血草 换 青竹钓竿*1")])
        self.assertEqual(main.commands, [("主魂", ".购买 23733")])

    def test_fishing_auto_cross_account_transfer_to_main_uses_reply_gift(self):
        class FakeClient:
            async def get_messages(self, chat_id, limit=200):
                return [SimpleNamespace(id=9201, sender_id=8615886738)]

        class DummyFishing(FishingMixin):
            def __init__(self, account_key, avatars):
                self.account_key = account_key
                self.avatars = avatars
                self.target_chat_id = -100123456
                self.client = FakeClient()
                self.state = {
                    "fishing": {},
                    "fishing_auto": {},
                    "avatars": {avatar: {} for avatar in avatars},
                }
                self.commands = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60, **kwargs):
                self.commands.append((identity, command, kwargs.get("reply_to")))
                if self.account_key == "main" and identity == "素缘子" and command == ".赠送 青竹钓竿*1":
                    if kwargs.get("reply_to") != 9201:
                        raise AssertionError(f"unexpected reply_to: {kwargs.get('reply_to')}")
                    return "赠送成功！你将【青竹钓竿】x1赠予了对方。"
                raise AssertionError(f"unexpected command: {self.account_key} {identity} {command} {kwargs}")

        main = DummyFishing("main", ["素缘子"])
        sub = DummyFishing("sub", ["厚土"])
        main.get_fishing_state("素缘子")["rod_owned"] = True
        sub.get_fishing_state("主魂")["rod_owned"] = False
        holder = {"account": "main", "identity": "素缘子"}
        target = {"account": "sub", "identity": "主魂"}

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertTrue(asyncio.run(sub.fishing_auto_publish_global_listing(holder, target)))
                transfer = fishing_features._load_fishing_auto_global_state()["transfer"]
                self.assertEqual(transfer["status"], "gift_requested")
                self.assertEqual(transfer["reply_to_msg_id"], 9201)

                self.assertTrue(asyncio.run(main.fishing_auto_handle_global_purchase()))
                global_state = fishing_features._load_fishing_auto_global_state()
                self.assertEqual(global_state["transfer"]["status"], "gifted")
                self.assertFalse(main.get_fishing_state("素缘子")["rod_owned"])
                self.assertEqual(global_state["rod_holder"]["account"], "sub")
                self.assertEqual(global_state["rod_holder"]["identity"], "主魂")

                self.assertTrue(sub.fishing_auto_adopt_purchased_global_rod(target))
                global_state = fishing_features._load_fishing_auto_global_state()
                self.assertEqual(global_state["transfer"], {})
                self.assertTrue(sub.get_fishing_state("主魂")["rod_owned"])

        self.assertEqual(sub.commands, [])
        self.assertEqual(main.commands, [("素缘子", ".赠送 青竹钓竿*1", 9201)])

    def test_fishing_auto_target_lists_directly_when_global_holder_is_remote(self):
        today = datetime.now().strftime("%Y-%m-%d")

        class DummyFishing(FishingMixin):
            account_key = "sub"
            avatars = ["厚土"]

            def __init__(self):
                self.state = {
                    "fishing": {
                        "last_sync_date": today,
                        "today_count": 5,
                        "daily_limit": 5,
                    },
                    "fishing_auto": {"preferred_bait": "灵米饵"},
                    "avatars": {"厚土": {"fishing": {"last_sync_date": today, "today_count": 0, "daily_limit": 5}}},
                }
                self.commands = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            async def fishing_auto_find_rod_holder(self, identities=None, scan=False):
                raise AssertionError("known remote holder should not trigger local rod scan")

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append((identity, command))
                if identity == "厚土" and command == ".上架 凝血草 换 青竹钓竿*1":
                    return "上架成功，挂单ID：23733。"
                raise AssertionError(f"unexpected command: {identity} {command}")

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(controls_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sub": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                }, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "state_main.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "avatars": {
                        "无咎子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "缘生子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "素缘子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                    },
                }, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "state_xiaohao.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "avatars": {
                        "问心子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "素心子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "缘生子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                    },
                }, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": today,
                    "preferred_bait": "灵米饵",
                    "active": {"account": "sub", "identity": "厚土", "key": "sub|厚土"},
                    "rod_holder": {"account": "main", "identity": "素缘子"},
                    "completed": {},
                    "transfer": {},
                }, f, ensure_ascii=False)

            actor = DummyFishing()
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertEqual(asyncio.run(actor.fishing_auto_tick()), 5)
                transfer = fishing_features._load_fishing_auto_global_state()["transfer"]

        self.assertEqual(actor.commands, [("厚土", ".上架 凝血草 换 青竹钓竿*1")])
        self.assertEqual(transfer["status"], "listed")
        self.assertEqual(transfer["listing_id"], "23733")

    def test_fishing_auto_new_day_starts_from_recorded_rod_holder(self):
        today = datetime.now().strftime("%Y-%m-%d")

        class DummyFishing(FishingMixin):
            account_key = "xiaohao"
            avatars = ["问心子", "素心子", "缘生子"]

            def __init__(self):
                self.state = {
                    "fishing": {},
                    "fishing_auto": {"preferred_bait": "灵米饵"},
                    "avatars": {
                        "问心子": {"fishing": {}},
                        "素心子": {"fishing": {}},
                        "缘生子": {"fishing": {"rod_owned": True}},
                    },
                }
                self.ticked = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            def identity_pause_seconds(self, identity):
                return 0

            async def fishing_auto_find_rod_holder(self, identities=None, scan=False):
                raise AssertionError("recorded holder should choose the target before scanning")

            async def fishing_tick(self, identity, ignore_dashboard=False):
                self.ticked.append((identity, ignore_dashboard))
                return 42

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(controls_path, "w", encoding="utf-8") as f:
                json.dump({
                    "main": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                    "sub": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                    "xiaohao": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                }, f, ensure_ascii=False)
            for filename in ("state_main.json", "state_sub.json"):
                with open(os.path.join(tmpdir, filename), "w", encoding="utf-8") as f:
                    json.dump({"fishing": {}, "avatars": {}}, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": "2026-01-01",
                    "preferred_bait": "灵米饵",
                    "active": {"account": "main", "identity": "主魂", "key": "main|主魂"},
                    "rod_holder": {"account": "xiaohao", "identity": "缘生子", "updated_at": "2026-01-01 00:00:00"},
                    "completed": {},
                    "transfer": {},
                }, f, ensure_ascii=False)

            actor = DummyFishing()
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertEqual(asyncio.run(actor.fishing_auto_tick()), 42)
                global_state = fishing_features._load_fishing_auto_global_state()

        self.assertEqual(actor.ticked, [("缘生子", True)])
        self.assertEqual(global_state["date"], today)
        self.assertEqual(global_state["rod_holder"]["account"], "xiaohao")
        self.assertEqual(global_state["rod_holder"]["identity"], "缘生子")
        self.assertEqual(global_state["active"]["account"], "xiaohao")
        self.assertEqual(global_state["active"]["identity"], "缘生子")

    def test_fishing_auto_waits_between_identity_handoffs(self):
        today = datetime.now().strftime("%Y-%m-%d")

        class DummyFishing(FishingMixin):
            account_key = "main"
            avatars = ["无咎子"]

            def __init__(self):
                self.state = {
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "fishing_auto": {"preferred_bait": "灵米饵"},
                    "avatars": {"无咎子": {"fishing": {"rod_owned": True}}},
                }
                self.ticked = []

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

            def identity_pause_seconds(self, identity):
                return 0

            async def fishing_tick(self, identity, ignore_dashboard=False):
                self.ticked.append((identity, ignore_dashboard))
                raise AssertionError("handoff delay should wait before fishing_tick")

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(controls_path, "w", encoding="utf-8") as f:
                json.dump({
                    "main": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                    "sub": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                    "xiaohao": {"主魂": {".全自动钓鱼": {"disabled": False, "bait": "灵米饵"}}},
                }, f, ensure_ascii=False)
            for filename in ("state_sub.json", "state_xiaohao.json"):
                with open(os.path.join(tmpdir, filename), "w", encoding="utf-8") as f:
                    json.dump({
                        "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                        "avatars": {
                            name: {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}}
                            for name in ("厚土", "缘生子", "寻真子", "问心子", "素心子")
                        },
                    }, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": today,
                    "preferred_bait": "灵米饵",
                    "active": {"account": "main", "identity": "主魂", "key": "main|主魂"},
                    "rod_holder": {"account": "main", "identity": "无咎子"},
                    "completed": {},
                    "transfer": {},
                }, f, ensure_ascii=False)

            actor = DummyFishing()
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                wait = asyncio.run(actor.fishing_auto_tick())
                global_state = fishing_features._load_fishing_auto_global_state()

        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, 300)
        self.assertEqual(actor.ticked, [])
        self.assertEqual(global_state["active"]["key"], "main|无咎子")
        self.assertGreater(common_seconds_until(global_state["handoff_not_before"]), 25 * 60)
        self.assertEqual(actor.get_fishing_auto_state()["last_status"], "handoff_wait")

    def test_fishing_auto_rotates_to_next_account_before_same_account_identity(self):
        today = datetime.now().strftime("%Y-%m-%d")

        class DummyFishing(FishingMixin):
            account_key = "main"
            avatars = ["无咎子"]

            def __init__(self):
                self.state = {
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "fishing_auto": {"preferred_bait": "灵米饵"},
                    "avatars": {"无咎子": {"fishing": {"last_sync_date": today, "today_count": 0, "daily_limit": 5}}},
                }

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            for filename in ("state_sub.json", "state_xiaohao.json"):
                with open(os.path.join(tmpdir, filename), "w", encoding="utf-8") as f:
                    json.dump({"fishing": {"last_sync_date": today, "today_count": 0, "daily_limit": 5}, "avatars": {}}, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": today,
                    "preferred_bait": "灵米饵",
                    "active": {"account": "main", "identity": "主魂", "key": "main|主魂"},
                    "rod_holder": {"account": "main", "identity": "无咎子"},
                    "completed": {},
                    "transfer": {},
                }, f, ensure_ascii=False)

            actor = DummyFishing()
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                global_state, snapshot = actor.fishing_auto_update_global_progress(bait="灵米饵")

        self.assertIn("main|无咎子", {item["key"] for item in snapshot["pending"]})
        self.assertEqual(global_state["active"]["key"], "sub|主魂")
        self.assertEqual(global_state["handoff_not_before"], "")
        self.assertEqual(global_state["handoff_from"], {})

    def test_fishing_auto_restores_incomplete_handoff_source(self):
        today = datetime.now().strftime("%Y-%m-%d")

        class DummyFishing(FishingMixin):
            account_key = "main"
            avatars = ["无咎子", "缘生子", "素缘子"]

            def __init__(self):
                self.state = {
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "fishing_auto": {"preferred_bait": "灵米饵"},
                    "avatars": {
                        "无咎子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "缘生子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "素缘子": {"fishing": {"last_sync_date": today, "today_count": 4, "daily_limit": 5}},
                    },
                }

            def get_avatar_state(self, avatar):
                return self.state.setdefault("avatars", {}).setdefault(avatar, {})

            def save_state(self):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(os.path.join(tmpdir, "state_sub.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "avatars": {
                        "厚土": {"fishing": {"last_sync_date": today, "today_count": 0, "daily_limit": 5}},
                        "缘生子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "寻真子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                    },
                }, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "state_xiaohao.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5},
                    "avatars": {
                        "问心子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "素心子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                        "缘生子": {"fishing": {"last_sync_date": today, "today_count": 5, "daily_limit": 5}},
                    },
                }, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": today,
                    "preferred_bait": "灵米饵",
                    "active": {"account": "sub", "identity": "厚土", "key": "sub|厚土"},
                    "rod_holder": {"account": "sub", "identity": "厚土"},
                    "completed": {},
                    "transfer": {},
                    "handoff_not_before": add_seconds_str(now_str(), -60),
                    "handoff_from": {
                        "account": "main",
                        "identity": "素缘子",
                        "key": "main|素缘子",
                        "completed_at": now_str(),
                    },
                }, f, ensure_ascii=False)

            actor = DummyFishing()
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                global_state, snapshot = actor.fishing_auto_update_global_progress(bait="灵米饵")

        self.assertIn("main|素缘子", {item["key"] for item in snapshot["pending"]})
        self.assertEqual(global_state["active"]["key"], "main|素缘子")
        self.assertEqual(global_state["handoff_not_before"], "")
        self.assertEqual(global_state["handoff_from"], {})

    def test_dashboard_can_set_fishing_auto_rod_holder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for account in ("main", "sub", "xiaohao"):
                identities = fishing_features.FISHING_AUTO_ACCOUNT_IDENTITIES[account]
                state = {"fishing": {}, "fishing_auto": {}, "avatars": {}}
                for identity in identities:
                    if identity != "主魂":
                        state["avatars"][identity] = {"fishing": {}}
                with open(os.path.join(tmpdir, f"state_{account}.json"), "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False)

            with patch.object(dashboard_server, "CONFIG_DIR", tmpdir):
                with self.assertRaises(Exception) as raised:
                    asyncio.run(dashboard_server.set_fishing_auto_holder(
                        {"account": "xiaohao", "identity": "缘生子"},
                        username="fixture",
                    ))

        self.assertEqual(getattr(raised.exception, "status_code", None), 410)

    def test_fishing_auto_chat_control_writes_single_global_switch(self):
        class DummyFishing(FishingMixin):
            account_key = "main"
            avatars = ["无咎子"]

            def __init__(self):
                self.state = {"fishing_auto": {}, "fishing": {}, "avatars": {"无咎子": {}}}
                self.saved = 0

            def save_state(self):
                self.saved += 1

        actor = DummyFishing()
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertTrue(actor.fishing_auto_apply_control("灵虫饵", reason="fixture"))
                with open(controls_path, "r", encoding="utf-8") as f:
                    controls = json.load(f)
                for account in ("main", "sub", "xiaohao"):
                    entry = controls[account]["主魂"][".全自动钓鱼"]
                    self.assertFalse(entry["disabled"])
                    self.assertEqual(entry["bait"], "灵虫饵")
                    self.assertNotIn(".全自动钓鱼 灵虫饵", controls[account]["主魂"])
        self.assertEqual(actor.get_fishing_auto_state()["preferred_bait"], "灵虫饵")
        self.assertGreater(actor.saved, 0)

    def test_fishing_auto_internal_send_bypasses_individual_pause(self):
        class DummyFishing(FishingMixin):
            account_key = "xiaohao"
            current_identity = "缘生子"
            topic_id = None
            last_sent_id = None

            def __init__(self):
                self.state = {"fishing_auto": {}, "avatars": {"缘生子": {}}}
                self.sent = []

            def save_state(self):
                pass

            async def send_and_wait_feedback_identity(self, identity, message, **kwargs):
                if not log_utils.command_send_allowed(self, message):
                    return ""
                self.sent.append((identity, message))
                return "【开始垂钓】灵线入水，静候鱼儿上钩。"

            async def send_and_wait_feedback(self, message, **kwargs):
                if not log_utils.command_send_allowed(self, message):
                    return ""
                self.sent.append(("主魂", message))
                return "【开始垂钓】灵线入水，静候鱼儿上钩。"

        actor = DummyFishing()
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            controls = {
                "xiaohao": {
                    "缘生子": {
                        ".钓鱼 灵米饵": {
                            "disabled": True,
                            "command": ".钓鱼 灵米饵",
                            "label": "钓鱼",
                        }
                    }
                }
            }
            with open(controls_path, "w", encoding="utf-8") as f:
                json.dump(controls, f, ensure_ascii=False)
            with patch.object(log_utils, "COMMAND_CONTROL_FILE", controls_path), \
                    patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path):
                self.assertTrue(log_utils.dashboard_command_disabled(actor, ".钓鱼 灵米饵", "缘生子")[0])
                actor._fishing_auto_dashboard_bypass = True
                resp = asyncio.run(actor.send_fishing_command("缘生子", ".钓鱼 灵米饵"))

                self.assertIn("开始垂钓", resp)
                self.assertEqual(actor.sent, [("缘生子", ".钓鱼 灵米饵")])
                self.assertTrue(log_utils.dashboard_command_disabled(actor, ".钓鱼 灵米饵", "缘生子")[0])

    def test_fishing_nest_plan_excludes_yaoxing_and_uses_two_rice_chaff(self):
        actor = type("DummyFishing", (FishingMixin,), {
            "__init__": lambda self: setattr(self, "state", {"fishing": {}}),
            "save_state": lambda self: None,
        })()
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(actor.fishing_next_nest("主魂"), "灵草窝")
        fishing["nest_counts"] = {"灵草窝": 1}
        self.assertEqual(actor.fishing_next_nest("主魂"), "米糠小窝")
        fishing["nest_counts"] = {"灵草窝": 1, "米糠小窝": 1}
        self.assertEqual(actor.fishing_next_nest("主魂"), "米糠小窝")
        fishing["nest_counts"] = {"灵草窝": 1, "米糠小窝": 2}
        self.assertEqual(actor.fishing_next_nest("主魂"), "")

    def test_fishing_unrecognized_start_retries_after_two_minutes(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                return ""

        actor = DummyFishing()
        self.assertFalse(asyncio.run(actor.fishing_start_round("主魂")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(actor.commands, [".钓鱼 灵米饵"])
        self.assertEqual(fishing_features.FISHING_RETRY_SECONDS, 2 * 60)
        self.assertEqual(fishing["last_status"], "start_unrecognized")
        self.assertLessEqual(common_seconds_until(fishing["next_action_at"]), 120)
        self.assertGreater(common_seconds_until(fishing["next_action_at"]), 100)

    def test_fishing_does_not_yield_to_zero_second_impending_command(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.started = False

            def save_state(self):
                pass

            def identity_pause_seconds(self, identity):
                return 0

            def _state_impending_command_wait(self, state, identity="主魂"):
                return 0

            async def fishing_ensure_daily_bait(self, identity):
                return True

            async def fishing_try_nest(self, identity):
                return True

            async def fishing_start_round(self, identity):
                self.started = True
                self.get_fishing_state(identity)["last_status"] = "fishing"
                return True

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "rod_owned": True,
            "today_count": 0,
            "daily_limit": 5,
        })

        asyncio.run(actor.fishing_tick("主魂", ignore_dashboard=True))
        self.assertTrue(actor.started)
        self.assertEqual(actor.get_fishing_state("主魂")["last_status"], "fishing")

    def test_fishing_daily_done_syncs_basket_after_twentieth_rod(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".提竿":
                    return (
                        "**【提竿成功】**\n"
                        "水下灵光一翻，竟是一尾 **【银须灵鲢】**！\n"
                        "- 伴生机缘：【煞气小刀】x1"
                    )
                if command == ".鱼篓":
                    return (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**5/5**\n"
                        "当前窝料：无\n\n"
                        "**鱼饵**\n- 灵米饵 x0\n\n"
                        "**鱼获**\n- 银须灵鲢 x1\n"
                    )
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "today_count": 4,
            "daily_limit": 5,
            "active": True,
            "active_due_at": now_str(),
        })

        self.assertTrue(asyncio.run(actor.fishing_raise_rod("主魂")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(actor.commands, [".提竿", ".鱼篓"])
        self.assertEqual(fishing["today_count"], 5)
        self.assertEqual(fishing["daily_limit"], 5)
        self.assertEqual(fishing["last_status"], "daily_done")
        self.assertEqual(fishing["daily_done_basket_sync_date"], datetime.now().strftime("%Y-%m-%d"))

    def test_fishing_basket_sync_below_limit_does_not_mark_daily_done(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".鱼篓":
                    return (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**4/5**\n"
                        "当前窝料：无\n\n"
                        "**鱼饵**\n暂无\n\n"
                        "**鱼获**\n- 银须灵鲢 x1\n"
                    )
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "today_count": 5,
            "daily_limit": 5,
            "last_status": "daily_done",
            "next_action_at": "2099-01-01 00:05:00",
        })

        self.assertFalse(asyncio.run(actor.fishing_sync_daily_done_basket("主魂")))
        self.assertEqual(actor.commands, [".鱼篓"])
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 4)
        self.assertEqual(fishing["last_status"], "synced")
        self.assertEqual(fishing["daily_done_basket_sync_date"], "")
        self.assertEqual(fishing["next_action_at"], "")

    def test_fishing_basket_sync_does_not_calibrate_daily_catches(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                return (
                    "**【鱼篓】**\n"
                    "青竹钓竿：**已持有**\n"
                    "今日竿数：**5/5**\n"
                    "当前窝料：无\n\n"
                    "**鱼获**\n"
                    "- 青鳞小鲫 x11\n"
                    "- 银须灵鲢 x7\n"
                    "- 赤尾火鲤 x1\n"
                )

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing["last_sync_date"] = datetime.now().strftime("%Y-%m-%d")
        fishing["daily_catches"] = {"青鳞小鲫": 8, "银须灵鲢": 4, "赤尾火鲤": 1}

        self.assertTrue(asyncio.run(actor.fishing_sync_basket("主魂")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 5)
        self.assertEqual(fishing["catches"], {"青鳞小鲫": 11, "银须灵鲢": 7, "赤尾火鲤": 1})
        self.assertEqual(fishing["daily_catches"], {"青鳞小鲫": 8, "银须灵鲢": 4, "赤尾火鲤": 1})

    def test_fishing_basket_sync_keeps_historical_catches_out_of_daily_totals(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.responses = [
                    (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**0/5**\n"
                        "当前窝料：无\n\n"
                        "**鱼获**\n- 青鳞小鲫 x2\n"
                    ),
                    (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**1/5**\n"
                        "当前窝料：无\n\n"
                        "**鱼获**\n- 青鳞小鲫 x3\n"
                    ),
                ]

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                return self.responses.pop(0)

        actor = DummyFishing()
        self.assertTrue(asyncio.run(actor.fishing_sync_basket("主魂")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["catches"], {"青鳞小鲫": 2})
        self.assertEqual(fishing["daily_catches"], {})

        self.assertTrue(asyncio.run(actor.fishing_sync_basket("主魂")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 1)
        self.assertEqual(fishing["catches"], {"青鳞小鲫": 3})
        self.assertEqual(fishing["daily_catches"], {})

    def test_fishing_unrecognized_raise_does_not_increment_count(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}

            def save_state(self):
                pass

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "today_count": 4,
            "daily_limit": 5,
            "active": True,
            "active_due_at": now_str(),
            "current_nest": "米糠小窝",
            "current_nest_remaining": 1,
        })

        self.assertFalse(asyncio.run(actor.fishing_record_rod_response("主魂", "这不是提竿回复")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 4)
        self.assertEqual(fishing["current_nest_remaining"], 1)
        self.assertEqual(fishing["last_status"], "raise_unrecognized")
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), fishing["next_action_at"])

    def test_fishing_rod_response_message_id_dedupes_daily_catches(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}

            def save_state(self):
                pass

        actor = DummyFishing()
        msg = SimpleNamespace(
            id=101,
            text="**【提竿成功】**\n水下灵光一翻，竟是一尾 **【银须灵鲢】**！",
        )
        actor.get_fishing_state("主魂")["active"] = True

        self.assertTrue(asyncio.run(actor.fishing_record_rod_response("主魂", msg, finish_daily=False)))
        self.assertTrue(asyncio.run(actor.fishing_record_rod_response("主魂", msg, finish_daily=False)))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 1)
        self.assertEqual(fishing["daily_catches"], {"银须灵鲢": 1})
        self.assertEqual(fishing["recorded_rod_message_ids"], [101])

    def test_fishing_inactive_rod_response_does_not_increment_count(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}

            def save_state(self):
                pass

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "today_count": 4,
            "daily_limit": 5,
            "active": False,
            "current_nest": "灵草窝",
            "current_nest_remaining": 1,
        })

        self.assertTrue(asyncio.run(actor.fishing_record_rod_response(
            "主魂",
            "**【空竿】**\n浮漂猛地一沉，又迅速归于平静。",
        )))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 4)
        self.assertEqual(fishing["current_nest_remaining"], 1)
        self.assertEqual(fishing["last_status"], "raise_ignored")

    def test_fishing_raise_rod_skips_when_round_already_cleared(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            def fishing_logger(self):
                return None

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                raise AssertionError("inactive fishing round should not send .提竿")

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "today_count": 2,
            "daily_limit": 5,
            "active": False,
            "active_due_at": "",
        })

        self.assertFalse(asyncio.run(actor.fishing_raise_rod("主魂")))
        self.assertEqual(actor.commands, [])

    def test_fishing_duplicate_final_rod_still_runs_daily_done_notice(self):
        class DummyFishing(FishingMixin):
            account_key = "sub"

            def __init__(self):
                self.state = {"avatars": {"厚土": {"fishing": {}}}}
                self.commands = []
                self.config = {"notify_target": "Waaiging"}

            def save_state(self):
                pass

            def get_avatar_state(self, identity):
                return self.state["avatars"].setdefault(identity, {})

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append((identity, command))
                if command == ".鱼篓":
                    return (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**5/5**\n\n"
                        "**鱼饵**\n- 灵米饵 x6\n\n"
                        "**鱼获**\n- 银须灵鲢 x10\n- 青鳞小鲫 x10\n"
                    )
                raise AssertionError(f"unexpected command: {command}")

        async def fake_alert(actor, title, text, logger=None):
            notices.append((title, text))
            return True

        actor = DummyFishing()
        fishing = actor.get_fishing_state("厚土")
        fishing.update({
            "last_sync_date": datetime.now().strftime("%Y-%m-%d"),
            "today_count": 4,
            "daily_limit": 5,
            "active": True,
            "active_due_at": now_str(),
        })
        msg = SimpleNamespace(
            id=202,
            text="**【提竿成功】**\n水下灵光一翻，竟是一尾 **【青鳞小鲫】**！",
        )

        notices = []
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path), \
                    patch.object(fishing_features, "send_text_alert", fake_alert):
                self.assertTrue(asyncio.run(actor.fishing_record_rod_response("厚土", msg, finish_daily=False)))
                self.assertTrue(asyncio.run(actor.fishing_record_rod_response("厚土", msg, finish_daily=True)))
                with open(controls_path, "r", encoding="utf-8") as f:
                    controls = json.load(f)

        fishing = actor.get_fishing_state("厚土")
        self.assertEqual(fishing["today_count"], 5)
        self.assertEqual(fishing["daily_catches"], {"青鳞小鲫": 1})
        self.assertEqual(fishing["recorded_rod_message_ids"], [202])
        self.assertEqual(actor.commands, [("厚土", ".鱼篓")])
        self.assertEqual(len(notices), 1)
        self.assertIn("副号 [厚土] 今日钓鱼已完成 5/5 竿", notices[0][1])
        self.assertIn("今日鱼获：青鳞小鲫 x1", notices[0][1])
        self.assertTrue(controls["sub"]["厚土"][".钓鱼 灵米饵"]["disabled"])

    def test_fishing_daily_done_auto_pauses_dashboard_and_notifies(self):
        self.assertEqual(
            fishing_catch_summary({"青鳞小鲫": 2, "银须灵鲢": 1, "空": 0}),
            "青鳞小鲫 x2、银须灵鲢 x1",
        )

        class DummyFishing(FishingMixin):
            account_key = "xiaohao"

            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []
                self.config = {"notify_target": "Waaiging"}

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".提竿":
                    return (
                        "**【提竿成功】**\n"
                        "水下灵光一翻，竟是一尾 **【银须灵鲢】**！\n"
                        "- 伴生机缘：【煞气小刀】x1"
                    )
                if command == ".鱼篓":
                    return (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**5/5**\n"
                        "当前窝料：无\n\n"
                        "**鱼饵**\n- 灵米饵 x0\n\n"
                        "**鱼获**\n- 银须灵鲢 x1\n"
                    )
                raise AssertionError(f"unexpected command: {command}")

        async def fake_alert(actor, title, text, logger=None):
            notices.append((title, text))
            return True

        actor = DummyFishing()
        fishing = actor.get_fishing_state("主魂")
        fishing.update({
            "today_count": 4,
            "daily_limit": 5,
            "active": True,
            "active_due_at": now_str(),
            "daily_catches": {"青鳞小鲫": 2},
        })

        notices = []
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path), \
                    patch.object(fishing_features, "send_text_alert", fake_alert):
                self.assertTrue(asyncio.run(actor.fishing_raise_rod("主魂")))
                self.assertTrue(asyncio.run(actor.fishing_sync_daily_done_basket("主魂")))
                with open(controls_path, "r", encoding="utf-8") as f:
                    controls = json.load(f)

        identity_controls = controls["xiaohao"]["主魂"]
        self.assertTrue(identity_controls[".钓鱼 灵米饵"]["disabled"])
        self.assertTrue(identity_controls[".钓鱼 灵虫饵"]["disabled"])
        self.assertEqual(actor.commands, [".提竿", ".鱼篓"])
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0][0], "钓鱼完成")
        self.assertIn("已自动暂停 dashboard 指令：.钓鱼 灵米饵", notices[0][1])
        self.assertIn("今日鱼获：青鳞小鲫 x2、银须灵鲢 x1", notices[0][1])
        self.assertIn("今日伴生机缘：煞气小刀 x1", notices[0][1])
        self.assertIn("最后一竿：银须灵鲢", notices[0][1])
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["daily_catches"], {"青鳞小鲫": 2, "银须灵鲢": 1})
        self.assertEqual(fishing["daily_loot"], {"煞气小刀": 1})
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(fishing["daily_done_auto_paused_date"], today)
        self.assertEqual(fishing["daily_done_notified_date"], today)

    def test_fishing_daily_limit_start_response_syncs_basket(self):
        class DummyFishing(FishingMixin):
            def __init__(self):
                self.state = {"fishing": {}}
                self.commands = []

            def save_state(self):
                pass

            async def send_fishing_command(self, identity, command, timeout=60):
                self.commands.append(command)
                if command == ".钓鱼 灵米饵":
                    return "你今日已垂钓 **5/5** 竿，神识已乏，明日再来。"
                if command == ".鱼篓":
                    return (
                        "**【鱼篓】**\n"
                        "青竹钓竿：**已持有**\n"
                        "今日竿数：**5/5**\n"
                        "当前窝料：**灵草窝**（剩余 5 竿）\n\n"
                        "**鱼饵**\n- 灵米饵 x0\n\n"
                        "**鱼获**\n- 银须灵鲢 x1\n"
                    )
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyFishing()
        self.assertTrue(asyncio.run(actor.fishing_start_round("主魂")))
        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(actor.commands, [".钓鱼 灵米饵", ".鱼篓"])
        self.assertEqual(fishing["today_count"], 5)
        self.assertEqual(fishing["last_status"], "daily_done")
        self.assertEqual(fishing["daily_done_basket_sync_date"], datetime.now().strftime("%Y-%m-%d"))

    def test_dashboard_fishing_default_paused_can_be_enabled_explicitly(self):
        self.assertTrue(dashboard_server.command_control_disabled(
            {},
            "xiaohao",
            "主魂",
            ".钓鱼 灵米饵",
            default_disabled=True,
        ))
        self.assertFalse(dashboard_server.command_control_disabled(
            {"xiaohao": {"主魂": {".钓鱼 灵米饵": {"disabled": False}}}},
            "xiaohao",
            "主魂",
            ".钓鱼 灵米饵",
            default_disabled=True,
        ))

    def test_dashboard_hides_fishing_command_rows(self):
        today = datetime.now().strftime("%Y-%m-%d")
        state = {
            "fishing": {
                "preferred_bait": "灵虫饵",
                "last_sync_date": today,
                "today_count": 3,
                "daily_limit": 5,
                "last_status": "enabled",
            }
        }
        commands = {
            command.get("command")
            for panel in build_command_panels("main", state)
            for command in panel.get("commands", [])
        }
        self.assertFalse(any(str(command or "").startswith(".钓鱼") for command in commands))

    def test_dashboard_fishing_auto_summary_allows_bait_selection(self):
        today = datetime.now().strftime("%Y-%m-%d")
        due_at = (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        state = {
            "fishing_auto": {
                "preferred_bait": "灵虫饵",
                "last_sync_date": today,
                "last_status": "running",
                "active_identity": "主魂",
                "rod_holder": "主魂",
            },
            "fishing": {
                "last_sync_date": today,
                "last_status": "fishing",
                "last_detail": "等待提竿",
                "today_count": 3,
                "daily_limit": 5,
                "active": True,
                "active_due_at": due_at,
                "rod_owned": True,
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir, \
                patch.object(dashboard_server, "CONFIG_DIR", tmpdir):
            controls = {
                account: {
                    "主魂": {
                        ".全自动钓鱼": {
                            "disabled": False,
                            "command": ".全自动钓鱼",
                            "label": "全自动钓鱼",
                            "bait": "灵虫饵",
                        }
                    }
                }
                for account in ("main", "sub", "xiaohao")
            }
            with open(os.path.join(tmpdir, "command_controls.json"), "w", encoding="utf-8") as f:
                json.dump(controls, f, ensure_ascii=False)
            with open(os.path.join(tmpdir, "fishing_auto_global.json"), "w", encoding="utf-8") as f:
                json.dump({
                    "date": today,
                    "preferred_bait": "灵虫饵",
                    "active": {"account": "main", "identity": "主魂", "key": "main|主魂"},
                    "rod_holder": {"account": "main", "identity": "主魂"},
                    "completed": {},
                    "transfer": {},
                }, f, ensure_ascii=False)
            summary = dashboard_server.fishing_auto_dashboard_summary({
                "main": state,
                "sub": {},
                "xiaohao": {},
            })
        self.assertTrue(summary["enabled"])
        self.assertEqual(summary["bait"], "灵虫饵")
        self.assertEqual(summary["bait_options"], ["凡饵", "灵虫饵", "灵米饵", "妖血饵"])
        self.assertEqual(summary["current"]["label"], "主号[主魂]")
        self.assertEqual(summary["current"]["today_count"], 3)
        self.assertEqual(summary["rod_holder"], "主号[主魂]")
        self.assertIn("主号[主魂] 钓鱼中", summary["status"])

    def test_fishing_stale_daily_count_has_no_dashboard_row_when_disabled(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = {
            "fishing": {
                "last_sync_date": yesterday,
                "today_count": 5,
                "daily_limit": 5,
                "daily_done_auto_paused_date": today,
                "daily_done_notified_date": today,
                "last_status": "daily_done",
                "last_detail": "今日已垂钓 5/5",
                "current_nest": "米糠小窝",
                "current_nest_remaining": 1,
                "next_action_at": f"{today} 00:05:00",
            }
        }

        commands = {
            command.get("command")
            for panel in build_command_panels("main", state)
            for command in panel.get("commands", [])
        }

        self.assertNotIn(".钓鱼 灵米饵", commands)

    def test_fishing_stale_daily_count_does_not_auto_pause_or_notify(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        class DummyFishing(FishingMixin):
            account_key = "main"

            def __init__(self):
                self.state = {
                    "fishing": {
                        "last_sync_date": yesterday,
                        "today_count": 5,
                        "daily_limit": 5,
                        "daily_done_auto_paused_date": today,
                        "daily_done_notified_date": today,
                        "last_status": "daily_done",
                        "last_detail": "今日已垂钓 5/5",
                    }
                }
                self.config = {}

            def save_state(self):
                pass

        async def fail_alert(*args, **kwargs):
            raise AssertionError("stale daily count must not notify")

        actor = DummyFishing()
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path), \
                    patch.object(fishing_features, "send_text_alert", fail_alert):
                asyncio.run(actor.fishing_finish_daily_done("主魂"))

            self.assertFalse(os.path.exists(controls_path))

        fishing = actor.get_fishing_state("主魂")
        self.assertEqual(fishing["today_count"], 0)
        self.assertEqual(fishing["daily_done_auto_paused_date"], "")
        self.assertEqual(fishing["daily_done_notified_date"], "")

    def test_fishing_gift_target_uses_message_event_cache(self):
        class DummyFishing(FishingMixin):
            account_key = "xiaohao"
            target_chat_id = 1680975844

            def fishing_logger(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "message_events.sqlite3")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE message_events (
                        account TEXT,
                        is_out INTEGER,
                        chat_id INTEGER,
                        msg_id INTEGER,
                        created_at TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO message_events(account,is_out,chat_id,msg_id,created_at) VALUES(?,?,?,?,?)",
                    ("main", 1, -1001680975844, 11299077, "2026-07-02 00:18:50"),
                )
                conn.execute(
                    "INSERT INTO message_events(account,is_out,chat_id,msg_id,created_at) VALUES(?,?,?,?,?)",
                    ("main", 1, -1001680975844, 11303198, "2026-07-02 01:29:31"),
                )
                conn.commit()
            finally:
                conn.close()

            actor = DummyFishing()
            with patch.object(fishing_features, "MESSAGE_EVENTS_DB_FILE", db_path):
                self.assertEqual(actor.fishing_find_recent_account_message_id_from_events("main"), 11303198)

    def test_fishing_lingchong_control_sets_preferred_bait(self):
        class DummyFishing(FishingMixin):
            account_key = "xiaohao"

            def __init__(self):
                self.state = {"fishing": {}}

            def save_state(self):
                pass

        actor = DummyFishing()
        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(controls_path, "w", encoding="utf-8") as f:
                json.dump({
                    "xiaohao": {
                        "主魂": {
                            ".钓鱼 灵虫饵": {
                                "disabled": False,
                                "command": ".钓鱼 灵虫饵",
                            }
                        }
                    }
                }, f, ensure_ascii=False)
            log_utils._COMMAND_CONTROLS_CACHE["mtime"] = None
            log_utils._COMMAND_CONTROLS_CACHE["data"] = {}
            with patch.object(fishing_features, "COMMAND_CONTROL_FILE", controls_path), \
                    patch.object(log_utils, "COMMAND_CONTROL_FILE", controls_path):
                self.assertTrue(actor.fishing_command_is_enabled("主魂"))
                with open(controls_path, "r", encoding="utf-8") as f:
                    controls = json.load(f)
            log_utils._COMMAND_CONTROLS_CACHE["mtime"] = None
            log_utils._COMMAND_CONTROLS_CACHE["data"] = {}

        identity_controls = controls["xiaohao"]["主魂"]
        self.assertNotIn(".钓鱼 灵米饵", identity_controls)
        self.assertEqual(actor.get_fishing_state("主魂")["preferred_bait"], "灵虫饵")

    def test_yinluo_parsers_cover_core_flow(self):
        status = parse_yinluo_status(
            "**【缘生子的阴罗幡】**\n\n"
            "**本命魔兵**: 血煞幡胚\n"
            "**幡体等阶**: 三阶中品\n"
            "**煞气池**: 1800 / 25000 (7%)\n"
            "**主魂流派**: 血煞幡\n"
            "**幡魂总炼化**: 2 缕\n\n"
            "**魂魄储备**:\n"
            " - 凶兽戾魄: 1 缕\n\n"
            "**炼化槽:**\n"
            "**1号槽**: [精华已成] - 凶兽戾魄\n"
            "**2号槽**: [空闲]\n"
            "**3号槽**: [魂力枯竭] ❗\n"
            "**4号槽**: [炼化中] - 妖兽精魄 (剩余: 3小时21分钟9秒)\n"
        )
        self.assertTrue(status["matched"])
        self.assertEqual(status["rank"], "三阶中品")
        self.assertEqual(status["sha_current"], 1800)
        self.assertEqual(status["reserves"][YINLUO_SOUL], 1)
        self.assertEqual(status["slots"][1]["status"], "精华已成")
        self.assertEqual(status["slots"][4]["remaining_seconds"], 12069)

        exhausted = parse_yinluo_status(
            "**【缘生子的阴罗幡】**\n\n"
            "**煞气池**: 800 / 25000 (3%)\n"
            "**幡魂总炼化**: 5 缕\n\n"
            "**魂魄储备**:\n"
            " - 妖兽精魄: 2 缕\n\n"
            "**炼化槽:**\n"
            "**1号槽**: [魂力枯竭] ❗\n"
            "**2号槽**: [空闲]\n"
        )
        self.assertTrue(exhausted["matched"])
        self.assertEqual(exhausted["reserves"]["妖兽精魄"], 2)
        self.assertEqual(exhausted["slots"][1]["status"], "魂力枯竭")
        self.assertEqual(exhausted["slots"][1]["soul"], "")
        self.assertEqual(exhausted["slots"][2]["status"], "空闲")

        blood = parse_yinluo_blood_wash(
            "**【血洗功成】**\n成功捕获了 **2** 缕【妖兽精魄】！"
        )
        self.assertEqual(blood["status"], "success")
        self.assertEqual(blood["souls"]["妖兽精魄"], 2)

        summon_cd = parse_yinluo_summon_shadow("魔域裂隙尚未平复，请在 **7小时56分钟13秒** 后再行召唤。")
        self.assertEqual(summon_cd["status"], "cooldown")
        self.assertEqual(summon_cd["cooldown_seconds"], 28573)
        summon_success = parse_yinluo_summon_shadow("魔影被你成功击溃，消散前留下了一道精纯的【凶兽戾魄】，已被你的阴罗幡吸收！")
        self.assertEqual(summon_success["status"], "success")
        self.assertEqual(summon_success["soul"], YINLUO_SOUL)

        convert_cd = parse_yinluo_convert("化功为煞失败，魔功反噬尚需调息，请在 **1小时2分钟3秒** 后再试。")
        self.assertTrue(convert_cd["matched"])
        self.assertEqual(convert_cd["status"], "cooldown")
        self.assertEqual(convert_cd["cooldown_seconds"], 3723)

        convert_failure = parse_yinluo_convert(
            "【转化失败·反噬】\n"
            "魔功失控，你消耗的 10000 点修为尽数逸散！\n"
            "你受到了【煞气反噬】，15分钟内闭关修炼收益大幅降低！"
        )
        self.assertTrue(convert_failure["matched"])
        self.assertEqual(convert_failure["status"], "failed")
        self.assertEqual(convert_failure["cooldown_seconds"], 3600)

        convert_short_cd = parse_yinluo_convert(
            "你刚施展过此术，经脉尚在恢复，请在 43分钟18秒 后再试。"
        )
        self.assertTrue(convert_short_cd["matched"])
        self.assertEqual(convert_short_cd["status"], "cooldown")
        self.assertEqual(convert_short_cd["cooldown_seconds"], 2598)
        self.assertTrue(log_utils.feedback_response_matches_command(
            YINLUO_CONVERT_COMMAND,
            "你刚施展过此术，经脉尚在恢复，请在 43分钟18秒 后再试。",
        ))
        self.assertFalse(log_utils.feedback_response_matches_command(
            ".每日献祭",
            "你刚施展过此术，经脉尚在恢复，请在 43分钟18秒 后再试。",
        ))

        imprison = parse_yinluo_imprison("一缕【凶兽戾魄】被强行打入1号炼化槽，在煞气的包裹下发出阵阵哀嚎，炼化已开始。")
        self.assertEqual(imprison["status"], "success")
        self.assertEqual(imprison["slot"], 1)
        self.assertEqual(imprison["soul"], YINLUO_SOUL)
        slot_busy = parse_yinluo_imprison("[Avatar: 缘生子]\n此炼化槽正在运转中，无法囚禁新的魂魄。")
        self.assertEqual(slot_busy["status"], "slot_busy")

        appease_done = parse_yinluo_appease("**安抚成功！**\n你消耗了 **50** 点修为，成功安抚了 1 个炼化槽。")
        self.assertEqual(appease_done["status"], "success")
        self.assertEqual(appease_done["count"], 1)
        appease_noop = parse_yinluo_appease("**安抚成功！**\n你消耗了 **0** 点修为，成功安抚了 0 个炼化槽。")
        self.assertEqual(appease_noop["status"], "success")
        self.assertEqual(appease_noop["count"], 0)

    def test_yinluo_tick_ignores_deep_meditation_and_does_not_periodic_sync(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.state["avatars"]["缘生子"].update({
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": add_seconds_str(now_str(), 8 * 3600),
                    "yinluo": {
                        "last_sync_at": "",
                        "next_sync_at": "",
                        "last_daily_sacrifice_date": datetime.now().strftime("%Y-%m-%d"),
                        "next_blood_wash_time": add_seconds_str(now_str(), 3600),
                        "next_summon_shadow_time": "",
                        "reserves": {},
                        "slots": {},
                    },
                })

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return -1

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                if command != ".召唤魔影":
                    raise AssertionError(f"unexpected command: {command}")
                return DummyMessage(301, text="魔影被你成功击溃，消散前留下了一道精纯的【凶兽戾魄】，已被你的阴罗幡吸收！")

        actor = DummyYinluo()
        wait = asyncio.run(actor.yinluo_tick("缘生子"))
        yinluo_state = actor.get_yinluo_state("缘生子")

        self.assertEqual(wait, 5)
        self.assertEqual(actor.sent[0][0], "缘生子")
        self.assertEqual(actor.sent[0][1], ".召唤魔影")
        self.assertTrue(actor.sent[0][2]["return_response_msg"])
        self.assertEqual(yinluo_state["last_status"], "summoned")
        self.assertTrue(yinluo_state["imprison_sync_pending"])
        self.assertNotEqual(yinluo_state["last_status"], "meditation_blocked")

    def test_yinluo_summon_triggers_single_pre_imprison_sync(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "last_daily_sacrifice_date": datetime.now().strftime("%Y-%m-%d"),
                    "next_blood_wash_time": add_seconds_str(now_str(), 3600),
                    "next_summon_shadow_time": "",
                    "reserves": {},
                    "slots": {},
                    "sha_current": 0,
                    "sha_max": 0,
                })

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return -1

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                if command == ".召唤魔影":
                    return DummyMessage(401, text="魔影被你成功击溃，消散前留下了一道精纯的【凶兽戾魄】，已被你的阴罗幡吸收！")
                if command == YINLUO_MASTER_COMMAND:
                    return DummyMessage(
                        402,
                        text=(
                            "**【缘生子的阴罗幡】**\n\n"
                            "**煞气池**: 1800 / 25000 (7%)\n"
                            "**幡魂总炼化**: 2 缕\n\n"
                            "**魂魄储备**:\n"
                            " - 凶兽戾魄: 1 缕\n\n"
                            "**炼化槽:**\n"
                            "**1号槽**: [空闲]\n"
                        ),
                    )
                if command == ".囚禁魂魄 1 凶兽戾魄":
                    return DummyMessage(403, text="一缕【凶兽戾魄】被强行打入1号炼化槽，在煞气的包裹下发出阵阵哀嚎，炼化已开始。")
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyYinluo()
        self.assertEqual(asyncio.run(actor.yinluo_tick("缘生子")), 5)
        actor.get_yinluo_state("缘生子")["next_action_at"] = ""
        self.assertEqual(asyncio.run(actor.yinluo_tick("缘生子")), 5)
        self.assertEqual(asyncio.run(actor.yinluo_tick("缘生子")), 5)

        self.assertEqual(
            [item[1] for item in actor.sent],
            [".召唤魔影", YINLUO_MASTER_COMMAND, ".囚禁魂魄 1 凶兽戾魄"],
        )
        yinluo_state = actor.get_yinluo_state("缘生子")
        self.assertFalse(yinluo_state["imprison_sync_pending"])
        self.assertEqual(yinluo_state["last_status"], "imprisoned")

    def test_yinluo_slot_busy_syncs_banner_before_next_imprison(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "reserves": {YINLUO_SOUL: 1},
                    "sha_current": 1800,
                    "sha_max": 25000,
                    "slots": {
                        1: {"status": "空闲", "soul": "", "remaining_seconds": 0, "remaining_text": "", "due_at": ""},
                    },
                })

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append(command)
                if command == ".囚禁魂魄 1 凶兽戾魄":
                    return DummyMessage(501, text="[Avatar: 缘生子]\n此炼化槽正在运转中，无法囚禁新的魂魄。")
                if command == YINLUO_MASTER_COMMAND:
                    return DummyMessage(
                        502,
                        text=(
                            "**【缘生子的阴罗幡】**\n\n"
                            "**煞气池**: 1800 / 25000 (7%)\n"
                            "**幡魂总炼化**: 2 缕\n\n"
                            "**魂魄储备**:\n"
                            " - 凶兽戾魄: 1 缕\n\n"
                            "**炼化槽:**\n"
                            "**1号槽**: [炼化中] - 妖兽精魄 (剩余: 6小时)\n"
                            "**2号槽**: [空闲]\n"
                        ),
                    )
                if command == ".囚禁魂魄 2 凶兽戾魄":
                    return DummyMessage(503, text="一缕【凶兽戾魄】被强行打入2号炼化槽，在煞气的包裹下发出阵阵哀嚎，炼化已开始。")
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyYinluo()
        self.assertTrue(asyncio.run(actor.yinluo_imprison_fierce_soul("缘生子")))
        self.assertEqual(actor.sent, [".囚禁魂魄 1 凶兽戾魄", YINLUO_MASTER_COMMAND, ".囚禁魂魄 2 凶兽戾魄"])
        yinluo_state = actor.get_yinluo_state("缘生子")
        self.assertEqual(yinluo_state["last_status"], "imprisoned")
        self.assertEqual(yinluo_state["reserves"][YINLUO_SOUL], 0)
        self.assertEqual(yinluo_state["slots"][2]["status"], "炼化中")

    def test_yinluo_convert_failure_cooldown_survives_sha_not_enough_flow(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "reserves": {YINLUO_SOUL: 1},
                    "sha_current": 1000,
                    "sha_max": 25000,
                    "slots": {
                        1: {"status": "空闲", "soul": "", "remaining_seconds": 0, "remaining_text": "", "due_at": ""},
                    },
                })

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append(command)
                if command == ".囚禁魂魄 1 凶兽戾魄":
                    return DummyMessage(601, text="煞气不足，无法囚禁魂魄。")
                if command == YINLUO_CONVERT_COMMAND:
                    return DummyMessage(
                        602,
                        text=(
                            "【转化失败·反噬】\n"
                            "魔功失控，你消耗的 10000 点修为尽数逸散！\n"
                            "你受到了【煞气反噬】，15分钟内闭关修炼收益大幅降低！"
                        ),
                    )
                raise AssertionError(f"unexpected command: {command}")

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return -1

        actor = DummyYinluo()
        self.assertFalse(asyncio.run(actor.yinluo_imprison_fierce_soul("缘生子")))
        self.assertEqual(actor.sent, [".囚禁魂魄 1 凶兽戾魄", YINLUO_CONVERT_COMMAND])

        yinluo_state = actor.get_yinluo_state("缘生子")
        self.assertEqual(yinluo_state["last_status"], "convert_failed")
        self.assertIn("化功为煞失败", yinluo_state["last_detail"])
        self.assertGreaterEqual(common_seconds_until(yinluo_state["next_action_at"]), 3500)

        wait = asyncio.run(actor.yinluo_tick("缘生子"))
        self.assertGreaterEqual(wait, 3500)
        self.assertEqual(actor.sent, [".囚禁魂魄 1 凶兽戾魄", YINLUO_CONVERT_COMMAND])

    def test_yinluo_edited_wait_blocks_regular_identity_commands(self):
        async def scenario():
            class DummyYinluo(DummyAvatarCommon, ConcubineMixin, YinluoMixin):
                def __init__(self):
                    super().__init__()
                    self.active_atomic_task = None
                    self.sent = []
                    self.fetch_started = asyncio.Event()
                    self.release_fetch = asyncio.Event()
                    self.client = SimpleNamespace(get_messages=self.get_messages)
                    self.target_chat_id = -100123

                async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                    self.sent.append(command)
                    return DummyMessage(701, text="你开始运转魔功，试图将 **10000** 点修为凝练为纯粹的煞气...")

                async def get_messages(self, chat_id, ids):
                    self.fetch_started.set()
                    await self.release_fetch.wait()
                    return DummyMessage(ids, text="**【转化成功】**\n你成功将 **10000** 点修为炼化，煞气池增加了 **2160** 点！")

                def time_critical_identity_command(self, command):
                    command = str(command or "").strip()
                    return command == ".观星" or command.startswith(".观星 ")

            actor = DummyYinluo()
            task = asyncio.create_task(
                actor.send_yinluo_command("缘生子", YINLUO_CONVERT_COMMAND, timeout=60, edited_wait=0.01)
            )
            await actor.fetch_started.wait()
            try:
                self.assertTrue(actor.should_wait_for_atomic_task(".元婴出窍"))
                self.assertFalse(actor.should_wait_for_atomic_task(".观星"))
            finally:
                actor.release_fetch.set()
            text = await task
            self.assertIn("转化成功", text)
            self.assertFalse(actor.should_wait_for_atomic_task(".元婴出窍"))

        asyncio.run(scenario())

    def test_yinluo_expired_sync_time_does_not_send_master_command(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "last_sync_at": add_seconds_str(now_str(), -3600),
                    "next_sync_at": add_seconds_str(now_str(), -60),
                    "last_daily_sacrifice_date": datetime.now().strftime("%Y-%m-%d"),
                    "next_blood_wash_time": add_seconds_str(now_str(), 3600),
                    "next_summon_shadow_time": add_seconds_str(now_str(), 3600),
                    "reserves": {},
                    "slots": {},
                })

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return -1

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return DummyMessage(404, text="")

        actor = DummyYinluo()
        wait = asyncio.run(actor.yinluo_tick("缘生子"))
        self.assertEqual(actor.sent, [])
        self.assertGreaterEqual(wait, 3000)

    def test_yinluo_impending_zero_does_not_starve_due_action(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "last_daily_sacrifice_date": "2026-01-01",
                    "next_blood_wash_time": add_seconds_str(now_str(), 3600),
                    "next_summon_shadow_time": add_seconds_str(now_str(), 3600),
                    "reserves": {},
                    "slots": {},
                })

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return 0

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append(command)
                if command == ".每日献祭":
                    return DummyMessage(701, text="煞气池增加了 **500** 点。")
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyYinluo()
        self.assertEqual(asyncio.run(actor.yinluo_tick("缘生子")), 5)
        self.assertEqual(actor.sent, [".每日献祭"])
        self.assertEqual(actor.get_yinluo_state("缘生子")["last_status"], "sacrificed")

    def test_yinluo_expired_refining_slot_syncs_banner(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "last_daily_sacrifice_date": datetime.now().strftime("%Y-%m-%d"),
                    "next_blood_wash_time": add_seconds_str(now_str(), 3600),
                    "next_summon_shadow_time": add_seconds_str(now_str(), 3600),
                    "reserves": {},
                    "slots": {
                        "1": {
                            "status": "炼化中",
                            "soul": YINLUO_SOUL,
                            "remaining_seconds": 0,
                            "remaining_text": "",
                            "due_at": add_seconds_str(now_str(), -60),
                        }
                    },
                })

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return -1

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append(command)
                if command == YINLUO_MASTER_COMMAND:
                    return DummyMessage(
                        702,
                        text=(
                            "**【缘生子的阴罗幡】**\n\n"
                            "**煞气池**: 1800 / 25000 (7%)\n"
                            "**幡魂总炼化**: 2 缕\n\n"
                            "**魂魄储备**:\n"
                            " - 凶兽戾魄: 0 缕\n\n"
                            "**炼化槽:**\n"
                            "**1号槽**: [精华已成] - 凶兽戾魄\n"
                        ),
                    )
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyYinluo()
        self.assertEqual(asyncio.run(actor.yinluo_tick("缘生子")), 5)
        self.assertEqual(actor.sent, [YINLUO_MASTER_COMMAND])
        self.assertEqual(actor.get_yinluo_state("缘生子")["slots"][1]["status"], "精华已成")

    def test_yinluo_stale_next_action_is_recomputed_to_future_trigger(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子").update({
                    "last_status": "yielding",
                    "next_action_at": add_seconds_str(now_str(), -300),
                    "last_daily_sacrifice_date": datetime.now().strftime("%Y-%m-%d"),
                    "next_blood_wash_time": add_seconds_str(now_str(), 3600),
                    "next_summon_shadow_time": add_seconds_str(now_str(), 7200),
                    "reserves": {},
                    "slots": {
                        "1": {
                            "status": "炼化中",
                            "soul": YINLUO_SOUL,
                            "remaining_seconds": 0,
                            "remaining_text": "",
                            "due_at": add_seconds_str(now_str(), 1800),
                        }
                    },
                })

            def identity_pause_seconds(self, identity):
                return 0

            def get_identity_impending_command_wait(self, identity):
                return -1

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append(command)
                raise AssertionError(f"unexpected command: {command}")

        actor = DummyYinluo()
        wait = asyncio.run(actor.yinluo_tick("缘生子"))
        yinluo_state = actor.get_yinluo_state("缘生子")

        self.assertEqual(actor.sent, [])
        self.assertGreaterEqual(wait, 1700)
        self.assertLessEqual(wait, 1900)
        self.assertGreaterEqual(common_seconds_until(yinluo_state["next_action_at"]), 1700)
        self.assertLessEqual(common_seconds_until(yinluo_state["next_action_at"]), 1900)

    def test_yinluo_appease_clears_exhausted_slot_state(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子")["slots"] = {
                    "1": {"status": "魂力枯竭", "soul": "", "remaining_seconds": 0, "remaining_text": "", "due_at": ""}
                }

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return DummyMessage(
                    302,
                    text="[Avatar: 缘生子]\n**安抚成功！**\n你消耗了 **50** 点修为，成功安抚了 1 个炼化槽。",
                )

        actor = DummyYinluo()
        self.assertEqual(actor.yinluo_exhausted_slots("缘生子"), [1])
        self.assertTrue(asyncio.run(actor.yinluo_appease_slot("缘生子", 1)))
        yinluo_state = actor.get_yinluo_state("缘生子")

        self.assertEqual(actor.sent[0][1], ".安抚幡灵 1")
        self.assertEqual(actor.yinluo_exhausted_slots("缘生子"), [])
        self.assertEqual(yinluo_state["slots"]["1"]["status"], "空闲")
        self.assertEqual(yinluo_state["next_sync_at"], "")
        self.assertEqual(yinluo_state["last_status"], "appeased")

    def test_yinluo_appease_noop_suppresses_same_slot_repeat(self):
        class DummyYinluo(DummyAvatarCommon, YinluoMixin):
            def __init__(self):
                super().__init__()
                self.sent = []
                self.get_yinluo_state("缘生子")["slots"] = {
                    "1": {"status": "魂力枯竭", "soul": "", "remaining_seconds": 0, "remaining_text": "", "due_at": ""}
                }

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.sent.append((identity, command, kwargs))
                return DummyMessage(
                    303,
                    text="[Avatar: 缘生子]\n**安抚成功！**\n你消耗了 **0** 点修为，成功安抚了 0 个炼化槽。",
                )

        actor = DummyYinluo()
        self.assertTrue(asyncio.run(actor.yinluo_appease_slot("缘生子", 1)))
        yinluo_state = actor.get_yinluo_state("缘生子")

        self.assertEqual(yinluo_state["last_status"], "appease_noop")
        self.assertEqual(yinluo_state["slots"]["1"]["status"], "空闲")
        self.assertGreater(common_seconds_until(yinluo_state["appease_suppressed_until"]["1"]), 60)

        yinluo_state["slots"]["1"]["status"] = "魂力枯竭"
        self.assertEqual(actor.yinluo_exhausted_slots("缘生子"), [])

    def test_dashboard_main_yuanshengzi_yinluo_commands_enabled_by_default(self):
        state = {
            "avatars": {
                "缘生子": {
                    "yinluo": {
                        "sha_current": 800,
                        "sha_max": 25000,
                        "reserves": {YINLUO_SOUL: 1, "妖兽精魄": 9},
                        "slots": {
                            "1": {"status": "空闲"},
                            "2": {"status": "炼化中", "soul": "妖兽精魄"},
                        },
                    }
                }
            }
        }
        panels = build_command_panels("main", state)
        panel = next(p for p in panels if p.get("identity") == "缘生子")
        rows = {r.get("command"): r for r in panel.get("commands", [])}
        self.assertIn(".我的阴罗幡", rows)
        self.assertIn(".囚禁魂魄 <槽位> 凶兽戾魄", rows)
        self.assertIn(".化功为煞 10000", rows)
        self.assertFalse(rows[".我的阴罗幡"].get("control_disabled"))
        self.assertEqual(rows[".囚禁魂魄 <槽位> 凶兽戾魄"]["control_key"], ".囚禁魂魄 *")
        self.assertNotIn(".囚禁魂魄 <槽位> 妖兽精魄", rows)

    def test_dashboard_removes_tower_and_keeps_checkin_support(self):
        rows = {
            row["command"]: row
            for row in dashboard_server.xiaohao_avatar_commands("问心子", {"last_tower_date": "2026-07-06"})
        }
        self.assertNotIn(".闯塔", rows)
        self.assertIn(common_command_features.MULAN_SUPPORT_COMMAND, rows)
        self.assertEqual(
            rows[common_command_features.MULAN_SUPPORT_COMMAND]["detail"],
            "随宗门点卯执行",
        )

    def test_dashboard_sub_main_yuanying_retreat_active_unknown_is_not_due(self):
        state = {
            "yuanying_out_active": True,
            "yuanying_out_end_time": "",
            "next_yuanying_out_time": "",
            "last_yuanying_return_time": "2026-06-26 20:58:24",
            "avatars": {},
        }

        panels = build_command_panels("sub", state)
        panel = next(p for p in panels if p.get("identity") == "主魂")
        rows = {r.get("command"): r for r in panel.get("commands", [])}
        row = rows[".元婴闭关"]

        self.assertEqual(row["status"], "闭关中")
        self.assertEqual(row["tone"], "active")
        self.assertEqual(row["remaining"], "等结算")
        self.assertNotIn("next_seconds", row)

    def test_clear_history_command_is_admin_plain_c_only(self):
        actor = SimpleNamespace(
            target_chat_id=-100123456,
            pause_admins={888},
            config={"monitor": {"watch_bot": "hantianz_bot"}},
        )
        msg = DummyMessage(1, chat_id=-100123456, text="c")

        self.assertTrue(log_utils.is_clear_history_command(actor, msg, "c", sender=None))
        self.assertTrue(log_utils.is_clear_history_command(actor, msg, " C ", sender=None))
        self.assertFalse(log_utils.is_clear_history_command(actor, msg, ".c", sender=None))

        msg.sender_id = 777
        self.assertFalse(log_utils.is_clear_history_command(actor, msg, "c", sender=None))

    def test_pause_admin_accepts_current_account_and_sender_variants(self):
        actor = SimpleNamespace(
            my_info=SimpleNamespace(id=42),
            pause_admins={888, -1003999815554},
            _avatar_chat_ids={"-1003658665113": "问心子"},
        )

        self.assertTrue(log_utils.sender_is_pause_admin(actor, SimpleNamespace(sender_id=42, out=False)))
        self.assertTrue(log_utils.sender_is_pause_admin(actor, SimpleNamespace(sender_id=777, out=True)))
        self.assertTrue(log_utils.sender_is_pause_admin(actor, SimpleNamespace(sender_id=3999815554, out=False)))
        self.assertTrue(log_utils.sender_is_pause_admin(actor, SimpleNamespace(sender_id=-1003658665113, out=False)))
        self.assertFalse(log_utils.sender_is_pause_admin(actor, SimpleNamespace(sender_id=123, out=False)))

    def test_pause_admin_is_not_an_account_outgoing_sender(self):
        actor = SimpleNamespace(
            my_info=SimpleNamespace(id=42),
            pause_admins={8219248252},
            _avatar_chat_ids={"-1003658665113": "问心子"},
        )

        admin_msg = SimpleNamespace(sender_id=8219248252, out=False)

        self.assertTrue(log_utils.sender_is_pause_admin(actor, admin_msg))
        self.assertFalse(log_utils._is_own_outgoing_sender(actor, admin_msg))

    def test_pause_admin_command_does_not_pollute_manual_ledger_or_state(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            state={},
            target_chat_id=-100123456,
            current_identity="主魂",
            my_info=SimpleNamespace(id=42),
            pause_admins={8219248252},
            _avatar_chat_ids={},
            _script_sent_message_ids=set(),
        )
        admin_command = DummyMessage(51001, text=".共历心劫")
        admin_command.sender_id = 8219248252
        bot_reply = DummyMessage(
            51002,
            text="共历心劫成功，冷却10小时。",
            reply_to_msg_id=admin_command.id,
        )

        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = os.path.join(tmpdir, "message_events.sqlite3")
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

                self.assertFalse(log_utils.log_manual_outgoing_if_needed(
                    actor, admin_command, text=admin_command.text
                ))
                self.assertFalse(asyncio.run(log_utils.record_manual_command_reply_state_if_needed(
                    actor,
                    bot_reply,
                    text=bot_reply.text,
                    sender=SimpleNamespace(username="fanrenxiuxian_bot"),
                )))

                self.assertNotIn("next_heart_trial_time", actor.state)
                conn = sqlite3.connect(log_utils.MESSAGE_EVENTS_DB_FILE)
                try:
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM command_ledger"
                    ).fetchone()[0], 0)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM daily_reward_events"
                    ).fetchone()[0], 0)
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM message_events WHERE direction='manual_out'"
                    ).fetchone()[0], 0)
                finally:
                    conn.close()
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_reward_log_rejects_unsupported_test_account(self):
        actor = SimpleNamespace(account_key="DummyReward")
        event = {
            "date": "2026-07-18",
            "time": "12:00:00",
            "identity": "主魂",
            "command": ".野外历练",
            "clean": "历练成功，获得修为100。",
        }

        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = os.path.join(tmpdir, "message_events.sqlite3")
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

                self.assertFalse(log_utils.record_daily_reward_event_log(actor, event))
                self.assertFalse(os.path.exists(log_utils.MESSAGE_EVENTS_DB_FILE))
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_clear_actor_command_history_deletes_only_old_dot_commands(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=40)
        new = datetime.now(timezone.utc) - timedelta(minutes=5)
        messages = [
            SimpleNamespace(id=10, out=True, raw_text=".登天阶", text=".登天阶", date=old, reply_to=None),
            SimpleNamespace(id=11, out=True, raw_text="闲聊", text="闲聊", date=old, reply_to=None),
            SimpleNamespace(id=12, out=True, raw_text=".闭关修炼", text=".闭关修炼", date=new, reply_to=None),
            SimpleNamespace(id=13, out=False, raw_text=".别人指令", text=".别人指令", date=old, reply_to=None),
        ]
        actor = SimpleNamespace(
            client=FakeClearClient(messages),
            target_chat_id=-100123456,
            topic_id=None,
        )

        result = asyncio.run(log_utils.clear_actor_command_history(actor, older_than_minutes=35))

        self.assertEqual(actor.client.deleted, [10])
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["deleted"], 1)

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

    def test_star_gazing_final_report_title_is_enough_all_accounts(self):
        text = "**【天机阁快报 - 心魔大劫】**\n本轮天机已定。"

        cases = [
            (Cultivator, "素缘子"),
            (SubCultivator, "厚土"),
            (CultivatorXiaoHao, "素心子"),
        ]
        for cls, avatar in cases:
            with self.subTest(cls=cls.__name__):
                actor = cls.__new__(cls)
                actor.avatars = [avatar]
                manifest = actor.current_star_report_manifest_dt()
                manifest_key = manifest.strftime("%Y-%m-%d %H:%M:%S")
                actor.state = {
                    "pending_star_gazing_manifest_time": manifest_key,
                    "star_gazing_claimed_manifest_time": manifest_key,
                    "star_gazing_claimed_avatar": avatar,
                    "avatars": {
                        avatar: {
                            "pending_star_gazing_target_time": manifest_key,
                            "next_star_gazing_time": manifest_key,
                        }
                    },
                }
                actor.save_state = lambda: None
                actor.star_shift_task = None
                actor.star_gazing_task = None

                self.assertTrue(actor.record_star_gazing_final_report_if_needed(
                    DummyMessage(7203),
                    text,
                    source="fixture",
                ))
                self.assertEqual(actor.state["last_star_gazing_report_manifest_time"], manifest_key)
                self.assertEqual(actor.state["pending_star_gazing_manifest_time"], "")
                self.assertEqual(actor.state["star_gazing_claimed_avatar"], "")

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

    def test_star_gazing_shift_time_uses_layered_windows_all_accounts(self):
        target = datetime(2026, 6, 15, 12, 0, 0)

        cases = (
            (intelligent_cultivator, -3, 2),
            (sub_cultivator, 3, 6),
            (cultivator_xiaohao, 6, 28),
        )
        for module, min_delay, max_delay in cases:
            with self.subTest(module=module.__name__):
                for _ in range(20):
                    shift_dt = module.star_gazing_shift_dt(target)
                    self.assertGreaterEqual(shift_dt, target + timedelta(seconds=min_delay))
                    self.assertLessEqual(shift_dt, target + timedelta(seconds=max_delay))

    def test_star_shift_predictor_defaults_without_history(self):
        target = datetime(2026, 6, 26, 15, 0, 0)
        now = datetime(2026, 6, 26, 14, 59, 0)
        missing_path = os.path.join(tempfile.gettempdir(), "missing-star-gazing-events.jsonl")

        min_delay, max_delay, reason = star_gazing_collector.predict_star_shift_delay_range(
            target,
            fate_type="Good - 星辰异象",
            now=now,
            history_file=missing_path,
        )

        self.assertEqual((min_delay, max_delay), (21, 24))
        self.assertIn("default", reason)

    def test_star_shift_predictor_fixed_profiles_ignore_history(self):
        target = datetime(2026, 6, 26, 15, 0, 0)
        now = datetime(2026, 6, 26, 14, 59, 0)

        for profile, expected in (("early", (-3, 2)), ("middle", (3, 6))):
            with self.subTest(profile=profile):
                min_delay, max_delay, reason = star_gazing_collector.predict_star_shift_delay_range(
                    target,
                    fate_type="Good - 星辰异象",
                    now=now,
                    history_file=os.path.join(tempfile.gettempdir(), "missing-star-gazing-events.jsonl"),
                    shift_profile=profile,
                )

                self.assertEqual((min_delay, max_delay), expected)
                self.assertIn(profile, reason)

    def test_star_shift_predictor_uses_recent_type_history(self):
        target = datetime(2026, 6, 26, 15, 0, 0)
        now = datetime(2026, 6, 26, 14, 59, 0)
        records = []
        for idx, offset in enumerate([33, 34, 35, 36, 37], 1):
            records.append({
                "event_kind": "news",
                "final_news_offset_seconds": offset,
                "message_time": f"2026-06-2{idx} 12:00:{offset:02d}",
                "target_manifest_time": f"2026-06-2{idx} 12:00:00",
                "message_id": idx,
                "text_hash": f"type-{idx}",
                "news_title": "星辰异象",
            })
        for idx, offset in enumerate([12, 13, 14, 15, 16], 100):
            records.append({
                "event_kind": "news",
                "final_news_offset_seconds": offset,
                "message_time": f"2026-06-2{idx - 99} 09:00:{offset:02d}",
                "target_manifest_time": f"2026-06-2{idx - 99} 09:00:00",
                "message_id": idx,
                "text_hash": f"other-{idx}",
                "news_title": "古修洞府现世",
            })

        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
            history_path = fh.name
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            min_delay, max_delay, reason = star_gazing_collector.predict_star_shift_delay_range(
                target,
                fate_type="Good - 星辰异象",
                now=now,
                history_file=history_path,
            )
        finally:
            os.remove(history_path)

        self.assertEqual((min_delay, max_delay), (23, 25))
        self.assertIn("recent type 星辰异象", reason)

    def test_star_shift_attempt_feedback_marks_avatar_and_clears_pending(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["缘生子"]
        actor.avatar_usernames = {"adai925": "缘生子"}
        actor.state = {
            "avatars": {"缘生子": {"pending_star_gazing_target_time": now_str()}},
            "pending_star_gazing_manifest_time": now_str(),
            "pending_star_gazing_fate_type": "Good - 五彩缤纷",
            "star_gazing_claimed_avatar": "缘生子",
            "star_gazing_claimed_manifest_time": now_str(),
        }
        actor.feedback_commands = {8001: ".改换星移 @TitanCreeper"}
        actor.feedback_identities = {8001: "缘生子"}
        actor.command_avatar_map = {8001: "缘生子"}
        actor.save_state = lambda: None
        actor.get_avatar_state = lambda name: actor.state["avatars"].setdefault(name, {})
        actor.set_avatar_state = lambda name, key, value: actor.state["avatars"].setdefault(name, {}).__setitem__(key, value)

        msg = DummyMessage(
            8002,
            text="[Avatar: 缘生子]\n你开始消耗 **33558** 点修为，尝试扭转因果...\n（成功率: **50%**）",
            reply_to_msg_id=8001,
        )

        self.assertTrue(actor.record_star_shift_attempt_if_needed(msg, msg.text, source="fixture"))
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(actor.state["avatars"]["缘生子"]["last_star_shift_date"], today)
        self.assertEqual(actor.state["avatars"]["缘生子"]["pending_star_gazing_target_time"], "")
        self.assertEqual(actor.state["star_gazing_claimed_avatar"], "")

    def test_star_shift_success_broadcast_marks_claimed_avatar(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_usernames = {"lvdoumiao": "缘生子"}
        actor.state = {
            "avatars": {"缘生子": {}},
            "pending_star_shift_target_time": now_str(),
            "pending_star_shift_msg_id": 9001,
            "pending_star_gazing_manifest_time": now_str(),
            "star_gazing_claimed_avatar": "缘生子",
            "star_gazing_claimed_manifest_time": now_str(),
        }
        actor.feedback_commands = {}
        actor.feedback_identities = {}
        actor.command_avatar_map = {}
        actor.save_state = lambda: None
        actor.get_avatar_state = lambda name: actor.state["avatars"].setdefault(name, {})
        actor.set_avatar_state = lambda name, key, value: actor.state["avatars"].setdefault(name, {}).__setitem__(key, value)

        text = """
[Avatar: 缘生子]
**【天机异动】**
星盘光芒大作！【星宫】弟子 @Lvdoumiao 强行施展【改换星移】之术，竟成功扭转了天机！

原本将降临于 @zedwang125 身上的**【Good - 五彩缤纷】**，现已改道，将由 **@Gamling33** 承受！
"""

        self.assertTrue(actor.record_star_shift_attempt_if_needed(DummyMessage(9002, text=text), text, source="fixture"))
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(actor.state["avatars"]["缘生子"]["last_star_shift_date"], today)
        self.assertEqual(actor.state["pending_star_shift_target_time"], "")
        self.assertEqual(actor.state["star_gazing_claimed_avatar"], "")

    def test_bad_manifest_after_boundary_clears_previous_round_pending_all_accounts(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 18, 3, 0, 10)
                return value.replace(tzinfo=tz) if tz else value

        class PendingTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        async def run_main_like(cls, module, avatar):
            actor = cls.__new__(cls)
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            actor.star_gazing_task = PendingTask()
            with patch.object(module, "datetime", FixedDatetime):
                previous_manifest = datetime(2026, 6, 18, 3, 0, 0)
                previous_key = previous_manifest.strftime("%Y-%m-%d %H:%M:%S")
                actor.state = {
                    "pending_star_gazing_date": "2026-06-18",
                    "pending_star_gazing_target_time": "2026-06-18 02:59:00",
                    "pending_star_gazing_scheduled_time": "2026-06-18 02:59:00",
                    "pending_star_gazing_manifest_time": previous_key,
                    "pending_star_gazing_fate_type": "Good - 地磁暴动",
                    "star_gazing_claimed_manifest_time": previous_key,
                    "star_gazing_claimed_avatar": avatar,
                    "next_star_gazing_time": "2026-06-18 02:59:00",
                }
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7307),
                    """**【星盘显化】**
**下一次天道演化将是**: **【Bad - 心魔大劫】**
**当前天命所归**: **@bar**
""",
                    SimpleNamespace(username="hantianzzzzzz_bot"),
                )
            return handled, actor.state, actor.star_gazing_task.cancelled

        async def run_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.avatars = ["素心子", "缘生子"]
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                previous_manifest = datetime(2026, 6, 18, 3, 0, 0)
                previous_key = previous_manifest.strftime("%Y-%m-%d %H:%M:%S")
                actor.state = {
                    "pending_star_gazing_manifest_time": previous_key,
                    "pending_star_gazing_fate_type": "Good - 地磁暴动",
                    "star_gazing_claimed_manifest_time": previous_key,
                    "star_gazing_claimed_avatar": "素心子",
                    "avatars": {
                        "素心子": {
                            "pending_star_gazing_date": "2026-06-18",
                            "pending_star_gazing_target_time": "2026-06-18 02:59:00",
                            "next_star_gazing_time": "2026-06-18 02:59:00",
                        }
                    },
                }
                await actor.avatar_handle_star_gazing_opportunity(
                    None,
                    DummyMessage(7308),
                    """**【星盘显化】**
**下一次天道演化将是**: **【Bad - 心魔大劫】**
**当前天命所归**: **@bar**
""",
                    SimpleNamespace(username="hantianzzzzzz_bot"),
                )
            return actor.state

        for cls, module, avatar in (
            (Cultivator, intelligent_cultivator, "素缘子"),
            (SubCultivator, sub_cultivator, "厚土"),
        ):
            with self.subTest(cls=cls.__name__):
                handled, state, cancelled = asyncio.run(run_main_like(cls, module, avatar))
                self.assertTrue(handled)
                self.assertTrue(cancelled)
                self.assertEqual(state["pending_star_gazing_manifest_time"], "")
                self.assertEqual(state["star_gazing_claimed_avatar"], "")

        xiaohao_state = asyncio.run(run_xiaohao())
        self.assertEqual(xiaohao_state["pending_star_gazing_manifest_time"], "")
        self.assertEqual(xiaohao_state["star_gazing_claimed_avatar"], "")
        self.assertEqual(
            xiaohao_state["avatars"]["素心子"]["pending_star_gazing_target_time"],
            "",
        )

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

    def test_star_gazing_collector_manifest_during_settlement_targets_current_boundary(self):
        msg = DummyMessage(7304, text="")
        msg.date = datetime(2026, 6, 23, 1, 0, 35, tzinfo=timezone.utc)
        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""

        record = star_gazing_collector.build_star_gazing_event_record("main", msg, text)

        self.assertEqual(record["event_kind"], "manifest")
        self.assertEqual(record["message_boundary_time"], "2026-06-23 09:00:00")
        self.assertEqual(record["target_manifest_time"], "2026-06-23 09:00:00")

    def test_star_gazing_collector_ignores_non_game_bot_sender(self):
        msg = DummyMessage(7304, text="")
        sender = SimpleNamespace(username="uuyuu_5", first_name="uuyuu_5")
        text = """
【星盘显化】
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！
下一次天道演化将是: 【Good - 地磁暴动】
当前天命所归: @bar
"""

        self.assertIsNone(
            star_gazing_collector.build_star_gazing_event_record(
                "sub", msg, text, sender=sender
            )
        )

    def test_new_hantianzun_bots_are_recognized_as_game_bots(self):
        actor = SimpleNamespace(mc={})
        for username in ("snpao_bot", "xlqlcy_bot"):
            sender = SimpleNamespace(username=username, first_name="韩天尊")
            self.assertTrue(log_utils.is_game_bot_sender(actor, sender))

        for index in range(10, 51):
            sender = SimpleNamespace(username=f"hantianzun{index}_bot", first_name="韩天尊")
            self.assertTrue(log_utils.is_game_bot_sender(actor, sender))

        msg = DummyMessage(7304, text="")
        msg.date = datetime(2026, 6, 23, 1, 0, 35, tzinfo=timezone.utc)
        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""

        for index in range(10, 51):
            sender = SimpleNamespace(username=f"hantianzun{index}_bot", first_name="韩天尊")
            record = star_gazing_collector.build_star_gazing_event_record(
                "sub", msg, text, sender=sender
            )

            self.assertIsNotNone(record)
            self.assertEqual(record["event_kind"], "manifest")

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
        self.assertTrue(scheduled[0][1]["immediate_shift"])

    def test_stale_star_gazing_claim_does_not_block_new_good_manifest(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 24, 11, 50, 0)
                return value.replace(tzinfo=tz) if tz else value

        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""
        sender = SimpleNamespace(username="hantianzzzzzz_bot")

        async def run_case(cls, module, avatar):
            actor = cls.__new__(cls)
            actor.avatars = [avatar]
            actor.avatar_nicknames = {avatar: ""}
            actor.star_gazing_lock = asyncio.Lock()
            actor.star_gazing_task = None
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "pending_star_gazing_date": "2026-06-22",
                "pending_star_gazing_target_time": "2026-06-22 17:59:00",
                "pending_star_gazing_scheduled_time": "2026-06-22 17:59:00",
                "pending_star_gazing_manifest_time": "2026-06-22 18:00:00",
                "pending_star_gazing_fate_type": "Good - 五彩缤纷",
                "star_gazing_claimed_manifest_time": "2026-06-22 18:00:00",
                "star_gazing_claimed_avatar": avatar,
                "star_gazing_assigned_manifest_time": "2026-06-22 18:00:00",
                "star_gazing_assigned_avatar": avatar,
                "next_star_gazing_time": "2026-06-22 17:59:00",
                "star_gazing_avatar_index": 0,
                "avatars": {avatar: {}},
            }
            with patch.object(module, "datetime", FixedDatetime):
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7312, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return handled, actor.state, scheduled

        for cls, module, avatar in (
            (Cultivator, intelligent_cultivator, "素缘子"),
            (SubCultivator, sub_cultivator, "厚土"),
        ):
            with self.subTest(cls=cls.__name__):
                handled, state, scheduled = asyncio.run(run_case(cls, module, avatar))
                self.assertTrue(handled)
                self.assertEqual(state["pending_star_gazing_manifest_time"], "2026-06-24 12:00:00")
                self.assertEqual(state["star_gazing_claimed_manifest_time"], "2026-06-24 12:00:00")
                self.assertEqual(state["pending_star_gazing_target_time"], "2026-06-24 11:59:00")
                self.assertEqual(state["star_gazing_claimed_avatar"], avatar)
                self.assertEqual(len(scheduled), 1)
                self.assertEqual(
                    scheduled[0][1]["manifest_dt"].strftime("%Y-%m-%d %H:%M:%S"),
                    "2026-06-24 12:00:00",
                )

    def test_same_manifest_assigned_round_does_not_rotate_second_avatar(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 24, 12, 0, 38)
                return value.replace(tzinfo=tz) if tz else value

        manifest_key = "2026-06-24 12:00:00"
        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""
        sender = SimpleNamespace(username="hantianzzzzzz_bot")

        async def run_main_like(cls, module, first_avatar, second_avatar=None):
            actor = cls.__new__(cls)
            actor.mc = {}
            actor.avatars = [first_avatar] + ([second_avatar] if second_avatar else [])
            actor.avatar_nicknames = {name: "" for name in actor.avatars}
            actor.star_gazing_lock = asyncio.Lock()
            actor.star_gazing_task = None
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "star_gazing_avatar_index": 1 if second_avatar else 0,
                "star_gazing_assigned_manifest_time": manifest_key,
                "star_gazing_assigned_avatar": first_avatar,
                "last_star_gazing_report_manifest_time": "",
                "avatars": {
                    first_avatar: {"last_gazing_date": "2026-06-24"},
                    **({second_avatar: {}} if second_avatar else {}),
                },
            }
            with patch.object(module, "datetime", FixedDatetime):
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7317, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return handled, actor.state, scheduled

        cases = (
            (Cultivator, intelligent_cultivator, "素缘子", None),
            (SubCultivator, sub_cultivator, "厚土", "缘生子"),
        )
        for cls, module, first_avatar, second_avatar in cases:
            with self.subTest(cls=cls.__name__):
                handled, state, scheduled = asyncio.run(
                    run_main_like(cls, module, first_avatar, second_avatar)
                )
                self.assertTrue(handled)
                self.assertEqual(scheduled, [])
                self.assertEqual(state.get("star_gazing_claimed_manifest_time", ""), "")
                self.assertEqual(state.get("star_gazing_claimed_avatar", ""), "")
                self.assertEqual(state["star_gazing_assigned_manifest_time"], manifest_key)
                self.assertEqual(state["star_gazing_assigned_avatar"], first_avatar)

        async def run_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.mc = {}
            actor.avatars = ["素心子", "缘生子"]
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.avatar_schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "star_gazing_avatar_index": 1,
                "star_gazing_assigned_manifest_time": manifest_key,
                "star_gazing_assigned_avatar": "素心子",
                "last_star_gazing_report_manifest_time": "",
                "avatars": {
                    "素心子": {"last_gazing_date": "2026-06-24"},
                    "缘生子": {},
                },
            }
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                await actor.avatar_handle_star_gazing_opportunity(
                    None,
                    DummyMessage(7318, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return actor.state, scheduled

        xiaohao_state, xiaohao_scheduled = asyncio.run(run_xiaohao())
        self.assertEqual(xiaohao_scheduled, [])
        self.assertEqual(xiaohao_state.get("star_gazing_claimed_manifest_time", ""), "")
        self.assertEqual(xiaohao_state.get("star_gazing_claimed_avatar", ""), "")
        self.assertEqual(xiaohao_state["star_gazing_assigned_manifest_time"], manifest_key)
        self.assertEqual(xiaohao_state["star_gazing_assigned_avatar"], "素心子")

    def test_good_notice_before_final_report_uses_current_manifest_all_accounts(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 24, 12, 0, 38)
                return value.replace(tzinfo=tz) if tz else value

        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""
        sender = SimpleNamespace(username="hantianzzzzzz_bot")

        async def run_main_like(cls, module, avatar):
            actor = cls.__new__(cls)
            actor.mc = {}
            actor.avatars = [avatar]
            actor.avatar_nicknames = {avatar: ""}
            actor.star_gazing_lock = asyncio.Lock()
            actor.star_gazing_task = None
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "star_gazing_avatar_index": 0,
                "last_star_gazing_report_manifest_time": "",
                "avatars": {avatar: {}},
            }
            with patch.object(module, "datetime", FixedDatetime):
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7313, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return handled, actor.state, scheduled

        for cls, module, avatar in (
            (Cultivator, intelligent_cultivator, "素缘子"),
            (SubCultivator, sub_cultivator, "厚土"),
        ):
            with self.subTest(cls=cls.__name__):
                handled, state, scheduled = asyncio.run(run_main_like(cls, module, avatar))
                self.assertTrue(handled)
                self.assertEqual(state["pending_star_gazing_manifest_time"], "2026-06-24 12:00:00")
                self.assertEqual(state["star_gazing_claimed_manifest_time"], "2026-06-24 12:00:00")
                self.assertEqual(len(scheduled), 1)
                self.assertTrue(scheduled[0][1]["immediate_shift"])
                self.assertEqual(
                    scheduled[0][1]["manifest_dt"].strftime("%Y-%m-%d %H:%M:%S"),
                    "2026-06-24 12:00:00",
                )

        async def run_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.mc = {}
            actor.avatars = ["素心子", "缘生子"]
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.avatar_schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "star_gazing_avatar_index": 0,
                "last_star_gazing_report_manifest_time": "",
                "avatars": {"素心子": {}, "缘生子": {}},
            }
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                await actor.avatar_handle_star_gazing_opportunity(
                    None,
                    DummyMessage(7314, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return actor.state, scheduled

        xiaohao_state, xiaohao_scheduled = asyncio.run(run_xiaohao())
        self.assertEqual(xiaohao_state["pending_star_gazing_manifest_time"], "2026-06-24 12:00:00")
        self.assertEqual(xiaohao_state["star_gazing_claimed_manifest_time"], "2026-06-24 12:00:00")
        self.assertEqual(len(xiaohao_scheduled), 1)
        self.assertTrue(xiaohao_scheduled[0][1]["immediate_shift"])

    def test_good_notice_after_final_report_does_not_consume_star_gazing_all_accounts(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 24, 12, 0, 45)
                return value.replace(tzinfo=tz) if tz else value

        final_report_manifest = "2026-06-24 12:00:00"
        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""
        sender = SimpleNamespace(username="hantianzzzzzz_bot")

        async def run_main_like(cls, module, avatar):
            actor = cls.__new__(cls)
            actor.mc = {}
            actor.avatars = [avatar]
            actor.avatar_nicknames = {avatar: ""}
            actor.star_gazing_lock = asyncio.Lock()
            actor.star_gazing_task = None
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "star_gazing_avatar_index": 0,
                "last_star_gazing_report_manifest_time": final_report_manifest,
                "avatars": {avatar: {}},
            }
            with patch.object(module, "datetime", FixedDatetime):
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7315, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return handled, actor.state, scheduled

        for cls, module, avatar in (
            (Cultivator, intelligent_cultivator, "素缘子"),
            (SubCultivator, sub_cultivator, "厚土"),
        ):
            with self.subTest(cls=cls.__name__):
                handled, state, scheduled = asyncio.run(run_main_like(cls, module, avatar))
                self.assertTrue(handled)
                self.assertEqual(scheduled, [])
                self.assertEqual(state.get("pending_star_gazing_manifest_time", ""), "")
                self.assertEqual(state.get("star_gazing_claimed_manifest_time", ""), "")

        async def run_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.mc = {}
            actor.avatars = ["素心子", "缘生子"]
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.avatar_schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "star_gazing_avatar_index": 0,
                "last_star_gazing_report_manifest_time": final_report_manifest,
                "avatars": {"素心子": {}, "缘生子": {}},
            }
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                await actor.avatar_handle_star_gazing_opportunity(
                    None,
                    DummyMessage(7316, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return actor.state, scheduled

        xiaohao_state, xiaohao_scheduled = asyncio.run(run_xiaohao())
        self.assertEqual(xiaohao_scheduled, [])
        self.assertEqual(xiaohao_state.get("pending_star_gazing_manifest_time", ""), "")
        self.assertEqual(xiaohao_state.get("star_gazing_claimed_manifest_time", ""), "")

    def test_sub_daily_star_gazing_fallback_uses_avatar_when_main_star_palace_disabled(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 1, 23, 59, 2)
                return value.replace(tzinfo=tz) if tz else value

        async def run_case():
            actor = SubCultivator.__new__(SubCultivator)
            actor.main_star_palace_enabled = False
            actor.avatars = ["厚土", "寻真子"]
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "last_gazing_date": "",
                "last_star_shift_date": "",
                "last_star_gazing_fallback_date": "",
                "star_gazing_avatar_index": 0,
                "avatars": {"厚土": {}, "寻真子": {}},
            }
            with patch.object(sub_cultivator, "datetime", FixedDatetime):
                handled = await actor.maybe_run_daily_star_gazing_fallback()
            return handled, actor.state, scheduled

        handled, state, scheduled = asyncio.run(run_case())
        self.assertTrue(handled)
        self.assertEqual(state["last_star_gazing_fallback_date"], "2026-07-01")
        self.assertEqual(state["star_gazing_claimed_avatar"], "厚土")
        self.assertEqual(state["star_gazing_claimed_manifest_time"], "2026-07-02 00:00:00")
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][1]["avatar"], "厚土")
        self.assertEqual(scheduled[0][1]["manifest_dt"].strftime("%Y-%m-%d %H:%M:%S"), "2026-07-02 00:00:00")
        self.assertEqual(scheduled[0][1]["gazing_date"], "2026-07-01")

    def test_xiaohao_daily_star_gazing_fallback_schedules_rotating_avatar(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 1, 23, 59, 5)
                return value.replace(tzinfo=tz) if tz else value

        async def run_case():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.avatars = ["素心子", "缘生子"]
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            actor.dashboard_command_paused = lambda command, identity=None: False
            scheduled = []

            async def fake_schedule(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.avatar_schedule_star_gazing_simple = fake_schedule
            actor.state = {
                "last_gazing_date": "",
                "last_star_shift_date": "",
                "last_star_gazing_fallback_date": "",
                "star_gazing_avatar_index": 1,
                "avatars": {"素心子": {}, "缘生子": {}},
            }
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                handled = await actor.maybe_run_daily_star_gazing_fallback()
            return handled, actor.state, scheduled

        handled, state, scheduled = asyncio.run(run_case())
        self.assertTrue(handled)
        self.assertEqual(state["last_star_gazing_fallback_date"], "2026-07-01")
        self.assertEqual(state["star_gazing_claimed_avatar"], "素心子")
        self.assertEqual(state["star_gazing_claimed_manifest_time"], "2026-07-02 00:00:00")
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0][0], "素心子")
        self.assertEqual(scheduled[0][1]["manifest_dt"].strftime("%Y-%m-%d %H:%M:%S"), "2026-07-02 00:00:00")
        self.assertEqual(scheduled[0][1]["gazing_date"], "2026-07-01")

    def test_xiaohao_daily_star_gazing_fallback_skips_after_avatar_observed(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 1, 23, 58, 30)
                return value.replace(tzinfo=tz) if tz else value

        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子", "缘生子"]
        actor.save_state = lambda: None
        actor.state = {
            "last_star_gazing_fallback_date": "",
            "last_star_shift_date": "",
            "avatars": {
                "素心子": {"last_gazing_date": "2026-07-01"},
                "缘生子": {},
            },
        }
        with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
            self.assertIsNone(actor.pending_daily_star_gazing_fallback_dt())

    def test_passive_claimed_star_gazing_result_marks_avatar_all_accounts(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 23, 8, 59, 6)
                return value.replace(tzinfo=tz) if tz else value

        manifest_key = "2026-06-23 09:00:00"
        send_key = "2026-06-23 08:59:00"
        text = """
**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@bar**
"""
        sender = SimpleNamespace(username="hantianzzz_bot")

        async def run_main_like(cls, module, avatar):
            actor = cls.__new__(cls)
            actor.avatars = [avatar]
            actor.avatar_nicknames = {avatar: ""}
            actor.avatar_usernames = {"foo": avatar}
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            scheduled = []

            async def fake_shift(avatar_arg, msg_id, manifest_dt, gazing_date=None):
                scheduled.append((avatar_arg, msg_id, manifest_dt.strftime("%Y-%m-%d %H:%M:%S"), gazing_date))

            actor.avatar_schedule_star_shift = fake_shift
            actor.state = {
                "pending_star_gazing_date": "2026-06-23",
                "pending_star_gazing_target_time": send_key,
                "pending_star_gazing_scheduled_time": send_key,
                "pending_star_gazing_manifest_time": manifest_key,
                "pending_star_gazing_fate_type": "Good - 星辰异象",
                "star_gazing_claimed_manifest_time": manifest_key,
                "star_gazing_claimed_avatar": avatar,
                "next_star_gazing_time": send_key,
                "avatars": {avatar: {}},
            }
            with patch.object(module, "datetime", FixedDatetime):
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7310, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return handled, actor.state, scheduled

        for cls, module, avatar in (
            (Cultivator, intelligent_cultivator, "素缘子"),
            (SubCultivator, sub_cultivator, "厚土"),
        ):
            with self.subTest(cls=cls.__name__):
                handled, state, scheduled = asyncio.run(run_main_like(cls, module, avatar))
                self.assertTrue(handled)
                self.assertEqual(state["avatars"][avatar]["last_gazing_date"], "2026-06-23")
                self.assertEqual(state["pending_star_gazing_target_time"], "")
                self.assertEqual(scheduled, [(avatar, 7310, manifest_key, "2026-06-23")])

        async def run_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.avatars = ["素心子", "缘生子"]
            actor.avatar_usernames = {"foo": "素心子"}
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            scheduled = []

            async def fake_shift(avatar_arg, msg_id, manifest_dt, gazing_date=None):
                scheduled.append((avatar_arg, msg_id, manifest_dt.strftime("%Y-%m-%d %H:%M:%S"), gazing_date))

            actor.avatar_schedule_star_shift = fake_shift
            actor.state = {
                "pending_star_gazing_manifest_time": manifest_key,
                "pending_star_gazing_fate_type": "Good - 星辰异象",
                "star_gazing_claimed_manifest_time": manifest_key,
                "star_gazing_claimed_avatar": "素心子",
                "avatars": {
                    "素心子": {
                        "pending_star_gazing_date": "2026-06-23",
                        "pending_star_gazing_target_time": send_key,
                        "next_star_gazing_time": send_key,
                    }
                },
            }
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                await actor.avatar_handle_star_gazing_opportunity(
                    None,
                    DummyMessage(7311, text=text),
                    text,
                    sender,
                )
                await asyncio.sleep(0)
            return actor.state, scheduled

        xiaohao_state, xiaohao_scheduled = asyncio.run(run_xiaohao())
        self.assertEqual(xiaohao_state["avatars"]["素心子"]["last_gazing_date"], "2026-06-23")
        self.assertEqual(xiaohao_state["avatars"]["素心子"]["pending_star_gazing_target_time"], "")
        self.assertEqual(xiaohao_scheduled, [("素心子", 7311, manifest_key, "2026-06-23")])

    def test_main_passive_star_gazing_rejects_other_account_reply_target(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 7, 7, 5, 59, 2)
                return value.replace(tzinfo=tz) if tz else value

        manifest_key = "2026-07-07 06:00:00"
        send_key = "2026-07-07 05:59:00"
        other_text = """
**【星盘显化】**
@hajiimiii 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Good - 星辰异象】**
**当前天命所归**: **@Tianyi**

你有 **5分钟** 时间，可以尝试回复此消息并使用 `.改换星移 @新目标` 来扭转此人天命！
"""

        async def run_case():
            actor = Cultivator.__new__(Cultivator)
            actor.avatars = ["素缘子"]
            actor.avatar_nicknames = {"素缘子": ""}
            actor.avatar_usernames = {"OldEinstein": "素缘子"}
            actor.star_gazing_lock = asyncio.Lock()
            actor.star_gazing_task = None
            actor.save_state = lambda: None
            scheduled = []

            async def fake_shift(*args, **kwargs):
                scheduled.append((args, kwargs))

            actor.avatar_schedule_star_shift = fake_shift
            actor.state = {
                "pending_star_gazing_date": "2026-07-07",
                "pending_star_gazing_target_time": send_key,
                "pending_star_gazing_scheduled_time": send_key,
                "pending_star_gazing_manifest_time": manifest_key,
                "pending_star_gazing_fate_type": "Good - 星辰异象",
                "star_gazing_claimed_manifest_time": manifest_key,
                "star_gazing_claimed_avatar": "素缘子",
                "next_star_gazing_time": send_key,
                "avatars": {"素缘子": {}},
            }
            with patch.object(intelligent_cultivator, "datetime", FixedDatetime):
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(11561018, text=other_text, reply_to_msg_id=11561015),
                    other_text,
                    SimpleNamespace(username="hantianzzzzzz_bot"),
                )
                await asyncio.sleep(0)
            return handled, actor.state, scheduled

        handled, state, scheduled = asyncio.run(run_case())

        self.assertTrue(handled)
        self.assertEqual(state["avatars"]["素缘子"].get("last_gazing_date", ""), "")
        self.assertEqual(state["pending_star_gazing_target_time"], send_key)
        self.assertEqual(scheduled, [])
        probe = DummyCommon()
        probe.avatar_usernames = {"OldEinstein": "素缘子"}
        self.assertEqual(probe.common_star_gazing_observer_identity("@OldEinstein 闭目凝神"), "素缘子")

    def test_sub_bad_manifest_cancels_same_round_pending_star_gazing(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 18, 2, 58, 12)
                return value.replace(tzinfo=tz) if tz else value

        class PendingTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        async def run_case():
            actor = SubCultivator.__new__(SubCultivator)
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            task = PendingTask()
            actor.star_gazing_task = task
            with patch.object(sub_cultivator, "datetime", FixedDatetime):
                manifest_dt = actor.star_gazing_target_for_opportunity()
                manifest_key = manifest_dt.strftime("%Y-%m-%d %H:%M:%S")
                send_key = (manifest_dt - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
                actor.state = {
                    "pending_star_gazing_date": "2026-06-18",
                    "pending_star_gazing_target_time": send_key,
                    "pending_star_gazing_scheduled_time": send_key,
                    "pending_star_gazing_manifest_time": manifest_key,
                    "pending_star_gazing_fate_type": "Good - 地磁暴动",
                    "star_gazing_claimed_manifest_time": manifest_key,
                    "star_gazing_claimed_avatar": "厚土",
                    "next_star_gazing_time": send_key,
                }
                handled = await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7303),
                    """**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Bad - 心魔大劫】**
**当前天命所归**: **@bar**
""",
                    SimpleNamespace(username="hantianzzzzzz_bot"),
                )
            return handled, actor.state, task.cancelled

        handled, state, cancelled = asyncio.run(run_case())

        self.assertTrue(handled)
        self.assertTrue(cancelled)
        self.assertEqual(state["pending_star_gazing_target_time"], "")
        self.assertEqual(state["pending_star_gazing_manifest_time"], "")
        self.assertEqual(state["star_gazing_claimed_manifest_time"], "")
        self.assertEqual(state["star_gazing_claimed_avatar"], "")
        self.assertEqual(state["next_star_gazing_time"], "")

    def test_manifest_notice_without_pending_star_gazing_does_not_parse_empty_time(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {}
        actor.star_gazing_lock = asyncio.Lock()
        actor.save_state = lambda: None

        handled = asyncio.run(actor.maybe_handle_star_gazing_opportunity(
            DummyMessage(7304),
            """**【星盘显化】**
**下一次天道演化将是**: **【Bad - 心魔大劫】**
**当前天命所归**: **@bar**
""",
            SimpleNamespace(username="hantianzun31_bot"),
        ))

        self.assertTrue(handled)

    def test_bad_manifest_cancels_same_round_pending_star_gazing_main_and_xiaohao(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime(2026, 6, 18, 2, 58, 12)
                return value.replace(tzinfo=tz) if tz else value

        class PendingTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        async def run_main():
            actor = Cultivator.__new__(Cultivator)
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            task = PendingTask()
            actor.star_gazing_task = task
            with patch.object(intelligent_cultivator, "datetime", FixedDatetime):
                manifest_dt = actor.star_gazing_target_for_opportunity()
                manifest_key = manifest_dt.strftime("%Y-%m-%d %H:%M:%S")
                send_key = (manifest_dt - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
                actor.state = {
                    "pending_star_gazing_date": "2026-06-18",
                    "pending_star_gazing_target_time": send_key,
                    "pending_star_gazing_scheduled_time": send_key,
                    "pending_star_gazing_manifest_time": manifest_key,
                    "pending_star_gazing_fate_type": "Good - 地磁暴动",
                    "star_gazing_claimed_manifest_time": manifest_key,
                    "star_gazing_claimed_avatar": "素缘子",
                    "next_star_gazing_time": send_key,
                }
                await actor.maybe_handle_star_gazing_opportunity(
                    DummyMessage(7305),
                    """**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Bad - 心魔大劫】**
**当前天命所归**: **@bar**
""",
                    SimpleNamespace(username="hantianzzzzzz_bot"),
                )
            return actor.state, task.cancelled

        async def run_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.star_gazing_lock = asyncio.Lock()
            actor.save_state = lambda: None
            actor.get_avatar_state = lambda name: actor.state.setdefault("avatars", {}).setdefault(name, {})
            actor.set_avatar_state = lambda name, key, value: actor.get_avatar_state(name).__setitem__(key, value)
            with patch.object(cultivator_xiaohao, "datetime", FixedDatetime):
                manifest_dt = actor.star_gazing_target_for_opportunity()
                manifest_key = manifest_dt.strftime("%Y-%m-%d %H:%M:%S")
                send_key = (manifest_dt - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
                actor.state = {
                    "pending_star_gazing_manifest_time": manifest_key,
                    "pending_star_gazing_fate_type": "Good - 地磁暴动",
                    "star_gazing_claimed_manifest_time": manifest_key,
                    "star_gazing_claimed_avatar": "素心子",
                    "avatars": {
                        "素心子": {
                            "pending_star_gazing_date": "2026-06-18",
                            "pending_star_gazing_target_time": send_key,
                            "next_star_gazing_time": send_key,
                        }
                    },
                }
                await actor.avatar_handle_star_gazing_opportunity(
                    None,
                    DummyMessage(7306),
                    """**【星盘显化】**
@foo 闭目凝神，推演天机...星盘之上，天机已然显现！

**下一次天道演化将是**: **【Bad - 心魔大劫】**
**当前天命所归**: **@bar**
""",
                    SimpleNamespace(username="hantianzzzzzz_bot"),
                )
            return actor.state

        main_state, main_cancelled = asyncio.run(run_main())
        xiaohao_state = asyncio.run(run_xiaohao())

        self.assertTrue(main_cancelled)
        self.assertEqual(main_state["pending_star_gazing_target_time"], "")
        self.assertEqual(main_state["star_gazing_claimed_avatar"], "")
        self.assertEqual(
            xiaohao_state["avatars"]["素心子"]["pending_star_gazing_target_time"],
            "",
        )
        self.assertEqual(xiaohao_state["star_gazing_claimed_avatar"], "")

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
            actor._last_game_bot_activity_ts = time.monotonic()
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
                with tempfile.TemporaryDirectory() as tmpdir:
                    shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
                    with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
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

    def test_dashboard_logs_only_show_command_traffic_and_issues(self):
        old_config_dir = dashboard_server.CONFIG_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dashboard_server.CONFIG_DIR = tmp
                entries = [
                    "2026-07-28 19:45:57,452 [INFO] Meditation Step 3: protected from .查看闭关 for 25分钟31秒.",
                    "2026-07-28 19:45:58,000 [INFO] OUT [主魂]:\n.查看闭关",
                    "2026-07-28 19:45:59,000 [INFO] IN [mention 100] 韩天尊(@hantianzun24_bot):\n重复提及消息",
                    "2026-07-28 19:46:00,000 [INFO] IN [.查看闭关] 韩天尊(@hantianzun24_bot):\n闭关回复",
                    "2026-07-28 19:46:01,000 [INFO] OUT [Mini App | 素心子]:\n指令 .元婴出窍",
                    "2026-07-28 19:46:02,000 [INFO] IN [Mini App | 素心子]:\n指令 .元婴出窍 -> 元婴已出窍",
                    "2026-07-28 19:46:03,000 [INFO] Yuanying out active. Auto-return due at 2026-07-28 20:52:40.",
                    "2026-07-28 19:46:04,000 [WARNING] 指令未收到回复，稍后重试。",
                    "2026-07-28 19:46:05,000 [ERROR] Mini App request failed.",
                ]
                with open(os.path.join(tmp, "cultivator.log"), "w", encoding="utf-8") as handle:
                    handle.write("\n".join(entries))

                page = dashboard_server.get_log_page("main", limit=20)
                content = page.get("content") or ""

                self.assertEqual(len(page.get("entries") or []), 5)
                self.assertIn("OUT [主魂]", content)
                self.assertIn("IN [.查看闭关]", content)
                self.assertIn("OUT [Mini App | 素心子]", content)
                self.assertIn("IN [Mini App | 素心子]", content)
                self.assertIn("[ERROR]", content)
                self.assertNotIn("[WARNING]", content)
                self.assertNotIn("Meditation Step 3", content)
                self.assertNotIn("Yuanying out active", content)
                self.assertNotIn("IN [mention 100]", content)

                incoming = dashboard_server.get_log_page("main", limit=20, kind="in")
                self.assertEqual(len(incoming.get("entries") or []), 2)
                issues = dashboard_server.get_log_page("main", limit=20, kind="issue")
                self.assertEqual(len(issues.get("entries") or []), 1)
        finally:
            dashboard_server.CONFIG_DIR = old_config_dir

    def test_dashboard_command_records_use_sent_ledger_rows(self):
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
                        (1004, ".灵树灌溉", "auto", "2026-06-14 12:49:32", None, None, ""),
                        (1005, "不是指令", "auto", "2026-06-14 12:50:32", None, None, ""),
                    ]
                    for cmd_msg, command, source, sent_at, resp_msg, response_at, text in rows:
                        conn.execute(
                            """
                            INSERT INTO command_ledger (
                                account, chat_id, command_msg_id, command, identity, source,
                                status, sent_at, response_msg_id, response_at, updated_at
                            ) VALUES ('main', 1, ?, ?, '主魂', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                cmd_msg,
                                command,
                                source,
                                "matched" if resp_msg else "sent",
                                sent_at,
                                resp_msg,
                                response_at,
                                response_at or sent_at,
                            ),
                        )
                        if resp_msg:
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

                self.assertEqual(row["count"], 4)
                self.assertEqual(row["auto_count"], 3)
                self.assertEqual(row["manual_count"], 1)
                self.assertEqual(row["last_time"], "2026-06-14 12:49:32")
                self.assertEqual(row["recent_times"], [
                    "2026-06-14 09:49:32",
                    "2026-06-14 09:50:28",
                    "2026-06-14 11:49:32",
                    "2026-06-14 12:49:32",
                ])
        finally:
            dashboard_server.CONFIG_DIR = old_config_dir
            dashboard_server.COMMAND_RECORD_CACHE.clear()

    def test_dashboard_daily_reward_log_filters_by_date_identity_and_command(self):
        old_config_dir = dashboard_server.CONFIG_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dashboard_server.CONFIG_DIR = tmp
                db_path = os.path.join(tmp, dashboard_server.MESSAGE_EVENTS_DB_FILE)
                conn = sqlite3.connect(db_path)
                try:
                    dashboard_server.ensure_daily_reward_events_schema(conn)
                    rows = [
                        ("main", "main-ask", "2026-07-03", "09:00:00", "主魂", ".问道", "成功", {"感悟": 1}, "问道参悟成功"),
                        ("xiaohao", "xiao-abyss", "2026-07-03", "10:00:00", "缘生子", ".探渊", "成功", {"三级妖丹": 2}, "灵兽探渊归来"),
                        ("main", "main-rift", "2026-07-03", "11:00:00", "无咎子", ".探寻裂缝", "成功", {"法则碎片·火": 1}, "探寻成功"),
                        ("xiaohao", "old-abyss", "2026-07-02", "10:00:00", "缘生子", ".探渊", "成功", {"三级妖丹": 9}, "昨日探渊"),
                    ]
                    for account, key, date_text, time_text, identity, command, outcome, rewards, excerpt in rows:
                        conn.execute(
                            """
                            INSERT INTO daily_reward_events (
                                account, event_key, event_date, event_time, identity, command,
                                source, outcome, final, rewards_json, reward_summary, excerpt,
                                clean, text_hash, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, '', ?, ?, ?, ?, ?)
                            """,
                            (
                                account,
                                key,
                                date_text,
                                time_text,
                                identity,
                                command,
                                command,
                                outcome,
                                json.dumps(rewards, ensure_ascii=False),
                                excerpt,
                                excerpt,
                                key,
                                f"{date_text} {time_text}",
                                f"{date_text} {time_text}",
                            ),
                        )
                    conn.commit()
                finally:
                    conn.close()

                payload = dashboard_server.build_daily_reward_log(
                    date="2026-07-03",
                    identity="缘生子",
                    command=".探渊",
                )

                self.assertEqual(payload["error"], "")
                self.assertEqual(len(payload["rows"]), 1)
                self.assertEqual(payload["rows"][0]["account"], "xiaohao")
                self.assertEqual(payload["rows"][0]["identity"], "缘生子")
                self.assertEqual(payload["rows"][0]["command"], ".探渊")
                self.assertEqual(payload["summary"]["reward_summary"], "三级妖丹x2")
                self.assertIn(".问道", payload["filters"]["commands"])
                self.assertIn(".探渊", payload["filters"]["commands"])
        finally:
            dashboard_server.CONFIG_DIR = old_config_dir
            dashboard_server.DAILY_REWARD_ENDPOINT_CACHE.clear()

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

    def test_username_less_profile_does_not_regress_main_cultivation(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = []
        actor.state = {
            "level": "元婴后期",
            "current_exp": 1931125,
            "total_exp": 2000000,
        }
        actor.save_state = lambda: None

        text = """
**【闭关成功】**
本次闭关，你的修为最终增加了 **71** 点。

当前境界: 筑基初期
当前修为: **3369 / 5000**
"""

        self.assertFalse(log_utils.record_cultivation_profile_from_text(
            actor,
            text,
            identity="主魂",
            source="manual .闭关修炼",
        ))
        self.assertEqual(actor.state["level"], "元婴后期")
        self.assertEqual(actor.state["current_exp"], 1931125)
        self.assertEqual(actor.state["total_exp"], 2000000)

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

    def test_wait_for_field_training_settlement_fetches_edited_result(self):
        actor = DummyCommon()
        pending = SimpleNamespace(
            id=73001,
            text="**【野外历练】**\n@foo 选择【均衡】策略，正向荒野深处行去...",
        )
        settled = SimpleNamespace(
            id=73001,
            text="**【野外历练 · 灵机暗藏】**\n@foo 采得一份机缘，获得修为 **+157**。",
        )

        class FakeClient:
            async def get_messages(self, chat_id, ids):
                self.seen = (chat_id, ids)
                return settled

        actor.client = FakeClient()
        actor.target_chat_id = -100123456

        result = asyncio.run(actor.wait_for_field_training_settlement(
            pending,
            "主魂",
            timeout_seconds=1,
            poll_seconds=0.01,
        ))

        self.assertIs(result, settled)
        self.assertTrue(actor.is_field_training_settlement_response(result.text))
        self.assertEqual(actor.client.seen, (-100123456, 73001))

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

    def test_auto_beast_roster_refresh_counts_and_parses_response(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.config = {"legacy_beast_roster_command_enabled": True}
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state = {
            "beasts_cache": [],
            "next_beast_status_check_time": "",
            "beast_roster_auto_query_date": today,
            "beast_roster_auto_query_count": 0,
        }
        actor.save_state = lambda: None
        sent = []
        roster = """
【灵兽伙伴们】
- 六翼(一阶)(休息中)
  - 种类: 天鹏
  - 经验: 12
  - 战力: 500
  - 体力: 80
"""

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return roster

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.update_beast_cache()))

        self.assertEqual(sent, [".我的灵兽"])
        self.assertEqual(actor.state["beast_roster_auto_query_count"], 1)
        self.assertEqual(actor.state["last_beast_roster_query_result"], "parsed")
        self.assertEqual(actor.state["beasts_cache"][0]["full_name"], "六翼 (一阶)")
        self.assertEqual(actor.state["beasts_cache"][0]["stamina"], 80)
        self.assertTrue(actor.state.get("beast_roster_updated_at"))

    def test_auto_beast_roster_daily_cap_uses_cached_roster(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.config = {"legacy_beast_roster_command_enabled": True}
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state = {
            "beast_roster_auto_query_date": today,
            "beast_roster_auto_query_count": 2,
            "next_beast_status_check_time": "",
            "beasts_cache": [
                {"full_name": "六翼", "species": "天鹏", "status": "休息中", "power": 500, "exp": 12, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None

        async def fail_send(*args, **kwargs):
            raise AssertionError(".我的灵兽 should not be sent after daily cap")

        actor.send_and_wait_feedback = fail_send

        self.assertTrue(asyncio.run(actor.update_beast_cache()))
        self.assertEqual(actor.state["beast_roster_auto_query_count"], 2)
        self.assertEqual(actor.state["last_beast_roster_query_result"], "daily_limit")
        self.assertGreater(common_seconds_until(actor.state["next_beast_status_check_time"]), 0)

    def test_auto_beast_roster_daily_cap_without_cache_defers(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.config = {"legacy_beast_roster_command_enabled": True}
        today = datetime.now().strftime("%Y-%m-%d")
        actor.state = {
            "beast_roster_auto_query_date": today,
            "beast_roster_auto_query_count": 2,
            "next_beast_status_check_time": "",
            "beasts_cache": [],
        }
        actor.save_state = lambda: None

        async def fail_send(*args, **kwargs):
            raise AssertionError(".我的灵兽 should not be sent after daily cap")

        actor.send_and_wait_feedback = fail_send

        self.assertFalse(asyncio.run(actor.update_beast_cache()))
        self.assertEqual(actor.state["last_beast_roster_query_result"], "daily_limit")
        self.assertGreater(common_seconds_until(actor.state["next_beast_status_check_time"]), 0)

    def test_deprecated_beast_roster_command_is_disabled_by_default(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.config = {}
        actor.state = {"beasts_cache": [{"full_name": "六翼"}]}
        actor.save_state = lambda: None

        async def fail_send(*args, **kwargs):
            raise AssertionError("deprecated .我的灵兽 must not be sent")

        actor.send_and_wait_feedback = fail_send

        self.assertFalse(asyncio.run(actor.update_beast_cache()))
        self.assertEqual(actor.state["last_beast_roster_query_result"], "miniapp_required")
        self.assertGreater(common_seconds_until(actor.state["next_beast_status_check_time"]), 0)

    def test_beast_candidate_protects_low_stamina_focus_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "六翼", "species": "天鹏", "status": "休息中", "power": 900, "exp": 10, "stamina": 45},
            {"full_name": "青蛟", "species": "蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 60},
        ]

        candidates = actor.beast_action_candidates("abyss", cache, 30)

        self.assertEqual([b["full_name"] for b in candidates], ["青蛟"])

    def test_beast_steal_prefers_focus_then_fallback(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        cache = [
            {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 90},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 100},
        ]

        self.assertEqual(actor.select_beast_for_steal(cache)["full_name"], "六翼")

        cache[0]["status"] = "受伤"
        self.assertEqual(actor.select_beast_for_steal(cache)["full_name"], "麻花藤")

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

    def test_pasture_dispatch_does_not_defer_abyss_or_steal(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        last_abyss = add_seconds_str(now_str(), -3 * 3600)
        next_abyss = add_seconds_str(last_abyss, 6 * 3600)
        last_steal = add_seconds_str(now_str(), -3600)
        next_steal = add_seconds_str(last_steal, 4 * 3600)
        pasture_until = add_seconds_str(now_str(), cultivator_xiaohao.PASTURE_CD_SECONDS)
        actor.state = {
            "last_abyss_time": last_abyss,
            "next_abyss_time": next_abyss,
            "last_steal_time": last_steal,
            "next_steal_time": next_steal,
            "next_pasture_time": pasture_until,
            "best_beast_name": "保龄球",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "保龄球", "species": "一阶灵兽", "status": "休息中", "power": 120, "exp": 0, "stamina": 100},
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None

        actor.record_pasture_dispatch("**保龄球、六翼 等2只灵兽** 欢快地冲入了万兽谷！\n它们将在 **4** 小时后自动归来。")

        self.assertEqual(actor.state["last_abyss_time"], last_abyss)
        self.assertEqual(actor.state["next_abyss_time"], next_abyss)
        self.assertEqual(actor.state["last_steal_time"], last_steal)
        self.assertEqual(actor.state["next_steal_time"], next_steal)
        self.assertEqual(actor.state["next_beast_cruise_time"], pasture_until)
        self.assertEqual(actor.state["next_beast_interaction_time"], pasture_until)

    def test_focus_pasture_after_abyss_protection_preserves_status(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "best_beast_name": "六翼",
            "best_beast_status": "放养中",
            "focus_pasture_after_abyss_until": add_seconds_str(now_str(), cultivator_xiaohao.PASTURE_CD_SECONDS),
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "放养中", "power": 4096, "exp": 0, "stamina": 80},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "放养中", "power": 31, "exp": 0, "stamina": 100},
            ],
        }
        actor.save_state = lambda: None

        actor.mark_all_pastured_beasts_returned()

        self.assertEqual(actor.state["best_beast_status"], "放养中")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "放养中")
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "休息中")

    def test_beast_not_before_helpers_preserve_last_action_times(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "last_abyss_time": "2026-06-25 16:12:39",
            "next_abyss_time": "2026-06-25 22:12:39",
            "last_steal_time": "2026-06-25 16:25:07",
            "next_steal_time": "2026-06-25 20:25:07",
        }

        actor.set_next_abyss_not_before("2026-06-26 00:32:02")
        actor.set_next_steal_not_before("2026-06-26 00:32:02")

        self.assertEqual(actor.state["last_abyss_time"], "2026-06-25 16:12:39")
        self.assertEqual(actor.state["next_abyss_time"], "2026-06-26 00:32:02")
        self.assertEqual(actor.state["last_steal_time"], "2026-06-25 16:25:07")
        self.assertEqual(actor.state["next_steal_time"], "2026-06-26 00:32:02")

    def test_avatar_yuanying_rift_checks_do_not_require_meditation_ready(self):
        async def capture_xiaohao():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            calls = []

            async def fake_yuanying(avatar, require_meditation_ready=False):
                calls.append(("yuanying", avatar, require_meditation_ready))

            async def fake_rift(avatar, cd_seconds, require_meditation_ready=False):
                calls.append(("rift", avatar, require_meditation_ready))

            actor.common_avatar_yuanying_out_check = fake_yuanying
            actor.common_avatar_rift_search_check = fake_rift
            await actor._avatar_yuanying_out_check("缘生子")
            await actor._avatar_rift_search_check("缘生子")
            return calls

        async def capture_sub():
            actor = SubCultivator.__new__(SubCultivator)
            calls = []

            async def fake_yuanying(avatar, require_meditation_ready=False):
                calls.append(("yuanying", avatar, require_meditation_ready))

            async def fake_rift(avatar, cd_seconds, require_meditation_ready=False):
                calls.append(("rift", avatar, require_meditation_ready))

            actor.common_avatar_yuanying_out_check = fake_yuanying
            actor.common_avatar_rift_search_check = fake_rift
            await actor._avatar_yuanying_out_check("缘生子")
            await actor._avatar_rift_search_check("缘生子")
            return calls

        calls = asyncio.run(capture_xiaohao()) + asyncio.run(capture_sub())

        self.assertEqual(calls, [
            ("yuanying", "缘生子", False),
            ("rift", "缘生子", False),
            ("yuanying", "缘生子", False),
            ("rift", "缘生子", False),
        ])

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

    def test_focus_beast_waits_for_steal_settlement_before_pasture(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "best_beast_name": "六翼",
            "best_beast_status": "出战中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "出战中", "power": 4096, "exp": 0, "stamina": 80},
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
        self.assertEqual(actor.get_cached_beast_by_name("六翼")["status"], "偷菜中")
        self.assertGreater(common_seconds_until(actor.state["next_focus_pasture_after_abyss_time"]), 9 * 60)

        settlement = "灵兽偷菜成功，获得【灵石】x1。"
        self.assertTrue(actor.record_manual_beast_command_response(".灵兽偷菜", settlement))
        self.assertTrue(actor.focus_pasture_after_abyss_due())

    def test_focus_beast_is_pastured_after_steal(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 100},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽出战 六翼":
                return "已将灵兽【六翼】设为出战状态。"
            if command == ".灵兽偷菜":
                return "灵兽偷菜成功，获得【灵石】x1。"
            if command == ".灵兽休息 六翼":
                return "已将灵兽【六翼】召回休息。"
            if command == ".一键放养":
                return "**六翼 等1只灵兽** 欢快地冲入了万兽谷！它将在 **4** 小时后自动归来。"
            return ""

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_steal_with_candidate()))
        self.assertEqual(sent, [
            ".灵兽出战 六翼",
            ".灵兽偷菜",
            ".灵兽休息 六翼",
            ".一键放养",
        ])
        self.assertEqual(actor.get_cached_beast_by_name("六翼")["status"], "放养中")

    def test_border_patrol_selects_highest_stamina_resting_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        cache = [
            {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 100},
            {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 95},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "出战中", "power": 31, "exp": 0, "stamina": 100},
        ]

        self.assertEqual(actor.select_beast_for_border_patrol(cache)["full_name"], "青蛟")

    def test_border_patrol_recall_never_selects_focus_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        cache = [
            {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "出战中", "power": 4096, "exp": 0, "stamina": 100},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "偷菜中", "power": 31, "exp": 0, "stamina": 24},
        ]

        self.assertEqual(actor.select_beast_to_recall_for_border_patrol(cache)["full_name"], "麻花藤")

    def test_border_patrol_skips_known_low_stamina_candidate(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        cache = [
            {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 23},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 24},
        ]

        self.assertEqual(actor.select_beast_for_border_patrol(cache)["full_name"], "麻花藤")

    def test_border_patrol_recall_skips_known_low_stamina_candidate(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        cache = [
            {"full_name": "青蛟", "species": "二阶蛟龙", "status": "偷菜中", "power": 420, "exp": 8, "stamina": 23},
            {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "偷菜中", "power": 31, "exp": 0, "stamina": 24},
        ]

        self.assertEqual(actor.select_beast_to_recall_for_border_patrol(cache)["full_name"], "麻花藤")

    def test_border_patrol_defaults_to_raid_mode(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 95},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return "灵兽【青蛟】领命前往边境巡行，执行【袭营】。"

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(sent, [".灵兽巡边 青蛟 袭营"])
        self.assertEqual(actor.state["beast_border_patrol_name"], "青蛟")
        self.assertEqual(actor.state["beast_border_patrol_mode"], "袭营")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "巡边中")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 70 * 60)

    def test_border_patrol_success_with_duration_marks_beast_busy(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "保龄球", "species": "二阶豹麟兽", "status": "休息中", "power": 278, "exp": 291, "stamina": 65},
            ],
        }
        actor.save_state = lambda: None
        response = "**【灵兽巡边】**\n你放出灵兽【保龄球】执行 **夜嗅敌营**。\n消耗体力 **24**，预计 **75分钟** 后归来。\n\n到时使用 `.巡边归来` 结算。"

        self.assertTrue(actor.record_beast_border_patrol_response(response, "保龄球", "袭营"))

        self.assertEqual(actor.state["beast_border_patrol_name"], "保龄球")
        self.assertEqual(actor.state["beast_border_patrol_mode"], "袭营")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "巡边中")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 70 * 60)

    def test_border_patrol_stamina_failure_tries_next_resting_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 95},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽巡边 青蛟 袭营":
                return "灵兽【青蛟】体力不足，至少需要 24 点体力。"
            if command == ".灵兽巡边 麻花藤 袭营":
                return "灵兽【麻花藤】领命前往边境巡行，执行【袭营】。"
            return ""

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(sent, [".灵兽巡边 青蛟 袭营", ".灵兽巡边 麻花藤 袭营"])
        self.assertEqual(actor.state["beasts_cache"][0]["stamina"], 23)
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "巡边中")
        self.assertEqual(actor.state["beast_border_patrol_name"], "麻花藤")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 70 * 60)

    def test_border_patrol_stamina_then_busy_candidate_tries_third_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "龟仔", "species": "二阶玄龟", "status": "休息中", "power": 510, "exp": 12, "stamina": 95},
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "休息中", "power": 195, "exp": 652, "stamina": 80},
                {"full_name": "谛听", "species": "二阶谛听", "status": "休息中", "power": 360, "exp": 90, "stamina": 61},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽巡边 龟仔 袭营":
                return "灵兽【龟仔】体力不足。本次夜嗅敌营需要 **24** 体力，当前 **17**。"
            if command == ".灵兽巡边 猴哥 袭营":
                return "灵兽【猴哥】当前正在(放养中)，无法巡边。"
            if command == ".灵兽巡边 谛听 袭营":
                return "灵兽【谛听】领命前往边境巡行，执行【袭营】。"
            return ""

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(
            sent,
            [".灵兽巡边 龟仔 袭营", ".灵兽巡边 猴哥 袭营", ".灵兽巡边 谛听 袭营"],
        )
        self.assertEqual(actor.state["beasts_cache"][0]["stamina"], 17)
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "放养中")
        self.assertEqual(actor.state["beasts_cache"][2]["status"], "巡边中")
        self.assertEqual(actor.state["beast_border_patrol_name"], "谛听")

    def test_border_patrol_busy_candidate_can_be_recalled_when_no_resting_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "休息中", "power": 195, "exp": 652, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽巡边 猴哥 袭营" and sent.count(command) == 1:
                return "灵兽【猴哥】当前正在(放养中)，无法巡边。"
            if command == ".灵兽休息 猴哥":
                return "已将灵兽【猴哥】召回休息。"
            if command == ".灵兽巡边 猴哥 袭营":
                return "灵兽【猴哥】领命前往边境巡行，执行【袭营】。"
            return ""

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(
            sent,
            [".灵兽巡边 猴哥 袭营", ".灵兽休息 猴哥", ".灵兽巡边 猴哥 袭营"],
        )
        self.assertEqual(actor.state["beast_border_patrol_name"], "猴哥")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "巡边中")

    def test_border_patrol_recalls_highest_stamina_when_no_resting_beast(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "出战中", "power": 4096, "exp": 0, "stamina": 80},
                {"full_name": "青蛟", "species": "二阶蛟龙", "status": "放养中", "power": 420, "exp": 8, "stamina": 95},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "偷菜中", "power": 31, "exp": 0, "stamina": 100},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽休息 麻花藤":
                return "已将灵兽【麻花藤】召回休息。"
            if command == ".灵兽巡边 麻花藤 袭营":
                return "灵兽【麻花藤】领命前往边境巡行，执行【袭营】。"
            return ""

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(sent, [".灵兽休息 麻花藤", ".灵兽巡边 麻花藤 袭营"])
        self.assertEqual(actor.state["beast_border_patrol_name"], "麻花藤")
        self.assertEqual(actor.state["beasts_cache"][2]["status"], "巡边中")

    def test_border_patrol_does_not_dispatch_after_early_pasture_recall(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {
                    "full_name": "铁甲龟 (之贰)",
                    "species": "一阶铁甲龟",
                    "status": "放养中",
                    "power": 15,
                    "exp": 90,
                    "stamina": 100,
                },
            ],
            "best_beast_name": "铁甲龟 (之贰)",
            "best_beast_status": "放养中",
            "best_beast_stamina": 100,
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return (
                "你已提前召回放养中的灵兽【铁甲龟 (之贰)】。\n"
                "原本还需 **27分钟18秒** 才会自行归来。\n"
                "提前召回不会结算放养收获。"
            )

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertFalse(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(sent, [".灵兽休息 铁甲龟 (之贰)"])
        self.assertEqual(actor.get_cached_beast_by_name("铁甲龟 (之贰)")["status"], "放养中")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 25 * 60)

    def test_border_patrol_recalls_low_cached_pastured_beasts_until_one_has_stamina(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "放养中", "power": 31, "exp": 0, "stamina": 23},
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "放养中", "power": 195, "exp": 652, "stamina": 17},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽休息 麻花藤":
                return "已将灵兽【麻花藤】召回休息。"
            if command == ".灵兽巡边 麻花藤 袭营":
                return "灵兽【麻花藤】体力不足。本次夜嗅敌营需要 **24** 体力，当前 **10**。"
            if command == ".灵兽休息 猴哥":
                return "已将灵兽【猴哥】召回休息。"
            if command == ".灵兽巡边 猴哥 袭营":
                return "灵兽【猴哥】领命前往边境巡行，执行【袭营】。"
            return ""

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(
            sent,
            [
                ".灵兽休息 麻花藤",
                ".灵兽巡边 麻花藤 袭营",
                ".灵兽休息 猴哥",
                ".灵兽巡边 猴哥 袭营",
            ],
        )
        self.assertEqual(actor.state["beasts_cache"][0]["stamina"], 10)
        self.assertEqual(actor.state["beast_border_patrol_name"], "猴哥")
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "巡边中")

    def test_border_patrol_recall_guard_tries_next_candidate(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "放养中", "power": 195, "exp": 652, "stamina": 97},
                {"full_name": "谛听", "species": "二阶噬魂兽", "status": "放养中", "power": 304, "exp": 2166, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽休息 猴哥":
                actor._last_command_guard_block = {
                    "key": ".灵兽休息 猴哥",
                    "wait": 805,
                    "blocked_until": time.monotonic() + 805,
                    "reason": "command_guard",
                    "at": time.monotonic(),
                }
                return ""
            if command == ".灵兽休息 谛听":
                return "已将灵兽【谛听】召回休息。"
            if command == ".灵兽巡边 谛听 袭营":
                return "灵兽【谛听】领命前往边境巡行，执行【袭营】。"
            return ""

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(
            sent,
            [".灵兽休息 猴哥", ".灵兽休息 谛听", ".灵兽巡边 谛听 袭营"],
        )
        self.assertEqual(actor.state["beast_border_patrol_name"], "谛听")
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "巡边中")

    def test_border_patrol_all_recall_guards_use_shortest_retry(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "放养中", "power": 195, "exp": 652, "stamina": 97},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            actor._last_command_guard_block = {
                "key": ".灵兽休息 猴哥",
                "wait": 805,
                "blocked_until": time.monotonic() + 805,
                "reason": "command_guard",
                "at": time.monotonic(),
            }
            return ""

        async def fake_sleep(*args, **kwargs):
            return None

        actor.send_and_wait_feedback = fake_send

        with patch.object(cultivator_xiaohao.asyncio, "sleep", fake_sleep):
            self.assertFalse(asyncio.run(actor.run_beast_border_patrol()))

        self.assertEqual(sent, [".灵兽休息 猴哥"])
        retry = common_seconds_until(actor.state["next_beast_border_patrol_time"])
        self.assertGreaterEqual(retry, 780)
        self.assertLess(retry, 830)

    def test_border_patrol_existing_runner_queries_status_cooldown(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "青蛟", "species": "二阶蛟龙", "status": "休息中", "power": 420, "exp": 8, "stamina": 95},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command.startswith(".灵兽巡边"):
                return "已有灵兽正在边境巡行。"
            return "灵兽【青蛟】正在边境巡行，剩余 12分钟。"

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(sent, [".灵兽巡边 青蛟 袭营", ".巡边状态"])
        self.assertEqual(actor.state["beast_border_patrol_name"], "青蛟")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 10 * 60)

    def test_border_patrol_status_records_colon_name_and_return_due(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "猴哥 (巡边中)", "species": "二阶金瞳妖猴", "status": "未知", "power": 195, "exp": 652, "stamina": 28},
            ],
        }
        actor.save_state = lambda: None
        text = "**【灵兽边境编队】**\n灵兽：**猴哥**\n路线：**夜嗅敌营**\n状态：尚需 **15秒** 可用 `.巡边归来` 结算。"

        self.assertTrue(actor.record_beast_border_patrol_status_response(text))

        self.assertEqual(actor.state["beast_border_patrol_name"], "猴哥")
        self.assertEqual(actor.state["beasts_cache"][0]["full_name"], "猴哥")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "巡边中")
        self.assertLessEqual(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 15)

    def test_cruise_blocked_by_border_patrol_records_beast_status(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "休息中", "power": 195, "exp": 652, "stamina": 28},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 38, "exp": 80, "stamina": 38},
            ],
        }
        actor.save_state = lambda: None
        text = "你的灵兽【猴哥】当前正在(巡边中)，无法巡游。"

        self.assertTrue(actor.record_manual_beast_command_response(".灵兽巡游 猴哥", text))

        self.assertEqual(actor.state["beasts_cache"][0]["status"], "巡边中")
        self.assertEqual(actor.state["beast_border_patrol_name"], "猴哥")
        self.assertTrue(actor.state.get("next_beast_border_patrol_time"))
        self.assertTrue(actor.state.get("next_beast_cruise_time"))
        self.assertEqual(actor.select_beast_for_cruise(actor.state["beasts_cache"])["full_name"], "麻花藤")

    def test_roster_refresh_preserves_active_border_patrol_status(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "保龄球",
            "next_beast_border_patrol_time": (datetime.now() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"),
            "beasts_cache": [],
        }
        actor.save_state = lambda: None
        text = """
**【灵兽伙伴们】**
- 保龄球 (休息中)
  - 种类: 二阶灵兽
  - 经验: 100
  - 战力: 278
  - 体力: 65
- 猴哥 (放养中)
  - 种类: 二阶金瞳妖猴
  - 经验: 652
  - 战力: 195
  - 体力: 4
"""

        self.assertTrue(actor.record_beast_roster_response(text, source="fixture"))

        by_name = {beast["full_name"]: beast for beast in actor.state["beasts_cache"]}
        self.assertEqual(by_name["保龄球"]["status"], "巡边中")
        self.assertEqual(actor.state["beast_border_patrol_name"], "保龄球")

    def test_roster_refresh_syncs_active_border_patrol_as_due_backstop(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "",
            "next_beast_border_patrol_time": "",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.save_state = lambda: None
        text = """
**@TitanCreeper**** 的灵兽伙伴们：**

**- 谛听** (巡边中)
  - **种类**: 二阶噬魂兽
  - **品阶**: 2阶, **等级**: 22
  - **经验**: 2166 / 2200
  - **战力**: 304
  - **灵性**: 忠诚: 100 | 体力: 61
  - **传闻**: 【谛听】受命沿慕兰边境执行夜嗅敌营。
**- 风希** (休息中)
  - **种类**: 一阶风雀
  - **品阶**: 1阶, **等级**: 9
  - **经验**: 644 / 900
  - **战力**: 29
  - **灵性**: 忠诚: 56 | 体力: 100
"""

        self.assertTrue(actor.record_beast_roster_response(text, source="fixture"))
        self.assertEqual(actor.state["beast_border_patrol_name"], "谛听")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 0)
        self.assertLessEqual(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 65)

    def test_border_patrol_active_sends_return_before_new_patrol(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "猴哥",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(minutes=76)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": "",
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "巡边中", "power": 195, "exp": 652, "stamina": 28},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 38, "exp": 80, "stamina": 38},
            ],
        }
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".巡边归来":
                return "灵兽【猴哥】巡边归来，带回【兽粮】x1。"
            if command == ".灵兽巡边 麻花藤 袭营":
                return "灵兽【麻花藤】领命前往边境巡行，执行【袭营】。"
            return ""

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.run_beast_border_patrol()))
        self.assertEqual(sent, [".巡边归来", ".灵兽巡边 麻花藤 袭营"])
        self.assertEqual(actor.state["beast_border_patrol_name"], "麻花藤")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "休息中")
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "巡边中")
        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 70 * 60)

    def test_pasture_return_wakes_overdue_border_patrol_retry(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": (datetime.now() + timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
            "pasture_pending_count": 1,
            "pasture_returned_count": 0,
            "pasture_pending_since": (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "beasts_cache": [
                {"full_name": "谛听", "species": "二阶噬魂兽", "status": "放养中", "power": 304, "exp": 2166, "stamina": 61},
            ],
        }
        actor._pasture_return_seen_counts = {}
        actor.save_state = lambda: None
        actor.is_pasture_return_message = lambda text: True
        actor.text_targets_self = lambda msg, text: True
        actor.parse_pasture_return_count = lambda text: 1
        actor.mark_pastured_beasts_returned = lambda text: 1
        actor.mark_all_pastured_beasts_returned = lambda: 0

        class DummyEvent:
            message = SimpleNamespace(text="谛听放养归来。", id=9901)

            async def get_sender(self):
                return None

        async def run_case():
            actor.beast_wakeup = asyncio.Event()
            return await actor.handle_pasture_return_event(DummyEvent())

        self.assertTrue(asyncio.run(run_case()))
        self.assertEqual(actor.state["next_beast_border_patrol_time"], "")

    def test_edited_pasture_return_targets_identity_username_and_updates_stamina(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.my_info = None
        actor.notify_users = []
        actor.mc = {"watch_bot": "fanrenxiuxian_bot"}
        actor.identity_usernames = {"主魂": ["TitanCreeper"]}
        actor.state = {
            "beast_border_patrol_name": "",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": (datetime.now() + timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
            "pasture_pending_count": 3,
            "pasture_returned_count": 0,
            "pasture_pending_since": (datetime.now() - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "beasts_cache": [
                {"full_name": "六翼", "species": "六阶玄冥冰祖", "status": "放养中", "power": 4096, "exp": 0, "stamina": 4},
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "放养中", "power": 195, "exp": 652, "stamina": 17},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "受伤", "power": 38, "exp": 80, "stamina": 74},
            ],
        }
        actor._pasture_return_seen_counts = {}
        actor.save_state = lambda: None

        text = """
**【灵兽归来】**
道友 @TitanCreeper，你放养的 **3** 只灵兽已一同归来，结算如下：
• **【六翼】**：体力恢复 **22**，心情 +8，羁绊 +3，获得 **68** 点经验
• **【猴哥】**：体力恢复 **27**，心情 +5，羁绊 +4，获得 **36** 点经验
• **【麻花藤】**：体力恢复 **31**，心情 +3，羁绊 +5，获得 **28** 点经验
"""

        class DummyEvent:
            message = SimpleNamespace(text=text, id=9902)

            async def get_sender(self):
                return SimpleNamespace(username="fanrenxiuxian_bot")

        async def run_case():
            actor.beast_wakeup = asyncio.Event()
            return await actor.handle_pasture_return_event(DummyEvent())

        self.assertTrue(asyncio.run(run_case()))
        by_name = {beast["full_name"]: beast for beast in actor.state["beasts_cache"]}
        self.assertEqual(actor.state["next_beast_border_patrol_time"], "")
        self.assertEqual(actor.state["pasture_pending_count"], 0)
        self.assertEqual(by_name["六翼"]["status"], "休息中")
        self.assertEqual(by_name["六翼"]["stamina"], 26)
        self.assertEqual(by_name["猴哥"]["stamina"], 44)
        self.assertEqual(by_name["麻花藤"]["status"], "休息中")
        self.assertEqual(by_name["麻花藤"]["stamina"], 100)

    def test_beast_action_timer_prioritizes_border_patrol_before_pasture(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "last_abyss_time": now_str(),
            "next_abyss_time": future,
            "last_steal_time": now_str(),
            "next_steal_time": future,
            "last_pasture_time": (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_pasture_time": (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "last_beast_interaction_time": now_str(),
            "next_beast_interaction_time": future,
            "last_beast_cruise_time": now_str(),
            "next_beast_cruise_time": future,
            "beast_border_patrol_name": "",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(minutes=80)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": "",
            "beasts_cache": [
                {"full_name": "谛听", "species": "二阶噬魂兽", "status": "休息中", "power": 304, "exp": 2166, "stamina": 61},
            ],
        }
        actor.active_atomic_task = None
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽巡边 谛听 袭营":
                return "灵兽【谛听】领命前往边境巡行，执行【袭营】。"
            if command == ".一键放养":
                return "灵兽放养失败：没有休息中灵兽。"
            return ""

        async def false_pause(*args, **kwargs):
            return False

        async def focus_ready():
            return True

        async def stop_after_sleep(sleep_for):
            actor.is_running = False

        async def run_once():
            actor.startup_done = asyncio.Event()
            actor.startup_done.set()
            actor.avatar_send_lock = asyncio.Lock()
            actor.beast_lock = asyncio.Lock()
            actor.beast_wakeup = asyncio.Event()
            actor.is_running = True
            actor.sleep_if_main_soul_paused = false_pause
            actor.send_and_wait_feedback = fake_send
            actor.ensure_focus_beast_ready_for_pasture = focus_ready
            actor.sleep_beast_action = stop_after_sleep
            await actor.run_beast_action_timer()

        old_sleep = cultivator_xiaohao.asyncio.sleep

        async def fake_sleep(*args, **kwargs):
            return None

        cultivator_xiaohao.asyncio.sleep = fake_sleep
        try:
            asyncio.run(run_once())
        finally:
            cultivator_xiaohao.asyncio.sleep = old_sleep

        self.assertGreaterEqual(len(sent), 1)
        self.assertEqual(sent[0], ".灵兽巡边 谛听 袭营")

    def test_pastured_beast_defer_does_not_delay_border_patrol(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        next_patrol = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "last_pasture_time": now_str(),
            "next_pasture_time": (datetime.now() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": next_patrol,
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "休息中", "power": 195, "exp": 652, "stamina": 97},
            ],
        }
        actor.save_state = lambda: None

        actor.defer_beast_actions_while_pastured("猴哥", "fixture")

        self.assertEqual(actor.state["next_beast_border_patrol_time"], next_patrol)
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "放养中")

    def test_repair_overdue_border_patrol_clears_delayed_next_time(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "六翼",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(minutes=80)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.repair_overdue_beast_border_patrol_schedule())
        self.assertEqual(actor.state["next_beast_border_patrol_time"], "")

    def test_repair_overdue_border_patrol_clears_stale_past_next_time(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "保龄球",
            "last_beast_border_patrol_time": (datetime.now() - timedelta(minutes=100)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_beast_border_patrol_time": (datetime.now() - timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.save_state = lambda: None
        actor.beast_wakeup = asyncio.Event()

        self.assertTrue(actor.repair_overdue_beast_border_patrol_schedule())
        self.assertEqual(actor.state["next_beast_border_patrol_time"], "")
        self.assertTrue(actor.beast_wakeup.is_set())

    def test_xiaohao_watchdog_ignores_stale_fishing_due_when_disabled(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        due_at = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
        actor.avatars = ["问心子"]
        actor.get_fishing_state = lambda identity: {"active": identity == "问心子", "active_due_at": due_at}

        stale = actor._stale_fishing_active_identities()

        self.assertEqual(stale, [])

    def test_xiaohao_watchdog_detects_stale_scheduler_due_item(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.field_training_command = ".野外历练 谨慎"
        actor.state = {
            "is_paused": False,
            "identity_pauses": {},
            "next_yuanying_out_time": (datetime.now() - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.main_soul_pause_seconds = lambda: 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False

        stale = actor.stale_scheduler_due_items()

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0][0], "next_yuanying_out_time")

    def test_xiaohao_dead_scheduler_tasks_ignores_bad_registry_type(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor._scheduler_task_registry = []

        self.assertEqual(actor.dead_scheduler_tasks(), [])

    def test_main_and_sub_watchdog_ignore_stale_fishing_due_when_disabled(self):
        for cls in (Cultivator, SubCultivator):
            with self.subTest(cls=cls.__name__):
                actor = cls.__new__(cls)
                due_at = (datetime.now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
                actor.avatars = ["测试化身"]
                actor.get_fishing_state = lambda identity, due_at=due_at: {
                    "active": identity == "测试化身",
                    "active_due_at": due_at,
                }

                stale = actor._stale_fishing_active_identities()

                self.assertEqual(stale, [])

    def test_main_and_sub_dead_scheduler_tasks_ignore_bad_registry_type(self):
        for cls in (Cultivator, SubCultivator):
            with self.subTest(cls=cls.__name__):
                actor = cls.__new__(cls)
                actor._scheduler_task_registry = []

                self.assertEqual(actor.dead_scheduler_tasks(), [])

    def test_main_watchdog_detects_stale_scheduler_due_item(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "is_paused": False,
            "identity_pauses": {},
            "next_yuanying_out_time": (datetime.now() - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False

        stale = actor.stale_scheduler_due_items()

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0][0], "next_yuanying_out_time")

    def test_main_watchdog_detects_due_active_concubine_voyage_without_next_time(self):
        actor = Cultivator.__new__(Cultivator)
        actor.account_key = "main"
        actor.enable_concubine = True
        actor.state = {
            "is_paused": False,
            "identity_pauses": {},
            "concubine_voyage_active": True,
            "next_concubine_voyage_time": "",
            "last_concubine_voyage_time": (
                datetime.now() - timedelta(hours=8)
            ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False

        stale = actor.stale_scheduler_due_items()

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0][0], "next_concubine_voyage_time")
        self.assertEqual(stale[0][1], ".远航归来")

    def test_sub_watchdog_detects_stale_scheduler_due_item(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.main_star_palace_enabled = False
        actor.field_training_command = ".野外历练 谨慎"
        actor.state = {
            "is_paused": False,
            "identity_pauses": {},
            "next_ask_dao_time": (datetime.now() - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False

        stale = actor.stale_scheduler_due_items()

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0][0], "next_ask_dao_time")

    def test_border_patrol_return_response_clears_due_without_extra_cooldown(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "猴哥",
            "next_beast_border_patrol_time": (datetime.now() + timedelta(minutes=75)).strftime("%Y-%m-%d %H:%M:%S"),
            "beasts_cache": [
                {"full_name": "猴哥", "species": "二阶金瞳妖猴", "status": "巡边中", "power": 195, "exp": 652, "stamina": 28},
            ],
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.record_beast_border_patrol_return_response("灵兽【猴哥】巡边归来，带回【兽粮】x1。"))
        self.assertEqual(actor.state["beast_border_patrol_name"], "")
        self.assertEqual(actor.state["next_beast_border_patrol_time"], "")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "休息中")

    def test_border_patrol_return_response_clears_stale_patrol_statuses(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "beast_border_patrol_name": "谛听",
            "next_beast_border_patrol_time": (datetime.now() + timedelta(minutes=75)).strftime("%Y-%m-%d %H:%M:%S"),
            "beasts_cache": [
                {"full_name": "谛听", "species": "二阶噬魂兽", "status": "巡边中", "power": 304, "exp": 2166, "stamina": 61},
                {"full_name": "风希", "species": "一阶风雀", "status": "巡边中", "power": 29, "exp": 644, "stamina": 76},
            ],
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.record_beast_border_patrol_return_response("**【巡边归来 · 夜嗅敌营】**\n获得灵石 **+104**"))
        self.assertEqual(actor.state["beast_border_patrol_name"], "")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "休息中")
        self.assertEqual(actor.state["beasts_cache"][1]["status"], "休息中")

    def test_border_patrol_unknown_response_uses_conservative_cooldown(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        actor.save_state = lambda: None

        with patch.object(cultivator_xiaohao, "notify_unrecognized_response") as notify:
            self.assertFalse(actor.record_beast_border_patrol_response("陌生回应", "青蛟", "袭营"))

        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 70 * 60)
        notify.assert_called_once()

    def test_border_patrol_unknown_status_uses_conservative_cooldown(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {}
        actor.save_state = lambda: None

        with patch.object(cultivator_xiaohao, "notify_unrecognized_response") as notify:
            self.assertFalse(actor.record_beast_border_patrol_status_response("陌生状态"))

        self.assertGreater(common_seconds_until(actor.state["next_beast_border_patrol_time"]), 70 * 60)
        notify.assert_called_once()

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

    def test_focus_beast_tries_pasture_immediately_after_abyss_success(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "next_pasture_time": add_seconds_str(now_str(), 3 * 3600),
            "best_beast_name": "六翼",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        actor.record_daily_reward_event = lambda *args, **kwargs: True
        sent = []

        async def fake_update():
            return True

        async def fake_abyss(beast_name):
            sent.append(f".探渊 {beast_name}")
            return "你的灵兽【六翼】成功击败了对手！它带回了战利品：【兽骨】x1。"

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".一键放养":
                return "**六翼 等1只灵兽** 欢快地冲入了万兽谷！它将在 **4** 小时后自动归来。"
            return ""

        actor.update_beast_cache = fake_update
        actor.send_abyss_with_busy_retry = fake_abyss
        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_abyss_with_fallback()))

        self.assertEqual(sent, [".探渊 六翼", ".一键放养"])
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "放养中")
        self.assertEqual(actor.state["best_beast_status"], "放养中")
        self.assertGreater(common_seconds_until(actor.state["next_abyss_time"]), 5 * 3600)
        self.assertEqual(actor.state["next_focus_pasture_after_abyss_time"], "")
        self.assertGreater(common_seconds_until(actor.state["focus_pasture_after_abyss_until"]), 3 * 3600)

    def test_focus_beast_finishes_due_abyss_and_steal_before_pasture(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "best_beast_name": "六翼",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
                {"full_name": "麻花藤", "species": "一阶噬灵花藤", "status": "休息中", "power": 31, "exp": 0, "stamina": 100},
            ],
        }
        actor.save_state = lambda: None
        actor.record_daily_reward_event = lambda *args, **kwargs: True
        sent = []

        async def fake_update():
            return True

        async def fake_abyss(beast_name):
            sent.append(f".探渊 {beast_name}")
            return "你的灵兽【六翼】成功击败了对手！它带回了战利品：【兽骨】x1。"

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".灵兽出战 六翼":
                return "已将灵兽【六翼】设为出战状态。"
            if command == ".灵兽偷菜":
                return "灵兽偷菜成功，获得【灵石】x1。"
            if command == ".灵兽休息 六翼":
                return "已将灵兽【六翼】召回休息。"
            if command == ".一键放养":
                return "**六翼 等1只灵兽** 欢快地冲入了万兽谷！它将在 **4** 小时后自动归来。"
            return ""

        actor.update_beast_cache = fake_update
        actor.send_abyss_with_busy_retry = fake_abyss
        actor.send_and_wait_feedback = fake_send

        async def run_actions():
            self.assertTrue(await actor.execute_abyss_with_fallback(defer_focus_pasture=True))
            self.assertNotIn(".一键放养", sent)
            self.assertTrue(await actor.execute_steal_with_candidate())

        asyncio.run(run_actions())

        self.assertEqual(sent, [
            ".探渊 六翼",
            ".灵兽出战 六翼",
            ".灵兽偷菜",
            ".灵兽休息 六翼",
            ".一键放养",
        ])
        self.assertEqual(actor.get_cached_beast_by_name("六翼")["status"], "放养中")

    def test_focus_beast_after_abyss_immediate_pasture_block_reschedules(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "best_beast_name": "六翼",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        actor.record_daily_reward_event = lambda *args, **kwargs: True
        sent = []

        async def fake_update():
            return True

        async def fake_abyss(beast_name):
            sent.append(f".探渊 {beast_name}")
            return "你的灵兽【六翼】成功击败了对手！它带回了战利品：【兽骨】x1。"

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            if command == ".一键放养":
                return "灵兽【六翼】刚刚探渊归来，尚需 23分钟 休养后才能放养。"
            return ""

        actor.update_beast_cache = fake_update
        actor.send_abyss_with_busy_retry = fake_abyss
        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_abyss_with_fallback()))

        self.assertEqual(sent, [".探渊 六翼", ".一键放养"])
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "休息中")
        self.assertEqual(actor.state["best_beast_status"], "休息中")
        self.assertGreater(common_seconds_until(actor.state["next_focus_pasture_after_abyss_time"]), 20 * 60)
        self.assertLess(common_seconds_until(actor.state["next_focus_pasture_after_abyss_time"]), 25 * 60)
        self.assertEqual(actor.state.get("focus_pasture_after_abyss_until", ""), "")

    def test_focus_pasture_after_abyss_success_marks_protected(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "next_focus_pasture_after_abyss_time": add_seconds_str(now_str(), -1),
            "best_beast_name": "六翼",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        response = "**六翼 等1只灵兽** 欢快地冲入了万兽谷！它将在 **4** 小时后自动归来。"

        handled = actor.record_auto_pasture_response(response, actor.state["beasts_cache"], "六翼", "休息中")
        actor.record_focus_pasture_after_abyss_attempt(response, handled)

        self.assertEqual(actor.state["next_focus_pasture_after_abyss_time"], "")
        self.assertEqual(actor.state["beasts_cache"][0]["status"], "放养中")
        self.assertEqual(actor.state["best_beast_status"], "放养中")
        self.assertGreater(common_seconds_until(actor.state["focus_pasture_after_abyss_until"]), 3 * 3600)

    def test_focus_pasture_after_abyss_rest_block_reschedules(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "next_focus_pasture_after_abyss_time": add_seconds_str(now_str(), -1),
            "best_beast_name": "六翼",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        response = "灵兽【六翼】刚刚探渊归来，尚需 23分钟 休养后才能放养。"

        handled = actor.record_auto_pasture_response(response, actor.state["beasts_cache"], "六翼", "休息中")
        actor.record_focus_pasture_after_abyss_attempt(response, handled)

        self.assertTrue(handled)
        self.assertGreater(common_seconds_until(actor.state["next_focus_pasture_after_abyss_time"]), 20 * 60)
        self.assertLess(common_seconds_until(actor.state["next_focus_pasture_after_abyss_time"]), 25 * 60)
        self.assertEqual(actor.state.get("focus_pasture_after_abyss_until", ""), "")

    def test_focus_beast_abyss_start_response_does_not_pasture_immediately(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.state = {
            "best_beast_name": "六翼",
            "best_beast_status": "休息中",
            "beasts_cache": [
                {"full_name": "六翼", "species": "四阶太古冰蜈", "status": "休息中", "power": 4096, "exp": 0, "stamina": 80},
            ],
        }
        actor.save_state = lambda: None
        actor.record_daily_reward_event = lambda *args, **kwargs: True
        sent = []

        async def fake_update():
            return True

        async def fake_abyss(beast_name):
            sent.append(f".探渊 {beast_name}")
            return "灵兽【六翼】已出发进入万兽渊历练。"

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            return ""

        actor.update_beast_cache = fake_update
        actor.send_abyss_with_busy_retry = fake_abyss
        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_abyss_with_fallback()))

        self.assertEqual(sent, [".探渊 六翼"])
        self.assertEqual(actor.state.get("focus_pasture_after_abyss_until", ""), "")

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

    def test_wait_for_bot_activity_reconnects_disconnected_client(self):
        class FakeClient:
            def __init__(self):
                self.connected = False
                self.connect_calls = 0

            def is_connected(self):
                return self.connected

            async def connect(self):
                self.connect_calls += 1
                self.connected = True

            async def is_user_authorized(self):
                return True

        actor = SimpleNamespace(
            client=FakeClient(),
            is_running=True,
            _last_game_bot_activity_ts=log_utils.time.monotonic(),
            _bot_unhealthy_until=0,
        )

        self.assertTrue(asyncio.run(
            log_utils.wait_for_bot_activity_before_send(actor, ".切换 缘生子")
        ))
        self.assertEqual(actor.client.connect_calls, 1)

    def test_wait_for_bot_activity_aborts_persistent_disconnect(self):
        class FakeClient:
            def is_connected(self):
                return False

            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

        actor = SimpleNamespace(
            client=FakeClient(),
            is_running=True,
            _last_game_bot_activity_ts=None,
            _bot_unhealthy_until=0,
        )

        with patch.object(log_utils, "CLIENT_DISCONNECT_ABORT_SECONDS", 0):
            self.assertFalse(asyncio.run(
                log_utils.wait_for_bot_activity_before_send(actor, ".切换 缘生子")
            ))

    def test_wait_for_bot_activity_uses_shared_cross_script_status(self):
        class FakeClient:
            def is_connected(self):
                return True

            async def get_messages(self, chat_id, limit=40):
                return []

        actor = SimpleNamespace(
            client=FakeClient(),
            target_chat_id=-100,
            is_running=True,
            state_file="state_main.json",
            _last_game_bot_activity_ts=None,
            _bot_unhealthy_until=0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
            with open(shared_path, "w", encoding="utf-8") as f:
                json.dump({
                    "accounts": {
                        "sub": {
                            "wall": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "wall_epoch": time.time(),
                            "bot_username": "hantianz_bot",
                        }
                    }
                }, f, ensure_ascii=False)

            with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
                self.assertTrue(asyncio.run(
                    log_utils.wait_for_bot_activity_before_send(actor, ".野外历练")
                ))

        self.assertIsNotNone(actor._last_game_bot_activity_ts)

    def test_shared_bot_maintenance_defers_watchdog_until_activity_resumes(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            _bot_no_response_times=[],
            _bot_unhealthy_until=0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
            with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
                log_utils.record_bot_no_response(actor, ".问道")
                self.assertFalse(log_utils.shared_bot_maintenance_status(actor)["active"])

                log_utils.record_bot_no_response(actor, ".问道")
                status = log_utils.shared_bot_maintenance_status(actor)
                self.assertTrue(status["active"])
                self.assertEqual(status["source"], "shared_health_pause")
                self.assertTrue(log_utils.watchdog_should_defer_for_bot_maintenance(actor))

                log_utils.record_game_bot_activity(actor)
                self.assertTrue(log_utils.shared_bot_maintenance_status(actor)["active"])

                log_utils.record_bot_response(actor, command=".问道")
                self.assertFalse(log_utils.shared_bot_maintenance_status(actor)["active"])

    def test_watchdog_defers_stale_due_while_atomic_task_is_active(self):
        class ActiveTask:
            def done(self):
                return False

            def get_name(self):
                return "FieldTraining-测试"

        task = ActiveTask()
        actor = SimpleNamespace(active_atomic_task=task, _common_atomic_label="FieldTraining-测试")

        self.assertTrue(log_utils.watchdog_should_defer_for_active_atomic_task(actor))
        actor._watchdog_atomic_task_since = time.monotonic() - 601
        self.assertFalse(log_utils.watchdog_should_defer_for_active_atomic_task(actor))

    def test_switch_feedback_rejects_passive_yuanying_settlement(self):
        settlement = "✨ **元神回响**：感应到 @Lvdoumiao 的元婴已神游归来，正在清点收获..."

        self.assertFalse(log_utils.feedback_response_matches_command(".切换 主魂", settlement))
        self.assertTrue(log_utils.feedback_response_matches_command(".元婴出窍", settlement))

    def test_unanswered_dotted_command_counts_as_maintenance_even_with_raw_bot_activity(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            target_chat_id=-100123456,
            _bot_unhealthy_until=0,
        )
        player = SimpleNamespace(username="player")
        bot = SimpleNamespace(username="fanrenxiuxian_bot")

        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
            db_path = os.path.join(tmpdir, "message_events.sqlite3")
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = db_path
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
                with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
                    command_msg = DummyMessage(90001, text=".洞府")
                    command_msg.date = datetime.now(timezone.utc)
                    self.assertTrue(log_utils.record_message_event(
                        actor, command_msg, text=".洞府", sender=player, event_kind="new", direction="raw"
                    ))
                    data = log_utils._read_shared_bot_activity()
                    data["command_probe"]["wall_epoch"] = time.time() - 120
                    data["accounts"] = {
                        "main": {
                            "wall": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "wall_epoch": time.time(),
                            "bot_username": "fanrenxiuxian_bot",
                        }
                    }
                    log_utils._write_shared_bot_activity(data)

                    log_utils.record_game_bot_activity(actor, bot)
                    status = log_utils.shared_bot_maintenance_status(actor)
                    self.assertTrue(status["active"])
                    self.assertEqual(status["source"], "unanswered_dot_command")
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_bot_reply_to_dotted_command_clears_command_silence(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            target_chat_id=-100123456,
            _bot_unhealthy_until=0,
        )
        player = SimpleNamespace(username="player")
        bot = SimpleNamespace(username="fanrenxiuxian_bot")

        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
            db_path = os.path.join(tmpdir, "message_events.sqlite3")
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = db_path
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
                with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
                    command_msg = DummyMessage(90011, text=".洞府")
                    command_msg.date = datetime.now(timezone.utc)
                    log_utils.record_message_event(
                        actor, command_msg, text=".洞府", sender=player, event_kind="new", direction="raw"
                    )
                    data = log_utils._read_shared_bot_activity()
                    data["command_probe"]["wall_epoch"] = time.time() - 120
                    log_utils._write_shared_bot_activity(data)
                    self.assertTrue(log_utils.shared_bot_maintenance_status(actor)["active"])

                    reply_msg = DummyMessage(90012, text="**【洞府】**", reply_to_msg_id=90011)
                    log_utils.record_game_bot_activity(actor, bot, msg=reply_msg, text=reply_msg.text)

                    self.assertFalse(log_utils.shared_bot_maintenance_status(actor)["active"])
                    data = log_utils._read_shared_bot_activity()
                    self.assertGreaterEqual(
                        data.get("command_response", {}).get("wall_epoch", 0),
                        data.get("command_probe", {}).get("wall_epoch", 0),
                    )
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_shared_command_response_accepts_empty_command(self):
        written = []
        actor = SimpleNamespace(state_file="state_main.json", target_chat_id=-100123456)
        msg = DummyMessage(90015, text="edited response")
        with patch.object(log_utils, "_read_shared_bot_activity", return_value={}), patch.object(
            log_utils,
            "_write_shared_bot_activity",
            side_effect=lambda data: written.append(data),
        ):
            self.assertTrue(log_utils._record_shared_command_response(actor, command="", msg=msg))
        self.assertEqual(written[0]["command_response"]["command"], "")

    def test_text_alert_resolves_numeric_target_from_dialog_cache(self):
        sent = []
        input_entity = object()

        class FakeClient:
            async def get_input_entity(self, target):
                raise ValueError("not cached")

            async def get_dialogs(self, limit=200):
                return [SimpleNamespace(
                    entity=SimpleNamespace(id=8219248252),
                    input_entity=input_entity,
                )]

            async def send_message(self, target, text, **kwargs):
                sent.append((target, text, kwargs))

        actor = SimpleNamespace(
            config={"notify_target": "8219248252"},
            client=FakeClient(),
        )
        self.assertTrue(asyncio.run(log_utils.send_text_alert(actor, "测试", "内容")))
        self.assertIs(sent[0][0], input_entity)

    def test_message_events_sqlite_retention_prunes_older_than_15_days(self):
        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "message_events.sqlite3")
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = db_path
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
                actor = SimpleNamespace(state_file="state_main.json", target_chat_id=-100123456)
                msg = DummyMessage(90100, text="new")
                self.assertTrue(log_utils.record_message_event(
                    actor,
                    msg,
                    text="new",
                    sender=SimpleNamespace(username="player"),
                    event_kind="new",
                    direction="raw",
                ))
                old_time = (datetime.now() - timedelta(days=20)).strftime(log_utils.TIME_FORMAT)
                old_date = old_time[:10]
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        """
                        INSERT INTO message_events (
                            account,event_kind,direction,chat_id,msg_id,is_out,is_game_bot,
                            text,text_hash,created_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        ("main", "new", "raw", -100123456, 90099, 0, 0, "old", "oldhash", old_time),
                    )
                    conn.execute(
                        """
                        INSERT INTO command_ledger (
                            account,chat_id,command_msg_id,command,identity,source,status,
                            sent_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        ("main", -100123456, 90099, ".旧", "主魂", "auto", "sent", old_time, old_time),
                    )
                    conn.execute(
                        """
                        INSERT INTO daily_reward_events (
                            account,event_key,event_date,event_time,identity,command,final,
                            text_hash,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        ("main", "old-event", old_date, "00:00:00", "主魂", ".旧", 1, "old", old_time, old_time),
                    )
                    conn.commit()
                finally:
                    conn.close()

                result = log_utils.prune_message_events_db(retention_days=15, batch_size=100)
                self.assertEqual(result["deleted"]["message_events"], 1)
                self.assertEqual(result["deleted"]["command_ledger"], 1)
                self.assertEqual(result["deleted"]["daily_reward_events"], 1)
                conn = sqlite3.connect(db_path)
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM message_events").fetchone()[0], 1)
                finally:
                    conn.close()
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_unanswered_dotted_command_probe_is_not_overwritten(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            target_chat_id=-100123456,
            _bot_unhealthy_until=0,
        )
        player = SimpleNamespace(username="player")

        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
            db_path = os.path.join(tmpdir, "message_events.sqlite3")
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = db_path
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
                with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
                    first_msg = DummyMessage(90021, text=".洞府")
                    second_msg = DummyMessage(90022, text=".野外历练")
                    first_msg.date = datetime.now(timezone.utc)
                    second_msg.date = datetime.now(timezone.utc)
                    log_utils.record_message_event(
                        actor, first_msg, text=".洞府", sender=player, event_kind="new", direction="raw"
                    )
                    data = log_utils._read_shared_bot_activity()
                    first_epoch = data["command_probe"]["wall_epoch"]

                    log_utils.record_message_event(
                        actor, second_msg, text=".野外历练", sender=player, event_kind="new", direction="raw"
                    )
                    data = log_utils._read_shared_bot_activity()
                    self.assertEqual(data["command_probe"]["command"], ".洞府")
                    self.assertEqual(data["command_probe"]["wall_epoch"], first_epoch)
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_watchdog_defers_while_send_waits_for_shared_stale_bot_activity(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            _bot_unhealthy_until=0,
            _bot_activity_waiting_command=".野外历练",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_path = os.path.join(tmpdir, "bot_activity_shared.json")
            stale_epoch = time.time() - 600
            with open(shared_path, "w", encoding="utf-8") as f:
                json.dump({
                    "accounts": {
                        "main": {"wall_epoch": stale_epoch},
                        "sub": {"wall_epoch": stale_epoch},
                        "xiaohao": {"wall_epoch": stale_epoch},
                    }
                }, f)

            with patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", shared_path):
                status = log_utils.shared_bot_maintenance_status(actor)
                self.assertTrue(status["active"])
                self.assertEqual(status["source"], "shared_activity_stale")
                self.assertTrue(log_utils.watchdog_should_defer_for_bot_maintenance(actor))

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

    def test_pending_feedback_rejects_human_reply_even_with_exact_reply_to(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            target_chat_id=-100123456,
            current_identity="主魂",
            feedback_events={1000: asyncio.Event()},
            feedback_commands={1000: ".切换 主魂"},
            feedback_identities={1000: "主魂"},
            feedback_senders={1000: 42},
            last_feedback_text={},
            last_feedback_msg={},
            identity_usernames={"主魂": ["Weeguu"]},
            avatar_usernames={},
        )
        msg = DummyMessage(1001, text="糊涂啊", reply_to_msg_id=1000)

        self.assertFalse(log_utils.match_pending_feedback_by_id(
            actor,
            1000,
            msg,
            msg.text,
            sender=SimpleNamespace(username="WalterWA2000"),
        ))
        self.assertFalse(actor.feedback_events[1000].is_set())

        bot_text = "你已收回神通，神念重归主魂肉身。"
        with patch.object(log_utils, "record_command_response_for_command_id", return_value=True):
            self.assertTrue(log_utils.match_pending_feedback_by_id(
                actor,
                1000,
                msg,
                bot_text,
                sender=SimpleNamespace(username="fanrenxiuxian_bot"),
            ))
        self.assertTrue(actor.feedback_events[1000].is_set())

    def test_pending_feedback_recovers_strong_beast_result_with_wrong_reply_target(self):
        actor = SimpleNamespace(
            state_file="state_xiaohao.json",
            target_chat_id=-100123456,
            topic_id=None,
            current_identity="主魂",
            feedback_events={1000: asyncio.Event()},
            feedback_commands={1000: ".灵兽互动 大圣"},
            feedback_identities={1000: "主魂"},
            feedback_senders={1000: 42},
            feedback_sent_ts={1000: time.monotonic()},
            last_feedback_text={},
            last_feedback_msg={},
            identity_usernames={"主魂": ["Weeguu"]},
            avatar_usernames={},
        )
        text = "你的灵兽【大圣】正在探险中，暂时无法互动。"
        msg = DummyMessage(1002, text=text, reply_to_msg_id=999)

        with patch.object(log_utils, "record_command_response_for_command_id", return_value=True):
            self.assertTrue(log_utils.match_pending_feedback_by_reply(
                actor,
                msg,
                text,
                candidate_fn=lambda command, body: "互动" in body,
                sender=SimpleNamespace(username="fanrenxiuxian_bot"),
            ))

        self.assertTrue(actor.feedback_events[1000].is_set())
        self.assertEqual(actor.last_feedback_text[1000], text)

    def test_pending_feedback_does_not_recover_unrelated_wrong_reply(self):
        actor = SimpleNamespace(
            state_file="state_xiaohao.json",
            target_chat_id=-100123456,
            topic_id=None,
            current_identity="主魂",
            feedback_events={1000: asyncio.Event()},
            feedback_commands={1000: ".灵兽互动 大圣"},
            feedback_identities={1000: "主魂"},
            feedback_senders={1000: 42},
            feedback_sent_ts={1000: time.monotonic()},
            last_feedback_text={},
            last_feedback_msg={},
            identity_usernames={"主魂": ["Weeguu"]},
            avatar_usernames={},
        )
        text = "灵兽【铁甲龟】领命前往边境巡行，执行【袭营】。"
        msg = DummyMessage(1002, text=text, reply_to_msg_id=999)

        self.assertFalse(log_utils.match_pending_feedback_by_reply(
            actor,
            msg,
            text,
            candidate_fn=lambda command, body: True,
            sender=SimpleNamespace(username="fanrenxiuxian_bot"),
        ))
        self.assertFalse(actor.feedback_events[1000].is_set())

    def test_command_ledger_resolves_retry_attempts_and_heart_trial_event_edits(self):
        actor = SimpleNamespace(
            state_file="state_main.json",
            target_chat_id=-100123456,
        )
        bot = SimpleNamespace(username="fanrenxiuxian_bot")
        old_db = log_utils.MESSAGE_EVENTS_DB_FILE
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                log_utils.MESSAGE_EVENTS_DB_FILE = os.path.join(tmpdir, "message_events.sqlite3")
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False
                first = DummyMessage(1100, text=".切换 素缘子", out=True)
                second = DummyMessage(1101, text=".切换 素缘子", out=True)
                log_utils.record_command_sent(actor, first, first.text, identity="主魂")
                log_utils.record_command_sent(actor, second, second.text, identity="主魂")
                response = DummyMessage(
                    1102,
                    text="切换成功！你的神念已附着在【素缘子】之上。",
                    reply_to_msg_id=1101,
                )
                with patch.object(log_utils, "_record_shared_command_response", return_value=True):
                    self.assertTrue(log_utils.record_command_response_for_command_id(
                        actor, 1101, response, text=response.text, sender=bot
                    ))

                heart = DummyMessage(1201, text=".稳", reply_to_msg_id=1200, out=True)
                log_utils.record_command_sent(
                    actor, heart, heart.text, identity="无咎子", reply_to=1200
                )
                settlement = DummyMessage(1200, text="【坠魔心劫·结算】稳 / 稳 / 稳")
                self.assertTrue(log_utils.record_command_response_for_related_event(
                    actor, settlement, text=settlement.text, sender=bot
                ))

                conn = sqlite3.connect(log_utils.MESSAGE_EVENTS_DB_FILE)
                try:
                    statuses = dict(conn.execute(
                        "SELECT command_msg_id,status FROM command_ledger ORDER BY command_msg_id"
                    ).fetchall())
                finally:
                    conn.close()
                self.assertEqual(statuses[1100], "matched")
                self.assertEqual(statuses[1101], "matched")
                self.assertEqual(statuses[1201], "edited")
            finally:
                log_utils.MESSAGE_EVENTS_DB_FILE = old_db
                log_utils._MESSAGE_EVENTS_SCHEMA_READY = False

    def test_destiny_catches_up_after_primary_window_and_records_only_confirmed_success(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 17, 12, 0, 0)

        actor = Cultivator.__new__(Cultivator)
        state = {"last_destiny_date": ""}
        saved = []
        sent = []
        responses = [
            "【观命结果】今日可定下的命星如下：【贪狼】、【太阴】。",
            "天机紊乱，未能定下命星。",
        ]
        actor.active_atomic_task = None
        actor.dashboard_command_paused = lambda command, identity: False
        actor.daily_one_shot_should_defer = lambda *args, **kwargs: True
        actor.get_avatar_state = lambda avatar: state
        actor.set_avatar_state = lambda avatar, key, value: (state.__setitem__(key, value), saved.append((key, value)))
        actor.response_text = lambda response: str(response or "")

        async def fake_send(identity, command, **kwargs):
            sent.append(command)
            return responses.pop(0)

        async def fake_sleep(seconds):
            return None

        actor.send_and_wait_feedback_identity = fake_send
        with patch.object(intelligent_cultivator, "datetime", FixedDatetime), patch.object(
            intelligent_cultivator.asyncio, "sleep", fake_sleep
        ):
            self.assertEqual(actor._avatar_destiny_wait_seconds("无咎子"), 0)
            asyncio.run(actor._avatar_destiny_check("无咎子"))

        self.assertEqual(sent, [".观命", ".定命 贪狼"])
        self.assertNotIn("last_destiny_date", {k: v for k, v in saved})

        responses.extend([
            "【观命结果】今日可定下的命星如下：【太阴】。",
            "你将今日命轨定在【太阴】。",
        ])
        sent.clear()
        with patch.object(intelligent_cultivator, "datetime", FixedDatetime), patch.object(
            intelligent_cultivator.asyncio, "sleep", fake_sleep
        ):
            asyncio.run(actor._avatar_destiny_check("无咎子"))

        self.assertEqual(sent, [".观命", ".定命 太阴"])
        self.assertEqual(state["last_destiny_date"], "2026-07-17")
        self.assertEqual(state["last_destiny_choice"], "太阴")

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

    def test_manual_not_deep_meditation_reply_clears_stale_guard(self):
        actor = SimpleNamespace(state={
            "in_deep_meditation": True,
            "deep_meditation_end_time": "2099-01-01 00:00:00",
            "deep_meditation_guard_until": "2099-01-01 00:03:00",
            "next_meditation_retry_time": "2099-01-01 00:10:00",
        })

        self.assertTrue(log_utils._manual_record_meditation_reply(
            actor,
            "你并未处于深度闭关之中。",
            "主魂",
        ))

        self.assertFalse(actor.state["in_deep_meditation"])
        self.assertEqual(actor.state["deep_meditation_end_time"], "")
        self.assertEqual(actor.state["deep_meditation_guard_until"], "")
        self.assertEqual(actor.state["next_meditation_retry_time"], "")

    def test_sub_main_deep_meditation_probes_actual_cooldown_when_configured(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.state = {}
        actor.actual_cooldown_probe_commands = {("主魂", ".深度闭关")}
        actor.actual_cooldown_probe_delay_seconds = 0
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, *args, **kwargs):
            sent.append((command, kwargs.get("force_meditation_check")))
            return "你正在深度闭关，预计还需 **6小时40分钟** 即可功成圆满。"

        async def fake_place(reason):
            return None

        actor.send_and_wait_feedback = fake_send
        actor.place_concubine_in_cave = fake_place

        ok = asyncio.run(actor.record_deep_meditation_start(
            "你已进入深度闭关状态，神魂将自行吐纳 **8** 小时。",
            "fixture",
        ))

        self.assertTrue(ok)
        self.assertEqual(sent, [(".查看闭关", True)])
        remaining = common_seconds_until(actor.state["deep_meditation_end_time"])
        self.assertGreater(remaining, 6 * 3600 + 35 * 60)
        self.assertLessEqual(remaining, 6 * 3600 + 40 * 60)

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

    def test_xiaohao_avatar_settle_reuses_existing_check_response(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        actor.state = {"avatars": {"素心子": {}}}
        actor.save_state = lambda: None
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            if command == ".闭关修炼":
                return "你结束闭关，修为有所精进。"
            if command == ".深度闭关":
                return "你已进入深度闭关状态，神魂将自行吐纳 **8小时**。"
            return "unexpected"

        actor.send_and_wait_feedback_identity = fake_send

        wait = asyncio.run(actor._avatar_settle_and_start_deep(
            "素心子",
            initial_check_text="你并未处于深度闭关之中。",
        ))

        self.assertEqual(sent, [("素心子", ".闭关修炼"), ("素心子", ".深度闭关")])
        self.assertEqual(wait, 300)

    def test_xiaohao_avatar_deep_start_without_duration_does_not_verify_with_check(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        actor.state = {"avatars": {"素心子": {}}}
        actor.save_state = lambda: None
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "should not send"

        actor.send_and_wait_feedback_identity = fake_send

        ok = asyncio.run(actor.record_avatar_deep_meditation_start(
            "素心子",
            "你已进入深度闭关状态，神魂将自行吐纳。",
        ))

        self.assertTrue(ok)
        self.assertEqual(sent, [])
        state = actor.get_avatar_state("素心子")
        self.assertTrue(state["in_deep_meditation"])
        self.assertTrue(state["deep_meditation_guard_until"])

    def test_xiaohao_avatar_loop_does_not_check_during_cached_deep_meditation(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "avatars": {
                "素心子": {
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": future,
                    "deep_meditation_guard_until": "",
                    "meditation_restart_pending": False,
                    "next_meditation_retry_time": "",
                }
            }
        }
        actor.save_state = lambda: None
        actor.is_running = True
        actor._avatar_loop_count = 0
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        sent = []
        sleeps = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "should not send"

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            raise asyncio.CancelledError()

        actor.send_and_wait_feedback_identity = fake_send
        old_sleep = cultivator_xiaohao.asyncio.sleep
        cultivator_xiaohao.asyncio.sleep = fake_sleep
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(actor.run_avatar_meditation_loop("素心子"))
        finally:
            cultivator_xiaohao.asyncio.sleep = old_sleep

        self.assertEqual(sent, [])
        self.assertTrue(sleeps)
        self.assertTrue(actor.get_avatar_state("素心子")["deep_meditation_guard_until"])

    def test_xiaohao_avatar_loop_records_meditation_from_message_response(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {
            "avatars": {
                "缘生子": {
                    "in_deep_meditation": False,
                    "deep_meditation_end_time": "",
                    "deep_meditation_guard_until": "",
                    "meditation_restart_pending": False,
                    "next_meditation_retry_time": "",
                }
            }
        }
        actor.save_state = lambda: None
        actor.is_running = True
        actor._avatar_loop_count = 0
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        sent = []
        sleeps = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return DummyMessage(
                8101,
                text="[Avatar: 缘生子]\n你正在深度闭关，预计还需 **5小时40分钟58秒** 即可功成圆满。",
            )

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            raise asyncio.CancelledError()

        actor.send_and_wait_feedback_identity = fake_send
        old_sleep = cultivator_xiaohao.asyncio.sleep
        cultivator_xiaohao.asyncio.sleep = fake_sleep
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(actor.run_avatar_meditation_loop("缘生子"))
        finally:
            cultivator_xiaohao.asyncio.sleep = old_sleep

        state = actor.get_avatar_state("缘生子")
        self.assertEqual(sent, [("缘生子", ".查看闭关")])
        self.assertTrue(sleeps)
        self.assertTrue(state["in_deep_meditation"])
        self.assertTrue(state["deep_meditation_end_time"])
        self.assertTrue(state["deep_meditation_guard_until"])
        self.assertFalse(state["meditation_restart_pending"])

    def test_xiaohao_avatar_loop_ignores_stale_restart_pending_with_future_end(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["素心子"]
        actor.avatar_nicknames = {"素心子": ""}
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "avatars": {
                "素心子": {
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": future,
                    "deep_meditation_guard_until": "",
                    "meditation_restart_pending": True,
                    "meditation_restart_mode": "",
                    "next_meditation_retry_time": "",
                }
            }
        }
        actor.save_state = lambda: None
        actor.is_running = True
        actor._avatar_loop_count = 0
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        sent = []
        sleeps = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "should not send"

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            raise asyncio.CancelledError()

        actor.send_and_wait_feedback_identity = fake_send
        old_sleep = cultivator_xiaohao.asyncio.sleep
        cultivator_xiaohao.asyncio.sleep = fake_sleep
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(actor.run_avatar_meditation_loop("素心子"))
        finally:
            cultivator_xiaohao.asyncio.sleep = old_sleep

        state = actor.get_avatar_state("素心子")
        self.assertEqual(sent, [])
        self.assertTrue(sleeps)
        self.assertFalse(state["meditation_restart_pending"])
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
        self.assertEqual(main_state["deep_meditation_guard_until"], add_seconds_str(future, 180))
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

    def test_concubine_voyage_only_main_main_uses_moon_route(self):
        identities = {
            "main": ["主魂", "无咎子", "缘生子", "素缘子"],
            "sub": ["主魂", "厚土", "缘生子", "寻真子"],
            "xiaohao": ["主魂", "问心子", "素心子", "缘生子"],
        }
        for account, names in identities.items():
            actor = DummyConcubine()
            actor.account_key = account
            for identity in names:
                enabled = account == "main" and identity == "主魂"
                with self.subTest(account=account, identity=identity):
                    self.assertEqual(actor.concubine_voyage_enabled(identity), enabled)
                    self.assertEqual(actor.concubine_voyage_auto_start_enabled(identity), enabled)

        main = DummyConcubine()
        main.account_key = "main"
        self.assertEqual(main.concubine_voyage_command("主魂"), ".侍妾远航 月殿寻痕")
        self.assertEqual(main.concubine_voyage_cooldown("主魂"), 6 * 3600)
        self.assertIn(
            ".侍妾远航 *",
            log_utils.command_control_candidate_keys(".侍妾远航 月殿寻痕"),
        )

    def test_main_concubine_chain_settles_due_voyage_before_target_search(self):
        actor = DummyConcubine()
        actor.account_key = "main"
        actor.state.update({
            "concubine_name": "慕沛灵",
            "target_concubine_found": False,
            "concubine_voyage_active": True,
            "next_concubine_voyage_time": "",
        })
        calls = []
        actor.align_concubine_chain_cooldowns = lambda identity="主魂": True

        async def fake_return(identity="主魂", **kwargs):
            calls.append("return")
            actor.state["concubine_voyage_active"] = False
            return True

        async def fake_search(identity="主魂"):
            calls.append("search")
            return False

        actor.execute_concubine_voyage_return = fake_return
        actor.execute_target_concubine_search = fake_search

        self.assertFalse(asyncio.run(actor.execute_concubine_chain()))
        self.assertEqual(calls, ["return", "search"])

    def test_concubine_voyage_start_sends_only_main_main_moon_route(self):
        actor = DummyConcubine()
        actor.account_key = "main"
        actor.dashboard_command_paused = lambda command, identity="": False
        actor.recent_concubine_status_mismatch = lambda identity="主魂": False
        actor.record_concubine_voyage_response = lambda *args, **kwargs: True
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            return None, "侍妾已启航，前往月殿寻痕。", False

        actor._send_concubine_identity_command = fake_send
        self.assertTrue(asyncio.run(actor.execute_concubine_voyage_start("主魂")))
        self.assertEqual(sent, [("主魂", ".侍妾远航 月殿寻痕")])

        actor.account_key = "sub"
        actor.state["next_concubine_voyage_time"] = ""
        actor.state["concubine_voyage_active"] = False
        self.assertFalse(asyncio.run(actor.execute_concubine_voyage_start("主魂")))
        self.assertEqual(sent, [("主魂", ".侍妾远航 月殿寻痕")])

    def test_dashboard_shows_voyage_only_for_main_main(self):
        seen = []
        for account in ("main", "sub", "xiaohao"):
            for panel in build_command_panels(account, {"avatars": {}}):
                identity = panel.get("identity") or "主魂"
                commands = [row.get("command", "") for row in panel.get("commands", [])]
                voyage_commands = [cmd for cmd in commands if cmd.startswith(".侍妾远航")]
                if voyage_commands:
                    seen.append((account, identity, voyage_commands))

        self.assertEqual(seen, [("main", "主魂", [".侍妾远航 月殿寻痕"])])

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

    def test_target_concubine_search_disabled_by_default(self):
        actor = DummyConcubine()
        delattr(actor, "target_concubine_search_enabled_flag")
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            return None, "", False

        actor._send_concubine_identity_command = fake_send

        self.assertFalse(actor.target_concubine_enabled("主魂"))
        self.assertEqual(actor.target_concubine_identities(), [])
        self.assertTrue(asyncio.run(actor.execute_target_concubine_search("主魂")))
        self.assertEqual(sent, [])
        self.assertFalse(actor.record_concubine_search_response(
            "**【红尘偶遇】**\n你遇见了 **【霓裳】**。",
            identity="主魂",
            source="fixture",
        ))
        self.assertFalse(actor.record_concubine_dismiss_response(
            "你与 **霓裳** 缘分已尽。",
            identity="主魂",
            source="fixture",
        ))

    def test_target_concubine_search_records_wrong_and_dismisses(self):
        actor = DummyConcubine()
        actor.avatars = ["无咎子"]
        actor.state["avatars"] = {"无咎子": {}}
        actor.get_avatar_state = lambda identity: actor.state.setdefault("avatars", {}).setdefault(identity, {})

        search_text = """
[Avatar: 无咎子]
**【红尘偶遇】**
你在人海中蓦然回首，与一位名为 **【霓裳】** 的女子四目相对，情愫暗生。
她愿随你共踏仙途，从此成为你的侍妾。快使用 `.我的侍妾` 查看吧！
"""
        self.assertTrue(actor.record_concubine_search_response(search_text, identity="无咎子", source="fixture"))
        state = actor.get_avatar_state("无咎子")
        self.assertEqual(state["concubine_name"], "霓裳")
        self.assertFalse(state["target_concubine_found"])
        self.assertGreater(seconds_until(state["next_concubine_search_time"]), 7100)

        dismiss_text = """
[Avatar: 无咎子]
你与 **霓裳** 缘分已尽，从此一别两宽，各自安好。
你现在可以再次于`.红尘寻缘`了。
"""
        self.assertTrue(actor.record_concubine_dismiss_response(dismiss_text, identity="无咎子", source="fixture"))
        self.assertEqual(state["concubine_name"], "")
        self.assertEqual(state["last_concubine_dismissed_name"], "霓裳")

    def test_target_concubine_search_records_no_match_cooldown(self):
        actor = DummyConcubine()

        text = "你踏遍万千红尘，却终是镜花水月，未能寻得有缘之人。"

        self.assertTrue(actor.record_concubine_search_response(text, identity="主魂", source="fixture"))
        self.assertEqual(actor.state["concubine_name"], "")
        self.assertFalse(actor.state["target_concubine_found"])
        self.assertGreater(seconds_until(actor.state["next_concubine_search_time"]), 7100)

    def test_target_concubine_search_existing_partner_marks_for_dismissal(self):
        actor = DummyConcubine()

        text = "你已拥有红颜知己，不可三心二意。若想另寻新欢请先使用 .遣散侍妾。"

        self.assertTrue(actor.record_concubine_search_response(text, identity="主魂", source="fixture"))
        self.assertEqual(actor.state["concubine_name"], "红颜知己")
        self.assertFalse(actor.state["target_concubine_found"])
        self.assertEqual(actor.state["next_concubine_search_time"], "")
        self.assertIn("existing_partner", actor.state["last_concubine_search_result"])

    def test_target_concubine_auto_search_dismisses_non_target(self):
        actor = DummyConcubine()
        actor.avatars = ["无咎子"]
        actor.state["avatars"] = {"无咎子": {}}
        actor.get_avatar_state = lambda identity: actor.state.setdefault("avatars", {}).setdefault(identity, {})
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            if command == ".我的侍妾":
                return None, "[Avatar: 无咎子]\n你还没有侍妾。", False
            if command == ".红尘寻缘":
                return None, (
                    "[Avatar: 无咎子]\n**【红尘偶遇】**\n"
                    "你在人海中蓦然回首，与一位名为 **【霓裳】** 的女子四目相对。\n"
                    "她愿随你共踏仙途，从此成为你的侍妾。"
                ), False
            if command == ".遣散侍妾":
                return None, "[Avatar: 无咎子]\n你与 **霓裳** 缘分已尽，从此一别两宽，各自安好。", False
            return None, "", False

        actor._send_concubine_identity_command = fake_send

        self.assertFalse(asyncio.run(actor.execute_target_concubine_search("无咎子")))

        self.assertEqual(sent, [
            ("无咎子", ".我的侍妾"),
            ("无咎子", ".红尘寻缘"),
            ("无咎子", ".遣散侍妾"),
        ])
        state = actor.get_avatar_state("无咎子")
        self.assertEqual(state["concubine_name"], "")
        self.assertFalse(state["target_concubine_found"])
        self.assertGreater(seconds_until(state["next_concubine_search_time"]), 7100)

    def test_target_concubine_avatar_unmarked_status_dismisses_before_search(self):
        actor = DummyConcubine()
        actor.avatars = ["无咎子"]
        actor.state["avatars"] = {"无咎子": {}}
        actor.get_avatar_state = lambda identity: actor.state.setdefault("avatars", {}).setdefault(identity, {})
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            if command == ".我的侍妾":
                return None, "**你的红尘道侣: 【绿袖】** (状态: 随行中)", False
            if command == ".遣散侍妾":
                return None, "你与 **绿袖** 缘分已尽，从此一别两宽，各自安好。", False
            if command == ".红尘寻缘":
                return None, "你踏遍万千红尘，却终是镜花水月，未能寻得有缘之人。", False
            return None, "", False

        actor._send_concubine_identity_command = fake_send

        self.assertFalse(asyncio.run(actor.execute_target_concubine_search("无咎子")))

        self.assertEqual(sent, [
            ("无咎子", ".我的侍妾"),
            ("无咎子", ".遣散侍妾"),
            ("无咎子", ".红尘寻缘"),
        ])
        state = actor.get_avatar_state("无咎子")
        self.assertEqual(state["concubine_name"], "")
        self.assertFalse(state["target_concubine_found"])
        self.assertGreater(seconds_until(state["next_concubine_search_time"]), 7100)

    def test_target_concubine_auto_search_dismisses_existing_partner_prompt(self):
        actor = DummyConcubine()
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            if command == ".我的侍妾":
                return None, "你还没有侍妾。", False
            if command == ".红尘寻缘":
                return None, "你已拥有红颜知己，不可三心二意。若想另寻新欢请先使用 `.遣散侍妾`。", False
            if command == ".遣散侍妾":
                return None, "你与 **红颜知己** 缘分已尽，从此一别两宽，各自安好。", False
            return None, "", False

        actor._send_concubine_identity_command = fake_send

        self.assertFalse(asyncio.run(actor.execute_target_concubine_search("主魂")))

        self.assertEqual(sent, [
            ("主魂", ".我的侍妾"),
            ("主魂", ".红尘寻缘"),
            ("主魂", ".遣散侍妾"),
        ])
        self.assertEqual(actor.state["concubine_name"], "")
        self.assertFalse(actor.state["target_concubine_found"])

    def test_target_concubine_avatar_unmarked_existing_partner_prompt_dismisses(self):
        actor = DummyConcubine()
        actor.avatars = ["无咎子"]
        actor.state["avatars"] = {"无咎子": {}}
        actor.get_avatar_state = lambda identity: actor.state.setdefault("avatars", {}).setdefault(identity, {})
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            if command == ".我的侍妾":
                return None, "你还没有侍妾。", False
            if command == ".红尘寻缘":
                return None, "你已觅得红颜知己，不可三心二意。若想另寻新欢，请先使用`.遣散侍妾`。", False
            if command == ".遣散侍妾":
                return None, "你与 **红颜知己** 缘分已尽，从此一别两宽，各自安好。", False
            return None, "", False

        actor._send_concubine_identity_command = fake_send

        self.assertFalse(asyncio.run(actor.execute_target_concubine_search("无咎子")))

        self.assertEqual(sent, [
            ("无咎子", ".我的侍妾"),
            ("无咎子", ".红尘寻缘"),
            ("无咎子", ".遣散侍妾"),
        ])
        state = actor.get_avatar_state("无咎子")
        self.assertEqual(state["concubine_name"], "")
        self.assertFalse(state["target_concubine_found"])

    def test_target_concubine_search_waits_for_edited_no_match(self):
        actor = DummyConcubine()
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.target_chat_id = -100
        sent = []
        initial = SimpleNamespace(
            id=1001,
            text="你消耗了 **100** 灵石和 **1000** 修为，于红尘俗世中开启了一段寻缘之旅...",
        )
        edited = SimpleNamespace(
            id=1001,
            text="你踏遍万千红尘，却终是镜花水月，未能寻得有缘之人。",
        )

        class FakeClient:
            async def get_messages(self, chat_id, ids):
                self.last_request = (chat_id, ids)
                return edited

        async def fake_sleep(seconds):
            return None

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command, kwargs))
            if command == ".我的侍妾":
                return None, "", False
            if command == ".红尘寻缘":
                return initial, initial.text, False
            return None, "", False

        actor.client = FakeClient()
        actor._send_concubine_identity_command = fake_send

        with patch("concubine_features.asyncio.sleep", new=fake_sleep):
            self.assertFalse(asyncio.run(actor.execute_target_concubine_search("主魂")))

        self.assertEqual([item[1] for item in sent], [".我的侍妾", ".红尘寻缘"])
        self.assertTrue(sent[0][2].get("suppress_no_response_alert"))
        self.assertTrue(sent[1][2].get("return_response_msg"))
        self.assertEqual(actor.state["concubine_name"], "")
        self.assertFalse(actor.state["target_concubine_found"])
        self.assertEqual(actor.state["last_concubine_search_result"], "auto_search")
        self.assertGreater(seconds_until(actor.state["next_concubine_search_time"]), 7100)

    def test_target_concubine_auto_search_stops_when_nangong_wan_found(self):
        actor = DummyConcubine()
        actor.state["target_concubine_name"] = "南宫婉"
        actor.dashboard_command_paused = lambda command, identity="主魂": False
        actor.identity_pause_seconds = lambda identity="主魂": 0
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            if command == ".我的侍妾":
                return None, "你还没有侍妾。", False
            if command == ".红尘寻缘":
                return None, (
                    "**【红尘偶遇】**\n"
                    "你在人海中蓦然回首，与一位名为 **【南宫婉】** 的女子四目相对。\n"
                    "她愿随你共踏仙途，从此成为你的侍妾。"
                ), False
            return None, "", False

        actor._send_concubine_identity_command = fake_send

        self.assertTrue(asyncio.run(actor.execute_target_concubine_search("主魂")))

        self.assertEqual(sent, [("主魂", ".我的侍妾"), ("主魂", ".红尘寻缘")])
        self.assertEqual(actor.state["concubine_name"], "南宫婉")
        self.assertTrue(actor.state["target_concubine_found"])
        self.assertEqual(actor.state["next_concubine_search_time"], "")

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
        self.assertEqual(actor.state["last_concubine_status_mismatch"], "")

    def test_concubine_name_mismatch_no_longer_blocks_status_sync(self):
        actor = DummyConcubine()
        actor.state["concubine_name"] = "若兰"
        status = """
**你的道心侍妾: 【柳玉】** (状态: 随行中)

**【第二期机缘】**
- 入梦寻图冷却: 479分钟
- 共历心劫冷却: 可施展
- 天机代卜冷却: 719分钟
"""

        self.assertTrue(actor.concubine_status_matches_identity(status, "主魂"))
        self.assertEqual(actor.state["concubine_name"], "柳玉")
        self.assertEqual(actor.state["last_concubine_status_mismatch"], "")

    def test_loose_main_voyage_return_syncs_by_concubine_name(self):
        actor = DummyConcubine()
        actor.state["concubine_name"] = "瑶光"
        actor.state["concubine_voyage_active"] = True
        actor.state["next_concubine_voyage_time"] = now_str()
        text = """
**【乱星海远航·归】**
侍妾【瑶光】已自 **冒险** 航线归来，向你呈上收获：
- 修为 **+392**
- 灵石 **+127**
"""

        self.assertEqual(actor.identity_for_concubine_voyage_text(text), "主魂")
        self.assertTrue(actor.record_passive_concubine_voyage_response(text))
        self.assertFalse(actor.state["concubine_voyage_active"])
        self.assertEqual(actor.state["next_concubine_voyage_time"], "")

    def test_loose_voyage_return_without_owner_is_ignored(self):
        actor = DummyConcubine()
        actor.state["concubine_name"] = "慕沛灵"
        actor.state["concubine_voyage_active"] = False
        text = """
**【乱星海远航·归】**
侍妾【陌生人】已自 **冒险** 航线归来，向你呈上收获：
- 修为 **+392**
"""

        self.assertEqual(actor.identity_for_concubine_voyage_text(text), "")
        self.assertFalse(actor.record_passive_concubine_voyage_response(text))

    def test_concubine_voyage_ignores_yuanying_start_text(self):
        actor = DummyConcubine()
        actor.avatars = ["缘生子"]
        actor.state["avatars"] = {"缘生子": {}}
        text = """
[Avatar: 缘生子]
你心念一动，丹田中的元婴化作一道流光飞出，消失在天际。
它将在外云游 **8** 小时，为你寻觅天地奇珍。下一次发言时若已归来，将自动结算收获。
"""

        self.assertEqual(actor.identity_for_concubine_voyage_text(text), "")
        self.assertFalse(actor.record_passive_concubine_voyage_response(text))

    def test_concubine_voyage_ignores_beast_recall_return_text(self):
        actor = DummyConcubine()
        due = now_str()
        actor.state["concubine_voyage_active"] = True
        actor.state["next_concubine_voyage_time"] = due
        text = """
你已提前召回放养中的灵兽【铁甲龟 (之贰)】。
原本还需 **2小时53分钟15秒** 才会自行归来。
提前召回不会结算放养收获。
"""

        self.assertEqual(actor.identity_for_concubine_voyage_text(text), "")
        self.assertFalse(actor.record_passive_concubine_voyage_response(text))
        self.assertTrue(actor.state["concubine_voyage_active"])
        self.assertEqual(actor.state["next_concubine_voyage_time"], due)

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
        actor.avatar_usernames = {"ding303": "寻真子"}
        actor.my_info = SimpleNamespace(username="my_self")

        self.assertTrue(actor.is_formation_pending("【周天星斗大阵-启】正在布设大阵，尚需 2 位道友助阵。"))
        self.assertTrue(actor.is_formation_success("【周天星斗大阵-成】大阵已成，星辉流转。"))
        invite = "【周天星斗大阵-启】@Ding303 正在布设大阵，尚需 2 位道友助阵。"
        own_invite = "【周天星斗大阵-启】@my_self 正在布设大阵，尚需 2 位道友助阵。"
        self.assertEqual(actor.formation_invite_actor_identity(invite), "寻真子")
        self.assertTrue(actor.is_external_formation_invite(invite))
        self.assertTrue(actor.is_own_formation_invite(own_invite))
        self.assertFalse(actor.is_external_formation_invite(own_invite))

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

    def test_spirit_tree_harvest_respects_irrigation_dashboard_pause(self):
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
        actor.dashboard_command_paused = lambda command, identity="": command == ".灵树灌溉"
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command, kwargs))
            return "should not send"

        async def run_case():
            actor.startup_done = asyncio.Event()
            actor.startup_done.set()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.send_and_wait_feedback_identity = fake_send
            await actor.execute_spirit_tree_harvest_once("fixture", identity="主魂")

        asyncio.run(run_case())

        self.assertEqual(sent, [])
        self.assertFalse(actor.state["spirit_tree_harvest_pending"])
        self.assertFalse(actor.state.get("spirit_tree_harvest_attempted_in_mature_period", False))

    def test_spirit_tree_mature_does_not_schedule_harvest_when_irrigation_paused(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "spirit_tree_harvest_pending": False,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": command == ".灵树灌溉"
        scheduled = []
        actor.schedule_spirit_tree_harvest_once = lambda reason="mature", identity="主魂": scheduled.append((identity, reason))

        matched = actor.maybe_record_spirit_tree_passive_message(
            None,
            "灵果已完全成熟，采摘期开启，将持续 **24小时**。",
            source="fixture",
            identity="主魂",
        )

        self.assertTrue(matched)
        self.assertEqual(scheduled, [])
        self.assertFalse(actor.state["spirit_tree_harvest_pending"])

    def test_spirit_tree_harvest_skips_non_luoyun_identity(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.identity_sect_names = {"主魂": "凌霄宫"}
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
        actor.dashboard_command_paused = lambda command, identity="": False
        sent = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command, kwargs))
            return "should not send"

        async def run_case():
            actor.startup_done = asyncio.Event()
            actor.startup_done.set()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.send_and_wait_feedback_identity = fake_send
            await actor.execute_spirit_tree_harvest_once("fixture", identity="主魂")

        asyncio.run(run_case())

        self.assertEqual(sent, [])
        self.assertFalse(actor.state["spirit_tree_harvest_pending"])
        self.assertFalse(actor.state.get("spirit_tree_harvest_attempted_in_mature_period", False))

    def test_spirit_tree_mature_does_not_schedule_harvest_for_non_luoyun_identity(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.identity_sect_names = {"主魂": "凌霄宫"}
        actor.state = {
            "spirit_tree_status": "灌溉期",
            "spirit_tree_harvest_pending": False,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": False
        scheduled = []
        actor.schedule_spirit_tree_harvest_once = lambda reason="mature", identity="主魂": scheduled.append((identity, reason))

        matched = actor.maybe_record_spirit_tree_passive_message(
            None,
            "灵果已完全成熟，采摘期开启，将持续 **24小时**。",
            source="fixture",
            identity="主魂",
        )

        self.assertTrue(matched)
        self.assertEqual(scheduled, [])
        self.assertFalse(actor.state["spirit_tree_harvest_pending"])

    def test_spirit_tree_expired_harvest_lock_does_not_block_future_mature_periods(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "next_spirit_tree_harvest_time": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        self.assertEqual(actor.spirit_tree_harvest_lock_until("主魂"), "")
        self.assertFalse(actor.spirit_tree_harvest_locked("主魂"))
        self.assertEqual(actor.state["next_spirit_tree_harvest_time"], "")

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

    def test_spirit_tree_harvest_non_sect_reply_is_known_reject(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.state = {
            "spirit_tree_status": "成熟采摘期",
            "spirit_tree_mature_until": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_spirit_tree_irrigation_time": "",
            "spirit_tree_irrigation_times": {},
            "spirit_tree_harvest_pending": True,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        self.assertTrue(actor.record_spirit_tree_harvest_response(
            "非本宗弟子，不得靠近灵眼之树。",
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

    def test_retired_spirit_tree_irrigation_is_not_time_critical(self):
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

        self.assertFalse(actor.time_critical_identity_command(".灵树灌溉"))
        self.assertEqual(actor.time_critical_identity_wait("缘生子", exclude_command=".抚摸法宝 青竹蜂云剑（神雷版）"), -1)
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

    def test_avatar_min_cd_ignores_retired_spirit_tree_schedule(self):
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

        self.assertEqual(wait, 1800)

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

    def test_spirit_tree_status_alarm_schedules_guard_opportunities(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.avatar_features = {"缘生子": {"spirit_tree_irrigation": True}}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        scheduled = []
        actor.schedule_spirit_tree_guard_opportunities = (
            lambda reason="invasion", preferred_identity=None: scheduled.append((reason, preferred_identity))
        )
        text = """
**【落云宗 · 灵眼之树】**
⚙️ **当前玩法**: 云梦灵眼定脉
⚔️ **警报**: 古剑门入侵中！大阵耐久: 15324
🛡️ **护山底蕴**: 999 | 🔥 **反击积蓄**: 999 | 🧱 **守山次数**: 518
请速用 `.协同守山`！
🏛️ **三派异动**: 【古剑门·攻山夺枝】
   古剑门趁灵树将熟，突袭山门，试图强夺本轮枝果。
"""

        self.assertTrue(actor.spirit_tree_text_indicates_invasion(text))
        self.assertTrue(actor.maybe_record_spirit_tree_passive_message(
            None,
            text,
            source="fixture",
            identity="主魂",
        ))
        self.assertEqual(actor.state["spirit_tree_invasion_status"], "古剑门来袭")
        self.assertTrue(actor.state["spirit_tree_guard_pending"])
        self.assertEqual(scheduled, [("fixture", "主魂")])

    def test_spirit_tree_three_sect_disturbance_alone_does_not_schedule_guard(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {}
        actor.avatar_usernames = {"kulipabp": "缘生子"}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        scheduled = []
        actor.schedule_spirit_tree_guard_opportunities = (
            lambda reason="invasion", preferred_identity=None: scheduled.append((reason, preferred_identity))
        )
        text = """
**【落云宗 · 灵眼之树】**
⚙️ **当前玩法**: 云梦灵眼定脉
🏛️ **三派异动**: 【古剑门·攻山夺枝】
   古剑门趁灵树将熟，突袭山门，试图强夺本轮枝果。
"""

        self.assertFalse(actor.spirit_tree_text_indicates_invasion(text))
        self.assertFalse(actor.maybe_record_spirit_tree_passive_message(
            None,
            text,
            source="fixture",
            identity="主魂",
        ))
        self.assertFalse(actor.state.get("spirit_tree_guard_pending", False))
        self.assertEqual(scheduled, [])

    def test_spirit_tree_guard_success_uses_short_retry(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        started = datetime.now()

        outcome = actor.record_spirit_tree_guard_response(
            "**【守山成功】**\n你消耗了 **200** 点修为，为护山大阵注入了精纯的灵力！\n🛡️ **大阵修复**: +60 耐久",
            identity="主魂",
        )

        self.assertEqual(outcome, "success")
        retry_at = datetime.strptime(actor.state["spirit_tree_guard_times"]["主魂"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(retry_at, started + timedelta(minutes=4, seconds=55))
        self.assertLessEqual(retry_at, datetime.now() + timedelta(minutes=5, seconds=5))
        self.assertEqual(actor.state["next_spirit_tree_guard_time"], actor.state["spirit_tree_guard_times"]["主魂"])
        self.assertTrue(actor.state["spirit_tree_guard_pending"])

    def test_spirit_tree_guard_cooldown_is_per_identity(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None
        started = datetime.now()

        self.assertEqual(actor.record_spirit_tree_guard_response("**【守山成功】**\n🛡️ **大阵修复**: +60 耐久", identity="主魂"), "success")
        self.assertEqual(actor.record_spirit_tree_guard_response("[Avatar: 缘生子]\n你刚刚注入过灵力，经脉尚需调息！\n请在 **35秒** 后再来守山。", identity="缘生子"), "cooldown")

        times = actor.state["spirit_tree_guard_times"]
        self.assertIn("主魂", times)
        self.assertIn("缘生子", times)
        self.assertEqual(actor.state["next_spirit_tree_guard_time"], times["主魂"])
        avatar_retry = datetime.strptime(times["缘生子"], "%Y-%m-%d %H:%M:%S")
        self.assertGreaterEqual(avatar_retry, started + timedelta(seconds=30))
        self.assertLessEqual(avatar_retry, datetime.now() + timedelta(seconds=40))

    def test_spirit_tree_guard_zero_second_cooldown_retries_immediately(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.state = {"avatars": {"缘生子": {}}}
        actor.save_state = lambda: None

        outcome = actor.record_spirit_tree_guard_response(
            "你刚刚注入过灵力，经脉尚需调息！\n请在 **0秒** 后再来守山。",
            identity="主魂",
        )

        self.assertEqual(outcome, "cooldown")
        retry_at = datetime.strptime(actor.state["spirit_tree_guard_times"]["主魂"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(retry_at, datetime.now() + timedelta(seconds=3))

    def test_spirit_tree_guard_no_invasion_clears_pending_without_long_cd(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.state = {
            "spirit_tree_invasion_status": "古剑门来袭",
            "spirit_tree_guard_pending": True,
            "next_spirit_tree_guard_time": (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            "spirit_tree_guard_times": {
                "主魂": (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
                "缘生子": (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None

        outcome = actor.record_spirit_tree_guard_response("当前并无外敌入侵，无需加固大阵。", identity="主魂")

        self.assertEqual(outcome, "no_invasion")
        self.assertEqual(actor.state["spirit_tree_invasion_status"], "")
        self.assertFalse(actor.state["spirit_tree_guard_pending"])
        self.assertEqual(actor.state["spirit_tree_guard_times"], {})
        self.assertEqual(actor.state["next_spirit_tree_guard_time"], "")
        self.assertFalse(log_utils.command_send_precheck(actor, ".协同守山", identity="主魂"))

    def test_spirit_tree_guard_runs_while_avatar_deep_meditation_protected(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_features = {"缘生子": {"spirit_tree_irrigation": True}}
        actor.state = {
            "spirit_tree_invasion_status": "古剑门来袭",
            "spirit_tree_guard_pending": True,
            "avatars": {
                "缘生子": {
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": (datetime.now() + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S"),
                    "deep_meditation_guard_until": (datetime.now() + timedelta(hours=6, minutes=3)).strftime("%Y-%m-%d %H:%M:%S"),
                }
            },
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.active_atomic_task = None
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        actor.pause_event = asyncio.Event()
        actor.pause_event.set()
        sent = []
        scheduled = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "**【守山成功】**\n你消耗了 **200** 点修为，为护山大阵注入了精纯的灵力！\n🛡️ **大阵修复**: +60 耐久"

        actor.send_and_wait_feedback_identity = fake_send
        actor.schedule_spirit_tree_guard_once = (
            lambda reason="invasion", identity="主魂", delay_seconds=0: scheduled.append((reason, identity, delay_seconds))
        )

        asyncio.run(actor.execute_spirit_tree_guard_once("fixture", identity="缘生子"))

        self.assertEqual(sent, [("缘生子", ".协同守山")])
        self.assertEqual(actor.state["spirit_tree_guard_times"].keys(), {"缘生子"})
        self.assertEqual(scheduled[0][1], "缘生子")

    def test_spirit_tree_guard_precheck_blocks_before_identity_send(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.state = {
            "spirit_tree_invasion_status": "古剑门来袭",
            "spirit_tree_guard_pending": True,
            "avatars": {"缘生子": {}},
        }
        actor.save_state = lambda: None
        actor.identity_pause_seconds = lambda identity: 0
        actor.active_atomic_task = None
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        actor.pause_event = asyncio.Event()
        actor.pause_event.set()
        sent = []
        prechecked = []

        async def fake_send(identity, command, *args, **kwargs):
            sent.append((identity, command))
            return "should not send"

        actor.send_and_wait_feedback_identity = fake_send
        old_precheck = intelligent_cultivator.command_send_precheck
        intelligent_cultivator.command_send_precheck = (
            lambda actor_arg, command, logger=None, identity=None: prechecked.append((command, identity)) and False
        )
        try:
            asyncio.run(actor.execute_spirit_tree_guard_once("fixture", identity="缘生子"))
        finally:
            intelligent_cultivator.command_send_precheck = old_precheck

        self.assertEqual(prechecked, [(".协同守山", "缘生子")])
        self.assertEqual(sent, [])

    def test_sub_identity_send_skips_dashboard_disabled_command_before_switch(self):
        async def run_case():
            actor = SubCultivator.__new__(SubCultivator)
            actor.avatars = ["寻真子"]
            actor._current_identity = "厚土"
            actor._main_confirmed = False
            actor.state = {"current_identity": "厚土", "avatars": {"寻真子": {}}}
            actor.state_file = "state_sub.json"
            actor.config = {}
            actor.client = None
            actor.my_info = SimpleNamespace(first_name="SubFixture")
            actor.avatar_send_lock = asyncio.Lock()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.save_state = lambda: None
            actor.should_wait_for_atomic_task = lambda *args, **kwargs: False
            actor.wait_while_identity_paused = lambda *args, **kwargs: asyncio.sleep(0, result=True)
            actor.time_critical_identity_command = lambda command: False

            async def fail_raw(command, *args, **kwargs):
                raise AssertionError(f"should not send raw command: {command}")

            actor._send_and_wait_feedback_raw = fail_raw
            result = await actor.send_and_wait_feedback_identity(
                "寻真子",
                ".启阵",
                max_retries=0,
            )
            return result, actor.current_identity, getattr(actor, "_last_command_guard_block", {})

        with tempfile.TemporaryDirectory() as tmpdir:
            controls_path = os.path.join(tmpdir, "command_controls.json")
            with open(controls_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sub": {
                        "寻真子": {
                            ".启阵": {
                                "disabled": True,
                                "command": ".启阵",
                            }
                        }
                    }
                }, f, ensure_ascii=False)

            log_utils._COMMAND_CONTROLS_CACHE["mtime"] = None
            log_utils._COMMAND_CONTROLS_CACHE["data"] = {}
            try:
                with patch.object(log_utils, "COMMAND_CONTROL_FILE", controls_path):
                    result, current_identity, block = asyncio.run(run_case())
            finally:
                log_utils._COMMAND_CONTROLS_CACHE["mtime"] = None
                log_utils._COMMAND_CONTROLS_CACHE["data"] = {}

        self.assertIsNone(result)
        self.assertEqual(current_identity, "厚土")
        self.assertEqual(block.get("reason"), "dashboard_disabled")
        self.assertEqual(block.get("identity"), "寻真子")

    def test_sub_identity_waits_for_bot_activity_before_avatar_lock(self):
        async def run_case():
            actor = SubCultivator.__new__(SubCultivator)
            actor.avatars = ["寻真子"]
            actor._current_identity = "厚土"
            actor._main_confirmed = False
            actor.state = {"current_identity": "厚土", "avatars": {"寻真子": {}}}
            actor.state_file = "state_sub.json"
            actor.config = {}
            actor.client = None
            actor.my_info = SimpleNamespace(first_name="SubFixture")
            actor.avatar_send_lock = asyncio.Lock()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.save_state = lambda: None
            actor.should_wait_for_atomic_task = lambda *args, **kwargs: False
            actor.wait_while_identity_paused = lambda *args, **kwargs: asyncio.sleep(0, result=True)
            actor.time_critical_identity_command = lambda command: False
            raw_calls = []
            wait_calls = []

            async def fail_raw(command, *args, **kwargs):
                raw_calls.append(command)
                raise AssertionError(f"should not send raw command: {command}")

            async def fake_wait(actor_arg, command, logger=None, *args, **kwargs):
                wait_calls.append((command, actor_arg.avatar_send_lock.locked()))
                self.assertFalse(actor_arg.avatar_send_lock.locked())
                return False

            actor._send_and_wait_feedback_raw = fail_raw
            with patch.object(sub_cultivator, "command_send_precheck", lambda *args, **kwargs: True):
                with patch.object(sub_cultivator, "wait_for_bot_activity_before_send", fake_wait):
                    result = await actor.send_and_wait_feedback_identity("寻真子", ".野外历练")

            return result, wait_calls, raw_calls, actor.avatar_send_lock.locked()

        result, wait_calls, raw_calls, lock_held = asyncio.run(run_case())

        self.assertIsNone(result)
        self.assertEqual(wait_calls, [(".野外历练", False)])
        self.assertEqual(raw_calls, [])
        self.assertFalse(lock_held)

    def test_spirit_tree_guard_command_guard_is_response_scoped(self):
        actor = SimpleNamespace(
            current_identity="主魂",
            config={},
            my_info=SimpleNamespace(first_name="Waaiging"),
            client=None,
        )
        alerts = []
        old_notify = log_utils._notify_command_guard_blocked
        log_utils._notify_command_guard_blocked = (
            lambda actor_arg, command, limit, window, block_seconds, logger=None, **kwargs: alerts.append(
                (command, limit, window, block_seconds)
            )
        )
        try:
            for idx in range(30):
                actor.current_identity = "主魂" if idx % 2 == 0 else "缘生子"
                self.assertTrue(log_utils.command_send_allowed(actor, ".协同守山"))
            self.assertEqual(alerts, [])

            log_utils.force_command_guard_block(
                actor,
                ".协同守山",
                60 * 60,
                identity="主魂",
                reason="fixture_response_error",
                alert=True,
                reason_text="返回信息表示当前无需守山，已暂停该命令 60 分钟。",
            )
            self.assertFalse(log_utils.command_send_precheck(actor, ".协同守山", identity="主魂"))
            actor.current_identity = "缘生子"
            self.assertFalse(log_utils.command_send_precheck(actor, ".协同守山", identity="缘生子"))

            log_utils.clear_command_guard_block(actor, ".协同守山", identity="主魂")
            self.assertTrue(log_utils.command_send_precheck(actor, ".协同守山", identity="缘生子"))
        finally:
            log_utils._notify_command_guard_blocked = old_notify

        self.assertEqual(alerts, [(".协同守山", 9999, 30 * 60, 60 * 60)])

    def test_repeated_response_guard_blocks_current_identity_command(self):
        actor = SimpleNamespace(current_identity="缘生子")
        calls = []
        old_force = command_feedback.force_command_guard_block
        command_feedback.force_command_guard_block = (
            lambda actor_arg, command, wait, logger=None, identity=None, reason="", alert=False, reason_text="":
            calls.append((command, wait, identity, reason, alert, reason_text))
        )
        try:
            text = "[Avatar: 缘生子]\n此炼化槽正在运转中，无法囚禁新的魂魄。"
            command = ".囚禁魂魄 1 凶兽戾魄"
            self.assertFalse(command_feedback._record_repeated_response_guard(actor, command, text, identity="缘生子"))
            self.assertFalse(command_feedback._record_repeated_response_guard(actor, command, text, identity="缘生子"))
            self.assertTrue(command_feedback._record_repeated_response_guard(actor, command, text, identity="缘生子"))
        finally:
            command_feedback.force_command_guard_block = old_force

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ".囚禁魂魄 1 凶兽戾魄")
        self.assertEqual(calls[0][1], 60 * 60)
        self.assertEqual(calls[0][2], "缘生子")
        self.assertEqual(calls[0][3], "repeated_response")
        self.assertTrue(calls[0][4])
        self.assertIn("同一回复重复 3 次", calls[0][5])

    def test_telegram_send_protection_stops_actor_on_write_forbidden(self):
        saved = []
        alerts = []
        actor = SimpleNamespace(
            current_identity="缘生子",
            is_running=True,
            state={},
            save_state=lambda: saved.append(True),
        )
        old_alert = command_feedback.send_text_alert

        async def fake_alert(actor_arg, title, text, logger=None, parse_mode=None):
            alerts.append((title, text))
            return True

        command_feedback.send_text_alert = fake_alert
        try:
            handled = asyncio.run(command_feedback._handle_telegram_send_protection(
                actor,
                ".囚禁魂魄 1 凶兽戾魄",
                RuntimeError("CHAT_WRITE_FORBIDDEN: You can't write in this chat"),
                identity="缘生子",
            ))
        finally:
            command_feedback.send_text_alert = old_alert

        self.assertTrue(handled)
        self.assertFalse(actor.is_running)
        self.assertTrue(saved)
        self.assertEqual(actor.state["telegram_send_protection_stop"]["reason"], "write_restricted")
        self.assertEqual(alerts[0][0], "Telegram发送保护")

    def test_xiaohao_send_protection_records_retry_and_clears_after_success(self):
        saved = []
        alerts = []
        actor = SimpleNamespace(
            account_key="xiaohao",
            current_identity="主魂",
            is_running=True,
            telegram_send_protection_retry_seconds=900,
            state={},
            save_state=lambda: saved.append(True),
        )
        old_alert = command_feedback.send_text_alert

        async def fake_alert(actor_arg, title, text, logger=None, parse_mode=None):
            alerts.append((title, text))
            return True

        command_feedback.send_text_alert = fake_alert
        try:
            handled = asyncio.run(command_feedback._handle_telegram_send_protection(
                actor,
                ".野外历练 谨慎",
                RuntimeError("USER_BANNED_IN_CHANNEL: banned from sending messages"),
                identity="主魂",
            ))
            self.assertTrue(handled)
            stop = actor.state["telegram_send_protection_stop"]
            self.assertTrue(stop["retry_at"])
            self.assertTrue(stop["alert_sent"])
            self.assertFalse(stop["alert_pending"])

            recovered = asyncio.run(command_feedback.record_telegram_send_success(actor))
        finally:
            command_feedback.send_text_alert = old_alert

        self.assertTrue(recovered)
        self.assertNotIn("telegram_send_protection_stop", actor.state)
        self.assertIn("telegram_send_protection_last_recovered", actor.state)
        self.assertEqual([item[0] for item in alerts], ["Telegram发送保护", "Telegram发送权限恢复"])
        self.assertGreaterEqual(len(saved), 3)

    def test_xiaohao_write_permission_monitor_triggers_protection(self):
        calls = []

        class FakeClient:
            async def get_permissions(self, chat_id, who):
                return SimpleNamespace(is_banned=False, send_messages=False, view_messages=True)

        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.client = FakeClient()
        actor.target_chat_id = -100123456
        actor.state = {}
        actor.save_state = lambda: None
        actor.current_identity = "主魂"
        actor.is_running = True
        actor.telegram_write_permission_poll_seconds = 30

        async def fake_handle(actor_arg, message, exc, logger=None, identity=None):
            calls.append((message, str(exc), identity))
            actor_arg.is_running = False
            return True

        with patch.object(cultivator_xiaohao, "_handle_telegram_send_protection", fake_handle):
            asyncio.run(actor.run_telegram_write_permission_monitor())

        self.assertEqual(calls[0][0], "(permission monitor)")
        self.assertIn("CHAT_WRITE_FORBIDDEN", calls[0][1])
        self.assertEqual(actor.state["telegram_write_permission_monitor"]["status"], "blocked")

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

    def test_field_training_missing_response_short_backs_off_without_confirmed_cooldown(self):
        actor = DummyCommon()
        actor.state = {
            "last_field_training_time": "",
            "next_field_training_time": "",
        }

        actor.record_field_training_response("", context="test")

        wait = common_seconds_until(actor.state["next_field_training_time"])
        self.assertGreater(wait, 4 * 60)
        self.assertLessEqual(wait, 5 * 60)

    def test_avatar_field_training_missing_response_short_backs_off(self):
        actor = DummyAvatarCommon()

        actor.record_identity_field_training_response("缘生子", "", context="test")

        next_time = actor.state["avatars"]["缘生子"]["next_field_training_time"]
        wait = common_seconds_until(next_time)
        self.assertGreater(wait, 4 * 60)
        self.assertLessEqual(wait, 5 * 60)

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

        actor.state = {
            "last_yuanying_out_time": "",
            "next_yuanying_out_time": "",
            "yuanying_out_end_time": "",
            "yuanying_out_active": False,
        }
        self.assertTrue(actor.record_yuanying_out_start_response(
            "你心念一动，元婴已在你丹田的次元空间内开始闭关，它将为你持续提供修为。",
        ))
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["next_yuanying_out_time"], "")
        self.assertEqual(actor.state["yuanying_out_end_time"], "")

        actor.state = {
            "last_yuanying_out_time": "2020-01-01 00:00:00",
            "next_yuanying_out_time": "",
            "yuanying_out_end_time": "",
            "yuanying_out_active": False,
        }
        self.assertTrue(actor.record_yuanying_out_start_response(
            "你的元婴正在执行“元婴闭关”任务，请先使用 `.元婴归窍` 将其召回。",
        ))
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["last_yuanying_out_time"], "2020-01-01 00:00:00")
        self.assertEqual(actor.state["next_yuanying_out_time"], "")
        self.assertEqual(actor.state["yuanying_out_end_time"], "")

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

    def test_sub_main_yuanying_retreat_settlement_reply_from_any_main_command(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.command_avatar_map = {5001: "主魂", 5002: "厚土"}
        future = (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "yuanying_out_active": True,
            "yuanying_out_end_time": future,
            "next_yuanying_out_time": future,
            "last_yuanying_out_time": "2020-01-01 00:00:00",
            "avatars": {"厚土": {}},
        }
        actor.save_state = lambda: None
        text = """**【元婴闭关结算】**
你的元婴在过去 **7** 小时内为你增加了 **7700** 点修为！
"""

        self.assertTrue(actor.maybe_record_main_yuanying_retreat_settlement_reply(
            DummyMessage(5101, text=text, reply_to_msg_id=5001),
            text,
            source="fixture",
        ))
        self.assertFalse(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["yuanying_out_end_time"], "")
        self.assertLessEqual(common_seconds_until(actor.state["next_yuanying_out_time"]), 10)

        actor.state["yuanying_out_active"] = True
        actor.state["yuanying_out_end_time"] = future
        actor.state["next_yuanying_out_time"] = future
        self.assertFalse(actor.maybe_record_main_yuanying_retreat_settlement_reply(
            DummyMessage(5102, text=text, reply_to_msg_id=5002),
            text,
            source="fixture",
        ))
        self.assertTrue(actor.state["yuanying_out_active"])
        self.assertEqual(actor.state["next_yuanying_out_time"], future)

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

    def test_lingxiao_loose_avatar_yuanying_start_syncs_avatar_state(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.avatar_usernames = {}
        actor.identity_usernames = {"主魂": ["Waaiging"]}
        actor.command_avatar_map = {}
        actor._current_identity = "主魂"
        actor.my_info = None
        actor.notify_users = []
        actor.state = {
            "avatars": {
                "缘生子": {
                    "last_yuanying_out_time": "",
                    "next_yuanying_out_time": "",
                    "yuanying_out_active": False,
                    "yuanying_out_end_time": "",
                }
            }
        }
        actor.save_state = lambda: None
        text = """
[Avatar: 缘生子]
你心念一动，丹田中的元婴化作一道流光飞出，消失在天际。
它将在外云游 **8** 小时，为你寻觅天地奇珍。下一次发言时若已归来，将自动结算收获。
"""

        actor.maybe_record_avatar_passive_states(DummyMessage(9101, text=text))

        a_state = actor.state["avatars"]["缘生子"]
        self.assertTrue(a_state["yuanying_out_active"])
        self.assertTrue(a_state["last_yuanying_out_time"])
        self.assertGreater(common_seconds_until(a_state["next_yuanying_out_time"]), 7 * 3600)
        self.assertEqual(a_state["yuanying_out_end_time"], a_state["next_yuanying_out_time"])
        self.assertFalse(a_state.get("concubine_voyage_active", False))

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

    def test_sub_yuanshengzi_yuanying_rift_checks_send_avatar_commands(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {
            "next_yuanying_out_time": "2099-01-01 00:00:00",
            "next_rift_search_time": "2099-01-02 00:00:00",
            "avatars": {"缘生子": {"deep_meditation_end_time": "2099-01-03 00:00:00"}},
        }
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": False
        actor.ensure_avatar_states()
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command))
            if command == ".元婴出窍":
                return "你心念一动，元婴出窍，消失在天际，将在外云游 **8小时**。"
            if command == ".探寻裂缝":
                return "你运转全身法力，撕开一道漆黑的空间裂缝，将元婴送入其中探寻机缘。"
            return ""

        actor.send_and_wait_feedback_identity = fake_send

        asyncio.run(actor._avatar_yuanying_out_check("缘生子"))
        asyncio.run(actor._avatar_rift_search_check("缘生子"))

        self.assertIn("缘生子", sub_cultivator.AVATAR_YUANYING_RIFT_AVATARS)
        self.assertEqual(sent, [("缘生子", ".元婴出窍"), ("缘生子", ".探寻裂缝")])
        self.assertEqual(actor.state["next_yuanying_out_time"], "2099-01-01 00:00:00")
        self.assertEqual(actor.state["next_rift_search_time"], "2099-01-02 00:00:00")
        self.assertTrue(actor.state["avatars"]["缘生子"]["yuanying_out_active"])
        self.assertGreater(common_seconds_until(actor.state["avatars"]["缘生子"]["next_rift_search_time"]), 11 * 3600)

    def test_manual_miniapp_star_reply_is_ignored(self):
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

        self.assertFalse(processed)
        self.assertEqual(actor.state["avatars"]["厚土"]["last_star_appease_time"], "")
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

    def test_xiaohao_yuanshengzi_yuanying_rift_checks_send_avatar_commands(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["缘生子"]
        actor.avatar_nicknames = {"缘生子": ""}
        actor.state = {
            "next_yuanying_out_time": "2099-01-01 00:00:00",
            "next_rift_search_time": "2099-01-02 00:00:00",
            "avatars": {"缘生子": {"deep_meditation_end_time": "2099-01-03 00:00:00"}},
        }
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": False
        actor.ensure_avatar_states()
        sent = []

        async def fake_send(identity, command, **kwargs):
            sent.append((identity, command, kwargs.get("force_identity_check")))
            if command == ".元婴出窍":
                return "你心念一动，元婴出窍，消失在天际，将在外云游 **8小时**。"
            if command == ".探寻裂缝":
                return "你运转全身法力，撕开一道漆黑的空间裂缝，将元婴送入其中探寻机缘。"
            return ""

        actor.send_and_wait_feedback_identity = fake_send

        asyncio.run(actor._avatar_yuanying_out_check("缘生子"))
        asyncio.run(actor._avatar_rift_search_check("缘生子"))

        self.assertIn("缘生子", cultivator_xiaohao.AVATAR_YUANYING_RIFT_AVATARS)
        self.assertEqual(sent, [
            ("缘生子", ".元婴出窍", True),
            ("缘生子", ".探寻裂缝", True),
        ])
        self.assertEqual(actor.state["next_yuanying_out_time"], "2099-01-01 00:00:00")
        self.assertEqual(actor.state["next_rift_search_time"], "2099-01-02 00:00:00")
        self.assertTrue(actor.state["avatars"]["缘生子"]["yuanying_out_active"])
        self.assertGreater(common_seconds_until(actor.state["avatars"]["缘生子"]["next_rift_search_time"]), 11 * 3600)

    def test_xiaohao_unowned_exit_text_does_not_clear_protected_avatar_meditation(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["问心子"]
        actor.avatar_nicknames = {"问心子": ""}
        actor.target_chat_id = -100123456
        actor.command_avatar_map = {}
        actor._current_identity = "问心子"
        future = (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "current_identity": "问心子",
            "avatars": {
                "问心子": {
                    "in_deep_meditation": True,
                    "deep_meditation_end_time": future,
                    "deep_meditation_guard_until": add_seconds_str(future, 180),
                    "meditation_restart_pending": False,
                }
            }
        }
        actor.save_state = lambda: None
        actor.text_targets_self = lambda msg, text: True
        actor.record_yuanying_out_active_response = lambda *args, **kwargs: False
        actor.record_yuanying_out_settlement_response = lambda *args, **kwargs: False
        actor.record_concubine_voyage_response = lambda *args, **kwargs: False

        actor.maybe_record_avatar_passive_states(DummyMessage(8201, text="闭关结束，神魂归位。"))

        state = actor.get_avatar_state("问心子")
        self.assertTrue(state["in_deep_meditation"])
        self.assertEqual(state["deep_meditation_end_time"], future)
        self.assertFalse(state["meditation_restart_pending"])

    def test_nine_heaven_wind_round_requirement_does_not_alert(self):
        actor = Cultivator.__new__(Cultivator)
        next_stairs = (datetime.now() + timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "next_stairs_time": next_stairs,
            "last_stairs_time": "",
            "nine_heaven_wind_cd_time": "",
        }
        actor.save_state = lambda: None
        alerts = []

        with patch.object(
            intelligent_cultivator,
            "notify_unrecognized_response",
            lambda *args, **kwargs: alerts.append((args, kwargs)),
        ):
            result = actor.record_nine_heaven_wind_response(
                "你尚未完成一轮周天巡天，无法承受九天罡风倒灌之术。(需 **1** 轮)"
            )

        self.assertFalse(result)
        self.assertEqual(alerts, [])
        self.assertEqual(actor.state["nine_heaven_wind_cd_time"], add_seconds_str(next_stairs, 60))

    def test_nine_heaven_wind_zero_completed_weeks_is_not_ready(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "completed_weeks": "0 轮",
            "nine_heaven_wind_cd_time": "",
        }

        self.assertFalse(actor.is_nine_heaven_wind_ready())

        actor.state["completed_weeks"] = "1 轮"
        self.assertTrue(actor.is_nine_heaven_wind_ready())

    def test_cloud_stairs_wind_cd_response_updates_stairs_and_wind_cd(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "cloud_stairs_progress": "3 / 12 阶",
            "completed_weeks": "0 轮",
        }
        actor.save_state = lambda: None

        result = actor.record_cloud_stairs_response("九天罡风尚未再聚，请在 **2小时30分钟45秒** 后再试。")

        self.assertFalse(result)
        self.assertEqual(actor.state["next_stairs_time"], actor.state["nine_heaven_wind_cd_time"])
        self.assertGreater(common_seconds_until(actor.state["next_stairs_time"]), 2 * 3600 + 29 * 60)

    def test_cloud_stairs_stale_cd_requires_status_refresh(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "cloud_stairs_progress": "3 / 12 阶",
            "next_stairs_time": "2026-06-11 21:41:12",
        }

        self.assertTrue(actor.cloud_stairs_status_needs_refresh())

        actor.state["next_stairs_time"] = (datetime.now() + timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertFalse(actor.cloud_stairs_status_needs_refresh())

    def test_cloud_stairs_loop_sends_climb_without_status_preflight(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "cloud_stairs_progress": "",
            "next_stairs_time": "2026-06-11 21:41:12",
            "heart_platform_date": datetime.now().strftime("%Y-%m-%d"),
        }
        actor.is_running = True
        actor.startup_done = asyncio.Event()
        actor.startup_done.set()
        sent = []

        async def fake_wait_for_main():
            return None

        async def fake_send(command, *args, **kwargs):
            sent.append(command)
            actor.is_running = False
            if command == ".登天阶":
                return "**【凌霄云阶】**\n当前云阶进度 4/12 阶，本次获得修为 +100。"
            return ""

        async def fake_heart_platform(*args, **kwargs):
            return False

        actor._wait_for_main_identity = fake_wait_for_main
        actor.dashboard_command_paused = lambda command, identity: False
        actor.send_and_wait_feedback = fake_send
        actor.maybe_use_heart_platform_before_climb = fake_heart_platform
        actor.save_state = lambda: None

        with patch.object(intelligent_cultivator, "scheduler_sleep_seconds", lambda seconds, minimum=1: 0):
            asyncio.run(actor.run_cloud_stairs_loop())

        self.assertNotIn(".天阶状态", sent)
        self.assertIn(".登天阶", sent)

    def test_cloud_stairs_restore_prefers_last_success_cd_over_stale_next(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "last_stairs_success_time": (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_stairs_time": (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.save_state = lambda: None

        restored = actor.restore_cloud_stairs_time_from_last()

        self.assertEqual(restored, actor.state["next_stairs_time"])
        self.assertGreater(common_seconds_until(restored), 2 * 3600 + 29 * 60)

    def test_cloud_stairs_restore_keeps_later_success_cd_over_short_future_next(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {
            "last_stairs_time": (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "next_stairs_time": (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        actor.save_state = lambda: None

        restored = actor.restore_cloud_stairs_time_from_last()

        self.assertEqual(restored, actor.state["next_stairs_time"])
        self.assertGreater(common_seconds_until(restored), 2 * 3600 + 29 * 60)

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

    def test_xiaohao_lingxiao_mismatch_invalidates_identity_cache(self):
        actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
        actor.avatars = ["问心子"]
        actor._current_identity = "问心子"
        actor._main_confirmed = True
        actor.state = {"current_identity": "问心子", "avatars": {"问心子": {"cloud_stairs_progress": "11 / 12 阶"}}}
        actor.save_state = lambda: None

        result = actor.record_avatar_cloud_stairs_response(
            "问心子",
            "你并非凌霄宫弟子，云阶禁制不会为你显现。"
        )

        state = actor.state["avatars"]["问心子"]
        self.assertFalse(result)
        self.assertEqual(actor.current_identity, "")
        self.assertTrue(state["next_stairs_time"])
        self.assertEqual(state.get("heart_platform_date", ""), "")

    def test_xiaohao_heart_platform_mismatch_does_not_mark_used(self):
        async def run_case():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.avatars = ["问心子"]
            actor._current_identity = "问心子"
            actor._main_confirmed = True
            actor.state = {
                "current_identity": "问心子",
                "avatars": {
                    "问心子": {
                        "cloud_stairs_progress": "11 / 12 阶",
                        "nine_heaven_wind_cd_time": add_seconds_str(now_str(), 3600),
                    }
                }
            }
            actor.save_state = lambda: None

            async def fake_send(identity, command, *args, **kwargs):
                self.assertEqual(identity, "问心子")
                self.assertEqual(command, ".问心台")
                return "你并非凌霄宫弟子，云阶禁制不会为你显现。"

            actor.send_and_wait_feedback_identity = fake_send
            return await actor.maybe_use_heart_platform_before_climb(
                "问心子",
                11,
                datetime.now().strftime("%Y-%m-%d"),
            ), actor.state

        result, state = asyncio.run(run_case())
        avatar_state = state["avatars"]["问心子"]
        self.assertFalse(result)
        self.assertEqual(state["current_identity"], "")
        self.assertEqual(avatar_state.get("heart_platform_date", ""), "")
        self.assertEqual(avatar_state.get("last_heart_time", ""), "")

    def test_xiaohao_lingxiao_commands_force_fresh_avatar_switch(self):
        async def run_case():
            actor = CultivatorXiaoHao.__new__(CultivatorXiaoHao)
            actor.avatars = ["问心子"]
            actor._current_identity = "问心子"
            actor._main_confirmed = False
            actor.state = {"current_identity": "问心子", "avatars": {"问心子": {}}}
            actor.avatar_send_lock = asyncio.Lock()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor._last_game_bot_activity_ts = time.monotonic()
            actor.save_state = lambda: None
            actor.should_wait_for_atomic_task = lambda *args, **kwargs: False
            actor.wait_while_identity_paused = lambda *args, **kwargs: asyncio.sleep(0, result=True)
            actor.time_critical_identity_command = lambda command: False
            actor.time_critical_defer_wait = lambda *args, **kwargs: -1
            actor.apply_switch_guard_backoff = lambda *args, **kwargs: False
            actor.check_and_record_switch_ban = lambda *args, **kwargs: False
            sent = []

            async def fake_raw(command, *args, **kwargs):
                sent.append(command)
                if command == ".切换 问心子":
                    return "切换成功！你的神念已附着在 **【问心子】** 之上。"
                return "ok"

            actor._send_and_wait_feedback_raw = fake_raw
            resp = await actor.send_and_wait_feedback_identity(
                "问心子",
                ".问心台",
                force_identity_check=True,
                max_retries=0,
            )
            return sent, resp

        sent, resp = asyncio.run(run_case())
        self.assertEqual(sent, [".切换 问心子", ".问心台"])
        self.assertEqual(resp, "ok")

    def test_main_heart_platform_mismatch_does_not_mark_used(self):
        async def run_case():
            actor = Cultivator.__new__(Cultivator)
            actor.state = {
                "completed_weeks": "0 轮",
                "heart_platform_date": "",
                "last_heart_time": "",
                "nine_heaven_wind_cd_time": "",
            }
            actor._main_confirmed = True
            actor.save_state = lambda: None

            async def fake_send(command, *args, **kwargs):
                self.assertEqual(command, ".问心台")
                return "你并非凌霄宫弟子，云阶禁制不会为你显现。"

            actor.send_and_wait_feedback = fake_send
            await actor.maybe_use_heart_platform_before_climb(
                11,
                datetime.now().strftime("%Y-%m-%d"),
            )
            return actor.state, actor._main_confirmed

        state, main_confirmed = asyncio.run(run_case())
        self.assertEqual(state.get("heart_platform_date", ""), "")
        self.assertEqual(state.get("last_heart_time", ""), "")
        self.assertFalse(main_confirmed)

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

    def test_dashboard_shows_sub_yuanshengzi_yuanying_and_rift(self):
        panels = build_command_panels("sub", {"avatars": {"缘生子": {}}})
        yuanshengzi = next(panel for panel in panels if panel.get("identity") == "缘生子")
        commands = {row.get("command") for row in yuanshengzi.get("commands", [])}

        self.assertIn(".元婴出窍", commands)
        self.assertIn(".探寻裂缝", commands)

    def test_dashboard_sub_yuanshengzi_uses_yinluo_not_star_palace(self):
        panels = build_command_panels("sub", {"avatars": {"缘生子": {"yinluo": {}}}})
        yuanshengzi = next(panel for panel in panels if panel.get("identity") == "缘生子")
        commands = {row.get("command") for row in yuanshengzi.get("commands", [])}

        self.assertIn(".我的阴罗幡", commands)
        self.assertIn(".血洗山林", commands)
        self.assertNotIn(".观星台", commands)
        self.assertNotIn(".启阵", commands)
        self.assertNotIn(".观星", commands)
        self.assertNotIn(".改换星移 @Gamling33", commands)
        self.assertFalse(any(str(command or "").startswith(".牵引星辰") for command in commands))

    def test_dashboard_shows_xiaohao_yuanshengzi_yuanying_and_rift(self):
        panels = build_command_panels("xiaohao", {"avatars": {"缘生子": {}}})
        yuanshengzi = next(panel for panel in panels if panel.get("identity") == "缘生子")
        commands = {row.get("command") for row in yuanshengzi.get("commands", [])}

        self.assertIn(".元婴出窍", commands)
        self.assertIn(".探寻裂缝", commands)
        self.assertIn(".引道 水", commands)
        self.assertNotIn(".助阵", commands)
        self.assertNotIn(".观星台", commands)
        self.assertNotIn(".观星", commands)
        self.assertNotIn(".改换星移 @TitanCreeper", commands)
        self.assertFalse(any(str(command or "").startswith(".牵引星辰") for command in commands))

    def test_dashboard_hides_miniapp_star_palace_commands(self):
        miniapp_commands = {".观星台", ".安抚星辰", ".收集精华"}
        cases = [
            ("main", "素缘子"),
            ("sub", "厚土"),
            ("sub", "寻真子"),
            ("xiaohao", "素心子"),
        ]

        for account, identity in cases:
            with self.subTest(account=account, identity=identity):
                panels = build_command_panels(account, {"avatars": {identity: {}}})
                panel = next(row for row in panels if row.get("identity") == identity)
                commands = {row.get("command") for row in panel.get("commands", [])}

                self.assertTrue(miniapp_commands.isdisjoint(commands))
                self.assertFalse(any(str(command or "").startswith(".牵引星辰") for command in commands))

    def test_miniapp_star_palace_commands_disabled_by_local_policy(self):
        actor = SimpleNamespace(current_identity="厚土")
        for command in (".观星台", ".安抚星辰", ".收集精华", ".牵引星辰 天雷星"):
            with self.subTest(command=command):
                self.assertFalse(log_utils.command_send_precheck(actor, command))
                self.assertFalse(log_utils.command_send_allowed(actor, command))

    def test_dashboard_xiaohao_hunt_stopped_is_not_actionable_due(self):
        panels = build_command_panels("xiaohao", {
            "beast_hunt_stopped": True,
            "beast_hunt_stopped_reason": "第十只灵兽种类是风雀：风希",
            "avatars": {},
        })
        main_panel = next(panel for panel in panels if panel.get("identity") == "主魂")
        hunt = next(row for row in main_panel.get("commands", []) if row.get("command") == ".寻觅灵兽")

        self.assertEqual(hunt["status"], "已停止")
        self.assertEqual(hunt["tone"], "done")
        self.assertFalse(hunt["actionable"])
        self.assertEqual(hunt["schedule_type"], "cooldown")

    def test_dashboard_avatar_field_training_commands_use_current_mode(self):
        panels = build_command_panels("main", {"avatars": {"无咎子": {}, "缘生子": {}, "素缘子": {}}})
        by_identity = {panel.get("identity"): panel for panel in panels}

        self.assertIn(".野外历练 深入", {row.get("command") for row in by_identity["无咎子"].get("commands", [])})
        self.assertNotIn(".推命 探索", {row.get("command") for row in by_identity["无咎子"].get("commands", [])})
        self.assertNotIn(".改命 探索", {row.get("command") for row in by_identity["无咎子"].get("commands", [])})
        self.assertIn(".野外历练", {row.get("command") for row in by_identity["缘生子"].get("commands", [])})
        self.assertIn(".野外历练", {row.get("command") for row in by_identity["素缘子"].get("commands", [])})
        self.assertNotIn(".野外历练 谨慎", {row.get("command") for row in by_identity["缘生子"].get("commands", [])})

        sub_panels = build_command_panels("sub", {"avatars": {"厚土": {}}})
        sub_avatar = next(panel for panel in sub_panels if panel.get("identity") == "厚土")
        self.assertIn(".野外历练", {row.get("command") for row in sub_avatar.get("commands", [])})

        xiaohao_panels = build_command_panels("xiaohao", {"avatars": {"问心子": {}}})
        xiaohao_avatar = next(panel for panel in xiaohao_panels if panel.get("identity") == "问心子")
        self.assertIn(".野外历练", {row.get("command") for row in xiaohao_avatar.get("commands", [])})

    def test_dashboard_force_exit_ignores_expired_formation_window(self):
        force_exit_time = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        active_until = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        row = dashboard_server.force_exit_command({
            "next_force_exit_time": force_exit_time,
            "formation_active_until": active_until,
            "in_deep_meditation": False,
        })

        self.assertEqual(row["tone"], "done")
        self.assertEqual(row["status"], "无需出关")

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

    def test_main_wujiuzi_field_training_no_longer_sends_exploration_prefixes(self):
        actor = Cultivator.__new__(Cultivator)
        actor.avatars = ["无咎子"]
        actor.avatar_nicknames = {"无咎子": "天星雷总"}
        actor.avatar_features = {
            "无咎子": {
                "meditation_prefix": ".推命",
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

    def test_soul_curse_main_extra_parsers_match_feedback_family(self):
        greeting = "婉影问安已成，南宫婉轻声回应。"
        co_study = "你与南宫婉同参封魂，封魂咒气息渐稳。"
        co_cooldown = "同参封魂不可频繁，请在 7小时59分钟 后再试。"

        self.assertEqual(parse_soul_curse_wanying_greeting(greeting).get("status"), "success")
        self.assertEqual(parse_soul_curse_co_study(co_study).get("status"), "success")
        parsed_cooldown = parse_soul_curse_co_study(co_cooldown)
        self.assertEqual(parsed_cooldown.get("status"), "cooldown")
        self.assertGreater(parsed_cooldown.get("cooldown_seconds"), 7 * 3600)
        self.assertTrue(log_utils.feedback_response_matches_command(SOUL_CURSE_WANYING_GREETING_COMMAND, greeting))
        self.assertTrue(log_utils.feedback_response_matches_command(SOUL_CURSE_CO_STUDY_COMMAND, co_study))

    def test_soul_curse_main_extra_tick_only_runs_for_main_account(self):
        actor = DummySoulCurse("main")
        profile = actor.soul_curse_publisher_profile()

        asyncio.run(actor.soul_curse_main_extra_tick(profile))
        asyncio.run(actor.soul_curse_main_extra_tick(profile))

        self.assertEqual(actor.sent, [SOUL_CURSE_WANYING_GREETING_COMMAND, SOUL_CURSE_CO_STUDY_COMMAND])
        curse = actor.state["soul_curse"]
        self.assertEqual(curse.get("last_wanying_greeting_date"), datetime.now().strftime("%Y-%m-%d"))
        self.assertGreater(common_seconds_until(curse.get("next_co_study_time")), 7 * 3600)

        xiaohao = DummySoulCurse("xiaohao")
        wait = asyncio.run(xiaohao.soul_curse_main_extra_tick(xiaohao.soul_curse_publisher_profile()))
        self.assertEqual(xiaohao.sent, [])
        self.assertEqual(wait, 600)

    def test_dashboard_shows_main_only_soul_curse_extra_commands(self):
        main_panel = next(panel for panel in build_command_panels("main", {}) if panel.get("identity") == "主魂")
        xiaohao_panel = next(panel for panel in build_command_panels("xiaohao", {}) if panel.get("identity") == "主魂")
        main_commands = {row.get("command") for row in main_panel.get("commands", [])}
        xiaohao_commands = {row.get("command") for row in xiaohao_panel.get("commands", [])}

        self.assertIn(SOUL_CURSE_WANYING_GREETING_COMMAND, main_commands)
        self.assertIn(SOUL_CURSE_CO_STUDY_COMMAND, main_commands)
        self.assertNotIn(SOUL_CURSE_WANYING_GREETING_COMMAND, xiaohao_commands)
        self.assertNotIn(SOUL_CURSE_CO_STUDY_COMMAND, xiaohao_commands)

    def test_dashboard_shows_main_soul_wanling_beast_commands(self):
        panels = build_command_panels("main", {"avatars": {}})
        main_panel = next(panel for panel in panels if panel.get("identity") == "主魂")
        commands = {row.get("command") for row in main_panel.get("commands", [])}

        self.assertNotIn(".天阶状态", commands)
        self.assertNotIn(".登天阶", commands)
        self.assertNotIn(".引九天罡风", commands)
        self.assertNotIn(".问心台", commands)
        self.assertTrue({
            ".寻觅灵兽",
            ".探渊 <灵兽>",
            ".一键放养",
            "miniapp:spirit-beast-contract",
            ".灵兽巡边 <灵兽> 袭营",
        }.issubset(commands))
        self.assertNotIn(".灵兽互动 <重点灵兽>", commands)
        self.assertFalse({
            ".灵树状态",
            ".灵树灌溉",
            ".协同守山",
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

    def test_small_world_prayer_keywords_trigger_manifest(self):
        for keyword in intelligent_cultivator.SMALL_WORLD_PRAYER_KEYWORDS:
            with self.subTest(keyword=keyword):
                actor = Cultivator.__new__(Cultivator)
                actor.state = {}
                actor.active_atomic_task = None
                actor.save_state = lambda: None
                actor.dashboard_command_paused = lambda command, identity="": False
                sent = []

                async def fake_send(command, **kwargs):
                    sent.append((command, kwargs.get("force_identity_check")))
                    if command == intelligent_cultivator.SMALL_WORLD_COMMAND:
                        return f"【小世界】发现{keyword}，可响应祈愿。"
                    return "显灵成功，愿力已降下。"

                actor.send_and_wait_feedback = fake_send

                self.assertTrue(asyncio.run(actor.execute_small_world_once()))
                self.assertEqual(sent, [
                    (intelligent_cultivator.SMALL_WORLD_COMMAND, True),
                    (intelligent_cultivator.SMALL_WORLD_MANIFEST_COMMAND, True),
                ])
                self.assertTrue(actor.state["last_manifest_time"])
                remaining = common_seconds_until(actor.state["next_small_world_time"])
                self.assertGreater(remaining, 5 * 3600 + 50 * 60)
                self.assertLessEqual(remaining, intelligent_cultivator.SMALL_WORLD_CD_SECONDS)

    def test_small_world_without_prayer_does_not_manifest(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {}
        actor.active_atomic_task = None
        actor.save_state = lambda: None
        actor.dashboard_command_paused = lambda command, identity="": False
        sent = []

        async def fake_send(command, **kwargs):
            sent.append(command)
            return "【小世界】天地安宁，暂无祈愿。"

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_small_world_once()))
        self.assertEqual(sent, [intelligent_cultivator.SMALL_WORLD_COMMAND])
        self.assertFalse(actor.state.get("last_manifest_time"))

    def test_small_world_feedback_families_accept_manifest_and_preaching_results(self):
        self.assertTrue(log_utils.feedback_response_matches_command(
            intelligent_cultivator.SMALL_WORLD_COMMAND,
            "【小世界】一名凡人祈愿，等待神明响应。",
        ))
        self.assertTrue(log_utils.feedback_response_matches_command(
            intelligent_cultivator.SMALL_WORLD_MANIFEST_COMMAND,
            "愿力汇聚，香火与功德均有所增长。",
        ))
        self.assertTrue(log_utils.feedback_response_matches_command(
            intelligent_cultivator.MIRACLE_PREACH_COMMAND,
            "【神迹】布道成功，凡人信仰有所增长。",
        ))

    def test_small_world_explicit_cooldown_and_empty_response_retry(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {}
        actor.save_state = lambda: None

        self.assertFalse(actor.record_small_world_response(
            "小世界尚在冷却，请在 **2小时15分钟** 后再查看。"
        ))
        cooldown = common_seconds_until(actor.state["next_small_world_time"])
        self.assertGreater(cooldown, 2 * 3600 + 10 * 60)
        self.assertLessEqual(cooldown, 2 * 3600 + 15 * 60)

        self.assertFalse(actor.record_small_world_response(""))
        retry = common_seconds_until(actor.state["next_small_world_time"])
        self.assertGreater(retry, 9 * 60)
        self.assertLessEqual(retry, intelligent_cultivator.SMALL_WORLD_RETRY_SECONDS)

    def test_miracle_preach_records_three_hour_cooldown(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {}
        actor.active_atomic_task = None
        actor.save_state = lambda: None
        sent = []

        async def fake_send(command, **kwargs):
            sent.append((command, kwargs.get("force_identity_check")))
            return "【神迹】布道成功，凡人信仰有所增长。"

        actor.send_and_wait_feedback = fake_send

        self.assertTrue(asyncio.run(actor.execute_miracle_preach_once()))
        self.assertEqual(sent, [(intelligent_cultivator.MIRACLE_PREACH_COMMAND, True)])
        remaining = common_seconds_until(actor.state["next_miracle_preach_time"])
        self.assertGreater(remaining, 2 * 3600 + 50 * 60)
        self.assertLessEqual(remaining, intelligent_cultivator.MIRACLE_PREACH_CD_SECONDS)

    def test_miracle_preach_real_cooldown_reply_is_matched_and_recorded(self):
        actor = Cultivator.__new__(Cultivator)
        actor.state = {}
        actor.save_state = lambda: None
        response = "凡间方才承受神谕，需再等待 **2小时21分钟37秒**。"

        self.assertTrue(log_utils.feedback_response_matches_command(
            intelligent_cultivator.MIRACLE_PREACH_COMMAND,
            response,
        ))
        self.assertFalse(actor.record_miracle_preach_response(response))
        remaining = common_seconds_until(actor.state["next_miracle_preach_time"])
        self.assertGreater(remaining, 2 * 3600 + 20 * 60)
        self.assertLessEqual(remaining, 2 * 3600 + 22 * 60)

    def test_main_watchdog_detects_stale_small_world_and_miracle(self):
        actor = Cultivator.__new__(Cultivator)
        overdue = (datetime.now() - timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S")
        actor.state = {
            "is_paused": False,
            "identity_pauses": {},
            "next_small_world_time": overdue,
            "next_miracle_preach_time": overdue,
        }
        actor.identity_pause_seconds = lambda identity="主魂": 0
        actor.state_time_command_paused = lambda key, identity="": False
        actor.dashboard_command_paused = lambda command, identity="": False

        stale = actor.stale_scheduler_due_items()

        self.assertEqual(
            {(item[0], item[1]) for item in stale},
            {
                ("next_small_world_time", intelligent_cultivator.SMALL_WORLD_COMMAND),
                ("next_miracle_preach_time", intelligent_cultivator.MIRACLE_PREACH_COMMAND),
            },
        )

    def test_dashboard_shows_small_world_only_for_main_main_soul(self):
        seen = []
        target_commands = {
            intelligent_cultivator.SMALL_WORLD_COMMAND,
            intelligent_cultivator.SMALL_WORLD_MANIFEST_COMMAND,
            intelligent_cultivator.MIRACLE_PREACH_COMMAND,
        }
        for account in ("main", "sub", "xiaohao"):
            for panel in build_command_panels(account, {"avatars": {}}):
                commands = {row.get("command") for row in panel.get("commands", [])}
                found = commands & target_commands
                if found:
                    seen.append((account, panel.get("identity") or "主魂", found))

        self.assertEqual(seen, [("main", "主魂", target_commands)])

    def test_main_star_gazing_force_check_refreshes_cached_avatar_identity(self):
        async def run_case():
            actor = Cultivator.__new__(Cultivator)
            actor.avatars = ["素缘子"]
            actor._current_identity = "素缘子"
            actor._main_confirmed = False
            actor.state = {"current_identity": "素缘子", "avatars": {"素缘子": {}}}
            actor.avatar_send_lock = asyncio.Lock()
            actor.pause_event = asyncio.Event()
            actor.pause_event.set()
            actor.active_atomic_task = None
            actor.is_running = True
            actor.save_state = lambda: None
            actor.wait_while_identity_paused = lambda *args, **kwargs: asyncio.sleep(0, result=True)
            sent = []

            async def fake_raw(command, *args, **kwargs):
                sent.append(command)
                if command == ".切换 素缘子":
                    return "切换成功！你的神念已附着在【素缘子】之上。"
                return "【观星结果】"

            async def bot_ready(*args, **kwargs):
                return True

            actor._send_and_wait_feedback_raw = fake_raw
            with patch.object(intelligent_cultivator, "command_send_precheck", return_value=True), patch.object(
                intelligent_cultivator, "wait_for_bot_activity_before_send", bot_ready
            ):
                response = await actor.send_and_wait_feedback_identity(
                    "素缘子",
                    ".观星",
                    force_identity_check=True,
                    max_retries=0,
                )
            return sent, response

        sent, response = asyncio.run(run_case())
        self.assertEqual(sent, [".切换 素缘子", ".观星"])
        self.assertEqual(response, "【观星结果】")

    def test_dashboard_removes_luoyun_spirit_tree_commands(self):
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
        commands = {
            row.get("command")
            for panel in panels
            for row in panel.get("commands", [])
        }
        self.assertFalse({".灵树灌溉", ".灵树状态", ".采摘灵果", ".协同守山"} & commands)

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

    def test_sub_star_gazing_rejects_untracked_passive_result(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatar_usernames = {"lvdoumiao": "缘生子"}
        actor.feedback_commands = {}
        actor.feedback_identities = {}
        actor.command_avatar_map = {}
        msg = DummyMessage(
            2002,
            text="**【星盘显化】**\n@OtherUser 闭目凝神，推演天机...\n**下一次天道演化将是**: **【Good - 地磁暴动】**",
            reply_to_msg_id=2001,
        )

        self.assertEqual(actor.claimed_star_gazing_reply_msg_id("缘生子", msg, msg.text), 0)

    def test_sub_star_gazing_accepts_tracked_avatar_result(self):
        actor = SubCultivator.__new__(SubCultivator)
        actor.avatar_usernames = {"lvdoumiao": "缘生子"}
        actor.feedback_commands = {3001: ".观星"}
        actor.feedback_identities = {3001: "缘生子"}
        actor.command_avatar_map = {3001: "缘生子"}
        msg = DummyMessage(
            3002,
            text="**【星盘显化】**\n@Lvdoumiao 闭目凝神，推演天机...\n**下一次天道演化将是**: **【Good - 地磁暴动】**",
            reply_to_msg_id=3001,
        )

        self.assertEqual(actor.claimed_star_gazing_reply_msg_id("缘生子", msg, msg.text), 3002)


if __name__ == "__main__":
    unittest.main()
