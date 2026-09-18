"""Paused commission publication must not stop enabled personal cooldowns."""
import asyncio
import copy
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import soul_curse_features as scf


INFER = scf.SOUL_CURSE_INFER_COMMAND
PROTECT = scf.SOUL_CURSE_PROTECT_COMMAND
PUBLISH = scf.SOUL_CURSE_PUBLISH_COMMAND
AVATAR = "素缘子"
SUCCESS = {INFER: "【推演封魂咒】 咒源 +18。", PROTECT: "【护持神魂】 魂封 -10，月魄 +1。"}
LIFECYCLE = (
    "chain_stage", "commission_id", "commission_status", "commission_target",
    "last_chain_time", "next_chain_time", "next_action_at",
)


class Publisher(scf.SoulCurseMixin):
    def __init__(self, account="main"):
        self.account_key = account
        self.avatars = [AVATAR]
        self.state = {"avatars": {AVATAR: {}}, "identity_sect_names": {"主魂": "散修", AVATAR: "散修"}}
        self.paused = {(identity, command) for identity in ("主魂", AVATAR) for command in (
            PUBLISH, scf.SOUL_CURSE_VISIT_COMMAND, scf.SOUL_CURSE_WANYING_GREETING_COMMAND,
            scf.SOUL_CURSE_MOON_MEDITATION_COMMAND,
        )}
        self.paused_identities = set()
        self.responses = {}
        self.sent = []
        self.saved_state = None
        self.on_lock = None

    def save_state(self):
        self.saved_state = copy.deepcopy(self.state)

    def get_avatar_state(self, identity):
        return self.state["avatars"].setdefault(identity, {})

    def resolve_avatar_identity(self, identity):
        return self.state.get("avatar_dao_name_aliases", {}).get(identity, identity)

    def identity_pause_seconds(self, identity):
        return 300 if identity in self.paused_identities else 0

    def dashboard_command_paused(self, command, identity="主魂"):
        return (identity, command) in self.paused

    @asynccontextmanager
    async def common_atomic_task(self, label):
        if self.on_lock:
            callback, self.on_lock = self.on_lock, None
            callback()
        yield

    async def send_and_wait_feedback(self, command, **kwargs):
        return await self.send_and_wait_feedback_identity("主魂", command, **kwargs)

    async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
        self.sent.append((identity, command))
        result = self.responses.get((identity, command), SUCCESS.get(command, ""))
        if isinstance(result, BaseException):
            raise result
        return result


class PausedPublishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = datetime(2026, 9, 18, 10, 0, 0)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings_path = Path(temporary.name) / "settings.json"
        self.shared_path = Path(temporary.name) / "commissions.json"
        self.settings = {"enabled": True, "identities": {
            account: {"主魂": True, AVATAR: True} for account in scf.SOUL_CURSE_PUBLISHERS
        }}
        self.save_settings()
        self.shared_path.write_text("{}", encoding="utf-8")
        for name, value in (
            ("SOUL_CURSE_SETTINGS_FILE", str(self.settings_path)),
            ("SOUL_CURSE_SHARED_FILE", str(self.shared_path)),
            # Transport retry/identity confirmation has its own regression suite.
            ("SOUL_CURSE_IDENTITY_RETRY_ATTEMPTS", 0),
        ):
            self.enterContext(patch.object(scf, name, value))
        self.enterContext(patch.object(scf, "now_str", side_effect=lambda: self.at()))
        self.enterContext(patch.object(scf, "is_future", side_effect=lambda value: bool(value) and value > self.at()))
        self.enterContext(patch.object(scf, "seconds_until", side_effect=lambda value:
            max(0, int((datetime.fromisoformat(value) - self.clock).total_seconds())) if value else 0))
        self.enterContext(patch.object(scf.asyncio, "sleep", new=AsyncMock()))

    def at(self, seconds=0):
        return (self.clock + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")

    def save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, ensure_ascii=False), encoding="utf-8")

    def stuck(self, identity="主魂", account="main"):
        actor = Publisher(account)
        state = actor.get_soul_curse_state(identity)
        state.update({
            "chain_stage": "publish", "commission_id": "101", "commission_status": "completed",
            "commission_target": "@Example", "last_chain_time": self.at(-3600),
            "next_chain_time": self.at(7200), "next_action_at": self.at(10800),
            "last_infer_time": self.at(-9 * 3600), "next_infer_time": self.at(-3600),
            "last_protect_time": self.at(-9 * 3600), "next_protect_time": self.at(-3600),
        })
        return actor

    async def tick(self, actor, identity):
        profile = dict(scf.SOUL_CURSE_PUBLISHERS[actor.account_key])
        if identity == "主魂":
            return await actor.soul_curse_run_publisher_chain(profile)
        profile["owner_key"] = actor.account_key + ":" + identity
        return await actor.soul_curse_run_avatar_publisher_chain(profile, identity)

    def restart(self, actor):
        restarted = Publisher(actor.account_key)
        restarted.state = json.loads(json.dumps(actor.saved_state))
        restarted.paused = set(actor.paused)
        restarted.responses = dict(actor.responses)
        return restarted

    async def check_resumes(self, identity):
        actor = self.stuck(identity)
        state = actor.get_soul_curse_state(identity)
        lifecycle = {key: state[key] for key in LIFECYCLE}
        await self.tick(actor, identity)
        self.assertEqual(actor.sent, [(identity, INFER)])
        await self.tick(actor, identity)
        self.assertEqual(actor.sent, [(identity, INFER), (identity, PROTECT)])
        self.assertEqual({key: state[key] for key in LIFECYCLE}, lifecycle)
        self.assertEqual(state["next_infer_time"], self.at(8 * 3600))
        self.assertEqual(state["next_protect_time"], self.at(8 * 3600))
        self.assertIn((identity, PUBLISH), actor.paused)
        restarted = self.restart(actor)
        await self.tick(restarted, identity)
        self.assertEqual(restarted.sent, [])
        self.clock += timedelta(hours=8)
        await self.tick(restarted, identity)
        await self.tick(restarted, identity)
        self.assertEqual(restarted.sent, [(identity, INFER), (identity, PROTECT)])

    async def test_stuck_main_resumes_personal_cycles_without_publishing(self):
        await self.check_resumes("主魂")

    async def test_stuck_avatar_resumes_personal_cycles_without_publishing(self):
        await self.check_resumes(AVATAR)

    async def test_each_command_pause_or_cooldown_leaves_other_command_available(self):
        for identity in ("主魂", AVATAR):
            for action, command, other in (("infer", INFER, PROTECT), ("protect", PROTECT, INFER)):
                for mode in ("paused", "cooldown"):
                    with self.subTest(identity=identity, action=action, mode=mode):
                        actor = self.stuck(identity)
                        if mode == "paused":
                            actor.paused.add((identity, command))
                        else:
                            actor.get_soul_curse_state(identity)[f"next_{action}_time"] = self.at(3600)
                        await self.tick(actor, identity)
                        await self.tick(actor, identity)
                        self.assertEqual(actor.sent, [(identity, other)])

    async def test_both_commands_paused_send_nothing(self):
        for identity in ("主魂", AVATAR):
            actor = self.stuck(identity)
            actor.paused.update(((identity, INFER), (identity, PROTECT)))
            await self.tick(actor, identity)
            self.assertEqual(actor.sent, [])

    async def test_reply_failures_persist_independent_retry_and_allow_other_command(self):
        for identity in ("主魂", AVATAR):
            for action, command, other in (("infer", INFER, PROTECT), ("protect", PROTECT, INFER)):
                cooldown = ("封魂咒变化极慢，请在 2小时3分钟 后再试。" if command == INFER
                            else "神魂护持不可过密，请在 2小时3分钟 后再试。")
                for response, wait in (("", 600), ("条件不足", 1800), ("未知回执", 1800), (cooldown, 7380)):
                    with self.subTest(identity=identity, action=action, response=response):
                        actor = self.stuck(identity)
                        actor.paused.add((identity, other))
                        actor.responses[identity, command] = response
                        before = copy.deepcopy(actor.get_soul_curse_state(identity))
                        await self.tick(actor, identity)
                        state = actor.get_soul_curse_state(identity)
                        self.assertEqual(state[f"next_{action}_time"], self.at(wait))
                        self.assertEqual(state[f"last_{action}_time"], before[f"last_{action}_time"])
                        self.assertEqual({key: state[key] for key in LIFECYCLE}, {key: before[key] for key in LIFECYCLE})
                        restarted = self.restart(actor)
                        restarted.paused.remove((identity, other))
                        await self.tick(restarted, identity)
                        await self.tick(restarted, identity)
                        self.assertEqual(restarted.sent, [(identity, other)])
                        self.clock += timedelta(seconds=wait)
                        await self.tick(restarted, identity)
                        self.assertEqual(restarted.sent[-1], (identity, command))

    async def test_send_exception_retries_only_failed_command(self):
        for identity in ("主魂", AVATAR):
            actor = self.stuck(identity)
            actor.responses[identity, INFER] = RuntimeError("test transport unavailable")
            with self.assertLogs(level="ERROR"):
                await self.tick(actor, identity)
            self.assertEqual(actor.get_soul_curse_state(identity)["next_infer_time"], self.at(600))
            restarted = self.restart(actor)
            await self.tick(restarted, identity)
            self.assertEqual(restarted.sent, [(identity, PROTECT)])

    async def test_cancellation_is_not_swallowed(self):
        actor = self.stuck()
        actor.responses["主魂", INFER] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.tick(actor, "主魂")

    async def test_global_and_identity_settings_stop_personal_actions(self):
        for identity in ("主魂", AVATAR):
            for scope in ("global", "identity"):
                with self.subTest(identity=identity, scope=scope):
                    actor = self.stuck(identity)
                    if scope == "global":
                        self.settings["enabled"] = False
                    else:
                        self.settings["identities"]["main"][identity] = False
                    self.save_settings()
                    await self.tick(actor, identity)
                    self.assertEqual(actor.sent, [])
                    self.settings["enabled"] = True
                    self.settings["identities"]["main"][identity] = True
                    self.save_settings()

    async def test_state_and_controls_are_rechecked_after_waiting_for_lock(self):
        for identity in ("主魂", AVATAR):
            for change in ("global", "identity", "pause_identity", "pause_infer", "infer_cooldown", "enable_publish"):
                with self.subTest(identity=identity, change=change):
                    actor = self.stuck(identity)

                    def update():
                        if change == "global":
                            self.settings["enabled"] = False
                        elif change == "identity":
                            self.settings["identities"]["main"][identity] = False
                        elif change == "pause_identity":
                            actor.paused_identities.add(identity)
                        elif change == "pause_infer":
                            actor.paused.add((identity, INFER))
                        elif change == "infer_cooldown":
                            actor.get_soul_curse_state(identity)["next_infer_time"] = self.at(3600)
                        else:
                            actor.paused.remove((identity, PUBLISH))
                        self.save_settings()

                    actor.on_lock = update
                    await self.tick(actor, identity)
                    expected = [(identity, PROTECT)] if change in {"pause_infer", "infer_cooldown"} else []
                    self.assertEqual(actor.sent, expected)
                    self.settings["enabled"] = True
                    self.settings["identities"]["main"][identity] = True
                    self.save_settings()

    async def test_pending_shared_commission_allows_personal_commands_without_changing_pool(self):
        actor = self.stuck()
        actor.avatars = []
        state = actor.get_soul_curse_state()
        state["commission_status"] = "pending_accept"
        shared = {"main": {"commission_id": "101", "status": "pending_accept", "updated_at": self.at(-3600)}}
        self.shared_path.write_text(json.dumps(shared), encoding="utf-8")
        before = self.shared_path.read_bytes()
        await actor.soul_curse_tick()
        await actor.soul_curse_tick()
        self.assertEqual(actor.sent, [("主魂", INFER), ("主魂", PROTECT)])
        self.assertEqual(state["commission_id"], "101")
        self.assertEqual(state["commission_status"], "pending_accept")
        self.assertEqual(self.shared_path.read_bytes(), before)

    async def test_completion_still_sets_chain_deadline_while_personal_actions_run(self):
        actor = self.stuck()
        actor.avatars = []
        state = actor.get_soul_curse_state()
        state["commission_status"] = "pending_accept"
        self.shared_path.write_text(json.dumps({"main": {
            "commission_id": "101", "status": "completed", "updated_at": self.at(),
        }}), encoding="utf-8")
        await actor.soul_curse_tick()
        self.assertEqual(state["commission_status"], "completed")
        self.assertEqual(state["chain_stage"], "")
        self.assertEqual(state["next_chain_time"], self.at(8 * 3600))
        self.assertEqual(state["next_action_at"], self.at(8 * 3600))
        self.assertEqual(actor.sent, [("主魂", INFER)])

    async def test_reenabled_publish_resumes_existing_stage_without_repeating_actions(self):
        for identity in ("主魂", AVATAR):
            actor = self.stuck(identity)
            actor.get_soul_curse_state(identity)["next_action_at"] = ""
            await self.tick(actor, identity)
            await self.tick(actor, identity)
            actor.paused.remove((identity, PUBLISH))
            actor.responses[identity, PUBLISH] = "【解咒委托已发布】 委托 ID：102 报酬：1 灵石"
            await self.tick(actor, identity)
            self.assertEqual(actor.sent, [(identity, INFER), (identity, PROTECT), (identity, PUBLISH)])
            self.assertEqual(actor.get_soul_curse_state(identity)["commission_id"], "102")

    async def test_enabled_full_chain_keeps_original_order_and_chain_cooldown(self):
        actor = Publisher("waaiging")
        actor.paused.remove(("主魂", PUBLISH))
        actor.responses["主魂", PUBLISH] = "【解咒委托已发布】 委托 ID：103 报酬：1 灵石"
        await self.tick(actor, "主魂")
        self.assertEqual(actor.sent, [("主魂", INFER), ("主魂", PROTECT), ("主魂", PUBLISH)])
        actor.soul_curse_mark_publisher_commission_completed("waaiging", "103", self.at())
        await self.tick(actor, "主魂")
        self.assertEqual(len(actor.sent), 3)

    async def test_alias_uses_current_identity_settings_and_state(self):
        actor = self.stuck(AVATAR)
        actor.state["avatar_dao_name_aliases"] = {"旧道号": AVATAR}
        self.settings["identities"]["main"] = {"旧道号": True}
        self.save_settings()
        await self.tick(actor, "旧道号")
        self.assertEqual(actor.sent, [(AVATAR, INFER)])
        self.assertNotIn("旧道号", actor.state["avatars"])
        self.assertNotIn("soul_curse", actor.state)

    async def test_accounts_and_main_avatar_cooldowns_are_isolated(self):
        main = self.stuck()
        other = self.stuck(account="sub")
        await self.tick(main, "主魂")
        await self.tick(main, AVATAR)
        await self.tick(other, "主魂")
        self.assertEqual(main.sent, [("主魂", INFER), (AVATAR, INFER)])
        self.assertEqual(other.sent, [("主魂", INFER)])

    async def test_manual_replies_update_identity_cooldowns_without_advancing_paused_chain(self):
        for identity in ("主魂", AVATAR):
            actor = self.stuck(identity)
            state = actor.get_soul_curse_state(identity)
            before = {key: state[key] for key in LIFECYCLE}
            for command in (INFER, PROTECT):
                self.assertTrue(actor.record_soul_curse_manual_response(command, SUCCESS[command], identity))
            await self.tick(actor, identity)
            self.assertEqual(actor.sent, [])
            self.assertEqual(state["next_infer_time"], self.at(8 * 3600))
            self.assertEqual(state["next_protect_time"], self.at(8 * 3600))
            self.assertEqual({key: state[key] for key in LIFECYCLE}, before)

    async def test_manual_avatar_cooldown_uses_alias_without_enabling_publication(self):
        actor = self.stuck(AVATAR)
        actor.state["avatar_dao_name_aliases"] = {"旧道号": AVATAR}
        self.assertTrue(actor.record_soul_curse_manual_response(
            INFER, "封魂咒变化极慢，请在 2小时3分钟 后再试。", "旧道号",
        ))
        await self.tick(actor, AVATAR)
        self.assertEqual(actor.sent, [(AVATAR, PROTECT)])
        self.assertEqual(actor.get_soul_curse_state(AVATAR)["next_infer_time"], self.at(7380))


if __name__ == "__main__":
    unittest.main()
