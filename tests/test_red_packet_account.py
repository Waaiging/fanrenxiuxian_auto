import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from miniapp_beast import MiniAppCircuitOpenError
from red_packet_account import (
    install_restricted_exchange_monitor,
    resume_restricted_miniapp_after_circuit,
)


class RestrictedExchangeMonitorTests(unittest.TestCase):
    def test_installs_new_and_edited_handlers_for_game_chat(self):
        class FakeClient:
            def __init__(self):
                self.handlers = []

            def add_event_handler(self, callback, builder):
                self.handlers.append((callback, builder))

        actor = SimpleNamespace(
            account_key="xiaohao",
            target_chat_id=2083016447,
            client=FakeClient(),
        )
        event = SimpleNamespace(message=SimpleNamespace(id=1, text="南陇侯"))

        with patch(
            "red_packet_account.maybe_restricted_exchange_place",
            new=AsyncMock(return_value=True),
        ) as handler:
            registrations = install_restricted_exchange_monitor(actor)
            asyncio.run(registrations[0][0](event))

        self.assertEqual(len(registrations), 2)
        self.assertEqual(actor.client.handlers, registrations)
        handler.assert_awaited_once_with(actor, event)

    def test_startup_recovery_only_runs_after_breaker_waits(self):
        actor = SimpleNamespace(
            is_running=True,
            state={},
            save_state=Mock(),
        )
        worker = SimpleNamespace(
            start=AsyncMock(
                side_effect=[
                    MiniAppCircuitOpenError(900, "2026-08-19 13:15:00"),
                    None,
                ]
            )
        )
        logger = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
        sleep = AsyncMock()

        with patch("red_packet_account.asyncio.sleep", new=sleep):
            recovered = asyncio.run(
                resume_restricted_miniapp_after_circuit(
                    worker,
                    actor,
                    "xiaohao",
                    MiniAppCircuitOpenError(600, "2026-08-19 13:00:00"),
                    logger=logger,
                )
            )

        self.assertTrue(recovered)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [600, 900])
        self.assertEqual(worker.start.await_count, 2)
        logger.error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
