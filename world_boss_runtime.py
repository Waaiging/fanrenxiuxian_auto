"""Run a battle clock independently of the Telegram worker's event loop."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import threading
from typing import Any, Awaitable, Callable


async def run_isolated_battle(
    factory: Callable[[], Awaitable[Any]], *, account: str,
) -> Any:
    """Drain cancellation before returning ownership to the account worker.

    The factory must only use battle HTTP/state operations. Telegram client
    requests and actor state updates remain on their owning event loop.
    A thread shares the process heap instead of spawning another Python worker
    on the memory-constrained VPS.
    """
    result: Future = Future()
    cancelled = threading.Event()
    running: dict[str, Any] = {}

    def work() -> None:
        try:
            with asyncio.Runner() as runner:
                running["loop"] = runner.get_loop()

                async def execute():
                    running["task"] = asyncio.current_task()
                    if cancelled.is_set():
                        raise asyncio.CancelledError
                    return await factory()

                value = runner.run(execute())
        except BaseException as exc:
            result.set_exception(exc)
        else:
            result.set_result(value)

    worker = threading.Thread(target=work, name=f"boss-clock-{account}", daemon=True)
    worker.start()
    wrapped = asyncio.wrap_future(result)
    try:
        return await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        cancelled.set()
        loop, task = running.get("loop"), running.get("task")
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker finished and closed its loop concurrently.
        # A checkpoint already being written must finish before another worker
        # can acquire the account lease. Repeated stop requests cannot bypass it.
        while not wrapped.done():
            try:
                await asyncio.shield(wrapped)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if wrapped.done() and not wrapped.cancelled():
            wrapped.exception()
        raise
