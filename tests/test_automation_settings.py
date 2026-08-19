import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import automation_settings as settings
import common_command_features


class AutomationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "automation_settings.json"
        self.sub_state_path = Path(self.tempdir.name) / "state_sub.json"
        self.file_patch = patch.object(settings, "AUTOMATION_SETTINGS_FILE", self.path)
        self.sub_state_patch = patch.object(settings, "SUB_STATE_FILE", self.sub_state_path)
        self.file_patch.start()
        self.sub_state_patch.start()

    def tearDown(self):
        self.sub_state_patch.stop()
        self.file_patch.stop()
        self.tempdir.cleanup()

    def test_sub_identity_state_is_parsed_once_per_file_version(self):
        self.sub_state_path.write_text(json.dumps({
            "avatar_dao_names_by_player_id": {"-1003885521329": "锋脉子"},
            "avatar_dao_name_aliases": {"竹和生": "锋脉子"},
        }, ensure_ascii=False), encoding="utf-8")
        settings._SUB_IDENTITY_STATE_CACHE["signature"] = None
        settings._SUB_IDENTITY_STATE_CACHE["state"] = {}

        real_load_state = settings.load_json_state
        with patch.object(settings, "load_json_state", wraps=real_load_state) as load_mock:
            value = settings.normalize_automation_settings({
                "world_boss": {"participants": ["sub|竹和生"]},
                "miniapp_fishing": {
                    "enabled": True,
                    "participants": ["sub|竹和生"],
                    "rod_owner": "sub|竹和生",
                },
                "miniapp_journey": {
                    "enabled": True,
                    "participants": ["sub|竹和生"],
                },
                "miniapp_tianji_trial": {
                    "enabled": True,
                    "participants": ["sub|竹和生"],
                },
                "miniapp_fate_cards": {
                    "enabled": True,
                    "participants": ["sub|竹和生"],
                },
            })

        self.assertEqual(load_mock.call_count, 1)
        self.assertEqual(value["miniapp_fate_cards"]["participants"], ["sub|锋脉子"])

    def test_missing_file_uses_four_main_souls_and_huzhen(self):
        value = settings.load_automation_settings()
        self.assertEqual(
            value["world_boss"]["participants"],
            [f"{account}|主魂" for account in settings.ACCOUNT_IDENTITIES],
        )
        self.assertEqual(value["mulan_support"]["mode"], "护阵")
        self.assertEqual(value["miniapp_beast_abyss"]["power_min"], 0)
        self.assertEqual(value["miniapp_beast_abyss"]["power_max"], 0)
        self.assertTrue(value["miniapp_fishing"]["enabled"])
        self.assertEqual(value["miniapp_fishing"]["participants"], ["main|主魂"])
        self.assertEqual(value["miniapp_fishing"]["rod"], "auto")
        self.assertEqual(value["miniapp_fishing"]["rod_owner"], "auto")
        self.assertEqual(value["miniapp_fishing"]["pond"], "qingxi")
        self.assertEqual(value["miniapp_fishing"]["bait"], "demon_blood")
        self.assertEqual(value["miniapp_fishing"]["chum"], "none")
        self.assertEqual(value["miniapp_fishing"]["start_time"], "")
        self.assertEqual(value["version"], 12)
        self.assertTrue(value["miniapp_journey"]["enabled"])
        self.assertEqual(
            value["miniapp_journey"]["participants"],
            ["main|主魂", "main|无咎子", "waaiging|主魂"],
        )
        self.assertTrue(value["miniapp_tianji_trial"]["enabled"])
        self.assertEqual(
            value["miniapp_tianji_trial"]["participants"],
            [
                f"{account}|{identity}"
                for account, identities in settings.automation_account_identities().items()
                for identity in identities
            ],
        )
        self.assertTrue(value["miniapp_fate_cards"]["enabled"])
        self.assertEqual(
            value["miniapp_fate_cards"]["participants"],
            [
                f"{account}|{identity}"
                for account, identities in settings.automation_account_identities().items()
                for identity in identities
            ],
        )
        self.assertEqual(value["tianxing"]["meditation_mode"], "deep")
        self.assertEqual(value["tianxing"]["meditation_switch_id"], "")
        self.assertFalse(value["tianxing"]["use_heqi_pill"])
        self.assertFalse(value["tianxing"]["tianji_grind_enabled"])
        self.assertEqual(value["tianxing"]["tianji_grind_participants"], ["main|主魂"])

    def test_save_accepts_one_identity_per_account_and_can_disable_account(self):
        value = settings.save_automation_settings(
            world_boss_participants=["main|无咎子", {"account": "sub", "identity": "寻真子"}],
            mulan_support_mode="破灯",
            updated_by="tester",
        )
        self.assertEqual(value["world_boss"]["participants"], ["main|无咎子", "sub|寻真子"])
        self.assertEqual(settings.world_boss_identities_for_account("main", value), ["无咎子"])
        self.assertEqual(settings.world_boss_identities_for_account("xiaohao", value), [])
        self.assertEqual(settings.mulan_support_command(value), ".支援慕兰 破灯")
        self.assertTrue(settings.miniapp_fishing_settings(value)["enabled"])
        self.assertEqual(settings.miniapp_beast_abyss_settings(value), {"power_min": 0, "power_max": 0})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["updated_by"], "tester")

    def test_save_updates_miniapp_fishing_choices(self):
        value = settings.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            miniapp_beast_abyss_power_min=1000,
            miniapp_beast_abyss_power_max=1300,
            miniapp_fishing_enabled=False,
            miniapp_fishing_pond="hantan",
            miniapp_fishing_bait="spirit_worm",
            miniapp_fishing_chum="grass",
            miniapp_fishing_participants=["main|无咎子", "sub|主魂", "sub|厚土", "sub|竹和生", "xiaohao|缘生子", "waaiging|主魂"],
            miniapp_fishing_rod="金雷竹钓竿",
            miniapp_fishing_rod_owner="xiaohao|缘生子",
            miniapp_fishing_start_time="06:30",
        )
        self.assertEqual(value["miniapp_beast_abyss"], {"power_min": 1000, "power_max": 1300})
        self.assertEqual(
            value["miniapp_fishing"],
            {
                "enabled": False,
                "participants": ["main|无咎子", "sub|主魂", "sub|厚土", "sub|竹和生", "xiaohao|缘生子", "waaiging|主魂"],
                "rod": "金雷竹钓竿",
                "rod_owner": "xiaohao|缘生子",
                "pond": "hantan",
                "bait": "spirit_worm",
                "chum": "grass",
                "start_time": "06:30",
            },
        )

    def test_legacy_sub_dao_name_is_migrated_without_changing_other_accounts(self):
        value = settings.normalize_automation_settings({
            "world_boss": {"participants": ["main|缘生子", "sub|缘生子"]},
            "miniapp_fishing": {
                "enabled": True,
                "participants": ["sub|缘生子", "xiaohao|缘生子"],
                "rod_owner": "sub|缘生子",
            },
            "miniapp_tianji_trial": {
                "enabled": True,
                "participants": ["sub|缘生子", "main|缘生子"],
            },
            "miniapp_fate_cards": {
                "enabled": True,
                "participants": ["sub|缘生子", "main|缘生子"],
            },
            "miniapp_journey": {
                "enabled": True,
                "participants": ["sub|缘生子", "main|无咎子"],
            },
        })

        self.assertEqual(
            value["world_boss"]["participants"],
            ["main|缘生子", "sub|竹和生"],
        )
        self.assertEqual(
            value["miniapp_fishing"]["participants"],
            ["sub|竹和生", "xiaohao|缘生子"],
        )
        self.assertEqual(value["miniapp_fishing"]["rod_owner"], "sub|竹和生")
        self.assertEqual(
            value["miniapp_tianji_trial"]["participants"],
            ["sub|竹和生", "main|缘生子"],
        )
        self.assertEqual(
            value["miniapp_fate_cards"]["participants"],
            ["sub|竹和生", "main|缘生子"],
        )
        self.assertEqual(
            value["miniapp_journey"]["participants"],
            ["sub|竹和生", "main|无咎子"],
        )

    def test_reborn_sub_dao_name_migrates_saved_participants_and_dashboard_options(self):
        self.sub_state_path.write_text(json.dumps({
            "avatar_dao_names_by_player_id": {"-1003885521329": "锋脉子"},
            "avatar_dao_name_aliases": {"缘生子": "锋脉子", "竹和生": "锋脉子"},
            "identity_sect_names": {"锋脉子": "阴罗宗"},
        }, ensure_ascii=False), encoding="utf-8")

        value = settings.normalize_automation_settings({
            "world_boss": {"participants": ["sub|竹和生"]},
            "miniapp_fishing": {
                "enabled": True,
                "participants": ["sub|竹和生"],
                "rod_owner": "sub|竹和生",
            },
            "miniapp_tianji_trial": {
                "enabled": True,
                "participants": ["sub|竹和生"],
            },
            "miniapp_fate_cards": {
                "enabled": True,
                "participants": ["sub|竹和生"],
            },
        })
        payload = settings.automation_dashboard_payload()
        sub_account = next(
            item for item in payload["miniapp_fishing"]["accounts"]
            if item["key"] == "sub"
        )

        self.assertEqual(value["world_boss"]["participants"], ["sub|锋脉子"])
        self.assertEqual(value["miniapp_fishing"]["participants"], ["sub|锋脉子"])
        self.assertEqual(value["miniapp_fishing"]["rod_owner"], "sub|锋脉子")
        self.assertEqual(value["miniapp_tianji_trial"]["participants"], ["sub|锋脉子"])
        self.assertEqual(value["miniapp_fate_cards"]["participants"], ["sub|锋脉子"])
        self.assertIn("锋脉子", [item["name"] for item in sub_account["identities"]])
        self.assertNotIn("竹和生", [item["name"] for item in sub_account["identities"]])

    def test_save_updates_tianxing_round_settings(self):
        value = settings.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            tianxing_meditation_mode="fate",
            tianxing_use_heqi_pill=True,
            tianxing_tianji_grind_enabled=True,
            tianxing_tianji_grind_target=12,
            tianxing_tianji_grind_participants=["main|无咎子", "waaiging|主魂"],
        )
        self.assertEqual(value["tianxing"]["meditation_mode"], "fate")
        self.assertTrue(value["tianxing"]["meditation_switch_id"])
        self.assertTrue(value["tianxing"]["use_heqi_pill"])
        self.assertEqual(value["tianxing"]["tianji_grind_target"], 12)
        self.assertEqual(
            value["tianxing"]["tianji_grind_participants"],
            ["main|无咎子", "waaiging|主魂"],
        )
        self.assertEqual(
            settings.tianxing_tianji_identities_for_account("main", value),
            ["无咎子"],
        )
        self.assertEqual(
            settings.tianxing_tianji_identities_for_account("waaiging", value),
            ["主魂"],
        )
        self.assertTrue(value["tianxing"]["tianji_round_id"])
        disabled = settings.set_tianxing_heqi_pill_enabled(False, updated_by="test")
        self.assertFalse(disabled["tianxing"]["use_heqi_pill"])
        self.assertEqual(disabled["tianxing"]["tianji_grind_target"], 12)

    def test_save_updates_tianji_trial_participants(self):
        value = settings.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            miniapp_tianji_trial_enabled=True,
            miniapp_tianji_trial_participants=["main|无咎子", "xiaohao|素心子"],
        )

        self.assertEqual(
            value["miniapp_tianji_trial"],
            {
                "enabled": True,
                "participants": ["main|无咎子", "xiaohao|素心子"],
            },
        )
        self.assertEqual(
            settings.miniapp_tianji_trial_identities_for_account("main", value),
            ["无咎子"],
        )

    def test_save_updates_fate_cards_participants(self):
        value = settings.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            miniapp_fate_cards_enabled=True,
            miniapp_fate_cards_participants=["main|无咎子", "xiaohao|素心子"],
        )

        self.assertEqual(
            value["miniapp_fate_cards"],
            {
                "enabled": True,
                "participants": ["main|无咎子", "xiaohao|素心子"],
            },
        )
        self.assertEqual(
            settings.miniapp_fate_cards_identities_for_account("main", value),
            ["无咎子"],
        )

    def test_save_updates_journey_participants_for_any_account(self):
        value = settings.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            miniapp_journey_enabled=True,
            miniapp_journey_participants=["sub|厚土", "xiaohao|素心子"],
        )

        self.assertEqual(
            value["miniapp_journey"],
            {
                "enabled": True,
                "participants": ["sub|厚土", "xiaohao|素心子"],
            },
        )
        self.assertEqual(
            settings.miniapp_journey_identities_for_account("sub", value),
            ["厚土"],
        )
        self.assertEqual(
            settings.miniapp_journey_identities_for_account("xiaohao", value),
            ["素心子"],
        )

    def test_save_rejects_invalid_tianxing_grind_target(self):
        with self.assertRaisesRegex(ValueError, "Tianxing Tianji grind target required"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                tianxing_tianji_grind_enabled=True,
                tianxing_tianji_grind_target=0,
            )
        with self.assertRaisesRegex(ValueError, "invalid Tianxing meditation mode"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                tianxing_meditation_mode="unknown",
            )
        with self.assertRaisesRegex(ValueError, "Tianxing Tianji grind participants required"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                tianxing_tianji_grind_enabled=True,
                tianxing_tianji_grind_target=1,
                tianxing_tianji_grind_participants=[],
            )
        with self.assertRaisesRegex(ValueError, "invalid Tianxing Tianji grind participant"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                tianxing_tianji_grind_participants=["sub|主魂"],
            )

    def test_common_command_reads_dashboard_mode_without_restart(self):
        settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="奇袭"
        )
        self.assertEqual(common_command_features.current_mulan_support_command(), ".支援慕兰 奇袭")
        settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="护阵"
        )
        self.assertEqual(common_command_features.current_mulan_support_command(), ".支援慕兰 护阵")

    def test_save_rejects_invalid_mode_and_duplicate_account(self):
        with self.assertRaisesRegex(ValueError, "invalid Mulan support mode"):
            settings.save_automation_settings(
                world_boss_participants=[], mulan_support_mode="未知"
            )
        with self.assertRaisesRegex(ValueError, "multiple world boss identities per account"):
            settings.save_automation_settings(
                world_boss_participants=["main|主魂", "main|缘生子"],
                mulan_support_mode="护阵",
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App beast abyss power range"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_beast_abyss_power_min=-1,
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App beast abyss power range"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_beast_abyss_power_min="abc",
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing participant"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_participants=["ghost|主魂"],
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing rod owner"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_rod_owner="sub|不存在",
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing rod"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_rod="玄铁钓竿",
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing start time"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_start_time="25:00",
            )
        with self.assertRaisesRegex(ValueError, "Mini App journey participants required"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_journey_enabled=True,
                miniapp_journey_participants=[],
            )

    def test_dashboard_payload_exposes_all_accounts_and_modes(self):
        settings.save_automation_settings(
            world_boss_participants=["waaiging|主魂"],
            mulan_support_mode="斥候",
            miniapp_beast_abyss_power_min=900,
            miniapp_beast_abyss_power_max=1300,
        )
        payload = settings.automation_dashboard_payload()
        self.assertEqual(payload["mulan_support"]["command"], ".支援慕兰 斥候")
        self.assertEqual(payload["mulan_support"]["modes"], list(settings.MULAN_SUPPORT_MODES))
        self.assertEqual(len(payload["world_boss"]["accounts"]), len(settings.ACCOUNT_IDENTITIES))
        waaiging = next(item for item in payload["world_boss"]["accounts"] if item["key"] == "waaiging")
        self.assertTrue(waaiging["identities"][0]["selected"])
        self.assertEqual(payload["miniapp_beast_abyss"], {"power_min": 900, "power_max": 1300})
        self.assertEqual(payload["miniapp_fishing"]["participants"], ["main|主魂"])
        self.assertEqual(payload["miniapp_fishing"]["rod"], "auto")
        self.assertEqual(payload["miniapp_fishing"]["rod_owner"], "auto")
        self.assertEqual(payload["miniapp_fishing"]["start_time"], "")
        self.assertEqual(len(payload["miniapp_fishing"]["accounts"]), 4)
        sub_fishing = next(
            item for item in payload["miniapp_fishing"]["accounts"] if item["key"] == "sub"
        )
        sub_names = [item["name"] for item in sub_fishing["identities"]]
        self.assertIn("竹和生", sub_names)
        self.assertNotIn("缘生子", sub_names)
        self.assertTrue(payload["miniapp_tianji_trial"]["enabled"])
        self.assertEqual(len(payload["miniapp_tianji_trial"]["accounts"]), 4)
        self.assertTrue(payload["miniapp_fate_cards"]["enabled"])
        self.assertEqual(len(payload["miniapp_fate_cards"]["accounts"]), 4)
        self.assertTrue(payload["miniapp_journey"]["enabled"])
        self.assertEqual(
            payload["miniapp_journey"]["participants"],
            ["main|主魂", "main|无咎子", "waaiging|主魂"],
        )
        self.assertEqual(len(payload["miniapp_journey"]["accounts"]), 4)
        self.assertEqual(len(payload["miniapp_fishing"]["ponds"]), 3)
        self.assertEqual(len(payload["miniapp_fishing"]["baits"]), 5)
        self.assertEqual(len(payload["tianxing"]["tianji_accounts"]), 2)
        self.assertEqual(
            [
                item["key"]
                for account in payload["tianxing"]["tianji_accounts"]
                for item in account["identities"]
            ],
            ["main|主魂", "main|无咎子", "waaiging|主魂"],
        )
        self.assertEqual(
            [item["key"] for item in payload["miniapp_fishing"]["rods"]],
            ["auto", "青竹钓竿", "银竹钓竿", "金竹钓竿", "金雷竹钓竿"],
        )


if __name__ == "__main__":
    unittest.main()
