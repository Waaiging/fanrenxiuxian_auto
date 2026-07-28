#!/usr/bin/env python3
"""Hybrid command routing: Mini App first for supported commands, group fallback.

Used by non-restricted accounts (main/sub). Commands accepted by
``miniapp_command_allowed`` are executed through the Mini App dwelling
transport for every identity (no ``.切换`` round-trip needed — the transport
addresses identities by playerId). Anything else, and any Mini App failure,
falls back to the original Telegram group send path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import (
    MiniAppDwellingTransport,
    apply_dwelling_snapshot,
    miniapp_command_allowed,
    normalize_miniapp_command,
)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_AUTH_REFRESH_SECONDS = 6 * 3600


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
        self.transport = MiniAppDwellingTransport(
            actor.client,
            entry_url,
            bot_username=str(settings.get("bot_username") or "fanrenxiuxian_bot"),
            timeout=int(settings.get("timeout_seconds") or 20),
            logger=self.log,
        )
        self._orig_send = None
        self._orig_send_identity = None
        self._last_auth_refresh = datetime.min

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
        if not self.enabled:
            self.log.info("Mini App command routing disabled for %s", self.account)
            return False
        try:
            await self.transport.initialize()
            self._last_auth_refresh = datetime.now()
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self.enabled = False
            self._record(miniapp_route_active=False, miniapp_route_last_error=code)
            self.log.error(
                "Mini App command routing setup failed (%s); all commands stay on the group path",
                code,
                exc_info=True,
            )
            return False
        self._orig_send = self.actor.send_and_wait_feedback
        self._orig_send_identity = getattr(self.actor, "send_and_wait_feedback_identity", None)
        self.actor.send_and_wait_feedback = self._send_main
        if self._orig_send_identity is not None:
            self.actor.send_and_wait_feedback_identity = self._send_identity
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
        self.log.warning(
            "[%s] Mini App command routing active for identities %s; unsupported commands stay in the group",
            self.account,
            known,
        )
        return True

    def _identity_routable(self, identity: str) -> bool:
        key = str(identity or "主魂").strip() or "主魂"
        ids = self.transport.identity_player_ids
        return key in ids or key.casefold() in ids

    def _should_route(self, identity: str, command: str, kwargs: dict[str, Any]) -> bool:
        if not self.enabled or not miniapp_command_allowed(command):
            return False
        # Replies target a concrete group message; those flows must stay there.
        if kwargs.get("reply_to") is not None:
            return False
        return self._identity_routable(identity)

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
        if not self._should_route(identity, command, kwargs):
            return await fallback(message, *args, **kwargs)
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
            self.log.info("Mini App OUT [%s]: %s", identity, command)
            self.log.info("Mini App IN [%s]: %s", identity, response.text[:500])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self._record(
                miniapp_route_last_error=code,
                miniapp_route_last_error_at=_now_text(),
            )
            self.log.warning(
                "Mini App command failed [%s] %s (%s); falling back to the group",
                identity,
                command,
                code,
            )
            return await fallback(message, *args, **kwargs)
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
