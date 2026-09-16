import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import automation_settings as settings
import dashboard_server
from miniapp_command_routing import MiniAppCommandRouter


class SettingsFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "automation_settings.json"
        file_patch = patch.object(settings, "AUTOMATION_SETTINGS_FILE", self.path)
        file_patch.start()
        self.addCleanup(file_patch.stop)
        participants = patch.object(settings, "tianxing_tianji_auto_participants", return_value=[])
        participants.start()
        self.addCleanup(participants.stop)

    def save_lead(self, seconds):
        return settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="护阵",
            star_gazing_lead_seconds=seconds, updated_by="test",
        )


class StarGazingSettingsTests(SettingsFixture, unittest.TestCase):
    def test_old_settings_default_to_ten_seconds(self):
        self.path.write_text(json.dumps({"version": 12}), encoding="utf-8")

        self.assertEqual(settings.star_gazing_settings(), {"lead_seconds": 10})
        self.assertEqual(settings.load_automation_settings()["version"], 16)
        self.assertEqual(json.loads(self.path.read_text())["version"], 12)

    def test_custom_timing_survives_unrelated_settings_save(self):
        initial = self.save_lead(37)

        saved = settings.save_automation_settings(
            world_boss_participants=[], mulan_support_mode="奇袭", updated_by="test",
        )

        self.assertEqual(saved["star_gazing"], {"lead_seconds": 37})
        self.assertEqual(saved["miniapp_fishing"], initial["miniapp_fishing"])
        self.assertEqual(settings.star_gazing_settings(), {"lead_seconds": 37})

    def test_invalid_input_cannot_overwrite_saved_timing(self):
        self.save_lead(20)
        before = self.path.read_bytes()
        for value in (-121, 121, 10.5, -10.0, "", "bad", [], {}, True, False):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "invalid star gazing lead seconds"):
                    self.save_lead(value)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(settings.star_gazing_settings({"star_gazing": {"lead_seconds": value}}),
                                 {"lead_seconds": 10})

    def test_range_boundaries_and_presets_are_available_to_dashboard(self):
        for value in (-120, -1, 0, 1, 120):
            with self.subTest(value=value):
                self.save_lead(value)
                payload = settings.automation_dashboard_payload()["star_gazing"]
                self.assertEqual(payload["lead_seconds"], value)
                self.assertEqual([item["key"] for item in payload["lead_options"]],
                                 ["120", "60", "30", "20", "10", "5", "0", "-5", "-10", "-20", "-30", "-60", "-120"])
                self.assertEqual((payload["min_lead_seconds"], payload["max_lead_seconds"]), (-120, 120))
                labels = {item["key"]: item["name"] for item in payload["lead_options"]}
                self.assertEqual(labels["-10"], "显化后 10 秒")
                self.assertEqual(labels["0"], "显化时发送")

    def test_signed_integer_strings_persist_as_numbers(self):
        for value in ("-37", "0", "+37"):
            with self.subTest(value=value):
                self.save_lead(value)
                self.assertEqual(settings.star_gazing_settings(), {"lead_seconds": int(value)})
                saved = settings.save_automation_settings(
                    world_boss_participants=[], mulan_support_mode="奇袭", updated_by="test",
                )
                self.assertEqual(saved["star_gazing"], {"lead_seconds": int(value)})

    def test_dashboard_save_persists_custom_lead_and_rejects_invalid_values(self):
        payload = {"world_boss_participants": [], "mulan_support_mode": "护阵",
                   "star_gazing": {"lead_seconds": 73}}

        result = asyncio.run(dashboard_server.automation_settings_control(payload, username="test"))

        self.assertTrue(result["success"])
        self.assertEqual(settings.star_gazing_settings(), {"lead_seconds": 73})
        payload["star_gazing"]["lead_seconds"] = -121
        rejected = asyncio.run(dashboard_server.automation_settings_control(payload, username="test"))
        self.assertFalse(rejected["success"])
        self.assertIn("-120 至 120", rejected["msg"])
        self.assertEqual(settings.star_gazing_settings(), {"lead_seconds": 73})

    def test_dashboard_accepts_negative_and_zero_offsets(self):
        for value in (-37, 0):
            with self.subTest(value=value):
                payload = {"world_boss_participants": [], "mulan_support_mode": "护阵",
                           "star_gazing": {"lead_seconds": value}}
                result = asyncio.run(dashboard_server.automation_settings_control(payload, username="test"))
                self.assertTrue(result["success"])
                self.assertEqual(settings.star_gazing_settings(), {"lead_seconds": value})


class StarGazingSchedulerTests(SettingsFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.clock = [datetime(2026, 9, 8, 8, 58, 0)]
        clock = self.clock

        class TestClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        time_patch = patch("miniapp_command_routing.datetime", TestClock)
        time_patch.start()
        self.addCleanup(time_patch.stop)
        self.actor = SimpleNamespace(state={}, is_running=True, save_state=Mock())
        self.router = MiniAppCommandRouter.__new__(MiniAppCommandRouter)
        self.router.actor = self.actor
        self.router.account = "main"
        self.router.log = Mock()
        self.manifest = datetime(2026, 9, 8, 9, 0, 0)
        self.after_sleep = None

        async def advance(seconds):
            self.assertGreater(seconds, 0)
            self.clock[0] += timedelta(seconds=seconds)
            if self.after_sleep is not None:
                self.after_sleep()

        self.sleep = AsyncMock(side_effect=advance)
        sleep_patch = patch("miniapp_command_routing.asyncio.sleep", self.sleep)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    async def wait_for_send(self):
        return await self.router._wait_for_star_palace_send_time("素缘子", self.manifest)

    async def test_default_sends_at_ten_seconds_before_manifestation(self):
        self.assertTrue(await self.wait_for_send())
        self.assertEqual(self.clock[0], datetime(2026, 9, 8, 8, 59, 50))
        self.assertEqual(self.actor.state["miniapp_star_palace_next_divine_time"], "2026-09-08 08:59:50")

    async def test_custom_value_controls_actual_wake_time(self):
        self.save_lead(37)

        self.assertTrue(await self.wait_for_send())

        self.assertEqual(self.clock[0], datetime(2026, 9, 8, 8, 59, 23))
        self.assertEqual(self.actor.state["miniapp_star_palace_lead_seconds"], 37)

    async def test_negative_offset_waits_across_manifestation(self):
        self.save_lead(-10)

        self.assertTrue(await self.wait_for_send())

        self.assertEqual(self.clock[0], self.manifest + timedelta(seconds=10))
        self.assertEqual(self.actor.state["miniapp_star_palace_next_divine_time"], "2026-09-08 09:00:10")

    async def test_zero_offset_sends_at_manifestation(self):
        self.save_lead(0)

        self.assertTrue(await self.wait_for_send())

        self.assertEqual(self.clock[0], self.manifest)

    async def test_fractional_final_wait_preserves_exact_send_time(self):
        self.save_lead(-10)
        self.clock[0] = self.manifest + timedelta(seconds=9.75)

        self.assertTrue(await self.wait_for_send())

        self.sleep.assert_awaited_once_with(0.25)
        self.assertEqual(self.clock[0], self.manifest + timedelta(seconds=10))

    async def test_restart_after_boundary_keeps_original_manifestation(self):
        self.save_lead(-10)
        self.clock[0] = self.manifest + timedelta(seconds=5)
        selected = self.router._star_palace_manifest_for_send()

        self.assertEqual(selected, self.manifest)
        self.assertTrue(await self.router._wait_for_star_palace_send_time("素缘子", selected))
        self.assertEqual(self.clock[0], self.manifest + timedelta(seconds=10))

    async def test_negative_and_zero_grace_expires_without_late_send(self):
        for lead in (-120, -10, 0):
            with self.subTest(lead=lead):
                self.save_lead(lead)
                self.clock[0] = self.manifest + timedelta(seconds=-lead + 4.5)
                self.assertEqual(self.router._star_palace_manifest_for_send(), self.manifest)
                self.assertTrue(await self.wait_for_send())
                self.clock[0] += timedelta(seconds=0.5)
                self.assertFalse(await self.wait_for_send())
                self.assertEqual(self.router._star_palace_manifest_for_send(), self.manifest + timedelta(hours=3))
        self.sleep.assert_not_awaited()

    async def test_earlier_saved_time_moves_an_existing_wait_forward(self):
        def change_once():
            self.save_lead(60)
            self.after_sleep = None

        self.after_sleep = change_once

        self.assertTrue(await self.wait_for_send())

        self.assertEqual(self.clock[0], datetime(2026, 9, 8, 8, 59, 0))

    async def test_later_saved_time_postpones_an_existing_wait(self):
        self.save_lead(60)

        def change_once():
            self.save_lead(5)
            self.after_sleep = None

        self.after_sleep = change_once

        self.assertTrue(await self.wait_for_send())

        self.assertEqual(self.clock[0], datetime(2026, 9, 8, 8, 59, 55))

    async def test_pending_timing_can_change_between_positive_and_negative(self):
        for initial, updated in ((10, -10), (-10, 10)):
            with self.subTest(initial=initial, updated=updated):
                self.save_lead(initial)
                self.clock[0] = self.manifest - timedelta(seconds=30)

                def change_once():
                    self.save_lead(updated)
                    self.after_sleep = None

                self.after_sleep = change_once
                self.assertTrue(await self.wait_for_send())
                self.assertEqual(self.clock[0], self.manifest - timedelta(seconds=updated))

    async def test_changing_to_positive_after_boundary_skips_expired_round(self):
        self.save_lead(-10)
        self.clock[0] = self.manifest

        def change_once():
            self.save_lead(10)
            self.after_sleep = None

        self.after_sleep = change_once

        self.assertFalse(await self.wait_for_send())
        self.assertEqual(self.router._star_palace_manifest_for_send(), self.manifest + timedelta(hours=3))

    async def test_restart_recalculates_a_stale_saved_plan(self):
        self.actor.state["miniapp_star_palace_next_divine_time"] = "2026-09-08 08:59:50"
        self.save_lead(20)

        self.assertTrue(await self.wait_for_send())

        self.assertEqual(self.clock[0], datetime(2026, 9, 8, 8, 59, 40))

    async def test_late_arrival_sends_before_boundary_without_further_wait(self):
        self.clock[0] = datetime(2026, 9, 8, 8, 59, 56)

        self.assertTrue(await self.wait_for_send())

        self.sleep.assert_not_awaited()

    async def test_no_send_at_or_after_manifestation(self):
        for time in (self.manifest, self.manifest + timedelta(seconds=1)):
            with self.subTest(time=time):
                self.clock[0] = time
                self.assertFalse(await self.wait_for_send())
        self.sleep.assert_not_awaited()

    async def test_pause_past_boundary_does_not_send_to_a_new_round(self):
        async def resume_late():
            self.clock[0] = self.manifest + timedelta(seconds=1)

        self.actor.pause_event = SimpleNamespace(wait=AsyncMock(side_effect=resume_late))

        self.assertFalse(await self.wait_for_send())

        self.sleep.assert_not_awaited()

    async def test_good_notice_inside_old_ninety_second_cutoff_is_not_discarded(self):
        self.clock[0] = datetime(2026, 9, 8, 8, 59, 0)
        with patch("miniapp_command_routing._confirmed_star_palace_good",
                   side_effect=[None, {"fate_type": "Good - 地磁暴动"}]):
            ready = await self.router._wait_for_confirmed_star_palace_good("素缘子", self.manifest)

        self.assertTrue(ready)
        self.assertEqual(self.clock[0], datetime(2026, 9, 8, 8, 59, 5))

    async def test_missing_good_notice_expires_without_sending(self):
        self.clock[0] = datetime(2026, 9, 8, 8, 59, 57)
        with patch("miniapp_command_routing._confirmed_star_palace_good", return_value=None):
            ready = await self.router._wait_for_confirmed_star_palace_good("素缘子", self.manifest)

        self.assertFalse(ready)
        self.assertEqual(self.clock[0], self.manifest)

    async def test_good_notice_after_boundary_can_trigger_delayed_send(self):
        self.save_lead(-10)
        self.clock[0] = self.manifest
        with patch("miniapp_command_routing._confirmed_star_palace_good",
                   side_effect=[None, {"fate_type": "Good - 地磁暴动"}]) as notice:
            ready = await self.router._wait_for_confirmed_star_palace_good("素缘子", self.manifest)

        self.assertTrue(ready)
        self.assertEqual(self.clock[0], self.manifest + timedelta(seconds=5))
        notice.assert_called_with("2026-09-08 09:00:00")
        self.assertTrue(await self.wait_for_send())
        self.assertEqual(self.clock[0], self.manifest + timedelta(seconds=10))

    async def test_missing_good_notice_expires_at_delayed_deadline(self):
        self.save_lead(-10)
        self.clock[0] = self.manifest
        with patch("miniapp_command_routing._confirmed_star_palace_good", return_value=None):
            ready = await self.router._wait_for_confirmed_star_palace_good("素缘子", self.manifest)

        self.assertFalse(ready)
        self.assertEqual(self.clock[0], self.manifest + timedelta(seconds=15))

    async def test_round_claim_and_cycle_begin_only_at_configured_time(self):
        observed = []

        async def cycle(*args):
            observed.append(self.clock[0])
            self.actor.is_running = False
            return True

        self.router.run_star_palace_cycle = AsyncMock(side_effect=cycle)
        claim_times = []

        def claim(*args):
            claim_times.append(self.clock[0])
            return True

        with patch("miniapp_command_routing._confirmed_star_palace_good", return_value={"fate_type": "Good - 地磁暴动"}), \
             patch("miniapp_command_routing._claim_star_palace_attempt", side_effect=claim), \
             patch("miniapp_command_routing._finish_star_palace_attempt") as finish:
            await self.router.run_star_palace_divine_loop("素缘子")

        expected = [datetime(2026, 9, 8, 8, 59, 50)]
        self.assertEqual(claim_times, expected)
        self.assertEqual(observed, expected)
        self.assertEqual(self.actor.state["miniapp_star_palace_next_divine_time"], "")
        finish.assert_called_once_with("main", "素缘子", "2026-09-08 09:00:00", True)

    async def test_delayed_loop_claims_original_manifestation_after_restart(self):
        self.save_lead(-10)
        self.clock[0] = self.manifest + timedelta(seconds=5)
        observed = []

        async def cycle(identity, manifest_dt, target):
            observed.append((self.clock[0], manifest_dt))
            self.actor.is_running = False
            return True

        self.router.run_star_palace_cycle = AsyncMock(side_effect=cycle)
        with patch("miniapp_command_routing._confirmed_star_palace_good", return_value={"fate_type": "Good - 地磁暴动"}), \
             patch("miniapp_command_routing._claim_star_palace_attempt", return_value=True) as claim, \
             patch("miniapp_command_routing._finish_star_palace_attempt") as finish:
            await self.router.run_star_palace_divine_loop("素缘子")

        self.assertEqual(observed, [(self.manifest + timedelta(seconds=10), self.manifest)])
        claim.assert_called_once_with("main", "素缘子", "2026-09-08 09:00:00")
        finish.assert_called_once_with("main", "素缘子", "2026-09-08 09:00:00", True)

    async def test_daily_completion_does_not_skip_midnight_zero_or_delayed_send(self):
        for lead in (0, -10):
            with self.subTest(lead=lead):
                self.save_lead(lead)
                self.clock[0] = datetime(2026, 9, 8, 23, 59, 58)
                self.actor.is_running = True
                self.actor.state["miniapp_star_palace_done_date"] = "2026-09-08"
                observed = []

                async def cycle(identity, manifest_dt, target):
                    observed.append((self.clock[0], manifest_dt))
                    self.actor.is_running = False
                    return True

                self.router.run_star_palace_cycle = AsyncMock(side_effect=cycle)
                with patch("miniapp_command_routing._confirmed_star_palace_good", return_value={"fate_type": "Good - 地磁暴动"}), \
                     patch("miniapp_command_routing._claim_star_palace_attempt", return_value=True), \
                     patch("miniapp_command_routing._finish_star_palace_attempt"):
                    await self.router.run_star_palace_divine_loop("素缘子")

                midnight = datetime(2026, 9, 9)
                self.assertEqual(observed, [(midnight - timedelta(seconds=lead), midnight)])

    async def test_cancellation_while_waiting_never_claims_or_sends(self):
        self.sleep.side_effect = asyncio.CancelledError()
        self.router.run_star_palace_cycle = AsyncMock()
        with patch("miniapp_command_routing._confirmed_star_palace_good", return_value={"fate_type": "Good - 地磁暴动"}), \
             patch("miniapp_command_routing._claim_star_palace_attempt") as claim:
            with self.assertRaises(asyncio.CancelledError):
                await self.router.run_star_palace_divine_loop("素缘子")
        claim.assert_not_called()
        self.router.run_star_palace_cycle.assert_not_awaited()
        self.assertEqual(self.actor.state["miniapp_star_palace_next_divine_time"], "")


if __name__ == "__main__":
    unittest.main()
