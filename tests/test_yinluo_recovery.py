"""Regression coverage for no-op appease loops and saved recovery progress."""
import asyncio
from collections import defaultdict
from datetime import datetime
import json
from types import SimpleNamespace
import unittest

from common_command_features import add_seconds_str, now_str
from yinluo_features import (
    YINLUO_APPEASE_COMMAND,
    YINLUO_MASTER_COMMAND,
    YINLUO_SOUL,
    YinluoMixin,
)


def banner(status="空闲", reserves=0):
    return (
        "【阴罗幡】\n煞气池: 3000 / 25000\n"
        f"魂魄储备:\n - {YINLUO_SOUL}: {reserves} 缕\n"
        f"炼化槽:\n1号槽: [{status}]\n"
    )


class Actor(YinluoMixin):
    def __init__(self, identity="主魂", saved=None):
        self.identity = identity
        self.state = json.loads(json.dumps(saved)) if saved else {
            "identity_sect_names": {identity: "阴罗宗"},
            "avatars": {} if identity == "主魂" else {identity: {}},
            "unrelated_progress": {"value": 27},
        }
        self.responses = defaultdict(list)
        self.sent = []
        self.saved = None
        if saved is None:
            self.get_yinluo_state(identity).update({
                "last_daily_sacrifice_date": datetime.now().strftime("%Y-%m-%d"),
                "next_daily_sacrifice_time": add_seconds_str(now_str(), 86400),
                "next_summon_shadow_time": add_seconds_str(now_str(), 8 * 3600),
                "next_blood_wash_time": add_seconds_str(now_str(), 4 * 3600),
                "next_action_at": add_seconds_str(now_str(), 4 * 3600),
                "sha_current": 3000,
                "slots": {"1": {"status": "魂力枯竭", "soul": "", "due_at": ""}},
            })

    def get_avatar_state(self, identity):
        return self.state["avatars"][identity]

    def save_state(self):
        self.saved = json.loads(json.dumps(self.state))

    async def send_and_wait_feedback_identity(self, identity, command, **kwargs):
        self.sent.append((identity, command))
        if not self.responses[command]:
            raise AssertionError(f"Unexpected command: {command}")
        response = self.responses[command].pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)

    def tick(self):
        return asyncio.run(self.yinluo_tick(self.identity))


class YinluoRecoveryTests(unittest.TestCase):
    def test_noop_refreshes_slot_and_does_not_repeat_after_suppression_expires(self):
        actor = Actor()
        actor.responses[YINLUO_APPEASE_COMMAND] = ["没有需要操作的炼化槽。"]
        actor.responses[YINLUO_MASTER_COMMAND] = [banner()]
        actor.tick()
        state = actor.get_yinluo_state(actor.identity)
        self.assertTrue(state["appease_sync_pending"])
        self.assertEqual(actor.yinluo_slot_item(actor.identity, 1)["status"], "状态待同步")
        state["appease_suppressed_until"]["1"] = add_seconds_str(now_str(), -1)
        actor.tick()
        for _ in range(3):
            actor.tick()
        self.assertEqual(actor.sent, [
            (actor.identity, YINLUO_APPEASE_COMMAND),
            (actor.identity, YINLUO_MASTER_COMMAND),
        ])
        self.assertFalse(state["appease_sync_pending"])
        self.assertEqual(actor.yinluo_slot_item(actor.identity, 1)["status"], "空闲")
        self.assertEqual(state["appease_suppressed_until"], {})

    def test_saved_legacy_noop_is_refreshed_before_any_new_appease(self):
        for identity in ("主魂", "主号阴罗化身", "副号阴罗化身"):
            for key, delta in (("1", 1800), (1, -1800)):
                with self.subTest(identity=identity, key=key, delta=delta):
                    actor = Actor(identity)
                    state = actor.get_yinluo_state(identity)
                    state.pop("appease_sync_pending")
                    state["appease_suppressed_until"] = {key: add_seconds_str(now_str(), delta)}
                    state["last_status"] = "appease_noop"
                    actor.save_state()
                    resumed = Actor(identity, actor.saved)
                    resumed.responses[YINLUO_MASTER_COMMAND] = [banner()]
                    resumed.tick()
                    resumed.tick()
                    self.assertEqual(resumed.sent, [(identity, YINLUO_MASTER_COMMAND)])
                    self.assertEqual(resumed.state["unrelated_progress"], {"value": 27})

    def test_sync_failure_and_restart_preserve_backoff_without_resending_appease(self):
        for failure in ("", RuntimeError("temporary transport failure")):
            with self.subTest(failure=repr(failure)):
                actor = Actor()
                state = actor.get_yinluo_state(actor.identity)
                state["appease_sync_pending"] = True
                actor.responses[YINLUO_MASTER_COMMAND] = [failure]
                if isinstance(failure, Exception):
                    with self.assertRaises(RuntimeError):
                        actor.tick()
                else:
                    actor.tick()
                resumed = Actor(actor.identity, actor.saved)
                self.assertGreater(resumed.tick(), 500)
                self.assertEqual(resumed.sent, [])
                state = resumed.get_yinluo_state(resumed.identity)
                state["next_sync_at"] = add_seconds_str(now_str(), -1)
                resumed.responses[YINLUO_MASTER_COMMAND] = [banner()]
                resumed.tick()
                self.assertEqual(resumed.sent, [(actor.identity, YINLUO_MASTER_COMMAND)])
                self.assertFalse(state["appease_sync_pending"])

    def test_post_summon_recovery_resumes_imprison_without_repeating_appease(self):
        actor = Actor()
        state = actor.get_yinluo_state(actor.identity)
        state.update(post_summon_stage="appease", next_action_at="", reserves={YINLUO_SOUL: 1})
        actor.responses[YINLUO_APPEASE_COMMAND] = ["没有需要操作的炼化槽。"]
        actor.responses[YINLUO_MASTER_COMMAND] = [""]
        self.assertFalse(asyncio.run(actor.yinluo_run_post_summon_flow(actor.identity)))
        self.assertEqual(state["post_summon_stage"], "imprison")
        resumed = Actor(actor.identity, actor.saved)
        state = resumed.get_yinluo_state(resumed.identity)
        state["next_sync_at"] = add_seconds_str(now_str(), -1)
        resumed.responses[YINLUO_MASTER_COMMAND] = [banner(reserves=1)]
        command = f".囚禁魂魄 1 {YINLUO_SOUL}"
        resumed.responses[command] = [f"一缕【{YINLUO_SOUL}】被强行打入1号炼化槽，炼化已开始。"]
        resumed.tick()
        resumed.tick()
        self.assertEqual(resumed.sent, [(actor.identity, YINLUO_MASTER_COMMAND), (actor.identity, command)])
        self.assertEqual(state["post_summon_stage"], "")
        self.assertEqual(state["last_status"], "summon_flow_complete")
        self.assertEqual(actor.yinluo_slot_item(resumed.identity, 1)["status"], "状态待同步")
        self.assertEqual(resumed.yinluo_slot_item(resumed.identity, 1)["status"], "炼化中")

    def test_forced_noop_with_no_exhausted_slots_does_not_trigger_a_query(self):
        actor = Actor()
        state = actor.get_yinluo_state(actor.identity)
        state["slots"]["1"]["status"] = "空闲"
        state["post_summon_stage"] = "appease"
        actor.responses[YINLUO_APPEASE_COMMAND] = ["没有需要操作的炼化槽。"]
        self.assertTrue(asyncio.run(actor.yinluo_run_post_summon_flow(actor.identity)))
        actor.tick()
        self.assertEqual(actor.sent, [(actor.identity, YINLUO_APPEASE_COMMAND)])
        self.assertFalse(state["appease_sync_pending"])

    def test_legacy_suppression_on_idle_slot_does_not_create_periodic_queries(self):
        actor = Actor()
        state = actor.get_yinluo_state(actor.identity)
        state.pop("appease_sync_pending")
        state["slots"]["1"]["status"] = "空闲"
        state["appease_suppressed_until"] = {"1": add_seconds_str(now_str(), -3600)}
        state["next_sync_at"] = add_seconds_str(now_str(), -3600)
        actor.tick()
        self.assertEqual(actor.sent, [])

    def test_paused_or_changed_sect_blocks_pending_sync(self):
        for paused in (True, False):
            with self.subTest(paused=paused):
                actor = Actor()
                actor.get_yinluo_state(actor.identity)["appease_sync_pending"] = True
                actor.state["is_paused"] = paused
                if not paused:
                    actor.state["identity_sect_names"][actor.identity] = "星宫"
                actor.tick()
                self.assertEqual(actor.sent, [])


if __name__ == "__main__":
    unittest.main()
