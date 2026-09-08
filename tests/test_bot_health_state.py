import json
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


if __name__ == "__main__":
    unittest.main()
