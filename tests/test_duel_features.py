import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import duel_features


ROTATION = duel_features.DUEL_ROTATION_QUEUE_KEY


FINAL_LOSS = """
【天道战报·文字版】
胜者：@Waaiging | 净得修为 +3.0万 | 法宝磨损 -5
败者：@kulipabp | 损失修为 -3.0万 | 法宝磨损 -10
胜负已分！
今日神念：5/10
"""


class DuelFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patchers = [
            patch.object(duel_features, "DUEL_STATE_FILE", os.path.join(self.temp.name, "duel.json")),
            patch.object(duel_features, "DUEL_LOCK_FILE", os.path.join(self.temp.name, "duel.lock")),
            patch.object(duel_features, "DUEL_DB_FILE", os.path.join(self.temp.name, "events.sqlite3")),
            patch.object(duel_features, "XIAOHAO_STATE_FILE", os.path.join(self.temp.name, "xiaohao.json")),
        ]
        for item in self.patchers:
            item.start()
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "出战中"}],
            }, handle, ensure_ascii=False)

    def use_external_rotation_targets(self):
        """Keep reservation-focused tests independent from target identity preparation."""
        state = duel_features.load_duel_state(write_back=True)
        rotation = state["queues"][ROTATION]
        for index, participant in enumerate(rotation["participants"].values()):
            participant["target_username"] = f"ExternalTarget{index + 1}"
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_queue_composition_matches_confirmed_rules(self):
        participants = [
            (item["account"], item["identity"])
            for item in duel_features.DUEL_QUEUES[ROTATION]["participants"]
        ]
        self.assertEqual(participants, [
            ("main", "主魂"), ("main", "无咎子"),
            ("main", "缘生子"), ("main", "素缘子"),
            ("sub", "主魂"), ("sub", "厚土"),
            ("sub", "缘生子"), ("sub", "寻真子"),
            ("xiaohao", "主魂"), ("xiaohao", "问心子"),
            ("xiaohao", "素心子"), ("xiaohao", "缘生子"),
            ("waaiging", "主魂"),
        ])

    def test_single_rotation_advances_across_all_identities(self):
        self.use_external_rotation_targets()
        first = duel_features.reserve_duel_for_account("main")
        self.assertEqual(first["queue_key"], ROTATION)
        self.assertEqual(first["identity"], "主魂")
        duel_features.finish_duel_reservation(first, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })
        state = duel_features.load_duel_state()
        state["queues"][ROTATION]["next_at"] = ""
        state["target_next_at"] = {}
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)

        second = duel_features.reserve_duel_for_account("main")
        self.assertEqual(second["queue_key"], ROTATION)
        self.assertEqual(second["identity"], "无咎子")
        state = duel_features.load_duel_state()
        next_at = duel_features.parse_duel_time(state["queues"][ROTATION]["next_at"])
        started = duel_features.parse_duel_time(state["queues"][ROTATION]["last_attempt_at"])
        self.assertGreaterEqual((next_at - started).total_seconds(), 359)

    def test_lease_prevents_duplicate_reservation(self):
        self.use_external_rotation_targets()
        first = duel_features.reserve_duel_for_account("main")
        self.assertIsNotNone(first)
        self.assertIsNone(duel_features.reserve_duel_for_account("xiaohao"))

    def test_result_parser_uses_final_report_fields(self):
        result = duel_features.parse_duel_result(FINAL_LOSS, "kulipabp", "Waaiging")
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["outcome"], "失败")
        self.assertEqual(result["remaining"], 5)
        self.assertEqual(result["cultivation_delta"], -30000)
        self.assertEqual(result["artifact_wear"], -10)

    def test_exhaustion_is_authoritative(self):
        result = duel_features.parse_duel_result(
            "你今日神念消耗过剧，已无力再战！每日可主动斗法 10 次",
            "wuxinglinggen",
            "Waaiging",
        )
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["remaining"], 0)

    def test_busy_target_response_is_final_and_retryable(self):
        text = "天机繁忙！对方正在进行另一场因果纠缠，请稍候再试。"

        self.assertTrue(duel_features.duel_text_is_final(text))
        result = duel_features.parse_duel_result(text, "crayonxxin", "Waaiging")

        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["outcome"], "目标繁忙")
        self.assertEqual(result["wait_seconds"], duel_features.DUEL_BUSY_RETRY_SECONDS)

    def test_rolling_target_limit_blocks_one_to_many_target_for_24_hours(self):
        duel_features.configure_duel_multi_plan(
            "main",
            "无咎子",
            [{"username": "ExternalTarget", "count": 2}],
            enabled=True,
        )
        reservation = duel_features.reserve_duel_for_account("main")
        text = "天道有则！你与 @ExternalTarget 在24小时内已交锋过多，暂不可再次斗法！"

        self.assertTrue(duel_features.duel_text_is_final(text))
        result = duel_features.parse_duel_result(
            text,
            reservation["challenger_username"],
            reservation["target_username"],
        )
        self.assertEqual(result["status"], "cooldown")
        self.assertEqual(result["outcome"], "目标24小时冷却")
        self.assertEqual(
            result["wait_seconds"],
            duel_features.DUEL_ROLLING_TARGET_COOLDOWN_SECONDS,
        )

        duel_features.finish_duel_reservation(reservation, result)
        state = duel_features.load_duel_state()
        target = state["multi"]["targets"][0]
        target_ready = duel_features.parse_duel_time(
            state["target_next_at"]["externaltarget"]
        )
        self.assertEqual(target["attempts"], 0)
        self.assertEqual(target["remaining"], 2)
        self.assertGreaterEqual(
            (target_ready - datetime.now()).total_seconds(),
            duel_features.DUEL_ROLLING_TARGET_COOLDOWN_SECONDS - 2,
        )

    def test_busy_target_does_not_consume_attempt_and_retries_soon(self):
        self.use_external_rotation_targets()
        reservation = duel_features.reserve_duel_for_account("main")
        result = duel_features.parse_duel_result(
            "天机繁忙！对方正在进行另一场因果纠缠，请稍候再试。",
            reservation["challenger_username"],
            reservation["target_username"],
        )

        duel_features.finish_duel_reservation(reservation, result)

        state = duel_features.load_duel_state()
        queue = state["queues"][reservation["queue_key"]]
        participant = queue["participants"][reservation["participant_key"]]
        retry_delay = (duel_features.parse_duel_time(queue["next_at"]) - datetime.now()).total_seconds()
        target_delay = (
            duel_features.parse_duel_time(
                state["target_next_at"][reservation["target_username"].lower()]
            )
            - datetime.now()
        ).total_seconds()
        self.assertEqual(participant["attempts"], 0)
        self.assertEqual(participant["remaining"], duel_features.DUEL_DAILY_LIMIT)
        self.assertEqual(participant["last_result"], "目标繁忙")
        self.assertGreaterEqual(retry_delay, duel_features.DUEL_BUSY_RETRY_SECONDS - 2)
        self.assertLessEqual(retry_delay, duel_features.DUEL_BUSY_RETRY_SECONDS)
        self.assertGreaterEqual(
            target_delay,
            duel_features.DUEL_TARGET_INTERVAL_SECONDS - 2,
        )

    def test_escape_result_is_settled_and_consumes_one_attempt(self):
        text = "面对境界压制，@Ding303 凭借神通侥幸逃脱！(成功率: 16%)"
        self.assertTrue(duel_features.duel_text_is_final(text))
        result = duel_features.parse_duel_result(text, "Ding303", "Waaiging")
        self.assertEqual(result["status"], "settled")
        self.assertEqual(result["outcome"], "逃脱")

        self.use_external_rotation_targets()
        reservation = duel_features.reserve_duel_for_account("main")
        duel_features.finish_duel_reservation(reservation, result)
        participant = duel_features.load_duel_state()["queues"][ROTATION]["participants"][
            reservation["participant_key"]
        ]
        self.assertEqual(participant["attempts"], 1)
        self.assertEqual(participant["remaining"], duel_features.DUEL_DAILY_LIMIT - 1)

    def test_same_target_is_blocked_within_rotation_after_completion(self):
        state = duel_features.load_duel_state(write_back=True)
        rotation = state["queues"][ROTATION]
        for key, participant in rotation["participants"].items():
            participant["enabled"] = key in {"main|主魂", "main|无咎子"}
            if participant["enabled"]:
                participant["target_username"] = "ExternalShared"
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)

        first = duel_features.reserve_duel_for_account("main")
        self.assertEqual(first["queue_key"], ROTATION)
        duel_features.finish_duel_reservation(first, {
            "status": "settled", "outcome": "失败", "remaining": 9,
        })

        state = duel_features.load_duel_state()
        state["queues"][ROTATION]["next_at"] = ""
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)

        self.assertIsNone(duel_features.reserve_duel_for_account("main"))
        blocked = duel_features.load_duel_state()["queues"][ROTATION]
        self.assertIn("@ExternalShared", blocked["last_result"])
        self.assertGreater(
            duel_features.parse_duel_time(blocked["next_at"]),
            datetime.now(),
        )

    def test_finish_reanchors_interval_after_a_delayed_execution(self):
        self.use_external_rotation_targets()
        reservation = duel_features.reserve_duel_for_account("main")
        state = duel_features.load_duel_state()
        state["queues"][ROTATION]["next_at"] = "2000-01-01 00:00:00"
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        duel_features.finish_duel_reservation(reservation, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })
        finished = duel_features.load_duel_state()["queues"][ROTATION]
        self.assertGreaterEqual(
            (duel_features.parse_duel_time(finished["next_at"]) - datetime.now()).total_seconds(),
            duel_features.DUEL_INTERVAL_SECONDS - 2,
        )

    def test_final_report_can_arrive_as_new_reply_message(self):
        command_id = 100
        pending = SimpleNamespace(
            id=101,
            text="战斗结束，正在整理天道战报...",
            reply_to=SimpleNamespace(reply_to_msg_id=command_id),
        )
        final = SimpleNamespace(
            id=102,
            text=FINAL_LOSS,
            reply_to=SimpleNamespace(reply_to_msg_id=command_id),
        )

        class Client:
            async def get_messages(self, chat_id, **kwargs):
                if "ids" in kwargs:
                    return pending
                return [final]

        actor = SimpleNamespace(client=Client(), target_chat_id=-1001, is_running=True)
        message, text = asyncio.run(
            duel_features.wait_for_duel_result(actor, pending, command_msg_id=command_id, timeout=2)
        )
        self.assertEqual(message.id, 102)
        self.assertIn("胜者", text)

    def test_final_report_wait_budget_covers_slow_bot_settlement(self):
        command_id = 200
        pending = SimpleNamespace(
            id=201,
            text="战斗结束，正在整理天道战报...",
            reply_to=SimpleNamespace(reply_to_msg_id=command_id),
        )
        final = SimpleNamespace(
            id=202,
            text=FINAL_LOSS,
            reply_to=SimpleNamespace(reply_to_msg_id=command_id),
        )

        class Client:
            def __init__(self):
                self.polls = 0

            async def get_messages(self, chat_id, **kwargs):
                if "ids" not in kwargs:
                    return []
                self.polls += 1
                return final if self.polls >= 42 else pending

        async def no_sleep(_seconds):
            return None

        actor = SimpleNamespace(client=Client(), target_chat_id=-1001, is_running=True)
        clock = iter(range(1000))
        with (
            patch.object(duel_features.asyncio, "sleep", new=no_sleep),
            patch.object(duel_features.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            message, text = asyncio.run(
                duel_features.wait_for_duel_result(
                    actor,
                    pending,
                    command_msg_id=command_id,
                    timeout=duel_features.DUEL_RESULT_WAIT_SECONDS,
                )
            )

        self.assertEqual(message.id, 202)
        self.assertIn("胜者", text)

    def test_event_recording_is_deduplicated(self):
        reservation = {
            "run_id": "run-1", "queue_key": "waaiging", "account": "main",
            "identity": "缘生子", "challenger_username": "kulipabp",
            "target_username": "Waaiging", "command": ".斗法 @Waaiging",
        }
        result = duel_features.parse_duel_result(FINAL_LOSS, "kulipabp", "Waaiging")
        duel_features.record_duel_event(reservation, result, command_msg_id=100, response_msg_id=102)
        duel_features.record_duel_event(reservation, result, command_msg_id=100, response_msg_id=102)
        rows = duel_features._duel_event_rows(datetime.now().strftime("%Y-%m-%d"), 20)
        self.assertEqual(len(rows), 1)

    def test_titan_gate_requires_main_soul_and_deployed_beast(self):
        self.assertTrue(duel_features.titan_target_status()["ready"])
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "缘生子",
                "beasts_cache": [{"full_name": "六翼", "status": "放养中"}],
            }, handle, ensure_ascii=False)
        self.assertFalse(duel_features.titan_target_status()["ready"])

    def test_titan_gate_rejects_retired_pasture_mode(self):
        with self.assertRaises(ValueError):
            duel_features.set_titan_beast_mode("pasture")

    def test_titan_gate_reports_send_restriction_when_selected_mode_is_not_applied(self):
        duel_features.set_titan_beast_mode("deploy")
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "放养中"}],
                "telegram_send_protection_stop": {
                    "reason": "write_restricted",
                    "error": "CHAT_WRITE_FORBIDDEN",
                },
            }, handle, ensure_ascii=False)

        status = duel_features.titan_target_status()

        self.assertFalse(status["ready"])
        self.assertTrue(status["preparation_blocked"])
        self.assertEqual(status["preparation_reason"], "小号当前无群组发送权限")

    def test_titan_preparation_only_runs_when_next_rotation_target_is_titan(self):
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "缘生子",
                "beasts_cache": [{"full_name": "六翼", "status": "放养中"}],
            }, handle, ensure_ascii=False)

        self.assertIsNone(duel_features.claim_titan_preparation("xiaohao"))

        state = duel_features.load_duel_state(write_back=True)
        rotation = state["queues"][ROTATION]
        for key, participant in rotation["participants"].items():
            participant["enabled"] = key == "sub|主魂"
        rotation["cursor"] = next(
            index
            for index, item in enumerate(duel_features.DUEL_QUEUES[ROTATION]["participants"])
            if (item["account"], item["identity"]) == ("sub", "主魂")
        )
        rotation["next_at"] = ""
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)

        claim = duel_features.claim_titan_preparation("xiaohao")
        self.assertIsNotNone(claim)
        self.assertEqual(claim["queue_key"], ROTATION)

    def test_rotation_exposes_titan_send_restriction_in_waiting_result(self):
        for item in duel_features.DUEL_QUEUES[ROTATION]["participants"]:
            key = duel_features.duel_participant_key(item["account"], item["identity"])
            duel_features.set_duel_participant_control(key == "main|主魂", key)
        duel_features.set_duel_participant_control(True, "main|主魂", "TitanCreeper")
        duel_features.set_titan_beast_mode("deploy")
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "放养中"}],
                "telegram_send_protection_stop": {
                    "reason": "write_restricted",
                    "error": "CHAT_WRITE_FORBIDDEN",
                },
            }, handle, ensure_ascii=False)

        self.assertIsNone(duel_features.reserve_duel_for_account("main"))
        state = duel_features.load_duel_state()
        self.assertEqual(
            state["queues"][ROTATION]["last_result"],
            "等待小号群组发送权限恢复后切换六翼出战",
        )

    def test_titan_preparation_applies_deploy_mode_on_main_soul(self):
        duel_features.set_titan_beast_mode("deploy")
        state_path = duel_features.XIAOHAO_STATE_FILE

        class Actor(duel_features.DuelMixin):
            account_key = "xiaohao"
            beast_lock = None

            def __init__(self):
                self.current_identity = "缘生子"
                self.state = {
                    "current_identity": self.current_identity,
                    "best_beast_name": "六翼",
                    "best_beast_status": "放养中",
                    "beasts_cache": [{"full_name": "六翼", "status": "放养中"}],
                }
                self.sent = []
                self.save_state()

            def save_state(self):
                with open(state_path, "w", encoding="utf-8") as handle:
                    json.dump(self.state, handle, ensure_ascii=False)

            async def switch_back_to_main(self, force=False):
                self.sent.append("切回主魂")
                self.current_identity = "主魂"
                self.state["current_identity"] = "主魂"
                self.save_state()

            def get_cached_beast_by_name(self, name):
                return self.state["beasts_cache"][0]

            async def send_and_wait_feedback(self, command, **kwargs):
                self.sent.append(command)
                if command == ".灵兽休息 六翼":
                    return "灵兽【六翼】已回到休息状态。"
                if command == ".灵兽出战 六翼":
                    return "灵兽【六翼】已进入出战状态。"
                return ""

            @staticmethod
            def response_text(response):
                return str(response or "")

            @staticmethod
            def parse_rest_response_status(response):
                return "休息中" if "休息" in response else ""

            @staticmethod
            def is_beast_deploy_success(response):
                return "出战" in response

            def set_best_beast_status(self, name, status):
                self.state["best_beast_status"] = status
                self.state["beasts_cache"][0]["status"] = status
                self.save_state()

        actor = Actor()
        success, detail = asyncio.run(actor.prepare_titan_target_for_duel())

        self.assertTrue(success, detail)
        self.assertEqual(actor.sent, ["切回主魂", ".灵兽休息 六翼", ".灵兽出战 六翼"])
        self.assertIn("六翼已出战", detail)

    def test_rotation_skips_xiaohao_only_while_write_restricted(self):
        state = duel_features.load_duel_state(write_back=True)
        rotation = state["queues"][ROTATION]
        for key, participant in rotation["participants"].items():
            participant["enabled"] = key in {"xiaohao|素心子", "main|主魂"}
            if participant["enabled"]:
                participant["target_username"] = (
                    "ExternalXiaohaoTarget" if key == "xiaohao|素心子" else "ExternalMainTarget"
                )
        xiaohao_index = next(
            index
            for index, item in enumerate(duel_features.DUEL_QUEUES[ROTATION]["participants"])
            if (item["account"], item["identity"]) == ("xiaohao", "素心子")
        )
        rotation["cursor"] = xiaohao_index
        rotation["next_at"] = ""
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "出战中"}],
                "telegram_send_protection_stop": {
                    "reason": "write_restricted",
                    "error": "CHAT_WRITE_FORBIDDEN",
                },
                "telegram_write_permission_monitor": {"status": "blocked"},
            }, handle, ensure_ascii=False)

        restricted = duel_features.reserve_duel_for_account("main")
        self.assertEqual((restricted["queue_key"], restricted["identity"]), (ROTATION, "主魂"))

        duel_features.finish_duel_reservation(restricted, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })
        state = duel_features.load_duel_state()
        state["queues"][ROTATION]["participants"]["main|主魂"]["enabled"] = False
        state["queues"][ROTATION]["cursor"] = xiaohao_index
        state["queues"][ROTATION]["next_at"] = ""
        state["target_next_at"] = {}
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "出战中"}],
                "telegram_write_permission_monitor": {"status": "allowed"},
            }, handle, ensure_ascii=False)

        recovered = duel_features.reserve_duel_for_account("xiaohao")
        self.assertEqual((recovered["queue_key"], recovered["identity"]), (ROTATION, "素心子"))

    def test_titan_target_can_be_challenged_while_xiaohao_send_is_restricted(self):
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "出战中"}],
                "telegram_send_protection_stop": {
                    "reason": "write_restricted",
                    "error": "CHAT_WRITE_FORBIDDEN",
                },
            }, handle, ensure_ascii=False)

        self.assertTrue(duel_features.titan_target_status()["ready"])
        self.assertTrue(duel_features.xiaohao_duel_send_status()["restricted"])


if __name__ == "__main__":
    unittest.main()
