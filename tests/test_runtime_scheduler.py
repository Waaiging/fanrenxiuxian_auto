import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from runtime_scheduler import create_scheduler_task


class RuntimeSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_registration_reuses_live_task_without_calling_factory(self):
        actor = SimpleNamespace(is_running=True)
        logger = Mock()
        ready = asyncio.Event()
        factory = Mock(side_effect=ready.wait)
        first = create_scheduler_task(actor, "cultivation", factory, logger)
        second = create_scheduler_task(actor, "cultivation", factory, logger)
        self.assertIs(first, second)
        factory.assert_called_once_with()
        actor.is_running = False
        ready.set()
        await first
        await asyncio.sleep(0)
        logger.critical.assert_not_called()

    async def test_failure_is_reported_once_and_task_can_be_replaced(self):
        actor = SimpleNamespace(is_running=True)
        logger = Mock()

        async def fail():
            raise ValueError("broken loop")

        first = create_scheduler_task(actor, "cultivation", fail, logger)
        with self.assertRaisesRegex(ValueError, "broken loop"):
            await first
        await asyncio.sleep(0)
        logger.critical.assert_called_once()
        actor.is_running = False
        second = create_scheduler_task(actor, "cultivation", lambda: asyncio.sleep(0), logger)
        self.assertIsNot(first, second)
        await second

    async def test_cancelled_task_does_not_report_worker_failure(self):
        actor = SimpleNamespace(is_running=True)
        logger = Mock()
        task = create_scheduler_task(actor, "cultivation", lambda: asyncio.sleep(60), logger)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        logger.critical.assert_not_called()


if __name__ == "__main__":
    unittest.main()
