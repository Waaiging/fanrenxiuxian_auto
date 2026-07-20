import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import duel_features


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

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_queue_composition_matches_confirmed_rules(self):
        waaiging = [(item["account"], item["identity"]) for item in duel_features.DUEL_QUEUES["waaiging"]["participants"]]
        titan = [(item["account"], item["identity"]) for item in duel_features.DUEL_QUEUES["titan"]["participants"]]
        self.assertEqual(waaiging, [
            ("main", "无咎子"), ("main", "缘生子"),
            ("xiaohao", "素心子"), ("xiaohao", "缘生子"),
        ])
        self.assertEqual(titan, [
            ("main", "素缘子"), ("sub", "厚土"),
            ("sub", "缘生子"), ("sub", "寻真子"),
        ])

    def test_independent_queues_can_be_reserved_for_main(self):
        first = duel_features.reserve_duel_for_account("main")
        second = duel_features.reserve_duel_for_account("main")
        self.assertEqual(first["queue_key"], "waaiging")
        self.assertEqual(first["identity"], "无咎子")
        self.assertEqual(second["queue_key"], "titan")
        self.assertEqual(second["identity"], "素缘子")
        state = duel_features.load_duel_state()
        for key in ("waaiging", "titan"):
            next_at = duel_features.parse_duel_time(state["queues"][key]["next_at"])
            started = duel_features.parse_duel_time(state["queues"][key]["last_attempt_at"])
            self.assertGreaterEqual((next_at - started).total_seconds(), 359)

    def test_lease_prevents_duplicate_reservation(self):
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

    def test_finish_reanchors_interval_after_a_delayed_execution(self):
        reservation = duel_features.reserve_duel_for_account("main")
        state = duel_features.load_duel_state()
        state["queues"]["waaiging"]["next_at"] = "2000-01-01 00:00:00"
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        duel_features.finish_duel_reservation(reservation, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })
        finished = duel_features.load_duel_state()["queues"]["waaiging"]
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

    def test_waaiging_queue_skips_xiaohao_only_while_write_restricted(self):
        state = duel_features.load_duel_state(write_back=True)
        state["queues"]["waaiging"]["cursor"] = 2
        state["queues"]["waaiging"]["next_at"] = ""
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
        self.assertEqual((restricted["queue_key"], restricted["identity"]), ("waaiging", "无咎子"))

        duel_features.finish_duel_reservation(restricted, {
            "status": "settled", "outcome": "胜利", "remaining": 9,
        })
        state = duel_features.load_duel_state()
        state["queues"]["waaiging"]["cursor"] = 2
        state["queues"]["waaiging"]["next_at"] = ""
        duel_features._atomic_write_json(duel_features.DUEL_STATE_FILE, state)
        with open(duel_features.XIAOHAO_STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({
                "current_identity": "主魂",
                "beasts_cache": [{"full_name": "六翼", "status": "出战中"}],
                "telegram_write_permission_monitor": {"status": "allowed"},
            }, handle, ensure_ascii=False)

        recovered = duel_features.reserve_duel_for_account("xiaohao")
        self.assertEqual((recovered["queue_key"], recovered["identity"]), ("waaiging", "素心子"))

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
