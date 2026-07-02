import asyncio
import unittest

from log_utils import handle_pause_control_command


class DummyClient:
    def __init__(self):
        self.sent = []
        self.deleted = []

    async def send_message(self, user_id, text):
        self.sent.append((user_id, text))

    async def delete_messages(self, chat_id, msg):
        self.deleted.append((chat_id, getattr(msg, "id", None)))


class DummyActor:
    def __init__(self):
        self.target_chat_id = 1680975844
        self.pause_admins = {8219248252, -1004240160265}
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.pause_control_event = asyncio.Event()
        self.state = {"is_paused": False}
        self.client = DummyClient()
        self.pause_notify_user_id = 8219248252
        self.saved = 0

    def save_state(self):
        self.saved += 1


class DummyMsg:
    def __init__(self, text, sender_id=8219248252, chat_id=-1001680975844, msg_id=1, out=False):
        self.text = text
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.id = msg_id
        self.out = out


class PauseControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_pauses_authorized_sender(self):
        actor = DummyActor()
        msg = DummyMsg("0")

        handled = await handle_pause_control_command(actor, msg, msg.text, label="测试脚本")

        self.assertTrue(handled)
        self.assertFalse(actor.pause_event.is_set())
        self.assertTrue(actor.state["is_paused"])
        self.assertEqual(actor.saved, 1)
        self.assertEqual(len(actor.client.sent), 1)
        self.assertEqual(actor.client.deleted, [(actor.target_chat_id, msg.id)])

    async def test_one_resumes_authorized_avatar_channel(self):
        actor = DummyActor()
        actor.pause_event.clear()
        actor.state["is_paused"] = True
        msg = DummyMsg("1", sender_id=4240160265)

        handled = await handle_pause_control_command(actor, msg, msg.text, label="测试脚本")

        self.assertTrue(handled)
        self.assertTrue(actor.pause_event.is_set())
        self.assertFalse(actor.state["is_paused"])
        self.assertEqual(actor.saved, 1)
        self.assertEqual(len(actor.client.sent), 1)

    async def test_unauthorized_zero_is_not_consumed(self):
        actor = DummyActor()
        msg = DummyMsg("0", sender_id=12345)

        handled = await handle_pause_control_command(actor, msg, msg.text, label="测试脚本")

        self.assertFalse(handled)
        self.assertTrue(actor.pause_event.is_set())
        self.assertFalse(actor.state["is_paused"])
        self.assertEqual(actor.saved, 0)
        self.assertEqual(actor.client.sent, [])
        self.assertEqual(actor.client.deleted, [])

    async def test_duplicate_pause_is_consumed_without_extra_notice(self):
        actor = DummyActor()
        actor.pause_event.clear()
        actor.state["is_paused"] = True
        msg = DummyMsg(".0")

        handled = await handle_pause_control_command(actor, msg, msg.text, label="测试脚本")

        self.assertTrue(handled)
        self.assertFalse(actor.pause_event.is_set())
        self.assertTrue(actor.state["is_paused"])
        self.assertEqual(actor.saved, 1)
        self.assertEqual(actor.client.sent, [])
        self.assertEqual(actor.client.deleted, [(actor.target_chat_id, msg.id)])


if __name__ == "__main__":
    unittest.main()
