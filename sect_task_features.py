"""Daily sect commands selected from each identity's confirmed membership."""
import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3

from command_modules import TimedCommandPlan
from log_utils import MESSAGE_EVENTS_DB_FILE, actor_account_key


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TAIYI_GUIDE_COMMAND = ".引道 水"
TAIYI_GUIDE_CD_SECONDS = 12 * 3600
TAIYI_GUIDE_RETRY_SECONDS = 600


@dataclass(frozen=True)
class SectDailyTask:
    key: str
    sect: str
    command: str
    last_key: str
    next_key: str

    def plan(self, identity):
        return TimedCommandPlan(identity, self.command, self.last_key, self.next_key,
                                timeout=60, max_retries=0, force_identity_check=True)


SECT_DAILY_TASKS = (
    SectDailyTask("taiyi_guide", "太一门", TAIYI_GUIDE_COMMAND,
                  "last_taiyi_guide_time", "next_taiyi_guide_time"),
    SectDailyTask("ask_dao", "元婴宗", ".问道", "last_ask_dao_time", "next_ask_dao_time"),
)


def parse_time(value):
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def task_for_command(command):
    root = str(command or "").strip().split()
    return next((task for task in SECT_DAILY_TASKS if root and root[0] == task.command.split()[0]), None)


class SectTaskMixin:
    def wake_sect_tasks(self):
        signal = getattr(self, "_sect_tasks_changed", None)
        if signal is not None:
            signal.set()
        router = getattr(self, "_miniapp_command_router", None)
        if router is not None and getattr(router, "_route_active", False):
            router._reconcile_star_palace_tasks()

    def sect_task_identities(self):
        return list(dict.fromkeys(self.resolve_avatar_identity(name)
                                 for name in ["主魂", *(getattr(self, "avatars", []) or [])]))

    def sect_command_allowed(self, command, identity=None):
        task = task_for_command(command)
        if task is None:
            return True
        identity = self.resolve_avatar_identity(identity or getattr(self, "current_identity", "主魂"))
        return identity in self.sect_task_identities() and self.identity_sect_name(identity) == task.sect

    def record_avatar_taiyi_guide_response(self, identity, response, source=TAIYI_GUIDE_COMMAND, *, observed_at=None):
        identity = self.resolve_avatar_identity(identity)
        state = self.identity_state_for_timed_command(identity)
        at = observed_at or datetime.now()
        if at.tzinfo is not None:
            at = at.astimezone().replace(tzinfo=None)
        previous = parse_time(state.get("taiyi_guide_observed_at"))
        if previous and previous > at:
            return "stale"
        text = self.common_response_text(response)
        clean = str(text or "").replace("**", "").replace("`", "")
        wait = max(0, int(self.parse_wait_time(clean) or 0))
        success = (
            (bool(re.search(r"引动[【\[][^】\]]+之道[】\]]", clean)) and "获得" in clean and "神识" in clean)
            or "引道成功" in clean
        )
        blocked = any(word in clean for word in (
            "非太一门", "不是太一门", "不属于太一门", "无法引道", "不能引道",
            "修为不足", "境界不足", "未加入", "失败",
        ))
        if blocked:
            status, wait = "blocked", 3600
        elif success:
            status, wait = "success", TAIYI_GUIDE_CD_SECONDS
        elif wait > 0 and any(word in clean for word in ("冷却", "后再", "还需", "尚需", "间隔", "请在")):
            status, wait = "cooldown", wait + 60
        else:
            status, wait = ("unknown" if clean else "empty"), TAIYI_GUIDE_RETRY_SECONDS
        state.update({
            "last_taiyi_guide_response": text[:500], "taiyi_guide_observed_at": at.strftime(TIME_FORMAT),
            "taiyi_guide_last_status": status,
            "next_taiyi_guide_time": (at + timedelta(seconds=wait)).strftime(TIME_FORMAT),
        })
        if status == "success":
            state["last_taiyi_guide_time"] = at.strftime(TIME_FORMAT)
        self.save_state()
        self.wake_sect_tasks()
        self.common_command_logger().info("Sect daily [%s] %s: %s; next at %s (%s).", identity,
            TAIYI_GUIDE_COMMAND, status, state["next_taiyi_guide_time"], source)
        return status

    def restore_sect_command_cooldowns(self):
        """Recover guide replies only when their original outgoing identity is known."""
        path = Path(MESSAGE_EVENTS_DB_FILE)
        if not path.is_file():
            return
        cutoff = (datetime.now() - timedelta(hours=13)).strftime(TIME_FORMAT)
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
                rows = database.execute("""
                    SELECT outgoing.identity, reply.text, reply.created_at
                    FROM message_events AS reply JOIN message_events AS outgoing
                      ON outgoing.account = reply.account AND outgoing.chat_id = reply.chat_id
                      AND outgoing.msg_id = reply.reply_to_msg_id
                    WHERE reply.account = ? AND reply.created_at >= ? AND reply.is_game_bot = 1
                      AND reply.direction IN ('bot_in', 'bot_edited') AND outgoing.command LIKE ?
                      AND outgoing.direction IN ('manual_out', 'auto_out') AND outgoing.created_at >= ?
                    ORDER BY reply.created_at, reply.id
                """, (actor_account_key(self), cutoff, ".引道 %", cutoff)).fetchall()
            restored = set()
            for identity, text, at_text in reversed(rows):
                identity = self.resolve_avatar_identity(identity)
                if identity not in self.sect_task_identities() or identity in restored:
                    continue
                state = self.identity_state_for_timed_command(identity)
                if state.get("next_taiyi_guide_time"):
                    continue
                at = parse_time(at_text)
                if at is None:
                    continue
                self.record_avatar_taiyi_guide_response(identity, text, source="history", observed_at=at)
                restored.add(identity)
        except (OSError, sqlite3.Error):
            self.common_command_logger().warning("Sect daily history unavailable; existing cooldowns retained.", exc_info=True)

    def sect_daily_wait(self, task, identity):
        identity = self.resolve_avatar_identity(identity)
        if not self.sect_command_allowed(task.command, identity):
            return 60
        if (self.state.get("is_paused") or self.identity_pause_seconds(identity) > 0
                or self.dashboard_command_paused(task.command, identity)):
            return 60
        state = self.identity_state_for_timed_command(identity)
        due = parse_time(state.get(task.next_key))
        return max(0, (due - datetime.now()).total_seconds()) if due else 0

    async def execute_sect_daily_once(self, task, identity):
        identity = self.resolve_avatar_identity(identity)
        if self.sect_daily_wait(task, identity) > 0:
            return
        if task.key == "ask_dao":
            await self.common_ask_dao_tick(identity=identity)
            return
        async with self.common_atomic_task("SectDaily-" + identity, log_lifecycle=False):
            identity = self.resolve_avatar_identity(identity)
            if self.sect_daily_wait(task, identity) > 0:
                return
            response = await self.send_timed_command_plan(task.plan(identity), identity)
            identity = self.resolve_avatar_identity(identity)
            if self.sect_command_allowed(task.command, identity):
                self.record_avatar_taiyi_guide_response(identity, response)

    async def run_sect_daily_loop(self):
        await self.startup_done.wait()
        self._sect_tasks_changed = asyncio.Event()
        self.restore_sect_command_cooldowns()
        while self.is_running:
            self._sect_tasks_changed.clear()
            wait = 60
            for identity in self.sect_task_identities():
                for task in SECT_DAILY_TASKS:
                    if not self.sect_command_allowed(task.command, identity):
                        continue
                    try:
                        if self.sect_daily_wait(task, identity) <= 0:
                            await self.execute_sect_daily_once(task, identity)
                        wait = min(wait, max(5, self.sect_daily_wait(task, identity)))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        state = self.identity_state_for_timed_command(self.resolve_avatar_identity(identity))
                        state[task.next_key] = (datetime.now() + timedelta(minutes=10)).strftime(TIME_FORMAT)
                        self.save_state()
                        self.common_command_logger().exception("Sect daily [%s] %s failed.", identity, task.command)
            if not self.is_running:
                break
            try:
                await asyncio.wait_for(self._sect_tasks_changed.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
