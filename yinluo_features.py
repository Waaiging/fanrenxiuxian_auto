"""
【阴罗宗功能模块 —— 阴罗宗化身的阴罗幡自动化】

主号缘生子和副号玄续玄共用这套阴罗宗指令，相关逻辑集中放在这里，
避免两个脚本改漏。主要流程：
  1. `.我的阴罗幡` 同步槽位、幡灵、煞气等状态。
  2. `.化功为煞 10000` 失败/冷却时必须解析回复时间，不能盲目重试。
  3. 每次召唤魔影成功后固定执行：同步阴罗幡、按槽收取凶兽戾魄
     精华、一键安抚幡灵、再把储备凶兽戾魄逐槽囚禁。

调用方只需要继承 YinluoMixin，并提供发送指令、状态读写和日志能力。
"""
import asyncio
import re
from datetime import datetime, timedelta

from common_command_features import add_seconds_str, is_future, now_str, seconds_until


YINLUO_IDENTITY = "缘生子"
YINLUO_MASTER_COMMAND = ".我的阴罗幡"
YINLUO_SOUL = "凶兽戾魄"
YINLUO_REFINE_COST_SHA = 1000
YINLUO_CONVERT_COMMAND = ".化功为煞 10000"
YINLUO_APPEASE_COMMAND = ".一键安抚幡灵"
YINLUO_COLLECT_COMMAND = ".收取精华"
YINLUO_CONVERT_FAILURE_RETRY_SECONDS = 60 * 60  # 明确转化失败后固定等待 1 小时
YINLUO_RETRY_SECONDS = 10 * 60              # 未知/短失败的保守重试间隔
YINLUO_SYNC_SECONDS = 30 * 60               # 状态缓存最多 30 分钟刷新一次
YINLUO_IMPENDING_GUARD_SECONDS = 120        # 到点前 2 分钟阻止其他流程抢身份
YINLUO_APPEASE_NOOP_SUPPRESS_SECONDS = 30 * 60  # 安抚无事可做时降噪


class _YinluoAtomicTask:
    """Keep edited-settlement Yinluo commands from being interrupted mid-result."""

    def __init__(self, actor, label):
        self.actor = actor
        self.label = label
        self.task = None
        self.acquired = False

    async def __aenter__(self):
        if not hasattr(self.actor, "active_atomic_task"):
            return self
        self.task = asyncio.current_task()
        while getattr(self.actor, "active_atomic_task", None) is not None and self.actor.active_atomic_task != self.task:
            await asyncio.sleep(0.5)
        if getattr(self.actor, "active_atomic_task", None) == self.task:
            return self
        self.actor.active_atomic_task = self.task
        self.actor._concubine_atomic_task = self.task
        self.actor._concubine_atomic_label = self.label
        self.actor._atomic_task_high_priority_bypass_task = self.task
        self.actor._atomic_task_high_priority_bypass_label = self.label
        self.acquired = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.acquired and getattr(self.actor, "active_atomic_task", None) == self.task:
            self.actor.active_atomic_task = None
        if self.acquired and getattr(self.actor, "_concubine_atomic_task", None) == self.task:
            self.actor._concubine_atomic_task = None
            self.actor._concubine_atomic_label = ""
            if getattr(self.actor, "_atomic_task_high_priority_bypass_task", None) == self.task:
                self.actor._atomic_task_high_priority_bypass_task = None
                self.actor._atomic_task_high_priority_bypass_label = ""
        return False


def _strip_markdown(text):
    return str(text or "").replace("**", "").replace("`", "")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _next_day_time(hour=0, minute=5):
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _parse_duration_seconds(text):
    """从阴罗宗回复中解析“1小时40分钟43秒”这类冷却文本。"""
    clean = str(text or "")
    total = 0
    matched = False
    for pattern, factor in (
        (r"(\d+)\s*天", 86400),
        (r"(\d+)\s*(?:小时|时)", 3600),
        (r"(\d+)\s*(?:分钟|分)", 60),
        (r"(\d+)\s*秒", 1),
    ):
        for match in re.finditer(pattern, clean):
            total += int(match.group(1)) * factor
            matched = True
    return total if matched else 0


def yinluo_default_state():
    return {
        "last_sync_at": "",
        "last_status": "init",
        "last_detail": "",
        "last_response": "",
        "next_action_at": "",
        "next_sync_at": "",
        "next_daily_sacrifice_time": "",
        "next_blood_wash_time": "",
        "next_summon_shadow_time": "",
        "rank": "",
        "weapon_name": "",
        "main_flow": "",
        "sha_current": 0,
        "sha_max": 0,
        "total_refined": 0,
        "reserves": {},
        "slots": {},
        "appease_suppressed_until": {},
        "imprison_sync_pending": False,
        "post_summon_stage": "",
        "post_summon_target_slots": [],
        "post_summon_started_at": "",
        "last_daily_sacrifice_date": "",
        "last_collected_at": "",
    }


def parse_yinluo_status(text):
    clean = _strip_markdown(text)
    result = {
        "matched": "阴罗幡" in clean and "炼化槽" in clean,
        "weapon_name": "",
        "rank": "",
        "sha_current": None,
        "sha_max": None,
        "main_flow": "",
        "total_refined": None,
        "reserves": {},
        "slots": {},
    }
    if not result["matched"]:
        return result

    patterns = {
        "weapon_name": r"本命魔兵:\s*([^\n]+)",
        "rank": r"幡体等阶:\s*([^\n]+)",
        "main_flow": r"主魂流派:\s*([^\n]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, clean)
        if match:
            result[key] = match.group(1).strip()

    sha_match = re.search(r"煞气池:\s*(\d+)\s*/\s*(\d+)", clean)
    if sha_match:
        result["sha_current"] = int(sha_match.group(1))
        result["sha_max"] = int(sha_match.group(2))

    refined_match = re.search(r"幡魂总炼化:\s*(\d+)\s*缕", clean)
    if refined_match:
        result["total_refined"] = int(refined_match.group(1))

    section = ""
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if line.startswith("魂魄储备"):
            section = "reserves"
            if "无" in line:
                result["reserves"] = {}
            continue
        if line.startswith("炼化槽"):
            section = "slots"
            continue
        if section == "reserves":
            reserve_match = re.match(r"-\s*([^:：]+)[:：]\s*(\d+)\s*缕", line)
            if reserve_match:
                result["reserves"][reserve_match.group(1).strip()] = int(reserve_match.group(2))
            continue
        if section == "slots":
            slot_match = re.match(r"(?:\*)?(\d+)号槽(?:\*)?:\s*\[([^\]]+)\]\s*(.*)$", line)
            if not slot_match:
                continue
            slot = int(slot_match.group(1))
            status = slot_match.group(2).strip()
            tail = (slot_match.group(3) or "").strip()
            remaining_match = re.search(r"\(剩余:\s*([^\)]+)\)", tail)
            soul_match = re.match(r"-\s*(.*?)(?:\s*\(剩余:|$)", tail)
            soul = (soul_match.group(1) if soul_match else "").strip().strip("!！❗")
            remaining_text = (remaining_match.group(1) if remaining_match else "").strip()
            remaining = _parse_duration_seconds(remaining_text)
            result["slots"][slot] = {
                "status": status,
                "soul": soul,
                "remaining_seconds": remaining,
                "remaining_text": remaining_text,
            }
    return result


def parse_yinluo_daily_sacrifice(text):
    clean = _strip_markdown(text)
    if "今日已献祭" in clean:
        return {"matched": True, "status": "done", "sha_gain": 0}
    gain_match = re.search(r"煞气池增加了\s*(\d+)\s*点", clean)
    if gain_match:
        return {"matched": True, "status": "success", "sha_gain": int(gain_match.group(1))}
    return {"matched": False, "status": "", "sha_gain": 0}


def parse_yinluo_blood_wash(text):
    clean = _strip_markdown(text)
    cooldown = re.search(r"请在\s*([^后]+?)\s*后再来", clean)
    if cooldown:
        return {"matched": True, "status": "cooldown", "cooldown_seconds": _parse_duration_seconds(cooldown.group(1)), "souls": {}}
    if "血洗功成" in clean:
        souls = {}
        for count, name in re.findall(r"(\d+)\s*缕【([^】]+)】", clean):
            souls[name] = souls.get(name, 0) + int(count)
        return {"matched": True, "status": "success", "cooldown_seconds": 4 * 3600, "souls": souls}
    if "前往附近的山脉" in clean:
        return {"matched": True, "status": "pending", "cooldown_seconds": 60, "souls": {}}
    return {"matched": False, "status": "", "cooldown_seconds": 0, "souls": {}}


def parse_yinluo_summon_shadow(text):
    clean = _strip_markdown(text)
    cooldown = re.search(r"请在\s*([^后]+?)\s*后再行召唤", clean)
    if cooldown:
        return {"matched": True, "status": "cooldown", "cooldown_seconds": _parse_duration_seconds(cooldown.group(1)), "soul": ""}
    if ("召唤成功" in clean and "镇压成功" in clean) or ("魔影被你成功击溃" in clean and "阴罗幡" in clean):
        soul_match = re.search(r"【([^】]+)】", clean)
        return {
            "matched": True,
            "status": "success",
            "cooldown_seconds": 8 * 3600,
            "soul": soul_match.group(1).strip() if soul_match else YINLUO_SOUL,
        }
    if "镇压失败" in clean:
        return {
            "matched": True,
            "status": "failed",
            "cooldown_seconds": 8 * 3600,
            "soul": "",
        }
    if "三级妖丹" in clean and "开始撕裂空间" in clean:
        return {"matched": True, "status": "pending", "cooldown_seconds": 60, "soul": ""}
    if "缺少" in clean or "不足" in clean:
        return {"matched": True, "status": "blocked", "cooldown_seconds": 3600, "soul": ""}
    return {"matched": False, "status": "", "cooldown_seconds": 0, "soul": ""}


def parse_yinluo_convert(text):
    clean = _strip_markdown(text)
    success = re.search(r"煞气池增加了\s*(\d+)\s*点", clean)
    if success:
        return {"matched": True, "status": "success", "sha_gain": int(success.group(1)), "cooldown_seconds": 0}
    if "开始运转魔功" in clean:
        return {"matched": True, "status": "pending", "sha_gain": 0, "cooldown_seconds": 0}

    # 只解析明确的重试时间，避免把“15分钟内闭关收益降低”等反噬时长当成冷却。
    cooldown = re.search(r"请在\s*([^后\n]+?)\s*后(?:再试|重试|再来|再行)", clean)
    cooldown_seconds = _parse_duration_seconds(cooldown.group(1)) if cooldown else 0
    if cooldown_seconds > 0:
        return {"matched": True, "status": "cooldown", "sha_gain": 0, "cooldown_seconds": cooldown_seconds}
    if any(key in clean for key in ("转化失败", "化功为煞失败", "煞气反噬")):
        return {
            "matched": True,
            "status": "failed",
            "sha_gain": 0,
            "cooldown_seconds": YINLUO_CONVERT_FAILURE_RETRY_SECONDS,
        }
    if "修为不足" in clean or "无法" in clean:
        return {"matched": True, "status": "blocked", "sha_gain": 0, "cooldown_seconds": YINLUO_CONVERT_FAILURE_RETRY_SECONDS}
    return {"matched": False, "status": "", "sha_gain": 0, "cooldown_seconds": 0}


def parse_yinluo_imprison(text):
    clean = _strip_markdown(text)
    if "被强行打入" in clean and "炼化已开始" in clean:
        slot_match = re.search(r"打入\s*(\d+)号炼化槽", clean)
        soul_match = re.search(r"一缕【([^】]+)】", clean)
        return {
            "matched": True,
            "status": "success",
            "slot": int(slot_match.group(1)) if slot_match else 0,
            "soul": soul_match.group(1).strip() if soul_match else "",
        }
    if "煞气不足" in clean:
        return {"matched": True, "status": "sha_not_enough", "slot": 0, "soul": ""}
    if "正在运转" in clean or "无法囚禁" in clean:
        return {"matched": True, "status": "slot_busy", "slot": 0, "soul": ""}
    if "魂魄袋中没有" in clean:
        return {"matched": True, "status": "no_soul", "slot": 0, "soul": ""}
    return {"matched": False, "status": "", "slot": 0, "soul": ""}


def parse_yinluo_collect(text):
    clean = _strip_markdown(text)
    if "收取成功" in clean or "成功收取" in clean:
        refined = {}
        for name, count in re.findall(r"([^,，:：\s]+)\+(\d+)", clean):
            refined[name.strip()] = int(count)
        return {"matched": True, "status": "success", "refined": refined}
    if any(key in clean for key in ("没有", "暂无", "尚未")) and ("精华" in clean or "可收取" in clean):
        return {"matched": True, "status": "empty", "refined": {}}
    return {"matched": False, "status": "", "refined": {}}


def parse_yinluo_appease(text):
    clean = _strip_markdown(text)
    count_match = re.search(r"成功安抚(?:了)?\s*(\d+)\s*个炼化槽", clean)
    if "安抚成功" in clean or count_match:
        return {"matched": True, "status": "success", "count": int(count_match.group(1)) if count_match else 0}
    if any(key in clean for key in (
        "无需安抚",
        "没有需要安抚",
        "暂无需要安抚",
        "没有需要操作的炼化槽",
    )):
        return {"matched": True, "status": "success", "count": 0}
    return {"matched": False, "status": "", "count": 0}


class YinluoMixin:
    def yinluo_identity_name(self):
        return str(getattr(self, "yinluo_identity", "") or YINLUO_IDENTITY).strip()

    def get_yinluo_state(self, identity=YINLUO_IDENTITY):
        target = self.state if identity == "主魂" else self.get_avatar_state(identity)
        state = target.get("yinluo")
        if not isinstance(state, dict):
            state = yinluo_default_state()
            target["yinluo"] = state
        else:
            defaults = yinluo_default_state()
            for key, value in defaults.items():
                state.setdefault(key, value)
        if state.get("imprison_sync_pending") and not state.get("post_summon_stage"):
            state["post_summon_stage"] = "sync"
            state["post_summon_started_at"] = state.get("post_summon_started_at") or now_str()
        return state

    def yinluo_logger(self):
        return getattr(self, "log", None)

    def yinluo_response_text(self, response):
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        return str(getattr(response, "text", "") or getattr(response, "raw_text", "") or "")

    def yinluo_set_status(self, identity, status, detail="", next_seconds=None, response=""):
        state = self.get_yinluo_state(identity)
        state["last_status"] = status
        state["last_detail"] = str(detail or "")[:300]
        if response:
            state["last_response"] = str(response or "")[:700]
        if next_seconds is not None:
            state["next_action_at"] = add_seconds_str(now_str(), next_seconds)
        self.save_state()

    async def send_yinluo_command(self, identity, command, timeout=60, edited_wait=0):
        async def _send_and_refresh():
            msg = await self.send_and_wait_feedback_identity(
                identity,
                command,
                timeout=timeout,
                max_retries=0,
                suppress_no_response_alert=True,
                return_response_msg=True,
            )
            text = self.yinluo_response_text(msg)
            if edited_wait and msg is not None and getattr(msg, "id", None):
                await asyncio.sleep(edited_wait)
                try:
                    fresh = await self.client.get_messages(self.target_chat_id, ids=msg.id)
                    fresh_text = self.yinluo_response_text(fresh)
                    if fresh_text:
                        text = fresh_text
                except Exception:
                    pass
            return text

        if edited_wait:
            command_head = str(command or "").strip().split(" ", 1)[0] or "command"
            async with _YinluoAtomicTask(self, f"YinluoEdit-{identity}-{command_head}"):
                return await _send_and_refresh()

        return await _send_and_refresh()

    def yinluo_wait_from_state(self, identity, default_seconds=YINLUO_RETRY_SECONDS):
        state = self.get_yinluo_state(identity)
        waits = []
        for key in (
            "next_action_at",
            "next_daily_sacrifice_time",
            "next_blood_wash_time",
            "next_summon_shadow_time",
        ):
            value = state.get(key, "")
            if value and is_future(value):
                waits.append(seconds_until(value))
        for slot in (state.get("slots") or {}).values():
            due_at = slot.get("due_at") if isinstance(slot, dict) else ""
            if due_at and is_future(due_at):
                waits.append(seconds_until(due_at))
        waits = [w for w in waits if w is not None and w >= 0]
        return min(waits) if waits else default_seconds

    def yinluo_refining_slots_due(self, identity):
        slots = self.get_yinluo_state(identity).get("slots") or {}
        due = []
        for slot, item in slots.items():
            if not isinstance(item, dict) or item.get("status") != "炼化中":
                continue
            due_at = item.get("due_at") or ""
            try:
                remaining = int(item.get("remaining_seconds") or 0)
            except (TypeError, ValueError):
                remaining = 0
            if (due_at and not is_future(due_at)) or (not due_at and remaining <= 0):
                due.append(int(slot))
        return sorted(due)

    def yinluo_schedule_next_action(self, identity):
        state = self.get_yinluo_state(identity)
        candidates = []
        for key in (
            "next_daily_sacrifice_time",
            "next_blood_wash_time",
            "next_summon_shadow_time",
        ):
            value = state.get(key, "")
            if value and is_future(value):
                candidates.append((seconds_until(value), value))
        for slot in (state.get("slots") or {}).values():
            if not isinstance(slot, dict) or slot.get("status") != "炼化中":
                continue
            due_at = slot.get("due_at") or ""
            if due_at and is_future(due_at):
                candidates.append((seconds_until(due_at), due_at))

        next_action = min(candidates, key=lambda item: item[0])[1] if candidates else ""
        if state.get("next_action_at", "") != next_action:
            state["next_action_at"] = next_action
            self.save_state()
        return seconds_until(next_action) if next_action and is_future(next_action) else None

    def yinluo_impending_wait(self, identity):
        try:
            wait = self.get_identity_impending_command_wait(identity)
        except Exception:
            return -1
        if wait is None:
            return -1
        try:
            return float(wait)
        except Exception:
            return -1

    def yinluo_empty_slots(self, identity):
        slots = self.get_yinluo_state(identity).get("slots") or {}
        return sorted(int(slot) for slot, item in slots.items() if isinstance(item, dict) and item.get("status") == "空闲")

    def yinluo_completed_slots(self, identity):
        slots = self.get_yinluo_state(identity).get("slots") or {}
        return sorted(int(slot) for slot, item in slots.items() if isinstance(item, dict) and item.get("status") == "精华已成")

    def yinluo_completed_fierce_slots(self, identity):
        slots = self.get_yinluo_state(identity).get("slots") or {}
        return sorted(
            int(slot)
            for slot, item in slots.items()
            if (
                isinstance(item, dict)
                and item.get("status") == "精华已成"
                and str(item.get("soul") or "").strip() == YINLUO_SOUL
            )
        )

    def yinluo_slot_item(self, identity, slot):
        slots = self.get_yinluo_state(identity).get("slots") or {}
        return slots.get(slot) or slots.get(str(slot)) or {}

    def yinluo_replace_slot(self, identity, slot, item):
        slots = self.get_yinluo_state(identity).setdefault("slots", {})
        key = slot if slot in slots else str(slot)
        slots.pop(slot, None)
        slots.pop(str(slot), None)
        slots[key] = dict(item)

    def yinluo_exhausted_slots(self, identity):
        state = self.get_yinluo_state(identity)
        slots = state.get("slots") or {}
        suppressed = state.get("appease_suppressed_until") or {}
        exhausted = []
        for slot, item in slots.items():
            if not isinstance(item, dict) or "魂力枯竭" not in str(item.get("status") or ""):
                continue
            slot_num = int(slot)
            until = suppressed.get(str(slot_num)) or suppressed.get(slot_num)
            if until and is_future(until):
                continue
            exhausted.append(slot_num)
        return sorted(exhausted)

    async def yinluo_sync_banner(self, identity):
        text = await self.send_yinluo_command(identity, YINLUO_MASTER_COMMAND, timeout=60)
        parsed = parse_yinluo_status(text)
        state = self.get_yinluo_state(identity)
        state["last_response"] = text[:700]
        if not parsed.get("matched"):
            self.yinluo_set_status(identity, "sync_failed", "阴罗幡回复未识别", YINLUO_RETRY_SECONDS, text)
            return False
        for key in ("weapon_name", "rank", "main_flow", "reserves", "slots"):
            state[key] = parsed.get(key)
        for key in ("sha_current", "sha_max", "total_refined"):
            if parsed.get(key) is not None:
                state[key] = parsed.get(key)
        now = now_str()
        for slot in state.get("slots", {}).values():
            if not isinstance(slot, dict):
                continue
            if slot.get("status") == "炼化中":
                try:
                    remaining = int(slot.get("remaining_seconds") or 0)
                except (TypeError, ValueError):
                    remaining = 0
                slot["due_at"] = add_seconds_str(
                    now,
                    remaining if remaining > 0 else YINLUO_RETRY_SECONDS,
                )
            else:
                slot["due_at"] = ""
        suppressed = state.get("appease_suppressed_until")
        if isinstance(suppressed, dict):
            exhausted_slots = {
                str(slot)
                for slot, item in (state.get("slots") or {}).items()
                if isinstance(item, dict) and "魂力枯竭" in str(item.get("status") or "")
            }
            for slot, until in list(suppressed.items()):
                if not is_future(until) or str(slot) not in exhausted_slots:
                    suppressed.pop(slot, None)
        state["last_sync_at"] = now
        state["next_sync_at"] = ""
        state["last_status"] = "synced"
        state["last_detail"] = f"煞气 {state.get('sha_current', 0)}/{state.get('sha_max', 0)}，凶兽戾魄 {state.get('reserves', {}).get(YINLUO_SOUL, 0)}"
        self.save_state()
        return True

    async def yinluo_daily_sacrifice(self, identity):
        text = await self.send_yinluo_command(identity, ".每日献祭", timeout=60)
        parsed = parse_yinluo_daily_sacrifice(text)
        state = self.get_yinluo_state(identity)
        if parsed.get("status") == "success":
            state["sha_current"] = int(state.get("sha_current") or 0) + int(parsed.get("sha_gain") or 0)
            state["last_daily_sacrifice_date"] = _today()
            state["next_daily_sacrifice_time"] = _next_day_time()
            self.yinluo_set_status(identity, "sacrificed", f"每日献祭 +{parsed.get('sha_gain')} 煞气", 5, text)
            return True
        if parsed.get("status") == "done":
            state["last_daily_sacrifice_date"] = _today()
            state["next_daily_sacrifice_time"] = _next_day_time()
            self.yinluo_set_status(identity, "sacrifice_done", "今日已献祭", None, text)
            return True
        self.yinluo_set_status(identity, "sacrifice_failed", "每日献祭回复未识别", YINLUO_RETRY_SECONDS, text)
        return False

    async def yinluo_blood_wash(self, identity):
        text = await self.send_yinluo_command(identity, ".血洗山林", timeout=60, edited_wait=6)
        parsed = parse_yinluo_blood_wash(text)
        state = self.get_yinluo_state(identity)
        if parsed.get("status") == "success":
            reserves = state.setdefault("reserves", {})
            for soul, count in (parsed.get("souls") or {}).items():
                reserves[soul] = int(reserves.get(soul, 0)) + int(count)
            state["next_blood_wash_time"] = add_seconds_str(now_str(), int(parsed.get("cooldown_seconds") or 4 * 3600))
            self.yinluo_set_status(identity, "blood_wash", "血洗山林完成", 5, text)
            return True
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or YINLUO_RETRY_SECONDS))
            state["next_blood_wash_time"] = add_seconds_str(now_str(), wait)
            self.yinluo_set_status(identity, "blood_wash_cd", f"血洗山林冷却 {wait}秒", None, text)
            return True
        if parsed.get("status") == "pending":
            self.yinluo_set_status(identity, "blood_wash_pending", "等待血洗山林结算", 60, text)
            return True
        self.yinluo_set_status(identity, "blood_wash_failed", "血洗山林回复未识别", YINLUO_RETRY_SECONDS, text)
        return False

    async def yinluo_summon_shadow(self, identity):
        async with _YinluoAtomicTask(self, f"YinluoSummonFlow-{identity}"):
            text = await self.send_yinluo_command(identity, ".召唤魔影", timeout=60, edited_wait=8)
            parsed = parse_yinluo_summon_shadow(text)
            state = self.get_yinluo_state(identity)
            if parsed.get("status") == "success":
                soul = parsed.get("soul") or YINLUO_SOUL
                reserves = state.setdefault("reserves", {})
                reserves[soul] = int(reserves.get(soul, 0)) + 1
                state["next_summon_shadow_time"] = add_seconds_str(
                    now_str(), int(parsed.get("cooldown_seconds") or 8 * 3600)
                )
                state["imprison_sync_pending"] = True
                state["post_summon_stage"] = "sync"
                state["post_summon_target_slots"] = []
                state["post_summon_started_at"] = now_str()
                state["next_sync_at"] = ""
                self.yinluo_set_status(
                    identity,
                    "summoned",
                    f"召唤魔影获得 {soul}，开始阴罗幡收取、安抚与囚禁流程",
                    5,
                    text,
                )
                await self.yinluo_run_post_summon_flow(identity)
                return True
            if parsed.get("status") == "cooldown":
                wait = max(60, int(parsed.get("cooldown_seconds") or YINLUO_RETRY_SECONDS))
                state["next_summon_shadow_time"] = add_seconds_str(now_str(), wait)
                self.yinluo_set_status(identity, "summon_cd", f"召唤魔影冷却 {wait}秒", None, text)
                return True
            if parsed.get("status") == "failed":
                wait = max(60, int(parsed.get("cooldown_seconds") or 8 * 3600))
                state["next_summon_shadow_time"] = add_seconds_str(now_str(), wait)
                self.yinluo_set_status(
                    identity,
                    "summon_failed",
                    f"召唤魔影镇压失败，{wait}秒后再试",
                    None,
                    text,
                )
                return True
            if parsed.get("status") == "pending":
                self.yinluo_set_status(identity, "summon_pending", "等待召唤魔影结算", 60, text)
                return True
            self.yinluo_set_status(identity, "summon_failed", "召唤魔影未成功或材料不足", 3600, text)
            return False

    async def yinluo_convert_sha(self, identity):
        text = await self.send_yinluo_command(identity, YINLUO_CONVERT_COMMAND, timeout=60, edited_wait=6)
        parsed = parse_yinluo_convert(text)
        state = self.get_yinluo_state(identity)
        if parsed.get("status") == "success":
            state["sha_current"] = int(state.get("sha_current") or 0) + int(parsed.get("sha_gain") or 0)
            self.yinluo_set_status(identity, "converted", f"化功为煞 +{parsed.get('sha_gain')} 煞气", 5, text)
            return True
        if parsed.get("status") == "pending":
            self.yinluo_set_status(identity, "convert_pending", "等待化功为煞结算", 60, text)
            return True
        if parsed.get("status") in ("cooldown", "failed", "blocked"):
            wait = max(60, int(parsed.get("cooldown_seconds") or YINLUO_CONVERT_FAILURE_RETRY_SECONDS))
            self.yinluo_set_status(identity, "convert_failed", f"化功为煞失败，{wait}秒后重试", wait, text)
            return False
        self.yinluo_set_status(identity, "convert_failed", "化功为煞回复未识别", YINLUO_CONVERT_FAILURE_RETRY_SECONDS, text)
        return False

    async def yinluo_imprison_fierce_soul(self, identity, preferred_slots=None):
        state = self.get_yinluo_state(identity)
        slots = self.yinluo_empty_slots(identity)
        preferred = [int(slot) for slot in (preferred_slots or [])]
        if preferred:
            slots = [slot for slot in preferred if slot in slots] + [
                slot for slot in slots if slot not in preferred
            ]
        if not slots:
            return False
        if int(state.get("reserves", {}).get(YINLUO_SOUL, 0)) <= 0:
            return False
        if int(state.get("sha_current") or 0) < YINLUO_REFINE_COST_SHA:
            if not await self.yinluo_convert_sha(identity):
                return False
            state = self.get_yinluo_state(identity)
            if int(state.get("sha_current") or 0) < YINLUO_REFINE_COST_SHA:
                return False

        attempted_slots = set()
        synced_after_busy = False
        while True:
            slots = [slot for slot in self.yinluo_empty_slots(identity) if slot not in attempted_slots]
            if preferred:
                slots = [slot for slot in preferred if slot in slots] + [
                    slot for slot in slots if slot not in preferred
                ]
            if not slots:
                self.yinluo_set_status(identity, "no_empty_slot_after_sync", "未解析到可囚禁的空闲炼化槽", YINLUO_SYNC_SECONDS)
                return False

            slot = slots[0]
            attempted_slots.add(slot)
            text = await self.send_yinluo_command(identity, f".囚禁魂魄 {slot} {YINLUO_SOUL}", timeout=60)
            parsed = parse_yinluo_imprison(text)
            state = self.get_yinluo_state(identity)
            if parsed.get("status") == "success":
                state["sha_current"] = max(0, int(state.get("sha_current") or 0) - YINLUO_REFINE_COST_SHA)
                reserves = state.setdefault("reserves", {})
                reserves[YINLUO_SOUL] = max(0, int(reserves.get(YINLUO_SOUL, 0)) - 1)
                self.yinluo_replace_slot(identity, slot, {
                    "status": "炼化中",
                    "soul": YINLUO_SOUL,
                    "remaining_seconds": 12 * 3600,
                    "remaining_text": "",
                    "due_at": add_seconds_str(now_str(), 12 * 3600),
                })
                self.yinluo_set_status(identity, "imprisoned", f"{slot}号槽囚禁 {YINLUO_SOUL}", 5, text)
                return True
            if parsed.get("status") == "slot_busy":
                slots_state = state.setdefault("slots", {})
                slots_state[slot] = {
                    "status": "状态待同步",
                    "soul": "",
                    "remaining_seconds": 0,
                    "remaining_text": "",
                    "due_at": "",
                }
                state["imprison_sync_pending"] = True
                state["next_sync_at"] = ""
                self.yinluo_set_status(identity, "slot_busy_syncing", f"{slot}号槽正在运转，重新同步阴罗幡", 5, text)
                if synced_after_busy:
                    return False
                synced_after_busy = True
                if not await self.yinluo_sync_banner(identity):
                    return False
                state = self.get_yinluo_state(identity)
                state["imprison_sync_pending"] = False
                self.save_state()
                continue
            if parsed.get("status") == "sha_not_enough":
                await self.yinluo_convert_sha(identity)
                return False
            self.yinluo_set_status(identity, "imprison_failed", "囚禁魂魄失败", YINLUO_RETRY_SECONDS, text)
            return False

    async def yinluo_collect_essence(self, identity, slot):
        slot = int(slot)
        item = self.yinluo_slot_item(identity, slot)
        if (
            item.get("status") != "精华已成"
            or str(item.get("soul") or "").strip() != YINLUO_SOUL
        ):
            return True
        text = await self.send_yinluo_command(
            identity, f"{YINLUO_COLLECT_COMMAND} {slot}", timeout=60
        )
        parsed = parse_yinluo_collect(text)
        state = self.get_yinluo_state(identity)
        if parsed.get("status") == "success":
            state["last_collected_at"] = now_str()
            self.yinluo_replace_slot(identity, slot, {
                "status": "魂力枯竭",
                "soul": "",
                "remaining_seconds": 0,
                "remaining_text": "",
                "due_at": "",
            })
            state["next_sync_at"] = ""
            self.yinluo_set_status(
                identity,
                "collected",
                f"已收取 {slot}号槽 {YINLUO_SOUL} 精华",
                5,
                text,
            )
            return True
        if parsed.get("status") == "empty":
            self.yinluo_replace_slot(identity, slot, {
                "status": "状态待同步",
                "soul": "",
                "remaining_seconds": 0,
                "remaining_text": "",
                "due_at": "",
            })
            self.yinluo_set_status(
                identity,
                "collect_empty",
                f"{slot}号槽暂无可收取精华",
                5,
                text,
            )
            return True
        self.yinluo_set_status(
            identity,
            "collect_failed",
            f"收取 {slot}号槽精华回复未识别",
            YINLUO_RETRY_SECONDS,
            text,
        )
        return False

    async def yinluo_appease_all(self, identity, force=False):
        if force:
            target_slots = sorted(
                int(slot)
                for slot, item in (self.get_yinluo_state(identity).get("slots") or {}).items()
                if isinstance(item, dict) and "魂力枯竭" in str(item.get("status") or "")
            )
        else:
            target_slots = self.yinluo_exhausted_slots(identity)
        if not target_slots and not force:
            return True
        text = await self.send_yinluo_command(identity, YINLUO_APPEASE_COMMAND, timeout=60)
        parsed = parse_yinluo_appease(text)
        if parsed.get("matched"):
            count = int(parsed.get("count") or 0)
            state = self.get_yinluo_state(identity)
            state["next_sync_at"] = ""
            suppressed = state.setdefault("appease_suppressed_until", {})
            if count > 0:
                for target_slot in target_slots:
                    self.yinluo_replace_slot(identity, target_slot, {
                        "status": "空闲",
                        "soul": "",
                        "remaining_seconds": 0,
                        "remaining_text": "",
                        "due_at": "",
                    })
                    suppressed.pop(str(target_slot), None)
                    suppressed.pop(target_slot, None)
                self.yinluo_set_status(identity, "appeased", f"一键安抚 {count} 个炼化槽成功", 5, text)
            else:
                for target_slot in target_slots:
                    suppressed[str(target_slot)] = add_seconds_str(now_str(), YINLUO_APPEASE_NOOP_SUPPRESS_SECONDS)
                detail = f"炼化槽无需安抚，{YINLUO_APPEASE_NOOP_SUPPRESS_SECONDS // 60}分钟内不重复"
                self.yinluo_set_status(identity, "appease_noop", detail, 5, text)
            return True
        self.yinluo_set_status(identity, "appease_failed", "一键安抚幡灵失败", YINLUO_RETRY_SECONDS, text)
        return False

    async def yinluo_appease_slot(self, identity, slot):
        return await self.yinluo_appease_all(identity, force=True)

    async def yinluo_run_post_summon_flow(self, identity):
        """Resume the fixed Mini App refinery chain after a successful summon."""
        async with _YinluoAtomicTask(self, f"YinluoPostSummon-{identity}"):
            while True:
                state = self.get_yinluo_state(identity)
                stage = str(state.get("post_summon_stage") or "").strip()
                if not stage:
                    return True

                if stage == "sync":
                    if not await self.yinluo_sync_banner(identity):
                        return False
                    state = self.get_yinluo_state(identity)
                    state["post_summon_target_slots"] = self.yinluo_completed_fierce_slots(identity)
                    state["post_summon_stage"] = "collect"
                    self.save_state()
                    continue

                if stage == "collect":
                    targets = [
                        int(slot)
                        for slot in (state.get("post_summon_target_slots") or [])
                    ]
                    for slot in targets:
                        item = self.yinluo_slot_item(identity, slot)
                        if (
                            item.get("status") != "精华已成"
                            or str(item.get("soul") or "").strip() != YINLUO_SOUL
                        ):
                            continue
                        if not await self.yinluo_collect_essence(identity, slot):
                            return False
                    state = self.get_yinluo_state(identity)
                    state["post_summon_stage"] = "appease"
                    self.save_state()
                    continue

                if stage == "appease":
                    if not await self.yinluo_appease_all(identity, force=True):
                        return False
                    state = self.get_yinluo_state(identity)
                    state["post_summon_stage"] = "imprison"
                    self.save_state()
                    continue

                if stage == "imprison":
                    preferred = [
                        int(slot)
                        for slot in (state.get("post_summon_target_slots") or [])
                    ]
                    while (
                        self.yinluo_empty_slots(identity)
                        and int(
                            self.get_yinluo_state(identity)
                            .get("reserves", {})
                            .get(YINLUO_SOUL, 0)
                        ) > 0
                    ):
                        if not await self.yinluo_imprison_fierce_soul(
                            identity, preferred_slots=preferred
                        ):
                            return False
                    state = self.get_yinluo_state(identity)
                    state["post_summon_stage"] = ""
                    state["post_summon_target_slots"] = []
                    state["post_summon_started_at"] = ""
                    state["imprison_sync_pending"] = False
                    state["next_action_at"] = ""
                    state["last_status"] = "summon_flow_complete"
                    state["last_detail"] = "召唤后的收取、安抚与囚禁流程已完成"
                    self.save_state()
                    return True

                state["post_summon_stage"] = "sync"
                self.save_state()

    async def yinluo_tick(self, identity=None):
        expected_identity = self.yinluo_identity_name()
        identity = str(identity or expected_identity).strip()
        if identity != expected_identity:
            return 3600
        if self.identity_pause_seconds(identity) > 0:
            return 60

        impending = self.yinluo_impending_wait(identity)
        if 0 < impending <= YINLUO_IMPENDING_GUARD_SECONDS:
            wait = max(10, min(int(impending) + 10, 120))
            self.yinluo_set_status(identity, "yielding", f"让路给 {impending:.0f}秒内到期的其他指令", wait)
            return wait

        state = self.get_yinluo_state(identity)
        if state.get("post_summon_stage"):
            if is_future(state.get("next_action_at", "")):
                return max(
                    10,
                    min(int(seconds_until(state.get("next_action_at", ""))), 600),
                )
            await self.yinluo_run_post_summon_flow(identity)
            return 5

        due_slots = self.yinluo_refining_slots_due(identity)
        if due_slots:
            self.yinluo_set_status(identity, "sync_due_slot", f"{due_slots[0]}号槽炼化到点，重新同步阴罗幡")
            if await self.yinluo_sync_banner(identity):
                completed = self.yinluo_completed_fierce_slots(identity)
                if completed:
                    state = self.get_yinluo_state(identity)
                    state["post_summon_stage"] = "collect"
                    state["post_summon_target_slots"] = completed
                    state["post_summon_started_at"] = now_str()
                    state["next_action_at"] = ""
                    self.save_state()
            return 5

        completed = self.yinluo_completed_fierce_slots(identity)
        if completed:
            state["post_summon_stage"] = "collect"
            state["post_summon_target_slots"] = completed
            state["post_summon_started_at"] = now_str()
            state["next_action_at"] = ""
            self.save_state()
            await self.yinluo_run_post_summon_flow(identity)
            return 5

        exhausted = self.yinluo_exhausted_slots(identity)
        if exhausted:
            await self.yinluo_appease_slot(identity, exhausted[0])
            return 5

        if is_future(state.get("next_action_at", "")):
            return max(30, min(int(seconds_until(state.get("next_action_at", ""))), 3600))

        if state.get("last_daily_sacrifice_date") != _today() and not is_future(state.get("next_daily_sacrifice_time", "")):
            await self.yinluo_daily_sacrifice(identity)
            return 5

        if self.yinluo_empty_slots(identity) and int(state.get("reserves", {}).get(YINLUO_SOUL, 0)) > 0:
            await self.yinluo_imprison_fierce_soul(identity)
            return 5

        if not is_future(state.get("next_summon_shadow_time", "")):
            await self.yinluo_summon_shadow(identity)
            return 5

        if not is_future(state.get("next_blood_wash_time", "")):
            await self.yinluo_blood_wash(identity)
            return 5

        scheduled_wait = self.yinluo_schedule_next_action(identity)
        wait = scheduled_wait if scheduled_wait is not None else self.yinluo_wait_from_state(identity, YINLUO_SYNC_SECONDS)
        return max(30, min(int(wait), 3600))

    async def run_yinluo_loop(self, identity=None, initial_delay=0):
        identity = str(identity or self.yinluo_identity_name()).strip()
        await self.startup_done.wait()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        log = self.yinluo_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                wait_seconds = await self.yinluo_tick(identity)
                if log:
                    log.info(f"Yinluo loop [{identity}] sleeping {int(wait_seconds)}s.")
                await asyncio.sleep(max(1, min(int(wait_seconds), 300)))
            except Exception as exc:
                if log:
                    log.error(f"Yinluo loop [{identity}] error: {exc}", exc_info=True)
                await asyncio.sleep(YINLUO_RETRY_SECONDS)
