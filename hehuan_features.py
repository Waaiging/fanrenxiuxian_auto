"""Hehuan dual cultivation with an identity-specific partner and settlement clock."""
import asyncio
from datetime import datetime, timedelta
import logging
import re

from command_feedback import command_response_text
from log_utils import actor_message_target
from log_utils import actor_account_key, dashboard_command_control_value

log = logging.getLogger("Hehuan")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DUAL_CULTIVATION_COMMAND = ".双修 温养"
DUAL_CULTIVATION_INTERVAL_SECONDS = 3600
DUAL_CULTIVATION_BUFFER_SECONDS = 2
DUAL_CULTIVATION_RETRY_SECONDS = 60


def now_str():
    return datetime.now().strftime(TIME_FORMAT)


def add_seconds_str(value, seconds):
    return (datetime.strptime(value, TIME_FORMAT) + timedelta(seconds=seconds)).strftime(TIME_FORMAT)


def seconds_until(value):
    return max(0, (datetime.strptime(value, TIME_FORMAT) - datetime.now()).total_seconds())


def dual_cultivation_default_target(account, identity, state=None):
    state = state or {}
    if "dual_cultivation_target_username" in state:
        return str(state["dual_cultivation_target_username"] or "").strip().lstrip("@")
    return "" if account == "main" and identity == "主魂" else "Weeguu"


def normalize_dual_cultivation_target(value):
    value = str(value or "").strip().lstrip("@")
    if value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", value):
        raise ValueError("请输入有效的 Telegram 用户名，或留空等待配置")
    return value


class HehuanMixin:
    def dual_cultivation_target(self, identity="主魂"):
        identity = self.resolve_avatar_identity(identity)
        state = self.identity_state_for_timed_command(identity)
        default = dual_cultivation_default_target(actor_account_key(self), identity, state)
        target = dashboard_command_control_value(
            self, DUAL_CULTIVATION_COMMAND, "target_username", default=default, identity=identity,
        )
        return normalize_dual_cultivation_target(target)

    def dual_cultivation_message_time(self, msg):
        value = getattr(msg, "edit_date", None) or getattr(msg, "date", None) or datetime.now()
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value.strftime(TIME_FORMAT)

    async def _find_main_soul_recent_message(self):
        return await self.find_dual_cultivation_target("主魂")

    async def find_dual_cultivation_target(self, identity):
        """Reuse a valid main-soul anchor or search that sender's history."""
        state = self.identity_state_for_timed_command(self.resolve_avatar_identity(identity))
        target = self.dual_cultivation_target(identity)
        if not target:
            return None
        chat_id, _ = actor_message_target(self)

        async def is_main_soul(msg):
            if msg is None or not getattr(msg, "id", None):
                return False
            sender = getattr(msg, "sender", None)
            if sender is None:
                sender = await msg.get_sender()
            return str(getattr(sender, "username", "") or "").lstrip("@").casefold() == target.casefold()

        cached_id = state.get("dual_cultivation_target_message_id")
        if cached_id and state.get("dual_cultivation_target_chat_id") == chat_id:
            try:
                cached = await self.client.get_messages(chat_id, ids=int(cached_id))
                if await is_main_soul(cached):
                    return cached
            except Exception:
                pass
        try:
            messages = await self.client.get_messages(
                chat_id, from_user=target, limit=5,
            )
            for msg in messages or []:
                if await is_main_soul(msg):
                    state["dual_cultivation_target_message_id"] = msg.id
                    state["dual_cultivation_target_chat_id"] = chat_id
                    self.save_state()
                    return msg
        except Exception as exc:
            log.warning("Dual cultivation: main-soul history lookup failed: %s", exc)
        state.pop("dual_cultivation_target_message_id", None)
        state.pop("dual_cultivation_target_chat_id", None)
        return None

    @staticmethod
    def _dual_cultivation_pending(response):
        text = command_response_text(response)
        return "准备进行温养双修" in text or "开始进行温养双修" in text

    def _schedule_dual_cultivation_retry(self, seconds, status, response="", identity="主魂"):
        state = self.identity_state_for_timed_command(self.resolve_avatar_identity(identity))
        state["next_dual_cultivation_time"] = add_seconds_str(now_str(), seconds)
        state["dual_cultivation_last_status"] = status
        state["dual_cultivation_last_response"] = command_response_text(response)[:700]
        self.save_state()

    async def execute_dual_cultivation_once(self, identity="主魂"):
        """Wait for the edited settlement before starting the one-hour clock."""
        identity = self.resolve_avatar_identity(identity)
        state = self.identity_state_for_timed_command(identity)
        def retry(seconds, status, response=""):
            return self._schedule_dual_cultivation_retry(seconds, status, response, identity=identity)
        pending_id = state.get("dual_cultivation_pending_response_id")
        if pending_id:
            # A restart or a slow edit must not cause another accepted action.
            try:
                response = await self.client.get_messages(
                    state["dual_cultivation_pending_chat_id"], ids=int(pending_id),
                )
            except Exception:
                response = None
        else:
            if not self.sect_operation_allowed(identity, ".双修 温养"):
                return False
            selected_target = self.dual_cultivation_target(identity)
            if not selected_target:
                retry(300, "target_unconfigured")
                return False
            target_msg = (await self._find_main_soul_recent_message() if identity == "主魂"
                          else await self.find_dual_cultivation_target(identity))
            if target_msg is None:
                retry(DUAL_CULTIVATION_RETRY_SECONDS, "target_unavailable")
                log.warning("Dual cultivation 温养: main-soul anchor unavailable; retry in 1m.")
                return False
            log.info("Dual cultivation 温养 due; replying to main-soul msg %s.", target_msg.id)
            state["last_dual_cultivation_attempt_time"] = now_str()
            self.save_state()
            if not self.sect_operation_allowed(identity, ".双修 温养"):
                return False
            if selected_target != self.dual_cultivation_target(identity):
                return False
            sender = self.send_and_wait_feedback if identity == "主魂" else (
                lambda command, **kwargs: self.send_and_wait_feedback_identity(identity, command, **kwargs))
            response = await sender(
                ".双修 温养", timeout=45, max_retries=0,
                reply_to=target_msg.id, return_response_msg=True,
            )
            if self._dual_cultivation_pending(response) and getattr(response, "id", None):
                state["dual_cultivation_pending_response_id"] = response.id
                state["dual_cultivation_pending_chat_id"] = getattr(response, "chat_id", None) or self.target_chat_id
                state["dual_cultivation_pending_since"] = self.dual_cultivation_message_time(response)
                self.save_state()

        for _ in range(10):
            if not self._dual_cultivation_pending(response) or not getattr(response, "id", None):
                break
            await asyncio.sleep(2)
            try:
                updated = await self.client.get_messages(
                    getattr(response, "chat_id", None) or self.target_chat_id, ids=response.id,
                )
            except Exception:
                break
            if updated is not None:
                response = updated

        text = command_response_text(response)
        if ("温养双修·" in text or "温养双修成功" in text) and not any(
            marker in text for marker in ("失败", "无法", "未能")
        ):
            settled_at = self.dual_cultivation_message_time(response)
            state["last_dual_cultivation_time"] = settled_at
            state["next_dual_cultivation_time"] = add_seconds_str(
                settled_at, DUAL_CULTIVATION_INTERVAL_SECONDS + DUAL_CULTIVATION_BUFFER_SECONDS,
            )
            state["dual_cultivation_last_status"] = "completed"
            state["dual_cultivation_last_response"] = text[:700]
            log.info("Dual cultivation 温养 settled; next at %s.", state["next_dual_cultivation_time"])
        elif any(k in text for k in ("心神尚未恢复", "冷却中", "冷却")):
            remaining = re.search(r"(?:剩余|还需|还要|等待)\s*((?:\d+\s*(?:小时|分钟|分|秒)\s*)+)", text)
            seconds = sum(
                int(value) * {"小时": 3600, "分钟": 60, "分": 60, "秒": 1}[unit]
                for value, unit in re.findall(r"(\d+)\s*(小时|分钟|分|秒)", remaining.group(1))
            ) if remaining else 0
            retry(
                seconds + DUAL_CULTIVATION_BUFFER_SECONDS if seconds else DUAL_CULTIVATION_RETRY_SECONDS,
                "cooldown", response,
            )
        elif state.get("dual_cultivation_pending_response_id") or self._dual_cultivation_pending(response):
            since = state.get("dual_cultivation_pending_since") or now_str()
            try:
                elapsed = max(0, (datetime.now() - datetime.strptime(since, TIME_FORMAT)).total_seconds())
            except (TypeError, ValueError):
                since = now_str()
                elapsed = 60
            if elapsed < 60 and state.get("dual_cultivation_pending_response_id"):
                retry(30, "awaiting_settlement", response)
                return False
            # The bot accepted the action, but the edited result is unavailable.
            # Preserve its cooldown without recording a fictitious success.
            ready_at = add_seconds_str(since, DUAL_CULTIVATION_INTERVAL_SECONDS + 20)
            retry(max(60, seconds_until(ready_at)), "unconfirmed", response)
        else:
            retry(300, "no_result", response)

        for key in ("dual_cultivation_pending_response_id", "dual_cultivation_pending_chat_id", "dual_cultivation_pending_since"):
            state.pop(key, None)
        self.save_state()
        return state["dual_cultivation_last_status"] == "completed"

    async def run_dual_cultivation_loop(self, identity="主魂"):
        await self.startup_done.wait()
        state = self.identity_state_for_timed_command(self.resolve_avatar_identity(identity))
        if state.get("dual_cultivation_schedule_version") != 1:
            old_next = str(state.get("next_dual_cultivation_time") or "")
            try:
                # Legacy schedules used the initial reply + 65 minutes. Retain
                # ten seconds for that reply's later settlement during migration.
                next_dt = datetime.strptime(old_next, TIME_FORMAT) - timedelta(seconds=290)
                state["next_dual_cultivation_time"] = next_dt.strftime(TIME_FORMAT)
            except (ValueError, TypeError):
                state["next_dual_cultivation_time"] = now_str()
            state["dual_cultivation_schedule_version"] = 1
            self.save_state()
        while self.is_running:
            identity = self.resolve_avatar_identity(identity)
            state = self.identity_state_for_timed_command(identity)
            if not self.sect_operation_allowed(identity, ".双修 温养"):
                await asyncio.sleep(30)
                continue
            next_time = str(state.get("next_dual_cultivation_time") or "")
            wait = seconds_until(next_time) if next_time else 0
            if wait > 0:
                await asyncio.sleep(min(wait, 30))
                continue
            try:
                await self.execute_dual_cultivation_once(identity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Dual cultivation 温养 failed: %s", exc)
                self._schedule_dual_cultivation_retry(300, "error", identity=identity)
