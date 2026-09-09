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
from hehuan_features import HehuanMixin
from lingxiao_features import LingxiaoMixin
from sect_rules import SectTaskStopped, command_sect, identity_names, task_paused


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


class SectTaskMixin(LingxiaoMixin, HehuanMixin):
    def wake_sect_tasks(self):
        signal = getattr(self, "_sect_tasks_changed", None)
        if signal is not None:
            signal.set()
        for signal in getattr(self, "_sect_identity_signals", {}).values():
            signal.set()
        router = getattr(self, "_miniapp_command_router", None)
        if router is not None and getattr(router, "_route_active", False):
            router._reconcile_star_palace_tasks()

    def sect_task_identities(self):
        return identity_names(self)

    def sect_command_allowed(self, command, identity=None):
        sect = command_sect(command)
        if not sect:
            return True
        identity = self.resolve_avatar_identity(identity or getattr(self, "current_identity", "主魂"))
        return identity in self.sect_task_identities() and self.identity_sect_name(identity) == sect

    def sect_operation_allowed(self, identity, command):
        if getattr(self, "_restricted_miniapp_worker", None) is not None:
            from miniapp_dwelling import miniapp_command_allowed
            if not str(command).startswith("miniapp:") and not miniapp_command_allowed(command):
                return False
        return self.sect_command_allowed(command, identity) and not task_paused(self, identity, command)

    def reconcile_sect_features(self):
        tasks = getattr(self, "_sect_identity_tasks", {})
        signals = getattr(self, "_sect_identity_signals", {})
        self._sect_identity_tasks, self._sect_identity_signals = tasks, signals
        for identity in self.sect_task_identities():
            key = next((name for name in tasks if self.resolve_avatar_identity(name) == identity), identity)
            if key in tasks and not tasks[key].done():
                continue
            signals[key] = asyncio.Event()
            register = getattr(self, "create_scheduler_task", None)
            if callable(register):
                tasks[key] = register("sect_features_" + key, lambda key=key: self.run_sect_identity_features(key))
            else:
                tasks[key] = asyncio.create_task(self.run_sect_identity_features(key), name="sect_features_" + key)

    async def run_sect_identity_features(self, original_identity):
        signal = self._sect_identity_signals[original_identity]
        while self.is_running:
            signal.clear()
            identity = self.resolve_avatar_identity(original_identity)
            wait = 60
            if identity not in self.sect_task_identities():
                await asyncio.sleep(60)
                continue
            try:
                if not task_paused(self, identity):
                    sect = self.identity_sect_name(identity)
                    state = self.identity_state_for_timed_command(identity)
                    runtime = state.setdefault("sect_task_runtime", {})
                    retry = parse_time(runtime.get("retry_at"))
                    if runtime.get("sect") == sect and retry and retry > datetime.now():
                        await asyncio.sleep(min(60, (retry - datetime.now()).total_seconds()))
                        continue
                    runtime.update({"sect": sect, "checked_at": datetime.now().strftime(TIME_FORMAT)})
                    runtime.pop("retry_at", None)
                    if sect == "凌霄宫":
                        wait = await self.lingxiao_tick(identity)
                    elif sect == "阴罗宗":
                        wait = await self.yinluo_tick(identity)
                    elif sect == "合欢宗" and self.sect_operation_allowed(identity, ".双修 温养"):
                        next_time = parse_time(state.get("next_dual_cultivation_time"))
                        wait = max(0, (next_time - datetime.now()).total_seconds()) if next_time else 0
                        if wait <= 0:
                            async with self.common_atomic_task("Hehuan-" + identity, log_lifecycle=False):
                                await self.execute_dual_cultivation_once(identity)
                            wait = 30
                    elif sect == "天星宗":
                        if state.get("last_destiny_observation_date") != datetime.now().strftime("%Y-%m-%d"):
                            now = datetime.now()
                            defer = now < now.replace(hour=0, minute=15, second=59)
                            if defer:
                                defer = self.daily_one_shot_should_defer(identity, ".观命", logger=self.common_command_logger())
                            if not defer and self.tianxing_destiny_retry_wait_seconds(identity) <= 0:
                                async with self.common_atomic_task("Tianxing-" + identity, log_lifecycle=False):
                                    await self.observe_tianxing_destiny(identity)
                                if state.get("last_destiny_observation_date") != datetime.now().strftime("%Y-%m-%d"):
                                    state["next_tianxing_destiny_retry_time"] = (datetime.now() + timedelta(minutes=10)).strftime(TIME_FORMAT)
                                    self.save_state()
                    elif sect == "万灵宗":
                        wait = await self.run_wanling_identity_once(identity)
                    runtime["status"] = "active" if sect in {"天星宗", "阴罗宗", "凌霄宫", "万灵宗", "合欢宗"} else "idle"
                    self.save_state()
            except asyncio.CancelledError:
                raise
            except SectTaskStopped:
                wait = 60
            except Exception:
                wait = 300
                state = self.identity_state_for_timed_command(self.resolve_avatar_identity(original_identity))
                state.setdefault("sect_task_runtime", {}).update({
                    "status": "retry", "retry_at": (datetime.now() + timedelta(seconds=wait)).strftime(TIME_FORMAT),
                })
                self.save_state()
                self.common_command_logger().exception("Sect workflow [%s] failed.", identity)
            if not self.is_running:
                return
            try:
                await asyncio.wait_for(signal.wait(), timeout=max(5, min(float(wait or 5), 60)))
            except asyncio.TimeoutError:
                pass

    async def run_wanling_identity_once(self, identity):
        from miniapp_beast_abyss import MiniAppBeastAbyssWorker
        from miniapp_beast_contract import MiniAppBeastContractWorker
        from miniapp_beast_seek import MiniAppBeastSeekWorker
        identity = self.resolve_avatar_identity(identity)
        router = getattr(self, "_miniapp_command_router", None)
        restricted = getattr(self, "_restricted_miniapp_worker", None)
        transport = getattr(router, "transport", None) if getattr(router, "_route_active", False) else None
        if transport is None and restricted is not None and self.state.get("restricted_miniapp_active"):
            transport = restricted.transport
        if transport is None:
            return 60
        cache = getattr(self, "_sect_wanling_workers", {})
        self._sect_wanling_workers = cache
        key = next((name for name in cache if self.resolve_avatar_identity(name) == identity), identity)
        if key not in cache or cache[key][0] is not transport:
            logger = self.common_command_logger()
            cache[key] = (transport,
                MiniAppBeastContractWorker(self, transport, logger, identity=identity),
                MiniAppBeastAbyssWorker(self, transport, actor_account_key(self), logger),
                MiniAppBeastSeekWorker(self, transport, actor_account_key(self), logger, identity=identity))
        _, contract, abyss, seek = cache[key]
        for worker, command, next_key, runner in (
            (contract, "miniapp:spirit-beast-contract", "beast_contract_interaction_next_time", contract.run_cycle),
            (abyss, "miniapp:spirit-beast-abyss", "beast_abyss_miniapp_next_time", lambda: abyss.run_once(identity)),
            (seek, ".寻觅灵兽", "beast_seek_miniapp_next_time", seek.run_cycle),
        ):
            identity = self.resolve_avatar_identity(identity)
            state = self.identity_state_for_timed_command(identity)
            due = parse_time(state.get(next_key))
            if worker.enabled and self.sect_operation_allowed(identity, command) and (not due or due <= datetime.now()):
                try:
                    await runner()
                except SectTaskStopped:
                    return 60
        return 60

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
        if not self.sect_operation_allowed(identity, task.command):
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
        try:
            await self._run_sect_daily_loop()
        finally:
            tasks = list(getattr(self, "_sect_identity_tasks", {}).values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_sect_daily_loop(self):
        await self.startup_done.wait()
        self._sect_tasks_changed = asyncio.Event()
        self.restore_sect_command_cooldowns()
        while self.is_running:
            self._sect_tasks_changed.clear()
            self.reconcile_sect_features()
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
