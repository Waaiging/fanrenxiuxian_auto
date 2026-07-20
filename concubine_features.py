"""
【侍妾功能模块 —— 所有账号脚本共享】

提供 ConcubineMixin 混入类，封装所有与侍妾（道侣）相关的操作：
  1. 入梦寻图 —— 虚天残图/苍坤残图线索收集
  2. 共历心劫 —— 三轮抉择（稳/狠/骗），侍妾连携
  3. 天机代卜 —— 侍妾代卜避劫，提升闭关收益

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 继承使用。
"""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta

from log_utils import (
    actor_account_key,            # 当前脚本账号 key
    command_send_allowed,          # 指令频率守卫
    log_incoming_message,          # 记录收到的消息
    notify_unrecognized_response,  # 无法识别的回复告警
    record_bot_no_response,        # 记录机器人无响应
    record_bot_response,           # 记录机器人有响应
    record_command_sent,           # 指令台账
    remember_script_send_intent,   # 记录脚本即将发送
    remember_script_sent_message,  # 记录脚本已发送
    schedule_command_auto_delete,  # 安排自动删除
    wait_for_bot_activity_before_send,  # 等待机器人活跃
)


log = logging.getLogger("Concubine")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONCUBINE_GRACE_SECONDS = 60  # 冷却宽容时间，避免频繁请求
CONCUBINE_VOYAGE_COMMAND = ".侍妾远航 冒险"
MAIN_SOUL_CONCUBINE_VOYAGE_COMMAND = ".侍妾远航 月殿寻痕"
CONCUBINE_VOYAGE_RETURN_COMMAND = ".远航归来"
CONCUBINE_VOYAGE_CD_SECONDS = 12 * 3600
MAIN_SOUL_CONCUBINE_VOYAGE_CD_SECONDS = 6 * 3600
CONCUBINE_VOYAGE_AUTO_START_ENABLED = True
CONCUBINE_CHAIN_TASK_KEYS = ("divination", "dream", "heart_trial", "voyage")
CONCUBINE_PRE_VOYAGE_TASK_KEYS = ("divination", "dream", "heart_trial")
TARGET_CONCUBINE_NAME = "南宫婉"
CONCUBINE_SEARCH_COMMAND = ".红尘寻缘"
CONCUBINE_DISMISS_COMMAND = ".遣散侍妾"
CONCUBINE_SEARCH_CD_SECONDS = 2 * 3600
CONCUBINE_SEARCH_RETRY_SECONDS = 10 * 60
CONCUBINE_SEARCH_EDIT_WAIT_SECONDS = 25
# 红尘寻缘已无法通过“非目标即遣散”的方式稳定寻找南宫婉；默认关闭整条目标侍妾搜索链。
TARGET_CONCUBINE_SEARCH_ENABLED = False
TARGET_CONCUBINE_IDENTITIES = {
    "main": {"主魂", "无咎子"},
}
TARGET_CONCUBINE_STATE_DEFAULTS = {
    "target_concubine_name": "",
    "target_concubine_found": False,
    "next_concubine_search_time": "",
    "last_concubine_search_time": "",
    "last_concubine_search_result": "",
    "last_concubine_dismiss_time": "",
    "last_concubine_dismissed_name": "",
    "last_concubine_search_error": "",
}
DEFAULT_CONCUBINE_NAMES = {
    "main": {
        "主魂": {"慕沛灵"},
        "无咎子": {"冰魄仙子"},
        "缘生子": {"瑶光"},
        "素缘子": {"元瑶"},
    },
    "sub": {
        "主魂": {"瑶光"},
        "厚土": {"霓裳"},
        "缘生子": {"元瑶"},
        "寻真子": {"若兰"},
    },
    "xiaohao": {
        "主魂": {"洛神"},
        "问心子": {"墨彩环"},
        "素心子": {"洛神"},
        "缘生子": {"夜姬"},
    },
}
STAR_CONCUBINE_VOYAGE_IDENTITIES = {
    "main": {"素缘子"},
    "sub": {"厚土", "缘生子", "寻真子"},
    "xiaohao": {"素心子", "缘生子"},
}

# =====================================================================
# 侍妾神通配置
# 每个任务包含：显示名称、指令、状态键、冷却时间(秒)、状态标签
# =====================================================================
CONCUBINE_TASKS = {
    "dream": {
        "label": "入梦寻图",
        "command": ".入梦寻图",
        "state_key": "next_dream_map_time",
        "cooldown": 480 * 60,             # 8小时
        "status_label": "入梦寻图冷却",
    },
    "heart_trial": {
        "label": "共历心劫",
        "command": ".共历心劫",
        "state_key": "next_heart_trial_time",
        "cooldown": 600 * 60,             # 10小时
        "status_label": "共历心劫冷却",
    },
    "divination": {
        "label": "天机代卜",
        "command": ".天机代卜",
        "state_key": "next_divination_time",
        "cooldown": 720 * 60,             # 12小时
        "status_label": "天机代卜冷却",
    },
    "voyage": {
        "label": "侍妾远航",
        "command": CONCUBINE_VOYAGE_COMMAND,
        "settle_command": CONCUBINE_VOYAGE_RETURN_COMMAND,
        "state_key": "next_concubine_voyage_time",
        "cooldown": CONCUBINE_VOYAGE_CD_SECONDS,
        "status_label": "侍妾远航冷却",
        "status_aliases": ("远航冷却", "远航归来冷却", "远航剩余", "航行冷却"),
    },
}


# =====================================================================
# 时间工具函数（与主脚本保持一致）
# =====================================================================

def dt_to_str(dt):
    return dt.strftime(TIME_FORMAT)

def now_str():
    return dt_to_str(datetime.now())

def str_to_dt(value):
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except Exception:
        return datetime.now()

def is_future(value):
    try:
        return str_to_dt(value) > datetime.now()
    except Exception:
        return False

def add_seconds_str(value, seconds):
    return dt_to_str(str_to_dt(value) + timedelta(seconds=seconds))

def seconds_until(value):
    try:
        return max(0, (str_to_dt(value) - datetime.now()).total_seconds())
    except Exception:
        return 0

def parse_duration_seconds(text):
    """解析游戏回复中的时间文本（如"13分钟26秒"）为秒数"""
    if not text:
        return -1
    clean = text.replace("**", "").replace(" ", "")
    h = re.search(r"(\d+)(?:小时|h)", clean)
    m = re.search(r"(\d+)(?:分钟|分|m)", clean)
    s = re.search(r"(\d+)(?:秒|s)", clean)
    total = 0
    found = False
    if h:
        total += int(h.group(1)) * 3600
        found = True
    if m:
        total += int(m.group(1)) * 60
        found = True
    if s:
        total += int(s.group(1))
        found = True
    return total if found else -1


# =====================================================================
# 默认状态数据
# =====================================================================

def concubine_default_state():
    """返回侍妾功能的默认状态字典"""
    return {
        "last_concubine_status_time": "",
        "next_dream_map_time": "",
        "next_heart_trial_time": "",
        "next_divination_time": "",
        "next_concubine_voyage_time": "",
        "last_concubine_voyage_time": "",
        "concubine_voyage_active": False,
        "last_concubine_voyage_error": "",
        "last_concubine_voyage_error_time": "",
        "concubine_name": "",
        "last_concubine_name_time": "",
        "last_concubine_status_mismatch": "",
        "last_concubine_status_mismatch_time": "",
        **TARGET_CONCUBINE_STATE_DEFAULTS,
    }


class _ConcubineAtomicTask:
    """Use the script-level atomic-task gate without importing each script's helper."""

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
        self.actor.active_atomic_task = self.task
        self.actor._concubine_atomic_task = self.task
        self.actor._concubine_atomic_label = self.label
        self.actor._atomic_task_high_priority_bypass_task = self.task
        self.actor._atomic_task_high_priority_bypass_label = self.label
        self.acquired = True
        log_method = log.debug if str(self.label).startswith("ConcubineChain-") else log.info
        log_method(f"Atomic task acquired by {self.label}.")
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
            log_method = log.debug if str(self.label).startswith("ConcubineChain-") else log.info
            log_method(f"Atomic task released by {self.label}.")
        return False


# =====================================================================
# ConcubineMixin 混入类
# 包含所有侍妾相关的方法，通过多重继承混入主 Cultivator 类
# =====================================================================

class ConcubineMixin:
    """侍妾功能混入类，提供侍妾神通的所有操作。"""

    def atomic_task_allows_high_priority_command(self, command):
        """Allow time-critical commands to interrupt a concubine atomic batch."""
        task = getattr(self, "_concubine_atomic_task", None)
        if task is None or task == asyncio.current_task():
            return False
        bypass_task = getattr(self, "_atomic_task_high_priority_bypass_task", None)
        if bypass_task is not task:
            return False
        if not hasattr(self, "time_critical_identity_command"):
            return False
        return self.time_critical_identity_command(command)

    def should_wait_for_atomic_task(self, command=None):
        """Return whether a sender should wait for the current atomic task."""
        current_task = asyncio.current_task()
        active_task = getattr(self, "active_atomic_task", None)
        if active_task is not None and active_task != current_task:
            if not self.atomic_task_allows_high_priority_command(command):
                return True

        concubine_task = getattr(self, "_concubine_atomic_task", None)
        if concubine_task is not None and concubine_task != current_task:
            if not self.atomic_task_allows_high_priority_command(command):
                return True
        return False

    # ---- 状态管理 ----

    def ensure_concubine_state(self):
        """确保状态字典包含所有侍妾默认键"""
        changed = False
        for key, value in concubine_default_state().items():
            if key not in self.state:
                self.state[key] = value
                changed = True
        if self.state.get("last_concubine_status_mismatch") or self.state.get("last_concubine_status_mismatch_time"):
            self.state["last_concubine_status_mismatch"] = ""
            self.state["last_concubine_status_mismatch_time"] = ""
            changed = True
        for avatar_state in (self.state.get("avatars") or {}).values():
            if not isinstance(avatar_state, dict):
                continue
            if avatar_state.get("last_concubine_status_mismatch") or avatar_state.get("last_concubine_status_mismatch_time"):
                avatar_state["last_concubine_status_mismatch"] = ""
                avatar_state["last_concubine_status_mismatch_time"] = ""
                changed = True
        if changed:
            self.save_state()

    def has_concubine_status_cache(self):
        """是否已有侍妾状态缓存"""
        return bool(self.state.get("last_concubine_status_time"))

    def parse_concubine_voyage_status_line(self, text, identity="主魂"):
        """从.我的侍妾状态里同步远航中/可归来状态，返回远航阻塞结束时间。"""
        clean = str(text or "").replace("**", "")
        if not self.concubine_status_matches_identity(clean, identity):
            return ""
        match = re.search(r"远航状态\s*[：:]\s*([^\n]+)", clean)
        if not match:
            return ""

        value = match.group(1).strip()
        now = now_str()
        if any(k in value for k in ["可归来", "可结算", "已归来", "等待归来"]):
            self._update_concubine_identity_state(
                identity,
                concubine_voyage_active=True,
                next_concubine_voyage_time="",
                last_concubine_voyage_error="",
                last_concubine_voyage_error_time="",
            )
            log.info(f"Concubine voyage [{identity}]: status ready to return.")
            return ""

        if any(k in value for k in ["进行中", "远航中", "航线", "剩余", "尚未归来", "仍在远航", "还在远航"]):
            cd = parse_duration_seconds(value)
            wait_seconds = cd + CONCUBINE_GRACE_SECONDS if cd > 0 else 1800
            block_until = add_seconds_str(now, wait_seconds)
            self.bind_concubine_chain_to_time(identity, block_until, active=True)
            log.info(f"Concubine voyage [{identity}]: status active, block concubine tasks until {block_until}.")
            return block_until

        if any(k in value for k in ["无", "未远航", "没有正在远航", "并未远航"]):
            self._update_concubine_identity_state(
                identity,
                concubine_voyage_active=False,
                next_concubine_voyage_time="",
                last_concubine_voyage_error="",
                last_concubine_voyage_error_time="",
            )
        return ""

    def concubine_voyage_block_until(self, identity="主魂"):
        """远航中会阻塞入梦寻图/共历心劫/天机代卜。"""
        if not self.concubine_voyage_enabled(identity):
            return ""
        state = self._concubine_state_container(identity or "主魂")
        next_time = state.get("next_concubine_voyage_time", "")
        if state.get("concubine_voyage_active") and next_time and is_future(next_time):
            return next_time
        return ""

    def bind_concubine_chain_to_time(self, identity="主魂", target_time="", active=None, include_voyage=True):
        """Bind divination, dream map, heart trial, and voyage to the same chain time."""
        if not target_time:
            return ""
        updates = {}
        for task_key in CONCUBINE_PRE_VOYAGE_TASK_KEYS:
            task = CONCUBINE_TASKS[task_key]
            if not self._concubine_command_paused(task["command"], identity):
                updates[task["state_key"]] = target_time
        if include_voyage and self.concubine_voyage_enabled(identity):
            updates[CONCUBINE_TASKS["voyage"]["state_key"]] = target_time
        if active is not None:
            updates["concubine_voyage_active"] = bool(active)
        if updates:
            self._update_concubine_identity_state(identity or "主魂", **updates)
        return target_time

    def latest_concubine_chain_time(self, identity="主魂"):
        """Return the voyage blocker; pre-voyage cooldowns must not block voyage start."""
        identity = identity or "主魂"
        state = self._concubine_state_container(identity)
        voyage_time = state.get(CONCUBINE_TASKS["voyage"]["state_key"], "")
        if voyage_time and is_future(voyage_time):
            return voyage_time
        return ""

    def align_concubine_chain_cooldowns(self, identity="主魂"):
        """
        Active voyage blocks .天机代卜/.入梦寻图/.共历心劫.

        Pre-voyage cooldowns do not block .侍妾远航; if a step is still cooling
        down, skip that step and still try to start voyage when voyage itself is due.
        """
        identity = identity or "主魂"
        voyage_block_until = self.concubine_voyage_block_until(identity)
        if not voyage_block_until:
            return True
        updates = {}
        for task_key in CONCUBINE_PRE_VOYAGE_TASK_KEYS:
            task = CONCUBINE_TASKS[task_key]
            if self._concubine_command_paused(task["command"], identity):
                continue
            updates[task["state_key"]] = voyage_block_until
        updates["concubine_voyage_active"] = True
        if updates:
            self._update_concubine_identity_state(identity, **updates)
            log.info(f"Concubine chain [{identity}]: active voyage blocks pre-voyage tasks until {voyage_block_until}.")
        return False

    def defer_concubine_task_until_voyage(self, task_key, identity="主魂", fallback_seconds=1800):
        task = CONCUBINE_TASKS[task_key]
        block_until = self.concubine_voyage_block_until(identity)
        if not block_until:
            block_until = add_seconds_str(now_str(), fallback_seconds)
        self._update_concubine_identity_state(identity or "主魂", **{task["state_key"]: block_until})
        log.info(f"Concubine {task['label']} [{identity or '主魂'}]: blocked by active voyage until {block_until}.")
        return block_until

    def concubine_response_indicates_active_voyage(self, text):
        clean = str(text or "").replace("**", "")
        return any(k in clean for k in ["仍在远航", "还在远航", "尚在远航", "正在远航", "远航途中"])

    def parse_concubine_status(self, text):
        """
        解析.我的侍妾回复，提取各神通的冷却时间。
        冷却已过的任务标记为就绪（state_key 置空），
        有冷却的标记为等待时间。
        """
        if not text:
            return False
        updated = False
        clean = text.replace("**", "")
        if not self.concubine_status_matches_identity(clean, "主魂"):
            return False
        voyage_block_until = self.parse_concubine_voyage_status_line(clean, "主魂")
        if voyage_block_until:
            updated = True
        for task in CONCUBINE_TASKS.values():
            if task["state_key"] == "next_concubine_voyage_time" and not self.concubine_voyage_enabled("主魂"):
                continue
            labels = [task["status_label"], *task.get("status_aliases", ())]
            line_match = None
            matched_label = ""
            for label in labels:
                line_match = re.search(rf"{re.escape(label)}\s*[：:]\s*([^\n]+)", clean)
                if line_match:
                    matched_label = label
                    break
            if not line_match:
                continue
            value = line_match.group(1).strip()
            if any(k in value for k in ["无", "可用", "可施展", "已就绪", "可归来", "可结算"]):
                if (
                    voyage_block_until
                    and task["state_key"] in {"next_dream_map_time", "next_heart_trial_time", "next_divination_time"}
                ):
                    self.state[task["state_key"]] = voyage_block_until
                else:
                    self.state[task["state_key"]] = ""
                if task["state_key"] == "next_concubine_voyage_time":
                    self.state["concubine_voyage_active"] = (
                        "归来" in matched_label
                        or any(k in value for k in ["可归来", "可结算"])
                    )
                updated = True
                continue
            cd = parse_duration_seconds(value)
            if cd == 0:
                self.state[task["state_key"]] = ""
                if task["state_key"] == "next_concubine_voyage_time":
                    self.state["concubine_voyage_active"] = "归来" in matched_label
                updated = True
                continue
            if cd > 0:
                next_time = add_seconds_str(now_str(), cd + CONCUBINE_GRACE_SECONDS)
                self.state[task["state_key"]] = next_time
                if task["state_key"] == "next_concubine_voyage_time":
                    self.state["concubine_voyage_active"] = True
                    self.bind_concubine_chain_to_time("主魂", next_time, active=True)
                updated = True
        if updated:
            self.state["last_concubine_status_time"] = now_str()
            self.save_state()
        return updated

    def record_concubine_cd(self, task_key, response_text=""):
        """
        记录侍妾任务的冷却时间。
        优先从回复文本中解析具体冷却时间，解析失败则使用默认冷却。
        """
        if task_key == "voyage":
            return self.record_concubine_voyage_response(
                response_text,
                identity="主魂",
                command=self.concubine_voyage_command("主魂"),
            )
        if self.concubine_response_indicates_active_voyage(response_text):
            self.defer_concubine_task_until_voyage(task_key, "主魂")
            return
        task = CONCUBINE_TASKS[task_key]
        cd = -1
        if response_text and any(k in response_text for k in ["冷却", "后再", "尚未", "仍在远航", "还在远航", "远航途中"]):
            for line in response_text.replace("**", "").splitlines():
                if task["label"] in line or "冷却" in line or "后再" in line or "远航" in line:
                    cd = parse_duration_seconds(line)
                    if cd > 0:
                        break
        if cd <= 0:
            cd = task["cooldown"]
        self.state[task["state_key"]] = add_seconds_str(now_str(), cd + CONCUBINE_GRACE_SECONDS)
        self.state["last_concubine_status_time"] = now_str()
        self.save_state()
        log.info(f"Concubine {task['label']}: next at {self.state[task['state_key']]}")

    def is_known_concubine_response(self, task_key, text):
        """判断侍妾回复是否可识别（含已知关键词）"""
        if not text:
            return False
        task = CONCUBINE_TASKS[task_key]
        clean = text.replace("**", "")
        known_keywords = [
            task["label"], "冷却", "后再", "尚未",
            "成功", "获得", "完成", "机缘",
            "残图", "入梦", "寻图", "天机", "代卜", "卜算",
            "共历心劫", "心劫", "道侣", "护道",
            "侍妾远航", "远航", "归来", "启航", "返航", "航程", "航海", "冒险",
            "远航途中", "正在远航", "仍在远航", "暂不可", "暂无法",
            "情缘不足", "情缘值不足",
        ]
        return any(k in clean for k in known_keywords)

    def defer_concubine_task(self, task_key, seconds=600):
        """推迟侍妾任务（遇到异常时使用）"""
        task = CONCUBINE_TASKS[task_key]
        self.state[task["state_key"]] = add_seconds_str(now_str(), seconds)
        self.save_state()

    def _concubine_state_container(self, identity="主魂"):
        """Return the state dict for main soul or avatar identity."""
        identity = identity or "主魂"
        if identity != "主魂" and hasattr(self, "get_avatar_state"):
            return self.get_avatar_state(identity)
        return self.state

    def extract_concubine_name(self, text):
        """Extract the concubine/partner name from a status or voyage response."""
        clean = str(text or "").replace("**", "")
        for pattern in (
            r"你的(?:道心侍妾|红尘道侣|红颜知己|红尘知己)\s*[：:]\s*【([^】]+)】",
            r"名为\s*【([^】]+)】",
            r"侍妾\s*【([^】]+)】",
            r"道侣\s*【([^】]+)】",
        ):
            match = re.search(pattern, clean)
            if match:
                return match.group(1).strip()
        return ""

    def expected_concubine_names(self, identity="主魂"):
        identity = identity or "主魂"
        names = set()
        account = actor_account_key(self)
        names.update(DEFAULT_CONCUBINE_NAMES.get(account, {}).get(identity, set()) or set())
        state_name = str(self._concubine_state_container(identity).get("concubine_name", "") or "").strip()
        if state_name:
            names.add(state_name)
        return {name for name in names if name}

    def managed_concubine_identities(self):
        identities = ["主魂"]
        for avatar in getattr(self, "avatars", []) or []:
            if avatar and avatar not in identities:
                identities.append(avatar)
        return identities

    def identity_for_concubine_voyage_text(self, text, fallback_identity=""):
        """Infer the owner of a loose voyage response that was not reply-linked."""
        clean = str(text or "")
        if any(k in clean for k in ["元婴", "元神"]) and not any(
            k in clean for k in ["侍妾", "道侣", "乱星海远航", "远航·"]
        ):
            return ""
        if not clean or not self.is_concubine_voyage_response(clean):
            return ""

        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", clean)
        if marker:
            candidate = marker.group(1).strip()
            if candidate in self.managed_concubine_identities():
                return candidate

        lower = clean.lower()
        for username, avatar in getattr(self, "avatar_usernames", {}).items():
            if f"@{str(username).lower().lstrip('@')}" in lower and avatar in self.managed_concubine_identities():
                return avatar
        for username in getattr(self, "identity_usernames", {}).get("主魂", []):
            if f"@{str(username).lower().lstrip('@')}" in lower:
                return "主魂"

        name = self.extract_concubine_name(clean)
        if name:
            matches = [
                identity
                for identity in self.managed_concubine_identities()
                if name in self.expected_concubine_names(identity)
            ]
            if len(matches) == 1:
                return matches[0]

        active_matches = []
        for identity in self.managed_concubine_identities():
            state = self._concubine_state_container(identity)
            if not state.get("concubine_voyage_active"):
                continue
            next_time = state.get("next_concubine_voyage_time", "")
            if not next_time or seconds_until(next_time) <= CONCUBINE_GRACE_SECONDS:
                active_matches.append(identity)
        if len(active_matches) == 1:
            return active_matches[0]

        fallback_identity = str(fallback_identity or "").strip()
        if fallback_identity in self.managed_concubine_identities():
            return fallback_identity
        return ""

    def record_passive_concubine_voyage_response(self, text, fallback_identity=""):
        identity = self.identity_for_concubine_voyage_text(text, fallback_identity=fallback_identity)
        if not identity:
            return False
        return self.record_concubine_voyage_response(text, identity=identity)

    def concubine_status_trusted_for_identity(self, text, identity="主魂"):
        """Return whether the status text is safely attributable to identity."""
        identity = identity or "主魂"
        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", str(text or ""))
        if not marker:
            return True
        marked_identity = marker.group(1).strip()
        if identity == "主魂":
            return False
        return marked_identity == identity

    def record_concubine_name_from_text(self, identity="主魂", text=""):
        identity = identity or "主魂"
        name = self.extract_concubine_name(text)
        if not name:
            return ""
        state = self._concubine_state_container(identity)
        if not self.concubine_status_trusted_for_identity(text, identity):
            return ""
        state["concubine_name"] = name
        state["last_concubine_name_time"] = now_str()
        state["last_concubine_status_mismatch"] = ""
        state["last_concubine_status_mismatch_time"] = ""
        self.update_target_concubine_state_from_name(identity, name, source="name_sync")
        self.save_state()
        return name

    def target_concubine_name(self, identity="主魂"):
        if not self.target_concubine_search_enabled():
            return ""
        identity = identity or "主魂"
        account = actor_account_key(self)
        if identity in TARGET_CONCUBINE_IDENTITIES.get(account, set()):
            return TARGET_CONCUBINE_NAME
        return ""

    def target_concubine_search_enabled(self):
        return bool(getattr(self, "target_concubine_search_enabled_flag", TARGET_CONCUBINE_SEARCH_ENABLED))

    def target_concubine_identities(self):
        if not self.target_concubine_search_enabled():
            return []
        account = actor_account_key(self)
        configured = TARGET_CONCUBINE_IDENTITIES.get(account, set())
        identities = []
        for identity in ["主魂", *list(getattr(self, "avatars", []) or [])]:
            if identity in configured:
                identities.append(identity)
        return identities

    def target_concubine_enabled(self, identity="主魂"):
        return bool(self.target_concubine_name(identity))

    def ensure_target_concubine_state(self, identity="主魂"):
        state = self._concubine_state_container(identity or "主魂")
        changed = False
        for key, value in TARGET_CONCUBINE_STATE_DEFAULTS.items():
            if key not in state:
                state[key] = value
                changed = True
        target = self.target_concubine_name(identity)
        if target and state.get("target_concubine_name") != target:
            state["target_concubine_name"] = target
            changed = True
        if changed and hasattr(self, "save_state"):
            self.save_state()
        return state

    def target_concubine_found(self, identity="主魂"):
        if not self.target_concubine_enabled(identity):
            return True
        state = self.ensure_target_concubine_state(identity)
        target = self.target_concubine_name(identity)
        return bool(state.get("target_concubine_found") and state.get("concubine_name") == target)

    def update_target_concubine_state_from_name(self, identity="主魂", name="", source=""):
        if not self.target_concubine_enabled(identity):
            return False
        state = self.ensure_target_concubine_state(identity)
        target = self.target_concubine_name(identity)
        name = str(name or "").strip()
        if not name:
            state["concubine_name"] = ""
            state["target_concubine_found"] = False
            state["last_concubine_search_result"] = source or "empty"
            return True
        state["concubine_name"] = name
        state["last_concubine_name_time"] = now_str()
        state["target_concubine_found"] = (name == target)
        state["last_concubine_search_result"] = f"{source}:{name}" if source else name
        if name == target:
            state["next_concubine_search_time"] = ""
            state["last_concubine_search_error"] = ""
            log.info(f"Target concubine [{identity}] found: {target}.")
        else:
            log.info(f"Target concubine [{identity}] not matched: got {name}, want {target}.")
        return True

    def record_target_concubine_status_text(self, identity="主魂", text="", source="status"):
        if not self.target_concubine_enabled(identity):
            return False
        if not self.concubine_status_trusted_for_identity(text, identity):
            return False
        clean = str(text or "").replace("**", "")
        state = self.ensure_target_concubine_state(identity)
        name = self.extract_concubine_name(clean)
        if name:
            changed = self.update_target_concubine_state_from_name(identity, name, source=source)
            if changed and hasattr(self, "save_state"):
                self.save_state()
            return changed
        if any(k in clean for k in ["还没有侍妾", "尚无侍妾", "没有侍妾", "暂无侍妾"]):
            state["concubine_name"] = ""
            state["target_concubine_found"] = False
            state["last_concubine_search_result"] = f"{source}:none"
            state["last_concubine_search_error"] = ""
            if hasattr(self, "save_state"):
                self.save_state()
            return True
        return False

    def concubine_status_matches_identity(self, text, identity="主魂"):
        """Reject only explicit identity-marker mismatches; do not validate concubine names."""
        clean = str(text or "")
        if not any(k in clean for k in ["你的道心侍妾", "你的红尘道侣", "【第二期机缘】", "远航状态"]):
            return True
        marker = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", clean)
        if marker and marker.group(1).strip() != (identity or "主魂"):
            return False
        self.record_concubine_name_from_text(identity, clean)
        return True

    def recent_concubine_status_mismatch(self, identity="主魂", window_seconds=120):
        return False

    def concubine_voyage_enabled(self, identity="主魂"):
        """Only the main account's main soul may use concubine voyage."""
        account = actor_account_key(self) or str(getattr(self, "account_key", "") or "")
        return account == "main" and (identity or "主魂") == "主魂"

    def concubine_voyage_command(self, identity="主魂"):
        """Return the route command for an account/identity pair."""
        if self.concubine_voyage_enabled(identity):
            return MAIN_SOUL_CONCUBINE_VOYAGE_COMMAND
        return CONCUBINE_VOYAGE_COMMAND

    def concubine_voyage_cooldown(self, identity="主魂"):
        """Return the cooldown for the selected route."""
        if self.concubine_voyage_command(identity) == MAIN_SOUL_CONCUBINE_VOYAGE_COMMAND:
            return MAIN_SOUL_CONCUBINE_VOYAGE_CD_SECONDS
        return CONCUBINE_VOYAGE_CD_SECONDS

    def concubine_voyage_auto_start_enabled(self, identity="主魂"):
        return CONCUBINE_VOYAGE_AUTO_START_ENABLED and self.concubine_voyage_enabled(identity)

    def concubine_task_enabled(self, task_key, identity="主魂"):
        if task_key != "voyage":
            return True
        if not self.concubine_voyage_enabled(identity):
            return False
        if self.concubine_voyage_auto_start_enabled(identity):
            return True
        state = self._concubine_state_container(identity)
        return bool(state.get("concubine_voyage_active"))

    def _concubine_command_paused(self, command, identity="主魂"):
        return hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(command, identity or "主魂")

    @staticmethod
    def _concubine_response_text(response):
        if hasattr(response, "text"):
            return response.text or ""
        if isinstance(response, str):
            return response
        return str(response) if response else ""

    def _set_concubine_search_backoff(self, identity="主魂", seconds=CONCUBINE_SEARCH_RETRY_SECONDS, reason=""):
        if not self.target_concubine_enabled(identity):
            return ""
        state = self.ensure_target_concubine_state(identity)
        next_time = add_seconds_str(now_str(), max(0, int(seconds or 0)))
        state["next_concubine_search_time"] = next_time
        if reason:
            state["last_concubine_search_error"] = reason[:160]
        if hasattr(self, "save_state"):
            self.save_state()
        return next_time

    def is_concubine_search_pending_response(self, text):
        clean = str(text or "").replace("**", "").replace("`", "")
        return "寻缘之旅" in clean and any(k in clean for k in ["开启", "消耗", "红尘俗世"])

    def is_concubine_search_final_response(self, text):
        clean = str(text or "").replace("**", "")
        if not clean or self.is_concubine_search_pending_response(clean):
            return False
        compact = clean.replace("`", "").replace(" ", "")
        if self.extract_concubine_name(clean):
            return True
        if (
            any(k in clean for k in ["红颜知己", "红尘知己", "三心二意"])
            and CONCUBINE_DISMISS_COMMAND in compact
        ):
            return True
        return any(k in clean for k in [
            "未能寻得", "没有寻得", "无缘之人", "镜花水月",
            "冷却", "后再", "尚未", "神念消耗过剧", "近日奔波",
            "灵石不足", "修为不足", "无法寻缘", "失败", "错误",
        ])

    async def wait_for_concubine_search_settlement(self, resp, identity="主魂", timeout_seconds=CONCUBINE_SEARCH_EDIT_WAIT_SECONDS):
        text = self._concubine_response_text(resp)
        if not self.is_concubine_search_pending_response(text):
            return resp

        msg_id = getattr(resp, "id", None)
        client = getattr(self, "client", None)
        chat_id = getattr(self, "target_chat_id", None)
        if not msg_id or not client or chat_id is None:
            log.warning(f"Target concubine [{identity}]: search pending response has no fetchable message id.")
            return resp

        deadline = time.monotonic() + max(1, int(timeout_seconds or 0))
        last_text = text
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            try:
                updated = await client.get_messages(chat_id, ids=msg_id)
            except Exception as exc:
                log.warning(f"Target concubine [{identity}]: edited-result fetch failed for {msg_id}: {exc}")
                return resp
            updated_text = self._concubine_response_text(updated)
            if self.is_concubine_search_final_response(updated_text):
                if updated_text and updated_text != last_text:
                    log.info(f"Target concubine [{identity}]: edited search result observed for msg {msg_id}.")
                return updated
            last_text = updated_text or last_text

        log.warning(f"Target concubine [{identity}]: search result was not edited within {timeout_seconds}s; retry later.")
        return resp

    def record_concubine_search_response(self, text, identity="主魂", source=""):
        """Record .红尘寻缘 result and update target-concubine search state."""
        if not self.target_concubine_enabled(identity):
            return False
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        if not self.concubine_status_trusted_for_identity(clean, identity):
            return False

        state = self.ensure_target_concubine_state(identity)
        now = now_str()
        state["last_concubine_search_time"] = now
        state["last_concubine_search_error"] = ""

        if self.is_concubine_search_pending_response(clean):
            state["target_concubine_found"] = False
            state["next_concubine_search_time"] = add_seconds_str(now, CONCUBINE_SEARCH_RETRY_SECONDS)
            state["last_concubine_search_result"] = source or "pending"
            state["last_concubine_search_error"] = clean[:160]
            if hasattr(self, "save_state"):
                self.save_state()
            return True

        name = self.extract_concubine_name(clean)
        if name:
            self.update_target_concubine_state_from_name(identity, name, source=source or CONCUBINE_SEARCH_COMMAND)
            if name == self.target_concubine_name(identity):
                state["next_concubine_search_time"] = ""
            else:
                state["next_concubine_search_time"] = add_seconds_str(now, CONCUBINE_SEARCH_CD_SECONDS)
            if hasattr(self, "save_state"):
                self.save_state()
            return True

        compact = clean.replace("`", "").replace(" ", "")
        if (
            any(k in clean for k in ["红颜知己", "红尘知己", "三心二意"])
            and CONCUBINE_DISMISS_COMMAND in compact
        ):
            current = str(state.get("concubine_name") or "").strip() or "红颜知己"
            state["concubine_name"] = current
            state["target_concubine_found"] = False
            state["next_concubine_search_time"] = ""
            state["last_concubine_search_result"] = f"{source or CONCUBINE_SEARCH_COMMAND}:existing_partner"
            state["last_concubine_search_error"] = clean[:160]
            if hasattr(self, "save_state"):
                self.save_state()
            return True

        if any(k in clean for k in ["未能寻得", "没有寻得", "无缘之人", "镜花水月"]):
            state["concubine_name"] = ""
            state["target_concubine_found"] = False
            state["next_concubine_search_time"] = add_seconds_str(now, CONCUBINE_SEARCH_CD_SECONDS)
            state["last_concubine_search_result"] = source or "no_match"
            if hasattr(self, "save_state"):
                self.save_state()
            return True

        if any(k in clean for k in ["冷却", "后再", "尚未", "神念消耗过剧", "近日奔波"]):
            cd = parse_duration_seconds(clean)
            wait = cd + CONCUBINE_GRACE_SECONDS if cd > 0 else CONCUBINE_SEARCH_CD_SECONDS
            state["next_concubine_search_time"] = add_seconds_str(now, wait)
            state["target_concubine_found"] = False
            state["last_concubine_search_result"] = source or "cooldown"
            state["last_concubine_search_error"] = clean[:160]
            if hasattr(self, "save_state"):
                self.save_state()
            return True

        if any(k in clean for k in ["灵石不足", "修为不足", "无法寻缘", "失败", "错误"]):
            state["next_concubine_search_time"] = add_seconds_str(now, CONCUBINE_SEARCH_RETRY_SECONDS)
            state["target_concubine_found"] = False
            state["last_concubine_search_result"] = source or "failed"
            state["last_concubine_search_error"] = clean[:160]
            if hasattr(self, "save_state"):
                self.save_state()
            return True
        return False

    def record_concubine_dismiss_response(self, text, identity="主魂", source=""):
        """Record .遣散侍妾 result without clearing the red-dust search cooldown."""
        if not self.target_concubine_enabled(identity):
            return False
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        if not self.concubine_status_trusted_for_identity(clean, identity):
            return False

        state = self.ensure_target_concubine_state(identity)
        dismissed = ""
        match = re.search(r"你与\s*([^，,\n]+?)\s*缘分已尽", clean)
        if match:
            dismissed = match.group(1).strip(" 【】")

        if dismissed or any(k in clean for k in ["缘分已尽", "可以再次", "没有侍妾", "尚无侍妾", "不可遣散"]):
            if dismissed:
                state["last_concubine_dismissed_name"] = dismissed
            state["last_concubine_dismiss_time"] = now_str()
            if not any(k in clean for k in ["不可遣散", "无法遣散"]):
                state["concubine_name"] = ""
                state["target_concubine_found"] = False
            state["last_concubine_search_result"] = source or CONCUBINE_DISMISS_COMMAND
            state["last_concubine_search_error"] = "" if dismissed else clean[:160]
            if hasattr(self, "save_state"):
                self.save_state()
            return True
        return False

    async def _send_target_concubine_command(self, identity, command, **kwargs):
        """Send a target-concubine command for main soul or avatar."""
        return await self._send_concubine_identity_command(identity, command, **kwargs)

    async def dismiss_current_concubine_if_needed(self, identity="主魂"):
        if not self.target_concubine_enabled(identity):
            return True
        state = self.ensure_target_concubine_state(identity)
        target = self.target_concubine_name(identity)
        current = str(state.get("concubine_name") or "").strip()
        if not current or current == target:
            return True
        if self._concubine_command_paused(CONCUBINE_DISMISS_COMMAND, identity):
            return False
        log.info(f"Target concubine [{identity}]: dismissing {current}, looking for {target}.")
        _, dismiss_text, _ = await self._send_target_concubine_command(
            identity,
            CONCUBINE_DISMISS_COMMAND,
            timeout=60,
            max_retries=0,
            delete_after=False,
        )
        if not dismiss_text:
            self._set_concubine_search_backoff(identity, CONCUBINE_SEARCH_RETRY_SECONDS, "dismiss_no_response")
            return False
        if not self.record_concubine_dismiss_response(dismiss_text, identity=identity, source="auto_dismiss"):
            notify_unrecognized_response(self, CONCUBINE_DISMISS_COMMAND, dismiss_text, log, f"寻找南宫婉[{identity}]")
            self._set_concubine_search_backoff(identity, CONCUBINE_SEARCH_RETRY_SECONDS, dismiss_text[:160])
            return False
        state = self.ensure_target_concubine_state(identity)
        if str(state.get("concubine_name") or "").strip() == current:
            self._set_concubine_search_backoff(identity, CONCUBINE_SEARCH_RETRY_SECONDS, dismiss_text[:160])
            return False
        return True

    async def execute_target_concubine_search(self, identity="主魂"):
        """Find the configured target concubine by red-dust search, dismissing non-target results."""
        identity = identity or "主魂"
        if not self.target_concubine_enabled(identity):
            return True
        if hasattr(self, "identity_pause_seconds") and self.identity_pause_seconds(identity) > 0:
            return False

        state = self.ensure_target_concubine_state(identity)
        if self.target_concubine_found(identity):
            return True
        target = self.target_concubine_name(identity)
        current = str(state.get("concubine_name") or "").strip()
        next_time = state.get("next_concubine_search_time", "")
        if next_time and is_future(next_time) and not (current and current != target):
            return False

        async with _ConcubineAtomicTask(self, f"TargetConcubine-{identity}"):
            if self.target_concubine_found(identity):
                return True
            state = self.ensure_target_concubine_state(identity)
            current = str(state.get("concubine_name") or "").strip()
            next_time = state.get("next_concubine_search_time", "")
            if next_time and is_future(next_time) and not (current and current != self.target_concubine_name(identity)):
                return False

            if not self._concubine_command_paused(".我的侍妾", identity):
                _, status_text, _ = await self._send_target_concubine_command(
                    identity,
                    ".我的侍妾",
                    timeout=60,
                    max_retries=0,
                    delete_after=False,
                    suppress_no_response_alert=True,
                )
                if status_text:
                    self.record_target_concubine_status_text(identity, status_text, source="target_status")
                    if self.target_concubine_found(identity):
                        return True

            if not await self.dismiss_current_concubine_if_needed(identity):
                return False

            state = self.ensure_target_concubine_state(identity)
            next_time = state.get("next_concubine_search_time", "")
            if next_time and is_future(next_time):
                return False
            if self._concubine_command_paused(CONCUBINE_SEARCH_COMMAND, identity):
                return False

            log.info(f"Target concubine [{identity}]: sending {CONCUBINE_SEARCH_COMMAND}.")
            search_resp, search_text, _ = await self._send_target_concubine_command(
                identity,
                CONCUBINE_SEARCH_COMMAND,
                timeout=90,
                max_retries=0,
                delete_after=False,
                return_response_msg=True,
            )
            search_resp = await self.wait_for_concubine_search_settlement(search_resp, identity=identity)
            search_text = self._concubine_response_text(search_resp) or search_text
            if not search_text:
                self._set_concubine_search_backoff(identity, CONCUBINE_SEARCH_RETRY_SECONDS, "search_no_response")
                return False
            if not self.record_concubine_search_response(search_text, identity=identity, source="auto_search"):
                notify_unrecognized_response(self, CONCUBINE_SEARCH_COMMAND, search_text, log, f"寻找南宫婉[{identity}]")
                self._set_concubine_search_backoff(identity, CONCUBINE_SEARCH_RETRY_SECONDS, search_text[:160])
                return False
            if self.target_concubine_found(identity):
                return True
            await asyncio.sleep(3)
            await self.dismiss_current_concubine_if_needed(identity)
            return False

    def mark_concubine_dream_executed(self, identity="主魂"):
        """Remember that this identity just ran .入梦寻图 so 8h voyage can run alongside it."""
        markers = getattr(self, "_concubine_recent_dream_runs", None)
        if markers is None:
            markers = {}
            self._concubine_recent_dream_runs = markers
        markers[identity or "主魂"] = time.monotonic()

    def concubine_dream_recently_executed(self, identity="主魂", window_seconds=300):
        markers = getattr(self, "_concubine_recent_dream_runs", None) or {}
        ts = markers.get(identity or "主魂")
        return bool(ts and time.monotonic() - ts <= window_seconds)

    def align_concubine_voyage_to_dream(self, identity="主魂"):
        """Compatibility wrapper for the old dream/voyage binding."""
        if self.concubine_dream_recently_executed(identity):
            return True
        return self.align_concubine_chain_cooldowns(identity)

    def latest_concubine_dream_voyage_time(self, identity="主魂"):
        """Compatibility wrapper: the bound batch now includes divination and heart trial."""
        return self.latest_concubine_chain_time(identity)

    def align_concubine_dream_voyage_cooldowns(self, identity="主魂"):
        """Compatibility wrapper for the old dream/voyage binding."""
        return self.align_concubine_chain_cooldowns(identity)

    def defer_concubine_dream_voyage(self, identity="主魂", seconds=600):
        target_time = add_seconds_str(now_str(), seconds)
        self.bind_concubine_chain_to_time(identity or "主魂", target_time)
        return target_time

    def _record_avatar_dream_map_response(self, avatar, text):
        """Record .入梦寻图 response for an avatar and return True on successful execution."""
        clean = str(text or "").replace("**", "")
        if not clean:
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 600))
            return False
        if "修为不足" in clean:
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 3600))
            return False
        if any(k in clean for k in ["仍在远航", "还在远航", "尚在远航"]) and any(k in clean for k in ["同梦寻图", "入梦寻图", "寻图"]):
            block_until = self.concubine_voyage_block_until(avatar) or add_seconds_str(now_str(), 1800)
            self._update_concubine_identity_state(
                avatar,
                concubine_voyage_active=True,
                next_dream_map_time=block_until,
                next_concubine_voyage_time=block_until,
            )
            log.info(f"Avatar [{avatar}] dream map blocked by active voyage until {block_until}.")
            return False
        if any(k in clean for k in ["未拥有", "碎片不足", "无碎片"]):
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 24 * 3600))
            log.info(f"Avatar [{avatar}] dream map: no fragments, pause 24h.")
            return False
        if any(k in clean for k in ["冷却", "后再", "尚未", "梦图感应尚未重启"]):
            cd = self.parse_wait_time(clean) if hasattr(self, "parse_wait_time") else parse_duration_seconds(clean)
            cd_seconds = cd if cd > 0 else 1800
            self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), cd_seconds))
            log.info(f"Avatar [{avatar}] dream map on cooldown: {cd_seconds}s.")
            return False

        self.set_avatar_state(avatar, "next_dream_map_time", add_seconds_str(now_str(), 8 * 3600))
        self.mark_concubine_dream_executed(avatar)
        log.info(f"Avatar [{avatar}] dream map success, next in 8h.")
        return True

    async def _send_avatar_bound_concubine_command(self, avatar, command, send_with_cultivation_check=None, **kwargs):
        forced_exit = False
        if send_with_cultivation_check:
            result = await send_with_cultivation_check(command, **kwargs)
            if isinstance(result, tuple):
                response, forced_exit = result
            else:
                response = result
        else:
            response = await self.send_and_wait_feedback_identity(avatar, command, **kwargs)
        return response, self._concubine_response_text(response), forced_exit

    async def execute_avatar_bound_dream_voyage(self, avatar, send_with_cultivation_check=None):
        """
        Execute the bound Star Palace batch:
        .远航归来 (when active) -> .入梦寻图 -> .拼图 if complete.
        Voyage starts are disabled for every avatar; they continue normal dream handling.

        Returns True when this identity is managed by the bound batch, even if it only aligned
        cooldowns and skipped sending. Returns False for identities that should use normal dream logic.
        """
        avatar = avatar or "主魂"
        if not self.concubine_voyage_enabled(avatar):
            return False
        auto_start_voyage = self.concubine_voyage_auto_start_enabled(avatar)
        if self._concubine_command_paused(CONCUBINE_VOYAGE_COMMAND, avatar):
            auto_start_voyage = False
        if self._concubine_command_paused(".入梦寻图", avatar):
            return True
        state = self._concubine_state_container(avatar)
        if not auto_start_voyage and not state.get("concubine_voyage_active"):
            next_dream = state.get("next_dream_map_time", "")
            if next_dream and is_future(next_dream):
                return False
        if auto_start_voyage and not self.align_concubine_dream_voyage_cooldowns(avatar):
            return True

        forced_exit = False
        async with _ConcubineAtomicTask(self, f"DreamVoyage-{avatar}"):
            if auto_start_voyage and not self.align_concubine_dream_voyage_cooldowns(avatar):
                return True
            try:
                state = self._concubine_state_container(avatar)
                if state.get("concubine_voyage_active"):
                    if state.get("next_concubine_voyage_time") and is_future(state.get("next_concubine_voyage_time", "")):
                        if auto_start_voyage:
                            self.align_concubine_dream_voyage_cooldowns(avatar)
                        return True
                    if self._concubine_command_paused(CONCUBINE_VOYAGE_RETURN_COMMAND, avatar):
                        return True
                    log.info(f"Concubine dream/voyage [{avatar}]: sending {CONCUBINE_VOYAGE_RETURN_COMMAND} before .入梦寻图.")
                    _, return_text, step_forced_exit = await self._send_avatar_bound_concubine_command(
                        avatar,
                        CONCUBINE_VOYAGE_RETURN_COMMAND,
                        send_with_cultivation_check=send_with_cultivation_check,
                        timeout=90,
                        max_retries=1,
                        delete_after=False,
                    )
                    forced_exit = forced_exit or step_forced_exit
                    if not return_text:
                        self.defer_concubine_dream_voyage(avatar, 600)
                        return True
                    if not self.record_concubine_voyage_response(return_text, identity=avatar, command=CONCUBINE_VOYAGE_RETURN_COMMAND):
                        notify_unrecognized_response(self, CONCUBINE_VOYAGE_RETURN_COMMAND, return_text, log, f"侍妾远航归来[{avatar}]")
                        self.defer_concubine_dream_voyage(avatar, 600)
                        return True
                    state = self._concubine_state_container(avatar)
                    if state.get("concubine_voyage_active") and is_future(state.get("next_concubine_voyage_time", "")):
                        if auto_start_voyage:
                            self.align_concubine_dream_voyage_cooldowns(avatar)
                        return True
                    await asyncio.sleep(3)

                log.info(f"Concubine dream/voyage [{avatar}]: sending .入梦寻图.")
                dream_resp, dream_text, step_forced_exit = await self._send_avatar_bound_concubine_command(
                    avatar,
                    ".入梦寻图",
                    send_with_cultivation_check=send_with_cultivation_check,
                    timeout=90,
                    max_retries=1,
                    delete_after=False,
                )
                forced_exit = forced_exit or step_forced_exit
                if dream_text == "PAUSE_1H":
                    self.defer_concubine_dream_voyage(avatar, 3600)
                    return True
                if "修为不足" in dream_text and hasattr(self, "handle_修为不足") and not send_with_cultivation_check:
                    async def retry_dream_map():
                        return await self.send_and_wait_feedback_identity(avatar, ".入梦寻图", timeout=90, max_retries=1, delete_after=False)

                    success, retry_text = await self.handle_修为不足(
                        avatar, retry_dream_map, cooldown_key="next_dream_map_time", cooldown_hours=8
                    )
                    dream_text = self._concubine_response_text(retry_text)
                    if not success:
                        self.align_concubine_dream_voyage_cooldowns(avatar)
                        return True
                if not dream_text:
                    self.defer_concubine_dream_voyage(avatar, 600)
                    log.warning(f"Concubine dream/voyage [{avatar}]: .入梦寻图 empty response.")
                    return True

                dream_success = self._record_avatar_dream_map_response(avatar, dream_text)
                if dream_success and dream_text and "4/4" in dream_text:
                    log.info(f"Avatar [{avatar}] dream map progress 4/4, sending .拼图")
                    await asyncio.sleep(3)
                    await self.send_and_wait_feedback_identity(avatar, ".拼图", timeout=60)
                if not dream_success:
                    if auto_start_voyage:
                        self.align_concubine_dream_voyage_cooldowns(avatar)
                    return True

                if not auto_start_voyage:
                    return True

                await asyncio.sleep(3)
                log.info(f"Concubine dream/voyage [{avatar}]: sending {CONCUBINE_VOYAGE_COMMAND}.")
                _, start_text, step_forced_exit = await self._send_avatar_bound_concubine_command(
                    avatar,
                    CONCUBINE_VOYAGE_COMMAND,
                    send_with_cultivation_check=send_with_cultivation_check,
                    timeout=90,
                    max_retries=1,
                    delete_after=False,
                )
                forced_exit = forced_exit or step_forced_exit
                if not start_text:
                    self.defer_concubine_dream_voyage(avatar, 600)
                    return True
                if not self.record_concubine_voyage_response(start_text, identity=avatar, command=CONCUBINE_VOYAGE_COMMAND):
                    notify_unrecognized_response(self, CONCUBINE_VOYAGE_COMMAND, start_text, log, f"侍妾远航[{avatar}]")
                    self.defer_concubine_dream_voyage(avatar, 600)
                    return True
                self.align_concubine_dream_voyage_cooldowns(avatar)
                return True
            finally:
                if forced_exit:
                    try:
                        await self.send_and_wait_feedback_identity(avatar, ".深度闭关", timeout=60, return_response_msg=False)
                    except Exception as exc:
                        log.warning(f"Concubine dream/voyage [{avatar}]: failed to restore deep meditation: {exc}")

    def _update_concubine_identity_state(self, identity="主魂", **values):
        """Update concubine-related state for one identity."""
        identity = identity or "主魂"
        if identity != "主魂" and hasattr(self, "set_avatar_state"):
            for key, value in values.items():
                self.set_avatar_state(identity, key, value)
            return
        self.state.update(values)
        if hasattr(self, "save_state"):
            self.save_state()

    def is_concubine_voyage_response(self, text):
        """判断文本是否与侍妾远航/远航归来相关。"""
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        if any(k in clean for k in ["元婴", "元神"]) and not any(
            k in clean for k in ["侍妾", "道侣", "乱星海远航", "远航·"]
        ):
            return False
        # 松散被动消息不能只凭“归来/收获/时长”判断，否则灵兽召回、
        # 巡边归来等文本会把到期的侍妾远航错误续期。
        if any(k in clean for k in ["灵兽", "巡边", "放养", "兽栏"]) and not any(
            k in clean for k in ["侍妾", "道侣", "乱星海远航", "远航·"]
        ):
            return False
        if not any(k in clean for k in ["侍妾", "道侣", "乱星海远航", "远航"]):
            return False
        return any(k in clean for k in [
            "侍妾远航", "远航", "归来", "启航", "返航", "航程", "航海",
            "带回", "收获", "冒险", "远航冷却", "航行冷却",
            "心神未定", "情缘值", "情缘不足", "未随行", "无法出航", "无法远航",
        ])

    def _set_concubine_voyage_backoff(self, identity, seconds, active=None):
        values = {"next_concubine_voyage_time": add_seconds_str(now_str(), seconds)}
        if active is not None:
            values["concubine_voyage_active"] = bool(active)
        self._update_concubine_identity_state(identity, **values)

    def record_concubine_voyage_response(self, text, identity="主魂", command=""):
        """同步侍妾远航/远航归来回复到主魂或化身 state。"""
        if not self.concubine_voyage_enabled(identity):
            return False
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        self.record_concubine_name_from_text(identity, clean)
        command = str(command or "").strip()
        explicit_voyage_command = (
            command == CONCUBINE_VOYAGE_RETURN_COMMAND
            or command.startswith(".侍妾远航")
        )
        if (
            not explicit_voyage_command
            and not self.is_concubine_voyage_response(clean)
            and not any(k in clean for k in ["还没有侍妾", "尚无侍妾"])
        ):
            return False

        now = now_str()
        cd = parse_duration_seconds(clean)
        if not command and any(k in clean for k in [
            "道心侍妾", "入梦寻图冷却", "共历心劫冷却", "天机代卜冷却",
            "侍妾远航冷却", "远航冷却",
        ]):
            return False

        if any(k in clean for k in ["还没有侍妾", "尚无侍妾", "没有侍妾"]):
            self._update_concubine_identity_state(
                identity,
                concubine_voyage_active=False,
                next_concubine_voyage_time=add_seconds_str(now, 24 * 3600),
                last_concubine_voyage_error=clean[:160],
                last_concubine_voyage_error_time=now,
            )
            log.info(f"Concubine voyage [{identity}]: no concubine, pause 24h.")
            return True

        if (
            any(k in clean for k in ["情缘不足", "情缘值不足", "情缘不够"])
            or ("情缘值" in clean and any(k in clean for k in ["至少需要", "需要"]))
        ):
            self._update_concubine_identity_state(
                identity,
                concubine_voyage_active=False,
                next_concubine_voyage_time=add_seconds_str(now, 3600),
                last_concubine_voyage_error=clean[:160],
                last_concubine_voyage_error_time=now,
            )
            log.info(f"Concubine voyage [{identity}]: affection insufficient; retry later.")
            return True

        if any(k in clean for k in ["心神未定", "未随行", "无法出航", "无法远航"]):
            self._update_concubine_identity_state(
                identity,
                concubine_voyage_active=False,
                next_concubine_voyage_time=add_seconds_str(now, 3600),
                last_concubine_voyage_error=clean[:160],
                last_concubine_voyage_error_time=now,
            )
            log.info(f"Concubine voyage [{identity}]: unavailable ({clean[:60]}), retry later.")
            return True

        is_return_context = (
            command == CONCUBINE_VOYAGE_RETURN_COMMAND
            or (not command and ("远航归来" in clean or ("归来" in clean and "远航" in clean)))
        )
        if is_return_context:
            if any(k in clean for k in ["尚未归来", "仍在远航", "还在远航", "尚在远航", "未归"]):
                wait_seconds = cd + CONCUBINE_GRACE_SECONDS if cd > 0 else 1800
                self.bind_concubine_chain_to_time(
                    identity,
                    add_seconds_str(now, wait_seconds),
                    active=True,
                )
                log.info(f"Concubine voyage [{identity}]: return not ready, retry in {wait_seconds}s.")
                return True
            if any(k in clean for k in ["没有正在远航", "并未远航", "尚未远航", "无需归来"]):
                self._update_concubine_identity_state(
                    identity,
                    concubine_voyage_active=False,
                    next_concubine_voyage_time="",
                    last_concubine_voyage_error="",
                    last_concubine_voyage_error_time="",
                )
                log.info(f"Concubine voyage [{identity}]: no active voyage; ready to start.")
                return True
            if any(k in clean for k in ["归来", "返航", "结算", "带回", "获得", "收获", "完成"]):
                self._update_concubine_identity_state(
                    identity,
                    concubine_voyage_active=False,
                    next_concubine_voyage_time="",
                    last_concubine_voyage_error="",
                    last_concubine_voyage_error_time="",
                )
                log.info(f"Concubine voyage [{identity}]: return settled.")
                return True

        if command.startswith(".侍妾远航") or "侍妾远航" in clean or "远航" in clean:
            if any(k in clean for k in ["可归来", "可结算", "可以归来", "等待归来"]):
                self._update_concubine_identity_state(
                    identity,
                    concubine_voyage_active=True,
                    next_concubine_voyage_time="",
                    last_concubine_voyage_error="",
                    last_concubine_voyage_error_time="",
                )
                log.info(f"Concubine voyage [{identity}]: voyage ready to return.")
                return True
            if any(k in clean for k in ["冷却", "后再", "尚未", "仍在远航", "还在远航", "已在远航", "尚在远航"]):
                wait_seconds = cd + CONCUBINE_GRACE_SECONDS if cd > 0 else self.concubine_voyage_cooldown(identity)
                next_time = add_seconds_str(now, wait_seconds)
                self.bind_concubine_chain_to_time(
                    identity,
                    next_time,
                    active=True,
                )
                log.info(f"Concubine voyage [{identity}]: active/cooldown, next at {next_time}")
                return True
            if (
                any(k in clean for k in ["出发", "启航", "开始", "派遣", "已远航", "远航中", "航程", "冒险"])
                or (
                    command.startswith(".侍妾远航")
                    and "远航" in clean
                    and not any(k in clean for k in ["无法", "失败", "错误", "不足", "尚无侍妾", "还没有侍妾"])
                )
            ):
                next_time = add_seconds_str(
                    now,
                    self.concubine_voyage_cooldown(identity) + CONCUBINE_GRACE_SECONDS,
                )
                self.bind_concubine_chain_to_time(
                    identity,
                    next_time,
                    active=True,
                )
                self._update_concubine_identity_state(identity, last_concubine_voyage_time=now)
                self._update_concubine_identity_state(
                    identity,
                    last_concubine_voyage_error="",
                    last_concubine_voyage_error_time="",
                )
                log.info(f"Concubine voyage [{identity}]: started, next return at {next_time}")
                return True

        if cd > 0 and any(k in clean for k in ["远航", "归来"]):
            self.bind_concubine_chain_to_time(
                identity,
                add_seconds_str(now, cd + CONCUBINE_GRACE_SECONDS),
                active=True,
            )
            return True
        return False

    async def _send_concubine_identity_command(self, identity, command, send_with_cultivation_check=None, **kwargs):
        """Send a concubine-chain command for main soul or an avatar and return (response, text, forced_exit)."""
        identity = identity or "主魂"
        if identity == "主魂":
            response = await self.send_and_wait_feedback(command, **kwargs)
            return response, self._concubine_response_text(response), False
        return await self._send_avatar_bound_concubine_command(
            identity,
            command,
            send_with_cultivation_check=send_with_cultivation_check,
            **kwargs,
        )

    def _concubine_task_due(self, task_key, identity="主魂"):
        state = self._concubine_state_container(identity or "主魂")
        next_time = state.get(CONCUBINE_TASKS[task_key]["state_key"], "")
        return not next_time or not is_future(next_time)

    def _concubine_task_ready_for_voyage(self, task_key, identity="主魂"):
        """A step is considered clean only when it now has a meaningful cooldown."""
        state = self._concubine_state_container(identity or "主魂")
        value = state.get(CONCUBINE_TASKS[task_key]["state_key"], "")
        if not value or not is_future(value):
            return False
        return seconds_until(value) >= int(CONCUBINE_TASKS[task_key]["cooldown"] * 0.7)

    async def execute_concubine_voyage_return(self, identity="主魂", send_with_cultivation_check=None):
        """Settle an active voyage if it is due. Return True when the chain may continue."""
        identity = identity or "主魂"
        task = CONCUBINE_TASKS["voyage"]
        if not self.concubine_voyage_enabled(identity):
            return True
        state = self._concubine_state_container(identity)
        if not state.get("concubine_voyage_active"):
            return True
        next_time = state.get(task["state_key"], "")
        if next_time and is_future(next_time):
            self.bind_concubine_chain_to_time(identity, next_time, active=True)
            return False
        if self._concubine_command_paused(CONCUBINE_VOYAGE_RETURN_COMMAND, identity):
            return False
        log.info(f"Concubine chain [{identity}]: sending {CONCUBINE_VOYAGE_RETURN_COMMAND}.")
        _, return_text, _ = await self._send_concubine_identity_command(
            identity,
            CONCUBINE_VOYAGE_RETURN_COMMAND,
            send_with_cultivation_check=send_with_cultivation_check,
            timeout=90,
            max_retries=1,
            delete_after=False,
        )
        if not return_text:
            self.bind_concubine_chain_to_time(identity, add_seconds_str(now_str(), 600), active=True)
            return False
        if not self.record_concubine_voyage_response(return_text, identity=identity, command=CONCUBINE_VOYAGE_RETURN_COMMAND):
            notify_unrecognized_response(self, CONCUBINE_VOYAGE_RETURN_COMMAND, return_text, log, f"侍妾远航归来[{identity}]")
            self.bind_concubine_chain_to_time(identity, add_seconds_str(now_str(), 600), active=True)
            return False
        state = self._concubine_state_container(identity)
        if state.get("concubine_voyage_active") and is_future(state.get(task["state_key"], "")):
            self.bind_concubine_chain_to_time(identity, state.get(task["state_key"], ""), active=True)
            return False
        await asyncio.sleep(3)
        return True

    async def execute_concubine_voyage_start(self, identity="主魂", send_with_cultivation_check=None):
        """Start the configured voyage route at the end of the bound concubine chain."""
        identity = identity or "主魂"
        task = CONCUBINE_TASKS["voyage"]
        voyage_command = self.concubine_voyage_command(identity)
        if not self.concubine_voyage_auto_start_enabled(identity):
            return False
        if self._concubine_command_paused(voyage_command, identity):
            return False
        if self.recent_concubine_status_mismatch(identity):
            log.info(f"Concubine chain [{identity}]: voyage start skipped after status mismatch.")
            return False
        state = self._concubine_state_container(identity)
        next_time = state.get(task["state_key"], "")
        if state.get("concubine_voyage_active") or (next_time and is_future(next_time)):
            return False
        log.info(f"Concubine chain [{identity}]: sending {voyage_command}.")
        _, start_text, _ = await self._send_concubine_identity_command(
            identity,
            voyage_command,
            send_with_cultivation_check=send_with_cultivation_check,
            timeout=90,
            max_retries=1,
            delete_after=False,
        )
        if not start_text:
            self._set_concubine_voyage_backoff(identity, 600, active=False)
            return False
        if not self.record_concubine_voyage_response(start_text, identity=identity, command=voyage_command):
            notify_unrecognized_response(self, voyage_command, start_text, log, f"侍妾远航[{identity}]")
            self._set_concubine_voyage_backoff(identity, 600, active=False)
            return False
        return True

    async def execute_concubine_voyage(self):
        """主魂侍妾远航兜底：到点先归来结算，再开启下一轮配置路线。"""
        task = CONCUBINE_TASKS["voyage"]
        voyage_command = self.concubine_voyage_command("主魂")
        if not self.concubine_voyage_enabled("主魂"):
            return
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(voyage_command, "主魂"):
            return
        if hasattr(self, "switch_back_to_main"):
            await self.switch_back_to_main()
        if not await self.execute_concubine_voyage_return("主魂"):
            return
        await self.execute_concubine_voyage_start("主魂")

    async def execute_avatar_concubine_voyage(self, avatar):
        """化身侍妾远航兜底：保持同一身份连续结算并重新出发。"""
        task = CONCUBINE_TASKS["voyage"]
        if not self.concubine_voyage_enabled(avatar):
            return False
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(task["command"], avatar):
            return False
        state = self._concubine_state_container(avatar)
        next_time = state.get(task["state_key"], "")
        if next_time and is_future(next_time):
            return False

        async with _ConcubineAtomicTask(self, f"ConcubineVoyage-{avatar}"):
            if not await self.execute_concubine_voyage_return(avatar):
                return False
            return await self.execute_concubine_voyage_start(avatar)

    def _record_avatar_concubine_direct_response(self, avatar, task_key, text):
        """Record avatar .天机代卜/.入梦寻图 replies."""
        if task_key == "dream":
            return self._record_avatar_dream_map_response(avatar, text)

        task = CONCUBINE_TASKS[task_key]
        clean = str(text or "").replace("**", "")
        if not clean:
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 600))
            return False
        if self.concubine_response_indicates_active_voyage(clean):
            self.defer_concubine_task_until_voyage(task_key, avatar)
            return False
        if any(k in clean for k in ["还没有侍妾", "尚无侍妾", "没有侍妾"]):
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 24 * 3600))
            return False
        if "修为不足" in clean:
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 3600))
            return False
        if any(k in clean for k in ["冷却", "后再", "尚未"]):
            cd = self.parse_wait_time(clean) if hasattr(self, "parse_wait_time") else parse_duration_seconds(clean)
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), (cd if cd > 0 else 1800) + CONCUBINE_GRACE_SECONDS))
            return False
        if not self.is_known_concubine_response(task_key, clean):
            return False
        self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), task["cooldown"] + CONCUBINE_GRACE_SECONDS))
        return True

    async def execute_avatar_concubine_direct(self, avatar, task_key, send_with_cultivation_check=None):
        """Execute one non-heart-trial concubine command for an avatar."""
        task = CONCUBINE_TASKS[task_key]
        if self._concubine_command_paused(task["command"], avatar):
            return True
        if self.concubine_voyage_block_until(avatar):
            self.defer_concubine_task_until_voyage(task_key, avatar)
            return False
        if not self._concubine_task_due(task_key, avatar):
            return True
        log.info(f"Concubine chain [{avatar}]: sending {task['command']}.")
        _, text, _ = await self._send_concubine_identity_command(
            avatar,
            task["command"],
            send_with_cultivation_check=send_with_cultivation_check,
            timeout=90,
            max_retries=1,
            delete_after=False,
        )
        if text == "PAUSE_1H":
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 3600))
            return False
        if not text:
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 600))
            return False
        ok = self._record_avatar_concubine_direct_response(avatar, task_key, text)
        if (
            not ok
            and not self.concubine_response_indicates_active_voyage(text)
            and not any(k in text for k in ["冷却", "后再", "尚未", "修为不足", "还没有侍妾", "尚无侍妾", "没有侍妾"])
        ):
            notify_unrecognized_response(self, task["command"], text, log, f"{task['label']}[{avatar}]")
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 600))
        if ok and task_key == "dream" and text and "4/4" in text:
            log.info(f"Avatar [{avatar}] dream map progress 4/4, sending .拼图")
            await asyncio.sleep(3)
            await self.send_and_wait_feedback_identity(avatar, ".拼图", timeout=60)
        return ok

    async def execute_avatar_heart_trial_via_status(self, avatar):
        """Query .我的侍妾 for an avatar and run the script-specific heart trial flow."""
        task = CONCUBINE_TASKS["heart_trial"]
        if self._concubine_command_paused(task["command"], avatar):
            return True
        if self.concubine_voyage_block_until(avatar):
            self.defer_concubine_task_until_voyage("heart_trial", avatar)
            return False
        if not self._concubine_task_due("heart_trial", avatar):
            return True
        status_msg = await self.send_and_wait_feedback_identity(
            avatar, ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = getattr(status_msg, "text", "") if hasattr(status_msg, "text") else str(status_msg) if isinstance(status_msg, str) else ""
        if not status_text:
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 600))
            return False
        if not self.concubine_status_matches_identity(status_text, avatar):
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 30))
            return False
        if any(k in status_text for k in ["还没有侍妾", "尚无侍妾", "没有侍妾"]):
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 24 * 3600))
            return False
        voyage_block_until = self.parse_concubine_voyage_status_line(status_text, avatar)
        if voyage_block_until:
            self.set_avatar_state(avatar, task["state_key"], voyage_block_until)
            return False
        clean_status = status_text.replace("**", "")
        heart_match = re.search(r"(?:共历)?心劫冷却\s*[：:]\s*([^\s\n|]+)", clean_status)
        if heart_match:
            val = heart_match.group(1).strip()
            if not any(k in val for k in ["无", "可用", "可施展", "已就绪"]):
                cd = self.parse_wait_time(val) if hasattr(self, "parse_wait_time") else parse_duration_seconds(val)
                self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), (cd if cd > 0 else 1800) + CONCUBINE_GRACE_SECONDS))
                return False
        if not (status_msg and hasattr(status_msg, "id")):
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 600))
            return False
        if not hasattr(self, "execute_avatar_heart_trial"):
            self.set_avatar_state(avatar, task["state_key"], add_seconds_str(now_str(), 600))
            return False
        result = await self.execute_avatar_heart_trial(avatar, status_msg)
        if result is False:
            return False
        return self._concubine_task_ready_for_voyage("heart_trial", avatar)

    async def execute_concubine_chain(self):
        """Main-soul bound flow; only main/main ends with the 月殿寻痕 voyage."""
        self.ensure_concubine_state()
        if self.target_concubine_enabled("主魂") and not self.target_concubine_found("主魂"):
            await self.execute_target_concubine_search("主魂")
            if not self.target_concubine_found("主魂"):
                return False
        if not self.align_concubine_chain_cooldowns("主魂"):
            return False
        async with _ConcubineAtomicTask(self, "ConcubineChain-主魂"):
            if not self.align_concubine_chain_cooldowns("主魂"):
                return False
            if hasattr(self, "switch_back_to_main"):
                await self.switch_back_to_main()
            if not await self.execute_concubine_voyage_return("主魂"):
                return True
            for task_key in ("divination", "dream"):
                task = CONCUBINE_TASKS[task_key]
                if self._concubine_command_paused(task["command"], "主魂"):
                    continue
                if not self._concubine_task_due(task_key, "主魂"):
                    continue
                ok = await self.execute_concubine_direct(task_key)
                if not ok:
                    log.info(f"Concubine chain [主魂]: {task['label']} not completed; continuing to voyage check.")
                    continue
                await asyncio.sleep(3)
            if (
                self._concubine_task_due("heart_trial", "主魂")
                and not self._concubine_command_paused(CONCUBINE_TASKS["heart_trial"]["command"], "主魂")
            ):
                if self._concubine_command_paused(".我的侍妾", "主魂"):
                    log.info("Concubine chain [主魂]: .我的侍妾 paused; deferring heart trial status check.")
                    self.defer_concubine_task("heart_trial", 6 * 3600)
                    return True
                await self.execute_heart_trial()
                if not self._concubine_task_ready_for_voyage("heart_trial", "主魂"):
                    log.info("Concubine chain [主魂]: heart trial not completed; continuing to voyage check.")
                else:
                    await asyncio.sleep(3)
            await self.execute_concubine_voyage_start("主魂")
            return True

    async def execute_avatar_concubine_chain(self, avatar, send_with_cultivation_check=None, heart_trial_executor=None):
        """Avatar bound flow; voyage is disabled while other concubine tasks remain active."""
        avatar = avatar or "主魂"
        if self.target_concubine_enabled(avatar) and not self.target_concubine_found(avatar):
            await self.execute_target_concubine_search(avatar)
            if not self.target_concubine_found(avatar):
                return True
        if not self.align_concubine_chain_cooldowns(avatar):
            return True
        async with _ConcubineAtomicTask(self, f"ConcubineChain-{avatar}"):
            if not self.align_concubine_chain_cooldowns(avatar):
                return True
            if not await self.execute_concubine_voyage_return(avatar, send_with_cultivation_check=send_with_cultivation_check):
                return True
            for task_key in ("divination", "dream"):
                ok = await self.execute_avatar_concubine_direct(
                    avatar,
                    task_key,
                    send_with_cultivation_check=send_with_cultivation_check,
                )
                if not ok:
                    log.info(f"Concubine chain [{avatar}]: {CONCUBINE_TASKS[task_key]['label']} not completed; continuing to voyage check.")
                    continue
                await asyncio.sleep(3)
            if self._concubine_task_due("heart_trial", avatar):
                if heart_trial_executor:
                    await heart_trial_executor(avatar)
                    heart_ok = self._concubine_task_ready_for_voyage("heart_trial", avatar)
                else:
                    heart_ok = await self.execute_avatar_heart_trial_via_status(avatar)
                if not heart_ok:
                    log.info(f"Concubine chain [{avatar}]: heart trial not completed; continuing to voyage check.")
                else:
                    await asyncio.sleep(3)
            await self.execute_concubine_voyage_start(
                avatar,
                send_with_cultivation_check=send_with_cultivation_check,
            )
            return True

    # ---- 状态同步 ----

    async def sync_concubine_status_if_needed(self):
        """如需刷新侍妾状态缓存，发送.我的侍妾并解析"""
        self.ensure_concubine_state()
        if self.has_concubine_status_cache():
            return
        log.info("Concubine: cache missing, querying .我的侍妾")
        status_msg = await self.send_and_wait_feedback(
            ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = (status_msg.text or "") if status_msg else ""
        if status_msg and hasattr(status_msg, "id"):
            self.state["last_concubine_status_msg_id"] = status_msg.id
        if not self.parse_concubine_status(status_text):
            log.warning("Concubine: .我的侍妾 response did not contain cooldowns.")
            if status_text:
                notify_unrecognized_response(self, ".我的侍妾", status_text, log, "侍妾状态")

    async def refresh_heart_trial_cooldown_after_uncertain(self, context):
        """不确定状态下重新查询侍妾状态，确认共历心劫的冷却"""
        task = CONCUBINE_TASKS["heart_trial"]
        log.info(f"Concubine 共历心劫: uncertain {context}, querying .我的侍妾 before alert.")
        status_msg = await self.send_and_wait_feedback(
            ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = (status_msg.text or "") if status_msg else ""
        if status_msg and hasattr(status_msg, "id"):
            self.state["last_concubine_status_msg_id"] = status_msg.id
        if not status_text:
            log.warning(f"Concubine 共历心劫: no .我的侍妾 response while checking {context}.")
            return False
        if not self.parse_concubine_status(status_text):
            log.warning(f"Concubine 共历心劫: .我的侍妾 did not expose cooldown while checking {context}.")
            return False
        next_time = self.state.get(task["state_key"], "")
        if next_time and is_future(next_time):
            log.info(f"Concubine 共历心劫: cooldown detected after uncertain {context}, next at {next_time}.")
            return True
        log.warning(f"Concubine 共历心劫: still no cooldown after uncertain {context}.")
        return False

    # ---- 执行神通 ----

    async def execute_concubine_direct(self, task_key):
        """执行非心劫类侍妾神通（入梦寻图/天机代卜），直接发送指令"""
        task = CONCUBINE_TASKS[task_key]
        if task_key in {"dream", "divination"} and self.concubine_voyage_block_until("主魂"):
            self.defer_concubine_task_until_voyage(task_key, "主魂")
            return False
        log.info(f"Concubine: sending {task['command']}")
        # 发送前确保身份对齐（防止化身协程在间隙抢走身份）
        if hasattr(self, 'switch_back_to_main'):
            await self.switch_back_to_main()
        resp = await self.send_and_wait_feedback(task["command"], timeout=90, delete_after=False)
        if resp:
            if not self.is_known_concubine_response(task_key, resp):
                log.warning(f"Concubine {task['label']}: unrecognized response, skipping this run.")
                notify_unrecognized_response(self, task["command"], resp, log, task["label"])
                self.defer_concubine_task(task_key)
                return False
            self.record_concubine_cd(task_key, resp)
            if task_key == "dream" and not any(k in resp for k in ["冷却", "后再", "尚未", "未拥有", "不足"]):
                self.mark_concubine_dream_executed("主魂")
            # 入梦寻图：集齐残纹后自动发送 .拼图
            if task_key == "dream" and resp and "4/4" in resp:
                log.info("Concubine dream: progress 4/4 reached, sending .拼图")
                await asyncio.sleep(3)
                puzzle_resp = await self.send_and_wait_feedback(".拼图", timeout=90, delete_after=False)
                if puzzle_resp:
                    log.info(f"Concubine .拼图 response: {puzzle_resp[:120]}")
                else:
                    log.warning("Concubine .拼图: no response.")
            return not any(k in resp for k in ["冷却", "后再", "尚未", "仍在远航", "还在远航"])
        else:
            log.warning(f"Concubine {task['label']}: no response, retrying later.")
            self.defer_concubine_task(task_key)
            return False

    # ---- 消息编辑检测 ----

    async def get_message_after_delay(self, msg, delay_sec=12):
        """延迟后重新获取消息（等待游戏机器人编辑回复）"""
        if not msg:
            return None
        await asyncio.sleep(delay_sec)
        try:
            return await self.client.get_messages(self.target_chat_id, ids=msg.id)
        except Exception as e:
            log.warning(f"Concubine: failed to fetch edited message {msg.id}: {e}")
            return msg

    async def wait_for_message_edit(self, msg, timeout_sec=45, poll_sec=3):
        """
        轮询等待消息被游戏机器人编辑。
        游戏机器人有时先发后编辑（逐步揭示结果），此函数用于等待最终版本。
        """
        if not msg:
            return None
        before_text = msg.text or ""
        deadline = datetime.now() + timedelta(seconds=timeout_sec)
        latest = msg
        while datetime.now() < deadline:
            await asyncio.sleep(poll_sec)
            try:
                fetched = await self.client.get_messages(self.target_chat_id, ids=msg.id)
            except Exception as e:
                log.warning(f"Concubine: failed to poll edited message {msg.id}: {e}")
                return latest
            if fetched:
                latest = fetched
                after_text = fetched.text or ""
                if after_text != before_text:
                    record_bot_response(self)
                    await asyncio.sleep(2)
                    return fetched
        log.warning(f"Concubine: message {msg.id} was not edited within {timeout_sec}s.")
        record_bot_no_response(self, ".稳", log)
        return latest

    # ---- 共历心劫核心流程 ----

    async def wait_for_heart_trial_round_result(self, current_msg, sent_msg, idx, timeout_sec=90, poll_sec=3):
        """
        等待心劫一轮的结果。
        同时监听：
        1. feedback_events（回复消息触发）
        2. 消息编辑（游戏机器人编辑原消息）
        任一方式确认本轮完成即返回。
        """
        if not current_msg:
            return None, "", False
        before_text = current_msg.text or ""
        latest_msg = current_msg
        latest_text = before_text
        sent_id = getattr(sent_msg, "id", None)
        evt = asyncio.Event()
        if sent_id is not None:
            self.feedback_events[sent_id] = evt
            self.feedback_commands[sent_id] = ".稳"
            self.feedback_sent_ts[sent_id] = time.monotonic()
            self.feedback_senders = getattr(self, "feedback_senders", {})
            self.feedback_senders[sent_id] = getattr(sent_msg, "sender_id", None)
            self.feedback_identities = getattr(self, "feedback_identities", {})
            self.feedback_identities[sent_id] = getattr(self, "current_identity", "主魂")

        deadline = datetime.now() + timedelta(seconds=timeout_sec)
        try:
            while datetime.now() < deadline:
                remaining = max(0.1, (deadline - datetime.now()).total_seconds())
                try:
                    await asyncio.wait_for(evt.wait(), timeout=min(poll_sec, remaining))
                except asyncio.TimeoutError:
                    pass

                if sent_id is not None and evt.is_set():
                    reply_text = self.last_feedback_text.pop(sent_id, "").strip()
                    reply_msg = self.last_feedback_msg.pop(sent_id, None)
                    evt.clear()
                    if reply_text:
                        try:
                            record_bot_response(self)
                        except Exception as exc:
                            # 记录/遥测失败不能中断心劫三轮流程；下面仍继续使用回复正文判断回合。
                            log.warning(f"Concubine: heart trial response telemetry failed: {exc}", exc_info=True)
                        latest_msg = reply_msg or latest_msg
                        latest_text = reply_text
                        if self.heart_trial_round_confirmed(reply_text, idx):
                            return latest_msg, latest_text, True
                        if any(k in reply_text for k in ["冷却", "尚未", "后再", "失败", "无法", "错误", "锚点已散"]):
                            return latest_msg, latest_text, False

                try:
                    fetched = await self.client.get_messages(self.target_chat_id, ids=current_msg.id)
                except Exception as e:
                    log.warning(f"Concubine: failed to poll heart trial message {current_msg.id}: {e}")
                    break

                if fetched and (fetched.text or "") != before_text:
                    latest_msg = fetched
                    latest_text = fetched.text or ""
                    try:
                        record_bot_response(self)
                    except Exception as exc:
                        # MessageEdited 只负责辅助记录，不能让后续 .稳 被外层循环吞掉。
                        log.warning(f"Concubine: heart trial edited-response telemetry failed: {exc}", exc_info=True)
                    if self.heart_trial_round_confirmed(latest_text, idx):
                        await asyncio.sleep(2)
                        return latest_msg, latest_text, True
                    if any(k in latest_text for k in ["冷却", "尚未", "后再", "失败", "无法", "错误", "锚点已散"]):
                        return latest_msg, latest_text, False

            log.warning(f"Concubine: .稳 ({idx}/3) did not produce a confirmed round within {timeout_sec}s.")
            record_bot_no_response(self, ".稳", log)
            return latest_msg, latest_text, False
        finally:
            if sent_id is not None:
                self.feedback_events.pop(sent_id, None)
                self.last_feedback_text.pop(sent_id, None)
                self.last_feedback_msg.pop(sent_id, None)
                self.feedback_commands.pop(sent_id, None)
                self.feedback_sent_ts.pop(sent_id, None)
                self.feedback_senders.pop(sent_id, None)
                if hasattr(self, "feedback_identities"):
                    self.feedback_identities.pop(sent_id, None)

    async def wait_for_heart_trial_round_result_safe(
        self,
        current_msg,
        sent_msg,
        idx,
        timeout_sec=90,
        poll_sec=3,
    ):
        """Wait for one heart-trial round without letting telemetry exceptions abort the flow."""
        try:
            return await self.wait_for_heart_trial_round_result(
                current_msg,
                sent_msg,
                idx,
                timeout_sec=timeout_sec,
                poll_sec=poll_sec,
            )
        except Exception as exc:
            log.warning(
                f"Concubine: heart trial round {idx} wait failed; recovering from the current anchor: {exc}",
                exc_info=True,
            )
            latest = current_msg
            latest_text = getattr(current_msg, "text", "") or ""
            msg_id = getattr(current_msg, "id", None)
            client = getattr(self, "client", None)
            chat_id = getattr(self, "target_chat_id", None)
            if msg_id and client and chat_id is not None:
                try:
                    fetched = await client.get_messages(chat_id, ids=msg_id)
                    if fetched:
                        latest = fetched
                        latest_text = getattr(fetched, "text", "") or ""
                except Exception as fetch_exc:
                    log.warning(
                        f"Concubine: failed to recover heart trial anchor {msg_id}: {fetch_exc}",
                        exc_info=True,
                    )
            return latest, latest_text, self.heart_trial_round_confirmed(latest_text, idx)

    async def delete_heart_trial_command_later(self, sent_msg, delay_sec=120):
        """延迟删除心劫指令（清理群聊记录）"""
        if not sent_msg or not hasattr(self, "delete_msg"):
            return
        await asyncio.sleep(delay_sec)
        try:
            await self.delete_msg(sent_msg)
        except Exception as e:
            log.warning(f"Concubine: delayed .稳 delete failed: {e}")

    def heart_trial_round_confirmed(self, text, idx):
        """检查心劫第idx轮是否已确认完成"""
        clean = (text or "").replace("**", "")
        if self.heart_trial_settled(text):
            return True
        current_labels = self.heart_trial_round_labels(idx)
        next_labels = self.heart_trial_round_labels(idx + 1)
        current_done = any(f"第{label}轮已定" in clean for label in current_labels)
        next_prompt = any(f"第{label}轮" in clean for label in next_labels)
        return current_done and next_prompt

    def heart_trial_settled(self, text):
        """检查心劫是否已结算（全部三轮完成）"""
        clean = (text or "").replace("**", "")
        return "坠魔心劫·结算" in clean

    @staticmethod
    def heart_trial_round_labels(idx):
        """心劫第n轮的数字标签（中英文）"""
        labels = {1: ("1", "一"), 2: ("2", "二"), 3: ("3", "三")}
        return labels.get(idx, (str(idx),))

    def heart_trial_round_prompt(self, text, idx):
        """检查文本是否包含第idx轮的提示"""
        clean = (text or "").replace("**", "")
        if "坠魔心劫" not in clean:
            return False
        return any(f"第{label}轮" in clean for label in self.heart_trial_round_labels(idx))

    def heart_trial_requires_reply_target(self, text):
        """检查游戏机器人是否要求回复到指定的侍妾消息"""
        clean = (text or "").replace("**", "")
        return (
            "请回复" in clean
            and ".共历心劫" in clean
            and ("侍妾" in clean or "道侣" in clean)
        )

    def heart_trial_terminal_failure(self, text):
        """当前心劫锚点已经不可继续，需重查侍妾状态后再排冷却。"""
        clean = (text or "").replace("**", "")
        return any(k in clean for k in [
            "心劫锚点已散",
            "需重新引动天劫",
            "无法共历心劫",
            "尚无侍妾",
            "还没有侍妾",
        ])

    async def sync_avatar_heart_trial_cooldown_after_failure(self, avatar, reason, fallback_seconds=600):
        """化身心劫锚点异常后查询 .我的侍妾，按真实冷却重排。"""
        if not hasattr(self, "send_and_wait_feedback_identity") or not hasattr(self, "set_avatar_state"):
            return False
        log.warning(f"Avatar [{avatar}] heart trial aborted: {reason}; syncing .我的侍妾 cooldown.")
        status_msg = await self.send_and_wait_feedback_identity(
            avatar, ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        status_text = (
            getattr(status_msg, "text", "")
            if hasattr(status_msg, "text")
            else str(status_msg) if isinstance(status_msg, str) else ""
        )
        if not status_text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), fallback_seconds))
            return False
        if hasattr(self, "concubine_status_matches_identity") and not self.concubine_status_matches_identity(status_text, avatar):
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 30))
            return False
        if "尚无侍妾" in status_text or "还没有侍妾" in status_text:
            self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), 24 * 3600))
            return True
        voyage_block_until = self.parse_concubine_voyage_status_line(status_text, avatar)
        if voyage_block_until:
            self.set_avatar_state(avatar, "next_heart_trial_time", voyage_block_until)
            return True
        clean_status = status_text.replace("**", "")
        heart_match = re.search(r"(?:共历)?心劫冷却\s*[：:]\s*([^\s\n|]+)", clean_status)
        if heart_match:
            val = heart_match.group(1).strip()
            if any(k in val for k in ["无", "可用", "可施展", "已就绪"]):
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), fallback_seconds))
                return True
            cd = self.parse_wait_time(val)
            if cd > 0:
                self.set_avatar_state(avatar, "next_heart_trial_time", add_seconds_str(now_str(), cd + 60))
                return True
        cd = self.parse_wait_time(status_text)
        self.set_avatar_state(
            avatar,
            "next_heart_trial_time",
            add_seconds_str(now_str(), (cd + 60) if cd > 0 else fallback_seconds),
        )
        return cd > 0

    async def execute_heart_trial(self):
        """
        共历心劫完整流程。

        步骤：
        1. 查询侍妾状态（.我的侍妾）
        2. 发送.共历心劫
        3. 如果游戏机器人要求回复到侍妾消息，尝试3次
        4. 依次发送三轮.稳 (稳/稳/稳 策略)
        5. 每轮等待结果确认
        6. 结算后记录冷却
        """
        task = CONCUBINE_TASKS["heart_trial"]
        log.info("Concubine: starting .共历心劫 flow via .我的侍妾")
        if self._concubine_command_paused(".我的侍妾", "主魂"):
            log.info("Concubine 共历心劫: .我的侍妾 paused; deferring status check.")
            self.defer_concubine_task("heart_trial", 6 * 3600)
            return
        if self._concubine_command_paused(task["command"], "主魂"):
            log.info("Concubine 共历心劫: .共历心劫 paused; deferring flow.")
            self.defer_concubine_task("heart_trial", 6 * 3600)
            return
        # 每条命令前确保身份对齐（防止化身协程在间隙抢走身份）
        if hasattr(self, 'switch_back_to_main'):
            await self.switch_back_to_main()
        status_msg = await self.send_and_wait_feedback(
            ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
        )
        if not status_msg:
            log.warning("Concubine 共历心劫: missing .我的侍妾 status message.")
            self.defer_concubine_task("heart_trial")
            return

        if hasattr(status_msg, "id"):
            self.state["last_concubine_status_msg_id"] = status_msg.id

        status_text = status_msg.text or ""
        if status_text and not self.concubine_status_matches_identity(status_text, "主魂"):
            log.warning("Concubine 共历心劫: mismatched .我的侍妾 status; retrying later.")
            self.defer_concubine_task("heart_trial", 30)
            return
        if not self.parse_concubine_status(status_text) and status_text:
            notify_unrecognized_response(self, ".我的侍妾", status_text, log, "共历心劫前状态")
            self.defer_concubine_task("heart_trial")
            return
        if self.state.get(task["state_key"]) and is_future(self.state[task["state_key"]]):
            log.info(f"Concubine 共历心劫: still on CD until {self.state[task['state_key']]}")
            return
        if self.concubine_voyage_block_until("主魂"):
            self.defer_concubine_task_until_voyage("heart_trial", "主魂")
            return

        trial_msg = None
        trial_text = ""
        for attempt in range(1, 4):
            log.info(f"Concubine 共历心劫: replying .共历心劫 to .我的侍妾 status message ({attempt}/3).")
            # 每次发送前确保身份对齐
            if hasattr(self, 'switch_back_to_main'):
                await self.switch_back_to_main()
            trial_msg = await self.send_and_wait_feedback(
                task["command"], reply_to=status_msg.id,
                timeout=90, return_response_msg=True, delete_after=False,
            )
            trial_text = (trial_msg.text or "") if trial_msg else ""
            if not self.heart_trial_requires_reply_target(trial_text):
                break
            log.warning("Concubine 共历心劫: bot did not accept the reply target; retrying.")
            if attempt < 3:
                status_msg = await self.send_and_wait_feedback(
                    ".我的侍妾", timeout=60, return_response_msg=True, delete_after=False,
                )
                if not status_msg:
                    break
                await asyncio.sleep(3)

        if not trial_msg:
            log.warning("Concubine 共历心劫: missing .共历心劫 response message.")
            self.defer_concubine_task("heart_trial")
            return

        if any(k in trial_text for k in ["冷却", "后再", "尚未"]):
            self.record_concubine_cd("heart_trial", trial_text)
            return
        if self.concubine_response_indicates_active_voyage(trial_text):
            self.defer_concubine_task_until_voyage("heart_trial", "主魂")
            return
        if self.heart_trial_terminal_failure(trial_text):
            if not await self.refresh_heart_trial_cooldown_after_uncertain("共历心劫终止返回"):
                self.defer_concubine_task("heart_trial", 600)
            return
        if self.heart_trial_requires_reply_target(trial_text):
            log.warning("Concubine 共历心劫: bot still requires reply target.")
            notify_unrecognized_response(self, task["command"], trial_text, log, "共历心劫回复目标")
            self.defer_concubine_task("heart_trial", 600)
            return
        if not self.heart_trial_round_prompt(trial_text, 1):
            log.warning("Concubine 共历心劫: response did not start round 1.")
            notify_unrecognized_response(self, task["command"], trial_text, log, "共历心劫开局")
            self.defer_concubine_task("heart_trial", 600)
            return
        if not self.is_known_concubine_response("heart_trial", trial_text):
            log.warning("Concubine 共历心劫: unrecognized trial response.")
            notify_unrecognized_response(self, task["command"], trial_text, log, "共历心劫")
            self.defer_concubine_task("heart_trial")
            return

        # 三轮心劫循环（每轮发.稳）
        current_msg = trial_msg
        # 发 .稳 前确保身份对齐
        if hasattr(self, 'switch_back_to_main'):
            await self.switch_back_to_main()
        async with _ConcubineAtomicTask(self, "HeartTrial-主魂"):
            async with self.cmd_lock:
                for idx in range(1, 4):
                    confirmed = False
                    current_text = ""
                    for attempt in range(1, 4):
                        try:
                            pause_event = getattr(self, "pause_event", None)
                            if pause_event is not None:
                                await pause_event.wait()
                            if not await wait_for_bot_activity_before_send(self, ".稳", log):
                                return
                            if not command_send_allowed(self, ".稳", log):
                                return
                            remember_script_send_intent(self, ".稳")
                            sent = await self.client.send_message(self.target_chat_id, ".稳", reply_to=current_msg.id)
                            remember_script_sent_message(self, sent)
                            record_command_sent(
                                self,
                                sent,
                                ".稳",
                                identity=getattr(self, "current_identity", "主魂"),
                                source="auto",
                                reply_to=current_msg.id,
                                logger=log,
                            )
                            schedule_command_auto_delete(self, sent, text=".稳", logger=log)
                            _identity = getattr(self, "current_identity", None)
                            _tag = f" [{_identity}]" if _identity else ""
                            log.info(f"🟢 OUT{_tag}:\n.稳 ({idx}/3, try {attempt}/3)")
                        except Exception as e:
                            log.error(f"Concubine 共历心劫: failed to send .稳 ({idx}/3): {e}")
                            return

                        result_msg, current_text, confirmed = await self.wait_for_heart_trial_round_result_safe(
                            current_msg, sent, idx, timeout_sec=90, poll_sec=3,
                        )
                        if result_msg:
                            current_msg = result_msg
                            await log_incoming_message(self, f".稳 {idx}/3 try {attempt}/3", current_text, msg=result_msg, logger=log)
                            if self.heart_trial_settled(current_text):
                                self.record_concubine_cd("heart_trial")
                                return
                            if confirmed:
                                break
                            if self.heart_trial_terminal_failure(current_text):
                                if not await self.refresh_heart_trial_cooldown_after_uncertain(f".稳 第{idx}轮终止返回"):
                                    self.defer_concubine_task("heart_trial", 600)
                                return
                            if self.heart_trial_round_prompt(current_text, idx):
                                if attempt < 3:
                                    log.warning(f"Concubine 共历心劫: still on round {idx}; retrying.")
                                    await asyncio.sleep(3)
                                    continue
                                if await self.refresh_heart_trial_cooldown_after_uncertain(f".稳 第{idx}轮"):
                                    return
                            log.warning(f"Concubine 共历心劫: .稳 ({idx}/3) did not confirm.")
                            notify_unrecognized_response(self, ".稳", current_text, log, f"共历心劫第{idx}轮")
                            self.defer_concubine_task("heart_trial", 600)
                            return
                        log.warning(f"Concubine 共历心劫: missing edit after .稳 ({idx}/3, try {attempt}/3).")
                        if attempt < 3:
                            await asyncio.sleep(3)
                            continue
                        if await self.refresh_heart_trial_cooldown_after_uncertain(f".稳 第{idx}轮无编辑"):
                            return
                        self.defer_concubine_task("heart_trial", 600)
                        return

                    if not confirmed:
                        return

        self.record_concubine_cd("heart_trial")

    # ---- 主循环 ----

    async def run_target_concubine_loop(self, initial_delay=0):
        """主号主魂/无咎子寻找指定侍妾；找到前不跑普通侍妾链。"""
        await self.startup_done.wait()
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while self.is_running:
            try:
                identities = self.target_concubine_identities()
                if not identities:
                    await asyncio.sleep(3600)
                    continue
                waits = []
                all_found = True
                for identity in identities:
                    await self.pause_event.wait()
                    if self.target_concubine_found(identity):
                        continue
                    all_found = False
                    await self.execute_target_concubine_search(identity)
                    state = self.ensure_target_concubine_state(identity)
                    next_time = state.get("next_concubine_search_time", "")
                    if next_time and is_future(next_time):
                        waits.append(seconds_until(next_time))
                    elif not self.target_concubine_found(identity):
                        waits.append(CONCUBINE_SEARCH_RETRY_SECONDS)
                    await asyncio.sleep(3)
                if all_found:
                    sleep_for = 600
                elif waits:
                    sleep_for = max(60, min(waits) + random.randint(15, 45))
                else:
                    sleep_for = 300
                log.info(f"Target concubine loop complete. Sleep {int(sleep_for)}s.")
                await asyncio.sleep(sleep_for)
            except Exception as e:
                log.error(f"Target concubine loop error: {e}")
                await asyncio.sleep(300)

    async def run_concubine_loop(self):
        """
        侍妾功能主循环。
        固定批次：远航归来 → 天机代卜 → 入梦寻图 → 共历心劫 → 主号主魂月殿寻痕。
        全部有冷却时等待最短的冷却时间。
        """
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(20, 60))
        await self.sync_concubine_status_if_needed()

        while self.is_running:
            self.ensure_concubine_state()
            # 主循环只检查冷却，不主动切回主魂；实际发送时再对齐身份。
            if hasattr(self, '_wait_for_main_identity'):
                await self._wait_for_main_identity()
            try:
                if await self.execute_concubine_chain():
                    await asyncio.sleep(random.randint(8, 18))
                    continue

                waits = [
                    seconds_until(self.state.get(task["state_key"], ""))
                    for task_key, task in CONCUBINE_TASKS.items()
                    if self.concubine_task_enabled(task_key, "主魂")
                    if self.state.get(task["state_key"], "") and is_future(self.state[task["state_key"]])
                ]
                if self.target_concubine_enabled("主魂") and not self.target_concubine_found("主魂"):
                    target_state = self.ensure_target_concubine_state("主魂")
                    target_time = target_state.get("next_concubine_search_time", "")
                    if target_time and is_future(target_time):
                        waits.append(seconds_until(target_time))
                sleep_for = max(60, min(waits) + random.randint(15, 45)) if waits else 300
                log.info(f"Concubine loop complete. Sleep {int(sleep_for)}s.")
                await asyncio.sleep(sleep_for)
            except Exception as e:
                log.error(f"Concubine loop error: {e}")
                await asyncio.sleep(300)
