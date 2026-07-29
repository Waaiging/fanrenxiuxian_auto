#!/usr/bin/env python3
"""Shared daily Mini App automation for pagoda and dwelling treasure hunts."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import Any

from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import identity_state


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_HUNT_HOUR = 7
DEFAULT_PAGODA_HOUR = 23
DEFAULT_DAILY_RETRY_SECONDS = 15 * 60
DEFAULT_TARGET_REWARD = "阴凝之晶"
ACCOUNT_MINUTE_OFFSETS = {
    "main": 0,
    "sub": 10,
    "xiaohao": 20,
    "waaiging": 30,
}
HUNT_DIRECTION_RE = re.compile(
    r"灵气流向\s*(东北|北东|东南|南东|西北|北西|西南|南西|北|南|东|西|此地)"
)
HUNT_DIRECTION_ALIASES = {
    "东北": "北东",
    "东南": "南东",
    "西北": "北西",
    "西南": "南西",
}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def hunt_counter(payload: Any) -> dict[str, int]:
    dwelling = payload.get("dwelling") if isinstance(payload, dict) else {}
    dwelling = dwelling if isinstance(dwelling, dict) else {}
    hunt = dwelling.get("hunt") if isinstance(dwelling.get("hunt"), dict) else {}
    limit = max(0, int(hunt.get("limit") or 0))
    used = max(0, int(hunt.get("used") or 0))
    remaining = hunt.get("remaining")
    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        remaining = max(0, limit - used)
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, remaining),
        "action_points": max(0, int(hunt.get("actionPoints") or 0)),
    }


def hunt_run(payload: Any) -> dict[str, Any]:
    run = payload.get("huntRun") if isinstance(payload, dict) else None
    return run if isinstance(run, dict) else {}


def hunt_loot_contains(run: Any, target: str) -> bool:
    target = str(target or "").strip()
    if not target or not isinstance(run, dict):
        return False
    for item in run.get("loot") or []:
        if not isinstance(item, dict):
            continue
        if target in str(item.get("name") or ""):
            return True
    return False


def hunt_result_loot(payload: Any) -> dict[str, int]:
    result = payload.get("huntResult") if isinstance(payload, dict) else {}
    result = result if isinstance(result, dict) else {}
    loot = result.get("loot") if isinstance(result.get("loot"), list) else []
    merged: dict[str, int] = {}
    for item in loot:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "物品").strip() or "物品"
        try:
            quantity = max(1, int(item.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        merged[name] = merged.get(name, 0) + quantity
    return merged


def normalize_hunt_loot(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for raw_name, raw_quantity in value.items():
        name = str(raw_name or "").strip()
        if not name:
            continue
        try:
            quantity = int(raw_quantity or 0)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            normalized[name] = quantity
    return normalized


def merge_hunt_loot(total: Any, payload: Any) -> dict[str, int]:
    merged = normalize_hunt_loot(total)
    for name, quantity in hunt_result_loot(payload).items():
        merged[name] = merged.get(name, 0) + quantity
    return merged


def hunt_loot_text(total: Any) -> str:
    loot = normalize_hunt_loot(total)
    return "，".join(f"{name} x{quantity}" for name, quantity in loot.items()) or "无物品"


def pagoda_result_text(payload: Any) -> str:
    replay = payload.get("replay") if isinstance(payload, dict) else {}
    replay = replay if isinstance(replay, dict) else {}
    report = str(replay.get("report") or "").strip()
    if report:
        return report
    return (
        f"琉璃问心塔通过 {int(replay.get('clearedCount') or 0)} 层，"
        f"抵达第 {int(replay.get('endFloor') or 0)} 层，"
        f"止步第 {int(replay.get('failedFloor') or 0)} 层"
    )


def _cell_direction(from_index: int, to_index: int, size: int) -> str:
    from_row, from_col = divmod(from_index, size)
    to_row, to_col = divmod(to_index, size)
    vertical = "北" if to_row < from_row else "南" if to_row > from_row else ""
    horizontal = "西" if to_col < from_col else "东" if to_col > from_col else ""
    return vertical + horizontal or "此地"


def _hunt_hint_data(run: dict[str, Any]) -> tuple[dict[int, str], list[tuple[int, str]]]:
    markers: dict[int, str] = {}
    constraints: list[tuple[int, str]] = []
    cells = run.get("cells") if isinstance(run.get("cells"), list) else []
    for cell in cells:
        if not isinstance(cell, dict) or not cell.get("revealed"):
            continue
        hint = cell.get("hint") if isinstance(cell.get("hint"), dict) else {}
        if not hint:
            continue
        try:
            source_index = int(cell.get("index"))
        except (TypeError, ValueError):
            continue
        match = HUNT_DIRECTION_RE.search(str(hint.get("text") or ""))
        if match and match.group(1) != "此地":
            direction = HUNT_DIRECTION_ALIASES.get(match.group(1), match.group(1))
            constraints.append((source_index, direction))
        for marker in hint.get("markers") or []:
            if not isinstance(marker, dict):
                continue
            try:
                marker_index = int(marker.get("index"))
            except (TypeError, ValueError):
                continue
            kind = str(marker.get("kind") or "").strip().casefold()
            if kind in {"risk", "treasure", "resource"}:
                markers[marker_index] = kind
    return markers, constraints


def choose_hunt_cell(run: Any, rng: Any = None) -> int | None:
    """Choose the next cell, prioritizing yellow treasure hints and avoiding risk."""
    if not isinstance(run, dict) or int(run.get("ap") or 0) <= 0:
        return None
    cells = run.get("cells") if isinstance(run.get("cells"), list) else []
    available = []
    for cell in cells:
        if not isinstance(cell, dict) or cell.get("revealed"):
            continue
        try:
            available.append(int(cell.get("index")))
        except (TypeError, ValueError):
            continue
    if not available:
        return None

    size = max(1, int(run.get("size") or 5))
    markers, constraints = _hunt_hint_data(run)
    constrained = [
        index
        for index in available
        if all(_cell_direction(source, index, size) == direction for source, direction in constraints)
    ]
    if constraints and not constrained:
        constrained = list(available)

    treasure = [index for index in available if markers.get(index) == "treasure"]
    treasure_constrained = [index for index in treasure if index in constrained]
    safe = [index for index in available if markers.get(index) not in {"risk", "resource"}]
    safe_constrained = [index for index in constrained if index in safe]
    unmarked_safe = [index for index in safe if index not in markers]

    if treasure_constrained:
        pool = treasure_constrained
    elif treasure:
        pool = treasure
    elif constraints and safe_constrained:
        pool = safe_constrained
    elif markers and unmarked_safe:
        pool = unmarked_safe
    elif safe:
        pool = safe
    else:
        pool = available
    chooser = rng or random.SystemRandom()
    return int(chooser.choice(sorted(pool)))


class MiniAppDailyActivities:
    """Run daily pagoda and treasure-hunt actions for every mapped identity."""

    def __init__(
        self,
        actor: Any,
        transport: Any,
        account: str,
        logger: Any,
    ) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        config = getattr(actor, "config", {}) or {}
        settings = config.get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        default_minute = ACCOUNT_MINUTE_OFFSETS.get(self.account, 0)
        self.pagoda_enabled = bool(settings.get("pagoda_daily_enabled", True))
        self.hunt_enabled = bool(settings.get("hunt_daily_enabled", True))
        self.pagoda_hour = _bounded_int(
            settings.get("pagoda_daily_hour"), DEFAULT_PAGODA_HOUR, 0, 23
        )
        self.pagoda_minute = _bounded_int(
            settings.get("pagoda_daily_minute"), default_minute, 0, 59
        )
        self.hunt_hour = _bounded_int(
            settings.get("hunt_daily_hour"), DEFAULT_HUNT_HOUR, 0, 23
        )
        self.hunt_minute = _bounded_int(
            settings.get("hunt_daily_minute"), default_minute, 0, 59
        )
        self.retry_seconds = max(
            60,
            int(settings.get("miniapp_daily_retry_seconds") or DEFAULT_DAILY_RETRY_SECONDS),
        )
        self.target_reward = str(
            settings.get("hunt_target_reward") or DEFAULT_TARGET_REWARD
        ).strip()
        self.rng = random.SystemRandom()

    def identities(self) -> list[str]:
        ids = getattr(self.transport, "identity_player_ids", {}) or {}
        result = []
        for identity in ["主魂", *(getattr(self.actor, "avatars", []) or [])]:
            key = str(identity or "主魂").strip() or "主魂"
            if key in ids or key.casefold() in ids:
                result.append(key)
        return result

    def _save(self) -> None:
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App daily activity state save failed", exc_info=True)

    def _state(self, identity: str) -> dict[str, Any]:
        return identity_state(self.actor, identity)

    def _record(self, identity: str, **updates: Any) -> None:
        self._state(identity).update(updates)
        self._save()

    def _record_next_schedule(self, feature: str, target: datetime) -> None:
        state = getattr(self.actor, "state", None)
        if not isinstance(state, dict):
            return
        state[f"miniapp_{feature}_next_run_time"] = target.strftime(TIME_FORMAT)
        self._save()

    def _identity_pause_seconds(self, identity: str) -> int:
        resolver = getattr(self.actor, "identity_pause_seconds", None)
        if not callable(resolver):
            return 0
        try:
            return max(0, int(resolver(identity) or 0))
        except Exception:
            return 0

    async def _wait_until_runnable(self) -> None:
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        pause = getattr(self.actor, "pause_event", None)
        if pause is not None:
            await pause.wait()

    def _record_error(self, identity: str, feature: str, exc: Exception) -> None:
        code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
        now = datetime.now().strftime(TIME_FORMAT)
        self._record(
            identity,
            **{
                f"miniapp_{feature}_last_error": code,
                f"miniapp_{feature}_last_error_time": now,
            },
        )
        self.log.error(
            "Mini App %s failed for %s: %s",
            feature,
            identity,
            code,
            exc_info=True,
        )

    def _hunt_summary_loot(self, identity: str, today: str) -> dict[str, int]:
        state = self._state(identity)
        if state.get("miniapp_hunt_summary_date") != today:
            state["miniapp_hunt_summary_date"] = today
            state["miniapp_hunt_summary_loot"] = {}
            state["miniapp_hunt_summary_completed"] = 0
            state["miniapp_hunt_summary_logged_date"] = ""
            state["miniapp_hunt_last_result"] = ""
            self._save()
        return normalize_hunt_loot(state.get("miniapp_hunt_summary_loot"))

    def _log_hunt_summary(
        self,
        identity: str,
        today: str,
        counter: dict[str, int],
    ) -> str:
        state = self._state(identity)
        summary = hunt_loot_text(state.get("miniapp_hunt_summary_loot"))
        if state.get("miniapp_hunt_summary_logged_date") == today:
            return summary
        limit = int(counter.get("limit") or 3)
        operation = f"洞府寻宝（每日 {limit} 局）"
        self.log.info("OUT [Mini App | %s]:\n%s", identity, operation)
        self.log.info("IN [Mini App | %s]:\n%s -> %s", identity, operation, summary)
        self._record(
            identity,
            miniapp_hunt_summary_logged_date=today,
            miniapp_hunt_last_result=summary,
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder):
            recorder(
                identity,
                ".洞府寻宝",
                f"三局总获得：{summary}",
                source="Mini App 洞府寻宝",
                final=True,
            )
        return summary

    async def run_pagoda_identity(self, identity: str, today: str | None = None) -> str:
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self._state(identity)
        if state.get("miniapp_pagoda_last_date") == today:
            return "done"
        if self._identity_pause_seconds(identity) > 0:
            return "paused"

        snapshot = await self.transport.pagoda_snapshot(identity)
        pagoda = snapshot.get("state") if isinstance(snapshot, dict) else {}
        pagoda = pagoda if isinstance(pagoda, dict) else {}
        attempted = bool(
            int(pagoda.get("todayHighest") or 0) > 0
            or int(pagoda.get("failedFloor") or 0) > 0
            or int(pagoda.get("resetsToday") or 0) > 0
        )
        if attempted:
            now = datetime.now().strftime(TIME_FORMAT)
            self._record(
                identity,
                miniapp_pagoda_last_date=today,
                miniapp_pagoda_last_time=now,
                miniapp_pagoda_last_result="服务器已记录今日登塔",
                miniapp_pagoda_last_error="",
                last_tower_date=today,
            )
            return "already"
        if not pagoda.get("canChallenge"):
            self._record(
                identity,
                miniapp_pagoda_last_error="pagoda_not_ready",
                miniapp_pagoda_last_error_time=datetime.now().strftime(TIME_FORMAT),
            )
            return "retry"

        payload = await self.transport.pagoda_challenge(identity)
        text = pagoda_result_text(payload)
        now = datetime.now().strftime(TIME_FORMAT)
        replay = payload.get("replay") if isinstance(payload, dict) else {}
        replay = replay if isinstance(replay, dict) else {}
        self._record(
            identity,
            miniapp_pagoda_last_date=today,
            miniapp_pagoda_last_time=now,
            miniapp_pagoda_last_result=text[:1000],
            miniapp_pagoda_last_end_floor=int(replay.get("endFloor") or 0),
            miniapp_pagoda_last_failed_floor=int(replay.get("failedFloor") or 0),
            miniapp_pagoda_last_error="",
            last_tower_date=today,
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder) and text:
            recorder(identity, ".闯塔", text, source="Mini App 琉璃问心塔", final=True)
        return "challenged"

    async def run_pagoda_daily_once(self, now: datetime | None = None) -> bool:
        await self.transport.initialize()
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        complete = True
        for identity in self.identities():
            try:
                result = await self.run_pagoda_identity(identity, today=today)
                if result in {"paused", "retry"}:
                    complete = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                complete = False
                self._record_error(identity, "pagoda", exc)
            await asyncio.sleep(1)
        return complete

    async def play_hunt_session(
        self,
        identity: str,
        run: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        reason = "status_closed"
        for _ in range(25):
            if hunt_loot_contains(run, self.target_reward):
                return run, "target_reward"
            if run.get("foundMain"):
                return run, "main_found"
            if str(run.get("status") or "active") != "active":
                return run, "status_closed"
            if int(run.get("ap") or 0) <= 0:
                return run, "ap_depleted"
            index = choose_hunt_cell(run, rng=self.rng)
            if index is None:
                return run, "no_cell"
            payload = await self.transport.hunt_reveal(
                identity,
                str(run.get("sessionId") or ""),
                index,
            )
            next_run = hunt_run(payload)
            if not next_run:
                raise MiniAppBeastError("hunt_session_missing")
            run = next_run
            self._record(
                identity,
                miniapp_hunt_last_action="reveal",
                miniapp_hunt_last_action_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_last_cell=index,
                miniapp_hunt_last_ap=int(run.get("ap") or 0),
                miniapp_hunt_last_error="",
            )
            await asyncio.sleep(0.2)
        return run, reason

    async def run_hunt_identity(self, identity: str, today: str | None = None) -> str:
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self._state(identity)
        if state.get("miniapp_hunt_last_date") == today:
            return "done"
        if self._identity_pause_seconds(identity) > 0:
            return "paused"

        snapshot = await self.transport.hunt_snapshot(identity)
        dwelling = snapshot.get("dwelling") if isinstance(snapshot, dict) else {}
        dwelling = dwelling if isinstance(dwelling, dict) else {}
        if dwelling.get("hasDwelling") is False:
            self._record(
                identity,
                miniapp_hunt_last_date=today,
                miniapp_hunt_last_result="当前身份未开辟专属洞府",
                miniapp_hunt_last_error="dwelling_missing",
            )
            return "unavailable"

        counter = hunt_counter(snapshot)
        run = hunt_run(snapshot)
        summary_loot = self._hunt_summary_loot(identity, today)
        completed = 0
        if counter["remaining"] <= 0 and not run:
            if summary_loot:
                self._log_hunt_summary(identity, today, counter)
            self._record(
                identity,
                miniapp_hunt_last_date=today,
                miniapp_hunt_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_used=counter["used"],
                miniapp_hunt_limit=counter["limit"],
                miniapp_hunt_last_result=(
                    hunt_loot_text(summary_loot) if summary_loot else "今日寻宝次数已用完"
                ),
                miniapp_hunt_last_error="",
            )
            return "already"

        safety_limit = max(1, min(3, counter["remaining"] + (1 if run else 0)))
        while (run or counter["remaining"] > 0) and completed < safety_limit:
            if not run:
                started = await self.transport.hunt_start(identity)
                run = hunt_run(started)
                started_counter = hunt_counter(started)
                if started_counter["limit"] > 0:
                    counter = started_counter
                if not run:
                    raise MiniAppBeastError("hunt_session_missing")
            run, reason = await self.play_hunt_session(identity, run)
            session_id = str(run.get("sessionId") or "").strip()
            settled = await self.transport.hunt_settle(identity, session_id)
            summary_loot = merge_hunt_loot(summary_loot, settled)
            settled_counter = hunt_counter(settled)
            if settled_counter["limit"] <= 0:
                settled_counter = hunt_counter(await self.transport.hunt_snapshot(identity))
            counter = settled_counter
            completed += 1
            self._record(
                identity,
                miniapp_hunt_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_last_stop_reason=reason,
                miniapp_hunt_used=counter["used"],
                miniapp_hunt_limit=counter["limit"],
                miniapp_hunt_last_error="",
                miniapp_hunt_summary_date=today,
                miniapp_hunt_summary_loot=summary_loot,
                miniapp_hunt_summary_completed=int(
                    self._state(identity).get("miniapp_hunt_summary_completed") or 0
                ) + 1,
            )
            run = {}
            await asyncio.sleep(1)

        if counter["remaining"] <= 0:
            summary = self._log_hunt_summary(identity, today, counter)
            self._record(
                identity,
                miniapp_hunt_last_date=today,
                miniapp_hunt_last_time=datetime.now().strftime(TIME_FORMAT),
                miniapp_hunt_used=counter["used"],
                miniapp_hunt_limit=counter["limit"],
                miniapp_hunt_last_result=summary,
                miniapp_hunt_last_error="",
            )
            return "completed"
        return "retry"

    async def run_hunt_daily_once(self, now: datetime | None = None) -> bool:
        await self.transport.initialize()
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        complete = True
        for identity in self.identities():
            try:
                result = await self.run_hunt_identity(identity, today=today)
                if result in {"paused", "retry"}:
                    complete = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                complete = False
                self._record_error(identity, "hunt", exc)
            await asyncio.sleep(1)
        return complete

    @staticmethod
    def _target_datetime(now: datetime, hour: int, minute: int) -> datetime:
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def _run_daily_loop(self, feature: str, hour: int, minute: int, callback: Any) -> None:
        await self._wait_until_runnable()
        while getattr(self.actor, "is_running", True):
            now = datetime.now()
            target = self._target_datetime(now, hour, minute)
            if now < target:
                self._record_next_schedule(feature, target)
                await asyncio.sleep(max(60, int((target - now).total_seconds())))
                continue
            pause = getattr(self.actor, "pause_event", None)
            if pause is not None:
                await pause.wait()
            try:
                complete = await callback(now=now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                self.log.error(
                    "Mini App %s daily cycle failed before identity processing: %s",
                    feature,
                    code,
                    exc_info=True,
                )
                complete = False
            if complete:
                next_target = target + timedelta(days=1)
                wait = max(60, int((next_target - datetime.now()).total_seconds()))
                self._record_next_schedule(feature, next_target)
            else:
                wait = self.retry_seconds
                self._record_next_schedule(
                    feature,
                    datetime.now() + timedelta(seconds=wait),
                )
            self.log.info(
                "Mini App %s daily cycle %s; next check in %ss",
                feature,
                "complete" if complete else "needs retry",
                wait,
            )
            await asyncio.sleep(wait)

    async def run_pagoda_loop(self) -> None:
        if not self.pagoda_enabled:
            return
        await self._run_daily_loop(
            "pagoda",
            self.pagoda_hour,
            self.pagoda_minute,
            self.run_pagoda_daily_once,
        )

    async def run_hunt_loop(self) -> None:
        if not self.hunt_enabled:
            return
        await self._run_daily_loop(
            "hunt",
            self.hunt_hour,
            self.hunt_minute,
            self.run_hunt_daily_once,
        )
