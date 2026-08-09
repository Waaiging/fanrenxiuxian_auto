import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from red_packet_account import install_restricted_exchange_monitor


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


if __name__ == "__main__":
    unittest.main()
