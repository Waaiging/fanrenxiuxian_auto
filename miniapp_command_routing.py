#!/usr/bin/env python3
"""Hybrid command routing: supported commands are Mini App-only.

Used by non-restricted accounts (main/sub). Commands accepted by
``miniapp_command_allowed`` are executed through the Mini App dwelling
transport for every identity (no ``.切换`` round-trip needed — the transport
addresses identities by playerId). Unsupported commands keep the original
Telegram group path. A supported command never falls back to the group when
Mini App initialization or execution fails.
"""

from __future__ import annotations

from automation_command_controls import CommandControlPaused, PROFILE, STAR_FARM, command_paused

import asyncio
import json
import logging
import random
import re
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from automation_settings import star_gazing_settings
from sect_rules import SectTaskStopped, identity_sect
from miniapp_beast import (
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    miniapp_circuit_wait_seconds,
)
from miniapp_beast_abyss import MiniAppBeastAbyssWorker
from miniapp_beast_seek import MiniAppBeastSeekWorker
from miniapp_dwelling import (
    MiniAppDwellingTransport,
    apply_dwelling_snapshot,
    command_result_text,
    identity_state,
    miniapp_command_allowed,
    miniapp_operation_result_text,
    normalize_miniapp_command,
    sect_farm_action_result_ok,
    sect_farm_collect_batch_operation,
    sect_farm_collect_batch_result_text,
    sect_farm_collection_due,
    sect_farm_pull_batch_operation,
    sect_farm_pull_batch_result_text,
    sect_farm_snapshot_status,
)
from miniapp_daily_activities import MiniAppDailyActivities
from miniapp_fishing import MiniAppFishingAutomation
from miniapp_inventory import MiniAppInventoryWorker
from miniapp_journey import MiniAppTianxingJourney


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_AUTH_REFRESH_SECONDS = 6 * 3600
DEFAULT_PROFILE_REFRESH_SECONDS = 30 * 60
DEFAULT_ROUTE_RECOVERY_SECONDS = 60
DEFAULT_STAR_FARM_RETRY_SECONDS = 5 * 60
STAR_FARM_WAKE_GRACE_SECONDS = 5
DEFAULT_STAR_FARM_TARGET = "天雷星"
ROUTE_BLOCKED_LOG_SUPPRESS_SECONDS = 15 * 60
DEFAULT_STAR_SHIFT_TARGET = "@Weeguu"
STAR_GAZING_BOUNDARY_INTERVAL_HOURS = 3
STAR_GAZING_LEAD_RANGE_SECONDS = (30, 40)
STAR_PALACE_SHIFT_SAFETY_SECONDS = 3
STAR_PALACE_TIME_CRITICAL_SECONDS = 90
STAR_PALACE_SHIFT_MAX_DELAY_SECONDS = 320
STAR_PALACE_ERROR_RETRY_SECONDS = 90
STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS = 300
STAR_PALACE_SETTINGS_POLL_SECONDS = 5
STAR_PALACE_SEND_GRACE_SECONDS = 5
STAR_PALACE_EVENT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "star_gazing_events.jsonl",
)
STAR_PALACE_COORDINATION_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "star_palace_coordination.json",
)
STAR_PALACE_COORDINATION_LOCK = STAR_PALACE_COORDINATION_FILE + ".lock"
STAR_PALACE_ATTEMPT_TTL_SECONDS = 150
STAR_PALACE_GOOD_FATE_TITLES = {
    "地磁暴动",
    "星辰异象",
    "五彩缤纷",
    "封魔裂隙回响",
}


def _now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def _star_palace_manifest_key(manifest_dt: datetime) -> str:
    return manifest_dt.strftime(TIME_FORMAT)


def _parse_star_palace_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.strptime(str(value or ""), TIME_FORMAT)
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None)


def _confirmed_star_palace_good(manifest_key: str) -> dict[str, Any] | None:
    """Read a target Good notice recorded by any account."""
    try:
        with open(STAR_PALACE_EVENT_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in reversed(fh.readlines()):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(record.get("target_manifest_time") or "") != manifest_key:
                    continue
                title = str(record.get("fate_type") or "")
                if title.startswith("Good - "):
                    title = title[len("Good - "):]
                if title not in STAR_PALACE_GOOD_FATE_TITLES:
                    continue
                return record
    except OSError:
        return None
    return None


def _load_star_palace_coordination() -> dict[str, Any]:
    try:
        with open(STAR_PALACE_COORDINATION_FILE, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_star_palace_coordination(value: dict[str, Any]) -> None:
    directory = os.path.dirname(STAR_PALACE_COORDINATION_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".star_palace_", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary_path, STAR_PALACE_COORDINATION_FILE)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_star_palace_lock():
    lock_directory = os.path.dirname(STAR_PALACE_COORDINATION_LOCK) or "."
    os.makedirs(lock_directory, exist_ok=True)
    with open(STAR_PALACE_COORDINATION_LOCK, "a+", encoding="utf-8") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _claim_star_palace_attempt(account: str, identity: str, manifest_key: str, now: datetime | None = None) -> bool:
    """Atomically allow one script identity for one manifestation round."""
    now = now or datetime.now()
    own_key = f"{account}|{identity}"
    active_cutoff = now - timedelta(seconds=STAR_PALACE_ATTEMPT_TTL_SECONDS)
    with _exclusive_star_palace_lock():
        # Reload under the lock so simultaneous scripts cannot both claim.
        locked_value = _load_star_palace_coordination()
        if locked_value.get("manifest_key") != manifest_key:
            locked_value = {"manifest_key": manifest_key, "attempts": []}
        attempts = locked_value.get("attempts")
        attempts = attempts if isinstance(attempts, list) else []
        attempt_keys = {
            f"{str(item.get('account') or '')}|{str(item.get('identity') or '')}"
            for item in attempts
            if isinstance(item, dict)
        }
        if own_key in attempt_keys:
            return False
        success = locked_value.get("success")
        if (
            locked_value.get("manifest_key") == manifest_key
            and isinstance(success, dict)
            and success.get("account")
            and success.get("identity")
        ):
            return False
        active = locked_value.get("active")
        active_claimed_at = _parse_star_palace_time(active.get("claimed_at")) if isinstance(active, dict) else None
        if (
            isinstance(active, dict)
            and str(active.get("manifest_key") or "") == manifest_key
            and active_claimed_at is not None
            and active_claimed_at > active_cutoff
        ):
            return False
        attempts.append({"account": account, "identity": identity})
        locked_value["attempts"] = attempts
        locked_value["active"] = {
            "manifest_key": manifest_key,
            "account": account,
            "identity": identity,
            "claimed_at": now.strftime(TIME_FORMAT),
        }
        _save_star_palace_coordination(locked_value)
        return True


def _finish_star_palace_attempt(account: str, identity: str, manifest_key: str, success: bool) -> None:
    value = _load_star_palace_coordination()
    if value.get("manifest_key") != manifest_key:
        return
    active = value.get("active")
    if not (
        isinstance(active, dict)
        and active.get("account") == account
        and active.get("identity") == identity
        and active.get("manifest_key") == manifest_key
    ):
        return
    if success:
        value["success"] = {"account": account, "identity": identity, "completed_at": _now_text()}
    value.pop("active", None)
    _save_star_palace_coordination(value)


class MiniAppCommandRouter:
    """Route Mini App-capable commands away from the game group."""

    def __init__(
        self,
        actor: Any,
        account: str,
        logger: logging.Logger | None = None,
        *,
        transport: MiniAppDwellingTransport | None = None,
        start_background_tasks: bool = True,
    ) -> None:
        self.actor = actor
        self.account = str(account or "").strip()
        self.log = logger or logging.getLogger(f"miniapp_route.{self.account}")
        config = getattr(actor, "config", {}) or {}
        settings = config.get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        entry_url = str(settings.get("entry_url") or "").strip()
        self.enabled = (
            bool(entry_url)
            and bool(settings.get("enabled", True))
            and bool(settings.get("command_routing_enabled", True))
        )
        self.auth_refresh_seconds = max(
            1800,
            int(settings.get("auth_refresh_seconds") or DEFAULT_AUTH_REFRESH_SECONDS),
        )
        self.profile_refresh_seconds = max(
            300,
            int(settings.get("profile_refresh_seconds") or DEFAULT_PROFILE_REFRESH_SECONDS),
        )
        self.route_recovery_seconds = max(
            15,
            int(settings.get("route_recovery_seconds") or DEFAULT_ROUTE_RECOVERY_SECONDS),
        )
        self.star_farm_enabled = bool(settings.get("star_farm_enabled", True))
        self.star_farm_retry_seconds = max(
            60,
            int(settings.get("star_farm_retry_seconds") or DEFAULT_STAR_FARM_RETRY_SECONDS),
        )
        self.star_farm_target = str(
            settings.get("star_farm_target") or DEFAULT_STAR_FARM_TARGET
        ).strip()
        self.star_shift_target = str(
            settings.get("star_shift_target") or DEFAULT_STAR_SHIFT_TARGET
        ).strip()
        self.transport = transport or MiniAppDwellingTransport(
            actor.client,
            entry_url,
            bot_username=str(settings.get("bot_username") or "fanrenxiuxian_bot"),
            timeout=int(settings.get("timeout_seconds") or 20),
            logger=self.log,
            config_file=getattr(actor, "config_file", "") or getattr(actor, "CONFIG_FILE", ""),
            entry_chat=getattr(actor, "target_chat_id", "fanrenxxz"),
        )
        self.start_background_tasks = bool(start_background_tasks)
        self.transport.sect_actor = actor
        self.daily_activities = MiniAppDailyActivities(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self.tianxing_journey = MiniAppTianxingJourney(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self.fishing = MiniAppFishingAutomation(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self.beast_abyss = MiniAppBeastAbyssWorker(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self.beast_seek = MiniAppBeastSeekWorker(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self.inventory = MiniAppInventoryWorker(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self._orig_send = None
        self._orig_send_identity = None
        self._last_auth_refresh = datetime.min
        self._last_auth_refresh_failure = datetime.min
        self._route_blocked_log_times: dict[tuple[str, str], datetime] = {}
        self._route_active = False
        self._background_tasks_started = False
        self._recovery_task: asyncio.Task[Any] | None = None
        self._profile_task: asyncio.Task[Any] | None = None
        self._star_farm_tasks: list[asyncio.Task[Any]] = []
        # Star Palace has its own Mini App endpoint and must not be routed
        # through the legacy group-command scheduler.  Keep these tasks
        # account-local so main/sub accounts can run the same flow as the
        # restricted XiaoHao account without duplicate workers.
        self._star_palace_tasks: dict[str, asyncio.Task[Any]] = {}
        self._daily_activity_tasks: list[asyncio.Task[Any]] = []

    def _record(self, **updates: Any) -> None:
        state = getattr(self.actor, "state", None)
        if not isinstance(state, dict):
            return
        state.update(updates)
        state["miniapp_route_updated_at"] = _now_text()
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App route state save failed", exc_info=True)

    def _log_route_blocked(self, identity: str, command: str) -> None:
        """Report an unavailable Mini App route without flooding the log."""
        key = (str(identity or "主魂"), str(command or ""))
        now = datetime.now()
        previous = self._route_blocked_log_times.get(key, datetime.min)
        if (now - previous).total_seconds() < ROUTE_BLOCKED_LOG_SUPPRESS_SECONDS:
            return
        self._route_blocked_log_times[key] = now
        self.log.warning(
            "Mini App-only command blocked while route is unavailable [%s] %s; "
            "automatic route recovery is pending",
            identity,
            command,
        )

    async def install(self) -> bool:
        self._orig_send = self.actor.send_and_wait_feedback
        self._orig_send_identity = getattr(self.actor, "send_and_wait_feedback_identity", None)
        self.actor.send_and_wait_feedback = self._send_main
        if self._orig_send_identity is not None:
            self.actor.send_and_wait_feedback_identity = self._send_identity
        if not self.enabled:
            self._record(
                miniapp_route_active=False,
                miniapp_route_last_error="route_disabled",
            )
            self.log.error(
                "Mini App command routing disabled for %s; supported commands are blocked",
                self.account,
            )
            return False
        if await self._initialize_route():
            return True
        self._start_recovery_task()
        return False

    async def _initialize_route(self, *, recovery: bool = False) -> bool:
        try:
            await self.transport.initialize(force=recovery)
            self._last_auth_refresh = datetime.now()
        except MiniAppCircuitOpenError as exc:
            self._route_active = False
            wait = miniapp_circuit_wait_seconds(exc, self.route_recovery_seconds)
            self._record(
                miniapp_route_active=False,
                miniapp_route_last_error=exc.code,
                miniapp_route_retry_at=exc.retry_at,
            )
            if not recovery:
                self.log.warning(
                    "Mini App command routing paused by upstream circuit until %s",
                    exc.retry_at or f"in {wait}s",
                )
            return False
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self._route_active = False
            self._record(miniapp_route_active=False, miniapp_route_last_error=code)
            if code == "dwelling_token_expired":
                self.log.error(
                    "Mini App command routing setup failed: fixed entry token expired; "
                    "supported commands are blocked while automatic recovery retries"
                )
            else:
                log_method = self.log.warning if recovery else self.log.error
                log_method(
                    "Mini App command routing %s failed (%s); retrying in %ss",
                    "recovery" if recovery else "setup",
                    code,
                    self.route_recovery_seconds,
                    exc_info=not recovery,
                )
            return False
        await self._activate_route()
        return True

    async def _activate_route(self) -> None:
        self._route_active = True
        self._sync_avatar_dao_names_from_transport()
        known = sorted(
            name
            for name in ["主魂", *(getattr(self.actor, "avatars", []) or [])]
            if name in self.transport.identity_player_ids
        )
        self._record(
            miniapp_route_active=True,
            miniapp_route_last_error="",
            miniapp_route_identities=known,
        )
        await self.sync_all_profiles(known)
        self._reconcile_star_palace_tasks(known)
        # Overview sync is authoritative for sect membership. Select Star
        # Palace identities only after it has corrected stale local mappings.
        star_identities = self.star_farm_identities(known) if self.start_background_tasks else []
        journey_identities = (
            self.tianxing_journey.identities(known) if self.start_background_tasks else []
        )
        self._record(
            miniapp_star_farm_identities=star_identities,
            miniapp_journey_identities=journey_identities,
        )
        if self.start_background_tasks and not self._background_tasks_started:
            self._background_tasks_started = True
            self._profile_task = asyncio.create_task(
                self.run_profile_sync_loop(),
                name=f"miniapp_{self.account}_profiles",
            )
            self._daily_activity_tasks.append(
                asyncio.create_task(
                    self.inventory.run_loop(),
                    name=f"miniapp_{self.account}_inventory",
                )
            )
            for identity in star_identities:
                self._star_farm_tasks.append(
                    asyncio.create_task(
                        self.run_star_farm_loop(identity),
                        name=f"miniapp_{self.account}_star_farm_{identity}",
                    )
                )
            if self.daily_activities.pagoda_enabled:
                self._daily_activity_tasks.append(
                    asyncio.create_task(
                        self.daily_activities.run_pagoda_loop(),
                        name=f"miniapp_{self.account}_pagoda",
                    )
                )
            if self.daily_activities.hunt_enabled:
                self._daily_activity_tasks.append(
                    asyncio.create_task(
                        self.daily_activities.run_hunt_loop(),
                        name=f"miniapp_{self.account}_hunt",
                    )
                )
            self._daily_activity_tasks.append(
                asyncio.create_task(
                    self.daily_activities.run_tianji_trial_loop(),
                    name=f"miniapp_{self.account}_tianji_trial",
                )
            )
            self._daily_activity_tasks.append(
                asyncio.create_task(
                    self.daily_activities.run_fate_cards_loop(),
                    name=f"miniapp_{self.account}_fate_cards",
                )
            )
            if self.tianxing_journey.enabled:
                self._daily_activity_tasks.append(
                    asyncio.create_task(
                        self.tianxing_journey.run_loop(),
                        name=f"miniapp_{self.account}_journey",
                    )
                )
            if self.fishing.supported:
                self._daily_activity_tasks.append(
                    asyncio.create_task(
                        self.fishing.run_loop(),
                        name=f"miniapp_{self.account}_fishing",
                    )
                )
        self.log.warning(
            "[%s] Mini App command routing active for identities %s; unsupported commands stay in the group",
            self.account,
            known,
        )

    def _reconcile_star_palace_tasks(self, identities: list[str] | None = None) -> None:
        """Ensure every current 星宫 identity has exactly one Mini App worker.

        XiaoHao already exposes an account-level reconciliation hook because
        its restricted worker has additional task bookkeeping.  Main/sub use
        this router-owned registry directly.  In both cases, a sect change
        cancels the old identity's worker before any group command can be
        attempted.
        """
        if not self.start_background_tasks or not self.enabled:
            return
        candidates = list(identities) if identities is not None else self.routable_identities()
        actor_reconcile = getattr(self.actor, "reconcile_miniapp_star_palace_tasks", None)
        if callable(actor_reconcile):
            try:
                actor_reconcile(candidates)
            except Exception:
                self.log.warning("Mini App Star Palace task reconciliation failed", exc_info=True)
            return

        # Some accounts share the same Mini App settings but must never run
        # Star Palace automation.  Respect the actor-level capability before
        # looking at persisted sect mappings or stale route state.
        if getattr(self.actor, "enable_miniapp_star_palace", True) is False:
            desired = set()
        else:
            desired = set(self.star_farm_identities(candidates))
        for identity in desired:
            task = self._star_palace_tasks.get(identity)
            if task is None or task.done():
                self._star_palace_tasks[identity] = asyncio.create_task(
                    self.run_star_palace_divine_loop(identity),
                    name=f"miniapp_{self.account}_star_palace_{identity}",
                )
        for identity, task in list(self._star_palace_tasks.items()):
            if identity not in desired:
                if task is not None and not task.done():
                    task.cancel()
                self._star_palace_tasks.pop(identity, None)

    def _start_recovery_task(self) -> None:
        if not self.start_background_tasks or not self.enabled:
            return
        if self._recovery_task is not None and not self._recovery_task.done():
            return
        self._recovery_task = asyncio.create_task(
            self.run_route_recovery_loop(),
            name=f"miniapp_{self.account}_route_recovery",
        )

    async def run_route_recovery_loop(self) -> None:
        while getattr(self.actor, "is_running", True) and not self._route_active:
            wait = self.route_recovery_seconds
            try:
                from miniapp_beast import miniapp_circuit_preflight

                probe = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
                if probe is not None:
                    wait = miniapp_circuit_wait_seconds(probe, wait)
            except Exception:
                pass
            await asyncio.sleep(max(self.route_recovery_seconds, wait))
            if not getattr(self.actor, "is_running", True):
                return
            if await self._initialize_route(recovery=True):
                self.log.warning(
                    "[%s] Mini App command routing recovered after an earlier setup failure",
                    self.account,
                )
                return

    def routable_identities(self) -> list[str]:
        return [
            name
            for name in ["主魂", *(getattr(self.actor, "avatars", []) or [])]
            if self._identity_routable(name)
        ]

    def _sync_avatar_dao_names_from_transport(self) -> int:
        """Use dwelling player IDs to refresh avatar Dao names after rebirth."""
        refresh = getattr(self.actor, "refresh_avatar_dao_name", None)
        if not callable(refresh):
            return 0
        changed = 0
        for choice in getattr(self.transport, "identity_choices", []) or []:
            if not isinstance(choice, dict):
                continue
            dao_name = str(choice.get("daoName") or "").strip()
            player_id = choice.get("playerId")
            if not dao_name or player_id is None:
                continue
            before = list(getattr(self.actor, "avatars", []) or [])
            refresh("", dao_name, player_id=player_id)
            changed += before != list(getattr(self.actor, "avatars", []) or [])
        return changed

    async def sync_all_profiles(self, identities: list[str] | None = None) -> int:
        self._sync_avatar_dao_names_from_transport()
        synced = 0
        for identity in identities if identities is not None else self.routable_identities():
            if command_paused(self.actor, PROFILE, identity):
                continue
            try:
                payload = await self.transport.overview(identity)
                apply_dwelling_snapshot(self.actor, identity, payload)
                synced += 1
            except asyncio.CancelledError:
                raise
            except CommandControlPaused:
                continue
            except MiniAppCircuitOpenError as exc:
                identity_state(self.actor, identity)["miniapp_last_error"] = exc.code
                self.log.info(
                    "Mini App profile sync paused for %s while upstream circuit is open; next probe %s",
                    identity,
                    exc.retry_at or f"in {exc.retry_after}s",
                )
                break
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                identity_state(self.actor, identity)["miniapp_last_error"] = code
                self.log.warning(
                    "Mini App profile sync failed for %s (%s)",
                    identity,
                    code,
                )
        self._record(
            miniapp_profile_last_sync_time=_now_text(),
            miniapp_profile_identity_count=synced,
        )
        return synced

    async def run_profile_sync_loop(self) -> None:
        while getattr(self.actor, "is_running", True):
            await asyncio.sleep(self.profile_refresh_seconds)
            if not getattr(self.actor, "is_running", True):
                return
            await self.sync_all_profiles()
            self._reconcile_star_palace_tasks()

    def star_farm_identities(self, identities: list[str] | None = None) -> list[str]:
        """Return routable identities whose configured sect is Star Palace."""
        if not self.star_farm_enabled:
            return []
        candidates = identities if identities is not None else self.routable_identities()
        mapping = getattr(self.actor, "identity_sect_names", {}) or {}
        resolver = getattr(self.actor, "identity_sect_name", None)
        result = []
        for identity in candidates:
            sect = ""
            if callable(resolver):
                try:
                    sect = str(resolver(identity) or "").strip()
                except Exception:
                    sect = ""
            if not sect and isinstance(mapping, dict):
                sect = str(mapping.get(identity) or "").strip()
            if sect == "星宫" and self._identity_routable(identity):
                result.append(identity)
        return result

    def _save_star_state(self) -> None:
        try:
            self.actor.save_state()
        except Exception as exc:
            self.log.warning("Mini App star-farm state save failed", exc_info=True)

    def _record_star_snapshot(
        self,
        identity: str,
        payload: dict[str, Any],
    ) -> tuple[int, int, list[str], int]:
        snapshot = sect_farm_snapshot_status(payload)
        ready = int(snapshot["ready_count"])
        troubled = int(snapshot["troubled_count"])
        empty = list(snapshot["empty_keys"])
        next_wait = int(snapshot["next_wait_seconds"])
        next_collect_time = (
            (datetime.now() + timedelta(seconds=next_wait + STAR_FARM_WAKE_GRACE_SECONDS)).strftime(TIME_FORMAT)
            if next_wait > 0
            else ""
        )
        state = identity_state(self.actor, identity)
        state.update({
            "star_miniapp_last_sync_time": _now_text(),
            "star_miniapp_ready_count": ready,
            "star_miniapp_troubled_count": troubled,
            "star_miniapp_empty_count": len(empty),
            "star_miniapp_next_collect_time": next_collect_time,
            "star_miniapp_next_wait_seconds": next_wait,
            "star_miniapp_last_error": "",
            "last_star_observatory_time": _now_text(),
            "star_observatory_needs_refresh": False,
            "next_star_collect_time": next_collect_time,
            "next_star_appease_time": next_collect_time,
            "next_star_check_time": next_collect_time,
            "star_observatory_summary": (
                f"Mini App: 可收集 {ready}，需安抚 {troubled}，空盘 {len(empty)}"
                f"，下次采集 {next_collect_time or '待结算'}"
            ),
        })
        self._save_star_state()
        return ready, troubled, empty, next_wait

    async def _star_farm_action(
        self,
        identity: str,
        action: str,
        plot_key: str = "",
        log_operation: bool = True,
    ) -> tuple[dict[str, Any], tuple[int, int, list[str], int]]:
        action_kwargs = {
            "plot_key": plot_key,
            "star_name": self.star_farm_target if action == "pull" else "",
        }
        if not log_operation:
            action_kwargs["log_operation"] = False
        payload = await self.transport.sect_farm_action(identity, action, **action_kwargs)
        if not sect_farm_action_result_ok(payload, action):
            raise MiniAppBeastError(f"star_farm_{action}_failed")
        now = _now_text()
        text = command_result_text(payload) or miniapp_operation_result_text(payload)
        state = identity_state(self.actor, identity)
        state.update({
            "star_miniapp_last_action": action,
            "star_miniapp_last_action_time": now,
            "star_miniapp_last_result": text,
        })
        if action == "soothe":
            state["last_calm_time"] = now
            state["last_star_appease_time"] = now
        elif action == "collect":
            state["last_collection_time"] = now
            state["last_star_collect_time"] = now
            recorder = getattr(self.actor, "record_daily_reward_event", None)
            if callable(recorder) and text:
                try:
                    recorder(identity, ".收集精华", text, source="Mini App 收集精华")
                except Exception:
                    self.log.warning(
                        "Mini App star essence reward recording failed for %s",
                        identity,
                        exc_info=True,
                    )
        elif action == "pull":
            state["star_attraction_start_time"] = now
            state["last_star_attraction_time"] = now
        self._save_star_state()
        return payload, self._record_star_snapshot(identity, payload)

    async def run_star_farm_cycle(self, identity: str) -> int:
        """Run one due batch and return seconds until the next collection."""
        if command_paused(self.actor, STAR_FARM, identity):
            return 60
        payload = await self.transport.sect_farm_snapshot(identity)
        ready, troubled, empty, next_wait = self._record_star_snapshot(identity, payload)
        collection_due = sect_farm_collection_due(ready, troubled, next_wait)
        if collection_due and not command_paused(self.actor, STAR_FARM + "-collect", identity):
            # One maintenance action immediately before the batch collection;
            # no fixed-interval soothing or polling while stars are maturing.
            _, (ready, troubled, empty, next_wait) = await self._star_farm_action(
                identity,
                "soothe",
            )
            if ready > 0 and sect_farm_collection_due(ready, troubled, next_wait):
                requested_count = ready
                empty_before = set(empty)
                operation = sect_farm_collect_batch_operation(requested_count)
                self.log.info("OUT [Mini App | %s]:\n%s", identity, operation)
                collect_payload, (ready, troubled, empty, next_wait) = await self._star_farm_action(
                    identity,
                    "collect",
                    log_operation=False,
                )
                empty_after = set(empty)
                confirmed_count = len(empty_after - empty_before)
                state = identity_state(self.actor, identity)
                state.update({
                    "star_miniapp_last_collect_requested_count": requested_count,
                    "star_miniapp_last_collect_confirmed_count": confirmed_count,
                    "star_miniapp_last_collect_empty_count": len(empty_after),
                })
                self._save_star_state()
                self.log.info(
                    "IN [Mini App | %s]:\n%s -> %s",
                    identity,
                    operation,
                    sect_farm_collect_batch_result_text(
                        requested_count,
                        confirmed_count,
                        len(empty_after),
                        command_result_text(collect_payload)
                        or miniapp_operation_result_text(collect_payload),
                    ),
                )
        # Keep already-empty plots idle while any sibling plot in the same
        # Tianlei batch is still maturing.  Once the remainder is collected,
        # all empty plots are pulled together and their timers realign.
        pull_keys = list(empty) if next_wait <= 0 else []
        if pull_keys and not command_paused(self.actor, STAR_FARM + "-pull", identity):
            operation = sect_farm_pull_batch_operation(pull_keys)
            self.log.info("OUT [Mini App | %s]:\n%s", identity, operation)
            pull_results = []
            for plot_key in pull_keys:
                pull_payload, (ready, troubled, _, next_wait) = await self._star_farm_action(
                    identity,
                    "pull",
                    plot_key=plot_key,
                    log_operation=False,
                )
                pull_results.append(
                    command_result_text(pull_payload) or miniapp_operation_result_text(pull_payload)
                )
                await asyncio.sleep(1)
            self.log.info(
                "IN [Mini App | %s]:\n%s -> %s",
                identity,
                operation,
                sect_farm_pull_batch_result_text(
                    pull_keys,
                    self.star_farm_target,
                    pull_results,
                ),
            )
        if next_wait > 0:
            return next_wait + STAR_FARM_WAKE_GRACE_SECONDS
        return self.star_farm_retry_seconds

    async def run_star_farm_loop(self, identity: str) -> None:
        startup_done = getattr(self.actor, "startup_done", None)
        if startup_done is not None:
            await startup_done.wait()
        while getattr(self.actor, "is_running", True):
            wait = self.star_farm_retry_seconds
            try:
                pause_event = getattr(self.actor, "pause_event", None)
                if pause_event is not None:
                    await pause_event.wait()
                pause_seconds = 0
                if hasattr(self.actor, "identity_pause_seconds"):
                    pause_seconds = int(self.actor.identity_pause_seconds(identity) or 0)
                if pause_seconds > 0:
                    wait = max(60, pause_seconds)
                else:
                    wait = await self.run_star_farm_cycle(identity)
            except asyncio.CancelledError:
                raise
            except CommandControlPaused:
                wait = 60
            except MiniAppCircuitOpenError as exc:
                state = identity_state(self.actor, identity)
                state["star_miniapp_last_error"] = exc.code
                state["star_miniapp_last_error_time"] = _now_text()
                self._save_star_state()
                wait = miniapp_circuit_wait_seconds(exc, self.star_farm_retry_seconds)
                self.log.info(
                    "Mini App star-farm paused while upstream circuit is open; next probe %s",
                    exc.retry_at or f"in {wait}s",
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                state = identity_state(self.actor, identity)
                state["star_miniapp_last_error"] = code
                state["star_miniapp_last_error_time"] = _now_text()
                self._save_star_state()
                self.log.error(
                    "Mini App star-farm loop failed for %s: %s",
                    identity,
                    code,
                    exc_info=True,
                )
                wait = self.star_farm_retry_seconds
            await asyncio.sleep(max(60, int(wait)))

    @staticmethod
    def _next_star_boundary(now: datetime | None = None) -> datetime:
        now = now or datetime.now()
        boundary = now.replace(
            hour=(now.hour // STAR_GAZING_BOUNDARY_INTERVAL_HOURS)
            * STAR_GAZING_BOUNDARY_INTERVAL_HOURS,
            minute=0,
            second=0,
            microsecond=0,
        )
        if now >= boundary:
            boundary += timedelta(hours=STAR_GAZING_BOUNDARY_INTERVAL_HOURS)
        return boundary

    @staticmethod
    def _star_action_mapping(value: Any, *keys: str) -> dict[str, Any]:
        current = value if isinstance(value, dict) else {}
        for key in keys:
            current = current.get(key) if isinstance(current, dict) else None
            current = current if isinstance(current, dict) else {}
        return current

    @staticmethod
    def _star_remaining_seconds(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        match = re.search(r"\d+", str(value))
        return max(0, int(match.group(0))) if match else None

    def _save_star_palace_identity_state(self, identity: str = "主魂", **updates: Any) -> None:
        state = identity_state(self.actor, identity)
        if isinstance(state, dict):
            state.update(updates)
            state["miniapp_star_palace_updated_at"] = _now_text()
            root = getattr(self.actor, "state", None)
            if isinstance(root, dict):
                # Keep a compact root-level summary for older Dashboard code;
                # authoritative done/error fields remain identity-scoped.
                root["miniapp_star_palace_last_identity"] = identity
                root["miniapp_star_palace_updated_at"] = state["miniapp_star_palace_updated_at"]
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App star-palace state save failed", exc_info=True)

    async def _wait_until_star_palace(self, wake_dt: datetime) -> bool:
        """Sleep until wake time in small chunks; return False when stopped."""
        while getattr(self.actor, "is_running", True):
            pause_event = getattr(self.actor, "pause_event", None)
            if pause_event is not None:
                await pause_event.wait()
            remaining = (wake_dt - datetime.now()).total_seconds()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(
                STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS,
                max(1, int(remaining) + (1 if remaining % 1 else 0)),
            ))
        return False

    @staticmethod
    def _star_palace_send_deadline(manifest_dt: datetime, lead: int) -> datetime:
        if lead > 0:
            return manifest_dt
        return manifest_dt + timedelta(seconds=-lead + STAR_PALACE_SEND_GRACE_SECONDS)

    def _star_palace_manifest_for_send(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now()
        boundary = self._next_star_boundary(now)
        previous = boundary - timedelta(hours=STAR_GAZING_BOUNDARY_INTERVAL_HOURS)
        lead = star_gazing_settings()["lead_seconds"]
        if lead <= 0 and now < self._star_palace_send_deadline(previous, lead):
            # A delayed send still belongs to the boundary that just passed,
            # including when a worker restarts inside that scheduled window.
            return previous
        return boundary

    async def _wait_for_confirmed_star_palace_good(
        self,
        identity: str,
        manifest_dt: datetime,
    ) -> bool:
        """Wait for any account to record a target Good notice before acting."""
        manifest_key = _star_palace_manifest_key(manifest_dt)
        while getattr(self.actor, "is_running", True):
            record = _confirmed_star_palace_good(manifest_key)
            if record is not None:
                return True
            lead = star_gazing_settings()["lead_seconds"]
            remaining = (self._star_palace_send_deadline(manifest_dt, lead) - datetime.now()).total_seconds()
            if remaining <= 0:
                self.log.info(
                    "Mini App star palace [%s]: no target Good notice for %s; skip this round",
                    identity,
                    manifest_key,
                )
                return False
            await asyncio.sleep(min(STAR_PALACE_SETTINGS_POLL_SECONDS, remaining))
        return False

    async def _wait_for_star_palace_send_time(
        self,
        identity: str,
        manifest_dt: datetime,
    ) -> bool:
        """Apply saved timing changes while waiting, without holding a round claim."""
        while getattr(self.actor, "is_running", True):
            pause_event = getattr(self.actor, "pause_event", None)
            if pause_event is not None:
                await pause_event.wait()
            if not getattr(self.actor, "is_running", True):
                return False
            now = datetime.now()
            lead = star_gazing_settings()["lead_seconds"]
            if now >= self._star_palace_send_deadline(manifest_dt, lead):
                return False
            send_dt = manifest_dt - timedelta(seconds=lead)
            send_text = send_dt.strftime(TIME_FORMAT)
            state = identity_state(self.actor, identity)
            if state.get("miniapp_star_palace_next_divine_time") != send_text:
                self._save_star_palace_identity_state(
                    identity,
                    miniapp_star_palace_next_divine_time=send_text,
                    miniapp_star_palace_lead_seconds=lead,
                )
                self.log.info(
                    "Mini App star palace [%s]: 观星 scheduled at %s (lead=%ss, manifestation=%s)",
                    identity, send_text, lead, manifest_dt.strftime(TIME_FORMAT),
                )
            if send_dt <= now:
                return True
            await asyncio.sleep(min(
                STAR_PALACE_SETTINGS_POLL_SECONDS,
                (send_dt - now).total_seconds(),
            ))
        return False

    async def run_star_palace_divine_loop(self, identity: str) -> None:
        """Observe stars through the dwelling at the configured manifestation offset."""
        startup_done = getattr(self.actor, "startup_done", None)
        if startup_done is not None:
            await startup_done.wait()
        target_username = str(getattr(self, "star_shift_target", DEFAULT_STAR_SHIFT_TARGET))
        while getattr(self.actor, "is_running", True):
            try:
                pause_event = getattr(self.actor, "pause_event", None)
                if pause_event is not None:
                    await pause_event.wait()
                if hasattr(self.actor, "identity_pause_seconds"):
                    pause_seconds = int(self.actor.identity_pause_seconds(identity) or 0)
                    if pause_seconds > 0:
                        await asyncio.sleep(min(max(60, pause_seconds), STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS))
                        continue

                today = datetime.now().strftime("%Y-%m-%d")
                if identity_state(self.actor, identity).get("miniapp_star_palace_done_date") == today:
                    next_boundary = self._next_star_boundary()
                    if next_boundary.date() == datetime.now().date():
                        wait = max(60, (next_boundary + timedelta(seconds=5) - datetime.now()).total_seconds())
                    else:
                        wait = max(1, (
                            datetime.combine(datetime.now().date(), datetime.min.time())
                            + timedelta(days=1)
                            - datetime.now()
                        ).total_seconds())
                    await asyncio.sleep(min(wait, STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS))
                    continue

                manifest_dt = self._star_palace_manifest_for_send()
                manifest_key = _star_palace_manifest_key(manifest_dt)
                if not await self._wait_for_confirmed_star_palace_good(identity, manifest_dt):
                    await asyncio.sleep(min(
                        max(30, (manifest_dt + timedelta(seconds=5) - datetime.now()).total_seconds()),
                        STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS,
                    ))
                    continue
                if not await self._wait_for_star_palace_send_time(identity, manifest_dt):
                    continue
                if not _claim_star_palace_attempt(self.account, identity, manifest_key):
                    await asyncio.sleep(min(5, max(1, (manifest_dt - datetime.now()).total_seconds())))
                    continue
                succeeded = False
                try:
                    succeeded = await self.run_star_palace_cycle(
                        identity,
                        manifest_dt,
                        target_username,
                    )
                except Exception:
                    _finish_star_palace_attempt(self.account, identity, manifest_key, False)
                    raise
                finally:
                    self._save_star_palace_identity_state(
                        identity, miniapp_star_palace_next_divine_time="",
                    )
                    _finish_star_palace_attempt(
                        self.account,
                        identity,
                        manifest_key,
                        succeeded,
                    )
                # The next opportunity follows the regular three-hour boundary.
                next_wait = max(
                    30,
                    (self._next_star_boundary(manifest_dt) + timedelta(seconds=5) - datetime.now()).total_seconds(),
                )
                await asyncio.sleep(min(next_wait, STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                self._save_star_palace_identity_state(identity,
                    miniapp_star_palace_last_error=code,
                    miniapp_star_palace_last_error_time=_now_text(),
                )
                self.log.error(
                    "Mini App star-palace loop failed for %s: %s",
                    identity,
                    code,
                    exc_info=True,
                )
                await asyncio.sleep(STAR_PALACE_ERROR_RETRY_SECONDS)
            finally:
                if identity_state(self.actor, identity).get("miniapp_star_palace_next_divine_time"):
                    self._save_star_palace_identity_state(
                        identity, miniapp_star_palace_next_divine_time="",
                    )

    async def run_star_palace_cycle(
        self,
        identity: str,
        manifest_dt: datetime,
        target_username: str = DEFAULT_STAR_SHIFT_TARGET,
    ) -> bool:
        manifest_key = manifest_dt.strftime(TIME_FORMAT)
        # A time-critical manifestation is worth one direct probe even when the
        # shared breaker is cooling; ordinary background loops must keep waiting.
        try:
            from miniapp_beast import miniapp_circuit_preflight
            circuit_error = miniapp_circuit_preflight(getattr(self.transport, "origin", ""))
            if circuit_error is not None:
                seconds_to_boundary = (manifest_dt - datetime.now()).total_seconds()
                if seconds_to_boundary <= STAR_PALACE_TIME_CRITICAL_SECONDS:
                    self.log.warning(
                        "Mini App star palace [%s]: breaker is open %ss before "
                        "manifestation; forcing one time-critical probe",
                        identity,
                        int(seconds_to_boundary),
                    )
                else:
                    raise circuit_error
        except ImportError:
            pass
        try:
            seconds_to_boundary = max(
                0,
                int((manifest_dt - datetime.now()).total_seconds()),
            )
            payload = await self.transport.star_palace_action(
                identity,
                "divine",
                time_critical=seconds_to_boundary <= STAR_PALACE_TIME_CRITICAL_SECONDS,
            )
        except MiniAppCircuitOpenError as exc:
            self.log.info(
                "Mini App star palace [%s] paused while upstream circuit is open; retry %s",
                identity,
                exc.retry_at or f"in {exc.retry_after}s",
            )
            raise
        except MiniAppBeastError as exc:
            message = ""
            self._save_star_palace_identity_state(identity,
                miniapp_star_palace_last_error=exc.code,
                miniapp_star_palace_last_error_time=_now_text(),
                miniapp_star_palace_last_manifest=manifest_key,
            )
            self.log.warning("Mini App star palace [%s] 观星失败：%s", identity, exc.code)
            details = await self.transport.details(identity)
            divination = self._star_action_mapping(details.get("account"), "starPalace", "divination")
            active = self._star_action_mapping(divination, "active")
            if not bool(divination.get("canDivine", True)):
                remaining = self._star_remaining_seconds(active.get("remainingSeconds"))
                if remaining is not None:
                    self.log.info(
                        "Mini App star palace [%s]: divine already active; retrying 改换星移 in %ss",
                        identity,
                        remaining,
                    )
                    await asyncio.sleep(min(remaining + 2, STAR_PALACE_IDLE_WAIT_CHUNK_SECONDS))
                    await self.run_star_palace_cycle(identity, manifest_dt, target_username)
                    return
            return False

        result = self._star_action_mapping(payload, "actionResult")
        divination = self._star_action_mapping(result, "divination") or self._star_action_mapping(result, "starPalace", "divination")
        active = self._star_action_mapping(divination, "active")
        remaining = self._star_remaining_seconds(active.get("remainingSeconds"))
        message = command_result_text(payload) or miniapp_operation_result_text(payload) or "完成"
        # 观星成功 = 机会已消耗：立即写入 done_date（不等 shift 成功）
        self._save_star_palace_identity_state(identity,
            miniapp_star_palace_last_error="",
            miniapp_star_palace_last_error_time="",
            miniapp_star_palace_last_success_time=_now_text(),
            miniapp_star_palace_last_manifest=manifest_key,
            miniapp_star_palace_last_result=message,
            miniapp_star_palace_done_date=datetime.now().strftime("%Y-%m-%d"),
        )
        self.log.info(
            "Mini App star palace [%s]: 观星 completed for manifestation %s; result=%s; active=%s",
            identity,
            manifest_key,
            message,
            bool(active),
        )

        # 显化窗口开启的信号：divination.active 字段，或结果文本明确包含改换星移提示
        shift_hint = "施展改换星移" in str(message)
        if not active and not shift_hint:
            return False

        # Shift while the manifestation is still pending, never after it lands.
        if remaining is None:
            # remainingSeconds 解析失败时显化窗口通常 300 秒，保底压哨
            remaining = 300
            self.log.info(
                "Mini App star palace [%s]: remainingSeconds missing; defaulting to %ss window",
                identity,
                remaining,
            )
        shift_delay = max(
            0,
            min(
                STAR_PALACE_SHIFT_MAX_DELAY_SECONDS,
                remaining - STAR_PALACE_SHIFT_SAFETY_SECONDS,
            ),
        )

        if shift_delay:
            self.log.info(
                "Mini App star palace [%s]: waiting %ss to 改换星移 -> %s (remaining=%ss)",
                identity,
                shift_delay,
                target_username,
                remaining,
            )
            await asyncio.sleep(shift_delay)
        try:
            shift_payload = await self.transport.star_palace_action(
                identity,
                "shift_destiny",
                target_username=target_username,
                time_critical=True,
            )
            shift_message = (
                command_result_text(shift_payload)
                or miniapp_operation_result_text(shift_payload)
                or "完成"
            )
            final_boundary = datetime.now().replace(
                hour=21,
                minute=0,
                second=0,
                microsecond=0,
            )
            updates = {
                "miniapp_star_palace_last_shift_time": _now_text(),
                "miniapp_star_palace_last_shift_target": target_username,
                "miniapp_star_palace_last_shift_result": shift_message,
            }
            if manifest_dt >= final_boundary:
                updates["miniapp_star_palace_done_date"] = datetime.now().strftime(
                    "%Y-%m-%d"
                )
            self._save_star_palace_identity_state(identity, **updates)
            self.log.info(
                "Mini App star palace [%s]: 改换星移 -> %s; result=%s",
                identity,
                target_username,
                shift_message,
            )
        except MiniAppCircuitOpenError:
            raise
        except MiniAppBeastError as exc:
            self._save_star_palace_identity_state(identity,
                miniapp_star_palace_last_error=exc.code,
                miniapp_star_palace_last_error_time=_now_text(),
                miniapp_star_palace_last_shift_target=target_username,
            )
            self.log.error(
                "Mini App star palace [%s] 改换星移失败：%s",
                identity,
                exc.code,
                exc_info=True,
            )
            return False

        return True

    def _identity_routable(self, identity: str) -> bool:
        key = self._resolve_identity(identity)
        ids = self.transport.identity_player_ids
        return key in ids or key.casefold() in ids

    def _resolve_identity(self, identity: str) -> str:
        resolver = getattr(self.actor, "resolve_avatar_identity", None)
        if callable(resolver):
            return str(resolver(identity) or identity or "主魂").strip() or "主魂"
        return str(identity or "主魂").strip() or "主魂"

    async def _maybe_refresh_auth(self) -> None:
        if (datetime.now() - self._last_auth_refresh).total_seconds() < self.auth_refresh_seconds:
            return
        try:
            await self.transport.initialize(force=True)
            self._sync_avatar_dao_names_from_transport()
            self._last_auth_refresh = datetime.now()
        except MiniAppCircuitOpenError as exc:
            self._last_auth_refresh = datetime.now()
            self._last_auth_refresh_failure = self._last_auth_refresh
            self._route_active = False
            self._record(
                miniapp_route_active=False,
                miniapp_route_last_error=exc.code,
                miniapp_route_retry_at=exc.retry_at,
            )
            self._start_recovery_task()
            return
        except Exception as exc:
            # Do not retry a failed refresh for every command.  In particular,
            # a fixed entry token can expire independently of Telegram initData;
            # reusing it cannot recover and only creates an error storm.
            self._last_auth_refresh = datetime.now()
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            if (datetime.now() - self._last_auth_refresh_failure).total_seconds() >= ROUTE_BLOCKED_LOG_SUPPRESS_SECONDS:
                self._last_auth_refresh_failure = self._last_auth_refresh
                self.log.warning(
                    "Mini App routing auth refresh failed (%s); retrying later",
                    code,
                    exc_info=code != "dwelling_token_expired",
                )
            if code == "dwelling_token_expired":
                self._route_active = False
                self._record(
                    miniapp_route_active=False,
                    miniapp_route_last_error=code,
                )
                self._start_recovery_task()

    async def _send_main(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return await self._route("主魂", message, self._orig_send, args, kwargs)

    async def _send_identity(self, identity: str, message: str, *args: Any, **kwargs: Any) -> Any:
        identity = self._resolve_identity(identity)

        async def fallback(msg: str, *a: Any, **kw: Any) -> Any:
            return await self._orig_send_identity(identity, msg, *a, **kw)

        return await self._route(identity, message, fallback, args, kwargs)

    async def _route(
        self,
        identity: str,
        message: str,
        fallback: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        identity = self._resolve_identity(identity)
        command = normalize_miniapp_command(message)
        if not miniapp_command_allowed(command):
            return await fallback(message, *args, **kwargs)
        if kwargs.get("reply_to") is not None:
            self._record(
                miniapp_route_last_error="reply_target_not_supported",
                miniapp_route_last_error_at=_now_text(),
            )
            self.log.error(
                "Mini App-only command blocked because it requires a group reply target [%s] %s",
                identity,
                command,
            )
            return None
        if not self.enabled or not self._route_active:
            self._record(
                miniapp_route_last_error="route_unavailable",
                miniapp_route_last_error_at=_now_text(),
            )
            self._log_route_blocked(identity, command)
            return None
        if not self._identity_routable(identity):
            self._record(
                miniapp_route_last_error="identity_unavailable",
                miniapp_route_last_error_at=_now_text(),
            )
            self.log.error(
                "Mini App-only command blocked for unknown identity [%s] %s",
                identity,
                command,
            )
            return None
        if hasattr(self.actor, "dashboard_command_paused") and self.actor.dashboard_command_paused(
            command,
            identity,
        ):
            self.log.info("[%s] Mini App command paused by dashboard: %s", identity, command)
            return None
        pause_event = getattr(self.actor, "pause_event", None)
        if pause_event is not None:
            await pause_event.wait()
        if hasattr(self.actor, "identity_pause_seconds") and self.actor.identity_pause_seconds(identity) > 0:
            return None
        if command == ".闭关修炼" and hasattr(
            self.actor, "ensure_tianxing_destiny_for_action"
        ):
            if not await self.actor.ensure_tianxing_destiny_for_action(
                identity, "cultivation"
            ):
                self._record(
                    miniapp_route_last_error="tianxing_destiny_failed",
                    miniapp_route_last_error_at=_now_text(),
                )
                return None
        await self._maybe_refresh_auth()
        if not self._route_active:
            self._log_route_blocked(identity, command)
            return None
        try:
            sect_allowed = getattr(self.actor, "sect_command_allowed", None)
            if callable(sect_allowed) and not sect_allowed(command, identity):
                return None
            if command == ".闭关修炼" and identity_sect(self.actor, identity) == "天星宗":
                response = await self.transport.command(command, identity=identity, meditation_prefix=True)
            else:
                response = await self.transport.command(command, identity=identity)
            apply_dwelling_snapshot(self.actor, identity, response.payload)
            self._record(
                miniapp_route_last_command=command,
                miniapp_route_last_identity=identity,
                miniapp_route_last_command_at=_now_text(),
                miniapp_route_last_error="",
            )
        except asyncio.CancelledError:
            raise
        except SectTaskStopped:
            return None
        except MiniAppCircuitOpenError as exc:
            self._record(
                miniapp_route_active=False,
                miniapp_route_last_error=exc.code,
                miniapp_route_last_error_at=_now_text(),
                miniapp_route_retry_at=exc.retry_at,
            )
            self._route_active = False
            self._start_recovery_task()
            return None
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self._record(
                miniapp_route_last_error=code,
                miniapp_route_last_error_at=_now_text(),
            )
            self.log.warning(
                "Mini App command failed [%s] %s (%s); group fallback is disabled",
                identity,
                command,
                code,
            )
            if code == "dwelling_token_expired":
                self._route_active = False
                self._record(miniapp_route_active=False)
                self._start_recovery_task()
            return None
        if kwargs.get("return_response_msg") or kwargs.get("return_msg"):
            return response
        return response.text


async def install_miniapp_command_router(
    actor: Any,
    account: str,
    logger: logging.Logger | None = None,
    *,
    transport: MiniAppDwellingTransport | None = None,
    start_background_tasks: bool = True,
) -> MiniAppCommandRouter:
    router = MiniAppCommandRouter(
        actor,
        account,
        logger=logger,
        transport=transport,
        start_background_tasks=start_background_tasks,
    )
    await router.install()
    setattr(actor, "_miniapp_command_router", router)
    return router
