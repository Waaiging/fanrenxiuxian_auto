#!/usr/bin/env python3
"""Automatic participation in the Qing Yuanzi Telegram Mini App world boss."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import inspect
import json
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

from automation_settings import world_boss_identities_for_account
from log_utils import is_game_bot_sender, resolve_actor_target_chats
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
WORLD_BOSS_HOLD_MS = 1200
WORLD_BOSS_STANCE = "强攻"
WORLD_BOSS_ENTRY_WAIT_SECONDS = 110
WORLD_BOSS_RECOVERY_WINDOW_SECONDS = 120
WORLD_BOSS_FINISH_GRACE_SECONDS = 2.2
WORLD_BOSS_HISTORY_LIMIT = 20
WORLD_BOSS_SCAN_LIMIT = 30
WORLD_BOSS_TIMEOUT_SECONDS = 20
WORLD_BOSS_DIAGNOSTIC_VERSION = 1
WORLD_BOSS_ACCOUNT_OFFSET_SLOTS = {
    "main": -4,
    "sub": -3,
    "xiaohao": -2,
    "waaiging": -1,
}

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

WORLD_BOSS_DIAGNOSTIC_SENSITIVE_PARTS = (
    "token",
    "initdata",
    "authorization",
    "cookie",
    "secret",
    "signature",
)


def _diagnostic_value(value: Any, *, depth: int = 0) -> Any:
    """Keep useful server metadata while excluding credentials and large payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if depth >= 4:
        return str(value)[:240]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:40]:
            key = str(raw_key or "")[:80]
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in WORLD_BOSS_DIAGNOSTIC_SENSITIVE_PARTS):
                continue
            result[key] = _diagnostic_value(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_diagnostic_value(item, depth=depth + 1) for item in list(value)[:20]]
    return str(value)[:240]


def _error_diagnostics(exc: BaseException) -> dict[str, Any]:
    details = _diagnostic_value(getattr(exc, "details", {}))
    return details if isinstance(details, dict) else {}


class _PersistentWorldBossJsonClient:
    """One keep-alive HTTP connection matching the browser's fetch behavior.

    The realtime hit window is only a few hundred milliseconds wide. Opening a new
    TLS connection for every hit regularly takes more than one second on the VPS,
    so all requests for one account/event are serialized over one warm connection.
    """

    def __init__(self, origin: str) -> None:
        parsed = urllib.parse.urlsplit(str(origin or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MiniAppBeastError("invalid_entry_url")
        self.origin = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        )
        self.scheme = parsed.scheme
        self.hostname = parsed.hostname
        self.port = parsed.port
        self.connection: http.client.HTTPConnection | None = None
        self.lock = asyncio.Lock()

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _new_connection(self, timeout: int) -> http.client.HTTPConnection:
        connection_type = (
            http.client.HTTPSConnection
            if self.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(
            self.hostname,
            port=self.port,
            timeout=max(5, int(timeout or WORLD_BOSS_TIMEOUT_SECONDS)),
        )

    def _post_sync(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        target = urllib.parse.urlsplit(
            urllib.parse.urljoin(self.origin.rstrip("/") + "/", str(path).lstrip("/"))
        )
        request_path = target.path or "/"
        if target.query:
            request_path += "?" + target.query
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "Origin": self.origin,
            "Referer": self.origin.rstrip("/") + "/miniapp/xianxia-world-boss",
            "User-Agent": "Mozilla/5.0 Telegram-Android/11.0",
            "Connection": "keep-alive",
        }

        for connection_attempt in range(2):
            if self.connection is None:
                self.connection = self._new_connection(timeout)
            else:
                self.connection.timeout = max(5, int(timeout or WORLD_BOSS_TIMEOUT_SECONDS))
            try:
                self.connection.request("POST", request_path, body=body, headers=headers)
                response = self.connection.getresponse()
                status = int(response.status or 0)
                response_body = response.read().decode("utf-8", errors="replace")
                if response.will_close:
                    self.close()
                break
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                if connection_attempt == 0:
                    continue
                raise MiniAppBeastError(type(exc).__name__.lower()) from exc
        else:
            raise MiniAppBeastError("request_failed")

        try:
            data = json.loads(response_body)
        except Exception as exc:
            raise MiniAppBeastError("invalid_json", status) from exc
        if not isinstance(data, dict):
            raise MiniAppBeastError("invalid_response", status)
        if status >= 400 or data.get("ok") is False:
            error = MiniAppBeastError(data.get("error") or f"http_{status}", status)
            error.details = _diagnostic_value(data)
            raise error
        return data

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        async with self.lock:
            return await asyncio.to_thread(self._post_sync, path, payload, timeout)


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
    chat_id: int | None
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
        chat_id=getattr(message, "chat_id", None),
        origin=miniapp_origin(selected_url),
        bot_username=bot_username,
        fingerprint=fingerprint,
        token=token,
    )


def select_identity_choice(actor: Any, choices: Any, identity: str) -> int | None:
    """Select only the requested, explicitly identifiable Mini App player."""

    identity = str(identity or WORLD_BOSS_IDENTITY).strip() or WORLD_BOSS_IDENTITY
    rows = [item for item in (choices or []) if isinstance(item, dict)]
    if identity == WORLD_BOSS_IDENTITY:
        personal = [item for item in rows if str(item.get("source") or "").casefold() == "personal"]
        if len(personal) == 1:
            try:
                return int(personal[0].get("playerId"))
            except (TypeError, ValueError):
                return None

    expected_names = {identity.casefold()}
    identity_usernames = getattr(actor, "identity_usernames", {}) or {}
    configured = identity_usernames.get(identity, []) if isinstance(identity_usernames, dict) else []
    if isinstance(configured, str):
        configured = [configured]
    for value in configured or []:
        key = str(value or "").strip().lstrip("@").casefold()
        if key:
            expected_names.add(key)
    me = getattr(actor, "my_info", None) if identity == WORLD_BOSS_IDENTITY else None
    if me is not None:
        for value in (getattr(me, "username", ""), getattr(me, "first_name", "")):
            key = str(value or "").strip().lstrip("@").casefold()
            if key:
                expected_names.add(key)

    matching_ids: set[int] = set()
    for item in rows:
        source_label = str(item.get("sourceLabel") or "").strip()
        source_label_key = source_label.casefold()
        values = {
            str(item.get(key) or "").strip().lstrip("@").casefold()
            for key in ("username", "displayName", "daoName", "name", "avatarName", "identity")
        }
        label_matches = (
            identity == WORLD_BOSS_IDENTITY
            and source_label in {"主魂", "本体", "本人", "个人"}
        ) or source_label_key == identity.casefold()
        if label_matches or (expected_names & values):
            try:
                matching_ids.add(int(item.get("playerId")))
            except (TypeError, ValueError):
                continue
    if len(matching_ids) == 1:
        return next(iter(matching_ids))

    if identity == WORLD_BOSS_IDENTITY and not (getattr(actor, "avatars", []) or []) and len(rows) == 1:
        try:
            return int(rows[0].get("playerId"))
        except (TypeError, ValueError):
            return None
    return None


def select_main_identity_choice(actor: Any, choices: Any) -> int | None:
    """Backward-compatible main-soul selection helper."""
    return select_identity_choice(actor, choices, WORLD_BOSS_IDENTITY)


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
        self.target_chats: list[Any] = []
        self._new_handler: Any = None
        self._edit_handler: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight_messages: set[tuple[Any, int]] = set()
        self._inflight_fingerprints: set[str] = set()
        self._fight_lock = asyncio.Lock()
        self._json_clients: dict[str, _PersistentWorldBossJsonClient] = {}

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
            self.target_chats = await resolve_actor_target_chats(self.actor, self.log)

            async def new_handler(event: Any) -> None:
                await self.process_message(event.message, source="new")

            async def edit_handler(event: Any) -> None:
                await self.process_message(event.message, source="edited")

            self._new_handler = new_handler
            self._edit_handler = edit_handler
            self.client.add_event_handler(new_handler, events.NewMessage(chats=self.target_chats))
            self.client.add_event_handler(edit_handler, events.MessageEdited(chats=self.target_chats))
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
        self.log.info(
            "[%s] Qing Yuanzi world-boss monitor ready for chats %s",
            self.account,
            self.target_chats,
        )

        for target_chat in self.target_chats:
            try:
                recent = await self.client.get_messages(target_chat, limit=WORLD_BOSS_SCAN_LIMIT)
                # Telegram returns newest first. Recover at most one eligible room per chat.
                for message in list(recent or []):
                    if await self.process_message(message, source="startup"):
                        break
            except Exception as exc:
                self.log.warning(
                    "World Boss startup recovery scan failed for chat %s: %s",
                    target_chat,
                    _error_code(exc),
                )
        return True

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for client in self._json_clients.values():
            client.close()
        self._json_clients.clear()
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
        identities = world_boss_identities_for_account(self.account)
        if not identities:
            return False
        if self._event_status(entry.fingerprint) in COMPLETED_EVENT_STATUSES:
            return False
        message_key = (entry.chat_id, entry.message_id)
        if (
            message_key in self._inflight_messages
            or entry.fingerprint in self._inflight_fingerprints
        ):
            return False

        self._inflight_messages.add(message_key)
        self._inflight_fingerprints.add(entry.fingerprint)
        self._record(entry, "queued", source=source, identities=identities, error="")
        task = asyncio.create_task(
            self._run_entry(entry, identities),
            name=f"world_boss_{self.account}_{entry.message_id}",
        )
        self._tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            self._inflight_messages.discard(message_key)
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

    async def _run_entry(
        self,
        entry: WorldBossEntry,
        identities: list[str] | None = None,
    ) -> None:
        if identities is None:
            identities = world_boss_identities_for_account(self.account)
        identities = list(identities)
        if not identities:
            return
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
                self._record(
                    entry,
                    "running",
                    identities=identities,
                    started_at=_now_text(),
                    error="",
                )
                init_data = await request_webview_init_data(
                    self.client,
                    entry.bot_username,
                    entry.token,
                )
                results = await asyncio.gather(
                    *(
                        self._run_identity(entry, identity, init_data)
                        for identity in identities
                    )
                )
                successful = [
                    item for item in results if item.get("status") in {"completed", "already_completed"}
                ]
                statuses = {str(item.get("status") or "failed") for item in results}
                if len(successful) == len(results):
                    event_status = "completed"
                elif successful:
                    event_status = "partial"
                elif len(statuses) == 1:
                    event_status = next(iter(statuses))
                else:
                    event_status = "failed"
                errors = [str(item.get("error") or "") for item in results if item.get("error")]
                self._record(
                    entry,
                    event_status,
                    completed_at=_now_text(),
                    identities=identities,
                    identity_results=results,
                    error=", ".join(dict.fromkeys(errors)),
                )
            except asyncio.CancelledError:
                self._record(entry, "cancelled", error="cancelled")
                raise
            except MiniAppBeastError as exc:
                code = exc.code
                self._record(entry, "failed", identities=identities, error=code)
                self.log.error(
                    "Mini App [%s] 青元子世界 Boss初始化失败：%s",
                    ", ".join(identities),
                    code,
                )
            except Exception as exc:
                code = _error_code(exc)
                self._record(entry, "failed", identities=identities, error=code)
                self.log.error(
                    "Mini App [%s] 青元子世界 Boss初始化失败：%s",
                    ", ".join(identities),
                    code,
                    exc_info=True,
                )
            finally:
                lease.release()

    async def _run_identity(
        self,
        entry: WorldBossEntry,
        identity: str,
        init_data: str,
    ) -> dict[str, Any]:
        self.log.info("OUT [Mini App | %s]:\n青元子世界 Boss 自动参战", identity)
        try:
            outcome = await self._participate(entry, identity=identity, init_data=init_data)
            summary = self._outcome_summary(outcome)
            self.log.info("IN [Mini App | %s]:\n青元子世界 Boss -> %s", identity, summary)
            return {"identity": identity, "status": "completed", **outcome, "error": ""}
        except asyncio.CancelledError:
            raise
        except MiniAppBeastError as exc:
            code = exc.code
            failure_diagnostics = _error_diagnostics(exc)
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
            if code == "boss_action_limit":
                self.log.info(
                    "IN [Mini App | %s]:\n青元子世界 Boss -> 服务器确认本轮已完成",
                    identity,
                )
            elif code == "boss_not_enough_participants":
                self.log.info(
                    "IN [Mini App | %s]:\n青元子世界 Boss -> 入场人数不足，本轮未开战",
                    identity,
                )
            else:
                self.log.error("Mini App [%s] 青元子世界 Boss失败：%s", identity, code)
            result = {"identity": identity, "status": status, "error": code}
            if failure_diagnostics:
                result["diagnostics"] = {
                    "version": WORLD_BOSS_DIAGNOSTIC_VERSION,
                    "recorded_at": _now_text(),
                    "failure": failure_diagnostics,
                }
            return result
        except Exception as exc:
            code = _error_code(exc)
            self.log.error(
                "Mini App [%s] 青元子世界 Boss失败：%s",
                identity,
                code,
                exc_info=True,
            )
            return {"identity": identity, "status": "failed", "error": code}

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

    async def _identity_player_id(self, identity: str) -> int | None:
        transport = self._discover_transport()
        if transport is None:
            return None
        try:
            initializer = getattr(transport, "initialize", None)
            if callable(initializer):
                result = initializer()
                if inspect.isawaitable(result):
                    await result
            return int(transport.player_id(identity))
        except Exception as exc:
            self.log.warning(
                "World Boss fixed-entry identity lookup failed for %s (%s); using event choices",
                identity,
                _error_code(exc),
            )
            return None

    async def _main_player_id(self) -> int | None:
        """Backward-compatible helper used by older tests/callers."""
        return await self._identity_player_id(WORLD_BOSS_IDENTITY)

    async def _request(
        self,
        origin: str,
        path: str,
        payload: dict[str, Any],
        *,
        retries: int = 0,
        timeout: int | None = None,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_started_at = self.monotonic()
        if trace is not None:
            trace.update(
                {
                    "path": "/" + str(path or "").rstrip("/").rsplit("/", 1)[-1],
                    "attempts": [],
                }
            )
        for attempt in range(max(0, retries) + 1):
            attempt_started_at = self.monotonic()
            try:
                request_timeout = int(timeout or self.timeout)
                if self.post_json is None:
                    normalized_origin = miniapp_origin(origin)
                    client = self._json_clients.get(normalized_origin)
                    if client is None:
                        client = _PersistentWorldBossJsonClient(normalized_origin)
                        self._json_clients[normalized_origin] = client
                    result = await client.post(path, payload, request_timeout)
                else:
                    result = await _post_json(
                        origin,
                        path,
                        payload,
                        request_timeout,
                        post_json=self.post_json,
                    )
            except MiniAppBeastError as exc:
                if trace is not None:
                    attempt_trace = {
                        "attempt": attempt + 1,
                        "duration_ms": max(
                            0,
                            int(round((self.monotonic() - attempt_started_at) * 1000)),
                        ),
                        "ok": False,
                        "error": exc.code,
                        "http_status": int(exc.status or 0),
                    }
                    details = _error_diagnostics(exc)
                    if details:
                        attempt_trace["server_details"] = details
                    trace["attempts"].append(attempt_trace)
                    trace["total_duration_ms"] = max(
                        0,
                        int(round((self.monotonic() - trace_started_at) * 1000)),
                    )
                retryable = exc.status in RETRY_HTTP_STATUSES or exc.code in TRANSIENT_WORLD_BOSS_ERRORS
                if attempt >= retries or not retryable:
                    raise
                await self.sleep(min(2.5, 0.45 * (attempt + 1)))
            except Exception as exc:
                if trace is not None:
                    trace["attempts"].append(
                        {
                            "attempt": attempt + 1,
                            "duration_ms": max(
                                0,
                                int(round((self.monotonic() - attempt_started_at) * 1000)),
                            ),
                            "ok": False,
                            "error": _error_code(exc),
                            "http_status": 0,
                        }
                    )
                    trace["total_duration_ms"] = max(
                        0,
                        int(round((self.monotonic() - trace_started_at) * 1000)),
                    )
                raise
            else:
                if trace is not None:
                    trace["attempts"].append(
                        {
                            "attempt": attempt + 1,
                            "duration_ms": max(
                                0,
                                int(round((self.monotonic() - attempt_started_at) * 1000)),
                            ),
                            "ok": True,
                            "http_status": 200,
                        }
                    )
                    trace["total_duration_ms"] = max(
                        0,
                        int(round((self.monotonic() - trace_started_at) * 1000)),
                    )
                return result
        raise MiniAppBeastError("request_failed")

    async def _start_request(
        self,
        entry: WorldBossEntry,
        init_data: str,
        token: str,
        player_id: int | None,
        *,
        trace: dict[str, Any] | None = None,
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
            trace=trace,
        )

    async def _wait_for_challenge(
        self,
        entry: WorldBossEntry,
        init_data: str,
        player_id: int | None,
        identity: str = WORLD_BOSS_IDENTITY,
    ) -> tuple[str, dict[str, Any]]:
        token = entry.token
        deadline = self.monotonic() + WORLD_BOSS_ENTRY_WAIT_SECONDS
        was_waiting = False
        entry_requests: list[dict[str, Any]] = []
        entry_request_count = 0
        while self.monotonic() < deadline:
            request_trace: dict[str, Any] = {}
            entry_request_count += 1
            try:
                payload = await self._start_request(
                    entry,
                    init_data,
                    token,
                    player_id,
                    trace=request_trace,
                )
            except MiniAppBeastError as exc:
                observation = {
                    "sequence": entry_request_count,
                    "request": request_trace,
                    "error": exc.code,
                    "http_status": int(exc.status or 0),
                }
                if len(entry_requests) < 30:
                    entry_requests.append(observation)
                if exc.code == "boss_battle_not_started" or (
                    was_waiting and exc.code in AUTH_TOKEN_ERRORS
                ):
                    if exc.code in AUTH_TOKEN_ERRORS:
                        token = entry.token
                    await self.sleep(1.2)
                    continue
                details = _error_diagnostics(exc)
                details["entry_request_count"] = entry_request_count
                details["entry_requests"] = entry_requests
                exc.details = details
                raise

            boss_observation = payload.get("boss") if isinstance(payload.get("boss"), dict) else {}
            observation = {
                "sequence": entry_request_count,
                "request": request_trace,
                "identity_selection": bool(payload.get("needsIdentitySelection")),
                "challenge_ready": bool(
                    isinstance(payload.get("challenge"), dict)
                    and payload["challenge"].get("challengeId")
                ),
                "room_status": str(boss_observation.get("roomStatus") or ""),
                "join_remaining_seconds": float(
                    boss_observation.get("joinRemainingSeconds")
                    or (payload.get("room") or {}).get("joinRemainingSeconds")
                    or 0
                ),
            }
            if len(entry_requests) < 30:
                entry_requests.append(observation)
            if payload.get("needsIdentitySelection"):
                choices = payload.get("identityChoices") or []
                available_ids = {
                    int(item.get("playerId"))
                    for item in choices
                    if isinstance(item, dict) and str(item.get("playerId") or "").isdigit()
                }
                if player_id is None:
                    player_id = select_identity_choice(self.actor, choices, identity)
                if player_id is None or (available_ids and player_id not in available_ids):
                    raise MiniAppBeastError("world_boss_identity_missing")
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
                payload = dict(payload)
                payload["_client_diagnostics"] = {
                    "entry_request_count": entry_request_count,
                    "entry_requests": entry_requests,
                    "selected_player_id": player_id,
                }
                return token, payload

            was_waiting = True
            remain = float(
                boss.get("joinRemainingSeconds")
                or (payload.get("room") or {}).get("joinRemainingSeconds")
                or 0
            )
            await self.sleep(5.0 if remain > 5 else 1.2)
        error = MiniAppBeastError("boss_challenge_timeout")
        error.details = {
            "entry_request_count": entry_request_count,
            "entry_requests": entry_requests,
        }
        raise error

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

    def _hit_offset_ms(self, window: dict[str, Any]) -> int:
        """Stagger accounts early so network latency does not push hits past center."""
        slot = int(WORLD_BOSS_ACCOUNT_OFFSET_SLOTS.get(self.account, 0))
        perfect_ms = max(1, int(window.get("perfectMs") or 1))
        step = min(40, max(0, (perfect_ms - 10) // 4))
        return slot * step

    async def _hit_window(
        self,
        entry: WorldBossEntry,
        init_data: str,
        session_token: str,
        challenge_id: str,
        battle_start: float,
        window: dict[str, Any],
        window_index: int = 0,
    ) -> dict[str, Any]:
        offset_ms = self._hit_offset_ms(window)
        target_ms = max(0, int(window["centerMs"]) + offset_ms)
        target = battle_start + target_ms / 1000.0
        wait = target - self.monotonic()
        if wait > 0:
            await self.sleep(wait)
        elapsed_ms = max(0, int((self.monotonic() - battle_start) * 1000))
        signed_delta_ms = elapsed_ms - int(window["centerMs"])
        delta_ms = abs(signed_delta_ms)
        action = {
            "t": elapsed_ms,
            "holdMs": WORLD_BOSS_HOLD_MS,
            "stance": WORLD_BOSS_STANCE,
        }
        matched = delta_ms <= int(window["hitMs"])
        perfect = matched and delta_ms <= int(window["perfectMs"])
        diagnostic = {
            "sequence": max(1, int(window_index or 0)),
            "window_id": str(window.get("id") or "")[:120],
            "center_ms": int(window["centerMs"]),
            "hit_ms": int(window["hitMs"]),
            "perfect_ms": int(window["perfectMs"]),
            "account_offset_ms": offset_ms,
            "target_ms": target_ms,
            "actual_elapsed_ms": elapsed_ms,
            "signed_delta_ms": signed_delta_ms,
            "wake_lateness_ms": elapsed_ms - target_ms,
            "hold_ms": WORLD_BOSS_HOLD_MS,
            "local_matched": matched,
            "local_perfect": perfect,
        }
        if not matched:
            diagnostic.update(
                {
                    "server_status": "not_sent",
                    "error": "local_window_missed",
                    "http_status": 0,
                }
            )
            return {
                "action": action,
                "ok": False,
                "matched": False,
                "perfect": False,
                "accepted_perfect": False,
                "damage": 0.0,
                "error": "local_window_missed",
                "diagnostic": diagnostic,
            }
        request_trace: dict[str, Any] = {}
        try:
            payload = await self._request(
                entry.origin,
                "/api/miniapp/xianxia-world-boss/hit",
                {
                    "token": session_token,
                    "initData": init_data,
                    "challengeId": challenge_id,
                    "windowId": window["id"],
                    "elapsedMs": elapsed_ms,
                    "holdMs": WORLD_BOSS_HOLD_MS,
                },
                retries=2,
                timeout=min(self.timeout, 10),
                trace=request_trace,
            )
            hit = payload.get("hit") if isinstance(payload.get("hit"), dict) else {}
            accepted_perfect = bool(hit.get("perfect")) if "perfect" in hit else perfect
            diagnostic.update(
                {
                    "request_completed_elapsed_ms": max(
                        0,
                        int((self.monotonic() - battle_start) * 1000),
                    ),
                    "request": request_trace,
                    "server_status": "accepted",
                    "http_status": 200,
                    "accepted_perfect": accepted_perfect,
                    "server_hit": _diagnostic_value(hit),
                }
            )
            return {
                "action": action,
                "ok": True,
                "matched": matched,
                "perfect": perfect,
                "accepted_perfect": accepted_perfect,
                "damage": float(hit.get("damageYi") or 0),
                "diagnostic": diagnostic,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status = int(getattr(exc, "status", 0) or 0)
            diagnostic.update(
                {
                    "request_completed_elapsed_ms": max(
                        0,
                        int((self.monotonic() - battle_start) * 1000),
                    ),
                    "request": request_trace,
                    "server_status": "rejected",
                    "http_status": status,
                    "accepted_perfect": False,
                    "error": _error_code(exc),
                }
            )
            server_details = _error_diagnostics(exc)
            if server_details:
                diagnostic["server_details"] = server_details
            return {
                "action": action,
                "ok": False,
                "matched": matched,
                "perfect": perfect,
                "accepted_perfect": False,
                "damage": 0.0,
                "error": _error_code(exc),
                "diagnostic": diagnostic,
            }

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

        begin_trace: dict[str, Any] = {}
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
            trace=begin_trace,
        )
        response_at = self.monotonic()
        round_trip = max(0.0, response_at - started_request_at)
        server_starts_in_ms = max(0.0, float(sync.get("startsInMs") or 0))
        starts_in = max(0.0, server_starts_in_ms / 1000.0 - round_trip / 2.0)
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
                    index,
                )
            )
            for index, window in enumerate(windows, start=1)
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
        local_matched_hits = sum(1 for item in hit_results if item.get("matched"))
        local_perfect_hits = sum(1 for item in hit_results if item.get("perfect"))
        accepted_perfect_hits = sum(
            1 for item in hit_results if item.get("ok") and item.get("accepted_perfect")
        )
        damage_yi_hits = [
            max(0.0, float(item.get("damage") or 0))
            for item in hit_results
        ]
        damage_yi_total = sum(damage_yi_hits)
        damaging_hits = [value for value in damage_yi_hits if value > 0]
        damage_yi_average = (
            damage_yi_total / len(damaging_hits)
            if damaging_hits
            else 0.0
        )
        hit_error_counts: dict[str, int] = {}
        for item in hit_results:
            if item.get("ok"):
                continue
            code = str(item.get("error") or "unknown")
            hit_error_counts[code] = hit_error_counts.get(code, 0) + 1
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
                # Match the browser proof: locally matched actions stay in clientStats
                # even when a realtime /hit report is rejected by the server.
                "dodges": local_matched_hits,
                "grazes": 0,
                "damage": 0,
                "hits": local_matched_hits,
                "perfects": local_perfect_hits,
                "combo": local_matched_hits,
                "bestCombo": local_matched_hits,
            },
            "realtimeDamageApplied": realtime_damage,
        }
        finish_trace: dict[str, Any] = {}
        finished = await self._request(
            entry.origin,
            "/api/miniapp/xianxia-world-boss/finish",
            {
                "token": session_token,
                "initData": init_data,
                "bossProof": proof,
            },
            retries=1,
            trace=finish_trace,
        )
        result = finished.get("result") if isinstance(finished.get("result"), dict) else {}
        player = _diagnostic_value(player)
        boss = payload.get("boss") if isinstance(payload.get("boss"), dict) else {}
        challenge_profile = {
            key: value
            for key, value in challenge.items()
            if key not in {"challengeId", "windows"}
        }
        challenge_diagnostics = _diagnostic_value(challenge_profile)
        diagnostics = {
            "version": WORLD_BOSS_DIAGNOSTIC_VERSION,
            "recorded_at": _now_text(),
            "strategy": {
                "stance": WORLD_BOSS_STANCE,
                "hold_ms": WORLD_BOSS_HOLD_MS,
                "account_offset_slot": int(
                    WORLD_BOSS_ACCOUNT_OFFSET_SLOTS.get(self.account, 0)
                ),
            },
            "player": player if isinstance(player, dict) else {},
            "boss": _diagnostic_value(boss),
            "challenge": {
                **(
                    challenge_diagnostics
                    if isinstance(challenge_diagnostics, dict)
                    else {}
                ),
                "window_count": len(windows),
            },
            "entry": _diagnostic_value(payload.get("_client_diagnostics") or {}),
            "clock_sync": {
                "request": begin_trace,
                "round_trip_ms": int(round(round_trip * 1000)),
                "server_starts_in_ms": int(round(server_starts_in_ms)),
                "applied_wait_ms": int(round(starts_in * 1000)),
            },
            "hits": [item.get("diagnostic") or {} for item in hit_results],
            "finish": {
                "request": finish_trace,
                "server_result": _diagnostic_value(result),
            },
        }
        return {
            "grade": str(result.get("grade") or ""),
            "score": int(result.get("score") or 0),
            "player_hp": int(result.get("player_hp") if result.get("player_hp") is not None else player_hp),
            "hit_count": successful_hits,
            "perfect_count": accepted_perfect_hits,
            "local_matched_count": local_matched_hits,
            "local_perfect_count": local_perfect_hits,
            "failed_hit_count": failed_hits,
            "hit_error_counts": hit_error_counts,
            "window_count": len(windows),
            "damage_yi_total": damage_yi_total,
            "damage_yi_average": damage_yi_average,
            "damage_yi_hit_count": len(damaging_hits),
            "damage_yi_hits": damage_yi_hits,
            "diagnostics": diagnostics,
        }

    async def _participate(
        self,
        entry: WorldBossEntry,
        identity: str = WORLD_BOSS_IDENTITY,
        init_data: str = "",
    ) -> dict[str, Any]:
        if not init_data:
            init_data = await request_webview_init_data(
                self.client,
                entry.bot_username,
                entry.token,
            )
        player_id = await self._identity_player_id(identity)
        session_token, payload = await self._wait_for_challenge(
            entry,
            init_data,
            player_id,
            identity=identity,
        )
        client_diagnostics = payload.get("_client_diagnostics")
        if isinstance(client_diagnostics, dict):
            client_diagnostics["requested_identity"] = identity
            client_diagnostics["fixed_player_id_available"] = player_id is not None
        return await self._fight(entry, init_data, session_token, payload)

    @staticmethod
    def _format_damage_yi(value: Any) -> str:
        try:
            amount = max(0.0, float(value or 0))
        except (TypeError, ValueError):
            amount = 0.0
        if amount >= 100_000_000:
            return f"{amount / 100_000_000:.2f}亿亿"
        if amount >= 10_000:
            return f"{amount / 10_000:.2f}万亿"
        if amount.is_integer():
            return f"{int(amount):,}亿"
        return f"{amount:,.2f}亿"

    @classmethod
    def _damage_summary(cls, outcome: dict[str, Any]) -> str:
        values = outcome.get("damage_yi_hits")
        if not isinstance(values, list) or not values:
            return ""
        total = cls._format_damage_yi(outcome.get("damage_yi_total"))
        average = cls._format_damage_yi(outcome.get("damage_yi_average"))
        damaging = int(outcome.get("damage_yi_hit_count") or 0)
        per_hit = ", ".join(
            f"{index}={cls._format_damage_yi(value)}"
            for index, value in enumerate(values, start=1)
        )
        return (
            f"伤害合计 {total}，有效 {damaging}/{len(values)}，均击 {average}，"
            f"逐击 [{per_hit}]"
        )

    @staticmethod
    def _timing_diagnostic_summary(outcome: dict[str, Any]) -> str:
        diagnostics = outcome.get("diagnostics")
        if not isinstance(diagnostics, dict):
            return ""
        parts: list[str] = []
        player = diagnostics.get("player")
        if isinstance(player, dict) and player:
            preferred_keys = (
                "label",
                "root",
                "maxHp",
                "attackBonus",
                "attackPower",
                "damageBonus",
                "power",
                "cultivationLevel",
                "realm",
                "sect",
            )
            profile = {
                key: player[key]
                for key in preferred_keys
                if key in player and not isinstance(player[key], (dict, list))
            }
            if profile:
                parts.append(
                    "战场参数 "
                    + json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
                )
        clock_sync = diagnostics.get("clock_sync")
        if isinstance(clock_sync, dict):
            parts.append(
                "校时 RTT {rtt}ms/服务端等待 {server}ms/实际等待 {applied}ms".format(
                    rtt=int(clock_sync.get("round_trip_ms") or 0),
                    server=int(clock_sync.get("server_starts_in_ms") or 0),
                    applied=int(clock_sync.get("applied_wait_ms") or 0),
                )
            )
        hits = diagnostics.get("hits")
        if isinstance(hits, list):
            durations = sorted(
                int((item.get("request") or {}).get("total_duration_ms") or 0)
                for item in hits
                if isinstance(item, dict) and isinstance(item.get("request"), dict)
            )
            if durations:
                median = durations[(len(durations) - 1) // 2]
                p95 = durations[max(0, (len(durations) * 95 + 99) // 100 - 1)]
                parts.append(
                    f"逐击 HTTP p50/p95/max {median}/{p95}/{durations[-1]}ms"
                )
            failures = []
            for item in hits:
                if not isinstance(item, dict) or not item.get("error"):
                    continue
                request = item.get("request") if isinstance(item.get("request"), dict) else {}
                failure = (
                    f"#{int(item.get('sequence') or 0)} {item.get('error')} "
                    f"计划{int(item.get('account_offset_ms') or 0):+d}ms/"
                    f"实际{int(item.get('signed_delta_ms') or 0):+d}ms/"
                    f"HTTP {int(request.get('total_duration_ms') or 0)}ms/"
                    f"状态{int(item.get('http_status') or 0)}"
                )
                details = item.get("server_details")
                if isinstance(details, dict) and details:
                    failure += "/服务端" + json.dumps(
                        details,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )[:240]
                failures.append(failure)
            if failures:
                parts.append("失败定位 [" + "；".join(failures) + "]")
        return "；".join(parts)

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
        local_perfects = int(outcome.get("local_perfect_count") or perfects)
        if local_perfects != perfects:
            summary += f"（本地判定 {local_perfects}，服务端确认 {perfects}）"
        if failed:
            summary += f"；{failed} 次实时回传失败"
            error_counts = outcome.get("hit_error_counts")
            if isinstance(error_counts, dict) and error_counts:
                details = ", ".join(
                    f"{code}x{int(count)}"
                    for code, count in sorted(error_counts.items())
                    if int(count or 0) > 0
                )
                if details:
                    summary += f"（{details}）"
        damage_summary = WorldBossMonitor._damage_summary(outcome)
        if damage_summary:
            summary += f"；{damage_summary}"
        diagnostic_summary = WorldBossMonitor._timing_diagnostic_summary(outcome)
        if diagnostic_summary:
            summary += f"；诊断：{diagnostic_summary}"
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
