#!/usr/bin/env python3
"""Daily Tianxing wild-experience automation through the Mini App journey tab."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import (
    apply_dwelling_snapshot,
    command_result_ok,
    command_result_text,
    identity_state,
    miniapp_operation_result_text,
)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_JOURNEY_HOUR = 7
DEFAULT_JOURNEY_RETRY_SECONDS = 15 * 60
DEFAULT_ACTION_DELAY_SECONDS = 2
TARGET_DAILY_ATTEMPTS = 2
JOURNEY_PREFIX_COMMAND = ".改命 探索"
JOURNEY_MODE = "deep"
ACCOUNT_MINUTE_OFFSETS = {
    "main": 0,
    "waaiging": 30,
}
ACCOUNT_IDENTITY_SCOPE = {
    "main": ("无咎子",),
    "waaiging": ("主魂",),
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def wild_experience_state(payload: Any) -> dict[str, Any]:
    """Return the wild-experience state from either details or action payloads."""
    payload = _mapping(payload)
    account = _mapping(payload.get("account"))
    candidates = (
        _mapping(_mapping(account.get("journey")).get("wildExperience")),
        _mapping(_mapping(payload.get("journey")).get("wildExperience")),
        _mapping(_mapping(payload.get("state")).get("wildExperience")),
        _mapping(payload.get("wildExperience")),
    )
    return next((item for item in candidates if item), {})


def _seconds_until_timestamp(value: Any, now: datetime | None = None) -> int:
    now = now or datetime.now()
    if value in (None, ""):
        return 0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0
    if numeric > 0:
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return max(0, int((datetime.fromtimestamp(numeric) - now).total_seconds()))
        except (OverflowError, OSError, ValueError):
            pass
    text = str(value or "").strip()
    if not text:
        return 0
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return max(0, int((parsed - now).total_seconds()))
        except ValueError:
            continue
    return 0


def journey_counter(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """Normalize the server-authoritative daily counter and cooldown."""
    raw = wild_experience_state(payload)
    server_limit = _nonnegative_int(
        raw.get("dailyLimit") if raw.get("dailyLimit") is not None else raw.get("limit"),
        TARGET_DAILY_ATTEMPTS,
    ) or TARGET_DAILY_ATTEMPTS
    limit = min(TARGET_DAILY_ATTEMPTS, server_limit)
    count_value = raw.get("dailyCount")
    if count_value is None:
        count_value = raw.get("used")
    remaining_value = raw.get("dailyRemaining")
    if remaining_value is None:
        remaining_value = raw.get("remaining")
    if count_value is None and remaining_value is not None:
        server_count = max(0, server_limit - _nonnegative_int(remaining_value))
    else:
        server_count = _nonnegative_int(count_value)
    count = min(limit, server_count)
    remaining = max(0, limit - count)
    if remaining_value is not None:
        remaining = min(remaining, _nonnegative_int(remaining_value))
    remaining_seconds = _nonnegative_int(raw.get("remainingSeconds"))
    if remaining_seconds <= 0:
        remaining_seconds = _seconds_until_timestamp(raw.get("readyAt"), now=now)
    available_value = raw.get("available")
    available = (
        bool(available_value)
        if available_value is not None
        else remaining > 0 and remaining_seconds <= 0
    )
    if remaining <= 0:
        available = False
    return {
        "raw": raw,
        "present": bool(raw),
        "available": available,
        "daily_count": count,
        "daily_limit": limit,
        "daily_remaining": remaining,
        "remaining_seconds": remaining_seconds,
        "ready_at": str(raw.get("readyAt") or ""),
        "reset_at": str(raw.get("resetAt") or ""),
    }


class MiniAppTianxingJourney:
    """Use both daily journey attempts for the two explicitly scoped accounts."""

    def __init__(self, actor: Any, transport: Any, account: str, logger: Any) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        default_enabled = self.account in ACCOUNT_IDENTITY_SCOPE
        self.enabled = default_enabled and bool(settings.get("journey_daily_enabled", True))
        self.hour = _bounded_int(
            settings.get("journey_daily_hour"),
            DEFAULT_JOURNEY_HOUR,
            0,
            23,
        )
        self.minute = _bounded_int(
            settings.get("journey_daily_minute"),
            ACCOUNT_MINUTE_OFFSETS.get(self.account, 0),
            0,
            59,
        )
        self.retry_seconds = max(
            60,
            int(settings.get("journey_retry_seconds") or DEFAULT_JOURNEY_RETRY_SECONDS),
        )
        delay_value = settings.get("journey_action_delay_seconds")
        if delay_value is None:
            delay_value = DEFAULT_ACTION_DELAY_SECONDS
        self.action_delay_seconds = max(0, int(delay_value))

    def _save(self) -> None:
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App journey state save failed", exc_info=True)

    def _state(self, identity: str) -> dict[str, Any]:
        return identity_state(self.actor, identity)

    def _record(self, identity: str, **updates: Any) -> None:
        self._state(identity).update(updates)
        self._save()

    def _record_root(self, **updates: Any) -> None:
        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state.update(updates)
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
        return ""

    def identities(self, candidates: list[str] | None = None) -> list[str]:
        """Return only main/无咎子 and Waaiging/主魂 when Mini App says 天星宗."""
        allowed = ACCOUNT_IDENTITY_SCOPE.get(self.account, ())
        candidate_set = set(candidates) if candidates is not None else None
        ids = getattr(self.transport, "identity_player_ids", {}) or {}
        result = []
        for identity in allowed:
            if candidate_set is not None and identity not in candidate_set:
                continue
            if identity not in ids and identity.casefold() not in ids:
                continue
            if self._identity_sect(identity) == "天星宗":
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

    def _record_counter(
        self,
        identity: str,
        counter: dict[str, Any],
        **updates: Any,
    ) -> None:
        now_text = datetime.now().strftime(TIME_FORMAT)
        values = {
            "miniapp_journey_last_sync_time": now_text,
            "miniapp_journey_daily_count": int(counter.get("daily_count") or 0),
            "miniapp_journey_daily_limit": int(counter.get("daily_limit") or TARGET_DAILY_ATTEMPTS),
            "miniapp_journey_daily_remaining": int(counter.get("daily_remaining") or 0),
            "miniapp_journey_available": bool(counter.get("available")),
            "miniapp_journey_remaining_seconds": int(counter.get("remaining_seconds") or 0),
            "miniapp_journey_ready_at": str(counter.get("ready_at") or ""),
            "miniapp_journey_reset_at": str(counter.get("reset_at") or ""),
        }
        values.update(updates)
        self._record(identity, **values)

    def _record_error(self, identity: str, code: str, detail: str = "") -> None:
        now_text = datetime.now().strftime(TIME_FORMAT)
        self._record(
            identity,
            miniapp_journey_last_error=str(code or "journey_failed"),
            miniapp_journey_last_error_time=now_text,
            miniapp_journey_last_result=str(detail or "")[:1000],
        )

    def _record_next_schedule(self, target: datetime, identities: list[str]) -> None:
        value = target.strftime(TIME_FORMAT)
        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state["miniapp_journey_next_run_time"] = value
            state["miniapp_journey_identities"] = list(identities)
        for identity in identities:
            self._state(identity)["miniapp_journey_next_run_time"] = value
        self._save()

    async def _refresh_counter(self, identity: str, now: datetime) -> dict[str, Any]:
        payload = await self.transport.journey_snapshot(identity)
        apply_dwelling_snapshot(self.actor, identity, payload)
        counter = journey_counter(payload, now=now)
        self._record_counter(identity, counter)
        return counter

    async def run_identity(
        self,
        identity: str,
        now: datetime | None = None,
    ) -> tuple[bool, int]:
        """Use all currently available attempts; return (daily_complete, retry_seconds)."""
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        counter = await self._refresh_counter(identity, now)
        if not counter.get("present"):
            raise MiniAppBeastError("wild_experience_state_missing")
        if counter["daily_remaining"] <= 0:
            self._record_counter(
                identity,
                counter,
                miniapp_journey_last_date=today,
                miniapp_journey_last_error="",
                miniapp_journey_last_result=(
                    self._state(identity).get("miniapp_journey_last_result")
                    or f"今日已完成 {counter['daily_count']}/{counter['daily_limit']} 次"
                ),
            )
            return True, 0

        safety_limit = max(1, int(counter["daily_limit"]))
        attempts = 0
        while counter["daily_remaining"] > 0 and attempts < safety_limit:
            pause_seconds = self._identity_pause_seconds(identity)
            if pause_seconds > 0:
                return False, max(60, pause_seconds)
            pause = getattr(self.actor, "pause_event", None)
            if pause is not None:
                await pause.wait()

            if not counter["available"]:
                remaining_seconds = int(counter.get("remaining_seconds") or 0)
                if remaining_seconds <= 0:
                    self._record_error(identity, "wild_experience_unavailable")
                    self.log.error(
                        "Mini App journey is unavailable for %s without a server cooldown",
                        identity,
                    )
                wait = max(5, remaining_seconds or self.retry_seconds)
                return False, wait

            combined = getattr(self.transport, "journey_with_destiny_prefix", None)
            if callable(combined):
                prefix, payload = await combined(
                    identity,
                    prefix_command=JOURNEY_PREFIX_COMMAND,
                    mode=JOURNEY_MODE,
                )
            else:
                prefix = await self.transport.command(JOURNEY_PREFIX_COMMAND, identity=identity)
                payload = None
            if not command_result_ok(prefix.payload):
                detail = command_result_text(prefix.payload) or prefix.text or "改命探索前置失败"
                self._record_error(identity, "journey_destiny_prefix_failed", detail)
                self.log.error(
                    "Mini App journey prefix failed for %s; deep action was blocked",
                    identity,
                )
                return False, self.retry_seconds
            if payload is None:
                payload = await self.transport.journey_action(identity, mode=JOURNEY_MODE)
            if not command_result_ok(payload):
                raise MiniAppBeastError("wild_experience_failed")
            apply_dwelling_snapshot(self.actor, identity, payload)
            result_text = miniapp_operation_result_text(payload)
            action_time = datetime.now().strftime(TIME_FORMAT)
            attempts += 1

            counter = await self._refresh_counter(identity, datetime.now())
            self._record_counter(
                identity,
                counter,
                miniapp_journey_last_time=action_time,
                miniapp_journey_last_result=result_text,
                miniapp_journey_last_error="",
                miniapp_journey_last_prefix=JOURNEY_PREFIX_COMMAND,
                miniapp_journey_last_mode=JOURNEY_MODE,
            )
            recorder = getattr(self.actor, "record_daily_reward_event", None)
            if callable(recorder) and result_text:
                try:
                    recorder(
                        identity,
                        ".游历 深入",
                        result_text,
                        source="Mini App 游历·深入",
                        final=True,
                    )
                except Exception:
                    self.log.warning(
                        "Mini App journey reward recording failed for %s",
                        identity,
                        exc_info=True,
                    )

            if counter["daily_remaining"] <= 0:
                self._record(
                    identity,
                    miniapp_journey_last_date=today,
                    miniapp_journey_last_error="",
                )
                return True, 0
            if not counter["available"]:
                wait = max(5, int(counter.get("remaining_seconds") or self.retry_seconds))
                return False, wait
            if self.action_delay_seconds:
                await asyncio.sleep(self.action_delay_seconds)

        complete = counter["daily_remaining"] <= 0
        return complete, 0 if complete else self.retry_seconds

    async def run_daily_once(
        self,
        now: datetime | None = None,
    ) -> tuple[bool, int]:
        await self.transport.initialize()
        now = now or datetime.now()
        identities = self.identities()
        if not identities:
            self._record_root(
                miniapp_journey_last_error="journey_identity_unavailable",
                miniapp_journey_last_error_time=now.strftime(TIME_FORMAT),
                miniapp_journey_identities=[],
            )
            return False, self.retry_seconds

        complete = True
        retry_after = 0
        for identity in identities:
            try:
                identity_complete, identity_retry = await self.run_identity(identity, now=now)
                complete = complete and identity_complete
                if not identity_complete:
                    retry_after = identity_retry if retry_after <= 0 else min(retry_after, identity_retry)
            except asyncio.CancelledError:
                raise
            except MiniAppBeastError as exc:
                if exc.code == "wild_experience_daily_limit":
                    counter = await self._refresh_counter(identity, datetime.now())
                    self._record_counter(
                        identity,
                        counter,
                        miniapp_journey_last_date=now.strftime("%Y-%m-%d"),
                        miniapp_journey_last_error="",
                    )
                    continue
                if exc.code == "wild_experience_unavailable":
                    try:
                        counter = await self._refresh_counter(identity, datetime.now())
                        if counter.get("present") and counter.get("daily_remaining", 0) > 0:
                            wait = max(
                                5,
                                int(counter.get("remaining_seconds") or self.retry_seconds),
                            )
                            complete = False
                            retry_after = wait if retry_after <= 0 else min(retry_after, wait)
                            self._record_counter(
                                identity,
                                counter,
                                miniapp_journey_last_error="",
                            )
                            continue
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass
                complete = False
                retry_after = retry_after or self.retry_seconds
                self._record_error(identity, exc.code)
                self.log.error(
                    "Mini App journey failed for %s: %s",
                    identity,
                    exc.code,
                    exc_info=True,
                )
            except Exception as exc:
                complete = False
                retry_after = retry_after or self.retry_seconds
                code = type(exc).__name__.lower()
                self._record_error(identity, code)
                self.log.error(
                    "Mini App journey failed for %s: %s",
                    identity,
                    code,
                    exc_info=True,
                )
        root_updates = {
            "miniapp_journey_last_cycle_time": datetime.now().strftime(TIME_FORMAT),
            "miniapp_journey_identities": identities,
        }
        if complete:
            root_updates["miniapp_journey_last_error"] = ""
        self._record_root(**root_updates)
        return complete, 0 if complete else (retry_after or self.retry_seconds)

    @staticmethod
    def _target_datetime(now: datetime, hour: int, minute: int) -> datetime:
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def run_loop(self) -> None:
        if not self.enabled:
            return
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        while getattr(self.actor, "is_running", True):
            now = datetime.now()
            target = self._target_datetime(now, self.hour, self.minute)
            identities = self.identities()
            if now < target:
                next_target = target
                wait = max(5, int((next_target - now).total_seconds()))
            else:
                pause = getattr(self.actor, "pause_event", None)
                if pause is not None:
                    await pause.wait()
                try:
                    complete, retry_after = await self.run_daily_once(now=now)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                    self._record_root(
                        miniapp_journey_last_error=code,
                        miniapp_journey_last_error_time=datetime.now().strftime(TIME_FORMAT),
                    )
                    self.log.error(
                        "Mini App journey daily cycle failed: %s",
                        code,
                        exc_info=True,
                    )
                    complete, retry_after = False, self.retry_seconds
                if complete:
                    next_target = target + timedelta(days=1)
                    wait = max(5, int((next_target - datetime.now()).total_seconds()))
                else:
                    wait = max(5, int(retry_after or self.retry_seconds))
                    next_target = datetime.now() + timedelta(seconds=wait)
            self._record_next_schedule(next_target, identities)
            await asyncio.sleep(wait)
