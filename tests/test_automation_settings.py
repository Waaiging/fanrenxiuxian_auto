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
        self.file_patch = patch.object(settings, "AUTOMATION_SETTINGS_FILE", self.path)
        self.file_patch.start()

    def tearDown(self):
        self.file_patch.stop()
        self.tempdir.cleanup()

    def test_missing_file_uses_four_main_souls_and_huzhen(self):
        value = settings.load_automation_settings()
        self.assertEqual(
            value["world_boss"]["participants"],
            [f"{account}|主魂" for account in settings.ACCOUNT_IDENTITIES],
        )
        self.assertEqual(value["mulan_support"]["mode"], "护阵")
        self.assertTrue(value["miniapp_fishing"]["enabled"])
        self.assertEqual(value["miniapp_fishing"]["participants"], ["main|主魂"])
        self.assertEqual(value["miniapp_fishing"]["rod_owner"], "auto")
        self.assertEqual(value["miniapp_fishing"]["pond"], "qingxi")
        self.assertEqual(value["miniapp_fishing"]["bait"], "demon_blood")
        self.assertEqual(value["miniapp_fishing"]["chum"], "none")
        self.assertEqual(value["miniapp_fishing"]["start_time"], "")

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
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["updated_by"], "tester")

    def test_save_updates_miniapp_fishing_choices(self):
        value = settings.save_automation_settings(
            world_boss_participants=[],
            mulan_support_mode="护阵",
            miniapp_fishing_enabled=False,
            miniapp_fishing_pond="hantan",
            miniapp_fishing_bait="spirit_worm",
            miniapp_fishing_chum="grass",
            miniapp_fishing_participants=["main|无咎子", "sub|主魂", "sub|厚土"],
            miniapp_fishing_rod_owner="sub|主魂",
            miniapp_fishing_start_time="06:30",
        )
        self.assertEqual(
            value["miniapp_fishing"],
            {
                "enabled": False,
                "participants": ["main|无咎子", "sub|主魂", "sub|厚土"],
                "rod_owner": "sub|主魂",
                "pond": "hantan",
                "bait": "spirit_worm",
                "chum": "grass",
                "start_time": "06:30",
            },
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
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing participant"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_participants=["xiaohao|主魂"],
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing rod owner"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_rod_owner="sub|不存在",
            )
        with self.assertRaisesRegex(ValueError, "invalid Mini App fishing start time"):
            settings.save_automation_settings(
                world_boss_participants=[],
                mulan_support_mode="护阵",
                miniapp_fishing_start_time="25:00",
            )

    def test_dashboard_payload_exposes_all_accounts_and_modes(self):
        settings.save_automation_settings(
            world_boss_participants=["waaiging|主魂"],
            mulan_support_mode="斥候",
        )
        payload = settings.automation_dashboard_payload()
        self.assertEqual(payload["mulan_support"]["command"], ".支援慕兰 斥候")
        self.assertEqual(payload["mulan_support"]["modes"], list(settings.MULAN_SUPPORT_MODES))
        self.assertEqual(len(payload["world_boss"]["accounts"]), len(settings.ACCOUNT_IDENTITIES))
        waaiging = next(item for item in payload["world_boss"]["accounts"] if item["key"] == "waaiging")
        self.assertTrue(waaiging["identities"][0]["selected"])
        self.assertEqual(payload["miniapp_fishing"]["participants"], ["main|主魂"])
        self.assertEqual(payload["miniapp_fishing"]["rod_owner"], "auto")
        self.assertEqual(payload["miniapp_fishing"]["start_time"], "")
        self.assertEqual(len(payload["miniapp_fishing"]["accounts"]), 2)
        self.assertEqual(len(payload["miniapp_fishing"]["ponds"]), 3)
        self.assertEqual(len(payload["miniapp_fishing"]["baits"]), 5)


if __name__ == "__main__":
    unittest.main()
