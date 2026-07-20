import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import clear_history


class FakeTelegramClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.started = False
        self.disconnected = False
        self.resolved_target = None
        self.iter_target = None
        self.delete_calls = []
        self.messages = [
            SimpleNamespace(
                id=42,
                out=True,
                raw_text=".宗门点卯",
                date=datetime.now(timezone.utc) - timedelta(minutes=40),
                reply_to=None,
                reply_to_msg_id=None,
                reply_to_top_id=None,
            )
        ]
        self.__class__.instances.append(self)

    async def start(self):
        self.started = True

    async def get_input_entity(self, target):
        self.resolved_target = target
        return SimpleNamespace(channel_id=2083016447)

    async def get_me(self):
        return SimpleNamespace(id=1)

    def iter_messages(self, target, **kwargs):
        self.iter_target = target

        async def generate():
            for message in self.messages:
                yield message

        return generate()

    async def delete_messages(self, target, ids, revoke=True):
        self.delete_calls.append((target, list(ids), revoke))

    async def disconnect(self):
        self.disconnected = True


class ClearHistoryTests(unittest.TestCase):
    def setUp(self):
        FakeTelegramClient.instances.clear()

    def test_normalize_raw_channel_id(self):
        self.assertEqual(clear_history.normalize_group_target("2083016447"), -1002083016447)
        self.assertEqual(clear_history.normalize_group_target(2083016447), -1002083016447)
        self.assertEqual(clear_history.normalize_group_target("-1002083016447"), -1002083016447)
        self.assertEqual(clear_history.normalize_group_target("@game_group"), "@game_group")

    def test_delete_uses_resolved_group_entity(self):
        config = {
            "api_id": 1,
            "api_hash": "hash",
            "session": "session",
            "chat_id": "2083016447",
            "topic_id": None,
            "label": "waaiging",
        }
        with patch.object(clear_history, "load_account_config", return_value=config), patch.object(
            clear_history, "TelegramClient", FakeTelegramClient
        ):
            result = asyncio.run(
                clear_history.delete_sent_messages(
                    "waaiging",
                    scan_limit=None,
                    older_than_minutes=35,
                    use_session_copy=False,
                )
            )

        client = FakeTelegramClient.instances[-1]
        self.assertEqual(client.resolved_target, -1002083016447)
        self.assertIs(client.iter_target, client.delete_calls[0][0])
        self.assertEqual(client.delete_calls[0][1], [42])
        self.assertTrue(client.disconnected)
        self.assertEqual(result, (1, 1, 1, 1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
