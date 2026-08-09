#!/usr/bin/env python3
"""Mini App scheduler used while an account cannot write to the game group."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any

from concubine_features import (
    CONCUBINE_GRACE_SECONDS,
    CONCUBINE_TASKS,
    add_seconds_str,
    is_future,
    now_str,
    parse_duration_seconds,
    seconds_until,
)
from miniapp_beast import MiniAppBeastError
from miniapp_beast_abyss import MiniAppBeastAbyssWorker
from miniapp_beast_contract import MiniAppBeastContractWorker
from miniapp_beast_seek import MiniAppBeastSeekWorker
from miniapp_daily_activities import MiniAppDailyActivities
from miniapp_fishing import MiniAppFishingAutomation
from miniapp_journey import MiniAppTianxingJourney
from miniapp_inventory import MiniAppInventoryWorker
from miniapp_dwelling import (
    MiniAppCommandResponse,
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


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_STATUS_REFRESH_SECONDS = 30 * 60
DEFAULT_AUTH_REFRESH_SECONDS = 6 * 3600
DEFAULT_BEAST_SYNC_SECONDS = 12 * 3600
DEFAULT_STAR_RETRY_SECONDS = 5 * 60
STAR_FARM_WAKE_GRACE_SECONDS = 5
STAR_IDENTITY = "素心子"
STAR_TARGET = "天雷星"
XIAOHAO_YUANYING_AVATARS = ("缘生子",)
RECOVERABLE_BLOCKED_COMMANDS = {".拼图"}


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _elapsed_seconds(value: Any) -> float:
    parsed = _parse_time(value)
    return max(0.0, (datetime.now() - parsed).total_seconds()) if parsed else float("inf")


def _periodic_wait_seconds(last_run: Any, interval_seconds: int) -> int:
    elapsed = _elapsed_seconds(last_run)
    if elapsed == float("inf"):
        return 0
    return max(0, int(interval_seconds) - int(elapsed))


class RestrictedMiniAppWorker:
    """Run only Mini App-backed automations plus the separate packet listener."""

    def __init__(self, actor: Any, account: str, logger: logging.Logger | None = None) -> None:
        self.actor = actor
        self.account = str(account or "").strip()
        self.log = logger or logging.getLogger(f"restricted_miniapp.{self.account}")
        settings = (getattr(actor, "config", {}) or {}).get("restricted_miniapp") or {}
        beast_settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        self.enabled = bool(settings.get("enabled", True))
        self.status_refresh_seconds = max(
            300,
            int(settings.get("status_refresh_seconds") or DEFAULT_STATUS_REFRESH_SECONDS),
        )
        self.auth_refresh_seconds = max(
            1800,
            int(settings.get("auth_refresh_seconds") or DEFAULT_AUTH_REFRESH_SECONDS),
        )
        self.beast_sync_seconds = max(
            6 * 3600,
            int(settings.get("beast_sync_seconds") or DEFAULT_BEAST_SYNC_SECONDS),
        )
        self.star_retry_seconds = max(
            60,
            int(settings.get("star_retry_seconds") or DEFAULT_STAR_RETRY_SECONDS),
        )
        self.star_enabled = bool(settings.get("star_farm_enabled", True))
        self.beast_enabled = bool(settings.get("beast_sync_enabled", True))
        self.transport = MiniAppDwellingTransport(
            actor.client,
            str(beast_settings.get("entry_url") or ""),
            bot_username=str(beast_settings.get("bot_username") or "fanrenxiuxian_bot"),
            timeout=int(beast_settings.get("timeout_seconds") or 20),
            logger=self.log,
            config_file=getattr(actor, "config_file", "") or getattr(actor, "CONFIG_FILE", ""),
            entry_chat=getattr(actor, "target_chat_id", "fanrenxxz"),
        )
        self.beast_contract = MiniAppBeastContractWorker(
            actor,
            self.transport,
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
        self.fishing = MiniAppFishingAutomation(
            actor,
            self.transport,
            self.account,
            self.log,
        )
        self._tasks: list[asyncio.Task[Any]] = []
        self._last_auth_refresh = datetime.min

    def identities(self) -> list[str]:
        return ["主魂", *(getattr(self.actor, "avatars", []) or [])]

    def _save(self) -> None:
        self.actor.save_state()

    def _record_worker_state(self, **updates: Any) -> None:
        state = self.actor.state
        state.update(updates)
        state["restricted_miniapp_account"] = self.account
        state["restricted_miniapp_updated_at"] = now_str()
        self._save()

    def _spawn(self, name: str, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=f"miniapp_{self.account}_{name}")
        self._tasks.append(task)

        def on_done(done: asyncio.Task[Any]) -> None:
            if done.cancelled() or not getattr(self.actor, "is_running", True):
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error:
                self.log.critical(
                    "Restricted Mini App task %s stopped: %s",
                    name,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )
            else:
                self.log.critical("Restricted Mini App task %s stopped unexpectedly", name)

        task.add_done_callback(on_done)
        return task

    async def start(self) -> None:
        if not self.enabled:
            self._record_worker_state(
                restricted_miniapp_active=False,
                restricted_miniapp_last_error="disabled",
            )
            return
        await self.transport.initialize()
        self._last_auth_refresh = datetime.now()
        missing = [name for name in self.identities() if name not in self.transport.identity_player_ids]
        if missing:
            raise MiniAppBeastError("miniapp_identity_mapping_incomplete")

        # Replace both group send paths before any legacy scheduler is released.
        self.actor.send_and_wait_feedback = self.send_main
        self.actor.send_and_wait_feedback_identity = self.send_identity
        self.actor._current_identity = "主魂"
        self.actor._main_confirmed = True
        await self.sync_all_details()
        self.actor.startup_done.set()
        self._record_worker_state(
            restricted_miniapp_active=True,
            restricted_miniapp_started_at=now_str(),
            restricted_miniapp_last_error="",
            restricted_miniapp_transport="fixed_entry_http_only",
        )

        self._spawn("auth", self.run_auth_refresh_loop())
        self._spawn("details", self.run_details_sync_loop())
        self._spawn("inventory", self.inventory.run_loop())
        if self.fishing.supported:
            self._spawn("fishing", self.fishing.run_loop())
        self._spawn("concubine", self.run_concubine_loop())
        self._spawn("custom", self.actor.run_custom_command_loop())
        self._spawn("meditation", self.actor.run_meditation_timer())
        self._spawn("yuanying", self.actor.run_yuanying_out_loop())
        if self.daily_activities.pagoda_enabled:
            self._spawn("pagoda", self.daily_activities.run_pagoda_loop())
        if self.daily_activities.hunt_enabled:
            self._spawn("hunt", self.daily_activities.run_hunt_loop())
        if self.tianxing_journey.enabled:
            self._spawn("journey", self.tianxing_journey.run_loop())
        if self.beast_abyss.enabled:
            self._spawn("beast_abyss", self.beast_abyss.run_loop())
        if self.beast_seek.enabled:
            self._spawn("beast_seek", self.beast_seek.run_loop())

        if self.account == "xiaohao":
            for avatar in getattr(self.actor, "avatars", []) or []:
                self._spawn(
                    f"meditation_{avatar}",
                    self.actor.run_avatar_meditation_loop(avatar, initial_delay=0),
                )
            for avatar in XIAOHAO_YUANYING_AVATARS:
                if avatar in (getattr(self.actor, "avatars", []) or []):
                    self._spawn(f"yuanying_{avatar}", self.run_avatar_yuanying_loop(avatar))
            if "问心子" in (getattr(self.actor, "avatars", []) or []):
                self._spawn(
                    "cloud_stairs_问心子",
                    self.actor.run_avatar_cloud_stairs_loop("问心子", initial_delay=0),
                )
            if self.star_enabled and STAR_IDENTITY in (getattr(self.actor, "avatars", []) or []):
                self._spawn("star_farm", self.run_star_farm_loop())
            if self.beast_enabled:
                self._spawn("beast_sync", self.run_beast_sync_loop())
            if self.beast_contract.enabled:
                self._spawn("beast_contract", self.beast_contract.run())
        elif self.account == "waaiging":
            self._spawn("destiny", self.actor.run_tianxing_destiny_loop())

        self.log.warning(
            "[%s] Restricted Mini App scheduler active; Telegram game-group sends are disabled",
            self.account,
        )

    async def stop(self) -> None:
        self.actor.is_running = False
        self.actor.startup_done.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._record_worker_state(
            restricted_miniapp_active=False,
            restricted_miniapp_stopped_at=now_str(),
        )

    async def send_main(self, message: str, *args: Any, **kwargs: Any) -> Any:
        return await self._send("主魂", message, **kwargs)

    async def send_identity(
        self,
        identity: str,
        message: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return await self._send(identity, message, **kwargs)

    async def _send(self, identity: str, message: str, **kwargs: Any) -> Any:
        command = normalize_miniapp_command(message)
        if not miniapp_command_allowed(command):
            self.log.warning(
                "[%s] Blocked non-Mini-App command in restricted mode: %s",
                identity,
                command,
            )
            self._record_worker_state(
                restricted_miniapp_last_blocked_command=command,
                restricted_miniapp_last_blocked_identity=identity,
                restricted_miniapp_last_blocked_at=now_str(),
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
                self._record_worker_state(
                    restricted_miniapp_last_error="tianxing_destiny_failed",
                    restricted_miniapp_last_error_at=now_str(),
                )
                return None

        try:
            response = await self.transport.command(
                command,
                identity=identity,
                meditation_prefix=(self.account == "waaiging"),
            )
            apply_dwelling_snapshot(self.actor, identity, response.payload)
            self._sync_concubine_response(identity, command, response.text)
            state = self.actor.state
            if (
                normalize_miniapp_command(state.get("restricted_miniapp_last_blocked_command"))
                == command
                and str(state.get("restricted_miniapp_last_blocked_identity") or "")
                == identity
            ):
                state.pop("restricted_miniapp_last_blocked_command", None)
                state.pop("restricted_miniapp_last_blocked_identity", None)
                state.pop("restricted_miniapp_last_blocked_at", None)
            self._record_worker_state(
                restricted_miniapp_last_command=command,
                restricted_miniapp_last_identity=identity,
                restricted_miniapp_last_command_at=now_str(),
                restricted_miniapp_last_error="",
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self._record_worker_state(
                restricted_miniapp_last_error=code,
                restricted_miniapp_last_error_at=now_str(),
            )
            self.log.error(
                "Mini App command failed [%s] %s: %s",
                identity,
                command,
                code,
                exc_info=True,
            )
            return None
        if kwargs.get("return_response_msg") or kwargs.get("return_msg"):
            return response
        return response.text

    async def recover_last_blocked_command(self) -> bool:
        """Retry a previously blocked command after Mini App support is added."""
        state = self.actor.state
        command = normalize_miniapp_command(
            state.get("restricted_miniapp_last_blocked_command")
        )
        identity = str(
            state.get("restricted_miniapp_last_blocked_identity") or ""
        ).strip()
        if (
            command not in RECOVERABLE_BLOCKED_COMMANDS
            or not miniapp_command_allowed(command)
            or identity not in self.identities()
        ):
            return False
        self.log.info(
            "[%s] Recovering previously blocked Mini App command: %s",
            identity,
            command,
        )
        response = await self._send(
            identity,
            command,
            return_response_msg=True,
        )
        if response is None:
            return False
        self.log.info(
            "[%s] Recovered previously blocked Mini App command: %s",
            identity,
            command,
        )
        return True

    async def sync_all_details(self) -> None:
        synced = 0
        for identity in self.identities():
            try:
                payload = await self.transport.overview(identity)
                apply_dwelling_snapshot(self.actor, identity, payload)
                synced += 1
            except Exception as exc:
                container = identity_state(self.actor, identity)
                container["miniapp_last_error"] = (
                    exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                )
                self.log.error("Mini App details sync failed for %s", identity, exc_info=True)
        self._record_worker_state(
            restricted_miniapp_last_sync_time=now_str(),
            restricted_miniapp_identity_count=synced,
        )

    async def run_auth_refresh_loop(self) -> None:
        while self.actor.is_running:
            await asyncio.sleep(300)
            if (datetime.now() - self._last_auth_refresh).total_seconds() < self.auth_refresh_seconds:
                continue
            try:
                await self.transport.initialize(force=True)
                self._last_auth_refresh = datetime.now()
                self._record_worker_state(
                    restricted_miniapp_last_auth_refresh=now_str(),
                    restricted_miniapp_last_error="",
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                self._record_worker_state(
                    restricted_miniapp_last_error=code,
                    restricted_miniapp_last_error_at=now_str(),
                )
                self.log.error("Mini App authentication refresh failed: %s", code, exc_info=True)

    async def run_details_sync_loop(self) -> None:
        while self.actor.is_running:
            await asyncio.sleep(self.status_refresh_seconds)
            await self.sync_all_details()

    def _sync_concubine_response(self, identity: str, command: str, text: str) -> None:
        if command != ".我的侍妾" or not text:
            return
        state = identity_state(self.actor, identity)
        clean = str(text).replace("**", "")
        for task_key in ("divination", "dream"):
            task = CONCUBINE_TASKS[task_key]
            match = re.search(rf"{re.escape(task['status_label'])}\s*[：:]\s*([^\n]+)", clean)
            if not match:
                continue
            value = match.group(1).strip()
            if any(marker in value for marker in ("可用", "可施展", "已就绪", "无")):
                state[task["state_key"]] = ""
            else:
                seconds = parse_duration_seconds(value)
                state[task["state_key"]] = (
                    add_seconds_str(now_str(), seconds + CONCUBINE_GRACE_SECONDS)
                    if seconds > 0
                    else add_seconds_str(now_str(), 30 * 60)
                )
        state["last_concubine_status_time"] = now_str()

    def _concubine_status_due(self, identity: str) -> bool:
        state = identity_state(self.actor, identity)
        return _elapsed_seconds(state.get("last_concubine_status_time")) >= 6 * 3600

    async def run_concubine_loop(self) -> None:
        await self.actor.startup_done.wait()
        await asyncio.sleep(random.randint(5, 15))
        while self.actor.is_running:
            try:
                await self.recover_last_blocked_command()
                for identity in self.identities():
                    if self._concubine_status_due(identity):
                        if not self.actor.dashboard_command_paused(".我的侍妾", identity):
                            await self._send(identity, ".我的侍妾", return_response_msg=True)
                            await asyncio.sleep(1)
                    for task_key in ("divination", "dream"):
                        task = CONCUBINE_TASKS[task_key]
                        state = identity_state(self.actor, identity)
                        next_time = str(state.get(task["state_key"]) or "")
                        if next_time and is_future(next_time):
                            continue
                        if self.actor.dashboard_command_paused(task["command"], identity):
                            continue
                        if identity == "主魂":
                            await self.actor.execute_concubine_direct(task_key)
                        else:
                            await self.actor.execute_avatar_concubine_direct(identity, task_key)
                        await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.error("Restricted Mini App concubine loop failed", exc_info=True)
            await asyncio.sleep(60)

    async def run_avatar_yuanying_loop(self, avatar: str) -> None:
        await self.actor.startup_done.wait()
        while self.actor.is_running:
            try:
                await self.actor.common_avatar_yuanying_out_check(avatar)
                state = identity_state(self.actor, avatar)
                next_time = str(state.get("next_yuanying_out_time") or "")
                wait = seconds_until(next_time) if next_time and is_future(next_time) else 300
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.error("Mini App avatar Yuanying loop failed for %s", avatar, exc_info=True)
                wait = 300
            await asyncio.sleep(max(60, min(int(wait or 300), 300)))

    def _record_star_snapshot(self, payload: dict[str, Any]) -> tuple[int, int, list[str], int]:
        snapshot = sect_farm_snapshot_status(payload)
        ready = int(snapshot["ready_count"])
        troubled = int(snapshot["troubled_count"])
        empty = list(snapshot["empty_keys"])
        next_wait = int(snapshot["next_wait_seconds"])
        next_collect_time = (
            add_seconds_str(now_str(), next_wait + STAR_FARM_WAKE_GRACE_SECONDS)
            if next_wait > 0
            else ""
        )
        state = identity_state(self.actor, STAR_IDENTITY)
        state["star_miniapp_last_sync_time"] = now_str()
        state["star_miniapp_ready_count"] = ready
        state["star_miniapp_troubled_count"] = troubled
        state["star_miniapp_empty_count"] = len(empty)
        state["star_miniapp_next_collect_time"] = next_collect_time
        state["star_miniapp_next_wait_seconds"] = next_wait
        state["star_miniapp_last_error"] = ""
        state["last_star_observatory_time"] = now_str()
        state["star_observatory_needs_refresh"] = False
        state["next_star_collect_time"] = next_collect_time
        state["next_star_appease_time"] = next_collect_time
        state["next_star_check_time"] = next_collect_time
        state["star_observatory_summary"] = (
            f"Mini App: 可收集 {ready}，需安抚 {troubled}，空盘 {len(empty)}"
            f"，下次采集 {next_collect_time or '待结算'}"
        )
        self._save()
        return ready, troubled, empty, next_wait

    async def _star_action(
        self,
        action: str,
        plot_key: str = "",
        log_operation: bool = True,
    ) -> tuple[dict[str, Any], tuple[int, int, list[str], int]]:
        action_kwargs = {
            "plot_key": plot_key,
            "star_name": STAR_TARGET if action == "pull" else "",
        }
        if not log_operation:
            action_kwargs["log_operation"] = False
        payload = await self.transport.sect_farm_action(STAR_IDENTITY, action, **action_kwargs)
        if not sect_farm_action_result_ok(payload, action):
            raise MiniAppBeastError(f"star_farm_{action}_failed")
        state = identity_state(self.actor, STAR_IDENTITY)
        state["star_miniapp_last_action"] = action
        state["star_miniapp_last_action_time"] = now_str()
        state["star_miniapp_last_result"] = (
            command_result_text(payload) or miniapp_operation_result_text(payload)
        )
        action_time = now_str()
        if action == "soothe":
            state["last_calm_time"] = action_time
            state["last_star_appease_time"] = action_time
        elif action == "collect":
            state["last_collection_time"] = action_time
            state["last_star_collect_time"] = action_time
            recorder = getattr(self.actor, "record_daily_reward_event", None)
            if callable(recorder) and state["star_miniapp_last_result"]:
                try:
                    recorder(
                        STAR_IDENTITY,
                        ".收集精华",
                        state["star_miniapp_last_result"],
                        source="Mini App 收集精华",
                    )
                except Exception:
                    self.log.warning(
                        "Mini App star essence reward recording failed for %s",
                        STAR_IDENTITY,
                        exc_info=True,
                    )
        elif action == "pull":
            state["star_attraction_start_time"] = action_time
            state["last_star_attraction_time"] = action_time
        return payload, self._record_star_snapshot(payload)

    async def run_star_farm_loop(self) -> None:
        await self.actor.startup_done.wait()
        while self.actor.is_running:
            wait = self.star_retry_seconds
            try:
                payload = await self.transport.sect_farm_snapshot(STAR_IDENTITY)
                ready, troubled, empty, next_wait = self._record_star_snapshot(payload)
                collection_due = sect_farm_collection_due(ready, troubled, next_wait)
                if collection_due:
                    _, (ready, troubled, empty, next_wait) = await self._star_action("soothe")
                    if ready > 0 and sect_farm_collection_due(ready, troubled, next_wait):
                        requested_count = ready
                        empty_before = set(empty)
                        operation = sect_farm_collect_batch_operation(requested_count)
                        self.log.info("OUT [Mini App | %s]:\n%s", STAR_IDENTITY, operation)
                        collect_payload, (ready, troubled, empty, next_wait) = await self._star_action(
                            "collect",
                            log_operation=False,
                        )
                        empty_after = set(empty)
                        confirmed_count = len(empty_after - empty_before)
                        state = identity_state(self.actor, STAR_IDENTITY)
                        state["star_miniapp_last_collect_requested_count"] = requested_count
                        state["star_miniapp_last_collect_confirmed_count"] = confirmed_count
                        state["star_miniapp_last_collect_empty_count"] = len(empty_after)
                        self._save()
                        self.log.info(
                            "IN [Mini App | %s]:\n%s -> %s",
                            STAR_IDENTITY,
                            operation,
                            sect_farm_collect_batch_result_text(
                                requested_count,
                                confirmed_count,
                                len(empty_after),
                                command_result_text(collect_payload)
                                or miniapp_operation_result_text(collect_payload),
                            ),
                        )
                # Do not immediately refill partial empty slots while the
                # rest of the Tianlei batch is still maturing.  Waiting lets
                # the next collection empty and refill all eight together.
                pull_keys = list(empty) if next_wait <= 0 else []
                if pull_keys:
                    operation = sect_farm_pull_batch_operation(pull_keys)
                    self.log.info("OUT [Mini App | %s]:\n%s", STAR_IDENTITY, operation)
                    pull_results = []
                    for plot_key in pull_keys:
                        pull_payload, (ready, troubled, _, next_wait) = await self._star_action(
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
                        STAR_IDENTITY,
                        operation,
                        sect_farm_pull_batch_result_text(pull_keys, STAR_TARGET, pull_results),
                    )
                wait = (
                    next_wait + STAR_FARM_WAKE_GRACE_SECONDS
                    if next_wait > 0
                    else self.star_retry_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                state = identity_state(self.actor, STAR_IDENTITY)
                state["star_miniapp_last_error"] = code
                state["star_miniapp_last_error_time"] = now_str()
                self._save()
                self.log.error("Star farm Mini App loop failed: %s", code, exc_info=True)
                wait = self.star_retry_seconds
            await asyncio.sleep(max(60, int(wait)))

    def _record_beast_snapshot(self, snapshot: dict[str, Any]) -> None:
        beasts = list(snapshot.get("beasts") or [])
        if not beasts:
            raise MiniAppBeastError("beast_roster_empty")
        state = self.actor.state
        state["beasts_cache"] = beasts
        state["beast_roster_updated_at"] = now_str()
        state["beast_roster_last_source"] = "miniapp"
        state["last_beast_roster_query_result"] = "parsed"
        state["beast_miniapp_last_sync_time"] = now_str()
        state["beast_miniapp_last_error"] = ""
        state["beast_miniapp_sync_count"] = int(state.get("beast_miniapp_sync_count") or 0) + 1
        state["beast_border_patrol_miniapp_supported"] = False
        state["beast_border_patrol_stopped_reason"] = "万兽谷 Mini App 未提供旧巡边接口"
        if hasattr(self.actor, "update_best_beast_tracking"):
            self.actor.update_best_beast_tracking()
        self._save()
        self.log.info("Wan Beast Valley Mini App roster synced: %s beasts", len(beasts))

    async def run_beast_sync_loop(self) -> None:
        await self.actor.startup_done.wait()
        while self.actor.is_running:
            last_sync = self.actor.state.get("beast_miniapp_last_sync_time")
            wait = _periodic_wait_seconds(last_sync, self.beast_sync_seconds)
            if wait > 0:
                await asyncio.sleep(min(wait, 600))
                continue
            self.actor.state["beast_miniapp_last_attempt_time"] = now_str()
            self._save()
            try:
                snapshot = await self.transport.spirit_beast_snapshot("主魂")
                self._record_beast_snapshot(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                self.actor.state["beast_miniapp_last_error"] = code
                self.actor.state["beast_miniapp_last_error_time"] = now_str()
                self._save()
                self.log.error("Wan Beast Valley Mini App sync failed: %s", code, exc_info=True)
                await asyncio.sleep(300)
