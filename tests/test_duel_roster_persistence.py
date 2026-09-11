"""Preferences must survive workers restoring different reborn avatar names."""

import copy
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import automation_settings
import duel_features


class DuelRosterPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime(2026, 9, 11, 10, 0)
        self.accounts = {
            "main": {
                "avatar_dao_names_by_player_id": {"-1003809391782": "玄续子"},
                "avatar_dao_name_aliases": {"缘生子": "玄续子"},
            },
            "sub": {
                "avatar_dao_names_by_player_id": {
                    "-1003885521329": "岚衍子",
                    "-1003340352216": "寒续尘",
                },
                "avatar_dao_name_aliases": {
                    "玄续玄": "岚衍子", "寻真子": "寒续尘",
                },
            },
            "xiaohao": {
                "avatar_dao_names_by_player_id": {"-1003996748766": "灵脉玄"},
                "avatar_dao_name_aliases": {"缘生子": "灵脉玄"},
            },
        }
        for account, state in self.accounts.items():
            path = self.root / f"state_{account}.json"
            self.write_json(path, state)
            attribute = {"main": "MAIN_STATE_FILE", "sub": "SUB_STATE_FILE",
                         "xiaohao": "XIAOHAO_STATE_FILE"}[account]
            self.enterContext(patch.object(automation_settings, attribute, path))

        self.dashboard = self.new_worker()
        self.dashboard.duel_dashboard_payload()
        self.preferences = {
            "main|玄续子": "Waaiging",
            "sub|寒续尘": "TitanCreeper",
            "xiaohao|灵脉玄": "Waaiging",
        }
        for key, target in self.preferences.items():
            self.dashboard.set_duel_participant_control(False, key, target)
        self.saved = self.dashboard.load_duel_state()
        for key in self.preferences:
            self.saved["queues"]["rotation"]["participants"][key].update({
                "attempts": 7, "remaining": 3, "wins": 2, "losses": 5,
            })
        self.write_json(self.root / "duel_state.json", self.saved)

    @staticmethod
    def write_json(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def new_worker(self):
        # Each import has its own module roster, like the Dashboard and four
        # independent account processes, but shares only disposable state files.
        spec = importlib.util.spec_from_file_location("isolated_duel_worker", duel_features.__file__)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        worker.DUEL_STATE_FILE = str(self.root / "duel_state.json")
        worker.DUEL_LOCK_FILE = str(self.root / "duel_state.lock")
        worker.DUEL_DB_FILE = str(self.root / "events.sqlite3")
        worker.XIAOHAO_STATE_FILE = str(self.root / "state_xiaohao.json")
        worker.duel_now = lambda: self.now
        return worker

    def assert_preferences(self, state, *, daily_reset=False, preferences=None):
        participants = state["queues"]["rotation"]["participants"]
        for key, target in (preferences or self.preferences).items():
            self.assertIn(key, participants)
            row = participants[key]
            self.assertFalse(row["enabled"], key)
            self.assertEqual(row["target_username"], target, key)
            self.assertEqual(row["remaining"], 10 if daily_reset else 3, key)
            self.assertEqual(row["attempts"], 0 if daily_reset else 7, key)

    def test_fresh_reader_keeps_disabled_reborn_participants(self):
        self.assert_preferences(self.new_worker().load_duel_state())
        self.assert_preferences(json.loads((self.root / "duel_state.json").read_text(encoding="utf-8")))
        payload = self.new_worker().duel_dashboard_payload()
        rows = {row["key"]: row for queue in payload["queues"] for row in queue["participants"]}
        for key, target in self.preferences.items():
            self.assertFalse(rows[key]["enabled"], key)
            self.assertEqual(rows[key]["target_username"], target, key)

    def test_startup_name_restore_preserves_other_accounts_and_custom_targets(self):
        for key in self.preferences:
            self.preferences[key] = "SavedOpponent"
            self.saved["queues"]["rotation"]["participants"][key]["target_username"] = "SavedOpponent"
        for account, old_name, new_name in (
            ("main", "缘生子", "玄续子"),
            ("sub", "寻真子", "寒续尘"),
            ("xiaohao", "缘生子", "灵脉玄"),
        ):
            with self.subTest(account=account):
                self.write_json(self.root / "duel_state.json", self.saved)
                worker = self.new_worker()
                worker.refresh_duel_identity_name(account, old_name, new_name)
                self.assert_preferences(json.loads((self.root / "duel_state.json").read_text(encoding="utf-8")))
                self.assert_preferences(self.new_worker().load_duel_state())

    def test_explicit_rename_keeps_new_name_before_account_snapshot_is_saved(self):
        # Startup can announce a name before save_state publishes its snapshot.
        self.write_json(self.root / "state_main.json", {})
        state = copy.deepcopy(self.saved)
        participants = state["queues"]["rotation"]["participants"]
        participants["main|缘生子"] = participants.pop("main|玄续子")
        self.write_json(self.root / "duel_state.json", state)

        worker = self.new_worker()
        self.assertTrue(worker.refresh_duel_identity_name("main", "缘生子", "玄续子"))
        self.assertEqual(worker.duel_identity_for_username("kulipabp")["identity"], "玄续子")
        self.assert_preferences(json.loads((self.root / "duel_state.json").read_text(encoding="utf-8")))

        self.write_json(self.root / "state_main.json", self.accounts["main"])
        self.assert_preferences(self.new_worker().load_duel_state())

    def test_separate_roster_refresh_does_not_lose_pending_state_migration(self):
        state = copy.deepcopy(self.saved)
        participants = state["queues"]["rotation"]["participants"]
        participants["sub|寻真子"] = participants.pop("sub|寒续尘")
        participants["xiaohao|缘生子"] = participants.pop("xiaohao|灵脉玄")
        self.write_json(self.root / "duel_state.json", state)

        worker = self.new_worker()
        worker.refresh_duel_identity_roster()  # Also called by surprise raids.
        self.assert_preferences(worker.load_duel_state())

    def test_stable_slot_migrates_without_account_alias_history(self):
        self.accounts["sub"]["avatar_dao_name_aliases"] = {}
        self.write_json(self.root / "state_sub.json", self.accounts["sub"])
        state = copy.deepcopy(self.saved)
        participants = state["queues"]["rotation"]["participants"]
        participants["sub|寻真子"] = participants.pop("sub|寒续尘")
        self.write_json(self.root / "duel_state.json", state)
        worker = self.new_worker()
        worker.refresh_duel_identity_roster()
        self.assert_preferences(worker.load_duel_state())

    def test_second_rebirth_migrates_saved_preferences_and_pending_plan(self):
        account = self.accounts["xiaohao"]
        account["avatar_dao_names_by_player_id"]["-1003996748766"] = "新灵脉子"
        account["avatar_dao_name_aliases"]["灵脉玄"] = "新灵脉子"
        self.write_json(self.root / "state_xiaohao.json", account)
        state = copy.deepcopy(self.saved)
        state["multi"].update({
            "enabled": True, "initiator_account": "xiaohao", "initiator_identity": "灵脉玄",
            "targets": [{"id": "saved", "username": "Waaiging", "count": 4, "remaining": 2}],
            "in_flight": {"account": "xiaohao", "identity": "灵脉玄",
                          "participant_key": "xiaohao|灵脉玄"},
        })
        self.write_json(self.root / "duel_state.json", state)

        loaded = self.new_worker().load_duel_state()
        expected = dict(self.preferences)
        expected["xiaohao|新灵脉子"] = expected.pop("xiaohao|灵脉玄")
        self.assert_preferences(loaded, preferences=expected)
        self.assertTrue(loaded["multi"]["enabled"])
        self.assertEqual(loaded["multi"]["initiator_identity"], "新灵脉子")
        self.assertEqual(loaded["multi"]["in_flight"]["participant_key"], "xiaohao|新灵脉子")

    def test_current_preferences_win_over_duplicate_retired_defaults(self):
        state = copy.deepcopy(self.saved)
        for account, old_name in (("main", "缘生子"), ("sub", "寻真子"), ("xiaohao", "缘生子")):
            state["queues"]["rotation"]["participants"][f"{account}|{old_name}"] = {
                "enabled": True, "target_username": "OldDefault", "attempts": 0, "remaining": 10,
            }
        self.write_json(self.root / "duel_state.json", state)
        loaded = self.new_worker().load_duel_state()
        self.assert_preferences(loaded)
        self.assertEqual(len(loaded["queues"]["rotation"]["participants"]), 13)

    def test_next_day_reset_after_restart_keeps_disabled_preferences(self):
        self.now += timedelta(days=1)
        self.assert_preferences(self.new_worker().load_duel_state(), daily_reset=True)


if __name__ == "__main__":
    unittest.main()
