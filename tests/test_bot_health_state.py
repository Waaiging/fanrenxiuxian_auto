import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import log_utils
from state_io import StateFileLockTimeout


class BotHealthStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "health.json"
        self.patcher = patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", str(self.path))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.actor = SimpleNamespace(account_key="main", target_chat_id=-10012345)

    def test_expired_unanswered_probe_allows_a_new_probe(self):
        with patch.object(log_utils.time, "time", return_value=1000):
            self.assertTrue(log_utils._record_shared_command_probe(self.actor, command=".问道"))
        with patch.object(log_utils.time, "time", return_value=1001):
            self.assertFalse(log_utils._record_shared_command_probe(self.actor, command=".探寻裂缝"))
        with patch.object(log_utils.time, "time", return_value=1001 + log_utils.BOT_COMMAND_SILENCE_MAX_SECONDS):
            self.assertTrue(log_utils._record_shared_command_probe(self.actor, command=".探寻裂缝"))
        self.assertEqual(log_utils._read_shared_bot_activity()["command_probe"]["command"], ".探寻裂缝")

    def test_future_probe_does_not_freeze_detection_after_clock_correction(self):
        with patch.object(log_utils.time, "time", return_value=2000):
            log_utils._record_shared_command_probe(self.actor, command=".问道")
        with patch.object(log_utils.time, "time", return_value=1000):
            self.assertTrue(log_utils._record_shared_command_probe(self.actor, command=".探寻裂缝"))

    def test_response_clears_pause_without_dropping_other_account_activity(self):
        other = SimpleNamespace(account_key="sub")
        log_utils.record_shared_game_bot_activity(other)
        log_utils._record_shared_bot_maintenance(self.actor, ".问道")
        self.assertTrue(log_utils._record_shared_command_response(self.actor, command=""))
        data = log_utils._read_shared_bot_activity()
        self.assertIn("sub", data["accounts"])
        self.assertNotIn("maintenance", data)
        self.assertEqual(data["command_response"]["command"], "")

    def test_concurrent_process_updates_preserve_every_account(self):
        gate = Path(self.directory.name) / "go"
        script = r'''
import sys,time
from pathlib import Path
from types import SimpleNamespace
import log_utils
log_utils.BOT_ACTIVITY_SHARED_FILE=sys.argv[1]
actor=SimpleNamespace(account_key=sys.argv[2])
gate=Path(sys.argv[3])
deadline=time.monotonic()+20
while not gate.exists():
    if time.monotonic()>deadline:raise SystemExit('gate timeout')
    time.sleep(0.01)
for i in range(20):
    deadline=time.monotonic()+15
    while not log_utils.record_shared_game_bot_activity(actor,SimpleNamespace(username=str(i))):
        if time.monotonic()>deadline:raise SystemExit('write deadline exceeded')
        time.sleep(0.02)
'''
        processes = []
        try:
            for account in ("main", "sub", "xiaohao", "waaiging"):
                processes.append(subprocess.Popen(
                    [sys.executable, "-X", "utf8", "-c", script,
                     str(self.path), account, str(gate)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                ))
            gate.touch()
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                # A saturated disk may hit the deliberately short lock timeout.
                # Each successful transaction must still preserve other writers.
                for line in stderr.splitlines():
                    self.assertTrue(line.startswith("Shared bot health update failed: state lock timeout:"), line)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        accounts = json.loads(self.path.read_text(encoding="utf-8"))["accounts"]
        self.assertEqual(set(accounts), {"main", "sub", "xiaohao", "waaiging"})
        self.assertTrue(all(item["bot_username"] == "19" for item in accounts.values()))

    def test_lock_timeout_reports_failure_without_overwriting_shared_state(self):
        self.assertTrue(log_utils.record_shared_game_bot_activity(self.actor))
        before = self.path.read_bytes()
        with patch.object(log_utils, "update_json_state", side_effect=StateFileLockTimeout("busy")):
            with self.assertLogs("log_utils", level="WARNING"):
                self.assertFalse(log_utils.record_shared_game_bot_activity(SimpleNamespace(account_key="sub")))
        self.assertEqual(self.path.read_bytes(), before)


class BotHealthMessageOrderingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "health.json"
        self.enterContext(patch.object(log_utils, "BOT_ACTIVITY_SHARED_FILE", str(self.path)))
        self.enterContext(patch.object(log_utils, "MESSAGE_EVENTS_DB_FILE", str(self.path.with_suffix(".sqlite3"))))
        self.enterContext(patch.object(log_utils, "_MESSAGE_EVENTS_SCHEMA_READY", False))
        self.actor = SimpleNamespace(account_key="main", target_chat_id=-10012345)
        self.other = SimpleNamespace(account_key="sub", target_chat_id=-10012345)
        self.bot = SimpleNamespace(username="fanrenxiuxian_bot")

    def message(self, number, epoch, *, text=".元婴状态", reply_to=None, out=True, chat_id=-10012345):
        return SimpleNamespace(id=number, chat_id=chat_id, text=text, out=out,
                               date=datetime.fromtimestamp(epoch, timezone.utc),
                               reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to is not None else None,
                               sender_id=100 if out else 200)

    def probe(self, message, received):
        with patch.object(log_utils.time, "time", return_value=received):
            return log_utils._record_shared_command_probe(self.actor, message, command=message.text)

    def response(self, message, received):
        with patch.object(log_utils.time, "time", return_value=received):
            return log_utils._record_shared_command_response(self.other, command=".元婴状态", msg=message, sender=self.bot)

    def silence(self, now):
        with patch.object(log_utils.time, "time", return_value=now):
            return log_utils.shared_bot_command_silence_status(self.actor)

    def test_other_players_commands_are_audited_without_pausing_our_accounts(self):
        foreign = self.message(100, 1000, text=".卜筮问天", out=False)
        with patch.object(log_utils.time, "time", return_value=1001):
            self.assertTrue(log_utils.record_message_event(self.actor, foreign, sender=SimpleNamespace(username="player")))
        self.assertFalse(self.silence(1040)["active"])
        with log_utils._message_db_connect() as connection:
            self.assertEqual(connection.execute("SELECT text FROM message_events").fetchone()[0], foreign.text)

    def test_own_avatar_command_without_out_flag_still_arms_the_guard(self):
        command = self.message(100, 1000, out=False)
        command.sender_id = -10098765
        self.actor._avatar_chat_ids = {-10098765: "化身"}
        with patch.object(log_utils.time, "time", return_value=1001):
            self.assertTrue(log_utils.record_message_event(self.actor, command))
        self.assertTrue(self.silence(1040)["active"])

    def test_late_duplicate_cannot_reopen_an_answered_command(self):
        command = self.message(100, 1000)
        self.assertTrue(self.probe(command, 1001))
        self.response(self.message(101, 1002, reply_to=100, out=False), 1003)
        self.assertFalse(self.probe(command, 1009))
        self.assertFalse(self.silence(1040)["active"])

    def test_saved_legacy_reply_id_overrides_inverted_receive_timestamps(self):
        self.path.write_text(json.dumps({
            "command_probe": {"command": ".元婴状态", "chat_id": -10012345, "msg_id": 100, "wall_epoch": 1009},
            "command_response": {"command": ".元婴状态", "chat_id": -10012345, "reply_to_msg_id": 100, "wall_epoch": 1003},
        }), encoding="utf-8")
        before = self.path.read_bytes()
        self.assertFalse(self.silence(1040)["active"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_delayed_old_command_does_not_follow_a_newer_response(self):
        self.response(self.message(101, 1005, reply_to=99, out=False), 1006)
        self.assertFalse(self.probe(self.message(100, 1000), 1009))
        self.assertFalse(self.silence(1040)["active"])

    def test_receive_delay_does_not_restart_the_response_grace_period(self):
        self.assertTrue(self.probe(self.message(100, 1000), 1020))
        self.assertTrue(self.silence(1035)["active"])

    def test_stale_history_command_does_not_arm_a_new_pause(self):
        self.assertFalse(self.probe(self.message(100, 1000), 1700))
        self.assertFalse(self.silence(1735)["active"])

    def test_bot_reply_seen_by_another_account_resolves_the_shared_probe(self):
        command = self.message(100, 1000)
        with patch.object(log_utils.time, "time", return_value=1001):
            log_utils.record_message_event(self.actor, command)
        self.assertTrue(self.silence(1035)["active"])
        reply = self.message(101, 1040, text="元婴状态", reply_to=100, out=False)
        with patch.object(log_utils.time, "time", return_value=1041):
            log_utils.record_game_bot_activity(self.other, self.bot, msg=reply)
        self.assertFalse(self.silence(1042)["active"])

    def test_same_message_number_in_another_chat_is_not_a_matching_reply(self):
        self.probe(self.message(100, 1000), 1001)
        self.response(self.message(101, 990, reply_to=100, out=False, chat_id=-10054321), 999)
        self.assertTrue(self.silence(1040)["active"])

    def test_delayed_older_reply_does_not_hide_a_new_unanswered_command(self):
        self.response(self.message(90, 1000, reply_to=89, out=False), 1001)
        self.probe(self.message(100, 1100), 1101)
        self.response(self.message(91, 1002, reply_to=89, out=False), 1102)
        self.assertTrue(self.silence(1140)["active"])

    def test_prior_response_in_same_second_does_not_answer_a_new_command(self):
        self.response(self.message(99, 1000, reply_to=98, out=False), 1001)
        self.assertTrue(self.probe(self.message(100, 1000), 1002))
        self.assertTrue(self.silence(1040)["active"])

    def test_delayed_prior_response_in_same_second_does_not_clear_pending_command(self):
        self.probe(self.message(100, 1000), 1001)
        self.response(self.message(99, 1000, reply_to=98, out=False), 1002)
        self.assertTrue(self.silence(1040)["active"])

    def test_later_response_in_same_second_confirms_bot_responsiveness(self):
        self.probe(self.message(100, 1000), 1001)
        self.response(self.message(101, 1000, reply_to=98, out=False), 1002)
        self.assertFalse(self.silence(1040)["active"])

    def test_out_of_order_same_second_responses_do_not_reopen_the_probe(self):
        self.probe(self.message(100, 1000), 1001)
        self.response(self.message(101, 1000, reply_to=98, out=False), 1002)
        self.response(self.message(99, 1000, reply_to=98, out=False), 1003)
        self.assertFalse(self.silence(1040)["active"])

    def test_same_second_response_from_another_chat_does_not_answer_the_probe(self):
        self.probe(self.message(100, 1000), 1001)
        self.response(self.message(101, 1000, reply_to=100, out=False, chat_id=-10054321), 1002)
        self.assertTrue(self.silence(1040)["active"])

    def test_matching_reply_wins_a_same_second_tie_between_chats(self):
        self.probe(self.message(100, 1000), 1001)
        unrelated = self.message(200, 1000, reply_to=100, out=False, chat_id=-10054321)
        self.response(unrelated, 1002)
        self.response(self.message(101, 1000, reply_to=100, out=False), 1003)
        self.response(unrelated, 1004)
        self.assertFalse(self.silence(1040)["active"])

    def test_cross_account_reply_lookup_tolerates_invalid_shared_state(self):
        self.path.write_text("[]", encoding="utf-8")
        reply = self.message(101, 1000, reply_to=100, out=False)
        self.assertEqual(log_utils.bot_command_response_text(self.other, reply), "")

    def test_stale_response_does_not_clear_shared_maintenance(self):
        with patch.object(log_utils.time, "time", return_value=1700):
            log_utils._record_shared_bot_maintenance(self.actor, ".探寻裂缝")
        self.response(self.message(101, 1000, reply_to=100, out=False), 1701)
        with patch.object(log_utils.time, "time", return_value=1702):
            self.assertTrue(log_utils.shared_bot_maintenance_status(self.actor)["active"])

    def test_edited_reply_uses_its_edit_time(self):
        self.probe(self.message(100, 1100), 1101)
        reply = self.message(90, 1000, reply_to=None, out=False)
        reply.edit_date = datetime.fromtimestamp(1102, timezone.utc)
        self.response(reply, 1103)
        self.assertFalse(self.silence(1140)["active"])


if __name__ == "__main__":
    unittest.main()
