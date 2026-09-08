"""Shared registration and failure reporting for account background tasks."""
import asyncio


def create_scheduler_task(actor, name, coro_factory, logger):
    """Keep one live task per name and always consume its completion result."""
    registry = getattr(actor, "_scheduler_task_registry", None)
    if not isinstance(registry, dict):
        registry = {}
        actor._scheduler_task_registry = registry
    existing = registry.get(name)
    if existing is not None and not existing.done():
        return existing

    task = asyncio.create_task(coro_factory(), name=name)
    registry[name] = task

    def on_done(done_task):
        if done_task.cancelled():
            return
        exc = done_task.exception()
        if not getattr(actor, "is_running", True):
            return
        if exc is not None:
            logger.critical(
                "Scheduler task [%s] exited with exception: %s", name, exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            logger.critical("Scheduler task [%s] exited unexpectedly without exception.", name)

    task.add_done_callback(on_done)
    return task
