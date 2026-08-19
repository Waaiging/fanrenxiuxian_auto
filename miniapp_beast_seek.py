#!/usr/bin/env python3
"""Mini App spirit-beast seeking for Wanling main souls.

The worker seeks once every configured interval and only ever releases the
beast whose id appeared during that specific seek.  Existing roster entries
are never candidates for automatic release.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError, miniapp_circuit_wait_seconds
from miniapp_beast_contract import main_soul_is_wanling


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_INTERVAL_SECONDS = 6 * 3600
DEFAULT_RETRY_SECONDS = 5 * 60
DEFAULT_TARGET_BEAST = "双瞳鼠"
COOLDOWN_RETRY_GRACE_SECONDS = 2
MIN_COOLDOWN_RETRY_SECONDS = 2


def _now_text(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime(TIME_FORMAT)


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _seconds_until(value: Any, now: datetime | None = None) -> int:
    target = _parse_time(value)
    return max(0, int((target - (now or datetime.now())).total_seconds())) if target else 0


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()


def _seek_cooldown_seconds(text: Any) -> int | None:
    clean = str(text or "").replace("**", "").replace("`", "")
    match = re.search(r"请在\s*([^后\n]+?)\s*后再来", clean)
    if not match:
        return None
    duration = match.group(1)
    total = 0
    matched = False
    for pattern, factor in (
        (r"(\d+)\s*天", 86400),
        (r"(\d+)\s*(?:小时|时)", 3600),
        (r"(\d+)\s*(?:分钟|分)", 60),
        (r"(\d+)\s*秒", 1),
    ):
        for value in re.findall(pattern, duration):
            total += int(value) * factor
            matched = True
    return total if matched else None


def _beast_id(beast: Any) -> int:
    if not isinstance(beast, dict):
        return 0
    try:
        return max(0, int(beast.get("id") or beast.get("beastId") or 0))
    except (TypeError, ValueError):
        return 0


def _beast_name(beast: Any) -> str:
    if not isinstance(beast, dict):
        return ""
    return str(beast.get("full_name") or beast.get("name") or "").strip()


def _beast_type(beast: Any) -> str:
    if not isinstance(beast, dict):
        return ""
    value = str(
        beast.get("beast_type")
        or beast.get("beastType")
        or beast.get("species")
        or ""
    ).strip()
    return re.sub(r"^[一二三四五六七八九十\d]+阶", "", value).strip()


def _roster_ids(beasts: Any) -> set[int]:
    return {
        beast_id
        for beast_id in (_beast_id(item) for item in (beasts or []))
        if beast_id > 0
    }


class MiniAppBeastSeekWorker:
    """Seek one beast every six hours and keep only the configured species."""

    def __init__(
        self,
        actor: Any,
        transport: Any,
        account: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger or logging.getLogger(f"miniapp_beast_seek.{self.account}")
        settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        entry_url = str(settings.get("entry_url") or "").strip()
        self.enabled = (
            bool(entry_url)
            and bool(settings.get("enabled", True))
            and bool(settings.get("beast_seek_enabled", True))
            and main_soul_is_wanling(actor)
        )
        self.interval_seconds = max(
            5 * 60,
            int(settings.get("beast_seek_interval_seconds") or DEFAULT_INTERVAL_SECONDS),
        )
        self.retry_seconds = max(
            60,
            int(settings.get("beast_seek_retry_seconds") or DEFAULT_RETRY_SECONDS),
        )
        self.target_beast = str(
            settings.get("beast_seek_target") or DEFAULT_TARGET_BEAST
        ).strip() or DEFAULT_TARGET_BEAST

    @property
    def state(self) -> dict[str, Any]:
        state = getattr(self.actor, "state", None)
        if not isinstance(state, dict):
            raise RuntimeError("actor_state_missing")
        return state

    def _save(self) -> None:
        self.actor.save_state()

    def _record_enabled_state(self) -> None:
        self.state.update({
            "beast_seek_miniapp_enabled": bool(self.enabled),
            "beast_seek_miniapp_account": self.account,
            "beast_seek_miniapp_interval_seconds": self.interval_seconds,
            "beast_seek_miniapp_target": self.target_beast,
            "beast_seek_miniapp_transport": "miniapp_http_only",
        })
        if self.enabled:
            # Retire the old wind-sparrow stop condition.  Dashboard pause
            # remains independent and can still suspend this worker.
            self.state["beast_hunt_stopped"] = False
            self.state["beast_hunt_stopped_reason"] = ""

    def _dashboard_paused(self) -> bool:
        checker = getattr(self.actor, "dashboard_command_paused", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(".寻觅灵兽", "主魂"))
        except Exception:
            return False

    def _update_cache(self, beasts: list[dict[str, Any]]) -> None:
        self.state.update({
            "beasts_cache": list(beasts),
            "beast_roster_updated_at": _now_text(),
            "beast_roster_last_source": "miniapp",
            "last_beast_roster_query_result": "parsed",
            "beast_miniapp_last_sync_time": _now_text(),
            "beast_miniapp_last_error": "",
        })
        updater = getattr(self.actor, "update_main_best_beast", None)
        if not callable(updater):
            updater = getattr(self.actor, "update_best_beast_tracking", None)
        if callable(updater):
            updater()

    def _schedule_wake(self, target: datetime) -> None:
        self.state["beast_seek_miniapp_next_time"] = _now_text(target)

    def _mark_seek_attempt(self, attempted_at: datetime) -> datetime:
        now = datetime.now()
        next_time = attempted_at + timedelta(seconds=self.interval_seconds)
        if next_time <= now:
            next_time = now + timedelta(seconds=self.interval_seconds)
        self.state.update({
            "last_hunt_time": _now_text(attempted_at),
            "next_hunt_time": _now_text(next_time),
            "beast_seek_miniapp_seek_next_time": _now_text(next_time),
            "beast_seek_miniapp_next_time": _now_text(next_time),
        })
        return next_time

    def _clear_inflight(self) -> None:
        self.state["beast_seek_miniapp_inflight_started_at"] = ""
        self.state["beast_seek_miniapp_inflight_existing_ids"] = []

    def _clear_pending_release(self) -> None:
        self.state["beast_seek_miniapp_pending_release_id"] = 0
        self.state["beast_seek_miniapp_pending_release_name"] = ""
        self.state["beast_seek_miniapp_pending_release_type"] = ""

    def _pending_release_id(self) -> int:
        try:
            return max(0, int(self.state.get("beast_seek_miniapp_pending_release_id") or 0))
        except (TypeError, ValueError):
            return 0

    def _seek_due(self, now: datetime) -> bool:
        target = (
            self.state.get("beast_seek_miniapp_seek_next_time")
            or self.state.get("next_hunt_time")
        )
        parsed = _parse_time(target)
        return parsed is None or parsed <= now

    def _capacity(self, snapshot: dict[str, Any], beasts: list[dict[str, Any]]) -> tuple[int, int]:
        raw = snapshot.get("raw") if isinstance(snapshot, dict) else {}
        raw = raw if isinstance(raw, dict) else {}
        capacity = raw.get("capacity") if isinstance(raw.get("capacity"), dict) else {}
        try:
            limit = max(1, int(capacity.get("limit") or 10))
        except (TypeError, ValueError):
            limit = 10
        try:
            remaining = int(capacity.get("remaining"))
        except (TypeError, ValueError):
            remaining = max(0, limit - len(beasts))
        return limit, max(0, remaining)

    async def _release_pending(self, current: list[dict[str, Any]]) -> bool:
        pending_id = self._pending_release_id()
        if pending_id <= 0:
            return True
        pending = next((item for item in current if _beast_id(item) == pending_id), None)
        if pending is None:
            self._clear_pending_release()
            self.state["beast_seek_miniapp_last_release_result"] = "待放生的新灵兽已不在兽栏"
            self._save()
            return True

        beast_type = _beast_type(pending)
        name = _beast_name(pending) or str(pending_id)
        if beast_type == self.target_beast:
            # Safety invariant: a target beast is never released, even if a
            # stale state file somehow marks its id as pending.
            self._clear_pending_release()
            self.state["beast_seek_miniapp_last_release_result"] = (
                f"安全拦截：ID {pending_id} 为目标灵兽【{name}】，未放生"
            )
            self._save()
            self.log.critical(
                "Mini App beast seek release safety guard kept target [%s] id=%s",
                name,
                pending_id,
            )
            return True

        released = await self.transport.spirit_beast_release(
            "主魂",
            pending_id,
            beast_name=name,
        )
        beasts = list((released or {}).get("beasts") or [])
        if any(_beast_id(item) == pending_id for item in beasts):
            refreshed = await self.transport.spirit_beast_snapshot("主魂", log_operation=False)
            beasts = list((refreshed or {}).get("beasts") or [])
        if any(_beast_id(item) == pending_id for item in beasts):
            raise MiniAppBeastError("beast_release_not_confirmed")

        self._update_cache(beasts)
        self._clear_pending_release()
        now = datetime.now()
        result = f"本次新寻得【{name}】（{beast_type or '未知种类'}），非目标，已放生"
        self.state.update({
            "last_release_beast_time": _now_text(now),
            "beast_seek_miniapp_last_release_time": _now_text(now),
            "beast_seek_miniapp_last_release_id": pending_id,
            "beast_seek_miniapp_last_release_name": name,
            "beast_seek_miniapp_last_release_type": beast_type,
            "beast_seek_miniapp_last_release_result": result,
            "beast_seek_miniapp_last_result": result,
            "last_hunt_result": result,
            "beast_seek_miniapp_last_error": "",
        })
        self._save()
        self.log.info(
            "Mini App beast seek released only newly found beast: id=%s name=%s type=%s",
            pending_id,
            name,
            beast_type,
        )
        return True

    async def _process_new_beast(
        self,
        beast: dict[str, Any],
        roster: list[dict[str, Any]],
        attempted_at: datetime,
    ) -> bool:
        beast_id = _beast_id(beast)
        name = _beast_name(beast) or str(beast_id)
        beast_type = _beast_type(beast)
        self._mark_seek_attempt(attempted_at)
        self._clear_inflight()
        self._update_cache(roster)
        self.state.update({
            "beast_seek_miniapp_last_found_id": beast_id,
            "beast_seek_miniapp_last_found_name": name,
            "beast_seek_miniapp_last_found_type": beast_type,
            "beast_seek_miniapp_last_found_time": _now_text(),
            "beast_seek_miniapp_last_found_is_target": beast_type == self.target_beast,
            "beast_seek_miniapp_last_error": "",
        })
        if beast_type == self.target_beast:
            self._clear_pending_release()
            result = f"本次新寻得目标灵兽【{name}】（{beast_type}），已保留"
            self.state.update({
                "beast_seek_miniapp_last_result": result,
                "last_hunt_result": result,
            })
            self._save()
            self.log.info("Mini App beast seek kept target [%s] id=%s", name, beast_id)
            return True

        # Persist the exact new id before issuing the destructive action.  A
        # restart can therefore retry only this beast and never choose from the
        # pre-existing roster.
        self.state.update({
            "beast_seek_miniapp_pending_release_id": beast_id,
            "beast_seek_miniapp_pending_release_name": name,
            "beast_seek_miniapp_pending_release_type": beast_type,
            "beast_seek_miniapp_last_result": (
                f"本次新寻得【{name}】（{beast_type or '未知种类'}），等待放生"
            ),
        })
        self._save()
        return await self._release_pending(roster)

    async def _resume_inflight(self, beasts: list[dict[str, Any]]) -> bool:
        started_text = str(self.state.get("beast_seek_miniapp_inflight_started_at") or "")
        started = _parse_time(started_text)
        if started is None:
            return False
        before_ids = {
            int(value)
            for value in (self.state.get("beast_seek_miniapp_inflight_existing_ids") or [])
            if str(value).isdigit() and int(value) > 0
        }
        new_beasts = [item for item in beasts if _beast_id(item) not in before_ids]
        if len(new_beasts) == 1:
            await self._process_new_beast(new_beasts[0], beasts, started)
            return True

        self._mark_seek_attempt(started)
        self._clear_inflight()
        if not new_beasts:
            result = "恢复上次寻觅：未发现新增灵兽，按一次寻觅计时"
            self.state.update({
                "beast_seek_miniapp_last_result": result,
                "last_hunt_result": result,
                "beast_seek_miniapp_last_error": "",
            })
            self._save()
            return True

        # More than one unfamiliar id may include a manual addition.  Never
        # guess which one to release.
        result = f"恢复上次寻觅时发现 {len(new_beasts)} 只新增灵兽，无法安全判定，均未放生"
        self.state.update({
            "beast_seek_miniapp_last_result": result,
            "last_hunt_result": result,
            "beast_seek_miniapp_last_error": "beast_seek_new_beast_ambiguous",
            "beast_seek_miniapp_last_error_time": _now_text(),
        })
        self._save()
        self.log.error(result)
        return True

    async def _run_cycle_unlocked(self) -> bool:
        self._record_enabled_state()
        self.state["beast_seek_miniapp_last_attempt_time"] = _now_text()
        self._save()
        if not self.enabled:
            return True
        if self._dashboard_paused():
            self.state["beast_seek_miniapp_paused"] = True
            self._schedule_wake(datetime.now() + timedelta(seconds=self.retry_seconds))
            self._save()
            return False
        self.state["beast_seek_miniapp_paused"] = False

        await self.transport.initialize()
        snapshot = await self.transport.spirit_beast_snapshot("主魂", log_operation=False)
        beasts = list((snapshot or {}).get("beasts") or [])
        self._update_cache(beasts)
        self._save()

        if self._pending_release_id() > 0:
            await self._release_pending(beasts)
            seek_next = _parse_time(self.state.get("beast_seek_miniapp_seek_next_time"))
            self._schedule_wake(seek_next or (datetime.now() + timedelta(seconds=self.interval_seconds)))
            self._save()
            return True

        if self.state.get("beast_seek_miniapp_inflight_started_at"):
            await self._resume_inflight(beasts)
            return True

        now = datetime.now()
        if not self._seek_due(now):
            seek_next = _parse_time(
                self.state.get("beast_seek_miniapp_seek_next_time")
                or self.state.get("next_hunt_time")
            )
            if seek_next is not None:
                self._schedule_wake(seek_next)
                self._save()
            return True

        limit, remaining = self._capacity(snapshot, beasts)
        if remaining <= 0 or len(beasts) >= limit:
            next_time = now + timedelta(seconds=self.interval_seconds)
            result = (
                f"灵兽袋已满（{len(beasts)}/{limit}）；按要求未放生任何现有灵兽，"
                f"{self.interval_seconds // 3600}小时后再检查"
            )
            self.state.update({
                "next_hunt_time": _now_text(next_time),
                "beast_seek_miniapp_seek_next_time": _now_text(next_time),
                "beast_seek_miniapp_next_time": _now_text(next_time),
                "beast_seek_miniapp_last_result": result,
                "last_hunt_result": result,
                "beast_seek_miniapp_last_error": "beast_capacity_reached",
                "beast_seek_miniapp_last_error_time": _now_text(now),
            })
            self._save()
            self.log.warning(result)
            return True

        before_ids = sorted(_roster_ids(beasts))
        self.state.update({
            "beast_seek_miniapp_inflight_started_at": _now_text(now),
            "beast_seek_miniapp_inflight_existing_ids": before_ids,
            "beast_seek_miniapp_last_error": "",
        })
        self._save()

        sought = await self.transport.spirit_beast_seek("主魂")
        post_beasts = list((sought or {}).get("beasts") or [])
        self._update_cache(post_beasts)
        new_beasts = [item for item in post_beasts if _beast_id(item) not in set(before_ids)]
        if len(new_beasts) == 1:
            return await self._process_new_beast(new_beasts[0], post_beasts, now)

        message = str((sought or {}).get("message") or "").strip()
        cooldown_seconds = _seek_cooldown_seconds(message)
        if not new_beasts and cooldown_seconds is not None:
            wait_seconds = max(
                MIN_COOLDOWN_RETRY_SECONDS,
                cooldown_seconds + COOLDOWN_RETRY_GRACE_SECONDS,
            )
            retry_at = datetime.now() + timedelta(seconds=wait_seconds)
            self._clear_inflight()
            self.state.update({
                "next_hunt_time": _now_text(retry_at),
                "beast_seek_miniapp_seek_next_time": _now_text(retry_at),
                "beast_seek_miniapp_next_time": _now_text(retry_at),
                "beast_seek_miniapp_last_result": message,
                "last_hunt_result": message,
                "beast_seek_miniapp_last_cooldown_seconds": cooldown_seconds,
                "beast_seek_miniapp_last_error": "",
            })
            self._save()
            self.log.info(
                "Mini App beast seek still cooling down (%ss); retrying in %ss",
                cooldown_seconds,
                wait_seconds,
            )
            return True

        self._mark_seek_attempt(now)
        self._clear_inflight()
        if not new_beasts:
            result = message or "本次寻觅未获得新灵兽"
            self.state.update({
                "beast_seek_miniapp_last_result": result,
                "last_hunt_result": result,
                "beast_seek_miniapp_last_error": "",
            })
            self._save()
            return True

        result = f"本次响应出现 {len(new_beasts)} 只新增灵兽，无法安全判定，均未放生"
        self.state.update({
            "beast_seek_miniapp_last_result": result,
            "last_hunt_result": result,
            "beast_seek_miniapp_last_error": "beast_seek_new_beast_ambiguous",
            "beast_seek_miniapp_last_error_time": _now_text(),
        })
        self._save()
        self.log.error(result)
        return False

    async def run_cycle(self) -> bool:
        try:
            lock = getattr(self.actor, "beast_lock", None)
            if lock is not None:
                async with lock:
                    return await self._run_cycle_unlocked()
            return await self._run_cycle_unlocked()
        except asyncio.CancelledError:
            raise
        except MiniAppCircuitOpenError as exc:
            wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
            self.state.update({
                "beast_seek_miniapp_last_error": exc.code,
                "beast_seek_miniapp_last_error_time": _now_text(),
                "beast_seek_miniapp_next_time": _now_text(
                    datetime.now() + timedelta(seconds=wait)
                ),
            })
            self._save()
            self.log.info(
                "Mini App Wanling beast seek paused by upstream circuit until %s",
                exc.retry_at or f"in {wait}s",
            )
            return False
        except Exception as exc:
            code = _error_code(exc)
            self.state.update({
                "beast_seek_miniapp_last_error": code,
                "beast_seek_miniapp_last_error_time": _now_text(),
                "beast_seek_miniapp_failure_count": int(
                    self.state.get("beast_seek_miniapp_failure_count") or 0
                ) + 1,
                "beast_seek_miniapp_next_time": _now_text(
                    datetime.now() + timedelta(seconds=self.retry_seconds)
                ),
            })
            self._save()
            self.log.error(
                "Mini App Wanling beast seek failed for %s: %s",
                self.account,
                code,
                exc_info=True,
            )
            return False

    async def run_loop(self) -> None:
        self._record_enabled_state()
        self._save()
        if not self.enabled:
            return
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        self.log.info(
            "Mini App Wanling beast seek scheduler started: target=%s interval=%ss",
            self.target_beast,
            self.interval_seconds,
        )
        while getattr(self.actor, "is_running", True):
            pause = getattr(self.actor, "pause_event", None)
            if pause is not None:
                await pause.wait()
            wait = _seconds_until(self.state.get("beast_seek_miniapp_next_time"))
            if wait > 0:
                await asyncio.sleep(min(wait, 300))
                continue
            await self.run_cycle()
            await asyncio.sleep(1)
