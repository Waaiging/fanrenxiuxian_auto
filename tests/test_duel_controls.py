import os
import tempfile
import unittest
from unittest.mock import patch

import duel_features


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

    def test_invalid_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid duel target username"):
            duel_features.set_duel_participant_control(True, "main|无咎子", "bad target!")


if __name__ == "__main__":
    unittest.main()
