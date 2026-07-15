#!/usr/bin/env python3
"""Telegram group visibility detection and xiaohao tmux process control."""

import asyncio
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
XIAOHAO_SCRIPT = "cultivator_xiaohao.py"
XIAOHAO_TMUX_TARGET = "xiuxian:2"


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


class TmuxXiaohaoProcessManager:
    """Start or stop the xiaohao process in its fixed tmux window."""

    def __init__(
        self,
        deploy_dir,
        logger,
        python_executable,
        script=XIAOHAO_SCRIPT,
        tmux_target=XIAOHAO_TMUX_TARGET,
    ):
        self.deploy_dir = os.path.abspath(deploy_dir)
        self.logger = logger
        self.python_executable = python_executable
        self.script = script
        self.tmux_target = tmux_target
        self._lock = asyncio.Lock()

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

    async def _start(self):
        run_command = (
            f"cd {shlex.quote(self.deploy_dir)} && "
            f"exec {shlex.quote(self.python_executable)} {shlex.quote(self.script)}"
        )
        tmux_command = f"bash -lc {shlex.quote(run_command)}"
        await asyncio.to_thread(
            self._run_tmux_sync,
            ["respawn-window", "-k", "-t", self.tmux_target, tmux_command],
        )
        if not await self._wait_for_running(True):
            raise RuntimeError("xiaohao did not start within 12 seconds")

    async def _stop(self):
        await asyncio.to_thread(
            self._run_tmux_sync,
            ["send-keys", "-t", self.tmux_target, "C-c"],
        )
        if await self._wait_for_running(False, timeout=6):
            return
        for sig in (signal.SIGINT, signal.SIGTERM):
            for pid in await self.process_pids():
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    continue
            if await self._wait_for_running(False, timeout=6):
                return
        raise RuntimeError("xiaohao did not stop after SIGINT/SIGTERM")

    async def ensure_running(self, desired_running):
        """Return started/stopped/already_running/already_stopped."""
        async with self._lock:
            running = bool(await self.process_pids())
            if desired_running:
                if running:
                    return "already_running"
                await self._start()
                return "started"
            if not running:
                return "already_stopped"
            await self._stop()
            return "stopped"


class TelegramGroupXiaohaoController:
    """Poll group visibility and reconcile the xiaohao process."""

    def __init__(
        self,
        client,
        target_chat_id,
        process_manager,
        logger,
        state=None,
        save_state=None,
        poll_seconds=60,
    ):
        self.client = client
        self.target_chat_id = target_chat_id
        self.process_manager = process_manager
        self.logger = logger
        self.state = state if isinstance(state, dict) else {}
        self.save_state = save_state
        self.poll_seconds = max(15, int(poll_seconds or 60))
        self._check_lock = asyncio.Lock()
        self._last_error = ""

    def update_targets_group(self, update):
        return telegram_update_targets_chat(update, self.target_chat_id)

    def _record_result(self, visibility, action, source):
        previous = str(self.state.get("target_group_visibility") or "")
        changed = previous != visibility
        if changed:
            self.state["target_group_visibility"] = visibility
            self.state["target_group_visibility_changed_at"] = datetime.now().strftime(TIME_FORMAT)
        if action in {"started", "stopped"}:
            self.state["xiaohao_visibility_last_action"] = action
            self.state["xiaohao_visibility_last_action_at"] = datetime.now().strftime(TIME_FORMAT)
        if changed or action in {"started", "stopped"}:
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
                        "Xiaohao visibility control: group=%s, action=%s (%s).",
                        visibility,
                        action,
                        source,
                    )
                if self._last_error:
                    self.logger.info("Xiaohao visibility control recovered after: %s", self._last_error)
                    self._last_error = ""
                return visibility
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if message != self._last_error:
                    self.logger.error(
                        "Xiaohao visibility control check failed (%s): %s",
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
