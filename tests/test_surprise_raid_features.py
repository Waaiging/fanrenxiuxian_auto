import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import surprise_raid_features as srf


class SurpriseRaidFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = self.tempdir.name
        self.patches = [
            patch.object(srf, "SURPRISE_RAID_STATE_FILE", os.path.join(root, "surprise_state.json")),
            patch.object(srf, "SURPRISE_RAID_LOCK_FILE", os.path.join(root, "surprise_state.lock")),
        ]
        for item in self.patches:
            item.start()
        srf.load_surprise_raid_state(write_back=True)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    def test_cooldown_parser_supports_hour_minute_second(self):
        self.assertEqual(srf.parse_surprise_raid_cooldown_seconds("请在 **3分钟15秒** 后再试。"), 195)
        self.assertEqual(srf.parse_surprise_raid_cooldown_seconds("请在 1小时2分3秒 后再试"), 3723)
        self.assertEqual(srf.parse_surprise_raid_cooldown_seconds("普通回复"), 0)

    def test_known_step_classification(self):
        retrieve = srf.classify_surprise_raid_step(
            ".从万宝阁取下 风雷翅", "你已将【风雷翅】从万宝阁收回储物袋。"
        )
        self.assertEqual(retrieve[0], "success")
        equip = srf.classify_surprise_raid_step(".装备 风雷翅", "当前祭出：【风雷翅】")
        self.assertEqual(equip[0], "success")
        pending = srf.classify_surprise_raid_step(".奇袭 夺宝", "催动【风雷翅】，对 @target 施展【夺宝】之术")
        self.assertEqual(pending[0], "pending")
        success = srf.classify_surprise_raid_step(".奇袭 夺宝", "【惊雷一击·夺宝】成功了")
        self.assertEqual(success[0], "success")
        cooldown = srf.classify_surprise_raid_step(".奇袭 夺宝", "消耗过巨，请在 3分钟 后再试")
        self.assertEqual(cooldown[0], "cooldown")
        self.assertEqual(srf.parse_surprise_raid_cooldown_seconds(cooldown[2]), 180)
        missing = srf.classify_surprise_raid_step(".装备 风雷翅", "你没有风雷翅")
        self.assertEqual(missing[0], "missing_item")
        unknown = srf.classify_surprise_raid_step(".装备 风雷翅", "完全陌生的异常信息")
        self.assertEqual(unknown, ("unknown", "未知回复", "完全陌生的异常信息"))

    def test_config_validation_and_persistence(self):
        with self.assertRaisesRegex(ValueError, "invalid surprise raid interval"):
            srf.set_surprise_raid_config(True, interval_seconds=1)

        state = srf.set_surprise_raid_config(
            True,
            interval_seconds=7200,
            raider_account="main",
            raider_identity="主魂",
            target_account="sub",
            target_identity="厚土",
        )
        self.assertTrue(state["enabled"])
        self.assertEqual(state["interval_seconds"], 7200)
        self.assertEqual(state["raider_account"], "main")
        self.assertEqual(state["target_identity"], "厚土")
        self.assertEqual(srf.load_surprise_raid_state()["target_identity"], "厚土")

    def test_lifecycle_reserve_claim_prepare_execute_finish(self):
        srf.set_surprise_raid_config(True, raider_account="main", raider_identity="主魂", target_account="sub", target_identity="厚土")
        state = srf.load_surprise_raid_state()
        state["next_run_at"] = srf.surprise_raid_time(srf.surprise_raid_now() - timedelta(seconds=1))
        srf._atomic_write_json(srf.SURPRISE_RAID_STATE_FILE, state)

        self.assertIsNotNone(srf.reserve_surprise_raid_for_account("sub"), "任一运行中的账号都可原子预约，随后归属目标账号")
        claim = srf.claim_surprise_raid_target_preparation("sub")
        self.assertIsNotNone(claim)
        self.assertTrue(srf.finish_surprise_raid_target_preparation(claim, True, "ready", reply_to_msg_id=9001))
        reservation = srf.claim_surprise_raid_execution("main")
        self.assertEqual(reservation["reply_to_msg_id"], 9001)
        self.assertTrue(srf.finish_surprise_raid_execution(reservation, {"status": "success", "outcome": "完成"}))
        finished = srf.load_surprise_raid_state()
        self.assertEqual(finished["phase"], "idle")
        self.assertGreaterEqual(finished["interval_seconds"], 7200)

    def test_end_to_end_command_sequence_and_unknown_stop(self):
        srf.set_surprise_raid_config(True, raider_account="main", raider_identity="主魂", target_account="sub", target_identity="厚土")
        state = srf.load_surprise_raid_state()
        state["next_run_at"] = srf.surprise_raid_time(srf.surprise_raid_now() - timedelta(seconds=1))
        srf._atomic_write_json(srf.SURPRISE_RAID_STATE_FILE, state)

        commands = []
        switch_message_id = 9001

        class RaiderActor(srf.SurpriseRaidMixin):
            account_key = "sub"
            is_running = True
            current_identity = "厚土"
            avatars = ["厚土"]
            identity_usernames = {}
            log = unittest.mock.Mock()
            client = None

            def surprise_raid_logger(self):
                return self.log

            async def prepare_identity_for_time_critical_command(self, identity, **kwargs):
                commands.append((self.account_key, f".切换 {identity}", None))
                return True, switch_message_id

            async def _send_surprise_raid_identity_command(self, identity, command, reply_to=None):
                commands.append((self.account_key, command, reply_to))
                if command == ".从万宝阁取下 风雷翅":
                    return SimpleNamespace(text="你已将【风雷翅】从万宝阁收回储物袋。")
                if command == ".装备 风雷翅":
                    return SimpleNamespace(text="当前祭出：【风雷翅】")
                if command == ".奇袭 夺宝":
                    return SimpleNamespace(text="催动【风雷翅】，对 @crayonxxin 施展【夺宝】之术")
                return SimpleNamespace(text=f"已处理 {command}")

        class MainRaiderActor(RaiderActor):
            account_key = "main"

        with patch.object(srf, "SURPRISE_RAID_RESULT_WAIT_SECONDS", 0), \
                patch.object(srf, "send_text_alert", new=unittest.mock.AsyncMock()) as alert:
            actor = MainRaiderActor()
            # Prepare first, then atomically reserve the due cycle.  In the
            # live scheduler these are two adjacent steps inside the target
            # account's task; the state reservation is the concurrency lock.
            target_actor = RaiderActor()
            actor_preparation = asyncio.run(target_actor._prepare_surprise_raid_target_identity({
                "target_account": "sub",
                "target_identity": "厚土",
                "target_username": "crayonxxin",
            }))
            self.assertTrue(actor_preparation[0], f"目标身份准备失败：{actor_preparation}")
            self.assertEqual(actor_preparation[2], switch_message_id)
            self.assertTrue(srf.reserve_surprise_raid_for_account("sub"))
            claim = srf.claim_surprise_raid_target_preparation("sub")
            self.assertIsNotNone(claim)
            srf.finish_surprise_raid_target_preparation(claim, *actor_preparation[:2], reply_to_msg_id=actor_preparation[2])
            reservation = srf.claim_surprise_raid_execution("main")
            self.assertIsNotNone(reservation)
            result = asyncio.run(srf.SurpriseRaidMixin.execute_surprise_raid_reservation(actor, reservation))

        self.assertEqual(result["status"], "unknown")
        state = srf.load_surprise_raid_state()
        self.assertFalse(state["enabled"])
        self.assertIn("未知回复", state["last_error"])
        self.assertIn(("main", ".奇袭 夺宝", switch_message_id), commands)
        self.assertIn(("main", ".散念 风雷翅", None), commands)
        self.assertNotIn(("main", ".上架至万宝阁 风雷翅", None), commands)

    def test_missing_wing_skips_without_attack(self):
        class Actor(srf.SurpriseRaidMixin):
            account_key = "main"
            identity_usernames = {}
            log = unittest.mock.Mock()

            async def _send_surprise_raid_identity_command(self, identity, command, reply_to=None):
                self.commands.append(command)
                return SimpleNamespace(text="没有找到风雷翅")

        actor = Actor()
        actor.commands = []
        result = asyncio.run(srf.SurpriseRaidMixin.execute_surprise_raid_reservation(
            actor,
            {"identity": "主魂", "target_username": "target", "reply_to_msg_id": 123},
        ))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(actor.commands, [".从万宝阁取下 风雷翅"])


if __name__ == "__main__":
    unittest.main()
