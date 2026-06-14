#!/usr/bin/env python3
"""
【通用固定冷却指令模块 —— 所有账号脚本共享】

提供 CommonCommandMixin 混入类，封装所有账号通用的固定冷却指令：
  1. 野外历练 —— 定时外出历练，策略可配置（谨慎/均衡/深入）
  2. 宗门战况/参战 —— 自动检测宗门战役，参战获取军勋
  3. 固定冷却指令的记录与重试逻辑

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 继承使用。
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

from log_utils import (
    actor_account_key,
    avatar_marker_identity_from_text,
    dashboard_command_disabled,
    identity_plain_usernames,
    is_game_bot_sender,
    is_reply_to_untracked_message,
    is_yuanying_rebirth_block_response,
    is_yuanying_rebirth_success_response,
    notify_unrecognized_response,
    text_targets_current_account,
    text_username_mentions,
    tracked_command_identity_for_reply,
)


# =====================================================================
# 常量定义
# =====================================================================
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
COMMAND_CONTROL_FILE = os.path.join(CONFIG_DIR, "command_controls.json")
CUSTOM_COMMAND_FILE = os.path.join(CONFIG_DIR, "dashboard_commands.json")
FIELD_TRAINING_COMMAND = ".野外历练 谨慎"      # 野外历练指令（各账号可覆盖）
FIELD_TRAINING_CD_SECONDS = 2 * 3600           # 野外历练冷却 2 小时
FIELD_TRAINING_MISSING_RESPONSE_RETRY_SECONDS = 0  # 空回复时下一轮立即重试
YUANYING_REBIRTH_PENDING_PAUSE_SECONDS = 30 * 60  # 已可夺舍但未重生时，短暂停自动主魂指令
SECT_WAR_STATUS_COMMAND = ".宗门战况"           # 查询宗门战况
SECT_WAR_JOIN_COMMAND = ".参战"                 # 参战指令
SECT_WAR_JOIN_CD_SECONDS = 2 * 3600            # 参战冷却 2 小时
SECT_WAR_RETRY_SECONDS = 10 * 60               # 宗门战重试间隔 10 分钟
TIME_CRITICAL_COMMAND_PREFIXES = (
    ".灵树灌溉",
    ".协同守山",
    ".采摘灵果",
    ".观星",
    ".改换星移",
    ".观命",
    ".定命",
    ".助阵",
)

STATE_TIME_COMMAND_MAP = {
    "next_rift_search_time": ".探寻裂缝",
    "next_yuanying_out_time": ".元婴出窍",
    "next_treasure_touch_time": ".抚摸法宝 青竹蜂云剑",
    "next_field_training_time": ".野外历练 谨慎",
    "next_meditation_time": ".闭关修炼",
    "next_meditation_retry_time": ".闭关修炼",
    "next_dream_map_time": ".入梦寻图",
    "next_heart_trial_time": ".共历心劫",
    "next_divination_time": ".天机代卜",
    "next_concubine_voyage_time": ".侍妾远航 冒险",
    "next_tower_time": ".闯塔",
    "next_stairs_time": ".登天阶",
    "next_heart_platform_time": ".问心台",
    "nine_heaven_wind_cd_time": ".引九天罡风",
    "heart_platform_time": ".问心台",
    "next_heart_time": ".问心台",
    "next_formation_time": ".启阵",
    "next_formation_retry_time": ".启阵",
    "next_force_exit_time": ".强行出关",
    "next_nurture_spirit_time": ".温养器灵 青竹蜂云剑（神雷版）",
    "next_spirit_tree_irrigation_time": ".灵树灌溉",
    "next_spirit_tree_guard_time": ".协同守山",
    "next_star_gazing_time": ".观星",
    "pending_star_gazing_target_time": ".观星",
    "pending_star_shift_target_time": ".改换星移",
    "next_star_palace_time": ".观星台",
    "next_star_check_time": ".观星台",
    "next_star_appease_time": ".安抚星辰",
    "next_star_collect_time": ".收集精华",
    "next_star_attraction_time": ".牵引星辰 天雷星",
    "star_attraction_retry_time": ".牵引星辰 天雷星",
    "next_steal_time": ".灵兽偷菜",
    "next_beast_status_check_time": ".我的灵兽",
    "next_abyss_time": ".探渊 <灵兽>",
    "next_pasture_time": ".一键放养",
    "next_beast_interaction_time": ".灵兽互动 六翼",
    "next_beast_cruise_time": ".灵兽巡游 六翼",
    "next_ask_dao_time": ".问道",
}

# 已知宗门列表（用于解析宗门战双方）
KNOWN_SECTS = (
    "凌霄宫", "星宫", "万灵宗", "元婴宗", "天星宗", "黄枫谷",
    "掩月宗", "落云宗", "古剑门", "百巧院", "鬼灵门",
    "合欢宗", "御灵宗", "天道盟", "九国盟",
)


# =====================================================================
# 时间工具函数
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

def add_seconds_str(value, seconds):
    return dt_to_str(str_to_dt(value) + timedelta(seconds=seconds))

def is_future(value):
    try:
        return str_to_dt(value) > datetime.now()
    except Exception:
        return False

def seconds_until(value):
    try:
        return max(0, (str_to_dt(value) - datetime.now()).total_seconds())
    except Exception:
        return 0


# =====================================================================
# 默认状态数据
# =====================================================================

def common_command_default_state():
    """返回通用命令的默认状态字典"""
    return {
        "last_field_training_time": "",
        "next_field_training_time": "",
        "sect_name": "",
        "sect_war_left": "",
        "sect_war_right": "",
        "sect_war_active_until": "",
        "last_sect_war_status_time": "",
        "next_sect_war_status_time": "",
        "last_sect_war_join_time": "",
        "next_sect_war_join_time": "",
        "last_sect_war_response": "",
        "custom_command_runs": {},
        "identity_pauses": {},
    }


# =====================================================================
# CommonCommandMixin 混入类
# =====================================================================

class CommonCommandMixin:
    """通用固定冷却指令混入类。"""

    # ---- 状态管理 ----

    def ensure_common_command_state(self):
        """确保状态字典包含所有默认键，并在 sect_name 变更时更新"""
        changed = False
        for key, value in common_command_default_state().items():
            if key not in self.state:
                self.state[key] = value
                changed = True
        sect = (getattr(self, "sect_name", "") or "").strip()
        if sect and self.state.get("sect_name") != sect:
            self.state["sect_name"] = sect
            changed = True
        if changed:
            self.save_state()

    def common_command_logger(self):
        """获取子类的日志记录器"""
        return logging.getLogger(self.__class__.__name__)

    # ---- 身份级暂停（元婴虚弱等）----

    def ensure_identity_pause_state(self):
        pauses = self.state.get("identity_pauses")
        if not isinstance(pauses, dict):
            pauses = {}
            self.state["identity_pauses"] = pauses
            self.save_state()
        return pauses

    def identity_pause_entry(self, identity):
        identity = str(identity or "主魂").strip() or "主魂"
        pauses = self.ensure_identity_pause_state()
        entry = pauses.get(identity)
        if not isinstance(entry, dict):
            entry = {}
        if identity == "主魂" and not entry.get("until") and self.state.get("main_soul_pause_until"):
            entry = {
                "until": self.state.get("main_soul_pause_until", ""),
                "reason": self.state.get("main_soul_pause_reason", ""),
            }
            pauses[identity] = entry
        return entry

    def identity_pause_seconds(self, identity="主魂"):
        identity = str(identity or "主魂").strip() or "主魂"
        entry = self.identity_pause_entry(identity)
        pause_until = entry.get("until", "")
        if not pause_until:
            return 0
        try:
            remaining = int((str_to_dt(pause_until) - datetime.now()).total_seconds())
        except Exception:
            remaining = 0
        if remaining > 0:
            return remaining

        pauses = self.ensure_identity_pause_state()
        if identity in pauses:
            pauses.pop(identity, None)
            if identity == "主魂":
                self.state["main_soul_pause_until"] = ""
                self.state["main_soul_pause_reason"] = ""
            self.save_state()
            self.common_command_logger().info(f"Identity pause expired for [{identity}].")
        return 0

    def set_identity_pause(self, identity, seconds, reason):
        identity = str(identity or "主魂").strip() or "主魂"
        seconds = max(0, int(seconds or 0))
        pause_until = add_seconds_str(now_str(), seconds)
        pauses = self.ensure_identity_pause_state()
        pauses[identity] = {
            "until": pause_until,
            "reason": str(reason or "").strip(),
        }
        if identity == "主魂":
            self.state["main_soul_pause_until"] = pause_until
            self.state["main_soul_pause_reason"] = str(reason or "").strip()
        self.save_state()
        return pause_until

    def set_identity_pause_until(self, identity, pause_until, reason):
        identity = str(identity or "主魂").strip() or "主魂"
        pause_until = str(pause_until or "").strip()
        pauses = self.ensure_identity_pause_state()
        pauses[identity] = {
            "until": pause_until,
            "reason": str(reason or "").strip(),
        }
        if identity == "主魂":
            self.state["main_soul_pause_until"] = pause_until
            self.state["main_soul_pause_reason"] = str(reason or "").strip()
        self.save_state()
        return pause_until

    def clear_identity_pause(self, identity="主魂", reason=""):
        identity = str(identity or "主魂").strip() or "主魂"
        pauses = self.ensure_identity_pause_state()
        old_entry = pauses.pop(identity, None)
        changed = old_entry is not None
        if identity == "主魂":
            if self.state.get("main_soul_pause_until") or self.state.get("main_soul_pause_reason"):
                changed = True
            self.state["main_soul_pause_until"] = ""
            self.state["main_soul_pause_reason"] = ""
        if changed:
            self.save_state()
            suffix = f" ({reason})" if reason else ""
            self.common_command_logger().info(f"Identity pause cleared for [{identity}]{suffix}.")
        return changed

    def clear_identity_command_guard(self, identity="主魂", commands=None, reason=""):
        identity = str(identity or "主魂").strip() or "主魂"
        guard = getattr(self, "_command_send_guard", None)
        if not isinstance(guard, dict) or not guard:
            return 0

        def key_for(command):
            command = str(command or "").strip()
            return f"{command} ({identity})" if identity != "主魂" else command

        if commands:
            remove_keys = {key_for(command) for command in commands if str(command or "").strip()}
        elif identity == "主魂":
            remove_keys = {key for key in guard if not re.search(r"\s\([^)]+\)$", str(key))}
        else:
            suffix = f" ({identity})"
            remove_keys = {key for key in guard if str(key).endswith(suffix)}

        removed = 0
        for key in list(remove_keys):
            if key in guard:
                guard.pop(key, None)
                removed += 1
        if removed:
            setattr(self, "_command_send_guard", guard)
            block = getattr(self, "_last_command_guard_block", None)
            if isinstance(block, dict):
                block_key = str(block.get("key") or "")
                block_identity = str(block.get("identity") or "")
                if block_key in remove_keys or (identity != "主魂" and block_identity == identity):
                    setattr(self, "_last_command_guard_block", {})
            suffix = f" ({reason})" if reason else ""
            self.common_command_logger().info(
                f"Cleared {removed} command guard entr{'y' if removed == 1 else 'ies'} "
                f"for [{identity}]{suffix}."
            )
        return removed

    def _identity_state_for_update(self, identity):
        identity = str(identity or "主魂").strip() or "主魂"
        if identity != "主魂" and hasattr(self, "get_avatar_state"):
            return self.get_avatar_state(identity)
        return self.state

    def _set_identity_state_value(self, identity, key, value):
        identity = str(identity or "主魂").strip() or "主魂"
        if identity != "主魂" and hasattr(self, "set_avatar_state"):
            self.set_avatar_state(identity, key, value)
        else:
            self.state[key] = value

    def _field_training_due_after_recovery(self, identity):
        state = self._identity_state_for_update(identity)
        floor = self._future_time_from_last(
            state.get("last_field_training_time", ""),
            FIELD_TRAINING_CD_SECONDS,
        )
        next_time = state.get("next_field_training_time", "")
        if not floor and (not next_time or is_future(next_time)):
            self._set_identity_state_value(identity, "next_field_training_time", now_str())

    def record_identity_yuanying_recovery_from_text(self, identity, text, source="", command=""):
        identity = str(identity or "主魂").strip() or "主魂"
        log = self.common_command_logger()
        if is_yuanying_rebirth_success_response(text):
            self.clear_identity_pause(identity, reason=f"{source or command or 'rebirth success'}")
            self.clear_identity_command_guard(identity, reason="yuanying rebirth success")
            if identity == "主魂":
                self.state["yuanying_out_active"] = False
                self.state["yuanying_out_end_time"] = ""
                self.state["last_yuanying_return_time"] = now_str()
            self._field_training_due_after_recovery(identity)
            self.save_state()
            log.info(f"Yuanying rebirth success synced for [{identity}] from {source or command or 'message'}.")
            return True

        if not is_yuanying_rebirth_block_response(text):
            return False

        current_remaining = self.identity_pause_seconds(identity)
        if current_remaining > YUANYING_REBIRTH_PENDING_PAUSE_SECONDS:
            pause_until = self.identity_pause_entry(identity).get("until", "")
        else:
            pause_until = self.set_identity_pause(
                identity,
                YUANYING_REBIRTH_PENDING_PAUSE_SECONDS,
                "元婴虚弱/待夺舍重生",
            )
        self.clear_identity_command_guard(identity, reason="yuanying rebirth pending")
        self._set_identity_state_value(identity, "next_field_training_time", pause_until)
        self.save_state()
        log.warning(
            f"Yuanying rebirth pending for [{identity}] from {source or command or 'message'}; "
            f"auto commands paused until {pause_until}."
        )
        return True

    async def wait_while_identity_paused(self, identity, command=""):
        """Wait outside physical send locks while one identity is paused."""
        identity = str(identity or "主魂").strip() or "主魂"
        last_log = 0.0
        while getattr(self, "is_running", True):
            remaining = self.identity_pause_seconds(identity)
            if remaining <= 0:
                return True
            now_mono = time.monotonic()
            if now_mono - last_log > 300:
                entry = self.identity_pause_entry(identity)
                reason = entry.get("reason") or "身份暂停"
                until = entry.get("until", "")
                self.common_command_logger().info(
                    f"Identity [{identity}] command paused ({command or 'unknown'}): "
                    f"{reason}; resume at {until}."
                )
                last_log = now_mono
            await asyncio.sleep(max(5, min(int(remaining), 300)))
        return False

    async def sleep_if_identity_paused(self, identity, loop_name="Identity loop"):
        """Skip one scheduler iteration for a paused identity without holding locks."""
        identity = str(identity or "主魂").strip() or "主魂"
        remaining = self.identity_pause_seconds(identity)
        if remaining <= 0:
            return False
        entry = self.identity_pause_entry(identity)
        reason = entry.get("reason") or "身份暂停"
        until = entry.get("until", "")
        self.common_command_logger().info(f"{loop_name} [{identity}] paused: {reason}; resume at {until}.")
        await asyncio.sleep(max(30, min(int(remaining), 300)))
        return True

    def _future_time_from_last(self, last_time, cd_seconds):
        """Return last_time + cd_seconds only when it is a valid future time."""
        if not last_time:
            return ""
        try:
            due = datetime.strptime(str(last_time), TIME_FORMAT) + timedelta(seconds=int(cd_seconds))
        except Exception:
            return ""
        return dt_to_str(due) if due > datetime.now() else ""

    def _latest_future_time(self, *values):
        """Return the latest valid future time from a list of state strings."""
        candidates = []
        for value in values:
            if not value:
                continue
            try:
                dt = datetime.strptime(str(value), TIME_FORMAT)
            except Exception:
                continue
            if dt > datetime.now():
                candidates.append((dt, str(value)))
        if not candidates:
            return ""
        return max(candidates, key=lambda item: item[0])[1]

    def preserve_cooldown_floor(self, state, last_key, next_key, cd_seconds, reason=""):
        """
        Keep next_key at least last_key + cd_seconds.

        This prevents a stale empty/short retry state from making fixed-cooldown
        commands fire before the last confirmed success cooldown has elapsed.
        """
        if not isinstance(state, dict):
            return ""
        floor = self._future_time_from_last(state.get(last_key, ""), cd_seconds)
        current = state.get(next_key, "")
        next_time = self._latest_future_time(current, floor)
        if floor and next_time and next_time != current:
            state[next_key] = next_time
            self.save_state()
            log = self.common_command_logger()
            detail = f" ({reason})" if reason else ""
            log.info(f"{next_key}: repaired cooldown floor{detail}, next at {next_time}.")
        return next_time

    def set_retry_preserving_cooldown(self, state, last_key, next_key, cd_seconds, retry_seconds, reason=""):
        """Set a retry time without moving a fixed-cooldown command before its floor."""
        if not isinstance(state, dict):
            return ""
        retry_at = add_seconds_str(now_str(), retry_seconds)
        floor = self._future_time_from_last(state.get(last_key, ""), cd_seconds)
        next_time = self._latest_future_time(state.get(next_key, ""), floor, retry_at) or retry_at
        state[next_key] = next_time
        log = self.common_command_logger()
        detail = f" ({reason})" if reason else ""
        if floor and next_time == floor:
            log.info(f"{next_key}: retry preserved cooldown floor{detail}, next at {next_time}.")
        return next_time

    def meditation_cultivation_retry_seconds(self, text):
        """Return retry seconds when .闭关修炼 replies with a cultivation cooldown."""
        clean = str(text or "").replace("**", "").replace(" ", "")
        if not clean or "闭关" not in clean:
            return 0
        if "正在深度闭关" in clean or "已在深度闭关" in clean:
            return 0
        cooldown_markers = (
            "需要打坐",
            "调息",
            "方可再次闭关",
            "方可进行下一次",
            "冷却",
            "无法立即",
            "尚未平复",
            "心浮气躁",
        )
        if not any(marker in clean for marker in cooldown_markers):
            return 0
        try:
            retry_seconds = self.parse_wait_time(text, line_identifier="闭关")
        except TypeError:
            retry_seconds = self.parse_wait_time(text)
        if retry_seconds <= 0:
            retry_seconds = self.parse_wait_time(text)
        return retry_seconds if retry_seconds > 0 else 600

    def defer_meditation_after_cultivation_cooldown(self, identity, text, source=".闭关修炼"):
        """.闭关修炼 rest text is informational; deep meditation must still start immediately."""
        retry_seconds = self.meditation_cultivation_retry_seconds(text)
        if retry_seconds <= 0:
            return 0

        identity = str(identity or "主魂").strip() or "主魂"
        self.common_command_logger().info(
            f"[{identity}] {source}: cultivation cooldown detected; "
            f"continuing to .深度闭关 immediately."
        )
        return 0

    def meditation_defer_until(self, state):
        """Return a future deep-meditation retry time in a state dict."""
        if not isinstance(state, dict):
            return ""
        value = state.get("next_meditation_retry_time", "")
        return value if value and is_future(value) else ""

    # ---- Dashboard 自定义指令调度 ----

    def load_dashboard_custom_commands(self):
        """读取 dashboard 自定义指令配置。"""
        try:
            with open(CUSTOM_COMMAND_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.common_command_logger().warning(f"Custom command config load failed: {exc}")
            return {}

    def dashboard_custom_command_entries(self):
        account = actor_account_key(self)
        if not account:
            return []
        account_data = self.load_dashboard_custom_commands().get(account, {})
        if not isinstance(account_data, dict):
            return []
        entries = []
        for identity, rows in account_data.items():
            identity = str(identity or "主魂").strip() or "主魂"
            if identity != "主魂" and identity not in getattr(self, "avatars", []):
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    entries.append((identity, row))
        return entries

    def clean_custom_command_id(self, entry):
        custom_id = str(entry.get("id") or "").strip()
        if custom_id:
            return custom_id
        identity = str(entry.get("identity") or "").strip()
        command = str(entry.get("command") or "").strip()
        return f"{identity}:{command}" if command else ""

    def custom_command_interval_seconds(self, entry):
        try:
            minutes = float(entry.get("interval_minutes") or 0)
        except Exception:
            minutes = 0
        if minutes <= 0:
            return 0
        return max(60, int(minutes * 60))

    def custom_command_timeout_seconds(self, entry):
        try:
            seconds = int(entry.get("timeout_seconds") or 45)
        except Exception:
            seconds = 45
        return max(10, min(180, seconds))

    def custom_command_max_retries(self, entry):
        try:
            retries = int(entry.get("max_retries") or 0)
        except Exception:
            retries = 0
        return max(0, min(2, retries))

    def custom_command_enabled(self, entry):
        if entry.get("schedule_enabled") is False:
            return False
        return bool(str(entry.get("command") or "").strip().startswith(".") and self.custom_command_interval_seconds(entry) > 0)

    def ensure_custom_command_runs(self):
        self.ensure_common_command_state()
        runs = self.state.get("custom_command_runs")
        if not isinstance(runs, dict):
            runs = {}
            self.state["custom_command_runs"] = runs
            self.save_state()
        return runs

    def custom_command_run_state(self, custom_id):
        runs = self.ensure_custom_command_runs()
        state = runs.get(custom_id)
        if not isinstance(state, dict):
            state = {}
            runs[custom_id] = state
        return state

    def custom_command_next_wait(self, entry):
        custom_id = self.clean_custom_command_id(entry)
        if not custom_id or not self.custom_command_enabled(entry):
            return -1
        run_state = self.custom_command_run_state(custom_id)
        next_run_at = run_state.get("next_run_at", "")
        if next_run_at:
            return seconds_until(next_run_at) if is_future(next_run_at) else 0
        return 0

    def custom_command_impending_wait(self, identity):
        """Return seconds until a custom command is due for identity, or -1."""
        min_wait = None
        for entry_identity, entry in self.dashboard_custom_command_entries():
            if entry_identity != identity:
                continue
            command = str(entry.get("command") or "").strip()
            if not command or dashboard_command_disabled(self, command, entry_identity)[0]:
                continue
            wait = self.custom_command_next_wait(entry)
            if wait >= 0:
                min_wait = wait if min_wait is None else min(min_wait, wait)
        return min_wait if min_wait is not None else -1

    def merge_impending_wait(self, base_wait, extra_wait):
        if extra_wait is None or extra_wait < 0:
            return base_wait
        if base_wait is None or base_wait < 0 or base_wait >= 999999:
            return extra_wait
        return min(base_wait, extra_wait)

    def state_time_command_for_key(self, key):
        return STATE_TIME_COMMAND_MAP.get(str(key or ""))

    def command_matches_prefix(self, command, prefix):
        command = str(command or "").strip()
        prefix = str(prefix or "").strip()
        return bool(command and prefix and (command == prefix or command.startswith(f"{prefix} ")))

    def time_critical_identity_command(self, command):
        return any(
            self.command_matches_prefix(command, prefix)
            for prefix in TIME_CRITICAL_COMMAND_PREFIXES
        )

    def latest_command_sent_at(self, identity, command):
        cache = getattr(self, "_last_command_sent_at_by_identity_command", None)
        if not isinstance(cache, dict):
            return ""
        return cache.get((str(identity or "主魂"), str(command or "").strip()), "")

    def time_critical_identity_wait(self, identity, exclude_command=""):
        identity = str(identity or "主魂").strip() or "主魂"
        exclude_command = str(exclude_command or "").strip()
        states = []
        if identity == "主魂":
            states.append(getattr(self, "state", {}))
        elif identity in (getattr(self, "avatars", []) or []) and hasattr(self, "get_avatar_state"):
            states.append(self.get_avatar_state(identity))

        candidates = []
        for state in states:
            if not isinstance(state, dict):
                continue
            for key, value in state.items():
                command = self.state_time_command_for_key(key)
                if not command or not self.time_critical_identity_command(command):
                    continue
                if exclude_command and self.command_matches_prefix(exclude_command, command):
                    continue
                if self.state_time_command_paused(key, identity):
                    continue
                if isinstance(value, str) and value:
                    candidates.append(seconds_until(value) if is_future(value) else 0)

            if (
                (state.get("spirit_tree_guard_pending") or state.get("spirit_tree_invasion_status"))
                and not self.command_matches_prefix(exclude_command, ".协同守山")
                and not self.dashboard_command_paused(".协同守山", identity)
            ):
                candidates.append(0)
            if (
                state.get("spirit_tree_harvest_pending")
                and not self.command_matches_prefix(exclude_command, ".采摘灵果")
                and not self.dashboard_command_paused(".采摘灵果", identity)
            ):
                candidates.append(0)

        if (
            hasattr(self, "get_spirit_tree_irrigation_time")
            and not self.command_matches_prefix(exclude_command, ".灵树灌溉")
            and not self.dashboard_command_paused(".灵树灌溉", identity)
        ):
            value = self.get_spirit_tree_irrigation_time(identity)
            if value:
                candidates.append(seconds_until(value) if is_future(value) else 0)

        return min(candidates) if candidates else -1

    def time_critical_defer_wait(self, identity, command, timeout=45):
        if self.time_critical_identity_command(command):
            return -1
        wait = self.time_critical_identity_wait(identity, exclude_command=command)
        if wait < 0:
            return -1
        try:
            timeout = float(timeout)
        except Exception:
            timeout = 45
        window = min(120, max(10, timeout + 5))
        return wait if wait <= window else -1

    def state_time_command_paused(self, key, identity=""):
        command = self.state_time_command_for_key(key)
        if not command:
            return False
        return dashboard_command_disabled(self, command, identity or "主魂")[0]

    def dashboard_command_paused(self, command, identity=""):
        return dashboard_command_disabled(self, command, identity or "主魂")[0]

    def dashboard_command_control_mtime(self):
        """Return command_controls.json mtime; 0 means missing/unreadable."""
        try:
            return os.path.getmtime(COMMAND_CONTROL_FILE)
        except OSError:
            return 0.0

    async def wait_for_dashboard_command_control_change(self, timeout, poll_interval=2):
        """Sleep until dashboard command controls change or timeout expires."""
        try:
            timeout = float(timeout)
        except Exception:
            timeout = 0
        if timeout <= 0:
            return False

        started_mtime = self.dashboard_command_control_mtime()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(float(poll_interval), remaining))
            current_mtime = self.dashboard_command_control_mtime()
            if current_mtime != started_mtime:
                return True

    def latest_command_block(self, command=None, max_age=120):
        block = getattr(self, "_last_command_guard_block", None)
        if not isinstance(block, dict):
            return {}
        try:
            if time.monotonic() - float(block.get("at") or 0) > max_age:
                return {}
        except Exception:
            return {}
        if command:
            key = str(block.get("key") or "")
            command = str(command or "")
            if key and command and key != command and not key.startswith(f"{command} "):
                return {}
        return block

    async def sleep_after_blocked_command(self, command, context="", default_seconds=300):
        block = self.latest_command_block(command)
        if not block:
            return False
        reason = str(block.get("reason") or "")
        wait = int(block.get("wait") or 0)
        if reason == "dashboard_disabled":
            wait = wait or default_seconds
        elif reason in {"command_guard", "bot_health", "identity_pause"}:
            wait = wait or 60
        elif reason == "disabled":
            wait = default_seconds
        else:
            return False
        sleep_seconds = max(30, min(wait, 600))
        log = self.common_command_logger()
        log.info(
            f"{context or command}: blocked by {reason}; "
            f"backing off {sleep_seconds}s."
        )
        if reason == "dashboard_disabled":
            identity = str(block.get("identity") or getattr(self, "current_identity", "主魂") or "主魂")
            if not self.dashboard_command_paused(command, identity):
                log.info(f"{context or command}: dashboard command is enabled again; rechecking now.")
                return True
            if await self.wait_for_dashboard_command_control_change(sleep_seconds):
                log.info(f"{context or command}: dashboard command controls changed; rechecking now.")
            return True
        await asyncio.sleep(sleep_seconds)
        return True

    def record_custom_command_result(self, entry, identity, response_text, status):
        custom_id = self.clean_custom_command_id(entry)
        if not custom_id:
            return
        interval = self.custom_command_interval_seconds(entry)
        now = now_str()
        state = self.custom_command_run_state(custom_id)
        state["identity"] = identity
        state["command"] = str(entry.get("command") or "").strip()
        state["last_run_at"] = now
        state["last_status"] = status
        state["last_response"] = (response_text or "").strip()[:500]
        delay = interval
        if status == "no_response":
            delay = min(interval, 10 * 60) if interval else 10 * 60
        state["next_run_at"] = add_seconds_str(now, delay)
        self.save_state()

    async def execute_custom_command(self, identity, entry):
        command = str(entry.get("command") or "").strip()
        if not command:
            return
        if self.identity_pause_seconds(identity) > 0:
            return
        log = self.common_command_logger()
        timeout = self.custom_command_timeout_seconds(entry)
        retries = self.custom_command_max_retries(entry)
        label = str(entry.get("label") or command).strip()
        log.info(f"Custom command due [{identity}] {label}: {command}")
        if identity == "主魂":
            resp = await self.send_and_wait_feedback(command, timeout=timeout, max_retries=retries)
        else:
            resp = await self.send_and_wait_feedback_identity(identity, command, timeout=timeout, max_retries=retries)
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else resp if isinstance(resp, str) else ""
        status = "responded" if resp_text else "no_response"
        self.record_custom_command_result(entry, identity, resp_text, status)
        log.info(f"Custom command done [{identity}] {command}: {status}")

    async def run_custom_command_loop(self):
        """Dashboard 自定义指令调度循环。"""
        self.ensure_common_command_state()
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(15, 45))
        log = self.common_command_logger()

        while self.is_running:
            try:
                self.ensure_common_command_state()
                next_sleep = 300
                ran_any = False
                for identity, entry in self.dashboard_custom_command_entries():
                    if not self.custom_command_enabled(entry):
                        continue
                    if self.identity_pause_seconds(identity) > 0:
                        next_sleep = min(next_sleep, 60)
                        continue
                    command = str(entry.get("command") or "").strip()
                    custom_id = self.clean_custom_command_id(entry)
                    if not command or not custom_id:
                        continue
                    if dashboard_command_disabled(self, command, identity)[0]:
                        state = self.custom_command_run_state(custom_id)
                        if state.get("last_status") != "paused":
                            state["identity"] = identity
                            state["command"] = command
                            state["last_status"] = "paused"
                            self.save_state()
                        next_sleep = min(next_sleep, 60)
                        continue

                    wait = self.custom_command_next_wait(entry)
                    if wait > 0:
                        next_sleep = min(next_sleep, wait)
                        continue
                    if wait < 0:
                        continue

                    await self.execute_custom_command(identity, entry)
                    ran_any = True
                    await asyncio.sleep(3)

                if ran_any:
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(max(10, min(int(next_sleep), 300)))
            except Exception as exc:
                log.error(f"Custom command loop error: {exc}", exc_info=True)
                await asyncio.sleep(60)

    # ---- 野外历练 ----

    def is_field_training_response(self, text):
        """判断游戏回复是否为野外历练相关的消息"""
        clean = (text or "").replace("**", "")
        return "野外历练" in clean or "山中灵机未复" in clean

    def is_field_training_command_result(self, text):
        """判断已归属到野外历练指令的回复是否代表本轮有结果。"""
        clean = (text or "").replace("**", "")
        return self.is_field_training_response(text) or ("卦象" in clean and "修为增加" in clean)

    def is_field_training_cooldown_response(self, text):
        """判断回复是否为野外历练冷却中"""
        clean = (text or "").replace("**", "")
        return any(k in clean for k in ["山中灵机未复", "冷却", "后再", "尚未", "请在"])

    def record_field_training_response(self, text, context="野外历练"):
        """
        记录野外历练的回复结果。
        解析冷却时间或成功状态，更新 next_field_training_time。
        """
        self.ensure_common_command_state()
        log = self.common_command_logger()
        if not text:
            self.set_retry_preserving_cooldown(
                self.state,
                "last_field_training_time",
                "next_field_training_time",
                FIELD_TRAINING_CD_SECONDS,
                FIELD_TRAINING_MISSING_RESPONSE_RETRY_SECONDS,
                "missing response",
            )
            self.save_state()
            log.warning(f"Field training: missing response; retry at {self.state['next_field_training_time']}.")
            return False

        if self.record_identity_yuanying_recovery_from_text(
            "主魂", text, source=context, command=FIELD_TRAINING_COMMAND
        ):
            return False

        cd = self.parse_wait_time(text)
        now = now_str()
        if self.is_field_training_cooldown_response(text) and cd > 0:
            cooldown_at = add_seconds_str(now, cd)
            floor = self._future_time_from_last(
                self.state.get("last_field_training_time", ""),
                FIELD_TRAINING_CD_SECONDS,
            )
            self.state["next_field_training_time"] = self._latest_future_time(cooldown_at, floor) or cooldown_at
            self.save_state()
            log.info(f"Field training cooldown from response: {cd}s, next at {self.state['next_field_training_time']}.")
            return True

        if self.is_field_training_command_result(text):
            self.state["last_field_training_time"] = now
            self.state["next_field_training_time"] = add_seconds_str(now, FIELD_TRAINING_CD_SECONDS)
            self.save_state()
            log.info(f"Field training recorded. Next at {self.state['next_field_training_time']}.")
            return True

        # 无法识别的回复：重试 10 分钟后
        notify_unrecognized_response(self, FIELD_TRAINING_COMMAND, text, log, context)
        self.set_retry_preserving_cooldown(
            self.state,
            "last_field_training_time",
            "next_field_training_time",
            FIELD_TRAINING_CD_SECONDS,
            600,
            "unrecognized response",
        )
        self.save_state()
        log.warning(f"Field training unrecognized response; skipped until {self.state['next_field_training_time']}.")
        return False

    def _field_training_identity_from_text(self, text):
        """Infer which execution identity a field-training result belongs to from @mentions."""
        mentions = set(text_username_mentions(text or ""))
        if not mentions:
            return ""
        candidates = list(getattr(self, "avatars", []) or []) + ["主魂"]
        for identity in candidates:
            known = identity_plain_usernames(self, identity)
            if known and mentions.intersection(known):
                return identity
        return ""

    def _field_training_identity_from_reply(self, msg, text):
        """Prefer reply-to attribution, then explicit avatar marker, then @username mapping."""
        identity = tracked_command_identity_for_reply(self, msg)
        if identity:
            return identity
        identity = avatar_marker_identity_from_text(text)
        if identity:
            return identity
        return self._field_training_identity_from_text(text)

    def record_identity_field_training_response(self, identity, text, context="野外历练"):
        """
        Record field-training cooldown for a specific identity.

        Avatar commands must not copy the top-level cooldown because the top-level
        state can belong to main-soul/manual passive sync from another identity.
        """
        identity = str(identity or "").strip() or "主魂"
        if identity == "主魂" or not hasattr(self, "set_avatar_state"):
            return self.record_field_training_response(text, context)

        log = self.common_command_logger()
        now = now_str()

        def update_avatar(values):
            if hasattr(self, "update_avatar_states"):
                self.update_avatar_states(identity, values)
            else:
                for key, value in values.items():
                    self.set_avatar_state(identity, key, value)

        if not text:
            next_time = self.set_retry_preserving_cooldown(
                self.get_avatar_state(identity),
                "last_field_training_time",
                "next_field_training_time",
                FIELD_TRAINING_CD_SECONDS,
                FIELD_TRAINING_MISSING_RESPONSE_RETRY_SECONDS,
                f"missing response [{identity}]",
            )
            update_avatar({"next_field_training_time": next_time})
            log.warning(f"Avatar [{identity}] field training: missing response; retry at {next_time}.")
            return False

        if self.record_identity_yuanying_recovery_from_text(
            identity, text, source=context, command=FIELD_TRAINING_COMMAND
        ):
            return False

        cd = self.parse_wait_time(text)
        if self.is_field_training_cooldown_response(text) and cd > 0:
            cooldown_at = add_seconds_str(now, cd)
            floor = self._future_time_from_last(
                self.get_avatar_state(identity).get("last_field_training_time", ""),
                FIELD_TRAINING_CD_SECONDS,
            )
            next_time = self._latest_future_time(cooldown_at, floor) or cooldown_at
            update_avatar({"next_field_training_time": next_time})
            log.info(f"Avatar [{identity}] field training cooldown from response: {cd}s, next at {next_time}.")
            return True

        if self.is_field_training_command_result(text):
            next_time = add_seconds_str(now, FIELD_TRAINING_CD_SECONDS)
            update_avatar({
                "last_field_training_time": now,
                "next_field_training_time": next_time,
            })
            log.info(f"Avatar [{identity}] field training recorded. Next at {next_time}.")
            return True

        notify_unrecognized_response(self, FIELD_TRAINING_COMMAND, text, log, f"{context} ({identity})")
        next_time = self.set_retry_preserving_cooldown(
            self.get_avatar_state(identity),
            "last_field_training_time",
            "next_field_training_time",
            FIELD_TRAINING_CD_SECONDS,
            600,
            f"unrecognized response [{identity}]",
        )
        update_avatar({"next_field_training_time": next_time})
        log.warning(f"Avatar [{identity}] field training unrecognized response; skipped until {next_time}.")
        return False

    def maybe_record_field_training_passive(self, msg, text):
        """
        被动同步野外历练状态。
        当群聊中出现本账号的野外历练结果时（由其他来源触发），
        直接记录冷却，避免重复发送。
        """
        if is_reply_to_untracked_message(self, msg):
            return False
        if not self.is_field_training_response(text):
            return False
        if not text_targets_current_account(self, msg, text):
            return False
        identity = self._field_training_identity_from_reply(msg, text)
        if identity and identity != "主魂":
            return self.record_identity_field_training_response(identity, text, "野外历练被动同步")
        return self.record_field_training_response(text, "野外历练被动同步")

    # ---- 宗门战 — 辅助方法 ----

    def account_sect_name(self):
        """获取本账号的宗门名称"""
        return (getattr(self, "sect_name", "") or self.state.get("sect_name", "") or "").strip()

    def clean_common_text(self, text):
        """去除 markdown 加粗标记和反引号"""
        return (text or "").replace("**", "").replace("`", "")

    def parse_sect_war_sides(self, text):
        """
        从宗门战消息中解析对战双方。
        先尝试用已知宗门列表匹配，再用正则匹配。
        """
        clean = self.clean_common_text(text)
        found = []
        for sect in KNOWN_SECTS:
            if sect in clean and sect not in found:
                found.append(sect)
        if len(found) >= 2:
            return found[0], found[1]

        # 正则匹配：如 "【凌霄宫】 vs 【星宫】"
        patterns = [
            r"【([^】]{2,12})】\s*(?:vs|VS|Vs|对阵|对战|迎战|挑战|伐山|攻打|攻|对)\s*【([^】]{2,12})】",
            r"([一-龥]{2,12})\s*(?:vs|VS|Vs|对阵|对战|迎战|挑战|伐山|攻打)\s*([一-龥]{2,12})",
        ]
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                left = match.group(1).strip()
                right = match.group(2).strip()
                if left and right and left != right:
                    return left, right
        return "", ""

    def sect_war_remaining_seconds(self, text):
        """从宗门战消息中提取剩余时间（秒）"""
        clean = self.clean_common_text(text)
        relevant_lines = []
        for line in clean.splitlines():
            if any(k in line for k in ["剩余", "持续", "结束", "战役", "战斗", "对战"]):
                relevant_lines.append(line)
        if relevant_lines:
            cd = self.parse_wait_time("\n".join(relevant_lines))
            if cd > 0:
                return cd
        return self.parse_wait_time(clean)

    def is_no_sect_war_response(self, text):
        """判断是否"暂无宗门战"的回复"""
        clean = self.clean_common_text(text)
        return bool(clean and any(k in clean for k in ["暂无", "没有", "未开启", "尚未开启"])
                    and any(k in clean for k in ["宗门战", "宗门对战", "战役", "战况"]))

    def is_sect_war_status_response(self, text):
        """判断消息是否为合法的宗门战况回复"""
        clean = self.clean_common_text(text)
        if not clean:
            return False
        left, right = self.parse_sect_war_sides(clean)
        return bool(left and right) or self.is_no_sect_war_response(clean)

    def sect_war_account_involved(self):
        """判断本账号的宗门是否在当前宗门战中"""
        sect = self.account_sect_name()
        if not sect:
            return False
        return sect in {self.state.get("sect_war_left", ""), self.state.get("sect_war_right", "")}

    def sect_war_is_active(self):
        """判断宗门战是否仍在有效期内"""
        active_until = self.state.get("sect_war_active_until", "")
        return bool(active_until and is_future(active_until))

    def sect_war_message_should_trigger_status(self, text):
        """判断是否应该因为某条消息触发宗门战况查询"""
        clean = self.clean_common_text(text)
        return "战役" in clean

    # ---- 宗门战 — 状态记录 ----

    def record_sect_war_status_response(self, text):
        """记录宗门战况回复：解析对战双方、剩余时间"""
        self.ensure_common_command_state()
        log = self.common_command_logger()
        now = now_str()
        if not text:
            self.state["next_sect_war_status_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
            self.save_state()
            return False

        left, right = self.parse_sect_war_sides(text)
        remaining = self.sect_war_remaining_seconds(text)
        self.state["last_sect_war_status_time"] = now
        self.state["last_sect_war_response"] = self.clean_common_text(text)[:500]

        if left and right:
            self.state["sect_war_left"] = left
            self.state["sect_war_right"] = right
            if remaining > 0:
                self.state["sect_war_active_until"] = add_seconds_str(now, remaining)
            self.state["next_sect_war_status_time"] = ""
            self.save_state()
            return True

        if self.is_no_sect_war_response(text):
            self.state["sect_war_left"] = ""
            self.state["sect_war_right"] = ""
            self.state["sect_war_active_until"] = ""
            self.state["next_sect_war_status_time"] = ""
            self.save_state()
            return True

        notify_unrecognized_response(self, SECT_WAR_STATUS_COMMAND, text, log, "宗门战况")
        self.state["next_sect_war_status_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
        self.save_state()
        return False

    # ---- 宗门战 — 参战 ----

    def is_sect_war_join_cooldown_response(self, text):
        return any(k in self.clean_common_text(text) for k in ["冷却", "后再", "尚需", "还需", "休整", "调息"])

    def is_sect_war_join_success_response(self, text):
        return any(k in self.clean_common_text(text) for k in [
            "参战成功", "加入战场", "奔赴战场", "投入战斗", "已参战",
            "参与了宗门", "前线请战", "个人军勋", "你本次获得",
        ])

    def is_sect_war_join_unavailable_response(self, text):
        return any(k in self.clean_common_text(text) for k in ["暂无", "没有", "未开启", "不在对战", "不属于交战", "无法参战"])

    def record_sect_war_join_response(self, text):
        """记录参战结果"""
        self.ensure_common_command_state()
        log = self.common_command_logger()
        now = now_str()
        if not text:
            # 保留旧有的冷却时间，避免频繁重试
            current_next = self.state.get("next_sect_war_join_time", "")
            if current_next and is_future(current_next):
                return False
            self.state["next_sect_war_join_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
            self.save_state()
            return False

        cd = self.parse_wait_time(text)
        if self.is_sect_war_join_cooldown_response(text) and cd > 0:
            self.state["next_sect_war_join_time"] = add_seconds_str(now, cd)
            self.save_state()
            return True

        if self.is_sect_war_join_success_response(text):
            self.state["last_sect_war_join_time"] = now
            self.state["next_sect_war_join_time"] = add_seconds_str(now, SECT_WAR_JOIN_CD_SECONDS)
            self.save_state()
            return True

        if self.is_sect_war_join_unavailable_response(text):
            # 无法参战：清空缓存，等待下一次触发
            self.state["sect_war_left"] = ""
            self.state["sect_war_right"] = ""
            self.state["sect_war_active_until"] = ""
            self.state["next_sect_war_status_time"] = ""
            self.state["next_sect_war_join_time"] = ""
            self.save_state()
            return True

        notify_unrecognized_response(self, SECT_WAR_JOIN_COMMAND, text, log, "宗门参战")
        self.state["next_sect_war_join_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
        self.save_state()
        return False

    # ---- 宗门战 — 自动参战流程 ----

    async def maybe_join_sect_war_now(self):
        """
        如果宗门战正在进行且本账号宗门参战，自动发送.参战。
        使用独立锁防止并发多次参战。
        """
        lock = getattr(self, "_sect_war_join_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_sect_war_join_lock", lock)

        async with lock:
            self.ensure_common_command_state()
            log = self.common_command_logger()
            if self.identity_pause_seconds("主魂") > 0:
                return False
            active_until = self.state.get("sect_war_active_until", "")
            if not active_until or not is_future(active_until):
                return False
            if not self.sect_war_account_involved():
                return False
            next_join = self.state.get("next_sect_war_join_time", "")
            if next_join and is_future(next_join):
                return False
            log.info(f"Sect war active for {self.account_sect_name()}; sending {SECT_WAR_JOIN_COMMAND}.")
            join_resp = await self.send_and_wait_feedback(SECT_WAR_JOIN_COMMAND, timeout=90)
            return self.record_sect_war_join_response(join_resp)

    async def fetch_sect_war_status_from_trigger(self):
        """由消息触发：机器人发送战役消息时，自动查询宗门战况"""
        try:
            log = self.common_command_logger()
            if self.identity_pause_seconds("主魂") > 0:
                return
            log.info(f"Sect war keyword detected from bot; sending {SECT_WAR_STATUS_COMMAND}.")
            status_resp = await self.send_and_wait_feedback(SECT_WAR_STATUS_COMMAND, timeout=90)
            self.record_sect_war_status_response(status_resp)
            await self.maybe_join_sect_war_now()
        finally:
            setattr(self, "_sect_war_status_task", None)

    def maybe_handle_sect_war_message(self, msg, text, sender):
        """
        被动检测群聊中的战役消息，自动触发宗门战况查询和参战。
        有多层保护避免误触发（只检测非回复消息、排除已有冷却的账号等）。
        """
        if not sender or not is_game_bot_sender(self, sender):
            return False
        if not self.sect_war_message_should_trigger_status(text):
            return False
        # 回复消息已经由 feedback 机制处理，不需要再触发
        if getattr(msg, 'reply_to', None):
            return False
        if self.is_sect_war_join_success_response(text) or self.is_sect_war_join_cooldown_response(text):
            return False
        if self.is_no_sect_war_response(text):
            return False

        if self.is_sect_war_status_response(text):
            self.record_sect_war_status_response(text)
            task = asyncio.create_task(self.maybe_join_sect_war_now())
            setattr(self, "_sect_war_join_task", task)
            return True

        if self.sect_war_is_active():
            return True

        next_status = self.state.get("next_sect_war_status_time", "")
        if next_status and is_future(next_status):
            return True

        task = getattr(self, "_sect_war_status_task", None)
        if task and not task.done():
            return True

        self.state["next_sect_war_status_time"] = add_seconds_str(now_str(), SECT_WAR_RETRY_SECONDS)
        self.save_state()
        task = asyncio.create_task(self.fetch_sect_war_status_from_trigger())
        setattr(self, "_sect_war_status_task", task)
        return True

    # ---- 主循环 ----

    async def run_field_training_loop(self):
        """野外历练主循环：定时发送野外历练指令"""
        self.ensure_common_command_state()
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(20, 80))

        while self.is_running:
            # 只检查本地冷却时不切身份；真正发送主魂命令时由 send_and_wait_feedback 对齐。
            self.ensure_common_command_state()
            if await self.sleep_if_identity_paused("主魂", "Field training loop"):
                continue
            repaired_next = self.preserve_cooldown_floor(
                self.state,
                "last_field_training_time",
                "next_field_training_time",
                FIELD_TRAINING_CD_SECONDS,
                "field training loop",
            )
            if repaired_next and is_future(repaired_next):
                await asyncio.sleep(min(seconds_until(repaired_next), 300))
                continue
            next_time = self.state.get("next_field_training_time", "")
            if next_time and is_future(next_time):
                wait_sec = seconds_until(next_time)
                await asyncio.sleep(min(wait_sec, 300))
                continue

            log = self.common_command_logger()
            cmd = getattr(self, 'field_training_command', FIELD_TRAINING_COMMAND)
            log.info(f"Field training due: sending {cmd}.")
            resp = await self.send_and_wait_feedback(
                cmd,
                timeout=90,
                max_retries=0,
                suppress_no_response_alert=True,
            )
            self.record_field_training_response(resp)
            await asyncio.sleep(5)

    async def run_sect_war_loop(self):
        """宗门战主循环：检测宗门战有效期并在可参战时自动参战"""
        self.ensure_common_command_state()
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(30, 90))

        while self.is_running:
            # 只检查本地冷却时不切身份；真正发送主魂命令时由 send_and_wait_feedback 对齐。
            self.ensure_common_command_state()
            if await self.sleep_if_identity_paused("主魂", "Sect war loop"):
                continue
            next_join = self.state.get("next_sect_war_join_time", "")
            active_until = self.state.get("sect_war_active_until", "")
            # 宗门战过期：清空缓存
            if active_until and not is_future(active_until):
                self.state["sect_war_left"] = ""
                self.state["sect_war_right"] = ""
                self.state["sect_war_active_until"] = ""
                self.state["next_sect_war_join_time"] = ""
                self.save_state()
                await asyncio.sleep(60)
                continue

            if active_until and self.sect_war_account_involved():
                if next_join and is_future(next_join):
                    wait_sec = min(seconds_until(next_join), seconds_until(active_until), 600)
                    await asyncio.sleep(max(5, wait_sec))
                    continue
                await self.maybe_join_sect_war_now()
                await asyncio.sleep(5)
                continue

            await asyncio.sleep(300)
