#!/usr/bin/env python3
"""Cooldown-aware Wan Beast Valley abyss automation through the Mini App."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from typing import Any

from automation_settings import miniapp_beast_abyss_settings
from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError, miniapp_circuit_wait_seconds
from miniapp_dwelling import identity_state, spirit_beast_abyss_result_text


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_RETRY_SECONDS = 15 * 60
READY_GRACE_SECONDS = 5
ACCOUNT_IDENTITY_SCOPE = {
    "main": ("主魂",),
    "xiaohao": ("主魂",),
}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _parse_server_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0
    if numeric > 0:
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric)
        except (OverflowError, OSError, ValueError):
            pass
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        return None


def abyss_data(payload: Any) -> dict[str, Any]:
    """Find the abyss block in either a start or action response."""
    payload = _mapping(payload)
    candidates = [
        _mapping(payload.get("abyss")),
        _mapping(_mapping(payload.get("state")).get("abyss")),
        _mapping(_mapping(payload.get("data")).get("abyss")),
        _mapping(_mapping(payload.get("result")).get("abyss")),
    ]
    return next((item for item in candidates if item), {})


def abyss_status(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """Normalize page cooldown state, preferring ``readyAt`` over counters."""
    now = now or datetime.now()
    raw = abyss_data(payload)
    ready_at = _parse_server_time(raw.get("readyAt"))
    if ready_at is not None:
        remaining_seconds = max(0, math.ceil((ready_at - now).total_seconds()))
        cooldown_source = "readyAt"
    else:
        remaining_seconds = _nonnegative_int(raw.get("remainingSeconds"))
        ready_at = now + timedelta(seconds=remaining_seconds) if remaining_seconds > 0 else None
        cooldown_source = "remainingSeconds"

    ready_value = raw.get("ready")
    ready = bool(ready_value) if ready_value is not None else remaining_seconds <= 0
    if remaining_seconds > 0:
        ready = False
    cooldown_hours = _nonnegative_int(raw.get("cooldownHours"), DEFAULT_COOLDOWN_HOURS)
    if cooldown_hours <= 0:
        cooldown_hours = DEFAULT_COOLDOWN_HOURS
    min_stamina = _nonnegative_int(raw.get("minStamina"), 30)
    return {
        "raw": raw,
        "present": bool(raw),
        "ready": ready,
        "ready_at": ready_at,
        "ready_at_raw": str(raw.get("readyAt") or ""),
        "remaining_seconds": remaining_seconds,
        "cooldown_hours": cooldown_hours,
        "cooldown_source": cooldown_source,
        "min_stamina": min_stamina,
    }


def choose_abyss_beast(
    beasts: Any,
    *,
    power_min: int = 0,
    power_max: int = 0,
) -> dict[str, Any] | None:
    """Choose the strongest beast that the Mini App explicitly enables."""
    candidates = []
    filter_active = int(power_min or 0) > 0 or int(power_max or 0) > 0
    for beast in beasts if isinstance(beasts, list) else []:
        if not isinstance(beast, dict) or not beast.get("can_explore_abyss"):
            continue
        if _nonnegative_int(beast.get("id")) <= 0:
            continue
        power = _nonnegative_int(beast.get("power"))
        if filter_active:
            if power_min > 0 and power < int(power_min):
                continue
            if power_max > 0 and power > int(power_max):
                continue
        candidates.append(beast)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            _nonnegative_int(item.get("power")),
            _nonnegative_int(item.get("stamina")),
            _nonnegative_int(item.get("tier")),
            str(item.get("full_name") or ""),
        ),
    )


class MiniAppBeastAbyssWorker:
    """Run abyss only for the two requested Wanling main souls."""

    def __init__(self, actor: Any, transport: Any, account: str, logger: Any) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        entry_url = str(settings.get("entry_url") or "").strip()
        self.enabled = (
            self.account in ACCOUNT_IDENTITY_SCOPE
            and self.transport is not None
            and bool(entry_url)
            and bool(settings.get("enabled", True))
            and bool(settings.get("beast_abyss_enabled", True))
        )
        self.retry_seconds = max(
            60,
            int(settings.get("beast_abyss_retry_seconds") or DEFAULT_RETRY_SECONDS),
        )
        grace_value = settings.get("beast_abyss_ready_grace_seconds")
        if grace_value is None:
            grace_value = READY_GRACE_SECONDS
        self.ready_grace_seconds = max(0, int(grace_value))

    def _save(self) -> None:
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App abyss state save failed", exc_info=True)

    def _state(self, identity: str = "主魂") -> dict[str, Any]:
        return identity_state(self.actor, identity)

    def _record(self, identity: str = "主魂", **updates: Any) -> None:
        self._state(identity).update(updates)
        self._save()

    def _identity_sect(self, identity: str) -> str:
        state = self._state(identity)
        for key in ("miniapp_sect_name", "sect_name"):
            sect = str(state.get(key) or "").strip()
            if sect:
                return sect
        mapping = getattr(self.actor, "identity_sect_names", {}) or {}
        if isinstance(mapping, dict):
            sect = str(mapping.get(identity) or "").strip()
            if sect:
                return sect
        resolver = getattr(self.actor, "identity_sect_name", None)
        if callable(resolver):
            try:
                return str(resolver(identity) or "").strip()
            except Exception:
                return ""
        return str(getattr(self.actor, "sect_name", "") or "").strip()

    def identities(self) -> list[str]:
        if not self.enabled:
            return []
        ids = getattr(self.transport, "identity_player_ids", {}) or {}
        result = []
        for identity in ACCOUNT_IDENTITY_SCOPE.get(self.account, ()):
            mapped = identity in ids or identity.casefold() in ids
            if mapped and self._identity_sect(identity) == "万灵宗":
                result.append(identity)
        return result

    def _identity_pause_seconds(self, identity: str) -> int:
        resolver = getattr(self.actor, "identity_pause_seconds", None)
        if not callable(resolver):
            return 0
        try:
            return max(0, int(resolver(identity) or 0))
        except Exception:
            return 0

    def _next_text(self, status: dict[str, Any], now: datetime) -> str:
        ready_at = status.get("ready_at")
        if isinstance(ready_at, datetime) and ready_at > now:
            return ready_at.strftime(TIME_FORMAT)
        remaining = _nonnegative_int(status.get("remaining_seconds"))
        return (
            (now + timedelta(seconds=remaining)).strftime(TIME_FORMAT)
            if remaining > 0
            else ""
        )

    def _record_snapshot(
        self,
        identity: str,
        snapshot: dict[str, Any],
        status: dict[str, Any],
        now: datetime,
    ) -> None:
        beasts = list(snapshot.get("beasts") or [])
        state = self._state(identity)
        state["beasts_cache"] = beasts
        state["beast_roster_updated_at"] = now.strftime(TIME_FORMAT)
        state["beast_roster_last_source"] = "miniapp"
        state["beast_abyss_miniapp_last_sync_time"] = now.strftime(TIME_FORMAT)
        state["beast_abyss_miniapp_ready"] = bool(status.get("ready"))
        state["beast_abyss_miniapp_ready_at"] = str(status.get("ready_at_raw") or "")
        state["beast_abyss_miniapp_remaining_seconds"] = _nonnegative_int(
            status.get("remaining_seconds")
        )
        state["beast_abyss_miniapp_cooldown_hours"] = _nonnegative_int(
            status.get("cooldown_hours"), DEFAULT_COOLDOWN_HOURS
        )
        state["beast_abyss_miniapp_min_stamina"] = _nonnegative_int(
            status.get("min_stamina"), 30
        )
        state["beast_abyss_miniapp_cooldown_source"] = str(
            status.get("cooldown_source") or ""
        )
        next_time = self._next_text(status, now)
        state["beast_abyss_miniapp_next_time"] = next_time
        state["next_abyss_time"] = next_time
        state["beast_abyss_miniapp_last_error"] = ""
        updater = getattr(self.actor, "update_main_best_beast", None)
        if not callable(updater):
            updater = getattr(self.actor, "update_best_beast_tracking", None)
        if callable(updater):
            updater()
        self._save()

    async def _snapshot(self, identity: str) -> dict[str, Any]:
        return await self.transport.spirit_beast_snapshot(
            identity,
            log_operation=False,
        )

    def _record_error(self, identity: str, code: str, next_seconds: int | None = None) -> None:
        now = datetime.now()
        retry = self.retry_seconds if next_seconds is None else max(0, int(next_seconds))
        self._record(
            identity,
            beast_abyss_miniapp_last_error=str(code or "beast_abyss_failed"),
            beast_abyss_miniapp_last_error_time=now.strftime(TIME_FORMAT),
            beast_abyss_miniapp_next_time=(now + timedelta(seconds=retry)).strftime(TIME_FORMAT),
            next_abyss_time=(now + timedelta(seconds=retry)).strftime(TIME_FORMAT),
        )

    async def run_once(self, identity: str = "主魂", now: datetime | None = None) -> int:
        """Read one page state and enter once only when its cooldown is ready."""
        now = now or datetime.now()
        initializer = getattr(self.transport, "initialize", None)
        if callable(initializer):
            await initializer()
        if identity not in self.identities():
            self._record_error(identity, "beast_abyss_identity_unavailable")
            return self.retry_seconds
        paused = self._identity_pause_seconds(identity)
        if paused > 0:
            return max(60, paused)

        lock = getattr(self.actor, "beast_lock", None)
        if lock is not None:
            async with lock:
                return await self._run_once_unlocked(identity, now)
        return await self._run_once_unlocked(identity, now)

    async def _run_once_unlocked(self, identity: str, now: datetime) -> int:
        selection = miniapp_beast_abyss_settings()
        power_min = _nonnegative_int(selection.get("power_min"))
        power_max = _nonnegative_int(selection.get("power_max"))
        self._record(
            identity,
            beast_abyss_miniapp_enabled=True,
            beast_abyss_miniapp_last_attempt_time=now.strftime(TIME_FORMAT),
            beast_abyss_miniapp_power_min=power_min,
            beast_abyss_miniapp_power_max=power_max,
        )
        snapshot = await self._snapshot(identity)
        status = abyss_status(snapshot.get("raw"), now=now)
        if not status.get("present"):
            raise MiniAppBeastError("beast_abyss_state_missing")
        self._record_snapshot(identity, snapshot, status, now)

        remaining = _nonnegative_int(status.get("remaining_seconds"))
        if remaining > 0:
            return remaining + self.ready_grace_seconds
        if not status.get("ready"):
            self._record_error(identity, "beast_abyss_not_ready")
            return self.retry_seconds

        beast = choose_abyss_beast(
            snapshot.get("beasts"),
            power_min=power_min,
            power_max=power_max,
        )
        if beast is None:
            self._record_error(identity, "beast_abyss_no_available_beast")
            self.log.warning(
                "Mini App abyss has no eligible beast for [%s] in power range %s-%s; retrying in %ss",
                identity,
                power_min or 0,
                power_max or "inf",
                self.retry_seconds,
            )
            return self.retry_seconds

        pause = getattr(self.actor, "pause_event", None)
        if pause is not None:
            await pause.wait()
        paused = self._identity_pause_seconds(identity)
        if paused > 0:
            return max(60, paused)

        beast_id = _nonnegative_int(beast.get("id"))
        beast_name = str(beast.get("full_name") or beast_id).strip()
        payload = await self.transport.spirit_beast_abyss_enter(
            identity,
            beast_id,
            beast_name=beast_name,
        )
        action_now = datetime.now()
        result_text = spirit_beast_abyss_result_text(payload)
        result = _mapping(payload.get("result"))
        won = result.get("won")
        cooldown_hours = _nonnegative_int(
            status.get("cooldown_hours"), DEFAULT_COOLDOWN_HOURS
        ) or DEFAULT_COOLDOWN_HOURS
        fallback_next = action_now + timedelta(hours=cooldown_hours)
        self._record(
            identity,
            last_abyss_time=action_now.strftime(TIME_FORMAT),
            next_abyss_time=fallback_next.strftime(TIME_FORMAT),
            beast_abyss_miniapp_next_time=fallback_next.strftime(TIME_FORMAT),
            beast_abyss_miniapp_last_time=action_now.strftime(TIME_FORMAT),
            beast_abyss_miniapp_last_beast=beast_name,
            beast_abyss_miniapp_last_beast_id=beast_id,
            beast_abyss_miniapp_last_result=result_text[:1000],
            beast_abyss_miniapp_last_won=won,
            beast_abyss_miniapp_last_error="",
            beast_abyss_miniapp_ready=False,
            beast_abyss_miniapp_ready_at="",
            beast_abyss_miniapp_cooldown_source="fallback",
            beast_abyss_miniapp_remaining_seconds=cooldown_hours * 3600,
            beast_abyss_miniapp_refresh_error="",
        )

        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder) and result_text:
            try:
                recorder(
                    identity,
                    f".探渊 {beast_name}",
                    result_text,
                    source="Mini App 万兽谷探渊",
                    final=True,
                )
            except Exception:
                self.log.warning(
                    "Mini App abyss reward recording failed for %s",
                    identity,
                    exc_info=True,
                )

        action_status = abyss_status(payload, now=action_now)
        if action_status.get("present"):
            remaining = _nonnegative_int(action_status.get("remaining_seconds"))
            if remaining <= 0:
                remaining = cooldown_hours * 3600
                next_time = fallback_next.strftime(TIME_FORMAT)
                ready = False
            else:
                next_time = self._next_text(action_status, action_now) or fallback_next.strftime(TIME_FORMAT)
                ready = False
            self._record(
                identity,
                next_abyss_time=next_time,
                beast_abyss_miniapp_next_time=next_time,
                beast_abyss_miniapp_ready=ready,
                beast_abyss_miniapp_ready_at=str(action_status.get("ready_at_raw") or ""),
                beast_abyss_miniapp_remaining_seconds=remaining,
                beast_abyss_miniapp_cooldown_hours=_nonnegative_int(
                    action_status.get("cooldown_hours"), cooldown_hours
                ),
                beast_abyss_miniapp_cooldown_source=str(
                    action_status.get("cooldown_source") or ""
                ),
            )
            return max(1, remaining) + self.ready_grace_seconds

        # Some action responses omit the page state.  Perform one immediate,
        # silent refresh; the fallback six-hour timestamp above already makes
        # the action idempotent if this read fails.
        try:
            refreshed = await self._snapshot(identity)
            refreshed_status = abyss_status(refreshed.get("raw"), now=datetime.now())
            if refreshed_status.get("present"):
                refresh_now = datetime.now()
                wait = _nonnegative_int(refreshed_status.get("remaining_seconds"))
                if wait > 0:
                    self._record_snapshot(identity, refreshed, refreshed_status, refresh_now)
                    self._record(identity, beast_abyss_miniapp_ready=False)
                    return wait + self.ready_grace_seconds
                self._record(
                    identity,
                    beast_abyss_miniapp_refresh_error="stale_state_after_action",
                    beast_abyss_miniapp_refresh_error_time=refresh_now.strftime(TIME_FORMAT),
                )
                self.log.warning(
                    "Mini App abyss returned stale ready state after action for %s; preserving fallback cooldown",
                    identity,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self._record(
                identity,
                beast_abyss_miniapp_refresh_error=code,
                beast_abyss_miniapp_refresh_error_time=datetime.now().strftime(TIME_FORMAT),
            )
            self.log.warning(
                "Mini App abyss post-action refresh failed for %s: %s",
                identity,
                code,
            )
        return cooldown_hours * 3600 + self.ready_grace_seconds

    async def run_loop(self) -> None:
        self._record(
            "主魂",
            beast_abyss_miniapp_enabled=bool(self.enabled),
            beast_abyss_miniapp_account=self.account,
        )
        if not self.enabled:
            return
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        while getattr(self.actor, "is_running", True):
            wait = self.retry_seconds
            try:
                wait = await self.run_once("主魂")
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError as exc:
                wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
                self._record_error("主魂", exc.code)
                self.log.info(
                    "Mini App Wan Beast Valley abyss paused by upstream circuit until %s",
                    exc.retry_at or f"in {wait}s",
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                self._record_error("主魂", code)
                self.log.error(
                    "Mini App Wan Beast Valley abyss failed for %s: %s",
                    self.account,
                    code,
                    exc_info=True,
                )
            await asyncio.sleep(max(5, int(wait or self.retry_seconds)))
