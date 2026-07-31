#!/usr/bin/env python3
"""Automatic participation in the Qing Yuanzi Telegram Mini App world boss."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import events

from log_utils import is_game_bot_sender, resolve_target_chat_id
from miniapp_beast import (
    MiniAppBeastError,
    _post_json,
    miniapp_origin,
    request_webview_init_data,
)


WORLD_BOSS_BUTTON_TEXT = "进入真仙战场"
WORLD_BOSS_TITLE_MARKERS = ("世界通告", "真仙试锋开启")
WORLD_BOSS_TOKEN_PREFIX = "qyz_"
WORLD_BOSS_IDENTITY = "主魂"
WORLD_BOSS_HOLD_MS = 900
WORLD_BOSS_STANCE = "强攻"
WORLD_BOSS_ENTRY_WAIT_SECONDS = 110
WORLD_BOSS_RECOVERY_WINDOW_SECONDS = 120
WORLD_BOSS_FINISH_GRACE_SECONDS = 2.2
WORLD_BOSS_HISTORY_LIMIT = 20
WORLD_BOSS_SCAN_LIMIT = 30
WORLD_BOSS_TIMEOUT_SECONDS = 20

AUTH_TOKEN_ERRORS = {
    "boss_token_missing",
    "boss_token_expired",
    "boss_token_used",
}
TRANSIENT_WORLD_BOSS_ERRORS = {
    "api_timeout",
    "api_unreachable",
    "request_failed",
    "bad_response",
    "server_busy",
    "server_error",
    "rate_limited",
    "timeouterror",
    "urlerror",
}
RETRY_HTTP_STATUSES = {429, 502, 503, 504}
COMPLETED_EVENT_STATUSES = {
    "completed",
    "already_completed",
    "join_closed",
    "not_enough_participants",
    "event_closed",
    "expired",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__.lower())


def _message_text(message: Any) -> str:
    return str(
        getattr(message, "raw_text", "")
        or getattr(message, "text", "")
        or ""
    )


def _normalized_button_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("\ufe0f", "")).strip()


def _button_url(button: Any) -> str:
    raw = getattr(button, "button", None)
    return str(getattr(button, "url", "") or getattr(raw, "url", "") or "").strip()


@dataclass(frozen=True, slots=True)
class WorldBossEntry:
    message_id: int
    origin: str
    bot_username: str
    fingerprint: str
    token: str = field(repr=False)


def extract_world_boss_entry(
    message: Any,
    *,
    sender_username: str = "",
) -> WorldBossEntry | None:
    """Extract and validate the dynamic world-boss entry without exposing its token."""

    text = _message_text(message)
    if not all(marker in text for marker in WORLD_BOSS_TITLE_MARKERS):
        return None

    selected_url = ""
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            if _normalized_button_text(getattr(button, "text", "")) != WORLD_BOSS_BUTTON_TEXT:
                continue
            selected_url = _button_url(button)
            if selected_url:
                break
        if selected_url:
            break
    if not selected_url:
        return None

    parsed = urllib.parse.urlsplit(selected_url)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        return None
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        return None
    bot_username = path_parts[0].lstrip("@").casefold()
    if not re.fullmatch(r"[a-z0-9_]{5,64}", bot_username) or not bot_username.endswith("_bot"):
        return None
    expected_sender = str(sender_username or "").strip().lstrip("@").casefold()
    if expected_sender and expected_sender != bot_username:
        return None

    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if (
        not token.startswith(WORLD_BOSS_TOKEN_PREFIX)
        or len(token) > 160
        or not re.fullmatch(r"[A-Za-z0-9_-]+", token)
    ):
        return None
    message_id = int(getattr(message, "id", 0) or 0)
    if message_id <= 0:
        return None
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return WorldBossEntry(
        message_id=message_id,
        origin=miniapp_origin(selected_url),
        bot_username=bot_username,
        fingerprint=fingerprint,
        token=token,
    )


def select_main_identity_choice(actor: Any, choices: Any) -> int | None:
    """Select only an explicitly identifiable personal/main-soul player."""

    rows = [item for item in (choices or []) if isinstance(item, dict)]
    personal = [item for item in rows if str(item.get("source") or "").casefold() == "personal"]
    if len(personal) == 1:
        try:
            return int(personal[0].get("playerId"))
        except (TypeError, ValueError):
            return None

    main_names: set[str] = set()
    identity_usernames = getattr(actor, "identity_usernames", {}) or {}
    configured = identity_usernames.get(WORLD_BOSS_IDENTITY, []) if isinstance(identity_usernames, dict) else []
    if isinstance(configured, str):
        configured = [configured]
    for value in configured or []:
        key = str(value or "").strip().lstrip("@").casefold()
        if key:
            main_names.add(key)
    me = getattr(actor, "my_info", None)
    if me is not None:
        for value in (getattr(me, "username", ""), getattr(me, "first_name", "")):
            key = str(value or "").strip().lstrip("@").casefold()
            if key:
                main_names.add(key)

    matching_ids: set[int] = set()
    for item in rows:
        source_label = str(item.get("sourceLabel") or "").strip()
        values = {
            str(item.get(key) or "").strip().lstrip("@").casefold()
            for key in ("username", "displayName", "daoName")
        }
        if source_label in {"主魂", "本体", "本人", "个人"} or (main_names & values):
            try:
                matching_ids.add(int(item.get("playerId")))
            except (TypeError, ValueError):
                continue
    if len(matching_ids) == 1:
        return next(iter(matching_ids))

    if not (getattr(actor, "avatars", []) or []) and len(rows) == 1:
        try:
            return int(rows[0].get("playerId"))
        except (TypeError, ValueError):
            return None
    return None


class _ProcessLease:
    """Prevent full and restricted workers from fighting on one account concurrently."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            # The VPS uses flock. The in-process monitor lock is sufficient for local runs.
            return True
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        if os.name != "nt":
            try:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        self.handle.close()
        self.handle = None


class WorldBossMonitor:
    def __init__(
        self,
        actor: Any,
        account: str,
        *,
        logger: logging.Logger | None = None,
        transport: Any = None,
        post_json: Any = None,
        sleep: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
        finish_grace_seconds: float = WORLD_BOSS_FINISH_GRACE_SECONDS,
    ) -> None:
        self.actor = actor
        self.client = actor.client
        self.account = str(account or "").strip()
        self.log = logger or logging.getLogger(f"world_boss.{self.account}")
        self.transport = transport
        self.post_json = post_json
        self.sleep = sleep
        self.monotonic = monotonic
        self.finish_grace_seconds = max(0.0, float(finish_grace_seconds))
        settings = (getattr(actor, "config", {}) or {}).get("world_boss") or {}
        self.enabled = bool(settings.get("enabled", True))
        self.timeout = max(5, min(60, int(settings.get("timeout_seconds") or WORLD_BOSS_TIMEOUT_SECONDS)))
        self.target_chat: Any = None
        self._new_handler: Any = None
        self._edit_handler: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight_messages: set[int] = set()
        self._inflight_fingerprints: set[str] = set()
        self._fight_lock = asyncio.Lock()

    def _save(self) -> None:
        saver = getattr(self.actor, "save_state", None)
        if callable(saver):
            saver()

    def _history(self) -> list[dict[str, Any]]:
        state = getattr(self.actor, "state", None)
        if not isinstance(state, dict):
            return []
        value = state.get("world_boss_events")
        if not isinstance(value, list):
            value = []
            state["world_boss_events"] = value
        return value

    def _event_status(self, fingerprint: str) -> str:
        for item in reversed(self._history()):
            if isinstance(item, dict) and item.get("fingerprint") == fingerprint:
                return str(item.get("status") or "")
        return ""

    def _record(self, entry: WorldBossEntry, status: str, **updates: Any) -> None:
        history = self._history()
        record = next(
            (
                item
                for item in history
                if isinstance(item, dict) and item.get("fingerprint") == entry.fingerprint
            ),
            None,
        )
        if record is None:
            record = {
                "message_id": entry.message_id,
                "fingerprint": entry.fingerprint,
            }
            history.append(record)
        record.update({"status": status, "updated_at": _now_text(), **updates})
        del history[:-WORLD_BOSS_HISTORY_LIMIT]
        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state.update(
                {
                    "world_boss_last_message_id": entry.message_id,
                    "world_boss_last_status": status,
                    "world_boss_last_updated_at": record["updated_at"],
                }
            )
            if updates.get("error") is not None:
                state["world_boss_last_error"] = str(updates.get("error") or "")
        self._save()

    def _lease(self) -> _ProcessLease:
        state_file = str(getattr(self.actor, "state_file", "") or "").strip()
        base = Path(state_file).resolve().parent if state_file else Path(__file__).resolve().parent
        return _ProcessLease(base / f".world_boss_{self.account}.lock")

    async def install(self) -> bool:
        if not self.enabled:
            return False
        try:
            self.target_chat = await resolve_target_chat_id(
                self.client,
                getattr(self.actor, "target_chat_id", ""),
                self.log,
            )

            async def new_handler(event: Any) -> None:
                await self.process_message(event.message, source="new")

            async def edit_handler(event: Any) -> None:
                await self.process_message(event.message, source="edited")

            self._new_handler = new_handler
            self._edit_handler = edit_handler
            self.client.add_event_handler(new_handler, events.NewMessage(chats=self.target_chat))
            self.client.add_event_handler(edit_handler, events.MessageEdited(chats=self.target_chat))
        except Exception as exc:
            state = getattr(self.actor, "state", None)
            if isinstance(state, dict):
                state["world_boss_monitor_active"] = False
                state["world_boss_last_error"] = _error_code(exc)
                self._save()
            self.log.error("World Boss monitor setup failed: %s", _error_code(exc), exc_info=True)
            return False

        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state["world_boss_monitor_active"] = True
            state["world_boss_monitor_started_at"] = _now_text()
            state["world_boss_last_error"] = ""
            self._save()
        self.log.info("[%s] Qing Yuanzi world-boss monitor ready", self.account)

        try:
            recent = await self.client.get_messages(self.target_chat, limit=WORLD_BOSS_SCAN_LIMIT)
            # Telegram returns newest first. Only recover the latest eligible room so
            # an older near-expiry notice cannot hold the per-account fight lock.
            for message in list(recent or []):
                if await self.process_message(message, source="startup"):
                    break
        except Exception as exc:
            self.log.warning("World Boss startup recovery scan failed: %s", _error_code(exc))
        return True

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        remover = getattr(self.client, "remove_event_handler", None)
        if callable(remover):
            if self._new_handler is not None:
                remover(self._new_handler)
            if self._edit_handler is not None:
                remover(self._edit_handler)
        state = getattr(self.actor, "state", None)
        if isinstance(state, dict):
            state["world_boss_monitor_active"] = False
            self._save()

    @staticmethod
    def _message_recent_enough(message: Any) -> bool:
        message_date = getattr(message, "date", None)
        if not isinstance(message_date, datetime):
            return True
        if message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - message_date.astimezone(timezone.utc)).total_seconds()
        return -30 <= age <= WORLD_BOSS_RECOVERY_WINDOW_SECONDS

    async def process_message(self, message: Any, *, source: str = "new") -> bool:
        entry = extract_world_boss_entry(message)
        if entry is None or (source == "startup" and not self._message_recent_enough(message)):
            return False
        try:
            sender = await message.get_sender()
        except Exception:
            return False
        if not sender or not is_game_bot_sender(self.actor, sender):
            return False
        sender_username = str(getattr(sender, "username", "") or "")
        entry = extract_world_boss_entry(message, sender_username=sender_username)
        if entry is None:
            return False
        if self._event_status(entry.fingerprint) in COMPLETED_EVENT_STATUSES:
            return False
        if (
            entry.message_id in self._inflight_messages
            or entry.fingerprint in self._inflight_fingerprints
        ):
            return False

        self._inflight_messages.add(entry.message_id)
        self._inflight_fingerprints.add(entry.fingerprint)
        self._record(entry, "queued", source=source, error="")
        task = asyncio.create_task(
            self._run_entry(entry),
            name=f"world_boss_{self.account}_{entry.message_id}",
        )
        self._tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            self._inflight_messages.discard(entry.message_id)
            self._inflight_fingerprints.discard(entry.fingerprint)
            if completed.cancelled():
                return
            try:
                error = completed.exception()
            except asyncio.CancelledError:
                return
            if error:
                self.log.critical(
                    "World Boss task stopped unexpectedly: %s",
                    _error_code(error),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(done)
        return True

    async def _run_entry(self, entry: WorldBossEntry) -> None:
        async with self._fight_lock:
            lease = self._lease()
            if not lease.acquire():
                self._record(entry, "delegated", error="another_process_active")
                self.log.info(
                    "[%s] Qing Yuanzi event %s is already handled by the other account process",
                    self.account,
                    entry.message_id,
                )
                return
            try:
                self._record(entry, "running", started_at=_now_text(), error="")
                self.log.info("OUT [Mini App | 主魂]:\n青元子世界 Boss 自动参战")
                outcome = await self._participate(entry)
                summary = self._outcome_summary(outcome)
                self._record(
                    entry,
                    "completed",
                    completed_at=_now_text(),
                    error="",
                    grade=str(outcome.get("grade") or ""),
                    score=int(outcome.get("score") or 0),
                    hit_count=int(outcome.get("hit_count") or 0),
                    perfect_count=int(outcome.get("perfect_count") or 0),
                    window_count=int(outcome.get("window_count") or 0),
                )
                self.log.info("IN [Mini App | 主魂]:\n青元子世界 Boss -> %s", summary)
            except asyncio.CancelledError:
                self._record(entry, "cancelled", error="cancelled")
                raise
            except MiniAppBeastError as exc:
                code = exc.code
                status_map = {
                    "boss_action_limit": "already_completed",
                    "boss_join_closed": "join_closed",
                    "boss_not_enough_participants": "not_enough_participants",
                    "boss_event_closed": "event_closed",
                    "boss_token_expired": "expired",
                    "boss_token_missing": "expired",
                    "boss_token_used": "expired",
                }
                status = status_map.get(code, "failed")
                self._record(entry, status, error=code)
                if code == "boss_action_limit":
                    self.log.info(
                        "IN [Mini App | 主魂]:\n青元子世界 Boss -> 服务器确认本轮已完成"
                    )
                elif code == "boss_not_enough_participants":
                    self.log.info(
                        "IN [Mini App | 主魂]:\n青元子世界 Boss -> 入场人数不足，本轮未开战"
                    )
                else:
                    self.log.error("Mini App [主魂] 青元子世界 Boss失败：%s", code)
            except Exception as exc:
                code = _error_code(exc)
                self._record(entry, "failed", error=code)
                self.log.error(
                    "Mini App [主魂] 青元子世界 Boss失败：%s",
                    code,
                    exc_info=True,
                )
            finally:
                lease.release()

    def _discover_transport(self) -> Any:
        if self.transport is not None:
            return self.transport
        for owner_name in (
            "_miniapp_command_router",
            "_miniapp_beast_contract",
            "_restricted_miniapp_worker",
        ):
            owner = getattr(self.actor, owner_name, None)
            transport = getattr(owner, "transport", None)
            if transport is not None:
                return transport
        return None

    async def _main_player_id(self) -> int | None:
        transport = self._discover_transport()
        if transport is None:
            return None
        try:
            initializer = getattr(transport, "initialize", None)
            if callable(initializer):
                result = initializer()
                if inspect.isawaitable(result):
                    await result
            return int(transport.player_id(WORLD_BOSS_IDENTITY))
        except Exception as exc:
            self.log.warning(
                "World Boss fixed-entry main identity lookup failed (%s); using event choices",
                _error_code(exc),
            )
            return None

    async def _request(
        self,
        origin: str,
        path: str,
        payload: dict[str, Any],
        *,
        retries: int = 0,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        for attempt in range(max(0, retries) + 1):
            try:
                return await _post_json(
                    origin,
                    path,
                    payload,
                    int(timeout or self.timeout),
                    post_json=self.post_json,
                )
            except MiniAppBeastError as exc:
                retryable = exc.status in RETRY_HTTP_STATUSES or exc.code in TRANSIENT_WORLD_BOSS_ERRORS
                if attempt >= retries or not retryable:
                    raise
                await self.sleep(min(2.5, 0.45 * (attempt + 1)))
        raise MiniAppBeastError("request_failed")

    async def _start_request(
        self,
        entry: WorldBossEntry,
        init_data: str,
        token: str,
        player_id: int | None,
    ) -> dict[str, Any]:
        return await self._request(
            entry.origin,
            "/api/miniapp/xianxia-world-boss/start",
            {
                "token": token,
                "initData": init_data,
                "playerId": player_id if player_id is not None else "",
            },
            retries=2,
            timeout=min(self.timeout, 12),
        )

    async def _wait_for_challenge(
        self,
        entry: WorldBossEntry,
        init_data: str,
        player_id: int | None,
    ) -> tuple[str, dict[str, Any]]:
        token = entry.token
        deadline = self.monotonic() + WORLD_BOSS_ENTRY_WAIT_SECONDS
        was_waiting = False
        while self.monotonic() < deadline:
            try:
                payload = await self._start_request(entry, init_data, token, player_id)
            except MiniAppBeastError as exc:
                if exc.code == "boss_battle_not_started" or (
                    was_waiting and exc.code in AUTH_TOKEN_ERRORS
                ):
                    if exc.code in AUTH_TOKEN_ERRORS:
                        token = entry.token
                    await self.sleep(1.2)
                    continue
                raise

            if payload.get("needsIdentitySelection"):
                choices = payload.get("identityChoices") or []
                available_ids = {
                    int(item.get("playerId"))
                    for item in choices
                    if isinstance(item, dict) and str(item.get("playerId") or "").isdigit()
                }
                if player_id is None:
                    player_id = select_main_identity_choice(self.actor, choices)
                if player_id is None or (available_ids and player_id not in available_ids):
                    raise MiniAppBeastError("world_boss_main_identity_missing")
                token = entry.token
                continue

            session_token = str(payload.get("sessionToken") or token).strip()
            if not session_token:
                raise MiniAppBeastError("boss_token_missing")
            token = session_token
            boss = payload.get("boss") if isinstance(payload.get("boss"), dict) else {}
            if int(boss.get("actionsUsed") or 0) > 0 or (
                boss.get("actionsRemaining") is not None
                and int(boss.get("actionsRemaining") or 0) <= 0
            ):
                raise MiniAppBeastError("boss_action_limit")
            if boss.get("failureReason") == "not_enough_participants":
                raise MiniAppBeastError("boss_not_enough_participants")
            challenge = payload.get("challenge")
            if isinstance(challenge, dict) and challenge.get("challengeId"):
                return token, payload

            was_waiting = True
            remain = float(
                boss.get("joinRemainingSeconds")
                or (payload.get("room") or {}).get("joinRemainingSeconds")
                or 0
            )
            await self.sleep(5.0 if remain > 5 else 1.2)
        raise MiniAppBeastError("boss_challenge_timeout")

    @staticmethod
    def _windows(challenge: dict[str, Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in challenge.get("windows") or []:
            if not isinstance(raw, dict):
                continue
            window_id = str(raw.get("id") or "").strip()
            try:
                center_ms = int(raw.get("centerMs") or 0)
                hit_ms = max(1, int(raw.get("hitMs") or 460))
                perfect_ms = max(1, int(raw.get("perfectMs") or 150))
            except (TypeError, ValueError):
                continue
            if (
                not window_id
                or window_id in seen
                or center_ms < 0
                or center_ms > 120_000
            ):
                continue
            seen.add(window_id)
            normalized.append(
                {
                    "id": window_id,
                    "centerMs": center_ms,
                    "hitMs": hit_ms,
                    "perfectMs": perfect_ms,
                }
            )
        normalized.sort(key=lambda item: item["centerMs"])
        if not normalized or len(normalized) > 64:
            raise MiniAppBeastError("boss_windows_invalid")
        return normalized

    async def _hit_window(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        challenge_id: str,
        battle_start: float,
        window: dict[str, Any],
    ) -> dict[str, Any]:
        target = battle_start + window["centerMs"] / 1000.0
        wait = target - self.monotonic()
        if wait > 0:
            await self.sleep(wait)
        action = {
            "t": window["centerMs"],
            "holdMs": WORLD_BOSS_HOLD_MS,
            "stance": WORLD_BOSS_STANCE,
        }
        try:
            payload = await self._request(
                entry.origin,
                "/api/miniapp/xianxia-world-boss/hit",
                {
                    "token": session_token,
                    "initData": init_data,
                    "challengeId": challenge_id,
                    "windowId": window["id"],
                    "elapsedMs": window["centerMs"],
                    "holdMs": WORLD_BOSS_HOLD_MS,
                },
                retries=2,
                timeout=min(self.timeout, 10),
            )
            hit = payload.get("hit") if isinstance(payload.get("hit"), dict) else {}
            return {
                "action": action,
                "ok": True,
                "damage": float(hit.get("damageYi") or 0),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"action": action, "ok": False, "damage": 0.0, "error": _error_code(exc)}

    async def _fight(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        challenge = payload.get("challenge") or {}
        challenge_id = str(challenge.get("challengeId") or "").strip()
        if not challenge_id:
            raise MiniAppBeastError("boss_challenge_missing")
        windows = self._windows(challenge)

        started_request_at = self.monotonic()
        sync = await self._request(
            entry.origin,
            "/api/miniapp/xianxia-world-boss/begin",
            {
                "token": session_token,
                "initData": init_data,
                "challengeId": challenge_id,
            },
            retries=1,
            timeout=min(self.timeout, 10),
        )
        response_at = self.monotonic()
        round_trip = max(0.0, response_at - started_request_at)
        starts_in = max(0.0, float(sync.get("startsInMs") or 0) / 1000.0 - round_trip / 2.0)
        battle_start = response_at + starts_in

        tasks = [
            asyncio.create_task(
                self._hit_window(
                    entry,
                    init_data,
                    session_token,
                    challenge_id,
                    battle_start,
                    window,
                )
            )
            for window in windows
        ]
        hit_results = await asyncio.gather(*tasks)
        last_end_ms = max(item["centerMs"] + item["hitMs"] for item in windows)
        finish_at = battle_start + last_end_ms / 1000.0 + self.finish_grace_seconds
        finish_wait = finish_at - self.monotonic()
        if finish_wait > 0:
            await self.sleep(finish_wait)

        actions = [item["action"] for item in hit_results]
        actions.sort(key=lambda item: item["t"])
        successful_hits = sum(1 for item in hit_results if item["ok"])
        failed_hits = len(hit_results) - successful_hits
        realtime_damage = any(float(item.get("damage") or 0) > 0 for item in hit_results)
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        player_hp = max(1, int(player.get("maxHp") or 100))
        elapsed_ms = max(0, int((self.monotonic() - battle_start) * 1000))
        duration_ms = max(
            last_end_ms + int(self.finish_grace_seconds * 1000),
            actions[-1]["t"],
            elapsed_ms,
        )
        proof = {
            "mode": "qyz_focus_burst_v2",
            "challengeId": challenge_id,
            "stance": WORLD_BOSS_STANCE,
            "durationMs": duration_ms,
            "playerHp": player_hp,
            "dead": False,
            "actions": actions,
            "clientStats": {
                "dodges": len(actions),
                "grazes": 0,
                "damage": 0,
                "hits": len(actions),
                "perfects": len(actions),
                "combo": len(actions),
                "bestCombo": len(actions),
            },
            "realtimeDamageApplied": realtime_damage,
        }
        finished = await self._request(
            entry.origin,
            "/api/miniapp/xianxia-world-boss/finish",
            {
                "token": session_token,
                "initData": init_data,
                "bossProof": proof,
            },
            retries=1,
        )
        result = finished.get("result") if isinstance(finished.get("result"), dict) else {}
        return {
            "grade": str(result.get("grade") or ""),
            "score": int(result.get("score") or 0),
            "player_hp": int(result.get("player_hp") if result.get("player_hp") is not None else player_hp),
            "hit_count": successful_hits,
            "perfect_count": len(actions),
            "failed_hit_count": failed_hits,
            "window_count": len(windows),
        }

    async def _participate(self, entry: WorldBossEntry) -> dict[str, Any]:
        init_data = await request_webview_init_data(
            self.client,
            entry.bot_username,
            entry.token,
        )
        player_id = await self._main_player_id()
        session_token, payload = await self._wait_for_challenge(
            entry,
            init_data,
            player_id,
        )
        return await self._fight(entry, init_data, session_token, payload)

    @staticmethod
    def _outcome_summary(outcome: dict[str, Any]) -> str:
        grade = str(outcome.get("grade") or "已结算")
        score = int(outcome.get("score") or 0)
        hits = int(outcome.get("hit_count") or 0)
        perfects = int(outcome.get("perfect_count") or 0)
        total = int(outcome.get("window_count") or 0)
        hp = int(outcome.get("player_hp") or 0)
        failed = int(outcome.get("failed_hit_count") or 0)
        summary = f"{grade} {score}分；命中 {hits}/{total}，完美 {perfects}，余血 {hp}"
        if failed:
            summary += f"；{failed} 次实时回传失败"
        return summary


async def install_world_boss_monitor(
    actor: Any,
    account: str,
    *,
    logger: logging.Logger | None = None,
    transport: Any = None,
) -> WorldBossMonitor:
    monitor = WorldBossMonitor(
        actor,
        account,
        logger=logger,
        transport=transport,
    )
    await monitor.install()
    setattr(actor, "_world_boss_monitor", monitor)
    return monitor
