import os
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_server as ds


class AvatarSoulCursePanelTests(unittest.TestCase):
    """所有身份（主魂+化身+waaiging 主魂）的指令弹窗都有封魂咒链路行。"""

    def test_lingxiao_avatar_panel_has_soul_curse_row(self):
        for name in ("无咎子", "缘生子", "素缘子"):
            with patch(
                "dashboard_server.soul_curse_identity_enabled_for_dashboard",
                return_value=False,
            ):
                rows = ds.lingxiao_avatar_commands(name, {}, root_state={})
            labels = [str(r.get("label", "")) for r in rows]
            self.assertTrue(
                any("封魂咒链路" in lb and name in lb for lb in labels),
                f"{name}: no soul-curse row in {labels[:5]}...",
            )
            toggle_rows = [r for r in rows if r.get("dashboard_action") == "soul-curse-toggle"]
            self.assertEqual(len(toggle_rows), 1, f"{name}: toggle row count {len(toggle_rows)}")
            self.assertEqual(toggle_rows[0].get("soul_curse_identity"), name)

    def test_star_avatar_panel_has_soul_curse_row(self):
        for name in ("厚土", "寻真子", ds.DEFAULT_SUB_YINLUO_IDENTITY):
            with patch(
                "dashboard_server.soul_curse_identity_enabled_for_dashboard",
                return_value=False,
            ):
                rows = ds.star_avatar_commands(name, {}, root_state={})
            labels = [str(r.get("label", "")) for r in rows]
            self.assertTrue(
                any("封魂咒链路" in lb and name in lb for lb in labels),
                f"{name}: no soul-curse row",
            )

    def test_xiaohao_avatar_panel_has_soul_curse_row(self):
        for name in ("问心子", "素心子", "灵脉玄"):
            with patch(
                "dashboard_server.soul_curse_identity_enabled_for_dashboard",
                return_value=False,
            ):
                rows = ds.xiaohao_avatar_commands(name, {}, root_state={})
            labels = [str(r.get("label", "")) for r in rows]
            self.assertTrue(
                any("封魂咒链路" in lb and name in lb for lb in labels),
                f"{name}: no soul-curse row",
            )

    def test_waaiging_main_soul_panel_has_soul_curse_row(self):
        state = {"sect_join_confirmed": True}
        with patch(
            "dashboard_server.soul_curse_identity_enabled_for_dashboard",
            return_value=False,
        ):
            rows = ds.soul_curse_publisher_commands(state, account="waaiging", identity="主魂")
        labels = [str(r.get("label", "")) for r in rows]
        self.assertTrue(any("封魂咒链路" in lb for lb in labels), labels)

    def test_avatar_panel_enabled_shows_full_chain(self):
        with patch(
            "dashboard_server.soul_curse_identity_enabled_for_dashboard",
            return_value=True,
        ):
            rows = ds.soul_curse_publisher_commands({}, account="main", identity="无咎子")
        commands = [str(r.get("command", "")) for r in rows]
        self.assertTrue(any("探望南宫婉" in c for c in commands))
        self.assertTrue(any("推演封魂咒" in c for c in commands))

    def test_renamed_yinluo_panels_show_publisher_and_assistant_commands(self):
        for account, identity, legacy, panel in (
            ("main", "玄续子", "缘生子", ds.lingxiao_avatar_commands),
            ("sub", "岚衍子", ds.DEFAULT_SUB_YINLUO_IDENTITY, ds.star_avatar_commands),
        ):
            with self.subTest(account=account):
                root_state = {
                    "identity_sect_names": {identity: "阴罗宗"},
                    "avatar_dao_name_aliases": {legacy: identity},
                }
                avatar_state = {"sect_name": "阴罗宗", "soul_curse": {}, "soul_curse_assist": {}}
                with patch.object(ds, "soul_curse_identity_enabled_for_dashboard", return_value=True):
                    rows = panel(identity, avatar_state, root_state=root_state, account=account)
                commands = {row.get("command", "").split()[0] for row in rows}
                self.assertTrue({
                    ".探望南宫婉", ".推演封魂咒", ".护持神魂", ".发布解咒委托",
                    ".接取解咒委托", ".辨认咒纹", ".借幡镇魂", ".剥离咒源",
                }.issubset(commands), commands)
                self.assertNotIn(".婉影问安", commands)
                with patch.object(ds, "soul_curse_identity_enabled_for_dashboard", return_value=False):
                    disabled = panel(identity, avatar_state, root_state=root_state, account=account)
                switches = [row for row in disabled if row.get("dashboard_action") == "soul-curse-toggle"]
                self.assertEqual(len(switches), 1)
                self.assertEqual(switches[0]["soul_curse_identity"], identity)
                infer = next(row for row in disabled if row.get("command") == ".推演封魂咒")
                self.assertEqual(infer["status"], "链路未启用")
                self.assertFalse(infer["actionable"])

    def test_final_command_panels_keep_both_yinluo_chains_and_one_switch(self):
        for account, identity, legacy in (
            ("main", "玄续子", "缘生子"),
            ("sub", "岚衍子", ds.DEFAULT_SUB_YINLUO_IDENTITY),
        ):
            root = {
                "identity_sect_names": {"主魂": "散修", identity: "阴罗宗"},
                "avatar_dao_name_aliases": {legacy: identity},
                "avatars": {identity: {"sect_name": "阴罗宗"}},
            }
            for enabled in (True, False):
                with self.subTest(account=account, enabled=enabled), patch.object(
                    ds, "soul_curse_identity_enabled_for_dashboard", return_value=enabled
                ), patch.object(ds, "load_custom_commands", return_value={}), patch.object(
                    ds, "load_command_controls", return_value={}
                ), patch.object(ds, "load_account_state_raw", return_value=None):
                    panel = next(p for p in ds.build_command_panels(account, root) if p["identity"] == identity)
                rows = panel["commands"]
                commands = {row.get("command", "").split()[0] for row in rows}
                for command in (
                    ".探望南宫婉", ".推演封魂咒", ".护持神魂", ".发布解咒委托",
                    ".接取解咒委托", ".辨认咒纹", ".借幡镇魂", ".剥离咒源",
                ):
                    self.assertIn(command, commands, (account, enabled, command))
                    row = next(row for row in rows if row.get("command", "").split()[0] == command)
                    self.assertTrue(row["controllable"])
                    if not enabled:
                        self.assertEqual(row["status"], "链路未启用")
                        self.assertFalse(row["actionable"])
                        self.assertIsNone(row["next_seconds"])
                switches = [row for row in rows if row.get("dashboard_action") == "soul-curse-toggle"]
                self.assertEqual(len(switches), 1)
                self.assertEqual(switches[0]["soul_curse_enabled"], enabled)

    def test_dashboard_switch_accepts_renamed_main_yinluo_alias(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            path = fh.name
            json.dump(
                {"enabled": True, "identities": {"main": {"缘生子": True}}},
                fh,
                ensure_ascii=False,
            )
        try:
            with patch.object(ds, "SOUL_CURSE_SETTINGS_FILE", path):
                self.assertTrue(
                    ds.soul_curse_identity_enabled_for_dashboard(
                        "main",
                        "玄续子",
                        state={"avatar_dao_name_aliases": {"缘生子": "玄续子"}},
                    )
                )
                self.assertFalse(
                    ds.soul_curse_identity_enabled_for_dashboard(
                        "main", "无咎子", state={"sect_name": "天星宗"}
                    )
                )
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
