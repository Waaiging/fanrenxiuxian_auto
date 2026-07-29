#!/usr/bin/env python3
"""Telegram group visibility and restricted-account process control."""

import asyncio
import json
import os
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
XIAOHAO_SCRIPT = "cultivator_xiaohao.py"
XIAOHAO_TMUX_TARGET = "xiuxian:2"
XIAOHAO_WRITE_RETRY_SECONDS = 15 * 60
TMUX_MISSING_TARGET_MARKERS = (
    "can't find window",
    "can't find session",
    "can't find pane",
    "no server running",
)
_TMUX_STRUCTURE_LOCK = threading.RLock()


def tmux_target_parts(value):
    """Split a configured ``session:window`` target into stable components."""
    text = str(value or "").strip()
    if ":" not in text:
        raise ValueError(f"invalid tmux target: {text or '<empty>'}")
    session, window = text.rsplit(":", 1)
    window = window.split(".", 1)[0]
    if not session or not window:
        raise ValueError(f"invalid tmux target: {text}")
    return session, window


def is_missing_tmux_target_error(value):
    text = str(value or "").strip().casefold()
    return any(marker in text for marker in TMUX_MISSING_TARGET_MARKERS)


def telegram_group_visibility(entity):
    """Return ``public`` or ``private`` for a Telegram group entity."""
    if entity is None:
        return "unknown"
    if getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
        return "unsupported"
    if str(getattr(entity, "username", "") or "").strip():
        return "public"
    for item in getattr(entity, "usernames", None) or []:
        if getattr(item, "active", False) and str(getattr(item, "username", "") or "").strip():
            return "public"
    return "private"


def normalize_telegram_chat_id(value):
    """Normalize Telethon's positive ID and Bot API's ``-100`` form."""
    text = str(value or "").strip()
    if text.startswith("-100"):
        text = text[4:]
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def telegram_chat_ids_match(left, right):
    left_id = normalize_telegram_chat_id(left)
    right_id = normalize_telegram_chat_id(right)
    return left_id is not None and left_id == right_id


def telegram_update_targets_chat(update, target_chat_id):
    """Recognize channel metadata updates for the configured group."""
    if type(update).__name__ not in {"UpdateChannel", "UpdateChannelTooLong"}:
        return False
    return telegram_chat_ids_match(getattr(update, "channel_id", None), target_chat_id)


def telegram_write_permission_status(permissions):
    """Return (allowed|blocked|unknown, reason) for Telethon permissions."""
    if permissions is None:
        return "unknown", "permissions unavailable"
    if bool(getattr(permissions, "is_banned", False)):
        return "blocked", "participant is banned"
    if getattr(permissions, "send_messages", None) is False:
        return "blocked", "send_messages is false"
    if getattr(permissions, "view_messages", None) is False:
        return "blocked", "view_messages is false"
    return "allowed", "group permissions allow messages"


def xiaohao_write_restriction_retry_wait(state, now=None, default_retry_seconds=XIAOHAO_WRITE_RETRY_SECONDS):
    """Return seconds until a stopped xiaohao may probe Telegram writes again."""
    if not isinstance(state, dict):
        return 0
    stop = state.get("telegram_send_protection_stop")
    if not isinstance(stop, dict) or stop.get("reason") != "write_restricted":
        return 0
    now = now or datetime.now()
    retry_at = str(stop.get("retry_at") or "").strip()
    if not retry_at:
        stopped_at = str(stop.get("at") or "").strip()
        try:
            retry_dt = datetime.strptime(stopped_at, TIME_FORMAT) + timedelta(seconds=default_retry_seconds)
        except Exception:
            return 0
    else:
        try:
            retry_dt = datetime.strptime(retry_at, TIME_FORMAT)
        except Exception:
            return 0
    return max(0, int((retry_dt - now).total_seconds()))


async def run_telegram_write_permission_monitor(actor, logger, protection_handler=None):
    """Poll one restricted account's target-group write permission."""
    if protection_handler is None:
        from command_feedback import _handle_telegram_send_protection

        protection_handler = _handle_telegram_send_protection

    last_error = ""
    last_persist = 0.0
    while getattr(actor, "is_running", True):
        try:
            permissions = await actor.client.get_permissions(actor.target_chat_id, "me")
            status, reason = telegram_write_permission_status(permissions)
            now_wall = datetime.now().strftime(TIME_FORMAT)
            monitor = actor.state.get("telegram_write_permission_monitor")
            if not isinstance(monitor, dict):
                monitor = {}
            previous = str(monitor.get("status") or "unknown")
            monitor.update({
                "status": status,
                "reason": reason,
                "checked_at": now_wall,
            })
            if status != previous:
                monitor["changed_at"] = now_wall
                logger.warning(
                    "Telegram write permission changed: %s -> %s (%s).",
                    previous,
                    status,
                    reason,
                )
            actor.state["telegram_write_permission_monitor"] = monitor
            if status != previous or time.monotonic() - last_persist >= 300:
                actor.save_state()
                last_persist = time.monotonic()
            if status == "blocked":
                await protection_handler(
                    actor,
                    "(permission monitor)",
                    RuntimeError(f"CHAT_WRITE_FORBIDDEN: {reason}"),
                    logger=logger,
                    identity=getattr(actor, "current_identity", "主魂"),
                )
                return
            if last_error:
                logger.info(
                    "Telegram write permission monitor recovered after: %s",
                    last_error,
                )
                last_error = ""
        except Exception as exc:
            handled = await protection_handler(
                actor,
                "(permission monitor)",
                exc,
                logger=logger,
                identity=getattr(actor, "current_identity", "主魂"),
            )
            if handled:
                return
            message = f"{type(exc).__name__}: {exc}"
            if message != last_error:
                logger.error(
                    "Telegram write permission monitor check failed: %s",
                    message,
                )
                last_error = message
            monitor = actor.state.get("telegram_write_permission_monitor")
            if not isinstance(monitor, dict):
                monitor = {}
            monitor.update({
                "status": "unknown",
                "reason": message,
                "checked_at": datetime.now().strftime(TIME_FORMAT),
            })
            actor.state["telegram_write_permission_monitor"] = monitor
            actor.save_state()
        poll_seconds = max(
            30,
            int(getattr(actor, "telegram_write_permission_poll_seconds", 60) or 60),
        )
        await asyncio.sleep(poll_seconds)


class TmuxXiaohaoProcessManager:
    """Start or stop one restricted account in its fixed tmux window."""

    def __init__(
        self,
        deploy_dir,
        logger,
        python_executable,
        script=XIAOHAO_SCRIPT,
        tmux_target=XIAOHAO_TMUX_TARGET,
        state_file=None,
        account_label="Xiaohao",
        fallback_script=None,
        fallback_account=None,
    ):
        self.deploy_dir = os.path.abspath(deploy_dir)
        self.logger = logger
        self.python_executable = python_executable
        self.script = script
        self.tmux_target = tmux_target
        self.state_file = state_file
        self.account_label = str(account_label or script)
        self.fallback_script = str(fallback_script or "").strip()
        self.fallback_account = str(fallback_account or "").strip()
        self.last_defer_seconds = 0
        self._lock = asyncio.Lock()

    def _write_restriction_retry_wait_sync(self):
        if not self.state_file or not os.path.exists(self.state_file):
            return 0
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            return xiaohao_write_restriction_retry_wait(state)
        except Exception:
            return 0

    def _process_pids_sync(self):
        result = subprocess.run(
            ["pgrep", "-af", self.script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(str(result.stderr or result.stdout or "pgrep failed").strip())
        pids = []
        for line in str(result.stdout or "").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid_text, command = parts
            if self.script not in command or "python" not in command or "tmux " in command:
                continue
            try:
                pids.append(int(pid_text))
            except ValueError:
                continue
        return pids

    async def process_pids(self):
        return await asyncio.to_thread(self._process_pids_sync)

    def _fallback_process_pids_sync(self):
        if not self.fallback_script or not self.fallback_account:
            return []
        result = subprocess.run(
            ["pgrep", "-af", self.fallback_script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(str(result.stderr or result.stdout or "pgrep failed").strip())
        account_arg = f"--account {self.fallback_account}"
        pids = []
        for line in str(result.stdout or "").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            pid_text, command = parts
            if self.fallback_script not in command or account_arg not in command or "python" not in command:
                continue
            try:
                pids.append(int(pid_text))
            except ValueError:
                continue
        return pids

    async def fallback_process_pids(self):
        return await asyncio.to_thread(self._fallback_process_pids_sync)

    async def _wait_for_running(self, expected, timeout=12):
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            if bool(await self.process_pids()) == bool(expected):
                return True
            await asyncio.sleep(0.5)
        return bool(await self.process_pids()) == bool(expected)

    def _run_tmux_sync(self, args):
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(str(result.stderr or result.stdout or "tmux command failed").strip())
        return result

    def _tmux_session_exists_sync(self, session):
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1 or is_missing_tmux_target_error(result.stderr or result.stdout):
            return False
        raise RuntimeError(str(result.stderr or result.stdout or "tmux has-session failed").strip())

    def _tmux_window_exists_sync(self, session, window):
        result = subprocess.run(
            ["tmux", "list-windows", "-t", session, "-F", "#{window_index}\t#{window_name}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            if is_missing_tmux_target_error(result.stderr or result.stdout):
                return False
            raise RuntimeError(str(result.stderr or result.stdout or "tmux list-windows failed").strip())
        for line in str(result.stdout or "").splitlines():
            index, _, name = line.partition("\t")
            if (window.isdigit() and index.strip() == window) or name.strip() == window:
                return True
        return False

    def _ensure_tmux_target_sync(self):
        """Create a missing tmux session/window without disturbing existing panes."""
        session, window = tmux_target_parts(self.tmux_target)
        created_session = False
        created_window = False
        with _TMUX_STRUCTURE_LOCK:
            if not self._tmux_session_exists_sync(session):
                initial_name = (
                    self.account_label
                    if window == "0"
                    else window if not window.isdigit() else "Bootstrap"
                )
                try:
                    self._run_tmux_sync([
                        "new-session",
                        "-d",
                        "-s",
                        session,
                        "-n",
                        initial_name,
                        "exec sleep infinity",
                    ])
                    created_session = True
                except RuntimeError as exc:
                    if "duplicate session" not in str(exc).casefold():
                        raise
            self._run_tmux_sync(["set-window-option", "-g", "remain-on-exit", "on"])
            if not self._tmux_window_exists_sync(session, window):
                target = f"{session}:{window}" if window.isdigit() else f"{session}:"
                try:
                    self._run_tmux_sync([
                        "new-window",
                        "-d",
                        "-t",
                        target,
                        "-n",
                        self.account_label,
                        "exec sleep infinity",
                    ])
                    created_window = True
                except RuntimeError:
                    if not self._tmux_window_exists_sync(session, window):
                        raise
            self._run_tmux_sync([
                "set-window-option",
                "-t",
                self.tmux_target,
                "remain-on-exit",
                "on",
            ])
        if created_session or created_window:
            self.logger.warning(
                "%s tmux target %s was missing; recreated automatically.",
                self.account_label,
                self.tmux_target,
            )
        return created_session or created_window

    def _respawn_window_sync(self, tmux_command):
        """Respawn a target, repairing a disappearance race and retrying once."""
        with _TMUX_STRUCTURE_LOCK:
            for attempt in range(2):
                self._ensure_tmux_target_sync()
                try:
                    self._run_tmux_sync(
                        ["respawn-window", "-k", "-t", self.tmux_target, tmux_command]
                    )
                    return
                except RuntimeError as exc:
                    if attempt == 0 and is_missing_tmux_target_error(exc):
                        continue
                    raise

    def _send_interrupt_sync(self):
        try:
            self._run_tmux_sync(["send-keys", "-t", self.tmux_target, "C-c"])
            return True
        except RuntimeError as exc:
            if not is_missing_tmux_target_error(exc):
                raise
            self.logger.warning(
                "%s tmux target %s disappeared before stop; falling back to PID signals.",
                self.account_label,
                self.tmux_target,
            )
            return False

    async def _start(self):
        run_command = (
            f"cd {shlex.quote(self.deploy_dir)} && "
            f"exec {shlex.quote(self.python_executable)} {shlex.quote(self.script)}"
        )
        tmux_command = f"bash -lc {shlex.quote(run_command)}"
        await asyncio.to_thread(
            self._respawn_window_sync,
            tmux_command,
        )
        if not await self._wait_for_running(True):
            raise RuntimeError(f"{self.account_label} did not start within 12 seconds")

    async def _start_fallback(self):
        if not self.fallback_script or not self.fallback_account:
            return False
        run_command = (
            f"cd {shlex.quote(self.deploy_dir)} && "
            f"exec {shlex.quote(self.python_executable)} {shlex.quote(self.fallback_script)} "
            f"--account {shlex.quote(self.fallback_account)}"
        )
        tmux_command = f"bash -lc {shlex.quote(run_command)}"
        await asyncio.to_thread(
            self._respawn_window_sync,
            tmux_command,
        )
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if await self.fallback_process_pids():
                return True
            await asyncio.sleep(0.5)
        raise RuntimeError(f"{self.account_label} red-packet standby did not start within 12 seconds")

    async def _ensure_fallback(self):
        if not self.fallback_script or not self.fallback_account:
            return False
        if await self.fallback_process_pids():
            return False
        return await self._start_fallback()

    async def _stop(self):
        sent_interrupt = await asyncio.to_thread(self._send_interrupt_sync)
        if sent_interrupt and await self._wait_for_running(False, timeout=6):
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            for pid in await self.process_pids():
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    continue
            if await self._wait_for_running(False, timeout=6):
                return
        raise RuntimeError(f"{self.account_label} did not stop after SIGINT/SIGTERM")

    async def ensure_running(self, desired_running):
        """Return started/stopped/already_running/already_stopped."""
        async with self._lock:
            await asyncio.to_thread(self._ensure_tmux_target_sync)
            running = bool(await self.process_pids())
            if desired_running:
                if running:
                    return "already_running"
                self.last_defer_seconds = await asyncio.to_thread(self._write_restriction_retry_wait_sync)
                if self.last_defer_seconds > 0:
                    await self._ensure_fallback()
                    return "deferred_write_restricted"
                await self._start()
                return "started"
            if not running:
                await self._ensure_fallback()
                return "already_stopped"
            await self._stop()
            await self._ensure_fallback()
            return "stopped"


class TelegramGroupXiaohaoController:
    """Poll group visibility and reconcile one restricted account process."""

    def __init__(
        self,
        client,
        target_chat_id,
        process_manager,
        logger,
        state=None,
        save_state=None,
        poll_seconds=60,
        account_label="Xiaohao",
        state_prefix="xiaohao",
    ):
        self.client = client
        self.target_chat_id = target_chat_id
        self.process_manager = process_manager
        self.logger = logger
        self.state = state if isinstance(state, dict) else {}
        self.save_state = save_state
        self.poll_seconds = max(15, int(poll_seconds or 60))
        self.account_label = str(account_label or "restricted account")
        self.state_prefix = str(state_prefix or "restricted_account")
        self._check_lock = asyncio.Lock()
        self._last_error = ""
        self._last_defer_log = 0.0

    def update_targets_group(self, update):
        return telegram_update_targets_chat(update, self.target_chat_id)

    def _record_result(self, visibility, action, source):
        previous = str(self.state.get("target_group_visibility") or "")
        changed = previous != visibility
        if changed:
            self.state["target_group_visibility"] = visibility
            self.state["target_group_visibility_changed_at"] = datetime.now().strftime(TIME_FORMAT)
        action_key = f"{self.state_prefix}_visibility_last_action"
        action_at_key = f"{self.state_prefix}_visibility_last_action_at"
        retry_wait_key = f"{self.state_prefix}_write_retry_wait_seconds"
        if action in {"started", "stopped", "deferred_write_restricted"}:
            self.state[action_key] = action
            self.state[action_at_key] = datetime.now().strftime(TIME_FORMAT)
        if action == "deferred_write_restricted":
            self.state[retry_wait_key] = int(
                getattr(self.process_manager, "last_defer_seconds", 0) or 0
            )
        if changed or action in {"started", "stopped", "deferred_write_restricted"}:
            if callable(self.save_state):
                self.save_state()
        if changed:
            self.logger.warning(
                "Target Telegram group visibility changed: %s -> %s (%s).",
                previous or "unknown",
                visibility,
                source,
            )

    async def check_once(self, source="poll"):
        async with self._check_lock:
            try:
                entity = await self.client.get_entity(self.target_chat_id)
                visibility = telegram_group_visibility(entity)
                if visibility not in {"public", "private"}:
                    raise RuntimeError(f"target entity is not a Telegram group: {visibility}")
                desired_running = visibility == "private"
                action = await self.process_manager.ensure_running(desired_running)
                self._record_result(visibility, action, source)
                if action in {"started", "stopped"}:
                    self.logger.warning(
                        "%s visibility control: group=%s, action=%s (%s).",
                        self.account_label,
                        visibility,
                        action,
                        source,
                    )
                elif action == "deferred_write_restricted":
                    now_monotonic = time.monotonic()
                    if now_monotonic - self._last_defer_log >= 300 or source != "poll":
                        self.logger.warning(
                            "%s restart deferred after Telegram write restriction; retry in %ss (%s).",
                            self.account_label,
                            int(getattr(self.process_manager, "last_defer_seconds", 0) or 0),
                            source,
                        )
                        self._last_defer_log = now_monotonic
                if self._last_error:
                    self.logger.info(
                        "%s visibility control recovered after: %s",
                        self.account_label,
                        self._last_error,
                    )
                    self._last_error = ""
                return visibility
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if message != self._last_error:
                    self.logger.error(
                        "%s visibility control check failed (%s): %s",
                        self.account_label,
                        source,
                        message,
                        exc_info=True,
                    )
                    self._last_error = message
                return "unknown"

    async def run(self, should_continue):
        while should_continue():
            await self.check_once("poll")
            await asyncio.sleep(self.poll_seconds)
