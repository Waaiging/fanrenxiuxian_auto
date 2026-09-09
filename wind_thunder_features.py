"""Shared Wind-Thunder Wings acceleration session.

The item is deliberately managed as a per-identity session:

1. When an accelerated command runs, the wings are taken off the market and
   equipped once.
2. After the command finishes the cleanup is *re-assessed* shortly (the
   caller persists the next schedule keys right after ``wind_thunder_send``
   returns). The re-assessment checks whether another accelerated command
   for this identity becomes due within the planning window (half an hour).
   If so the session is held until that command has actually executed (its
   scheduled time plus an execution buffer); the completion path then
   re-assesses again.
3. Only when nothing else is coming does the cleanup perform
   ``.散念 风雷翅`` followed by ``.上架至万宝阁 风雷翅``.
4. Disabling an identity stops this automation, including persisted cleanup
   and listing retries. Observed equipment state is retained for manual use.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from automation_settings import load_automation_settings, wind_thunder_identities_for_account

_INTERNAL_ACTORS = ContextVar("wind_thunder_internal_actors", default=frozenset())
log = logging.getLogger("WindThunder")

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
WIND_THUNDER_HOLD_SECONDS = 30 * 60
WIND_THUNDER_PLANNING_WINDOW_SECONDS = 30 * 60
# 指令完成后到重新评估的间隔：等调用方把新排期写入 state 再判断，
# 而不是固定持有整个窗口。
WIND_THUNDER_REASSESS_SECONDS = 90
# 待执行指令的调度时间之外再留出的执行缓冲（循环到期到实际执行需要时间）。
WIND_THUNDER_EXEC_BUFFER_SECONDS = 10 * 60
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
        settings = load_automation_settings()
        return bool((settings.get("wind_thunder") or {}).get("enabled")) and (
            normalized_identity in wind_thunder_identities_for_account(account, settings=settings)
        )
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


def _pause_disabled_cleanup(actor: Any, identity: str) -> bool:
    """Honor the current opt-in without claiming the item has been stored."""
    if wind_thunder_enabled(actor, identity):
        return False
    tasks = getattr(actor, "_wind_thunder_cleanup_tasks", {})
    task = tasks.get(identity) if isinstance(tasks, dict) else None
    if task is not None:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        lock = getattr(actor, "_wind_thunder_lock", None)
        # Let an in-flight operation record its reply. Every subsequent
        # request rechecks the switch; sleeping timers can be cancelled now.
        if task.done() or task is current or not (lock and lock.locked()):
            tasks.pop(identity, None)
            if not task.done() and task is not current:
                task.cancel()
    state = _identity_state(actor, identity)
    if state.get("wind_thunder_equipped") or state.get("wind_thunder_list_pending") or state.get("wind_thunder_cleanup_due_at"):
        if state.get("wind_thunder_cleanup_due_at") or state.get("wind_thunder_last_defer_reason") != "disabled":
            state["wind_thunder_cleanup_due_at"] = ""
            state["wind_thunder_last_defer_reason"] = "disabled"
            _save(actor)
    return True


def _next_due_command(actor: Any, identity: str) -> tuple[str, datetime]:
    """Return the accelerated command due within the planning window, if any.

    Reads each command's persisted schedule key. An empty/missing schedule
    value means "not scheduled / unknown"; skip it (conservative).  A past
    value older than the planning window is a dead schedule (the owning loop
    is gone) — treating it as "ready right now" would defer the cleanup
    forever, so it is ignored.  Returns ``(command, due_at)``; ``("", now)``
    when nothing is coming.
    """
    state = _identity_state(actor, identity)
    now = _now()
    best: tuple[str, datetime] | None = None
    for command, key in WIND_THUNDER_SCHEDULE_KEYS.items():
        due = _parse_dt(state.get(key))
        if due is None:
            continue
        age = (now - due).total_seconds()
        if 0 <= age <= WIND_THUNDER_PLANNING_WINDOW_SECONDS:
            # 过期但在窗口内：循环刚到期、即将执行，持有。
            if best is None or due < best[1]:
                best = (command, due)
        elif age < 0 and -age <= WIND_THUNDER_PLANNING_WINDOW_SECONDS:
            # 未来且在窗口内：即将到来，持有到它执行完毕。
            if best is None or due < best[1]:
                best = (command, due)
    if best is None:
        return "", now
    return best


async def _send_internal(actor: Any, identity: str, command: str, **kwargs: Any) -> Any:
    token = _INTERNAL_ACTORS.set(_INTERNAL_ACTORS.get() | {id(actor)})
    try:
        if identity != "主魂" and hasattr(actor, "send_and_wait_feedback_identity"):
            return await actor.send_and_wait_feedback_identity(identity, command, **kwargs)
        return await actor.send_and_wait_feedback(command, **kwargs)
    finally:
        _INTERNAL_ACTORS.reset(token)


def _session_lock(actor: Any) -> asyncio.Lock:
    lock = getattr(actor, "_wind_thunder_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(actor, "_wind_thunder_lock", lock)
    return lock


def _cleanup_backoff_seconds(response_text: str) -> int:
    """Short backoff after a failed listing; the reply rarely carries a duration."""
    _ = response_text
    return 5 * 60


def _wind_thunder_notify(actor: Any, message: str) -> None:
    """Best-effort private alert to the owner for hunt-risk exposure."""
    client = getattr(actor, "client", None)
    if client is None:
        return

    async def _send() -> None:
        try:
            await client.send_message(8219248252, f"[风雷翅] {message}")
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        pass


def _listing_confirmed(text: str) -> bool:
    return (
        "风雷翅" in text
        and any(marker in text for marker in (
            "放置在万宝阁", "上架成功", "已上架", "已经上架", "已在万宝阁", "交易挂单",
        ))
        and not any(marker in text for marker in ("失败", "未能", "无法"))
    )


def sync_wind_thunder_manual_response(actor: Any, identity: str, command: str, text: str) -> bool:
    """Sync state from a *manual* wind-thunder command reply (duck-typed hook).

    Called by log_utils for replies to manually sent ``.装备 风雷翅`` /
    ``.散念 风雷翅`` / ``.从万宝阁取下 风雷翅`` / ``.上架至万宝阁 风雷翅``.
    Keeps the persisted ``wind_thunder_equipped`` flag in step with reality so
    manual intervention never leaves a stale flag behind (the root cause of
    the 2026-09-01 main-account divergence).
    """
    # Manual replies are observations, even when automatic management is off.
    clean = _text(text)
    state = _identity_state(actor, identity)
    changed = False
    now = _now()

    if command == ".装备 风雷翅":
        if "祭出" in clean and "风雷翅" in clean:
            if not state.get("wind_thunder_equipped"):
                state["wind_thunder_equipped"] = True
                state["wind_thunder_equipped_at"] = now.strftime(TIME_FORMAT)
                state["wind_thunder_list_pending"] = False
                changed = True
        elif "没有" in clean and "风雷翅" in clean:
            # 手动装备失败（不在储物袋/不在万宝阁）——清除陈旧标记。
            if state.get("wind_thunder_equipped"):
                state["wind_thunder_equipped"] = False
                state["wind_thunder_list_pending"] = False
                state["wind_thunder_cleanup_due_at"] = ""
                changed = True
    elif command == ".散念 风雷翅":
        if "散去" in clean or "没有" in clean:
            # 成功散念，或翅膀本就不在身上——两种情况都不再装备。
            if state.get("wind_thunder_equipped"):
                state["wind_thunder_equipped"] = False
                changed = True
            # 散念后翅膀在储物袋等待上架（自动收尾稍后接管；若用户手动
            # 散念则由随后的手动上架回复清 list_pending）。
            if not state.get("wind_thunder_list_pending"):
                state["wind_thunder_list_pending"] = True
                changed = True
    elif command == ".从万宝阁取下 风雷翅":
        if "取下" in clean or "取回" in clean:
            # 已从万宝阁取下、进了储物袋，但尚未装备。
            state["wind_thunder_list_pending"] = False
            state["wind_thunder_cleanup_due_at"] = ""
            if state.get("wind_thunder_equipped"):
                state["wind_thunder_equipped"] = False
            changed = True
    elif command == ".上架至万宝阁 风雷翅":
        if _listing_confirmed(clean):
            if (
                state.get("wind_thunder_equipped")
                or state.get("wind_thunder_list_pending")
            ):
                state["wind_thunder_equipped"] = False
                state["wind_thunder_list_pending"] = False
                state["wind_thunder_cleanup_due_at"] = ""
                state["wind_thunder_last_cleanup_time"] = now.strftime(TIME_FORMAT)
                state["wind_thunder_last_cleanup_error"] = ""
                state["wind_thunder_last_defer_reason"] = ""
                state["wind_thunder_list_fail_count"] = 0
                changed = True
        elif "放置失败" in clean:
            if not state.get("wind_thunder_list_pending"):
                state["wind_thunder_list_pending"] = True
                changed = True

    if changed:
        _save(actor)
        _pause_disabled_cleanup(actor, identity)
    return changed


def _listing_backoff_seconds(state: dict[str, Any]) -> int:
    """Escalating backoff for repeated listing failures: 5m → 15m → 30m."""
    try:
        fails = int(state.get("wind_thunder_list_fail_count") or 0)
    except (TypeError, ValueError):
        fails = 0
    if fails >= 3:
        return 30 * 60
    if fails == 2:
        return 15 * 60
    return 5 * 60


async def _retry_listing(actor: Any, identity: str, state: dict[str, Any]) -> bool:
    """Retry the market listing for an already-san-nian'd wing (exposure state)."""
    if _pause_disabled_cleanup(actor, identity):
        return False
    try:
        list_resp = await _send_internal(
            actor,
            identity,
            ".上架至万宝阁 风雷翅",
            timeout=60,
            max_retries=0,
            retry_on_timeout=False,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )
        list_text = str(list_resp or "") if isinstance(list_resp, str) else (
            (getattr(list_resp, "text", "") or "") if list_resp else ""
        )
        if not _listing_confirmed(list_text):
            if _pause_disabled_cleanup(actor, identity):
                return False
            try:
                fails = int(state.get("wind_thunder_list_fail_count") or 0) + 1
            except (TypeError, ValueError):
                fails = 1
            state["wind_thunder_list_fail_count"] = fails
            state["wind_thunder_last_cleanup_error"] = (
                "list_failed" if "放置失败" in list_text else "list_unconfirmed"
            )
            state["wind_thunder_list_pending"] = True
            state["wind_thunder_cleanup_due_at"] = (
                _now() + timedelta(seconds=_listing_backoff_seconds(state))
            ).strftime(TIME_FORMAT)
            _save(actor)
            _schedule_cleanup(actor, identity)
            _wind_thunder_notify(
                actor,
                f"[{identity}] 风雷翅上架未确认（第{fails}次），已散念、上架状态待确认，"
                f"处于追杀暴露状态，{_listing_backoff_seconds(state) // 60}分钟后重试。",
            )
            return False
        state.update({
            "wind_thunder_equipped": False,
            "wind_thunder_cleanup_due_at": "",
            "wind_thunder_last_cleanup_time": _now().strftime(TIME_FORMAT),
            "wind_thunder_last_cleanup_error": "",
            "wind_thunder_last_cleanup_detail": "",
            "wind_thunder_last_defer_reason": "",
            "wind_thunder_list_pending": False,
            "wind_thunder_list_fail_count": 0,
        })
        _save(actor)
        return True
    except Exception as exc:
        if _pause_disabled_cleanup(actor, identity):
            return False
        _record_cleanup_exception(actor, identity, state, "上架", exc)
        state["wind_thunder_last_cleanup_error"] = type(exc).__name__.lower()
        state["wind_thunder_list_pending"] = True
        state["wind_thunder_cleanup_due_at"] = (_now() + timedelta(minutes=5)).strftime(TIME_FORMAT)
        _save(actor)
        _schedule_cleanup(actor, identity)
        return False


def _record_cleanup_exception(actor: Any, identity: str, state: dict[str, Any], phase: str, exc: Exception) -> None:
    detail = f"{phase}: {type(exc).__name__}: {exc}"[:500]
    first_occurrence = state.get("wind_thunder_last_cleanup_detail") != detail
    state["wind_thunder_last_cleanup_detail"] = detail
    log.error("Wind-Thunder cleanup [%s] %s failed: %s", identity, phase, exc, exc_info=True)
    if first_occurrence:
        _wind_thunder_notify(actor, f"[{identity}] 风雷翅{phase}异常，尚未确认入阁，仍有追杀风险。5 分钟后重试。{detail}")


async def _cleanup(actor: Any, identity: str) -> bool:
    # Timers and command execution must share the same lock: never unequip
    # while an accelerated command is still waiting for its response.
    async with _session_lock(actor):
        return await _cleanup_locked(actor, identity)


async def _cleanup_locked(actor: Any, identity: str) -> bool:
    if _pause_disabled_cleanup(actor, identity):
        return False
    state = _identity_state(actor, identity)
    if state.get("wind_thunder_list_pending"):
        # 散念已成功、上架失败的补挂路径：跳过散念，直接重试上架。
        return await _retry_listing(actor, identity, state)
    if not state.get("wind_thunder_equipped"):
        return True
    # 规划检查：半小时内还有可加速指令待执行 → 持有到该指令执行完毕
    # （调度时间 + 执行缓冲），其完成路径会再次触发重新评估。
    upcoming, upcoming_due = _next_due_command(actor, identity)
    if upcoming:
        hold_until = max(
            upcoming_due + timedelta(seconds=WIND_THUNDER_EXEC_BUFFER_SECONDS),
            _now() + timedelta(seconds=WIND_THUNDER_REASSESS_SECONDS),
        )
        state["wind_thunder_cleanup_due_at"] = hold_until.strftime(TIME_FORMAT)
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
            retry_on_timeout=False,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )
        san_text = str(san_resp or "") if isinstance(san_resp, str) else (
            (getattr(san_resp, "text", "") or "") if san_resp else ""
        )
        # "储物袋中没有【风雷翅】" = 翅膀根本不在身上（取下/装备实际未生效，
        # state 与游戏脱节）。散念的目标已天然达成，直接走上架收尾自愈；
        # 上架结果仍需明确确认，不能用空回复推断已在万宝阁。
        if "没有" in san_text and "风雷翅" in san_text:
            state["wind_thunder_last_cleanup_error"] = ""
            state["wind_thunder_equipped"] = False
            state["wind_thunder_list_pending"] = True
            _save(actor)
            return await _retry_listing(actor, identity, state)
        if (
            not any(marker in san_text for marker in ("散去", "散念成功", "已散念"))
            or any(marker in san_text for marker in ("失败", "未能", "无法"))
        ):
            if _pause_disabled_cleanup(actor, identity):
                return False
            # 散念未确认成功（可能未装备或响应未匹配）——不盲目继续上架，
            # 短退避后重试整个收尾流程。
            state["wind_thunder_last_cleanup_error"] = "san_nian_unconfirmed"
            state["wind_thunder_cleanup_due_at"] = (
                _now() + timedelta(seconds=_cleanup_backoff_seconds(san_text))
            ).strftime(TIME_FORMAT)
            _save(actor)
            _schedule_cleanup(actor, identity)
            return False
        # 散念已确认成功，上架（含失败退避与通知）统一走 _retry_listing。
        state["wind_thunder_equipped"] = False
        state["wind_thunder_list_pending"] = True
        _save(actor)
        return await _retry_listing(actor, identity, state)
    except Exception as exc:
        if _pause_disabled_cleanup(actor, identity):
            return False
        _record_cleanup_exception(actor, identity, state, "散念", exc)
        state["wind_thunder_last_cleanup_error"] = type(exc).__name__.lower()
        state["wind_thunder_cleanup_due_at"] = (_now() + timedelta(minutes=5)).strftime(TIME_FORMAT)
        _save(actor)
        _schedule_cleanup(actor, identity)
        return False


def _schedule_cleanup(actor: Any, identity: str) -> None:
    identity = _text(identity) or "主魂"
    if _pause_disabled_cleanup(actor, identity):
        return
    tasks = getattr(actor, "_wind_thunder_cleanup_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        setattr(actor, "_wind_thunder_cleanup_tasks", tasks)
    key = _text(identity) or "主魂"
    old = tasks.get(key)
    if old and not old.done() and old is not asyncio.current_task():
        old.cancel()

    state = _identity_state(actor, key)
    if state.get("wind_thunder_last_defer_reason") == "disabled":
        state["wind_thunder_last_defer_reason"] = ""
        if not _parse_dt(state.get("wind_thunder_cleanup_due_at")):
            state["wind_thunder_cleanup_due_at"] = _now().strftime(TIME_FORMAT)
        _save(actor)
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
            log.exception("Wind-Thunder cleanup timer [%s] failed.", key)
            return

    try:
        tasks[key] = asyncio.create_task(runner())
    except RuntimeError:
        tasks.pop(key, None)


def recover_wind_thunder_sessions(actor: Any) -> None:
    """Re-arm persisted cleanup only for identities that are still enabled."""
    candidates = {"主魂"}
    avatars = getattr(actor, "avatars", None)
    if isinstance(avatars, (list, tuple, set)):
        candidates.update(str(value).strip() for value in avatars if str(value).strip())
    for identity in sorted(candidates):
        if _pause_disabled_cleanup(actor, identity):
            continue
        state = _identity_state(actor, identity)
        due = _parse_dt(state.get("wind_thunder_cleanup_due_at"))
        if not (state.get("wind_thunder_equipped") or state.get("wind_thunder_list_pending")):
            continue
        if due is None or state.get("wind_thunder_last_cleanup_error") == "typeerror":
            due = _now()
            state["wind_thunder_cleanup_due_at"] = due.strftime(TIME_FORMAT)
            _save(actor)
        _schedule_cleanup(actor, identity)


async def wind_thunder_send(
    actor: Any,
    identity: str,
    command: str,
    sender: Callable[[], Awaitable[Any]],
) -> Any:
    """Equip, execute, and retain Wind-Thunder Wings for the shared window."""
    identity = _text(identity) or "主魂"
    if id(actor) in _INTERNAL_ACTORS.get():
        return await sender()
    if _pause_disabled_cleanup(actor, identity) or _text(command) not in WIND_THUNDER_COOLDOWNS:
        return await sender()

    async with _session_lock(actor):
        if _pause_disabled_cleanup(actor, identity):
            return await sender()
        state = _identity_state(actor, identity)
        now = _now()
        # 脱节防护：equipped 标记的装备时间超出持有窗口（或缺失）= state
        # 已不反映游戏实况（手动散念/上架、进程漂移、散念退避中）。
        # 清除标记走完整装备路径自愈，绝不在陈旧标记上裸发指令。
        if state.get("wind_thunder_equipped"):
            equipped_at = _parse_dt(state.get("wind_thunder_equipped_at"))
            if not equipped_at or not (
                0 <= (now - equipped_at).total_seconds() <= WIND_THUNDER_HOLD_SECONDS
            ):
                state["wind_thunder_equipped"] = False
                state["wind_thunder_list_pending"] = False
                state["wind_thunder_cleanup_due_at"] = ""
                _save(actor)
        # The item is held for one fixed window.  A second accelerated
        # command inside that window reuses the existing deadline.
        due = _parse_dt(state.get("wind_thunder_cleanup_due_at"))
        if state.get("wind_thunder_equipped") and due and due > now:
            equipped_at = _parse_dt(state.get("wind_thunder_equipped_at"))
            if equipped_at and 0 <= (now - equipped_at).total_seconds() <= WIND_THUNDER_HOLD_SECONDS:
                result = await sender()
                # 第二条指令执行完毕 → 同样短延迟重新评估持有必要性。
                state = _identity_state(actor, identity)
                state["wind_thunder_cleanup_due_at"] = (
                    _now() + timedelta(seconds=WIND_THUNDER_REASSESS_SECONDS)
                ).strftime(TIME_FORMAT)
                _save(actor)
                _schedule_cleanup(actor, identity)
                return result
        if state.get("wind_thunder_equipped") and due and due <= now:
            await _cleanup_locked(actor, identity)
            state = _identity_state(actor, identity)
        if _pause_disabled_cleanup(actor, identity):
            return await sender()
        if state.get("wind_thunder_list_pending"):
            # 上架失败的翅膀还滞留储物袋（追杀暴露态）——不重新装备，
            # 优先把上架补上；本次指令按普通冷却执行。
            # Re-enabling an identity can leave a paused pending listing
            # without a timer. Resume it without re-equipping or bypassing
            # an existing retry deadline.
            tasks = getattr(actor, "_wind_thunder_cleanup_tasks", {})
            task = tasks.get(identity) if isinstance(tasks, dict) else None
            if task is None or task.done():
                _schedule_cleanup(actor, identity)
            _wind_thunder_notify(
                actor,
                f"[{identity}] 风雷翅仍在待上架状态（此前上架失败），本次 {command} 不启用加速，"
                f"优先补上架以脱离追杀暴露。",
            )
            return await sender()
        if not state.get("wind_thunder_equipped"):
            await _send_internal(actor, identity, ".从万宝阁取下 风雷翅", timeout=60, max_retries=0, force_identity_check=True)
            if _pause_disabled_cleanup(actor, identity):
                return await sender()
            equip_resp = await _send_internal(actor, identity, ".装备 风雷翅", timeout=60, max_retries=0, force_identity_check=True)
            equip_text = str(equip_resp or "") if isinstance(equip_resp, str) else (
                (getattr(equip_resp, "text", "") or "") if equip_resp else ""
            )
            if "祭出" not in equip_text:
                if _pause_disabled_cleanup(actor, identity):
                    return await sender()
                # 装备未确认成功（Bot 不可用被跳过发送、翅膀不在万宝阁等）。
                # 绝不标记 equipped——否则 state 与游戏脱节，收尾定时器会
                # 每 5 分钟重发 .散念 撞"储物袋中没有"死循环。本次按普通冷却执行。
                state["wind_thunder_last_cleanup_error"] = "equip_unconfirmed"
                state["wind_thunder_equipped"] = False
                state["wind_thunder_cleanup_due_at"] = ""
                state["wind_thunder_list_pending"] = False
                _save(actor)
                _wind_thunder_notify(
                    actor,
                    f"[{identity}] 风雷翅装备未确认（{(equip_text or '无回复')[:50]}），"
                    f"本次指令按普通冷却执行，不启用加速。",
                )
                return await sender()
            state["wind_thunder_equipped"] = True
            state["wind_thunder_equipped_at"] = now.strftime(TIME_FORMAT)
        if _pause_disabled_cleanup(actor, identity):
            return await sender()
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
        # 执行完毕后短延迟重新评估：调用方此刻才把新排期写进 state，
        # 90 秒后若半小时内无待执行指令则立即收摊（散念+上架），
        # 有则持有到该指令执行完毕。
        state["wind_thunder_cleanup_due_at"] = (
            _now() + timedelta(seconds=WIND_THUNDER_REASSESS_SECONDS)
        ).strftime(TIME_FORMAT)
        _save(actor)
        _schedule_cleanup(actor, identity)
        return result


def _register_manual_sync_hook() -> None:
    """Wire log_utils' manual-reply state sync to the wind-thunder hook.

    log_utils cannot import this module (would create a cycle through the
    cultivator scripts), so it exposes a settable duck-typed hook instead.
    Importing log_utils from here is safe: it depends only on stdlib.
    """
    try:
        import log_utils
    except Exception:
        return
    register = getattr(log_utils, "set_wind_thunder_manual_sync_hook", None)
    if callable(register):
        register(sync_wind_thunder_manual_response)


_register_manual_sync_hook()
