import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from common_command_features import CommonCommandMixin
from cultivator_xiaohao import CultivatorXiaoHao
from intelligent_cultivator import Cultivator
from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError
from miniapp_command_routing import MiniAppCommandRouter
from miniapp_dwelling import MiniAppCommandResponse, MiniAppDwellingTransport
from sub_cultivator import SubCultivator


def relative_time(**kwargs):
    return (datetime.now() + timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


class YuanyingActor(CommonCommandMixin):
    def __init__(self):
        self.state = {"avatars": {"分身": {}}}
        self.log = Mock()

    def common_command_logger(self):
        return self.log

    def get_avatar_state(self, identity):
        return self.state["avatars"][identity]


class YuanyingRecoveryTests(unittest.TestCase):
    def test_late_timeout_preserves_confirmed_active_deadline_for_each_identity(self):
        for identity in ("主魂", "分身"):
            with self.subTest(identity=identity):
                actor = YuanyingActor()
                # The manual start reply arrives while an older automatic send
                # is waiting. Its later timeout must not shorten this deadline.
                state = actor.identity_state_for_timed_command(identity)
                deadline = relative_time(hours=8)
                state.update(
                    yuanying_out_active=True,
                    last_yuanying_out_time=relative_time(),
                    yuanying_out_end_time=deadline,
                    next_yuanying_out_time=deadline,
                )
                self.assertFalse(actor.record_yuanying_out_start_response(None, identity))
                self.assertEqual(state["next_yuanying_out_time"], deadline)
                self.assertEqual(state["yuanying_out_end_time"], deadline)
                self.assertTrue(state["yuanying_out_active"])

    def test_late_timeout_preserves_retreat_waiting_for_passive_settlement(self):
        actor = YuanyingActor()
        actor.yuanying_main_command = ".元婴闭关"
        actor.state.update(
            yuanying_out_active=True,
            yuanying_out_end_time="",
            next_yuanying_out_time="",
        )
        self.assertFalse(actor.record_yuanying_out_start_response(None))
        self.assertEqual(actor.state["next_yuanying_out_time"], "")
        self.assertTrue(actor.state["yuanying_out_active"])

    def test_unconfirmed_timeout_still_schedules_retry(self):
        actor = YuanyingActor()
        self.assertFalse(actor.record_yuanying_out_start_response(None))
        retry = datetime.strptime(actor.state["next_yuanying_out_time"], "%Y-%m-%d %H:%M:%S")
        self.assertGreater((retry - datetime.now()).total_seconds(), 3500)
        self.assertLessEqual((retry - datetime.now()).total_seconds(), 3600)

    def test_watchdogs_use_active_end_instead_of_stale_retry_from_saved_state(self):
        for cls in (Cultivator, SubCultivator, CultivatorXiaoHao):
            with self.subTest(worker=cls.__name__):
                actor = cls.__new__(cls)
                actor.state = {
                    "is_paused": False,
                    "identity_pauses": {},
                    "yuanying_out_active": True,
                    "next_yuanying_out_time": relative_time(hours=-2),
                    "yuanying_out_end_time": relative_time(hours=5),
                }
                actor.avatars = []
                actor.enable_concubine = False
                actor.main_star_palace_enabled = False
                actor.identity_pause_seconds = lambda identity="主魂": 0
                actor.main_soul_pause_seconds = lambda: 0
                actor.state_time_command_paused = lambda key, identity="": False
                actor.dashboard_command_paused = lambda command, identity="": False
                actor.identity_sect_name = lambda identity: ""
                self.assertEqual(actor.stale_scheduler_due_items(), [])

                # An active Yuanying task must not hide another overdue task.
                actor.state["next_rift_search_time"] = relative_time(hours=-2)
                stale = actor.stale_scheduler_due_items()
                self.assertEqual([item[0] for item in stale], ["next_rift_search_time"])
                del actor.state["next_rift_search_time"]

                # Expired active deadlines and inactive retries still expose a
                # stopped scheduler, even if the other timestamp is in future.
                actor.state["next_yuanying_out_time"] = relative_time(hours=5)
                actor.state["yuanying_out_end_time"] = relative_time(hours=-2)
                self.assertEqual(len(actor.stale_scheduler_due_items()), 1)
                actor.state["yuanying_out_active"] = False
                actor.state["next_yuanying_out_time"] = relative_time(hours=-2)
                actor.state["yuanying_out_end_time"] = relative_time(hours=5)
                self.assertEqual(len(actor.stale_scheduler_due_items()), 1)


class SharedTransportRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_router(self):
        entry = "https://t.me/fanrenxiuxian_bot?startapp=df_fixture"
        actor = SimpleNamespace(
            client=object(), config={"miniapp_beast": {"entry_url": entry}},
            state={}, avatars=[], is_running=True, save_state=Mock(),
            send_and_wait_feedback=AsyncMock(),
        )
        transport = MiniAppDwellingTransport(object(), entry)
        transport.identity_player_ids = {"主魂": 100}
        transport.initialize = AsyncMock()
        transport.overview = AsyncMock(return_value={})
        transport.command = AsyncMock(return_value=MiniAppCommandResponse("已查看闭关", {}))
        router = MiniAppCommandRouter(
            actor, "xiaohao", logger=Mock(), transport=transport,
            start_background_tasks=False,
        )
        return actor, router, transport

    async def finish_recovery(self, router):
        self.assertIsNotNone(router._recovery_task)
        task = router._recovery_task
        router._start_recovery_task()
        self.assertIs(router._recovery_task, task)
        with patch("miniapp_command_routing.asyncio.sleep", new=AsyncMock()), patch(
            "miniapp_beast.miniapp_circuit_preflight", return_value=None
        ):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(router._route_active)
        self.assertIsNone(router._profile_task)
        self.assertEqual(router._daily_activity_tasks, [])
        self.assertEqual(router._star_palace_tasks, {})

    async def test_shared_transport_recovers_after_setup_failure(self):
        actor, router, transport = self.make_router()
        transport.initialize.side_effect = [MiniAppBeastError("invalid_json"), None]
        self.assertFalse(await router.install())
        self.assertIsNone(await actor.send_and_wait_feedback(".查看闭关"))
        router._orig_send.assert_not_awaited()
        await self.finish_recovery(router)
        transport.initialize.assert_any_await(force=True)
        self.assertEqual(await actor.send_and_wait_feedback(".查看闭关"), "已查看闭关")
        router._orig_send.assert_not_awaited()

    async def test_shared_transport_recovers_after_circuit_or_token_failure(self):
        for error in (MiniAppCircuitOpenError(60), MiniAppBeastError("dwelling_token_expired")):
            with self.subTest(error=error.code):
                actor, router, transport = self.make_router()
                self.assertTrue(await router.install())
                transport.command.side_effect = [error, MiniAppCommandResponse("已查看闭关", {})]
                self.assertIsNone(await actor.send_and_wait_feedback(".查看闭关"))
                self.assertFalse(router._route_active)
                await self.finish_recovery(router)
                self.assertEqual(await actor.send_and_wait_feedback(".查看闭关"), "已查看闭关")
                router._orig_send.assert_not_awaited()

    async def test_disabled_route_does_not_start_recovery(self):
        _, router, transport = self.make_router()
        router.enabled = False
        self.assertFalse(await router.install())
        router._start_recovery_task()
        self.assertIsNone(router._recovery_task)
        transport.initialize.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
