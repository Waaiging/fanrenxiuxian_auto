import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from common_command_features import CommonCommandMixin
import soul_curse_features as scf
from tests.test_soul_curse_features import DummySoulCurseActor


class Daytime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz).replace(hour=12, minute=0, second=0)


class YinluoPublisherActor(DummySoulCurseActor):
    resolve_avatar_identity = CommonCommandMixin.resolve_avatar_identity
    soul_curse_identity_enabled = scf.SoulCurseMixin.soul_curse_identity_enabled

    def __init__(self, account):
        super().__init__(account)
        identity = "玄续子" if account == "main" else "岚衍子"
        legacy = "缘生子" if account == "main" else scf.DEFAULT_SUB_YINLUO_IDENTITY
        aliases = {legacy: identity}
        if account == "sub":
            aliases.update({scf.SUB_YINLUO_IDENTITY: identity, "玄续玄": identity})
        self.avatars = [identity]
        self.state = {
            "avatars": {identity: {"soul_curse_assist": {"status": "existing_assist_progress"}}},
            "identity_sect_names": {"主魂": "散修", identity: "阴罗宗"},
            "avatar_dao_name_aliases": aliases,
            "soul_curse": {"commission_id": "UNCHANGED_MAIN_SOUL"},
        }


class YinluoPublisherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Path(temp.name) / "settings.json"
        self.settings.write_text(json.dumps({
            "enabled": True,
            "identities": {"main": {"缘生子": True}, "sub": {scf.DEFAULT_SUB_YINLUO_IDENTITY: True}},
        }))
        for name, value in (
            ("SOUL_CURSE_SETTINGS_FILE", str(self.settings)),
            ("SOUL_CURSE_SHARED_FILE", str(Path(temp.name) / "commissions.json")),
            ("datetime", Daytime),
        ):
            patcher = patch.object(scf, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_both_renamed_yinluo_avatars_run_full_chain_in_their_own_state(self):
        for account, target in (("main", "@kulipabp"), ("sub", "@lvdoumiao")):
            with self.subTest(account=account):
                actor = YinluoPublisherActor(account)
                identity = actor.avatars[0]
                actor.identity_responses = {
                    (identity, scf.SOUL_CURSE_VISIT_COMMAND): ["【探望南宫婉】 婉心 +6，魂封 -3。"],
                    (identity, scf.SOUL_CURSE_INFER_COMMAND): ["【推演封魂咒】 咒源 +18。"],
                    (identity, scf.SOUL_CURSE_PROTECT_COMMAND): ["【护持神魂】 魂封 -10，月魄 +1。"],
                    (identity, scf.SOUL_CURSE_PUBLISH_COMMAND): ["【解咒委托已发布】 委托 ID：19"],
                }
                with patch.object(scf.asyncio, "sleep", new=AsyncMock()):
                    asyncio.run(actor.soul_curse_avatar_publishers_tick([]))
                    asyncio.run(actor.soul_curse_avatar_publishers_tick([]))
                self.assertEqual(actor.identity_sent, [
                    (identity, command) for command in (
                        scf.SOUL_CURSE_VISIT_COMMAND, scf.SOUL_CURSE_INFER_COMMAND,
                        scf.SOUL_CURSE_PROTECT_COMMAND, scf.SOUL_CURSE_PUBLISH_COMMAND,
                    )
                ])
                self.assertEqual(actor.main_sent, [])
                self.assertEqual(actor.state["soul_curse"], {"commission_id": "UNCHANGED_MAIN_SOUL"})
                self.assertEqual(actor.get_avatar_state(identity)["soul_curse_assist"], {"status": "existing_assist_progress"})
                state = actor.get_soul_curse_state(identity)
                self.assertEqual(state["commission_target"], target)
                self.assertTrue(scf.is_future(state["next_chain_time"]))
                item = scf.read_soul_curse_shared_state()[f"{account}:{identity}"]
                self.assertEqual(item["target_username"], target)
                self.assertEqual(item["assistant_account"], "")
                sent = list(actor.identity_sent)
                asyncio.run(actor.soul_curse_avatar_publishers_tick([]))
                self.assertEqual(actor.identity_sent, sent, "A completed chain must retain its cooldown")

    def test_current_name_switch_overrides_legacy_enabled_and_stops_both_roles(self):
        for account in ("main", "sub"):
            actor = YinluoPublisherActor(account)
            identity = actor.avatars[0]
            settings = json.loads(self.settings.read_text())
            settings["identities"][account][identity] = False
            self.settings.write_text(json.dumps(settings))
            asyncio.run(actor.soul_curse_avatar_publishers_tick([]))
            self.assertEqual(actor.identity_sent, [])
            self.assertFalse(actor.soul_curse_assistant_enabled(identity))

    def test_later_rename_keeps_the_publisher_profile_and_only_explicit_yinluo_slots_qualify(self):
        actor = YinluoPublisherActor("main")
        actor.state["avatar_dao_name_aliases"]["玄续子"] = "新道号"
        actor.state["avatars"]["新道号"] = actor.state["avatars"].pop("玄续子")
        actor.state["identity_sect_names"].update({"新道号": "阴罗宗", "无咎子": "阴罗宗"})
        actor.avatars = ["新道号", "无咎子"]
        profiles = actor.soul_curse_avatar_publisher_profiles()
        self.assertEqual(len([p for p in profiles if p["identity"] == "新道号"]), 1)
        self.assertFalse(any(p["identity"] == "无咎子" for p in profiles))
        self.assertTrue(actor.soul_curse_identity_enabled(identity="新道号"))

    def test_own_legacy_named_commission_is_left_for_another_account(self):
        for account, other in (("main", "sub"), ("sub", "main")):
            with self.subTest(account=account):
                actor = YinluoPublisherActor(account)
                identity = actor.avatars[0]
                legacy = "缘生子" if account == "main" else scf.DEFAULT_SUB_YINLUO_IDENTITY
                key = f"{account}:{legacy}"
                scf.write_soul_curse_shared_state({key: {
                    "commission_id": "19", "status": "pending_accept", "assistant_account": "",
                    "target_username": "@kulipabp" if account == "main" else "@lvdoumiao",
                }})
                actor.soul_curse_process_assist_commission = AsyncMock(return_value=600)
                asyncio.run(actor.soul_curse_shared_assist_tick({"owner_account": account, "assistant_identity": identity}))
                actor.soul_curse_process_assist_commission.assert_not_awaited()
                self.assertEqual(scf.read_soul_curse_shared_state()[key]["assistant_account"], "")
                helper = YinluoPublisherActor(other)
                helper.soul_curse_process_assist_commission = AsyncMock(return_value=600)
                asyncio.run(helper.soul_curse_shared_assist_tick({"owner_account": account, "assistant_identity": helper.avatars[0]}))
                helper.soul_curse_process_assist_commission.assert_awaited_once()
                self.assertEqual(scf.read_soul_curse_shared_state()[key]["assistant_account"], other)

    def test_own_commission_does_not_hide_another_avatar_commission(self):
        actor = YinluoPublisherActor("main")
        scf.write_soul_curse_shared_state({
            "main:玄续子": {"commission_id": "SELF", "status": "pending_accept", "assistant_account": ""},
            "main:素缘子": {"commission_id": "OTHER", "status": "pending_accept", "assistant_account": ""},
        })
        actor.soul_curse_process_assist_commission = AsyncMock(return_value=600)
        asyncio.run(actor.soul_curse_shared_assist_tick({"owner_account": "main", "assistant_identity": "玄续子"}))
        self.assertEqual(actor.soul_curse_process_assist_commission.await_args.args[0]["commission_id"], "OTHER")
        self.assertEqual(scf.read_soul_curse_shared_state()["main:玄续子"]["assistant_account"], "")


if __name__ == "__main__":
    unittest.main()
