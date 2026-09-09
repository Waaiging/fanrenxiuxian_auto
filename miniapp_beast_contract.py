#!/usr/bin/env python3
"""Per-beast Mini App contract interaction scheduler for Wanling accounts."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError, miniapp_circuit_wait_seconds
from miniapp_dwelling import MiniAppDwellingTransport, identity_state
from sect_rules import SectTaskStopped, identity_sect, require_command, resolve_identity, task_paused


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_INTERVAL_SECONDS = 2 * 3600
DEFAULT_RETRY_SECONDS = 5 * 60
DEFAULT_ACTION_DELAY_SECONDS = 1


def contract_now() -> datetime:
    return datetime.now()


def contract_time(value: datetime | None = None) -> str:
    return (value or contract_now()).strftime(TIME_FORMAT)


def contract_add_seconds(seconds: int, value: datetime | None = None) -> str:
    return contract_time((value or contract_now()) + timedelta(seconds=max(0, int(seconds or 0))))


def parse_contract_time(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def contract_seconds_until(value: Any) -> int:
    target = parse_contract_time(value)
    return max(0, int((target - contract_now()).total_seconds())) if target else 0


def main_soul_is_wanling(actor: Any) -> bool:
    mapping = getattr(actor, "identity_sect_names", {}) or {}
    sect = str(mapping.get("主魂") or getattr(actor, "sect_name", "") or "").strip()
    return sect == "万灵宗"


def _error_code(exc: Exception) -> str:
    return exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()


def _message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("actionResult")
    if isinstance(result, dict):
        text = result.get("rawMessage") or result.get("message")
        if text:
            return str(text).strip()
    return str(payload.get("message") or "").strip()


def _find_action_beast(payload: Any, beast_id: int) -> dict[str, Any]:
    """Find the updated beast record without depending on one response wrapper."""
    if not isinstance(payload, dict):
        return {}
    candidates: list[Any] = [
        payload.get("beast"),
        payload.get("selectedBeast"),
        payload.get("spiritBeast"),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend((data.get("beast"), data.get("selectedBeast"), data.get("spiritBeast")))
    for owner in (payload, data):
        if isinstance(owner, dict) and isinstance(owner.get("beasts"), list):
            candidates.extend(owner["beasts"])
    for item in candidates:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id") or item.get("beastId") or 0)
        except (TypeError, ValueError):
            continue
        if item_id == beast_id:
            return item
    return {}


def _apply_action_beast(cached: dict[str, Any], update: dict[str, Any]) -> None:
    if not update:
        return
    integer_fields = {
        "stamina": "stamina",
        "combatPower": "power",
        "experience": "exp",
        "level": "level",
        "tier": "tier",
    }
    for source, target in integer_fields.items():
        if source not in update:
            continue
        try:
            cached[target] = max(0, int(update[source] or 0))
        except (TypeError, ValueError):
            pass
    if update.get("status"):
        cached["status"] = str(update["status"]).strip()
    if update.get("name"):
        cached["full_name"] = str(update["name"]).strip()


class MiniAppBeastContractWorker:
    """Interact with every current spirit beast and retry only failed beasts."""

    def __init__(
        self,
        actor: Any,
        transport: MiniAppDwellingTransport | None,
        logger: logging.Logger | None = None,
        identity: str = "主魂",
    ) -> None:
        self.actor = actor
        self._identity = identity
        self.transport = transport
        self.log = logger or logging.getLogger("MiniAppBeastContract")
        settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        entry_url = str(settings.get("entry_url") or "").strip()
        self.configured_enabled = (
            bool(settings.get("enabled", bool(entry_url)))
            and bool(entry_url)
            and bool(settings.get("contract_interaction_enabled", True))
        )
        self.interval_seconds = max(
            5 * 60,
            int(settings.get("contract_interaction_seconds") or DEFAULT_INTERVAL_SECONDS),
        )
        self.retry_seconds = max(
            60,
            int(settings.get("contract_interaction_retry_seconds") or DEFAULT_RETRY_SECONDS),
        )
        delay_value = settings.get("contract_interaction_delay_seconds", DEFAULT_ACTION_DELAY_SECONDS)
        if delay_value is None:
            delay_value = DEFAULT_ACTION_DELAY_SECONDS
        self.action_delay_seconds = max(0, min(60, int(delay_value)))

    @property
    def identity(self) -> str:
        return resolve_identity(self.actor, self._identity)

    @property
    def enabled(self) -> bool:
        return self.configured_enabled and identity_sect(self.actor, self.identity) == "万灵宗"

    @property
    def state(self) -> dict[str, Any]:
        return identity_state(self.actor, self.identity)

    @classmethod
    def from_actor(
        cls,
        actor: Any,
        logger: logging.Logger | None = None,
    ) -> "MiniAppBeastContractWorker":
        settings = (getattr(actor, "config", {}) or {}).get("miniapp_beast") or {}
        entry_url = str(settings.get("entry_url") or "").strip()
        transport = (
            MiniAppDwellingTransport(
                actor.client,
                entry_url,
                bot_username=str(settings.get("bot_username") or "fanrenxiuxian_bot"),
                timeout=int(settings.get("timeout_seconds") or 20),
                logger=logger,
                config_file=getattr(actor, "config_file", "") or getattr(actor, "CONFIG_FILE", ""),
                entry_chat=getattr(actor, "target_chat_id", "fanrenxxz"),
            )
            if entry_url
            else None
        )
        if transport is not None:
            transport.sect_actor = actor
        return cls(actor, transport, logger=logger)

    def _save(self) -> None:
        self.actor.save_state()

    def _results(self) -> dict[str, dict[str, Any]]:
        value = self.state.get("beast_contract_interaction_results")
        if not isinstance(value, dict):
            value = {}
            self.state["beast_contract_interaction_results"] = value
        return value

    def _record_enabled_state(self) -> None:
        self.state.update({
            "beast_contract_interaction_enabled": bool(self.enabled),
            "beast_contract_interaction_interval_seconds": self.interval_seconds,
            "beast_contract_interaction_transport": "miniapp_http_only",
        })

    def _update_cache(self, beasts: list[dict[str, Any]]) -> None:
        self.state["beasts_cache"] = beasts
        self.state["beast_contract_roster_time"] = contract_time()
        self.state["beast_contract_roster_count"] = len(beasts)
        updater = getattr(self.actor, "update_main_best_beast", None)
        if not callable(updater):
            updater = getattr(self.actor, "update_best_beast_tracking", None)
        if self.identity == "主魂" and callable(updater):
            updater()

    async def run_cycle(self) -> bool:
        if not self.enabled or task_paused(self.actor, self.identity, "miniapp:spirit-beast-contract"):
            return False
        lock = getattr(self.actor, "beast_lock", None)
        if lock is not None:
            async with lock:
                return await self._run_cycle_unlocked()
        return await self._run_cycle_unlocked()

    async def _run_cycle_unlocked(self) -> bool:
        require_command(self.actor, self.identity, "miniapp:spirit-beast-contract")
        state = self.state
        self._record_enabled_state()
        state["beast_contract_interaction_last_attempt_time"] = contract_time()
        state["beast_contract_interaction_last_error"] = ""
        self._save()
        try:
            if self.transport is None:
                raise MiniAppBeastError("invalid_entry_url")
            snapshot = await self.transport.spirit_beast_snapshot(self.identity)
            beasts = list((snapshot or {}).get("beasts") or [])
            if not beasts:
                raise MiniAppBeastError("beast_roster_empty")
        except asyncio.CancelledError:
            raise
        except SectTaskStopped:
            return False
        except MiniAppCircuitOpenError as exc:
            wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
            state["beast_contract_interaction_last_error"] = exc.code
            state["beast_contract_interaction_last_error_time"] = contract_time()
            state["beast_contract_interaction_next_time"] = contract_add_seconds(wait)
            self._save()
            self.log.info(
                "Wan Beast Valley contract paused by upstream circuit until %s",
                exc.retry_at or f"in {wait}s",
            )
            return False
        except Exception as exc:
            code = _error_code(exc)
            state["beast_contract_interaction_last_error"] = code
            state["beast_contract_interaction_last_error_time"] = contract_time()
            state["beast_contract_interaction_next_time"] = contract_add_seconds(self.retry_seconds)
            state["beast_contract_interaction_failure_count"] = int(
                state.get("beast_contract_interaction_failure_count") or 0
            ) + 1
            self._save()
            self.log.error("Wan Beast Valley contract roster failed: %s", code, exc_info=True)
            return False

        self._update_cache(beasts)
        results = self._results()
        current_ids = {str(int(item.get("id") or 0)) for item in beasts if int(item.get("id") or 0) > 0}
        for stale_id in set(results) - current_ids:
            results.pop(stale_id, None)

        due: list[dict[str, Any]] = []
        for beast in beasts:
            try:
                beast_id = int(beast.get("id") or 0)
            except (TypeError, ValueError):
                beast_id = 0
            if beast_id <= 0:
                continue
            previous = results.get(str(beast_id)) or {}
            if contract_seconds_until(previous.get("next_time")) <= 0:
                due.append(beast)

        active_cycle = str(state.get("beast_contract_interaction_active_cycle") or "")
        target_next = str(state.get("beast_contract_interaction_cycle_target_time") or "")
        if not due:
            known_next = [
                parse_contract_time((results.get(str(int(beast.get("id") or 0))) or {}).get("next_time"))
                for beast in beasts
                if int(beast.get("id") or 0) > 0
            ]
            known_next = [value for value in known_next if value is not None]
            state["beast_contract_interaction_next_time"] = (
                contract_time(min(known_next)) if known_next else contract_add_seconds(self.retry_seconds)
            )
            self._save()
            return True
        if not active_cycle:
            cycle_start = contract_now()
            active_cycle = contract_time(cycle_start)
            target_next = contract_add_seconds(self.interval_seconds, cycle_start)
            state["beast_contract_interaction_active_cycle"] = active_cycle
            state["beast_contract_interaction_cycle_target_time"] = target_next
        elif not parse_contract_time(target_next):
            target_next = contract_add_seconds(self.interval_seconds)
            state["beast_contract_interaction_cycle_target_time"] = target_next

        failed = 0
        batch_details: list[str] = []
        for index, beast in enumerate(due):
            beast_id = int(beast["id"])
            result_key = str(beast_id)
            name = str(beast.get("full_name") or beast_id)
            before = int(beast.get("stamina") or 0)
            item_state = dict(results.get(result_key) or {})
            item_state.update({
                "beast_id": beast_id,
                "name": name,
                "last_attempt_time": contract_time(),
                "stamina_before": before,
            })
            try:
                require_command(self.actor, self.identity, "miniapp:spirit-beast-contract")
                payload = await self.transport.spirit_beast_interaction(
                    self.identity,
                    beast_id,
                    "安抚",
                )
                _apply_action_beast(beast, _find_action_beast(payload, beast_id))
                now = contract_now()
                item_state.update({
                    "last_success_time": contract_time(now),
                    "next_time": target_next,
                    "cycle_time": active_cycle,
                    "status": "success",
                    "message": _message(payload)[:240],
                    "stamina_after": int(beast.get("stamina") or before),
                    "error": "",
                })
                state["beast_contract_interaction_success_count"] = int(
                    state.get("beast_contract_interaction_success_count") or 0
                ) + 1
                batch_details.append(
                    f"{name} 体力{before}→{item_state['stamina_after']}"
                )
            except asyncio.CancelledError:
                results[result_key] = item_state
                self._save()
                raise
            except (MiniAppCircuitOpenError, SectTaskStopped):
                results[result_key] = item_state
                self._save()
                raise
            except Exception as exc:
                code = _error_code(exc)
                failed += 1
                item_state.update({
                    "next_time": contract_add_seconds(self.retry_seconds),
                    "status": "failed",
                    "error": code,
                })
                state["beast_contract_interaction_failure_count"] = int(
                    state.get("beast_contract_interaction_failure_count") or 0
                ) + 1
                state["beast_contract_interaction_last_error"] = f"{name}: {code}"[:240]
                state["beast_contract_interaction_last_error_time"] = contract_time()
                batch_details.append(f"{name} 失败({code})")
            results[result_key] = item_state
            self._save()
            if index + 1 < len(due) and self.action_delay_seconds > 0:
                await asyncio.sleep(self.action_delay_seconds)

        next_times = [
            (results.get(str(int(beast.get("id") or 0))) or {}).get("next_time")
            for beast in beasts
            if int(beast.get("id") or 0) > 0
        ]
        parsed_next = [parse_contract_time(value) for value in next_times]
        parsed_next = [value for value in parsed_next if value is not None]
        state["beast_contract_interaction_next_time"] = (
            contract_time(min(parsed_next)) if parsed_next else contract_add_seconds(self.retry_seconds)
        )
        state["beast_contract_interaction_last_processed_count"] = len(due)
        state["beast_contract_interaction_last_failure_count"] = failed
        if not failed and all(
            (results.get(str(int(beast.get("id") or 0))) or {}).get("status") == "success"
            and (results.get(str(int(beast.get("id") or 0))) or {}).get("cycle_time") == active_cycle
            for beast in beasts
            if int(beast.get("id") or 0) > 0
        ):
            state["beast_contract_interaction_last_completed_time"] = contract_time()
            state["beast_contract_interaction_completed_count"] = int(
                state.get("beast_contract_interaction_completed_count") or 0
            ) + 1
            state["beast_contract_interaction_last_error"] = ""
            state["beast_contract_interaction_active_cycle"] = ""
            state["beast_contract_interaction_cycle_target_time"] = ""
        self._save()
        summary = (
            f"共处理 {len(due)} 只，成功 {len(due) - failed}，失败 {failed}"
            f"：{'；'.join(batch_details)}"
        )
        if failed:
            self.log.error("Mini App [%s] 万兽谷灵兽安抚部分失败：%s", self.identity, summary)
        else:
            self.log.info("IN [Mini App | %s]:\n万兽谷灵兽安抚 -> %s", self.identity, summary)
        return failed == 0

    async def run(self) -> None:
        self._record_enabled_state()
        self._save()
        if not self.enabled:
            self.log.info("Spirit-beast contract scheduler disabled or account is not Wanling")
            return
        await self.actor.startup_done.wait()
        self.log.info(
            "Wan Beast Valley contract scheduler started: interval=%ss retry=%ss",
            self.interval_seconds,
            self.retry_seconds,
        )
        while getattr(self.actor, "is_running", True):
            if not self.enabled or task_paused(self.actor, self.identity, "miniapp:spirit-beast-contract"):
                await asyncio.sleep(60)
                continue
            pause_event = getattr(self.actor, "pause_event", None)
            if pause_event is not None and not pause_event.is_set():
                await asyncio.sleep(60)
                continue
            wait = contract_seconds_until(
                self.state.get("beast_contract_interaction_next_time")
            )
            if wait > 0:
                await asyncio.sleep(min(wait, 300))
                continue
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except SectTaskStopped:
                await asyncio.sleep(60)
            except MiniAppCircuitOpenError as exc:
                wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
                self.state["beast_contract_interaction_last_error"] = exc.code
                self.state["beast_contract_interaction_last_error_time"] = contract_time()
                self.state["beast_contract_interaction_next_time"] = contract_add_seconds(wait)
                self._save()
                self.log.info(
                    "Wan Beast Valley contract paused by upstream circuit until %s",
                    exc.retry_at or f"in {wait}s",
                )
                await asyncio.sleep(max(60, wait))
