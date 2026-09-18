"""Only an explicit, unexpired server cooldown may retry a rejected pill."""
import asyncio
import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import meditation_features as meditation
from tests import test_meditation_modes as fixtures


class MeditationPillRetryTests(unittest.TestCase):
    setUp = fixtures.MeditationModesTests.setUp
    select = fixtures.MeditationModesTests.select
    ready = fixtures.MeditationModesTests.ready

    def prepare(self, identity="主魂"):
        self.select(f"main|{identity}", mode="daily", use_heqi_pill=True)
        actor = fixtures.MeditationActor()
        runtime = self.ready(actor, identity, 1)
        actor.responses[".服用 合气丹"] = "【抗药性】你不久前才吃过合气丹，请在 **47秒** 后再服用。"
        self.clock = datetime(2026, 9, 18, 10)
        clock = patch.object(meditation, "datetime", wraps=datetime)
        clock.start().now.side_effect = lambda: self.clock
        self.addCleanup(clock.stop)
        asyncio.run(actor.configured_meditation_tick(identity))
        return actor, runtime

    def test_server_rejection_persists_retry_without_sleep_or_extra_cultivation(self):
        actor, runtime = self.prepare()
        self.assertEqual(actor.commands(), [".闭关修炼", ".服用 合气丹"])
        self.assertEqual(runtime["pill_status"], "cooldown")
        self.assertEqual(runtime["pill_available_at"], "2026-09-18 10:00:49")
        self.assertEqual(runtime["pill_retry"]["at"], "2026-09-18 10:00:49")
        self.assertEqual(runtime["success_count"], 2)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands(), [".闭关修炼", ".服用 合气丹"])

    def test_due_retry_uses_same_identity_and_counts_followup_once(self):
        actor, runtime = self.prepare("无咎子")
        actor.responses[".服用 合气丹"] = "你服下一枚【合气丹】，可以继续闭关了。接下来 2小时 内御宝法力 +2。"
        actor.sects["无咎子"] = "天星宗"
        self.clock += timedelta(seconds=49)
        asyncio.run(actor.configured_meditation_tick("无咎子"))
        asyncio.run(actor.configured_meditation_tick("无咎子"))
        self.assertEqual(actor.commands("无咎子"), [
            ".闭关修炼", ".服用 合气丹", ".服用 合气丹", ".推命 闭关", ".闭关修炼",
        ])
        self.assertEqual(actor.commands(), [])
        self.assertEqual(runtime["success_count"], 3)
        self.assertFalse(runtime.get("pill_retry"))
        self.assertFalse(runtime.get("pill_available_at"))

    def test_retry_survives_restart(self):
        actor, _ = self.prepare()
        restored = fixtures.MeditationActor()
        restored.state = copy.deepcopy(actor.state)
        self.clock += timedelta(seconds=60)
        asyncio.run(restored.configured_meditation_tick())
        self.assertEqual(restored.commands(), [".服用 合气丹", ".闭关修炼"])
        self.assertEqual(restored._meditation_runtime("主魂")["success_count"], 3)

    def test_uncertain_retry_reply_does_not_send_twice(self):
        actor, runtime = self.prepare()
        actor.responses[".服用 合气丹"] = ""
        self.clock += timedelta(seconds=60)
        asyncio.run(actor.configured_meditation_tick())
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands().count(".服用 合气丹"), 2)
        self.assertEqual(actor.commands().count(".闭关修炼"), 1)
        self.assertFalse(runtime.get("pill_retry"))

    def test_pill_deselection_cancels_pending_retry(self):
        actor, runtime = self.prepare()
        self.select(mode="daily", use_heqi_pill=False)
        self.clock += timedelta(seconds=60)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands().count(".服用 合气丹"), 1)
        self.assertFalse(runtime.get("pill_retry"))

    def test_paused_pill_does_not_block_normal_cultivation(self):
        actor, runtime = self.prepare()
        actor.paused_commands.add(("主魂", ".服用 合气丹"))
        self.clock += timedelta(seconds=60)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands().count(".服用 合气丹"), 1)
        self.clock += timedelta(minutes=10)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands().count(".闭关修炼"), 2)
        self.assertFalse(runtime.get("pill_retry"))

    def test_natural_cooldown_expiry_discards_retry_before_consumption(self):
        actor, runtime = self.prepare()
        self.clock += timedelta(minutes=11)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands(), [".闭关修炼", ".服用 合气丹", ".闭关修炼"])
        self.assertFalse(runtime.get("pill_retry"))

    def test_changed_success_count_invalidates_pending_retry(self):
        actor, runtime = self.prepare()
        runtime["success_count"] = 3
        self.clock += timedelta(seconds=60)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands().count(".服用 合气丹"), 1)
        self.assertFalse(runtime.get("pill_retry"))

    def test_repeated_server_cooldown_replaces_deadline(self):
        actor, runtime = self.prepare()
        self.clock += timedelta(seconds=60)
        actor.responses[".服用 合气丹"] = "【抗药性】请在 20秒 后再服用。"
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(runtime["pill_retry"]["at"], "2026-09-18 10:01:22")
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(actor.commands().count(".服用 合气丹"), 2)

    def test_known_long_cooldown_skips_future_pill_attempts(self):
        actor, runtime = self.prepare()
        self.clock += timedelta(seconds=60)
        actor.responses[".服用 合气丹"] = "【抗药性】请在 1小时 后再服用。"
        asyncio.run(actor.configured_meditation_tick())
        self.assertFalse(runtime.get("pill_retry"))
        self.clock += timedelta(minutes=10)
        asyncio.run(actor.configured_meditation_tick())
        self.clock += timedelta(minutes=10)
        asyncio.run(actor.configured_meditation_tick())
        self.assertEqual(runtime["success_count"], 4)
        self.assertEqual(actor.commands().count(".服用 合气丹"), 2)

    def test_cooldown_without_duration_does_not_create_retry(self):
        actor, runtime = self.prepare()
        self.clock += timedelta(seconds=60)
        actor.responses[".服用 合气丹"] = "【抗药性】现在不能服用。"
        asyncio.run(actor.configured_meditation_tick())
        asyncio.run(actor.configured_meditation_tick())
        self.assertFalse(runtime.get("pill_retry"))
        self.assertEqual(actor.commands().count(".服用 合气丹"), 2)


if __name__ == "__main__":
    unittest.main()
