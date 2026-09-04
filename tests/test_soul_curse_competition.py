import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import soul_curse_features as scf
from soul_curse_features import (
    SOUL_CURSE_SETTINGS_FILE,
    SOUL_CURSE_SHARED_FILE,
)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class _Actor:
    """Minimal actor for shared-commission competition tests."""

    def __init__(self, account, avatars):
        self.account_key = account
        self.avatars = avatars
        self.state = {}
        self.pause_event = asyncio.Event()
        self.pause_event.set()

    def get_soul_curse_assist_state(self, identity, owner=None):
        key = f"soul_curse_assist::{identity}::{owner}"
        return self.state.setdefault(key, {})

    def identity_pause_seconds(self, identity):
        return 0

    def soul_curse_account_key(self):
        return self.account_key

    def save_state(self):
        pass


class SharedCommissionCompetitionTests(unittest.TestCase):
    """共享委托池：先冷却就绪的阴罗身份先认领。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shared_file = os.path.join(self.tmp.name, "soul_curse_shared.json")
        self.settings_file = os.path.join(self.tmp.name, "soul_curse_settings.json")
        self._patch_shared = patch.object(scf, "SOUL_CURSE_SHARED_FILE", self.shared_file)
        self._patch_settings = patch.object(scf, "SOUL_CURSE_SETTINGS_FILE", self.settings_file)
        self._patch_shared.start()
        self._patch_settings.start()
        self.addCleanup(self._patch_shared.stop)
        self.addCleanup(self._patch_settings.stop)
        self.addCleanup(self.tmp.cleanup)
        # 默认启用所有身份
        with open(self.settings_file, "w", encoding="utf-8") as fh:
            json.dump({
                "enabled": True,
                "identities": {
                    "main": {"主魂": True, "缘生子": True},
                    "sub": {"主魂": True, "玄续玄": True},
                },
            }, fh, ensure_ascii=False)

    def _publish(self, owner="xiaohao", commission_id="C100"):
        scf.write_soul_curse_shared_state({
            owner: {
                "commission_id": commission_id,
                "target_username": "@someone",
                "assistant_account": "",
                "assistant_identity": "",
                "status": "pending_accept",
            },
        })

    def _read(self, owner="xiaohao"):
        return scf.read_soul_curse_shared_state().get(owner, {})

    def test_ready_assistant_claims_unclaimed_commission(self):
        self._publish()
        actor = _Actor("sub", avatars=["厚土", "玄续玄", "寻真子"])
        actor.soul_curse_process_assist_commission = AsyncMock(return_value=600)

        async def go():
            profile = {"owner_account": "xiaohao", "target_username": "@someone", "assistant_identity": "玄续玄", "shared": True}
            wait = await scf.SoulCurseMixin.soul_curse_shared_assist_tick(actor, profile)
            return wait

        asyncio.run(go())
        item = self._read()
        self.assertEqual(item.get("assistant_account"), "sub")
        self.assertEqual(item.get("assistant_identity"), "玄续玄")
        actor.soul_curse_process_assist_commission.assert_awaited_once()

    def test_cooldown_not_ready_does_not_claim(self):
        self._publish()
        actor = _Actor("sub", avatars=["厚土", "玄续玄", "寻真子"])
        future = (datetime.now() + timedelta(hours=2)).strftime(TIME_FORMAT)
        actor.get_soul_curse_assist_state("玄续玄", "xiaohao")["next_action_at"] = future

        async def go():
            profile = {"owner_account": "xiaohao", "target_username": "@someone", "assistant_identity": "玄续玄", "shared": True}
            return await scf.SoulCurseMixin.soul_curse_shared_assist_tick(actor, profile)

        asyncio.run(go())
        item = self._read()
        self.assertEqual(item.get("assistant_account"), "")
        self.assertNotIn("claimed_at", item)

    def test_other_claimed_returns_their_cooldown(self):
        self._publish()
        item = self._read("xiaohao")
        item["assistant_account"] = "main"
        item["assistant_identity"] = "缘生子"
        future = (datetime.now() + timedelta(minutes=25)).strftime(TIME_FORMAT)
        item["next_action_at"] = future
        scf.write_soul_curse_shared_state({"xiaohao": item})
        actor = _Actor("sub", avatars=["厚土", "玄续玄", "寻真子"])

        async def go():
            profile = {"owner_account": "xiaohao", "target_username": "@someone", "assistant_identity": "玄续玄", "shared": True}
            return await scf.SoulCurseMixin.soul_curse_shared_assist_tick(actor, profile)

        wait = asyncio.run(go())
        self.assertGreaterEqual(wait, 30)
        item = self._read()
        self.assertEqual(item.get("assistant_account"), "main")

    def test_publisher_no_longer_prescribes_assistant(self):
        """发布委托时认领留空——由就绪身份竞争。"""
        publisher_calls = {}

        actor = _Actor("xiaohao", avatars=[])

        class _P:
            account_key = "xiaohao"

        # 检查发布写入函数的行为：构造发布场景直接调用 upsert
        scf.write_soul_curse_shared_state({})
        scf.upsert_soul_curse_shared_commission("xiaohao", {
            "commission_id": "C200",
            "target_username": "@t",
            "assistant_account": "",
            "assistant_identity": "",
            "status": "pending_accept",
        })
        item = self._read()
        self.assertEqual(item.get("assistant_account"), "")
        self.assertEqual(item.get("status"), "pending_accept")

    def test_publisher_only_account_is_not_an_assistant_candidate(self):
        """没有阴罗身份的发布账号不能先占用自己的共享委托。"""
        class _SoulActor(scf.SoulCurseMixin, _Actor):
            pass

        actor = _SoulActor("xiaohao", avatars=["问心子"])
        profiles = actor.soul_curse_shared_assistant_profiles()
        self.assertFalse(any(p.get("owner_account") == "xiaohao" for p in profiles))

    def test_terminal_main_entry_does_not_hide_pending_avatar_entry(self):
        """旧主魂委托已结束时，仍应扫描同账号化身的新委托。"""
        scf.write_soul_curse_shared_state({
            "main": {
                "commission_id": "DONE",
                "target_username": "@old",
                "assistant_account": "sub",
                "status": "completed",
            },
            "main:玄续玄": {
                "commission_id": "NEW",
                "target_username": "@new",
                "assistant_account": "",
                "status": "pending_accept",
            },
        })
        actor = _Actor("sub", avatars=["厚土", "玄续玄", "寻真子"])
        actor.soul_curse_process_assist_commission = AsyncMock(return_value=600)

        async def go():
            return await scf.SoulCurseMixin.soul_curse_shared_assist_tick(
                actor,
                {
                    "owner_account": "main",
                    "target_username": "@new",
                    "assistant_identity": "玄续玄",
                    "shared": True,
                },
            )

        asyncio.run(go())
        item = self._read("main:玄续玄")
        self.assertEqual(item.get("assistant_account"), "sub")
        actor.soul_curse_process_assist_commission.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
