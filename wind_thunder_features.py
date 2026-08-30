"""Shared Wind-Thunder Wings acceleration session.

The item is deliberately managed as a per-identity session:

1. When an accelerated command runs, the wings are taken off the market and
   equipped once.
2. The cleanup timer is *planning-aware*: when it fires it checks whether any
   other accelerated command for this identity becomes due within the next
   half hour. If so the session is extended to cover it, so a burst of
   accelerated commands shares one equip/list cycle.
3. Only when nothing else is coming does the cleanup perform
   ``.散念 风雷翅`` followed by ``.上架至万宝阁 风雷翅``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from automation_settings import wind_thunder_identities_for_account

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
WIND_THUNDER_HOLD_SECONDS = 30 * 60
WIND_THUNDER_PLANNING_WINDOW_SECONDS = 30 * 60
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
# 排期状态键 → 对应可加速指令；用于持有窗口规划（“还有指令要做就别收摊”）
WIND_THUNDER_SCHEDULE_KEYS = {
    ".探寻裂缝": "next_rift_search_time",
    ".问道": "next_ask_dao_time",
    ".寻觅灵兽": "next_hunt_time",
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
    account = str(
        getattr(actor, "account_key", "")
        or ("main" if str(getattr(actor, "state_file", "")).endswith("state_main.json") else "")
    ).strip()
    if account not in {"main", "sub", "xiaohao", "waaiging"}:
        return False
    normalized_identity = _text(identity) or "主魂"
    try:
        return normalized_identity in wind_thunder_identities_for_account(account)
    except Exception:
        return False


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


def _next_due_command(actor: Any, identity: str) -> str:
    """Return the accelerated command due within the planning window, if any.

    Reads each command's persisted schedule key. An empty/missing schedule
    value means "not scheduled / unknown"; skip it (conservative) unless the
    schedule is in the past, in which case the command is ready right now and
    keeps the session alive.
    """
    state = _identity_state(actor, identity)
    now = _now()
    for command, key in WIND_THUNDER_SCHEDULE_KEYS.items():
        due = _parse_dt(state.get(key))
        if due is None:
            continue
        if due <= now or (due - now).total_seconds() <= WIND_THUNDER_PLANNING_WINDOW_SECONDS:
            return command
    return ""


async def _send_internal(actor: Any, identity: str, command: str, **kwargs: Any) -> Any:
    depth = int(getattr(actor, "_wind_thunder_internal_depth", 0) or 0)
    setattr(actor, "_wind_thunder_internal_depth", depth + 1)
    try:
        if identity != "主魂" and hasattr(actor, "send_and_wait_feedback_identity"):
            return await actor.send_and_wait_feedback_identity(identity, command, **kwargs)
        return await actor.send_and_wait_feedback(command, **kwargs)
    finally:
        setattr(actor, "_wind_thunder_internal_depth", depth)


def _cleanup_backoff_seconds(response_text: str) -> int:
    """Short backoff after a failed listing; the reply rarely carries a duration."""
    _ = response_text
    return 5 * 60


async def _cleanup(actor: Any, identity: str) -> bool:
    if not wind_thunder_enabled(actor, identity):
        return False
    state = _identity_state(actor, identity)
    if not state.get("wind_thunder_equipped"):
        return True
    # 规划检查：半小时内还有可加速指令待执行 → 续期，不收摊
    upcoming = _next_due_command(actor, identity)
    if upcoming:
        state["wind_thunder_cleanup_due_at"] = (
            _now() + timedelta(seconds=WIND_THUNDER_HOLD_SECONDS)
        ).strftime(TIME_FORMAT)
        state["wind_thunder_last_defer_reason"] = f"upcoming:{upcoming}"
        _save(actor)
        _schedule_cleanup(actor, identity)
        return True
    try:
        san_resp = await _send_internal(
            actor,
            identity,
            ".散念 风雷翅",
            timeout=60,
            max_retries=0,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )
        san_text = str(san_resp or "") if isinstance(san_resp, str) else (
            (getattr(san_resp, "text", "") or "") if san_resp else ""
        )
        if "散去" not in san_text and "散念" not in san_text:
            # 散念未确认成功（可能未装备或响应未匹配）——不盲目继续上架，
            # 短退避后重试整个收尾流程。
            state["wind_thunder_last_cleanup_error"] = "san_nian_unconfirmed"
            state["wind_thunder_cleanup_due_at"] = (
                _now() + timedelta(seconds=_cleanup_backoff_seconds(san_text))
            ).strftime(TIME_FORMAT)
            _save(actor)
            _schedule_cleanup(actor, identity)
            return False
        list_resp = await _send_internal(
            actor,
            identity,
            ".上架至万宝阁 风雷翅",
            timeout=60,
            max_retries=0,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )
        list_text = str(list_resp or "") if isinstance(list_resp, str) else (
            (getattr(list_resp, "text", "") or "") if list_resp else ""
        )
        if "放置失败" in list_text:
            # 游戏侧业务失败（因果牵连）——保持装备态，短退避后重试上架。
            # 绝不重复发送上架指令；当前持有窗口顺延。
            state["wind_thunder_last_cleanup_error"] = "list_failed"
            state["wind_thunder_cleanup_due_at"] = (
                _now() + timedelta(seconds=_cleanup_backoff_seconds(list_text))
            ).strftime(TIME_FORMAT)
            _save(actor)
            _schedule_cleanup(actor, identity)
            return False
        state.update({
            "wind_thunder_equipped": False,
            "wind_thunder_cleanup_due_at": "",
            "wind_thunder_last_cleanup_time": _now().strftime(TIME_FORMAT),
            "wind_thunder_last_cleanup_error": "",
            "wind_thunder_last_defer_reason": "",
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
