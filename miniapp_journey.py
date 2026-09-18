#!/usr/bin/env python3
"""Mini App wild experience with server quotas and one daily result summary."""

from __future__ import annotations

from automation_command_controls import CommandControlPaused, JOURNEY, command_paused

import asyncio
from datetime import datetime, timedelta
import math
from typing import Any

from automation_settings import (
    MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS,
    miniapp_journey_identities_for_account,
    miniapp_journey_settings,
)
from miniapp_beast import (
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    miniapp_circuit_preflight,
    miniapp_circuit_wait_seconds,
)
from miniapp_dwelling import (
    apply_dwelling_snapshot,
    command_result_ok,
    command_result_text,
    identity_state,
    miniapp_operation_result_text,
)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_JOURNEY_RETRY_SECONDS = 15 * 60
DEFAULT_SETTINGS_RELOAD_SECONDS = 60
# 野外历练已改为按服务器可用次数连续执行，不再本地强制 3 小时冷却。
JOURNEY_INTERVAL_SECONDS = 0
JOURNEY_PREFIX_COMMAND = ".改命 探索"
JOURNEY_MODE = "deep"


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
            return max(0, math.ceil((datetime.fromtimestamp(numeric) - now).total_seconds()))
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
            return max(0, math.ceil((parsed - now).total_seconds()))
        except ValueError:
            continue
    return 0


def journey_counter(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """Read server cooldowns and optional quotas without imposing a daily cap."""
    raw = wild_experience_state(payload)
    server_limit = _nonnegative_int(
        raw.get("dailyLimit") if raw.get("dailyLimit") is not None else raw.get("limit"),
    )
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
    remaining = max(0, server_limit - server_count) if server_limit else None
    if remaining_value is not None:
        remaining = (
            min(remaining, _nonnegative_int(remaining_value))
            if remaining is not None else _nonnegative_int(remaining_value)
        )
    remaining_seconds = _nonnegative_int(raw.get("remainingSeconds"))
    if remaining_seconds <= 0:
        remaining_seconds = _seconds_until_timestamp(raw.get("readyAt"), now=now)
    available_value = raw.get("available")
    available = (
        bool(available_value)
        if available_value is not None
        else remaining != 0 and remaining_seconds <= 0
    )
    if remaining == 0 or remaining_seconds > 0:
        available = False
    return {
        "raw": raw,
        "present": bool(raw),
        "available": available,
        "daily_count": server_count,
        "daily_limit": server_limit,
        "daily_remaining": remaining,
        "remaining_seconds": remaining_seconds,
        "ready_at": str(raw.get("readyAt") or ""),
        "reset_at": str(raw.get("resetAt") or ""),
    }


def journey_rounds_summary(batch: dict[str, Any]) -> str:
    """Format the results actually captured, including gaps in server history."""
    records = batch.get("records") or []
    if not records:
        return ""
    count = max(len(records), _nonnegative_int(batch.get("daily_count")))
    limit = _nonnegative_int(batch.get("daily_limit"))
    progress = f"今日完成 {count}/{limit} 次" if limit else f"已完成 {count} 次"
    if not batch.get("complete"):
        progress += "，跨日汇总"
    lines = [f"野外历练汇总（深入 · {batch['date']} · {progress}）"]
    if count > len(records):
        lines.append(f"本条记录 {len(records)} 次；其余 {count - len(records)} 次无本地明细。")
    for index, record in enumerate(records, 1):
        time_text = str(record.get("completed_at") or "").partition(" ")[2]
        result = " ".join(str(record.get("result") or "历练已完成，服务端未返回明细").split())
        line = f"{index}. {time_text} {result}"
        prefix = " ".join(str(record.get("prefix_result") or "").split())
        if prefix:
            line += f"（改命探索：{prefix}）"
        lines.append(line)
    return "\n".join(lines)


class MiniAppTianxingJourney:
    """Follow each identity's server quota and persist results until summary."""

    def __init__(self, actor: Any, transport: Any, account: str, logger: Any) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        self.supported = self.account in MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS
        # Retain the existing enable switch; the old daily start hour is obsolete.
        self.enabled = self.supported and bool(settings.get("journey_daily_enabled", True))
        self.retry_seconds = max(
            60,
            int(settings.get("journey_retry_seconds") or DEFAULT_JOURNEY_RETRY_SECONDS),
        )
        self.settings_reload_seconds = max(
            15,
            int(
                settings.get("journey_settings_reload_seconds")
                or DEFAULT_SETTINGS_RELOAD_SECONDS
            ),
        )

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

    def _emit_daily_summary(self, identity: str, now: datetime) -> bool:
        batch = _mapping(self._state(identity).get("miniapp_journey_summary"))
        if not batch.get("records") or batch.get("emitted"):
            return False
        if not batch.get("complete") and str(batch.get("date") or "") >= now.strftime("%Y-%m-%d"):
            return False
        summary = journey_rounds_summary(batch)
        self.log.info("IN [Mini App | %s]:\n%s", identity, summary)
        self._record(
            identity,
            miniapp_journey_summary={**batch, "emitted": True},
            miniapp_journey_last_daily_summary=summary,
            miniapp_journey_last_daily_summary_time=now.strftime(TIME_FORMAT),
        )
        return True

    def _summary_after_success(
        self,
        identity: str,
        result: str,
        prefix_result: str,
        before: dict[str, Any],
        after: dict[str, Any],
        started_at: datetime,
        completed_at: datetime,
    ) -> dict[str, Any]:
        # Flush an interrupted previous day before replacing its journal.
        self._emit_daily_summary(identity, completed_at)
        date = completed_at.strftime("%Y-%m-%d")
        batch = _mapping(self._state(identity).get("miniapp_journey_summary"))
        if batch.get("date") != date:
            batch = {"date": date, "records": [], "emitted": False}
        records = list(batch.get("records") or [])
        # A confirmed action consumes one attempt even when its response omits
        # the updated counter. Never carry yesterday's quota over midnight.
        same_day = started_at.date() == completed_at.date()
        count = max(
            len(records) + 1,
            _nonnegative_int(after.get("daily_count")),
            _nonnegative_int(batch.get("daily_count")) + 1,
            _nonnegative_int(before.get("daily_count")) + 1 if same_day else 0,
        )
        limit = _nonnegative_int(after.get("daily_limit")) or (
            _nonnegative_int(before.get("daily_limit")) if same_day else 0
        )
        records.append({
            "completed_at": completed_at.strftime(TIME_FORMAT),
            "result": result,
            "prefix_result": prefix_result,
        })
        return {
            **batch,
            "records": records,
            "daily_count": count,
            "daily_limit": limit,
            "complete": bool(
                after.get("present") and after.get("daily_remaining") == 0
                or same_day and before.get("daily_remaining") == 1
                or limit and count >= limit
            ),
        }

    def _observe_summary_counter(self, identity: str, counter: dict[str, Any], now: datetime) -> None:
        batch = _mapping(self._state(identity).get("miniapp_journey_summary"))
        if batch.get("date") == now.strftime("%Y-%m-%d") and batch.get("records"):
            self._record(identity, miniapp_journey_summary={
                **batch,
                "daily_count": max(
                    _nonnegative_int(batch.get("daily_count")),
                    _nonnegative_int(counter.get("daily_count")),
                ),
                "daily_limit": _nonnegative_int(counter.get("daily_limit")) or batch.get("daily_limit", 0),
                "complete": bool(batch.get("complete") or counter.get("daily_remaining") == 0),
            })
        self._emit_daily_summary(identity, now)

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

    def runtime_enabled(self) -> bool:
        return bool(self.enabled and miniapp_journey_settings().get("enabled"))

    def configured_identities(self) -> list[str]:
        return miniapp_journey_identities_for_account(self.account)

    def identities(self, candidates: list[str] | None = None) -> list[str]:
        """Return selected identities that are available in the Mini App."""
        allowed = self.configured_identities()
        candidate_set = set(candidates) if candidates is not None else None
        ids = getattr(self.transport, "identity_player_ids", {}) or {}
        result = []
        for identity in allowed:
            if candidate_set is not None and identity not in candidate_set:
                continue
            if identity not in ids and identity.casefold() not in ids:
                continue
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
            "miniapp_journey_daily_limit": int(counter.get("daily_limit") or 0),
            "miniapp_journey_daily_remaining": counter.get("daily_remaining"),
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
        # The root is also the main soul's state. Never overwrite its own
        # cooldown with the earliest avatar's wake-up time.
        self._record_root(
            miniapp_journey_next_cycle_time=target.strftime(TIME_FORMAT),
            miniapp_journey_identities=list(identities),
        )

    def _local_cooldown(self, identity: str, now: datetime) -> int:
        last = self._state(identity).get("miniapp_journey_last_time")
        try:
            target = datetime.strptime(str(last), TIME_FORMAT) + timedelta(
                seconds=JOURNEY_INTERVAL_SECONDS
            )
        except (TypeError, ValueError):
            return 0
        return max(0, math.ceil((target - now).total_seconds()))

    def _migrate_schedule(self, identity: str, now: datetime) -> None:
        state = self._state(identity)
        if state.get("miniapp_journey_interval_seconds") == JOURNEY_INTERVAL_SECONDS:
            return
        # Old next_run_time / last_date represented tomorrow's daily batch.
        # Rebuild only from the last action and an observed server cooldown.
        wait = max(
            self._local_cooldown(identity, now),
            _seconds_until_timestamp(state.get("miniapp_journey_ready_at"), now),
        )
        self._record(
            identity,
            miniapp_journey_interval_seconds=JOURNEY_INTERVAL_SECONDS,
            miniapp_journey_next_run_time=(now + timedelta(seconds=wait)).strftime(TIME_FORMAT),
        )

    def _schedule(self, identity: str, seconds: int, now: datetime | None = None) -> int:
        now = now or datetime.now()
        wait = max(5, int(seconds), self._local_cooldown(identity, now))
        self._record(
            identity,
            miniapp_journey_next_run_time=(now + timedelta(seconds=wait)).strftime(TIME_FORMAT),
        )
        return wait

    def _counter_wait(self, counter: dict[str, Any], now: datetime) -> int:
        wait = int(counter.get("remaining_seconds") or 0)
        if counter.get("daily_remaining") == 0:
            # Some servers may still report an explicit quota. Respect it,
            # but never invent a two-per-day quota when those fields are absent.
            reset = _seconds_until_timestamp(counter.get("reset_at"), now)
            if not reset:
                midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                reset = math.ceil((midnight - now).total_seconds())
            wait = max(wait, reset)
        return max(5, wait or self.retry_seconds)

    async def _refresh_counter(self, identity: str, now: datetime) -> dict[str, Any]:
        payload = await self.transport.journey_snapshot(identity)
        apply_dwelling_snapshot(self.actor, identity, payload)
        counter = journey_counter(payload, now=now)
        self._record_counter(identity, counter)
        self._observe_summary_counter(identity, counter, now)
        return counter

    async def run_identity(
        self,
        identity: str,
        now: datetime | None = None,
    ) -> tuple[bool, int]:
        """Run at most one due action; return (performed, next_wait_seconds)."""
        now = now or datetime.now()
        self._emit_daily_summary(identity, now)
        if command_paused(self.actor, JOURNEY, identity):
            return False, 60
        self._migrate_schedule(identity, now)
        wait = _seconds_until_timestamp(
            self._state(identity).get("miniapp_journey_next_run_time"),
            now,
        )
        if wait > 0:
            return False, wait
        pause_seconds = self._identity_pause_seconds(identity)
        if pause_seconds > 0:
            return False, max(60, pause_seconds)
        pause = getattr(self.actor, "pause_event", None)
        if pause is not None:
            await pause.wait()
        if command_paused(self.actor, JOURNEY, identity):
            return False, 60

        counter = await self._refresh_counter(identity, now)
        if not counter.get("present"):
            raise MiniAppBeastError("wild_experience_state_missing")
        if not counter["available"]:
            wait = self._counter_wait(counter, now)
            if counter.get("daily_remaining") != 0 and not counter.get("remaining_seconds"):
                self._record_error(identity, "wild_experience_unavailable")
            else:
                self._record(identity, miniapp_journey_last_error="")
            return False, self._schedule(identity, wait, now)

        prefix_result = ""
        use_destiny_prefix = self._identity_sect(identity) == "天星宗"
        if use_destiny_prefix:
            ensure_destiny = getattr(self.actor, "ensure_tianxing_destiny_for_action", None)
            if callable(ensure_destiny) and not await ensure_destiny(identity, "exploration"):
                self._record_error(identity, "tianxing_destiny_failed")
                return False, self._schedule(identity, self.retry_seconds)
            combined = getattr(self.transport, "journey_with_destiny_prefix", None)
            if callable(combined):
                prefix, payload = await combined(
                    identity, prefix_command=JOURNEY_PREFIX_COMMAND, mode=JOURNEY_MODE,
                    log_operation=False,
                )
            else:
                prefix = await self.transport.command(
                    JOURNEY_PREFIX_COMMAND, identity=identity, log_operation=False,
                )
                payload = None
            if not command_result_ok(prefix.payload):
                detail = command_result_text(prefix.payload) or prefix.text or "改命探索前置失败"
                self._record_error(identity, "journey_destiny_prefix_failed", detail)
                self.log.error("Mini App journey prefix failed for %s; deep action was blocked: %s", identity, detail)
                return False, self._schedule(identity, self.retry_seconds)
            prefix_result = command_result_text(prefix.payload) or prefix.text
            if payload is None:
                payload = await self.transport.journey_action(identity, mode=JOURNEY_MODE, log_operation=False)
        else:
            payload = await self.transport.journey_action(identity, mode=JOURNEY_MODE, log_operation=False)
        if not command_result_ok(payload):
            detail = miniapp_operation_result_text(payload) or "历练未完成"
            self._record_error(identity, "wild_experience_failed", detail)
            self.log.error("Mini App journey failed for %s: %s", identity, detail)
            return False, self._schedule(identity, self.retry_seconds)

        apply_dwelling_snapshot(self.actor, identity, payload)
        result_text = miniapp_operation_result_text(payload)
        action_now = datetime.now()
        # Persist the result with success BEFORE the follow-up read, so a
        # failed read or restart cannot lose a confirmed result in the batch.
        self._record(
            identity,
            miniapp_journey_last_time=action_now.strftime(TIME_FORMAT),
            miniapp_journey_next_run_time=action_now.strftime(TIME_FORMAT),
            miniapp_journey_last_date=action_now.strftime("%Y-%m-%d"),
            miniapp_journey_last_result=result_text,
            miniapp_journey_last_error="",
            miniapp_journey_last_prefix=JOURNEY_PREFIX_COMMAND if use_destiny_prefix else "",
            miniapp_journey_last_mode=JOURNEY_MODE,
            miniapp_journey_summary=self._summary_after_success(
                identity, result_text, prefix_result, counter,
                journey_counter(payload, now=action_now), now, action_now,
            ),
        )
        self._emit_daily_summary(identity, action_now)
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder) and result_text:
            try:
                recorder(identity, ".游历 深入", result_text, source="Mini App 游历·深入", final=True)
            except Exception:
                self.log.warning("Mini App journey reward recording failed for %s", identity, exc_info=True)

        counter = await self._refresh_counter(identity, datetime.now())
        wait = self._local_cooldown(identity, datetime.now())
        if counter.get("present") and not counter["available"]:
            wait = max(wait, self._counter_wait(counter, datetime.now()))
        return True, self._schedule(identity, wait)

    async def run_once(self, now: datetime | None = None) -> tuple[bool, int]:
        """Check each selected identity independently against its server deadline."""
        if not self.runtime_enabled():
            return False, self.settings_reload_seconds
        circuit_error = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
        if circuit_error is not None:
            raise circuit_error
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

        performed = False
        waits = []
        for identity in identities:
            try:
                did_run, wait = await self.run_identity(identity, now=now)
                performed = performed or did_run
            except asyncio.CancelledError:
                raise
            except CommandControlPaused:
                wait = 60
            except MiniAppCircuitOpenError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                recovered = False
                wait = self.retry_seconds
                if code in {"wild_experience_daily_limit", "wild_experience_unavailable", "wild_experience_cooldown"}:
                    try:
                        counter = await self._refresh_counter(identity, datetime.now())
                        if counter.get("present") and not counter["available"]:
                            wait = self._counter_wait(counter, datetime.now())
                            self._record(identity, miniapp_journey_last_error="")
                            recovered = True
                    except (asyncio.CancelledError, MiniAppCircuitOpenError):
                        raise
                    except Exception:
                        pass
                if not recovered:
                    self._record_error(identity, code)
                    self.log.error("Mini App journey failed for %s: %s", identity, code, exc_info=True)
                wait = self._schedule(identity, wait)
            waits.append(max(5, int(wait)))
        self._record_root(
            miniapp_journey_last_cycle_time=datetime.now().strftime(TIME_FORMAT),
            miniapp_journey_identities=identities,
        )
        return performed, min(waits) if waits else self.retry_seconds

    async def run_loop(self) -> None:
        if not self.supported:
            return
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        while getattr(self.actor, "is_running", True):
            identities = []
            if not self.runtime_enabled() or not self.configured_identities():
                wait = self.settings_reload_seconds
                self._record_root(miniapp_journey_last_error="", miniapp_journey_identities=[])
            else:
                try:
                    _, wait = await self.run_once()
                    identities = self.identities()
                except asyncio.CancelledError:
                    raise
                except MiniAppCircuitOpenError as exc:
                    wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
                    self._record_root(
                        miniapp_journey_last_error=exc.code,
                        miniapp_journey_last_error_time=datetime.now().strftime(TIME_FORMAT),
                    )
                    self.log.info("Mini App journey cycle paused by upstream circuit until %s", exc.retry_at or f"in {wait}s")
                except Exception as exc:
                    code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                    self._record_root(
                        miniapp_journey_last_error=code,
                        miniapp_journey_last_error_time=datetime.now().strftime(TIME_FORMAT),
                    )
                    self.log.error("Mini App journey cycle failed: %s", code, exc_info=True)
                    wait = self.retry_seconds
            self._record_next_schedule(datetime.now() + timedelta(seconds=wait), identities)
            # Re-read participation and pause switches promptly. run_identity
            # consults its persisted cooldown before any network request.
            await asyncio.sleep(min(max(5, wait), self.settings_reload_seconds))
