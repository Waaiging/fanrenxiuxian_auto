import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import duel_features
from common_command_features import CommonCommandMixin


class DuelControlTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = self.tempdir.name
        self.patches = [
            patch.object(duel_features, "DUEL_STATE_FILE", os.path.join(root, "duel_state.json")),
            patch.object(duel_features, "DUEL_LOCK_FILE", os.path.join(root, "duel_state.lock")),
            patch.object(duel_features, "XIAOHAO_STATE_FILE", os.path.join(root, "state_xiaohao.json")),
            patch.object(duel_features, "DUEL_DB_FILE", os.path.join(root, "events.sqlite3")),
        ]
        for item in self.patches:
            item.start()
        duel_features.load_duel_state(write_back=True)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    def test_disabled_participant_is_skipped_and_custom_target_is_reserved(self):
        duel_features.set_duel_control(False, "titan")
        duel_features.set_duel_participant_control(False, "main|无咎子", "FirstTarget")
        duel_features.set_duel_participant_control(True, "main|缘生子", "ChosenTarget")

        reservation = duel_features.reserve_duel_for_account("main")

        self.assertEqual(reservation["participant_key"], "main|缘生子")
        self.assertEqual(reservation["target_username"], "ChosenTarget")
        self.assertEqual(reservation["command"], ".斗法 @ChosenTarget")

    def test_custom_titan_queue_target_does_not_require_titan_preparation(self):
        duel_features.set_duel_control(False, "waaiging")
        for key in ("sub|厚土", "sub|缘生子", "sub|寻真子"):
            duel_features.set_duel_participant_control(False, key)
        duel_features.set_duel_participant_control(True, "main|素缘子", "FreeTarget")

        reservation = duel_features.reserve_duel_for_account("main")

        self.assertEqual(reservation["queue_key"], "titan")
        self.assertEqual(reservation["target_username"], "FreeTarget")

    def test_daily_reset_preserves_participant_preferences(self):
        state = duel_features.load_duel_state()
        participant = state["queues"]["waaiging"]["participants"]["main|无咎子"]
        participant["enabled"] = False
        participant["target_username"] = "RememberMe"
        participant["remaining"] = 2
        state["date"] = "2000-01-01"

        reset = duel_features._ensure_duel_state_shape(state)
        participant = reset["queues"]["waaiging"]["participants"]["main|无咎子"]

        self.assertFalse(participant["enabled"])
        self.assertEqual(participant["target_username"], "RememberMe")
        self.assertEqual(participant["remaining"], duel_features.DUEL_DAILY_LIMIT)

    def test_version_one_state_migrates_without_resetting_legacy_preferences(self):
        state = duel_features.duel_default_state()
        state["version"] = 1
        state.pop("multi", None)
        participant = state["queues"]["waaiging"]["participants"]["main|无咎子"]
        participant["enabled"] = False
        participant["target_username"] = "RememberLegacyTarget"

        migrated = duel_features._ensure_duel_state_shape(state, reset_daily=False)

        self.assertEqual(migrated["version"], 2)
        self.assertFalse(
            migrated["queues"]["waaiging"]["participants"]["main|无咎子"]["enabled"]
        )
        self.assertEqual(
            migrated["queues"]["waaiging"]["participants"]["main|无咎子"]["target_username"],
            "RememberLegacyTarget",
        )
        self.assertFalse(migrated["multi"]["enabled"])
        self.assertEqual(migrated["multi"]["targets"], [])

    def test_titan_beast_mode_is_deploy_only_and_survives_daily_reset(self):
        with self.assertRaises(ValueError):
            duel_features.set_titan_beast_mode("pasture")
        duel_features.set_titan_beast_mode("deploy")

        state = duel_features.load_duel_state()
        self.assertEqual(state["queues"]["titan"]["beast_mode"], "deploy")
        self.assertIn("六翼出战", state["queues"]["titan"]["last_result"])

        state["queues"]["titan"]["next_at"] = "2099-01-01 00:00:00"
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        state = duel_features.set_titan_beast_mode("deploy")
        self.assertEqual(state["queues"]["titan"]["next_at"], "")

        state["date"] = "2000-01-01"
        reset = duel_features._ensure_duel_state_shape(state)
        self.assertEqual(reset["queues"]["titan"]["beast_mode"], "deploy")

    def test_dashboard_payload_returns_identity_controls(self):
        duel_features.set_duel_participant_control(False, "main|无咎子", "DashboardTarget")

        payload = duel_features.duel_dashboard_payload()
        row = next(
            participant
            for queue in payload["queues"]
            for participant in queue["participants"]
            if participant["key"] == "main|无咎子"
        )

        self.assertFalse(row["enabled"])
        self.assertEqual(row["target_username"], "DashboardTarget")
        self.assertIn("Waaiging", payload["target_options"])

    def test_dashboard_payload_returns_titan_beast_mode(self):
        duel_features.set_titan_beast_mode("出战")

        payload = duel_features.duel_dashboard_payload()
        titan = next(queue for queue in payload["queues"] if queue["key"] == "titan")

        self.assertEqual(titan["target_status"]["desired_mode"], "deploy")
        self.assertEqual(titan["target_status"]["desired_label"], "出战")
        self.assertIn("preparation_blocked", titan["target_status"])

    def test_invalid_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid duel target username"):
            duel_features.set_duel_participant_control(True, "main|无咎子", "bad target!")

    def test_invalid_titan_beast_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid titan beast mode"):
            duel_features.set_titan_beast_mode("rest")

    def test_one_to_many_plan_cycles_targets_and_stops_when_complete(self):
        duel_features.configure_duel_multi_plan(
            "main",
            "无咎子",
            [
                {"username": "TitanCreeper", "count": 2},
                {"username": "Gamling33", "count": 1},
            ],
            enabled=True,
        )

        first = duel_features.reserve_duel_for_account("main")
        self.assertEqual((first["queue_key"], first["target_username"]), ("multi", "TitanCreeper"))
        duel_features.finish_duel_reservation(first, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })

        state = duel_features.load_duel_state()
        state["multi"]["next_at"] = ""
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        second = duel_features.reserve_duel_for_account("main")
        self.assertEqual(second["target_username"], "Gamling33")
        duel_features.finish_duel_reservation(second, {
            "status": "settled", "outcome": "失败", "remaining": 8,
        })

        state = duel_features.load_duel_state()
        state["multi"]["next_at"] = ""
        state["target_next_at"].pop("titancreeper", None)
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        third = duel_features.reserve_duel_for_account("main")
        self.assertEqual(third["target_username"], "TitanCreeper")
        duel_features.finish_duel_reservation(third, {
            "status": "settled", "outcome": "胜利", "remaining": 7,
        })

        multi = duel_features.load_duel_state()["multi"]
        self.assertFalse(multi["enabled"])
        self.assertEqual(multi["last_result"], "一对多计划已全部完成")
        self.assertEqual([item["remaining"] for item in multi["targets"]], [0, 0])

    def test_one_to_many_target_interval_is_at_least_eleven_minutes(self):
        duel_features.configure_duel_multi_plan(
            "sub",
            "厚土",
            [{"username": "ExternalTarget", "count": 2}],
            enabled=True,
        )

        reservation = duel_features.reserve_duel_for_account("sub")
        state = duel_features.load_duel_state()
        ready_at = duel_features.parse_duel_time(state["target_next_at"]["externaltarget"])
        reserved_at = duel_features.parse_duel_time(reservation["reserved_at"])

        self.assertGreaterEqual(
            (ready_at - reserved_at).total_seconds(),
            duel_features.DUEL_TARGET_INTERVAL_SECONDS,
        )
        self.assertGreaterEqual(
            (ready_at - datetime.now()).total_seconds(),
            duel_features.DUEL_TARGET_INTERVAL_SECONDS - 2,
        )

    def test_busy_result_does_not_shorten_one_to_many_target_interval(self):
        duel_features.configure_duel_multi_plan(
            "sub",
            "厚土",
            [{"username": "ExternalTarget", "count": 2}],
            enabled=True,
        )

        reservation = duel_features.reserve_duel_for_account("sub")
        reserved_ready = duel_features.parse_duel_time(
            duel_features.load_duel_state()["target_next_at"]["externaltarget"]
        )
        duel_features.finish_duel_reservation(reservation, {
            "status": "busy",
            "outcome": "目标繁忙",
            "wait_seconds": duel_features.DUEL_BUSY_RETRY_SECONDS,
        })

        ready_at = duel_features.parse_duel_time(
            duel_features.load_duel_state()["target_next_at"]["externaltarget"]
        )
        self.assertGreaterEqual(ready_at, reserved_ready)
        self.assertGreaterEqual(
            (ready_at - datetime.now()).total_seconds(),
            duel_features.DUEL_TARGET_INTERVAL_SECONDS - 2,
        )

    def test_avatar_target_must_be_prepared_before_one_to_many_reservation(self):
        duel_features.configure_duel_multi_plan(
            "main",
            "无咎子",
            [{"username": "ding303", "count": 1}],
            enabled=True,
        )

        self.assertIsNone(duel_features.reserve_duel_for_account("main"))
        preparation = duel_features.load_duel_state()["multi"]["preparation"]
        self.assertEqual(
            (preparation["owner"], preparation["target_identity"], preparation["status"]),
            ("sub", "寻真子", "pending"),
        )

        claim = duel_features.claim_duel_target_preparation("sub")
        self.assertIsNotNone(claim)
        self.assertTrue(
            duel_features.finish_duel_target_preparation(
                claim,
                True,
                "寻真子已发送切换消息",
                reply_to_msg_id=8101,
            )
        )

        reservation = duel_features.reserve_duel_for_account("main")
        self.assertEqual((reservation["queue_key"], reservation["target_username"]), ("multi", "ding303"))
        self.assertEqual(reservation["command"], ".斗法")
        self.assertEqual(reservation["reply_to_msg_id"], 8101)
        self.assertEqual(
            duel_features.load_duel_state()["multi"]["preparation"]["status"],
            "holding",
        )
        duel_features.finish_duel_reservation(reservation, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })
        self.assertEqual(duel_features.load_duel_state()["multi"]["preparation"], {})

    def test_stale_ready_preparation_without_reply_anchor_is_recreated(self):
        duel_features.configure_duel_multi_plan(
            "main",
            "无咎子",
            [{"username": "ding303", "count": 1}],
            enabled=True,
        )
        self.assertIsNone(duel_features.reserve_duel_for_account("main"))
        state = duel_features.load_duel_state()
        old_run_id = state["multi"]["preparation"]["run_id"]
        state["multi"]["preparation"]["status"] = "ready"
        state["multi"]["preparation"].pop("reply_to_msg_id", None)
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)

        self.assertIsNone(duel_features.reserve_duel_for_account("main"))
        recreated = duel_features.load_duel_state()["multi"]["preparation"]

        self.assertEqual(recreated["status"], "pending")
        self.assertNotEqual(recreated["run_id"], old_run_id)

    def test_target_preparation_switches_and_confirms_requested_avatar(self):
        class Actor(duel_features.DuelMixin):
            account_key = "sub"
            avatars = ["寻真子"]

            def __init__(self):
                self.current_identity = "主魂"
                self.calls = []

            async def prepare_identity_for_time_critical_command(self, identity, command="", timeout=0, **kwargs):
                self.calls.append((identity, command, timeout, kwargs))
                self.current_identity = identity
                return True, 8123

        actor = Actor()
        success, detail, reply_to_msg_id = asyncio.run(actor.prepare_duel_target_identity({
            "owner": "sub",
            "target_identity": "寻真子",
            "target_username": "ding303",
        }))

        self.assertTrue(success, detail)
        self.assertEqual(actor.current_identity, "寻真子")
        self.assertEqual(reply_to_msg_id, 8123)
        self.assertEqual(actor.calls, [(
            "寻真子",
            ".斗法",
            30,
            {"force_fresh": True, "return_switch_message_id": True},
        )])

    def test_fresh_target_switch_returns_the_outgoing_message_id(self):
        class Actor(CommonCommandMixin):
            avatars = ["厚土"]

            def __init__(self):
                self.state = {}
                self._current_identity = "厚土"
                self._main_confirmed = False
                self.avatar_send_lock = asyncio.Lock()
                self.last_sent_id = 7001
                self.calls = []

            def save_state(self):
                pass

            async def _send_and_wait_feedback_raw(self, command, **kwargs):
                self.calls.append((command, kwargs))
                self.last_sent_id = 7002
                return "已切换至厚土"

        actor = Actor()
        with patch("common_command_features.command_send_precheck", return_value=True):
            prepared, message_id = asyncio.run(
                actor.prepare_identity_for_time_critical_command(
                    "厚土",
                    command=".斗法",
                    timeout=30,
                    force_fresh=True,
                    return_switch_message_id=True,
                )
            )

        self.assertTrue(prepared)
        self.assertEqual(message_id, 7002)
        self.assertEqual(actor.calls[0][0], ".切换 厚土")
        self.assertFalse(actor.calls[0][1]["delete_after"])

    def test_avatar_duel_replies_to_switch_message_with_plain_command(self):
        class Actor(duel_features.DuelMixin):
            is_running = True
            last_sent_id = 9001

            def __init__(self):
                self.calls = []
                self.client = SimpleNamespace()
                self.target_chat_id = -1001

            async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
                self.calls.append((identity, command, kwargs))
                return SimpleNamespace(
                    id=9002,
                    text="胜者：@wuxinglinggen\n败者：@ding303\n胜负已分！\n今日神念：9/10",
                    reply_to=SimpleNamespace(reply_to_msg_id=9001),
                )

        reservation = {
            "queue_key": "multi",
            "run_id": "run-reply",
            "participant_key": "main|无咎子",
            "account": "main",
            "identity": "无咎子",
            "challenger_username": "wuxinglinggen",
            "target_id": "target-1",
            "target_username": "ding303",
            "target_account": "sub",
            "target_identity": "寻真子",
            "preparation_run_id": "prep-1",
            "reply_to_msg_id": 8101,
            "command": ".斗法",
        }
        actor = Actor()
        with (
            patch.object(duel_features, "record_duel_event"),
            patch.object(duel_features, "finish_duel_reservation"),
        ):
            result = asyncio.run(actor.execute_duel_reservation(reservation))

        self.assertEqual(result["status"], "settled")
        self.assertEqual(actor.calls[0][0:2], ("无咎子", ".斗法"))
        self.assertEqual(actor.calls[0][2]["reply_to"], 8101)

    def test_one_to_many_rejects_target_on_same_account(self):
        with self.assertRaisesRegex(ValueError, "same account duel target"):
            duel_features.configure_duel_multi_plan(
                "main",
                "无咎子",
                [{"username": "kulipabp", "count": 1}],
            )

    def test_one_to_many_rejects_empty_target(self):
        with self.assertRaisesRegex(ValueError, "invalid duel target username"):
            duel_features.configure_duel_multi_plan(
                "main",
                "无咎子",
                [{"username": "", "count": 1}],
            )

    def test_dashboard_payload_exposes_one_to_many_plan(self):
        duel_features.configure_duel_multi_plan(
            "xiaohao",
            "素心子",
            [{"username": "Waaiging", "count": 3}],
            enabled=False,
        )

        payload = duel_features.duel_dashboard_payload()

        self.assertTrue(payload["multi"]["configured"])
        self.assertEqual(payload["multi"]["initiator"]["identity"], "素心子")
        self.assertEqual(payload["multi"]["targets"][0]["count"], 3)
        self.assertEqual(payload["target_interval_seconds"], 11 * 60)
        self.assertTrue(any(item["username"] == "ding303" for item in payload["identity_options"]))

        duel_features.configure_duel_multi_plan(
            "main",
            "无咎子",
            [{"username": "ding303", "count": 1}],
            enabled=False,
        )
        avatar_target = duel_features.duel_dashboard_payload()["multi"]["targets"][0]
        self.assertEqual(avatar_target["duel_method"], "reply_switch")


if __name__ == "__main__":
    unittest.main()
