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

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import (
    MiniAppDwellingTransport,
    apply_dwelling_snapshot,
    command_result_text,
    identity_state,
    miniapp_command_allowed,
    miniapp_operation_result_text,
    normalize_miniapp_command,
    sect_farm_action_result_ok,
    sect_farm_snapshot_status,
)
from miniapp_daily_activities import MiniAppDailyActivities
from miniapp_journey import MiniAppTianxingJourney


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_AUTH_REFRESH_SECONDS = 6 * 3600
DEFAULT_PROFILE_REFRESH_SECONDS = 30 * 60
DEFAULT_STAR_FARM_RETRY_SECONDS = 5 * 60
STAR_FARM_WAKE_GRACE_SECONDS = 5
DEFAULT_STAR_FARM_TARGET = "天雷星"


def _now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


class MiniAppCommandRouter:
    """Route Mini App-capable commands away from the game group."""

    def __init__(self, actor: Any, account: str, logger: logging.Logger | None = None) -> None:
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
        self.star_farm_enabled = bool(settings.get("star_farm_enabled", True))
        self.star_farm_retry_seconds = max(
            60,
            int(settings.get("star_farm_retry_seconds") or DEFAULT_STAR_FARM_RETRY_SECONDS),
        )
        self.star_farm_target = str(
            settings.get("star_farm_target") or DEFAULT_STAR_FARM_TARGET
        ).strip()
        self.transport = MiniAppDwellingTransport(
            actor.client,
            entry_url,
            bot_username=str(settings.get("bot_username") or "fanrenxiuxian_bot"),
            timeout=int(settings.get("timeout_seconds") or 20),
            logger=self.log,
        )
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
        self._orig_send = None
        self._orig_send_identity = None
        self._last_auth_refresh = datetime.min
        self._profile_task: asyncio.Task[Any] | None = None
        self._star_farm_tasks: list[asyncio.Task[Any]] = []
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
        try:
            await self.transport.initialize()
            self._last_auth_refresh = datetime.now()
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self.enabled = False
            self._record(miniapp_route_active=False, miniapp_route_last_error=code)
            self.log.error(
                "Mini App command routing setup failed (%s); supported commands are blocked",
                code,
                exc_info=True,
            )
            return False
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
        # Overview sync is authoritative for sect membership.  Select Star
        # Palace identities only after it has corrected stale local mappings.
        star_identities = self.star_farm_identities(known)
        journey_identities = self.tianxing_journey.identities(known)
        self._record(
            miniapp_star_farm_identities=star_identities,
            miniapp_journey_identities=journey_identities,
        )
        self._profile_task = asyncio.create_task(
            self.run_profile_sync_loop(),
            name=f"miniapp_{self.account}_profiles",
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
        if self.tianxing_journey.enabled:
            self._daily_activity_tasks.append(
                asyncio.create_task(
                    self.tianxing_journey.run_loop(),
                    name=f"miniapp_{self.account}_journey",
                )
            )
        self.log.warning(
            "[%s] Mini App command routing active for identities %s; unsupported commands stay in the group",
            self.account,
            known,
        )
        return True

    def routable_identities(self) -> list[str]:
        return [
            name
            for name in ["主魂", *(getattr(self.actor, "avatars", []) or [])]
            if self._identity_routable(name)
        ]

    async def sync_all_profiles(self, identities: list[str] | None = None) -> int:
        synced = 0
        for identity in identities if identities is not None else self.routable_identities():
            try:
                payload = await self.transport.overview(identity)
                apply_dwelling_snapshot(self.actor, identity, payload)
                synced += 1
            except asyncio.CancelledError:
                raise
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
        except Exception:
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
    ) -> tuple[dict[str, Any], tuple[int, int, list[str], int]]:
        payload = await self.transport.sect_farm_action(
            identity,
            action,
            plot_key=plot_key,
            star_name=self.star_farm_target if action == "pull" else "",
        )
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
        payload = await self.transport.sect_farm_snapshot(identity)
        ready, troubled, empty, next_wait = self._record_star_snapshot(identity, payload)
        collection_due = ready > 0 or (troubled > 0 and next_wait <= 0)
        if collection_due:
            # One maintenance action immediately before the batch collection;
            # no fixed-interval soothing or polling while stars are maturing.
            _, (ready, troubled, empty, next_wait) = await self._star_farm_action(
                identity,
                "soothe",
            )
            if ready > 0:
                _, (ready, troubled, empty, next_wait) = await self._star_farm_action(
                    identity,
                    "collect",
                )
        for plot_key in empty:
            _, (ready, troubled, _, next_wait) = await self._star_farm_action(
                identity,
                "pull",
                plot_key=plot_key,
            )
            await asyncio.sleep(1)
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

    def _identity_routable(self, identity: str) -> bool:
        key = str(identity or "主魂").strip() or "主魂"
        ids = self.transport.identity_player_ids
        return key in ids or key.casefold() in ids

    async def _maybe_refresh_auth(self) -> None:
        if (datetime.now() - self._last_auth_refresh).total_seconds() < self.auth_refresh_seconds:
            return
        try:
            await self.transport.initialize(force=True)
            self._last_auth_refresh = datetime.now()
        except Exception:
            # A failed proactive refresh is not fatal: the transport retries
            # authentication on demand when a command hits an auth error.
            self.log.warning("Mini App routing auth refresh failed", exc_info=True)

    async def _send_main(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return await self._route("主魂", message, self._orig_send, args, kwargs)

    async def _send_identity(self, identity: str, message: str, *args: Any, **kwargs: Any) -> Any:
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
        if not self.enabled:
            self._record(
                miniapp_route_last_error="route_unavailable",
                miniapp_route_last_error_at=_now_text(),
            )
            self.log.error(
                "Mini App-only command blocked while route is unavailable [%s] %s",
                identity,
                command,
            )
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
        await self._maybe_refresh_auth()
        try:
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
            return None
        if kwargs.get("return_response_msg") or kwargs.get("return_msg"):
            return response
        return response.text


async def install_miniapp_command_router(
    actor: Any,
    account: str,
    logger: logging.Logger | None = None,
) -> MiniAppCommandRouter:
    router = MiniAppCommandRouter(actor, account, logger=logger)
    await router.install()
    setattr(actor, "_miniapp_command_router", router)
    return router
