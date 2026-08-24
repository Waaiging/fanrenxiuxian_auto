"""Shared Wind-Thunder Wings acceleration session.

The item is deliberately managed as a short-lived per-identity session.  A
second accelerated command within thirty minutes reuses the equipped item;
the cleanup task is moved forward after every command and finally performs
``.散念 风雷翅`` followed by ``.上架至万宝阁 风雷翅``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
WIND_THUNDER_HOLD_SECONDS = 30 * 60
WIND_THUNDER_COOLDOWNS = {
    # Round before converting so 0.70 does not produce an off-by-one second
    # result due to binary floating-point representation.
    ".寻觅灵兽": round(360 * 60 * 0.70),
    ".问道": round(720 * 60 * 0.70),
    ".探寻裂缝": round(720 * 60 * 0.75),
}
WIND_THUNDER_IDENTITIES = {
    ("main", "主魂"),
    ("main", "无咎子"),
    ("sub", "主魂"),
    ("waaiging", "主魂"),
}


def _now() -> datetime:
    return datetime.now()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _identity_state(actor: Any, identity: str) -> dict[str, Any]:
    resolver = getattr(actor, "identity_state_for_timed_command", None)
    if callable(resolver):
        try:
            value = resolver(identity)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    if identity != "主魂":
        getter = getattr(actor, "get_avatar_state", None)
        if callable(getter):
            try:
                value = getter(identity)
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
    value = getattr(actor, "state", None)
    return value if isinstance(value, dict) else {}


def wind_thunder_enabled(actor: Any, identity: str = "主魂") -> bool:
    account = str(getattr(actor, "account_key", "") or "").strip()
    identity = _text(identity) or "主魂"
    config = getattr(actor, "config", {}) or {}
    settings = config.get("wind_thunder") if isinstance(config, dict) else None
    configured = None
    if isinstance(settings, dict):
        configured = settings.get("identities")
    if configured is None and isinstance(config, dict):
        configured = config.get("wind_thunder_identities")
    if isinstance(configured, (list, tuple, set)):
        values = set()
        for item in configured:
            text = _text(item)
            if "|" in text:
                values.add(tuple(part.strip() for part in text.split("|", 1)))
            elif text:
                values.add((account, text))
        return (account, identity) in values
    return (account, identity) in WIND_THUNDER_IDENTITIES


def wind_thunder_target_cooldown(command: str, fallback_seconds: int) -> int:
    root = _text(command).split(" ", 1)[0]
    target = WIND_THUNDER_COOLDOWNS.get(_text(command)) or WIND_THUNDER_COOLDOWNS.get(root)
    return min(int(fallback_seconds or 0), target) if target else int(fallback_seconds or 0)


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _save(actor: Any) -> None:
    saver = getattr(actor, "save_state", None)
    if callable(saver):
        try:
            saver()
        except Exception:
            pass


async def _send_internal(actor: Any, identity: str, command: str, **kwargs: Any) -> Any:
    depth = int(getattr(actor, "_wind_thunder_internal_depth", 0) or 0)
    setattr(actor, "_wind_thunder_internal_depth", depth + 1)
    try:
        if identity != "主魂" and hasattr(actor, "send_and_wait_feedback_identity"):
            return await actor.send_and_wait_feedback_identity(identity, command, **kwargs)
        return await actor.send_and_wait_feedback(command, **kwargs)
    finally:
        setattr(actor, "_wind_thunder_internal_depth", depth)


async def _cleanup(actor: Any, identity: str) -> bool:
    if not wind_thunder_enabled(actor, identity):
        return False
    state = _identity_state(actor, identity)
    if not state.get("wind_thunder_equipped"):
        return True
    try:
        await _send_internal(
            actor,
            identity,
            ".散念 风雷翅",
            timeout=60,
            max_retries=0,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )
        await _send_internal(
            actor,
            identity,
            ".上架至万宝阁 风雷翅",
            timeout=60,
            max_retries=0,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )
        state.update({
            "wind_thunder_equipped": False,
            "wind_thunder_cleanup_due_at": "",
            "wind_thunder_last_cleanup_time": _now().strftime(TIME_FORMAT),
            "wind_thunder_last_cleanup_error": "",
        })
        _save(actor)
        return True
    except Exception as exc:
        state["wind_thunder_last_cleanup_error"] = type(exc).__name__.lower()
        state["wind_thunder_cleanup_due_at"] = (_now() + timedelta(minutes=5)).strftime(TIME_FORMAT)
        _save(actor)
        return False


def _schedule_cleanup(actor: Any, identity: str) -> None:
    tasks = getattr(actor, "_wind_thunder_cleanup_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        setattr(actor, "_wind_thunder_cleanup_tasks", tasks)
    key = _text(identity) or "主魂"
    old = tasks.get(key)
    if old and not old.done():
        old.cancel()

    state = _identity_state(actor, key)
    due = _parse_dt(state.get("wind_thunder_cleanup_due_at"))
    delay = WIND_THUNDER_HOLD_SECONDS
    if due:
        delay = max(0.0, (due - _now()).total_seconds())

    async def runner() -> None:
        try:
            await asyncio.sleep(delay)
            await _cleanup(actor, key)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    try:
        tasks[key] = asyncio.create_task(runner())
    except RuntimeError:
        tasks.pop(key, None)


def recover_wind_thunder_sessions(actor: Any) -> None:
    """Re-arm persisted cleanup timers after a process restart."""
    candidates = {"主魂"}
    avatars = getattr(actor, "avatars", None)
    if isinstance(avatars, (list, tuple, set)):
        candidates.update(str(value).strip() for value in avatars if str(value).strip())
    for identity in sorted(candidates):
        if not wind_thunder_enabled(actor, identity):
            continue
        state = _identity_state(actor, identity)
        due = _parse_dt(state.get("wind_thunder_cleanup_due_at"))
        if not (state.get("wind_thunder_equipped") and due):
            continue
        try:
            if due > _now():
                _schedule_cleanup(actor, identity)
            else:
                task = asyncio.create_task(_cleanup(actor, identity))
                tasks = getattr(actor, "_wind_thunder_cleanup_tasks", None)
                if not isinstance(tasks, dict):
                    tasks = {}
                    setattr(actor, "_wind_thunder_cleanup_tasks", tasks)
                tasks[identity] = task
        except RuntimeError:
            return


async def wind_thunder_send(
    actor: Any,
    identity: str,
    command: str,
    sender: Callable[[], Awaitable[Any]],
) -> Any:
    """Equip, execute, and retain Wind-Thunder Wings for the shared window."""
    identity = _text(identity) or "主魂"
    if not wind_thunder_enabled(actor, identity) or _text(command) not in WIND_THUNDER_COOLDOWNS:
        return await sender()
    if int(getattr(actor, "_wind_thunder_internal_depth", 0) or 0) > 0:
        return await sender()

    lock = getattr(actor, "_wind_thunder_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(actor, "_wind_thunder_lock", lock)
    async with lock:
        state = _identity_state(actor, identity)
        due = _parse_dt(state.get("wind_thunder_cleanup_due_at"))
        now = _now()
        # The item is held for one fixed window.  A second accelerated
        # command inside that window reuses the existing deadline.
        if state.get("wind_thunder_equipped") and due and due > now:
            equipped_at = _parse_dt(state.get("wind_thunder_equipped_at"))
            if equipped_at and 0 <= (now - equipped_at).total_seconds() <= WIND_THUNDER_HOLD_SECONDS:
                result = await sender()
                state = _identity_state(actor, identity)
                persisted_due = _parse_dt(state.get("wind_thunder_cleanup_due_at"))
                if persisted_due:
                    state["wind_thunder_cleanup_due_at"] = persisted_due.strftime(TIME_FORMAT)
                    _save(actor)
                    _schedule_cleanup(actor, identity)
                return result
        if state.get("wind_thunder_equipped") and due and due <= now:
            await _cleanup(actor, identity)
            state = _identity_state(actor, identity)
        if not state.get("wind_thunder_equipped"):
            await _send_internal(actor, identity, ".从万宝阁取下 风雷翅", timeout=60, max_retries=0, force_identity_check=True)
            await _send_internal(actor, identity, ".装备 风雷翅", timeout=60, max_retries=0, force_identity_check=True)
            state["wind_thunder_equipped"] = True
            state["wind_thunder_equipped_at"] = now.strftime(TIME_FORMAT)
        state["wind_thunder_last_command"] = _text(command)
        state["wind_thunder_last_command_time"] = now.strftime(TIME_FORMAT)
        state["wind_thunder_cleanup_due_at"] = (now + timedelta(seconds=WIND_THUNDER_HOLD_SECONDS)).strftime(TIME_FORMAT)
        _save(actor)
        _schedule_cleanup(actor, identity)
        try:
            result = await sender()
        except Exception:
            _schedule_cleanup(actor, identity)
            raise
        state = _identity_state(actor, identity)
        state["wind_thunder_cleanup_due_at"] = (_now() + timedelta(seconds=WIND_THUNDER_HOLD_SECONDS)).strftime(TIME_FORMAT)
        _save(actor)
        _schedule_cleanup(actor, identity)
        return result
