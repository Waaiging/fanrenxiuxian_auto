#!/usr/bin/env python3
"""
【通用固定冷却指令模块 —— 所有账号脚本共享】

提供 CommonCommandMixin 混入类，封装所有账号通用的固定冷却指令：
  1. 野外历练 —— 定时外出历练，策略可配置（谨慎/均衡/深入）
  2. 宗门战况/参战 —— 自动检测宗门战役，参战获取军勋
  3. 固定冷却指令的记录与重试逻辑

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 继承使用。

【阅读导览】
- common_command_default_state：所有账号脚本都会合并进去的通用 state 字段。
- CommonCommandMixin 前半段：解析、状态读写、日报统计、身份暂停。
- 中段：Dashboard 自定义指令、野外历练、元婴/裂缝/法宝等通用循环。
- 后半段：黄龙山、宗门战、主循环辅助。

本模块不直接创建 TelegramClient；它假设继承方已经提供 send_and_wait_feedback、
send_and_wait_feedback_identity、save_state、state、avatars 等能力。
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import time
from meditation_features import MeditationModeMixin
from datetime import datetime, timedelta
from logging import getLogger

from log_utils import (
    MESSAGE_EVENTS_DB_FILE,
    actor_account_key,
    avatar_marker_identity_from_text,
    dashboard_command_control_value,
    dashboard_command_disabled,
    identity_from_single_username_mention,
    identity_plain_usernames,
    command_send_precheck,
    is_deep_meditation_ongoing_response,
    is_deep_meditation_settlement_response,
    is_game_bot_sender,
    is_not_deep_meditation_response,
    is_reply_to_untracked_message,
    meaningful_reply_to_msg_id,
    is_yuanying_out_settlement_response,
    is_yuanying_rebirth_block_response,
    is_yuanying_rebirth_success_response,
    notify_unrecognized_response,
    record_daily_reward_event_log,
    send_text_alert,
    text_targets_current_account,
    text_username_mentions,
    tracked_command_identity_for_reply,
    tracked_command_text_for_reply,
)
from command_modules import (
    ask_dao_plan,
    ASK_DAO_COMMAND,
    field_training_plan_from_features,
    NODE_SEARCH_COMMAND,
    node_search_plan,
    TREASURE_REFINE_COMMAND,
    treasure_refine_plan,
    nurture_spirit_plan,
    rift_search_plan,
    treasure_touch_plan,
    YUANYING_OUT_COMMAND,
    YUANYING_RETREAT_COMMAND,
    yuanying_command_for_identity,
    yuanying_out_plan,
)
from command_feedback import (
    is_retired_auto_command,
    second_soul_busy,
    second_soul_cooldown_seconds,
)
from automation_settings import (
    mulan_support_command as configured_mulan_support_command,
    tianxing_settings,
    tianxing_tianji_identities_for_account,
)
from miniapp_beast import (
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    miniapp_circuit_wait_seconds,
)
from miniapp_dwelling import apply_dwelling_snapshot, command_result_text
from sect_task_features import SectTaskMixin
from sect_rules import SectTaskStopped, require_command
from wind_thunder_features import wind_thunder_enabled, wind_thunder_send, wind_thunder_target_cooldown
from reward_parsing import (
    clean_reward_text as shared_clean_reward_text,
    context_reward_items as shared_context_reward_items,
    daily_reward_items_for_command as shared_daily_reward_items_for_command,
    field_training_settlement_text as shared_field_training_settlement_text,
    is_mulan_settlement_text,
    parse_reward_items as shared_parse_reward_items,
    reward_text_for_command as shared_reward_text_for_command,
    rift_settlement_text as shared_rift_settlement_text,
    trust_empty_reward_reparse,
)

log = getLogger(__name__)


# =====================================================================
# 常量定义
# =====================================================================
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
COMMAND_CONTROL_FILE = os.path.join(CONFIG_DIR, "command_controls.json")
CUSTOM_COMMAND_FILE = os.path.join(CONFIG_DIR, "dashboard_commands.json")
SECOND_SOUL_TRAIN_COMMAND = ".元神修炼"        # 第二元神修炼指令
SECOND_SOUL_STATUS_COMMAND = ".第二元神"       # 第二元神状态查询（解析剩余冷却）
SECOND_SOUL_INTERVAL_SECONDS = 24 * 3600       # 第二元神修炼间隔 24 小时
SECOND_SOUL_COOLDOWN_BUFFER_SECONDS = 300      # 解析出剩余冷却后额外留 5 分钟缓冲
SECOND_SOUL_RECHECK_SECONDS = 300              # 状态回复无时间格式时，5 分钟后重查
FIELD_TRAINING_COMMAND = ".野外历练 谨慎"      # 野外历练指令（各账号可覆盖）
FIELD_TRAINING_CD_SECONDS = 2 * 3600           # 野外历练冷却 2 小时
FIELD_TRAINING_MISSING_RESPONSE_RETRY_SECONDS = 5 * 60  # 空回复短退避，避免 dashboard 长时间显示 0 秒到期
FIELD_TRAINING_SETTLEMENT_WAIT_SECONDS = 15    # 等待野外历练初始回复编辑为结算
YUANYING_REBIRTH_PENDING_PAUSE_SECONDS = 30 * 60  # 已可夺舍但未重生时，短暂停自动主魂指令
YUANYING_REBIRTH_WAIT_SECONDS = 365 * 24 * 3600   # 探寻裂缝失败后等待手动 .重生 成功
YUANYING_OUT_CD_SECONDS = 8 * 3600
TREASURE_TOUCH_CD_SECONDS = 2 * 3600
ASK_DAO_CD_SECONDS = 12 * 3600
ASK_DAO_RETRY_SECONDS = 10 * 60
# 化神境 .搜寻节点 固定冷却：12 小时（+5 分钟缓冲，游戏冷却从结算时刻起算）。
NODE_SEARCH_CD_SECONDS = 12 * 3600 + 300
# 虚天鼎炼焰冷却：8 小时（+5 分钟缓冲）。炼焰 9/9 圆满后停止发送。
TREASURE_REFINE_CD_SECONDS = 8 * 3600 + 300
SECT_SKILL_MAX_DAILY = 3
MEDITATION_SETTLEMENT_GRACE_SECONDS = 3 * 60      # 闭关到点后给机器人结算状态留 3 分钟余量
SECT_WAR_STATUS_COMMAND = ".宗门战况"           # 查询宗门战况
SECT_WAR_JOIN_COMMAND = ".参战"                 # 参战指令
SECT_WAR_JOIN_CD_SECONDS = 2 * 3600            # 参战冷却 2 小时
SECT_WAR_RETRY_SECONDS = 10 * 60               # 宗门战重试间隔 10 分钟
HUANGLONG_REPORT_TITLE = "黄龙山轮值军报"
HUANGLONG_SIGNUP_COMMAND = ".报名黄龙山"
HUANGLONG_REPORT_SEARCH_RETRY_SECONDS = 2 * 60
HUANGLONG_REPORT_SEARCH_LIMIT = 12
TRANSIENT_SECT_NAMES = {
    "读取中", "加载中", "同步中", "查询中", "未知", "未知宗门", "暂无数据", "-", "--",
}
TRANSIENT_AVATAR_DAO_NAMES = {"一缕残魂"}
# .支援慕兰 <参数> is an independent daily command for each identity.
# Keep the exported constant as the default for legacy imports; the runtime
# helper below reads Dashboard settings on every due check.
MULAN_SUPPORT_COMMAND = ".支援慕兰 护阵"
MULAN_SUPPORT_RETRY_SECONDS = 10 * 60
MULAN_SUPPORT_START_HOUR = 10
MULAN_SUPPORT_START_MINUTE = 0
AVATAR_TOWER_SETTLEMENT_WAIT_SECONDS = 30
AVATAR_TOWER_SETTLEMENT_TIMEOUT_SECONDS = 45
TIME_CRITICAL_COMMAND_PREFIXES = (
    ".观星",
    ".改换星移",
    ".观命",
    ".定命",
    ".助阵",
    ".安抚信徒",
)
STALE_STAR_TIME_CRITICAL_KEYS = {
    "next_star_gazing_time",
    "pending_star_gazing_target_time",
    "pending_star_shift_target_time",
}
STALE_STAR_TIME_CRITICAL_GRACE_SECONDS = 180


def seconds_until_mulan_support_start(now=None):
    """Return seconds until today's independent 10:00 Mulan support window."""
    now = now or datetime.now()
    target = now.replace(
        hour=MULAN_SUPPORT_START_HOUR,
        minute=MULAN_SUPPORT_START_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        return 0
    return max(1, int((target - now).total_seconds()) + 1)


def mulan_support_start_label():
    return f"{MULAN_SUPPORT_START_HOUR:02d}:{MULAN_SUPPORT_START_MINUTE:02d}"

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
    # Route suffix differs by account; match both adventure and moon-palace voyages.
    "next_concubine_voyage_time": ".侍妾远航",
    "next_concubine_search_time": ".红尘寻缘",
    "next_stairs_time": ".登天阶",
    "next_heart_platform_time": ".问心台",
    "nine_heaven_wind_cd_time": ".引九天罡风",
    "heart_platform_time": ".问心台",
    "next_heart_time": ".问心台",
    "next_formation_time": ".启阵",
    "next_formation_retry_time": ".启阵",
    "next_force_exit_time": ".强行出关",
    "next_nurture_spirit_time": ".温养器灵 青竹蜂云剑（神雷版）",
    "next_small_world_time": ".小世界",
    "next_small_world_calamity_time": ".安抚信徒",
    "next_miracle_preach_time": ".神迹 布道",
    "next_star_gazing_time": ".观星",
    "pending_star_gazing_target_time": ".观星",
    "pending_star_shift_target_time": ".改换星移",
    "next_taiyi_guide_time": ".引道 水",
    "next_steal_time": ".灵兽偷菜",
    "next_beast_status_check_time": ".我的灵兽",
    "next_abyss_time": ".探渊 <灵兽>",
    "next_pasture_time": ".一键放养",
    "next_beast_interaction_time": ".灵兽互动 六翼",
    "next_beast_cruise_time": ".灵兽巡游 六翼",
    "next_ask_dao_time": ".问道",
}
LOW_PRIORITY_DAILY_COMMANDS = {
    ".宗门点卯",
    ".支援慕兰",
    ".观命",
    ".定命",
}


def current_mulan_support_command():
    """Return the currently selected Dashboard Mulan-support parameter."""
    return configured_mulan_support_command()
LOW_PRIORITY_DAILY_DEFER_SECONDS = 5 * 60
LOW_PRIORITY_DAILY_LOG_INTERVAL_SECONDS = 5 * 60
TIANXING_RIFT_PREFIX_COMMANDS = (".推命 探索", ".改命 探索")
TIANXING_RIFT_PREFIX_DELAY_SECONDS = 3
TIANXING_RIFT_PREFIX_RETRY_SECONDS = 5 * 60
TIANXING_DESTINY_CHOICES = ("天府", "紫微", "贪狼", "太阴")
TIANXING_DESTINY_ACTION_PREFERENCES = {
    "cultivation": ("紫微", "贪狼"),
    "exploration": ("贪狼", "太阴"),
    "crafting": ("天府", "太阴"),
}
TIANXING_DESTINY_FAILURE_KEYWORDS = (
    "无法", "不能", "不可", "闭关中", "深度闭关", "正在闭关", "闭关状态",
    "冷却", "修为不足", "并非", "未开启", "错误",
)
TIANXING_DESTINY_FAILURE_LOG_SUPPRESS_SECONDS = 15 * 60
TIANXING_DESTINY_ROUTE_RETRY_SECONDS = 15 * 60

# 已知宗门列表（用于解析宗门战双方）
KNOWN_SECTS = (
    "凌霄宫", "星宫", "万灵宗", "元婴宗", "天星宗", "太一门", "阴罗宗", "散修", "黄枫谷",
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

TIANXING_TIANJI_PREFIX_COMMAND = ".推命 炼制"


def common_command_default_state():
    """返回通用命令的默认状态字典。

    这些键会被合并进每个账号 state。新增字段时要考虑旧 state 迁移：
    旧文件里没有的键应通过 setdefault 补齐，不要直接假设存在。
    """
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
        "huanglong_signup_date": "",
        "huanglong_signup_sect": "",
        "huanglong_signup_report_msg_id": "",
        "huanglong_signup_time": "",
        "huanglong_signup_status": "",
        "huanglong_signup_response": "",
        "huanglong_signup_records": {},
        "huanglong_rotation_report_date": "",
        "huanglong_rotation_report_sect": "",
        "huanglong_rotation_report_msg_id": "",
        "huanglong_rotation_report_time": "",
        "huanglong_rotation_report_source": "",
        "huanglong_rotation_report_status": "",
        "next_huanglong_report_search_time": "",
        "custom_command_runs": {},
        "identity_pauses": {},
        "star_gazing_assigned_manifest_time": "",
        "star_gazing_assigned_avatar": "",
        "star_gazing_assigned_time": "",
        "daily_reward_events": [],
        "daily_reward_last_sent_date": "",
        "last_mulan_support_date": "",
        "last_mulan_support_time": "",
        "next_mulan_support_time": "",
        "last_mulan_support_response": "",
        "last_mulan_support_error": "",
        "last_destiny_observation_date": "",
        "last_destiny_observation_time": "",
        "tianxing_destiny_options": [],
        "tianxing_destiny_options_date": "",
        "next_tianxing_destiny_retry_time": "",
        "tianxing_destiny_retry_reason": "",
        "tianxing_destiny_failure_log_time": "",
    }


# =====================================================================
# CommonCommandMixin 混入类
# =====================================================================

class _CommonAtomicTask:
    """共享原子任务门闩。

    多步骤链（如宗门战、化身任务批次）会短暂占用
    active_atomic_task，普通发送会等待它释放，避免中途被其他循环切身份。
    """

    def __init__(self, actor, label, log_lifecycle=True):
        self.actor = actor
        self.label = str(label or "Task")
        self.log_lifecycle = bool(log_lifecycle)
        self.task = None
        self.acquired = False
        self.reentrant = False

    async def __aenter__(self):
        if not hasattr(self.actor, "active_atomic_task"):
            return self
        self.task = asyncio.current_task()
        while getattr(self.actor, "active_atomic_task", None) is not None and self.actor.active_atomic_task != self.task:
            await asyncio.sleep(0.5)
        if getattr(self.actor, "active_atomic_task", None) == self.task:
            self.reentrant = True
            return self
        self.actor.active_atomic_task = self.task
        self.actor._common_atomic_task = self.task
        self.actor._common_atomic_label = self.label
        self.actor._common_atomic_started_at = time.monotonic()
        self.acquired = True
        if self.log_lifecycle:
            try:
                self.actor.common_command_logger().info(f"Atomic task acquired by {self.label}.")
            except Exception:
                pass
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.acquired and getattr(self.actor, "active_atomic_task", None) == self.task:
            self.actor.active_atomic_task = None
        if self.acquired and getattr(self.actor, "_common_atomic_task", None) == self.task:
            self.actor._common_atomic_task = None
            self.actor._common_atomic_label = ""
            self.actor._common_atomic_started_at = 0.0
            if self.log_lifecycle:
                try:
                    self.actor.common_command_logger().info(f"Atomic task released by {self.label}.")
                except Exception:
                    pass
        return False


class CommonCommandMixin(SectTaskMixin, MeditationModeMixin):
    """通用固定冷却指令混入类。

    这里的方法尽量只依赖继承方暴露的统一接口，不直接区分主号/副号/小号。
    真正的账号差异通过 account_key、identity_sect_names、avatar_features 和
    各脚本覆盖的 state_time_command_for_key 等钩子注入。
    """

    # ---- 轻量解析/判断工具 ----

    def message_age_seconds(self, msg):
        """Return message age in seconds, based on the original send time."""
        msg_dt = getattr(msg, "date", None)
        if not msg_dt:
            return 0
        try:
            now_dt = datetime.now(msg_dt.tzinfo) if msg_dt.tzinfo else datetime.utcnow()
            return max(0, (now_dt - msg_dt).total_seconds())
        except Exception:
            return 0

    def formation_invite_actor_username(self, text):
        """Extract the actor username from a formation invite."""
        if not text:
            return ""
        match = re.search(r"@([A-Za-z0-9_]+)\s*正在布设大阵", text)
        if not match:
            match = re.search(r"@([A-Za-z0-9_]+)", text)
        return match.group(1).lower() if match else ""

    def avatar_username_for_identity(self, avatar):
        for username, name in (getattr(self, "avatar_usernames", {}) or {}).items():
            if name == avatar:
                return username.lower()
        return ""

    def resolve_avatar_identity(self, identity):
        """Resolve a stale avatar Dao name to its current name.

        Long-running avatar loops retain their original function argument.  A
        rebirth can therefore change the displayed Dao name while a loop still
        holds the old one.  Keep a short persistent alias chain so those loops
        route their next command to the current Dao name instead of emitting a
        stale ``.切换`` command.
        """
        original = str(identity or "").strip()
        if not original or original == "主魂":
            return original or "主魂"
        state = getattr(self, "state", {}) or {}
        aliases = state.get("avatar_dao_name_aliases") if isinstance(state, dict) else None
        if not isinstance(aliases, dict):
            return original

        current = original
        seen = set()
        while current not in seen:
            seen.add(current)
            mapped = str(aliases.get(current) or "").strip()
            if not mapped or mapped == current:
                break
            current = mapped

        known_avatars = set(getattr(self, "avatars", []) or [])
        return current if current in known_avatars else original

    def refresh_avatar_dao_name(self, identity, dao_name, player_id=None, persist=True):
        """Migrate one avatar to its current Dao name using its stable player ID."""
        dao_name = str(dao_name or "").strip()
        if not dao_name or dao_name == "主魂" or dao_name in TRANSIENT_AVATAR_DAO_NAMES:
            return str(identity or "").strip()

        player_key = str(player_id or "").strip()
        old_name = ""
        for mapping_name in ("_avatar_chat_ids", "avatar_identities"):
            for mapped_player_id, mapped_name in (getattr(self, mapping_name, {}) or {}).items():
                if player_key and str(mapped_player_id) == player_key:
                    old_name = str(mapped_name or "").strip()
                    break
            if old_name:
                break

        state = getattr(self, "state", {}) or {}
        avatar_states = state.get("avatars") if isinstance(state, dict) else {}
        if not old_name and isinstance(avatar_states, dict) and player_key:
            for candidate, candidate_state in avatar_states.items():
                if str((candidate_state or {}).get("miniapp_player_id") or "") == player_key:
                    old_name = str(candidate or "").strip()
                    break

        known_avatars = list(getattr(self, "avatars", []) or [])
        requested = str(identity or "").strip()
        if not old_name and requested in known_avatars:
            old_name = requested
        if old_name not in known_avatars:
            return requested

        if not player_key:
            for mapping_name in ("_avatar_chat_ids", "avatar_identities"):
                for mapped_player_id, mapped_name in (getattr(self, mapping_name, {}) or {}).items():
                    if str(mapped_name or "").strip() == old_name:
                        player_key = str(mapped_player_id or "").strip()
                        break
                if player_key:
                    break

        # The dwelling identity list can lag one request behind a rebirth.  A
        # player ID is authoritative, so never let a previously confirmed
        # name be renamed back to an alias returned by that stale list.
        dao_names = state.setdefault("avatar_dao_names_by_player_id", {}) if isinstance(state, dict) else {}
        if isinstance(state, dict) and not dao_names:
            legacy_names = state.get("avatar_dao_names_by_tgid")
            if isinstance(legacy_names, dict):
                dao_names.update(legacy_names)
        authoritative_name = str(dao_names.get(player_key) or "").strip() if player_key else ""
        aliases = state.get("avatar_dao_name_aliases") if isinstance(state, dict) else None
        if authoritative_name and authoritative_name != dao_name:
            alias_target = ""
            if isinstance(aliases, dict):
                alias_target = str(aliases.get(dao_name) or "").strip()
                seen = set()
                while alias_target and alias_target not in seen:
                    seen.add(alias_target)
                    mapped = str(aliases.get(alias_target) or "").strip()
                    if not mapped or mapped == alias_target:
                        break
                    alias_target = mapped
            if alias_target == authoritative_name:
                miniapp_router = getattr(self, "_miniapp_command_router", None)
                miniapp_transport = getattr(miniapp_router, "transport", None)
                miniapp_player_ids = getattr(miniapp_transport, "identity_player_ids", None)
                if isinstance(miniapp_player_ids, dict):
                    for route_key, route_player_id in tuple(miniapp_player_ids.items()):
                        if str(route_player_id) == player_key and route_key != "主魂":
                            miniapp_player_ids.pop(route_key, None)
                    try:
                        numeric_player_id = int(player_key)
                    except (TypeError, ValueError):
                        numeric_player_id = player_key
                    miniapp_player_ids[authoritative_name] = numeric_player_id
                    miniapp_player_ids[authoritative_name.casefold()] = numeric_player_id
                choices = getattr(miniapp_transport, "identity_choices", None)
                if isinstance(choices, list):
                    for choice in choices:
                        if isinstance(choice, dict) and str(choice.get("playerId")) == player_key:
                            choice["daoName"] = authoritative_name
                            if choice.get("displayName") == dao_name:
                                choice["displayName"] = authoritative_name
                self.common_command_logger().warning(
                    "Ignored stale Dao name for player %s: %s (current %s).",
                    player_key,
                    dao_name,
                    authoritative_name,
                )
                return authoritative_name

        if old_name == dao_name:
            return old_name
        if dao_name in known_avatars:
            self.common_command_logger().warning(
                "Ignored Dao name refresh for %s: %s is already assigned to another avatar.",
                old_name,
                dao_name,
            )
            return old_name

        # ``playerId`` comes from the dwelling identity selection and remains
        # stable through rebirth.  It is not the Telegram group/chat ID.
        previous_player_dao_name = str(dao_names.get(player_key) or "").strip() if player_key else ""
        if player_key and isinstance(dao_names, dict):
            dao_names[player_key] = dao_name

        aliases = state.setdefault("avatar_dao_name_aliases", {}) if isinstance(state, dict) else {}
        if isinstance(aliases, dict):
            for legacy_name, current_name in tuple(aliases.items()):
                if str(current_name or "").strip() == old_name:
                    aliases[legacy_name] = dao_name
            aliases[old_name] = dao_name
            aliases.pop(dao_name, None)

        self.avatars = [dao_name if name == old_name else name for name in known_avatars]
        for mapping_name in ("_avatar_chat_ids", "avatar_identities", "avatar_usernames"):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict):
                for key, value in tuple(mapping.items()):
                    if value == old_name:
                        mapping[key] = dao_name
        miniapp_router = getattr(self, "_miniapp_command_router", None)
        miniapp_transport = getattr(miniapp_router, "transport", None)
        miniapp_player_ids = getattr(miniapp_transport, "identity_player_ids", None)
        if player_key and isinstance(miniapp_player_ids, dict):
            try:
                numeric_player_id = int(player_key)
            except (TypeError, ValueError):
                numeric_player_id = player_key
            # Remove every stale display-name key for this player before
            # registering the canonical Dao name.
            for route_key, route_player_id in tuple(miniapp_player_ids.items()):
                if str(route_player_id) == str(numeric_player_id) and route_key not in {"主魂"}:
                    miniapp_player_ids.pop(route_key, None)
            miniapp_player_ids[dao_name] = numeric_player_id
            miniapp_player_ids[dao_name.casefold()] = numeric_player_id
        if player_key and miniapp_transport is not None:
            choices = getattr(miniapp_transport, "identity_choices", None)
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict) or str(choice.get("playerId")) != player_key:
                        continue
                    choice["daoName"] = dao_name
                    if choice.get("displayName") in {old_name, previous_player_dao_name}:
                        choice["displayName"] = dao_name
        for mapping_name in ("avatar_nicknames", "avatar_features"):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict) and old_name in mapping:
                mapping[dao_name] = mapping.pop(old_name)

        for mapping_name in ("identity_sect_names",):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict) and old_name in mapping:
                mapping[dao_name] = mapping.pop(old_name)
        for mapping_name in ("identity_sect_names",):
            mapping = state.get(mapping_name) if isinstance(state, dict) else None
            if isinstance(mapping, dict) and old_name in mapping:
                mapping[dao_name] = mapping.pop(old_name)

        if isinstance(avatar_states, dict) and old_name in avatar_states:
            avatar_states[dao_name] = avatar_states.pop(old_name)
        pauses = state.get("identity_pauses") if isinstance(state, dict) else None
        if isinstance(pauses, dict) and old_name in pauses:
            old_pause = pauses.pop(old_name)
            current_pause = pauses.get(dao_name)
            if not isinstance(current_pause, dict) or old_pause.get("wait_for_rebirth"):
                pauses[dao_name] = old_pause
        confirmed_rebirth_rename = bool(
            previous_player_dao_name
            and previous_player_dao_name not in TRANSIENT_AVATAR_DAO_NAMES
            and previous_player_dao_name != dao_name
        )
        if confirmed_rebirth_rename and isinstance(pauses, dict):
            pause_keys = {old_name, dao_name, player_key, *TRANSIENT_AVATAR_DAO_NAMES}
            for pause_key in pause_keys:
                entry = pauses.get(pause_key)
                if not isinstance(entry, dict):
                    continue
                reason = str(entry.get("reason") or "")
                if entry.get("wait_for_rebirth") or any(
                    keyword in reason for keyword in ("元婴", "肉身", "夺舍", "重生")
                ):
                    pauses.pop(pause_key, None)
        command_map = getattr(self, "command_avatar_map", None)
        if isinstance(command_map, dict):
            for key, value in tuple(command_map.items()):
                if value == old_name:
                    command_map[key] = dao_name
        probe_commands = getattr(self, "actual_cooldown_probe_commands", None)
        if isinstance(probe_commands, set):
            self.actual_cooldown_probe_commands = {
                (dao_name if command_identity == old_name else command_identity, command)
                for command_identity, command in probe_commands
            }

        for attr in ("_current_identity", "_persisted_identity", "_manual_identity_label"):
            if getattr(self, attr, None) == old_name:
                setattr(self, attr, dao_name)
        if isinstance(state, dict):
            if state.get("current_identity") == old_name:
                state["current_identity"] = dao_name
            for key in (
                "miniapp_route_identities",
                "miniapp_star_farm_identities",
                "miniapp_journey_identities",
            ):
                if isinstance(state.get(key), list):
                    state[key] = [dao_name if name == old_name else name for name in state[key]]
            if state.get("miniapp_route_last_identity") == old_name:
                state["miniapp_route_last_identity"] = dao_name
            for key, value in tuple(state.items()):
                # Keep alias keys intact: old loop closures and persisted
                # dashboard selections still need old_name -> dao_name.
                if key == "avatar_dao_name_aliases":
                    continue
                if isinstance(value, dict) and old_name in value:
                    value[dao_name] = value.pop(old_name)
            history = state.setdefault("avatar_dao_name_history", [])
            if isinstance(history, list):
                history.append({
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "player_id": player_key,
                    "old_name": old_name,
                    "new_name": dao_name,
                })
                del history[:-50]

        handler = getattr(self, "on_avatar_dao_name_changed", None)
        if callable(handler):
            handler(old_name, dao_name)
        # Publish the authoritative account snapshot before updating the shared
        # duel roster, which other processes reconcile against this snapshot.
        if persist:
            try:
                self.save_state()
            except Exception:
                self.common_command_logger().warning("Failed to persist refreshed avatar Dao name.", exc_info=True)
        account_key = str(getattr(self, "account_key", "") or "").strip()
        if account_key:
            try:
                from duel_features import refresh_duel_identity_name
                refresh_duel_identity_name(account_key, old_name, dao_name)
            except Exception:
                self.common_command_logger().warning(
                    "Failed to refresh duel identity after avatar Dao name change.",
                    exc_info=True,
                )
        self.common_command_logger().warning(
            "Avatar Dao name refreshed for player %s: %s -> %s.",
            player_key or "unknown",
            old_name,
            dao_name,
        )
        return dao_name

    def restore_avatar_dao_names(self):
        """Restore Dao names persisted from prior Mini App dwelling snapshots."""
        state = getattr(self, "state", {}) or {}
        state_changed = False
        pauses = state.get("identity_pauses") if isinstance(state, dict) else None
        if isinstance(pauses, dict):
            for pause_identity, entry in tuple(pauses.items()):
                if not isinstance(entry, dict) or entry.get("wait_for_rebirth"):
                    continue
                pause_until = str(entry.get("until") or "").strip()
                if not pause_until:
                    continue
                try:
                    expired = str_to_dt(pause_until) <= datetime.now()
                except Exception:
                    expired = False
                if expired:
                    pauses.pop(pause_identity, None)
                    state_changed = True
        saved_names = state.get("avatar_dao_names_by_player_id") if isinstance(state, dict) else None
        if not isinstance(saved_names, dict) and isinstance(state, dict):
            saved_names = state.get("avatar_dao_names_by_tgid")
        if not isinstance(saved_names, dict):
            if state_changed:
                self.save_state()
            return 0
        changed = 0
        for player_id, dao_name in tuple(saved_names.items()):
            dao_name = str(dao_name or "").strip()
            if dao_name in TRANSIENT_AVATAR_DAO_NAMES:
                player_key = str(player_id or "").strip()
                configured_name = ""
                for mapping_name in ("_avatar_chat_ids", "avatar_identities"):
                    mapping = getattr(self, mapping_name, {}) or {}
                    configured_name = str(mapping.get(player_key) or "").strip()
                    if configured_name:
                        break
                if not configured_name:
                    continue

                saved_names[player_id] = configured_name
                aliases = state.get("avatar_dao_name_aliases")
                if isinstance(aliases, dict):
                    for legacy_name, current_name in tuple(aliases.items()):
                        if str(current_name or "").strip() != dao_name:
                            continue
                        if legacy_name == configured_name:
                            aliases.pop(legacy_name, None)
                        else:
                            aliases[legacy_name] = configured_name
                avatar_states = state.get("avatars")
                if isinstance(avatar_states, dict) and dao_name in avatar_states:
                    stale_state = avatar_states.pop(dao_name)
                    current_state = avatar_states.setdefault(configured_name, {})
                    if isinstance(current_state, dict) and isinstance(stale_state, dict):
                        current_state.update(stale_state)
                        current_state["miniapp_dao_name"] = configured_name
                for mapping_name in ("identity_sect_names",):
                    mapping = state.get(mapping_name)
                    if isinstance(mapping, dict) and dao_name in mapping:
                        mapping[configured_name] = mapping.pop(dao_name)
                pauses = state.get("identity_pauses")
                if isinstance(pauses, dict) and dao_name in pauses:
                    stale_pause = pauses.pop(dao_name)
                    if configured_name not in pauses or stale_pause.get("wait_for_rebirth"):
                        pauses[configured_name] = stale_pause
                if state.get("current_identity") == dao_name:
                    state["current_identity"] = configured_name
                changed += 1
                continue
            before = list(getattr(self, "avatars", []) or [])
            self.refresh_avatar_dao_name("", dao_name, player_id=player_id, persist=False)
            changed += before != list(getattr(self, "avatars", []) or [])
        if changed or state_changed:
            try:
                self.save_state()
            except Exception:
                self.common_command_logger().warning("Failed to persist restored avatar Dao names.", exc_info=True)
        return changed

    def formation_result_includes_avatar(self, text, avatar):
        username = self.avatar_username_for_identity(avatar)
        return bool(username and f"@{username}" in (text or "").lower())

    def is_formation_success(self, text):
        """Return whether a formation message says the formation is complete."""
        return bool(text and ("周天星斗大阵-成" in text or "大阵已成" in text))

    def is_formation_pending(self, text):
        """Return whether a formation message is still asking for assists."""
        return bool(text and ("周天星斗大阵-启" in text or "尚需" in text or "助阵" in text))

    def formation_invite_actor_identity(self, text):
        username = self.formation_invite_actor_username(text)
        return (getattr(self, "avatar_usernames", {}) or {}).get(username, "")

    def is_own_formation_invite(self, text):
        """Return whether a formation invite mentions this account username."""
        if not text or not getattr(self, "my_info", None):
            return False
        username = (getattr(self.my_info, "username", "") or "").lower().lstrip("@")
        return bool(username and f"@{username}" in text.lower())

    def is_external_formation_invite(self, text):
        """Return whether text is another account's pending formation invite."""
        if not text or self.is_formation_success(text):
            return False
        if self.is_own_formation_invite(text):
            return False
        return (
            "周天星斗大阵-启" in text
            and "正在布设大阵" in text
            and ("尚需" in text or "助阵" in text)
        )

    def common_has_pending_star_gazing_action(self):
        """Return whether .观星 or .改换星移 has a future scheduled action."""
        pending_gazing_target = self.state.get("pending_star_gazing_target_time", "")
        pending_shift_target = self.state.get("pending_star_shift_target_time", "")
        return bool(
            (pending_gazing_target and is_future(pending_gazing_target))
            or (pending_shift_target and is_future(pending_shift_target))
        )

    def common_clear_pending_star_gazing_schedule(self):
        """Clear scheduled account-level .观星 fields."""
        self.state["pending_star_gazing_date"] = ""
        self.state["pending_star_gazing_target_time"] = ""
        self.state["pending_star_gazing_scheduled_time"] = ""
        self.state["pending_star_gazing_manifest_time"] = ""
        self.state["pending_star_gazing_fate_type"] = ""

    def common_clear_star_gazing_round_claim(self):
        """Clear account-level .观星 round claim fields."""
        self.state["star_gazing_claimed_manifest_time"] = ""
        self.state["star_gazing_claimed_avatar"] = ""
        self.state["pending_star_gazing_manifest_time"] = ""
        self.state["pending_star_gazing_fate_type"] = ""

    def common_star_gazing_manifest_key(self, manifest):
        if isinstance(manifest, datetime):
            return dt_to_str(manifest)
        return str(manifest or "").strip()

    def common_star_gazing_assigned_avatar_for_manifest(self, manifest):
        """Return the script-level identity already assigned to this manifest round."""
        manifest_key = self.common_star_gazing_manifest_key(manifest)
        if not manifest_key:
            return ""
        claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
        claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
        if claimed_manifest == manifest_key and claimed_avatar:
            return claimed_avatar
        assigned_manifest = self.state.get("star_gazing_assigned_manifest_time", "")
        if assigned_manifest != manifest_key:
            return ""
        return self.state.get("star_gazing_assigned_avatar", "") or claimed_avatar or "unknown"

    def common_mark_star_gazing_round_assigned(self, manifest, avatar, source="", logger=None):
        """Remember that this script has already spent this manifest round on one identity."""
        manifest_key = self.common_star_gazing_manifest_key(manifest)
        if not manifest_key:
            return False
        identity = str(avatar or "主魂").strip() or "主魂"
        changed = (
            self.state.get("star_gazing_assigned_manifest_time", "") != manifest_key
            or self.state.get("star_gazing_assigned_avatar", "") != identity
        )
        self.state["star_gazing_assigned_manifest_time"] = manifest_key
        self.state["star_gazing_assigned_avatar"] = identity
        self.state["star_gazing_assigned_time"] = now_str()
        if changed:
            log = logger or self.common_command_logger()
            suffix = f" ({source})" if source else ""
            log.info(
                f"Star gazing [{identity}]: manifest {manifest_key} assigned at script level{suffix}."
            )
        return changed

    def common_star_gazing_claim_matches(self, avatar, manifest_dt, default_identity="主魂"):
        """Return whether the current account-level claim still belongs to identity."""
        if not manifest_dt:
            return True
        manifest_key = dt_to_str(manifest_dt)
        expected_avatar = avatar if avatar is not None else default_identity
        return (
            self.state.get("star_gazing_claimed_manifest_time", "") == manifest_key
            and self.state.get("star_gazing_claimed_avatar", "") == expected_avatar
        )

    def common_claimed_star_gazing_pending_due(self, avatar, pending, now=None):
        """Return whether a claimed .观星 send time is due."""
        if not avatar:
            return False
        pending_dt = str_to_dt(pending)
        if not pending_dt:
            return False
        now = now or datetime.now()
        return now >= pending_dt - timedelta(seconds=1)

    def common_clear_stale_star_gazing_claim_before_manifest(
        self,
        manifest_dt,
        sender_info="",
        text_preview="",
        logger=None,
    ):
        """Clear an older claimed .观星 round before handling a newer manifest."""
        claimed_manifest = self.state.get("star_gazing_claimed_manifest_time", "")
        pending_manifest = self.state.get("pending_star_gazing_manifest_time", "") or claimed_manifest
        if not pending_manifest or not manifest_dt:
            return False
        pending_manifest_dt = str_to_dt(pending_manifest)
        if pending_manifest_dt >= manifest_dt:
            return False

        pending = self.state.get("pending_star_gazing_target_time", "")
        claimed_avatar = self.state.get("star_gazing_claimed_avatar", "")
        self.common_clear_pending_star_gazing_schedule()
        self.common_clear_star_gazing_round_claim()
        if hasattr(self, "star_gazing_task") and self.star_gazing_task and not self.star_gazing_task.done():
            self.star_gazing_task.cancel()
        self.state["next_star_gazing_time"] = ""
        self.save_state()
        (logger or self.common_command_logger()).info(
            f"Star gazing: cleared stale pending .观星 (was at {pending or 'none'}) "
            f"for manifest {pending_manifest}, claimed by {claimed_avatar or 'none'}; "
            f"handling current manifest {dt_to_str(manifest_dt)}. "
            f"Triggered by {sender_info}: {text_preview}"
        )
        return True

    # ---- 天星宗命星前置 ----

    def tianxing_identity_state(self, identity="主魂"):
        identity = str(identity or "主魂").strip() or "主魂"
        # A time-critical task can wait across a rebirth.  Resolve the name
        # immediately before doing any pause/precheck work so its switch
        # anchor cannot use a stale Dao name retained by the task closure.
        resolver = getattr(self, "resolve_avatar_identity", None)
        if callable(resolver):
            identity = resolver(identity)
        if identity == "主魂":
            return self.state
        getter = getattr(self, "get_avatar_state", None)
        if callable(getter):
            return getter(identity)
        avatars = self.state.setdefault("avatars", {})
        return avatars.setdefault(identity, {})

    def tianxing_identity_enabled(self, identity="主魂"):
        return self.identity_sect_name(self.resolve_avatar_identity(identity)) == "天星宗"

    def tianxing_identity_names(self):
        """Return every locally configured identity currently belonging to Tianxing."""
        return [identity for identity in self.sect_task_identities()
                if self.tianxing_identity_enabled(identity)]

    def miniapp_small_world_transport(self):
        router = getattr(self, "_miniapp_command_router", None)
        transport = getattr(router, "transport", None)
        if transport is None:
            restricted = getattr(self, "_restricted_miniapp_worker", None)
            transport = getattr(restricted, "transport", None)
        if transport is None:
            raise MiniAppBeastError("miniapp_route_unavailable")
        return transport

    async def _run_tianxing_tianji_identity_round(self, identity, target, round_id, transport):
        """Run at most one forge round for one Tianxing identity."""
        identity = self.resolve_avatar_identity(identity)
        if not self.sect_operation_allowed(identity, TIANXING_TIANJI_PREFIX_COMMAND):
            return False
        state = self.tianxing_identity_state(identity)
        if state.get("tianxing_tianji_round_id") != round_id:
            state.update(
                {
                    "tianxing_tianji_round_id": round_id,
                    "tianxing_tianji_completed": 0,
                    "tianxing_tianji_last_result": "",
                    "tianxing_tianji_error": "",
                    "tianxing_tianji_retry_time": "",
                }
            )
            self.save_state()

        retry_time = str(state.get("tianxing_tianji_retry_time") or "")
        if retry_time and is_future(retry_time):
            return False
        completed = max(0, int(state.get("tianxing_tianji_completed") or 0))
        if completed >= target:
            return False
        if not await self.ensure_tianxing_destiny_for_action(identity, "crafting"):
            state["tianxing_tianji_error"] = "命星未确认"
            retry_seconds = max(
                300,
                self.tianxing_destiny_retry_wait_seconds(identity),
            )
            state["tianxing_tianji_retry_time"] = add_seconds_str(now_str(), retry_seconds)
            self.save_state()
            return True

        try:
            async with self.common_atomic_task(
                f"Tianxing-tianji-round-{identity}", log_lifecycle=False
            ):
                require_command(self, identity, TIANXING_TIANJI_PREFIX_COMMAND)
                prefix = await transport.command(
                    TIANXING_TIANJI_PREFIX_COMMAND,
                    identity=identity,
                    log_operation=False,
                )
                apply_dwelling_snapshot(self, identity, prefix.payload)
                prefix_text = command_result_text(prefix.payload) or prefix.text
                # The command-center may report a business response with
                # actionResult.ok=false even when the returned text confirms
                # that the prediction was created.  The semantic text check
                # is authoritative here, including the pending-prediction
                # response that the game treats as a normal continuation.
                if not self.tianxing_prefix_response_ok(
                    TIANXING_TIANJI_PREFIX_COMMAND, prefix_text
                ):
                    wait_seconds = self.tianxing_prefix_wait_seconds(prefix_text)
                    if wait_seconds > 0:
                        raise MiniAppBeastError(
                            f"tianji_destiny_prefix_wait:{wait_seconds}"
                        )
                    raise MiniAppBeastError("tianji_destiny_prefix_failed")
                require_command(self, identity, TIANXING_TIANJI_PREFIX_COMMAND)
                forged = await transport.forge_treasure(
                    identity,
                    "treasure_001",
                    times=1,
                    log_operation=False,
                    required_command=TIANXING_TIANJI_PREFIX_COMMAND,
                )
                apply_dwelling_snapshot(self, identity, forged)
                result = forged.get("actionResult") if isinstance(forged, dict) else {}
                if isinstance(result, dict) and result.get("ok") is False:
                    raise MiniAppBeastError(
                        str(result.get("error") or "tianji_forge_failed")
                    )
                state.update(
                    {
                        "tianxing_tianji_completed": completed + 1,
                        "tianxing_tianji_last_time": now_str(),
                        "tianxing_tianji_last_result": (
                            str(result.get("rawMessage") or result.get("message") or "玄铁剑炼制完成")
                            if isinstance(result, dict)
                            else "玄铁剑炼制完成"
                        )[:240],
                        "tianxing_tianji_error": "",
                        "tianxing_tianji_retry_time": "",
                    }
                )
                self.save_state()
            return True
        except asyncio.CancelledError:
            raise
        except SectTaskStopped:
            return False
        except MiniAppCircuitOpenError as exc:
            retry_seconds = miniapp_circuit_wait_seconds(exc, 300)
            state.update(
                {
                    "tianxing_tianji_error": exc.code,
                    "tianxing_tianji_last_time": now_str(),
                    "tianxing_tianji_retry_time": add_seconds_str(
                        now_str(), retry_seconds
                    ),
                }
            )
            self.save_state()
            self.common_command_logger().info(
                "Tianxing Tianji grind [%s] paused by upstream circuit until %s",
                identity,
                exc.retry_at or f"in {retry_seconds}s",
            )
            return False
        except Exception as exc:
            error_text = str(exc)
            match = re.fullmatch(r"tianji_destiny_prefix_wait:(\d+)", error_text)
            if match:
                retry_seconds = max(1, int(match.group(1)))
                state.update(
                    {
                        "tianxing_tianji_error": "",
                        "tianxing_tianji_last_result": f"推命冷却，{retry_seconds} 秒后重试",
                        "tianxing_tianji_last_time": now_str(),
                        "tianxing_tianji_retry_time": add_seconds_str(now_str(), retry_seconds),
                    }
                )
                self.save_state()
                self.common_command_logger().info(
                    "Tianxing Tianji prefix [%s] is cooling down; retrying in %ss.",
                    identity,
                    retry_seconds,
                )
                return True
            state.update(
                {
                    "tianxing_tianji_error": error_text[:240],
                    "tianxing_tianji_last_time": now_str(),
                    "tianxing_tianji_retry_time": add_seconds_str(now_str(), 300),
                }
            )
            self.save_state()
            self.common_command_logger().error(
                "Tianxing Tianji grind [%s] error: %s",
                identity,
                exc,
                exc_info=True,
            )
            return True

    def _summarize_tianxing_tianji_round_if_complete(
        self, identities, target, round_id
    ):
        """Log one total after every selected identity finishes the round."""
        identities = [str(identity or "").strip() for identity in identities]
        identities = [identity for identity in identities if identity]
        if not identities or target <= 0 or not round_id:
            return False
        states = [self.tianxing_identity_state(identity) for identity in identities]
        if any(
            state.get("tianxing_tianji_round_id") != round_id
            or max(0, int(state.get("tianxing_tianji_completed") or 0)) < target
            for state in states
        ):
            return False
        summary_key = f"{round_id}|{target}|{'|'.join(identities)}"
        if self.state.get("tianxing_tianji_summary_key") == summary_key:
            return False
        total = sum(
            min(target, max(0, int(state.get("tianxing_tianji_completed") or 0)))
            for state in states
        )
        self.state.update(
            {
                "tianxing_tianji_summary_key": summary_key,
                "tianxing_tianji_summary_total": total,
                "tianxing_tianji_summary_time": now_str(),
            }
        )
        self.save_state()
        self.common_command_logger().info(
            "刷天机值完成：总数 %s/%s，身份 %s",
            total,
            target,
            "、".join(identities),
        )
        return True

    async def run_tianxing_tianji_grind_loop(self):
        """Run the independent Tianji-value skill for every Tianxing identity."""
        await self.startup_done.wait()
        while self.is_running:
            config = tianxing_settings()
            selected = set(
                tianxing_tianji_identities_for_account(
                    getattr(self, "account_key", "main") or "main",
                    {"tianxing": config},
                )
            )
            identities = [
                identity
                for identity in self.tianxing_identity_names()
                if identity in selected
            ]
            if not config.get("tianji_grind_enabled") or not identities:
                await asyncio.sleep(60)
                continue
            target = max(0, int(config.get("tianji_grind_target") or 0))
            round_id = str(config.get("tianji_round_id") or "")
            if target <= 0:
                await asyncio.sleep(300)
                continue
            try:
                transport = self.miniapp_small_world_transport()
            except Exception as exc:
                log.error("Tianxing Tianji transport unavailable: %s", exc)
                await asyncio.sleep(300)
                continue
            made_progress = False
            for identity in identities:
                if not self.is_running:
                    break
                made_progress = (
                    await self._run_tianxing_tianji_identity_round(
                        identity, target, round_id, transport
                    )
                    or made_progress
                )
                await asyncio.sleep(3)
            self._summarize_tianxing_tianji_round_if_complete(
                identities, target, round_id
            )
            await asyncio.sleep(3 if made_progress else 60)


    def parse_tianxing_destiny_options(self, text):
        clean = str(text or "").replace("**", "")
        found = []
        for match in re.finditer("|".join(TIANXING_DESTINY_CHOICES), clean):
            name = match.group(0)
            if name not in found:
                found.append(name)
        return found

    def tianxing_prefix_response_ok(self, command, text):
        """Require semantic success before following a .推命/.改命 action."""
        command = str(command or "").strip()
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        if self.tianxing_prefix_is_pending(clean):
            return True
        failure_markers = (
            "已有一道", "尚未应验", "还需等待", "冷却", "无法", "不能",
            "不足", "失败",
        )
        if any(marker in clean for marker in failure_markers):
            return False
        if command.startswith((".推命 ", ".改命 ")):
            return any(
                marker in clean
                for marker in ("推命命中", "推下一段命数", "预留了一次", "执行成功", "成功")
            )
        return True

    def tianxing_prefix_is_pending(self, text):
        """Detect an already-active prediction/change without treating it as an action ban."""
        clean = str(text or "").replace("**", "")
        prediction_active = "推命" in clean and "尚未应验" in clean
        change_active = "改命" in clean and any(
            marker in clean for marker in ("尚未耗尽", "还可维持", "尚可维持")
        )
        return prediction_active or change_active

    def tianxing_prefix_wait_seconds(self, text):
        """Parse a standalone prediction cooldown, excluding a pending prediction reply."""
        clean = str(text or "").replace("**", "")
        if self.tianxing_prefix_is_pending(clean):
            return 0
        if not any(marker in clean for marker in ("还需等待", "冷却", "后再")):
            return 0
        wait_seconds = self.parse_wait_time(text)
        return max(0, int(wait_seconds or 0))

    async def send_tianxing_identity_command(self, identity, command, **kwargs):
        identity = self.resolve_avatar_identity(identity)
        if not self.sect_operation_allowed(identity, command):
            return None
        if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
            return await self.send_and_wait_feedback_identity(identity, command, **kwargs)
        return await self.send_and_wait_feedback(command, **kwargs)

    def tianxing_miniapp_route_unavailable(self):
        state = getattr(self, "state", {})
        if getattr(self, "_restricted_miniapp_worker", None) is not None:
            return not state.get("restricted_miniapp_active")
        return isinstance(state, dict) and state.get("miniapp_route_active") is False

    def tianxing_destiny_retry_wait_seconds(self, identity="主魂"):
        state = self.tianxing_identity_state(identity)
        retry_at = str(state.get("next_tianxing_destiny_retry_time") or "").strip()
        if not retry_at or not is_future(retry_at):
            return 0
        return max(1, int(seconds_until(retry_at)) + 1)

    def defer_tianxing_destiny_for_miniapp_route(self, identity="主魂", action=""):
        """Pause Tianxing destiny work while its Mini App route is unavailable."""
        if not self.tianxing_miniapp_route_unavailable():
            state = self.tianxing_identity_state(identity)
            if state.get("tianxing_destiny_retry_reason") == "miniapp_route_unavailable":
                state["next_tianxing_destiny_retry_time"] = ""
                state["tianxing_destiny_retry_reason"] = ""
                self.save_state()
            return 0

        identity = str(identity or "主魂").strip() or "主魂"
        state = self.tianxing_identity_state(identity)
        existing_wait = self.tianxing_destiny_retry_wait_seconds(identity)
        if existing_wait > 0 and state.get("tianxing_destiny_retry_reason") == "miniapp_route_unavailable":
            return existing_wait

        route_retry_at = str(
            getattr(self, "state", {}).get("miniapp_route_retry_at") or ""
        ).strip()
        route_wait = int(seconds_until(route_retry_at)) + 5 if is_future(route_retry_at) else 0
        wait = max(TIANXING_DESTINY_ROUTE_RETRY_SECONDS, route_wait)
        state["next_tianxing_destiny_retry_time"] = add_seconds_str(now_str(), wait)
        state["tianxing_destiny_retry_reason"] = "miniapp_route_unavailable"

        last_log = str(state.get("tianxing_destiny_failure_log_time") or "")
        try:
            elapsed = (datetime.now() - datetime.strptime(last_log, TIME_FORMAT)).total_seconds()
        except (TypeError, ValueError):
            elapsed = float("inf")
        if elapsed >= TIANXING_DESTINY_FAILURE_LOG_SUPPRESS_SECONDS:
            state["tianxing_destiny_failure_log_time"] = now_str()
            self.common_command_logger().warning(
                "Tianxing destiny deferred [%s/%s]: Mini App route is unavailable; "
                "retrying no earlier than %s",
                identity,
                str(action or "action").strip() or "action",
                state["next_tianxing_destiny_retry_time"],
            )
        self.save_state()
        return wait

    def record_tianxing_destiny_observation(self, identity, options, today=None):
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self.tianxing_identity_state(identity)
        state["last_destiny_observation_date"] = today
        state["last_destiny_observation_time"] = now_str()
        state["tianxing_destiny_options_date"] = today
        state["tianxing_destiny_options"] = list(dict.fromkeys(options))
        self.save_state()

    async def observe_tianxing_destiny(self, identity="主魂", force=False):
        identity = str(identity or "主魂").strip() or "主魂"
        if not self.tianxing_identity_enabled(identity):
            return True
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.tianxing_identity_state(identity)
        if self.defer_tianxing_destiny_for_miniapp_route(identity, "observation") > 0:
            return False
        if not force and state.get("last_destiny_observation_date") == today:
            return bool(state.get("tianxing_destiny_options"))
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(
            ".观命", identity
        ):
            return False

        response = await self.send_tianxing_identity_command(
            identity,
            ".观命",
            timeout=90,
            max_retries=0,
        )
        text = self.response_text(response).replace("**", "")
        options = self.parse_tianxing_destiny_options(text)
        already_fixed = any(
            marker in text
            for marker in ("今日已定命", "命轨已定", "今日命轨定在")
        )
        if not text or (
            any(keyword in text for keyword in TIANXING_DESTINY_FAILURE_KEYWORDS)
            and not already_fixed
        ):
            if self.defer_tianxing_destiny_for_miniapp_route(identity, "observation") > 0:
                return False
            self.common_command_logger().warning(
                "Tianxing destiny observation failed [%s]: %s",
                identity,
                text[:160] or "empty response",
            )
            return False
        if not options:
            self.common_command_logger().warning(
                "Tianxing destiny observation had no candidates [%s]: %s",
                identity,
                text[:160],
            )
            return False

        self.record_tianxing_destiny_observation(identity, options, today=today)
        if already_fixed:
            current = next(
                (
                    name
                    for name in TIANXING_DESTINY_CHOICES
                    if re.search(rf"(?:定在|命星)[^天府紫微贪狼太阴]*{name}", text)
                ),
                options[0] if len(options) == 1 else "",
            )
            if current:
                state["last_destiny_date"] = today
                state["last_destiny_time"] = now_str()
                state["last_destiny_choice"] = current
                self.save_state()
        self.common_command_logger().info(
            "Tianxing destiny candidates [%s]: %s",
            identity,
            "、".join(options),
        )
        return True

    def unavailable_meditation_destiny(self, identity="主魂"):
        """Distinguish a missing daily candidate from an unconfirmed observation."""
        preferences = self.meditation_destiny_preferences(identity)
        if not preferences:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        state = self.tianxing_identity_state(identity)
        if (
            state.get("last_destiny_observation_date") != today
            or state.get("tianxing_destiny_options_date") != today
        ):
            return ""
        options = [name for name in state.get("tianxing_destiny_options", [])
                   if name in TIANXING_DESTINY_CHOICES]
        return "、".join(preferences) if options and not any(name in options for name in preferences) else ""

    async def ensure_tianxing_destiny_for_action(self, identity, action):
        identity = str(identity or "主魂").strip() or "主魂"
        action = str(action or "").strip().casefold()
        preferences = TIANXING_DESTINY_ACTION_PREFERENCES.get(action)
        if not preferences or not self.tianxing_identity_enabled(identity):
            return True
        if self.defer_tianxing_destiny_for_miniapp_route(identity, action) > 0:
            return False

        today = datetime.now().strftime("%Y-%m-%d")
        state = self.tianxing_identity_state(identity)
        if (
            state.get("last_destiny_observation_date") != today
            or state.get("tianxing_destiny_options_date") != today
            or not state.get("tianxing_destiny_options")
        ):
            if not await self.observe_tianxing_destiny(identity):
                return False
            state = self.tianxing_identity_state(identity)

        options = [
            name
            for name in state.get("tianxing_destiny_options", [])
            if name in TIANXING_DESTINY_CHOICES
        ]
        # Apply daily-meditation preferences only to cultivation.
        daily_preferences = self.meditation_destiny_preferences(identity) if action == "cultivation" else ()
        if daily_preferences:
            preferences = daily_preferences
        choice = next((name for name in preferences if name in options), "")
        if not choice:
            if daily_preferences and self._defer_meditation_for_missing_destiny(
                identity, self.meditation_config(identity)
            ):
                return False
            self.common_command_logger().error(
                "Tianxing destiny has no usable candidate [%s/%s]: %s",
                identity,
                action,
                "、".join(options) or "none",
            )
            return False
        if (
            state.get("last_destiny_date") == today
            and state.get("last_destiny_choice") == choice
        ):
            return True

        command = f".定命 {choice}"
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(
            command, identity
        ):
            return False
        response = await self.send_tianxing_identity_command(
            identity,
            command,
            timeout=90,
            max_retries=0,
        )
        text = self.response_text(response).replace("**", "")
        success = (
            bool(text)
            and choice in text
            and any(
                marker in text
                for marker in ("命轨定在", "定下命星", "今日命轨", "定命成功", "命星切换")
            )
            and not any(keyword in text for keyword in TIANXING_DESTINY_FAILURE_KEYWORDS)
        )
        if not success:
            if self.defer_tianxing_destiny_for_miniapp_route(identity, action) > 0:
                return False
            self.common_command_logger().error(
                "Tianxing destiny was not confirmed [%s/%s]: %s",
                identity,
                action,
                text[:160] or "empty response",
            )
            return False

        state["last_destiny_date"] = today
        state["last_destiny_time"] = now_str()
        state["last_destiny_choice"] = choice
        self.save_state()
        self.common_command_logger().info(
            "Tianxing destiny selected [%s/%s]: %s", identity, action, choice
        )
        return True

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

    def mulan_support_command(self):
        return current_mulan_support_command()

    def common_atomic_task(self, label, log_lifecycle=True):
        return _CommonAtomicTask(self, label, log_lifecycle=log_lifecycle)

    async def run_avatar_meditation_restart_chain(
        self,
        avatar,
        *,
        initial_check_text=None,
        check_first=True,
        prefix="",
        source="meditation",
        check_timeout=30,
        check_retries=1,
        cultivation_timeout=45,
        cultivation_retries=1,
        deep_timeout=60,
        deep_retries=1,
    ):
        """Run .查看闭关 -> optional prefix -> .闭关修炼 -> .深度闭关 as one identity chain."""
        if self.identity_meditation_mode(avatar) != "deep":
            await self.configured_meditation_tick(avatar)
            return {"status": "deferred", "wait": 60, "text": "闭关按身份设置执行"}
        async with self.common_atomic_task(f"Meditation-{avatar}"):
            check_text = str(initial_check_text or "")
            if check_first and initial_check_text is None:
                check_resp = await self.send_and_wait_feedback_identity(
                    avatar, ".查看闭关", timeout=check_timeout, max_retries=check_retries,
                )
                check_text = self.response_text(check_resp)

            if check_text:
                if is_deep_meditation_ongoing_response(check_text) or "预计还需" in check_text:
                    cd = self.parse_wait_time(check_text)
                    if cd > 0:
                        self.update_avatar_states(
                            avatar,
                            self.meditation_active_state_values(add_seconds_str(now_str(), cd)),
                        )
                        return {"status": "ongoing", "wait": cd, "text": check_text}
                    return {"status": "ongoing_unknown", "wait": 300, "text": check_text}
                if not (
                    is_deep_meditation_settlement_response(check_text)
                    or is_not_deep_meditation_response(check_text)
                ):
                    return {"status": "unknown_check", "wait": 600, "text": check_text}

            await asyncio.sleep(3)
            if not await self.ensure_tianxing_destiny_for_action(avatar, "cultivation"):
                return {
                    "status": "destiny_failed",
                    "wait": max(600, self.tianxing_destiny_retry_wait_seconds(avatar)),
                    "text": "",
                }
            prefix = str(prefix or "").strip()
            if prefix:
                await self.send_and_wait_feedback_identity(avatar, f"{prefix} 闭关")
                await asyncio.sleep(3)

            cultivation_resp = await self.send_and_wait_feedback_identity(
                avatar, ".闭关修炼", timeout=cultivation_timeout, max_retries=cultivation_retries,
            )
            cultivation_text = self.response_text(cultivation_resp)
            if not any(k in cultivation_text for k in ["闭关成功", "闭关失败"]):
                retry_cd = self.defer_meditation_after_cultivation_cooldown(
                    avatar, cultivation_text, f"[{avatar}] {source} .闭关修炼",
                )
                if retry_cd:
                    return {"status": "deferred", "wait": retry_cd, "text": cultivation_text}

            await asyncio.sleep(3)
            deep_resp = await self.send_and_wait_feedback_identity(
                avatar, ".深度闭关", timeout=deep_timeout, max_retries=deep_retries,
            )
            deep_text = self.response_text(deep_resp)
            started = await self.record_avatar_deep_meditation_start(avatar, deep_text)
            return {
                "status": "started" if started else "failed",
                "wait": 60 if started else 600,
                "text": deep_text,
            }

    # ---- 每日收益汇总 ----

    def daily_reward_enabled_commands(self):
        return {
            ".元婴出窍",
            ".元婴闭关",
            ".探寻裂缝",
            ".野外历练",
            ".登天阶",
            ".收集精华",
            ".探渊",
            ".灵兽探渊",
            ".问道",
            ".支援慕兰",
            ".闯塔",
            ".洞府寻宝",
        }

    def daily_reward_account_label(self):
        key = getattr(self, "account_key", "") or ""
        return {
            "main": "主号",
            "sub": "副号",
            "xiaohao": "小号",
            "waaiging": "Waaiging",
        }.get(key, key or self.__class__.__name__)

    def daily_reward_summary_push_enabled(self):
        """Telegram push is opt-in; the Dashboard/event log remains the primary viewer."""
        config = getattr(self, "config", {}) or {}
        return bool(config.get("daily_reward_summary_push", False))

    def clean_reward_text(self, text):
        return shared_clean_reward_text(text)

    def daily_reward_edited_settlement_commands(self):
        return {
            ".探寻裂缝",
            ".野外历练",
            ".探渊",
            ".灵兽探渊",
            ".元婴闭关",
            ".元婴出窍",
            ".问道",
            ".支援慕兰",
            ".闯塔",
        }

    def daily_reward_parse_text_for_command(self, command, text):
        """Return the text segment that should be counted for daily rewards."""
        return shared_reward_text_for_command(command, text)

    def daily_reward_field_training_settlement_text(self, text):
        return shared_field_training_settlement_text(text)

    def daily_reward_rift_settlement_text(self, text):
        return shared_rift_settlement_text(text)

    def parse_reward_items_from_text(self, text):
        """Best-effort parser for command rewards used by the daily summary."""
        return shared_parse_reward_items(text)

    def daily_reward_outcome_from_text(self, command, text, rewards=None):
        root = str(command or "").split()[0] if command else ""
        full_clean = self.clean_reward_text(text)
        clean = self.daily_reward_parse_text_for_command(root or command, text)
        rewards = rewards if isinstance(rewards, dict) else {}
        if root == ".支援慕兰":
            if not is_mulan_settlement_text(full_clean):
                return ""
            if "险还" in full_clean:
                return "脱险"
            if any(marker in full_clean for marker in ("惨败", "败退", "任务失败")):
                return "失败"
            return "成功" if rewards or "边境军功" in full_clean else ""
        if root in {".探渊", ".灵兽探渊"}:
            if any(marker in full_clean for marker in ("不敌", "重伤退回", "败退", "失败")):
                return "失败"
            if rewards or any(marker in full_clean for marker in ("击败", "战利品归来", "探渊胜利")):
                return "成功"
            return ""
        if root == ".闯塔":
            if any(marker in full_clean for marker in ("无法闯塔", "挑战失败", "修为不足")):
                return "失败"
            if rewards or any(marker in full_clean for marker in ("试炼古塔 - 战报", "总收获", "本次共闯过")):
                return "成功"
            return ""
        if root == ".问道":
            if any(k in clean for k in ["未加入", "不是元婴宗", "无法问道", "条件不足", "境界不足", "修为不足"]):
                return "失败"
            if any(k in clean for k in ["冷却", "后再", "尚需", "剩余", "请在"]):
                return ""
            if rewards or self.is_ask_dao_response(clean):
                return "成功"
        if root == ".野外历练":
            cultivation = int(rewards.get("修为", 0) or 0)
            if any(marker in clean for marker in ("改命脱险", "改命回天", "未损修为")):
                return "脱险"
            failure_markers = (
                "失败", "失利", "负伤而归", "一时判断失误", "修为折损",
                "修为倒退", "扣除修为", "损失修为", "消耗修为",
            )
            if cultivation < 0 or any(marker in clean for marker in failure_markers):
                return "失败"
            if cultivation > 0:
                return "成功"
            if self.is_field_training_settlement_response(clean) or rewards:
                return "失败"
            return ""
        if any(marker in clean for marker in (
            "失败", "不敌败退", "身受重创", "遭受重创", "倒退", "折损",
            "肉身破碎", "肉身化为", "元婴遁逃", "虚弱期", "大凶",
        )):
            return "失败"
        if rewards:
            return "成功"
        return ""

    def daily_reward_is_final_settlement_text(self, command, text):
        clean = self.clean_reward_text(text)
        root = str(command or "").split()[0] if command else ""
        if root == ".野外历练":
            return self.is_field_training_settlement_response(clean)
        if root == ".深度闭关":
            return "深度闭关总结" in clean or "修为最终变化" in clean
        if root in {".元婴出窍", ".元婴闭关"}:
            return any(k in clean for k in ("元神归窍总结", "元婴归窍总结", "带回了以下收获", "元婴闭关结算"))
        if root == ".探寻裂缝":
            if any(k in clean for k in ("冷却", "后再", "尚未", "请在", "送入其中探寻机缘")):
                return False
            return any(k in clean for k in (
                "为你带来了", "带回了", "获得修为", "获得了", "收获", "发现",
                "时空异兽", "遭遇风暴", "不敌败退", "身受重创", "遭受重创",
                "肉身破碎", "肉身化为", "元婴遁逃", "虚弱期", "大凶", "修为倒退",
            ))
        if root in {".探渊", ".灵兽探渊"}:
            return any(k in clean for k in ("探渊", "万兽渊", "获得", "收获", "带回", "战利品", "奖励"))
        if root == ".支援慕兰":
            return is_mulan_settlement_text(clean)
        if root == ".闯塔":
            if any(k in clean for k in ("冷却", "今日已闯", "修为不足", "无法闯塔")):
                return False
            return any(k in clean for k in ("试炼古塔 - 战报", "总收获", "本次共闯过")) or bool(
                self.daily_reward_items_for_command(command, clean)
            )
        if root == ".问道":
            if any(k in clean for k in ("冷却", "后再", "尚需", "剩余", "请在")):
                return False
            return self.is_ask_dao_response(clean)
        return bool(self.daily_reward_items_for_command(command, clean))

    def daily_reward_sorted_reward_items(self, rewards, priority=False):
        if not rewards:
            return []
        if not priority:
            return sorted(rewards)

        priority_names = {
            "修为": 0,
            "天机": 1,
            "天机值": 1,
            "宗门贡献": 2,
            "贡献": 2,
            "边境军功": 3,
            "塔印": 4,
            "灵石": 5,
            "神识": 6,
            "气血": 7,
            "煞气": 8,
            "道韵": 9,
            "感悟": 10,
            "经验": 11,
            "星辰精华": 12,
            "精华": 13,
        }

        def sort_key(name):
            text = str(name or "")
            if text in priority_names:
                return (priority_names[text], text)
            if text.startswith("法则碎片"):
                return (20, text)
            if text.endswith("妖丹"):
                return (30, text)
            return (100, text)

        return sorted(rewards, key=sort_key)

    def summarize_reward_items(self, rewards, limit=None, priority=False):
        if not rewards:
            return ""
        names = self.daily_reward_sorted_reward_items(rewards, priority=priority)
        hidden = 0
        if limit and len(names) > limit:
            hidden = len(names) - limit
            names = names[:limit]
        parts = []
        for name in names:
            value = int(rewards.get(name, 0) or 0)
            if value > 0:
                parts.append(f"{name} +{value}")
            else:
                parts.append(f"{name} {value}")
        if hidden:
            parts.append(f"等 {hidden} 项")
        return "、".join(parts)

    def daily_reward_merge_rewards(self, target, rewards):
        if not isinstance(rewards, dict):
            return target
        for name, value in rewards.items():
            target[name] = int(target.get(name, 0) or 0) + int(value or 0)
        return target

    def daily_reward_outcome_summary_text(self, outcomes, unparsed=0):
        parts = []
        if outcomes:
            preferred = [name for name in ("成功", "失败") if name in outcomes]
            preferred += [name for name in sorted(outcomes) if name not in preferred]
            parts.extend(f"{name} {outcomes[name]}" for name in preferred)
        if unparsed:
            parts.append(f"未解析 {unparsed}")
        return " / ".join(parts)

    def daily_reward_bucket_outcome_text(self, bucket, include_unparsed=False):
        outcome_text = self.daily_reward_outcome_summary_text(
            bucket.get("outcomes") or {},
            bucket.get("unparsed", 0) if include_unparsed else 0,
        )
        return f"（{outcome_text}）" if outcome_text else ""

    def daily_reward_empty_reward_label(self, bucket):
        outcomes = bucket.get("outcomes") or {}
        if outcomes.get("失败") and not any(name != "失败" for name in outcomes):
            return "无收益"
        return "未解析"

    def daily_reward_plain_command_label(self, command):
        return str(command or "未知指令").split()[0] or "未知指令"

    def daily_reward_context_reward_items(self, command, text):
        """Parse useful destiny-side gains that should be shown in compact daily details."""
        return shared_context_reward_items(command, text)

    def daily_reward_items_for_command(self, command, text):
        """Parse one command settlement using command-aware reward boundaries."""
        return shared_daily_reward_items_for_command(command, text)

    def daily_reward_command_short_label(self, command):
        root = str(command or "").split()[0] if command else ""
        return {
            ".野外历练": "历练",
            ".探寻裂缝": "裂缝",
            ".元婴出窍": "出窍",
            ".元婴闭关": "闭关",
            ".探渊": "探渊",
            ".灵兽探渊": "探渊",
            ".登天阶": "登阶",
            ".收集精华": "精华",
            ".问道": "问道",
            ".支援慕兰": "慕兰",
            ".闯塔": "问心塔",
            ".洞府寻宝": "寻宝",
        }.get(root, root.lstrip(".") or "未知")

    def daily_reward_command_counts_compact_text(self, command_counts):
        if not command_counts:
            return ""
        ordered = ["历练", "裂缝", "出窍", "闭关", "探渊", "问道", "慕兰", "登阶", "问心塔", "寻宝", "精华"]
        normalized = {}
        for command, count in command_counts.items():
            label = self.daily_reward_command_short_label(command)
            normalized[label] = int(normalized.get(label, 0) or 0) + int(count or 0)
        labels = [label for label in ordered if label in normalized]
        labels.extend(sorted(label for label in normalized if label not in labels))
        return "｜".join(f"{label}{normalized[label]}" for label in labels)

    def daily_reward_display_reward_name(self, name):
        text = str(name or "").strip()
        if text == "宗门贡献":
            return "贡献"
        if text == "天机值":
            return "天机"
        return text

    def daily_reward_format_number(self, value):
        try:
            return f"{int(value):,}"
        except Exception:
            return str(value or 0)

    def daily_reward_compact_reward_item(self, name, value):
        display = self.daily_reward_display_reward_name(name)
        try:
            amount = int(value or 0)
        except Exception:
            return ""
        if not display or amount == 0:
            return ""
        plus_names = {
            "修为", "天机", "贡献", "边境军功", "塔印", "神识", "气血", "煞气",
            "道韵", "感悟", "经验", "星辰精华", "精华",
        }
        if amount < 0:
            return f"{display}{self.daily_reward_format_number(amount)}"
        if display in plus_names:
            return f"{display}+{self.daily_reward_format_number(amount)}"
        return f"{display}x{self.daily_reward_format_number(amount)}"

    def summarize_reward_items_compact(self, rewards, limit=None, priority=True):
        if not rewards:
            return ""
        normalized = {}
        for name, value in rewards.items():
            display = self.daily_reward_display_reward_name(name)
            if not display:
                continue
            normalized[display] = int(normalized.get(display, 0) or 0) + int(value or 0)
        names = self.daily_reward_sorted_reward_items(normalized, priority=priority)
        hidden = 0
        if limit and len(names) > limit:
            hidden = len(names) - limit
            names = names[:limit]
        parts = []
        for name in names:
            item = self.daily_reward_compact_reward_item(name, normalized.get(name, 0))
            if item:
                parts.append(item)
        if hidden:
            parts.append(f"等{hidden}项")
        return "｜".join(parts)

    def daily_reward_outcome_counts_compact_text(self, outcomes, unparsed=0, always=False):
        outcomes = outcomes or {}
        parts = []
        for name in ("成功", "脱险", "失败"):
            count = int(outcomes.get(name, 0) or 0)
            if always or count:
                parts.append(f"{name}: {count}")
        for name in sorted(outcomes):
            if name not in {"成功", "脱险", "失败"}:
                parts.append(f"{name}: {int(outcomes.get(name, 0) or 0)}")
        if unparsed:
            parts.append(f"未解析: {int(unparsed)}")
        return "｜".join(parts)

    def daily_reward_event_time_label(self, event_time):
        match = re.search(r"(\d{2}):(\d{2})", str(event_time or ""))
        return f"{match.group(1)}:{match.group(2)}" if match else "--:--"

    def daily_reward_event_outcome_label(self, event):
        outcome = event.get("outcome") or ""
        clean = event.get("clean") or event.get("excerpt") or ""
        command = event.get("command") or ""
        root = str(command or "").split()[0]
        if outcome == "脱险":
            return "~改命脱险" if "改命" in clean else "~脱险"
        short_label = self.daily_reward_command_short_label(command)
        if outcome == "失败":
            return f"✗{short_label}"
        if root == ".野外历练":
            if "灵机" in clean:
                return "✓灵机"
            if any(k in clean for k in ("妖兽遭遇", "斗法", "伏诛", "胜算")):
                return "✓胜"
            return "✓历练"
        if root in {
            ".探寻裂缝", ".元婴出窍", ".元婴闭关",
            ".探渊", ".灵兽探渊", ".问道", ".登天阶", ".收集精华",
            ".支援慕兰",
        }:
            return f"✓{short_label}"
        return f"✓{short_label}" if outcome == "成功" else "?"

    def daily_reward_event_detail_text(self, event):
        rewards = event.get("rewards") or {}
        reward_text = self.summarize_reward_items_compact(rewards, limit=10, priority=True)
        if not reward_text:
            reward_text = "无收益" if event.get("outcome") == "失败" else "未解析"
        return (
            f"{self.daily_reward_event_time_label(event.get('time'))} "
            f"{self.daily_reward_event_outcome_label(event)}｜{reward_text}"
        )

    def telegram_markdown_v2_escape(self, text):
        return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text or ""))

    def telegram_markdown_v2_bold(self, text):
        return f"*{self.telegram_markdown_v2_escape(text)}*"

    def telegram_markdown_v2_code(self, text):
        clean = str(text or "").replace("\\", "\\\\").replace("`", "\\`")
        return f"`{clean}`"

    def daily_reward_message_key(self, msg, identity, command):
        msg_id = getattr(msg, "id", None)
        if msg_id is None:
            return ""
        root = str(command or "").split()[0] if command else ""
        return f"{identity}|{root or command}|{msg_id}"

    def record_daily_reward_event(self, identity, command, text, source="", msg=None, final=False, outcome=""):
        self.ensure_common_command_state()
        command = str(command or "").strip()
        root = command.split()[0] if command else ""
        if command not in self.daily_reward_enabled_commands() and root not in self.daily_reward_enabled_commands():
            return False
        clean = self.clean_reward_text(text)
        if not clean:
            return False
        mentions = text_username_mentions(clean)
        tracked_identity = tracked_command_identity_for_reply(self, msg) if msg is not None else ""
        if (
            mentions
            and not tracked_identity
            and not identity_from_single_username_mention(self, clean)
            and not text_targets_current_account(self, msg, clean)
        ):
            return False

        now = datetime.now()
        event_date = now.strftime("%Y-%m-%d")
        event_time = now.strftime(TIME_FORMAT)
        identity = str(identity or "主魂").strip() or "主魂"
        message_key = self.daily_reward_message_key(msg, identity, root or command)
        sig = hashlib.sha1(f"{identity}|{root or command}|{clean[:1200]}".encode("utf-8")).hexdigest()
        events = self.state.get("daily_reward_events")
        if not isinstance(events, list):
            events = []
            self.state["daily_reward_events"] = events

        rewards = self.daily_reward_items_for_command(root or command, clean)
        final = bool(final or self.daily_reward_is_final_settlement_text(root or command, clean))
        outcome = outcome or self.daily_reward_outcome_from_text(root or command, clean, rewards)
        if not final and not rewards:
            return False
        if message_key:
            for old in reversed(events[-80:]):
                if old.get("date") != event_date or old.get("message_key") != message_key:
                    continue
                old_rewards = old.get("rewards") if isinstance(old.get("rewards"), dict) else {}
                should_replace = (
                    final and not old.get("final")
                    or (rewards and not old_rewards)
                    or clean != old.get("clean")
                )
                if not should_replace:
                    return False
                excerpt = re.sub(r"\s+", " ", clean)
                if len(excerpt) > 180:
                    excerpt = excerpt[:180] + "..."
                old.update({
                    "time": event_time,
                    "source": source or command,
                    "rewards": rewards,
                    "excerpt": excerpt,
                    "clean": clean,
                    "sig": sig,
                    "final": final,
                    "outcome": outcome,
                })
                record_daily_reward_event_log(self, old, logger=self.common_command_logger())
                self.save_state()
                return True

        for old in reversed(events[-30:]):
            if old.get("sig") != sig:
                continue
            try:
                age = (now - str_to_dt(old.get("time", ""))).total_seconds()
            except Exception:
                age = 999999
            if 0 <= age <= 120:
                return False

        excerpt = re.sub(r"\s+", " ", clean)
        if len(excerpt) > 180:
            excerpt = excerpt[:180] + "..."
        event_entry = {
            "date": event_date,
            "time": event_time,
            "identity": identity,
            "command": root or command,
            "source": source or command,
            "rewards": rewards,
            "excerpt": excerpt,
            "clean": clean,
            "sig": sig,
            "message_key": message_key,
            "final": final,
            "outcome": outcome,
        }
        events.append(event_entry)
        record_daily_reward_event_log(self, event_entry, logger=self.common_command_logger())

        cutoff = (now - timedelta(days=4)).strftime("%Y-%m-%d")
        self.state["daily_reward_events"] = [
            event for event in events
            if str(event.get("date", "")) >= cutoff
        ]
        self.save_state()
        return True

    def daily_reward_command_from_text(self, text, fallback_command=""):
        clean = self.clean_reward_text(text)
        fallback = str(fallback_command or "").strip()
        fallback_root = fallback.split()[0] if fallback else ""
        if fallback_root in self.daily_reward_enabled_commands():
            return fallback
        if "野外历练" in clean:
            return ".野外历练"
        if "深度闭关总结" in clean or "修为最终变化" in clean:
            return ".深度闭关"
        if "元婴闭关结算" in clean:
            return ".元婴闭关"
        if is_yuanying_out_settlement_response(clean):
            return ".元婴出窍"
        if any(k in clean for k in ("探寻裂缝", "时空异兽", "不敌败退")):
            return ".探寻裂缝"
        if any(k in clean for k in ("探渊", "万兽渊")):
            return ".探渊"
        if any(k in clean for k in ("问道", "悟道", "论道", "道韵", "大道感悟")):
            return ".问道"
        if any(k in clean for k in ("慕兰烽烟", "边境军功", "连续支援")):
            return ".支援慕兰"
        if any(k in clean for k in ("琉璃问心塔", "试炼古塔 - 战报", "闯塔历程", "本次共闯过")):
            return ".闯塔"
        return ""

    def daily_reward_identity_from_message(self, msg, text):
        identity = tracked_command_identity_for_reply(self, msg)
        if identity:
            return identity
        identity = avatar_marker_identity_from_text(text)
        if identity:
            return identity
        identity = identity_from_single_username_mention(self, text)
        if identity:
            return identity
        identity = self._field_training_identity_from_text(text)
        if identity:
            return identity
        return getattr(self, "current_identity", "主魂") or "主魂"

    def maybe_record_daily_reward_from_edited_message(self, msg, text, source="edited message"):
        if is_reply_to_untracked_message(self, msg):
            return False
        clean = self.clean_reward_text(text)
        if not clean:
            return False
        fallback_command = tracked_command_text_for_reply(self, msg)
        command = self.daily_reward_command_from_text(clean, fallback_command=fallback_command)
        root = command.split()[0] if command else ""
        if root not in self.daily_reward_edited_settlement_commands():
            return False
        if not self.daily_reward_is_final_settlement_text(command, clean):
            return False
        identity = self.daily_reward_identity_from_message(msg, clean)
        if (
            not tracked_command_identity_for_reply(self, msg)
            and not avatar_marker_identity_from_text(clean)
            and not identity_from_single_username_mention(self, clean)
            and not text_targets_current_account(self, msg, clean)
        ):
            return False
        return self.record_daily_reward_event(
            identity,
            command,
            clean,
            source=source,
            msg=msg,
            final=True,
        )

    def build_daily_reward_summary_markdown_text(self, summary_date, grouped):
        overall = {"count": 0, "rewards": {}, "unparsed": 0, "outcomes": {}, "events": [], "command_counts": {}}
        identity_order = ["主魂"] + [name for name in getattr(self, "avatars", []) if name != "主魂"]
        identity_order += [name for name in grouped if name not in identity_order]
        identity_totals = {}

        for identity in identity_order:
            commands = grouped.get(identity)
            if not commands:
                continue
            identity_total = {"count": 0, "rewards": {}, "unparsed": 0, "outcomes": {}, "events": [], "command_counts": {}}
            for command, bucket in commands.items():
                identity_total["count"] += int(bucket.get("count", 0) or 0)
                identity_total["unparsed"] += int(bucket.get("unparsed", 0) or 0)
                identity_total["command_counts"][command] = int(identity_total["command_counts"].get(command, 0) or 0) + int(bucket.get("count", 0) or 0)
                self.daily_reward_merge_rewards(identity_total["rewards"], bucket.get("rewards") or {})
                for name, count in (bucket.get("outcomes") or {}).items():
                    identity_total["outcomes"][name] = int(identity_total["outcomes"].get(name, 0) or 0) + int(count or 0)
                identity_total["events"].extend(bucket.get("events") or [])

            overall["count"] += identity_total["count"]
            overall["unparsed"] += identity_total["unparsed"]
            self.daily_reward_merge_rewards(overall["rewards"], identity_total["rewards"])
            for name, count in identity_total["outcomes"].items():
                overall["outcomes"][name] = int(overall["outcomes"].get(name, 0) or 0) + int(count or 0)
            overall["events"].extend(identity_total["events"])
            for command, count in identity_total["command_counts"].items():
                overall["command_counts"][command] = int(overall["command_counts"].get(command, 0) or 0) + int(count or 0)
            identity_totals[identity] = identity_total

        overall_commands = self.daily_reward_command_counts_compact_text(overall.get("command_counts") or {})
        overall_text = (
            f"有效{overall['count']}｜结算{overall['count']}"
            + (f"｜{overall_commands}" if overall_commands else "")
            + (f"｜{self.daily_reward_outcome_counts_compact_text(overall['outcomes'], overall['unparsed'], always=True)}")
            + (f"｜{self.summarize_reward_items_compact(overall['rewards'], limit=12, priority=True)}" if overall["rewards"] else "")
        )
        lines = [
            self.telegram_markdown_v2_bold("周期收益日报"),
            self.telegram_markdown_v2_escape(f"统计日期：{summary_date}"),
            self.telegram_markdown_v2_escape(f"账号：{self.daily_reward_account_label()}"),
            self.telegram_markdown_v2_escape(f"总计：{overall_text}"),
            "",
            self.telegram_markdown_v2_bold("账号明细:"),
        ]

        for identity in identity_order:
            commands = grouped.get(identity)
            if not commands:
                continue
            total = identity_totals.get(identity) or {"count": 0, "rewards": {}, "unparsed": 0, "outcomes": {}}
            identity_text = (
                f"有效{total['count']}｜结算{total['count']}"
                + (f"｜{self.daily_reward_command_counts_compact_text(total.get('command_counts') or {})}" if total.get("command_counts") else "")
                + f"｜{self.daily_reward_outcome_counts_compact_text(total.get('outcomes') or {}, total.get('unparsed', 0), always=True)}"
            )
            identity_rewards = self.summarize_reward_items_compact(total.get("rewards") or {}, limit=10, priority=True)
            if identity_rewards:
                identity_text += f"｜{identity_rewards}"
            lines.append(
                "\\- "
                + self.telegram_markdown_v2_bold(identity)
                + self.telegram_markdown_v2_escape(f": {identity_text}")
            )
            events = sorted(
                total.get("events") or [],
                key=lambda item: str(item.get("time") or ""),
            )
            for event in events:
                lines.append("  \\- " + self.telegram_markdown_v2_escape(self.daily_reward_event_detail_text(event)))
        return "\n".join(lines)

    def build_daily_reward_summary_text(self, summary_date, markdown=False):
        events = [
            event for event in self.state.get("daily_reward_events", [])
            if event.get("date") == summary_date
        ]
        if not events:
            return ""

        grouped = {}
        for event in events:
            identity = event.get("identity") or "主魂"
            command = event.get("command") or "未知指令"
            command_root = str(command or "").split()[0]
            if command_root == ".深度闭关":
                continue
            raw_text = event.get("clean") or event.get("excerpt") or ""
            if command_root == ".支援慕兰" and raw_text and not is_mulan_settlement_text(raw_text):
                # Older records captured the short "正赶往天南边境" acknowledgement
                # as a fake reward. Only the edited battle settlement is revenue.
                continue
            if not event.get("final") and not event.get("rewards"):
                continue
            stored_rewards = event.get("rewards") if isinstance(event.get("rewards"), dict) else {}
            if not stored_rewards and raw_text and not self.daily_reward_is_final_settlement_text(command, raw_text):
                continue
            bucket = grouped.setdefault(identity, {}).setdefault(command, {
                "count": 0,
                "rewards": {},
                "unparsed": 0,
                "samples": [],
                "outcomes": {},
                "events": [],
            })
            bucket["count"] += 1
            rewards = self.daily_reward_items_for_command(command, raw_text) if raw_text else {}
            if command_root == ".野外历练" and rewards:
                rewards.pop("宗门贡献", None)
                rewards.pop("贡献", None)
            trust_empty_reparse = trust_empty_reward_reparse(command, raw_text)
            if not rewards and not trust_empty_reparse and isinstance(event.get("rewards"), dict):
                rewards = dict(event.get("rewards") or {})
                if command_root == ".野外历练":
                    rewards.pop("宗门贡献", None)
                    rewards.pop("贡献", None)
            outcome = self.daily_reward_outcome_from_text(command, raw_text, rewards) if raw_text else ""
            outcome = outcome or event.get("outcome") or ""
            if outcome:
                bucket["outcomes"][outcome] = int(bucket["outcomes"].get(outcome, 0) or 0) + 1
            if rewards:
                for name, value in rewards.items():
                    bucket["rewards"][name] = int(bucket["rewards"].get(name, 0) or 0) + int(value or 0)
            else:
                if outcome != "失败":
                    bucket["unparsed"] += 1
                if event.get("excerpt") and len(bucket["samples"]) < 2:
                    bucket["samples"].append(event.get("excerpt"))
            bucket["events"].append({
                "time": event.get("time", ""),
                "identity": identity,
                "command": command,
                "rewards": dict(rewards),
                "outcome": outcome,
                "excerpt": event.get("excerpt", ""),
                "clean": raw_text,
                "final": bool(event.get("final") or rewards),
            })

        if not grouped:
            return ""

        if markdown:
            return self.build_daily_reward_summary_markdown_text(summary_date, grouped)

        lines = [
            f"统计日期：{summary_date}",
            f"账号：{self.daily_reward_account_label()}",
        ]
        identity_order = ["主魂"] + [name for name in getattr(self, "avatars", []) if name != "主魂"]
        identity_order += [name for name in grouped if name not in identity_order]
        for identity in identity_order:
            commands = grouped.get(identity)
            if not commands:
                continue
            lines.append("")
            lines.append(f"【{identity}】")
            for command in sorted(commands):
                bucket = commands[command]
                reward_text = self.summarize_reward_items(bucket["rewards"])
                detail = reward_text if reward_text else "收益未解析"
                if bucket["unparsed"] and reward_text:
                    detail += f"；未解析 {bucket['unparsed']} 次"
                outcome_text = self.daily_reward_bucket_outcome_text(bucket)
                lines.append(
                    f"- {self.daily_reward_plain_command_label(command)}："
                    f"{bucket['count']} 次{outcome_text}；{detail}"
                )
                if not reward_text:
                    for sample in bucket["samples"]:
                        lines.append(f"  - 摘录：{sample}")
        return "\n".join(lines)

    async def send_daily_reward_summary_for_date(self, summary_date):
        self.ensure_common_command_state()
        if self.state.get("daily_reward_last_sent_date") == summary_date:
            return False
        text = self.build_daily_reward_summary_text(summary_date, markdown=True)
        self.state["daily_reward_last_sent_date"] = summary_date
        self.save_state()
        if not text:
            return False
        log = self.common_command_logger()
        if not self.daily_reward_summary_push_enabled():
            log.info(f"Daily reward summary push disabled for {summary_date}; retained in event log/dashboard.")
            return False
        sent = await send_text_alert(self, "周期收益日报", text, log, parse_mode="MarkdownV2")
        if sent:
            log.info(f"Daily reward summary sent for {summary_date}.")
        else:
            log.warning(f"Daily reward summary failed for {summary_date}; marked sent to avoid spam.")
        return sent

    def seconds_until_daily_reward_summary(self, now=None):
        now = now or datetime.now()
        target = now.replace(hour=0, minute=10, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        return max(1, int((target - now).total_seconds()))

    async def run_daily_reward_summary_loop(self, initial_delay=0):
        await self.startup_done.wait()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        while getattr(self, "is_running", True):
            try:
                now = datetime.now()
                today_target = now.replace(hour=0, minute=10, second=0, microsecond=0)
                if now >= today_target:
                    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    await self.send_daily_reward_summary_for_date(yesterday)
                await asyncio.sleep(min(self.seconds_until_daily_reward_summary(), 3600))
            except Exception as exc:
                self.common_command_logger().error(f"Daily reward summary loop error: {exc}", exc_info=True)
                await asyncio.sleep(300)

    # ---- 身份级暂停（元婴虚弱等）----

    def ensure_identity_pause_state(self):
        pauses = self.state.get("identity_pauses")
        if not isinstance(pauses, dict):
            pauses = {}
            self.state["identity_pauses"] = pauses
            self.save_state()
        return pauses

    def identity_pause_entry(self, identity):
        identity = self.resolve_avatar_identity(identity)
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
        if entry.get("wait_for_rebirth"):
            return YUANYING_REBIRTH_WAIT_SECONDS
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
        identity = self.resolve_avatar_identity(identity)
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
        identity = self.resolve_avatar_identity(identity)
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

    def set_identity_rebirth_pending_pause(self, identity, reason="肉体破碎/元婴虚弱，等待 .重生"):
        identity = self.resolve_avatar_identity(identity)
        reason = str(reason or "").strip() or "肉体破碎/元婴虚弱，等待 .重生"
        pauses = self.ensure_identity_pause_state()
        pauses[identity] = {
            "until": "",
            "reason": reason,
            "wait_for_rebirth": True,
            "started_at": now_str(),
        }
        if identity == "主魂":
            self.state["main_soul_pause_until"] = ""
            self.state["main_soul_pause_reason"] = reason
        self.save_state()
        return "等待 .重生 1 / .重生 2 / .重生 3 任一成功"

    def clear_identity_pause(self, identity="主魂", reason=""):
        identity = self.resolve_avatar_identity(identity)
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
            clean = str(text or "").replace("**", "").replace("`", "")
            old_match = re.search(r"原道号\s*[：:]\s*([^）)\s，,]+)", clean)
            new_match = re.search(r"将以\s*【([^】]+)】\s*为名", clean)
            old_name = old_match.group(1).strip() if old_match else ""
            new_name = new_match.group(1).strip() if new_match else ""
            rename_from = old_name if old_name in getattr(self, "avatars", []) else identity
            if new_name and rename_from in getattr(self, "avatars", []):
                refreshed = self.refresh_avatar_dao_name(rename_from, new_name)
                if refreshed in getattr(self, "avatars", []):
                    identity = refreshed

                for transient_name in TRANSIENT_AVATAR_DAO_NAMES:
                    self.clear_identity_command_guard(
                        transient_name,
                        reason="yuanying rebirth Dao name refresh",
                    )

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

        wait_for_rebirth = bool(self.identity_pause_entry(identity).get("wait_for_rebirth"))
        if wait_for_rebirth:
            pause_until = self.identity_pause_entry(identity).get("until", "") or "等待 .重生 1 / .重生 2 / .重生 3 任一成功"
            self.clear_identity_command_guard(identity, reason="yuanying rebirth pending")
            log.warning(
                f"Yuanying rebirth still pending for [{identity}] from {source or command or 'message'}; "
                "auto commands remain paused until rebirth succeeds."
            )
            return True

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

    def mark_identity_rift_rebirth_pending(self, identity, response="", source=".探寻裂缝"):
        identity = str(identity or "主魂").strip() or "主魂"
        pause_label = self.set_identity_rebirth_pending_pause(
            identity,
            "肉体破碎/元婴虚弱，等待重生",
        )
        if identity == "主魂":
            self.state["next_rift_search_time"] = ""
        elif identity in getattr(self, "avatars", []):
            self.set_avatar_state(identity, "next_rift_search_time", "")
        self.clear_identity_command_guard(identity, reason="rift rebirth pending")
        self.save_state()
        self.common_command_logger().critical(
            f"Rift weakness detected from {source}. Identity [{identity}] paused until rebirth succeeds. "
            f"{response or ''}"
        )
        return pause_label

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
                until = entry.get("until", "") or ("等待 .重生 1 / .重生 2 / .重生 3 任一成功" if entry.get("wait_for_rebirth") else "")
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
        until = entry.get("until", "") or ("等待 .重生 1 / .重生 2 / .重生 3 任一成功" if entry.get("wait_for_rebirth") else "")
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

    def meditation_protected_until(self, state, grace_seconds=MEDITATION_SETTLEMENT_GRACE_SECONDS):
        """Return the latest time before which .查看闭关 should not be sent.

        The game often still reports a short remaining deep-meditation timer at
        the local end timestamp, so every cached end time is protected by a
        small settlement grace window.
        """
        if not isinstance(state, dict):
            return ""
        now = datetime.now()
        candidates = []

        guard_time = state.get("deep_meditation_guard_until", "")
        if guard_time:
            try:
                guard_dt = datetime.strptime(str(guard_time), TIME_FORMAT)
                if guard_dt > now:
                    candidates.append(guard_dt)
            except Exception:
                pass

        end_time = state.get("deep_meditation_end_time", "")
        if end_time:
            try:
                end_dt = datetime.strptime(str(end_time), TIME_FORMAT)
                protected_dt = end_dt + timedelta(seconds=int(grace_seconds or 0))
                if protected_dt > now:
                    candidates.append(protected_dt)
            except Exception:
                pass

        if not candidates:
            return ""
        return dt_to_str(max(candidates))

    def ensure_meditation_guard_from_end_time(self, state):
        """Repair deep meditation guard state from a cached end time."""
        if not isinstance(state, dict):
            return False
        protected_until = self.meditation_protected_until(state)
        guard_time = state.get("deep_meditation_guard_until", "")
        changed = False

        if protected_until:
            try:
                should_update = (
                    not guard_time
                    or not is_future(guard_time)
                    or datetime.strptime(str(guard_time), TIME_FORMAT) < datetime.strptime(protected_until, TIME_FORMAT)
                )
            except Exception:
                should_update = True
            if should_update:
                state["deep_meditation_guard_until"] = protected_until
                changed = True
            if state.get("deep_meditation_end_time") and not state.get("in_deep_meditation"):
                state["in_deep_meditation"] = True
                changed = True
            return changed

        if guard_time and not is_future(guard_time):
            state["deep_meditation_guard_until"] = ""
            changed = True
        return changed

    def meditation_guard_wait_seconds_for_state(self, state):
        protected_until = self.meditation_protected_until(state)
        return seconds_until(protected_until) if protected_until else 0

    def meditation_guard_active_for_state(self, state):
        return self.meditation_guard_wait_seconds_for_state(state) > 0

    def compact_duration_text(self, seconds):
        seconds = max(0, int(seconds or 0))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        parts = []
        if hours:
            parts.append(f"{hours}小时")
        if minutes:
            parts.append(f"{minutes}分钟")
        if secs or not parts:
            parts.append(f"{secs}秒")
        return "".join(parts)

    def early_meditation_check_response_for_state(self, identity, state, logger=None):
        if self.ensure_meditation_guard_from_end_time(state):
            self.save_state()
        wait_seconds = self.meditation_guard_wait_seconds_for_state(state)
        if wait_seconds <= 0:
            return ""
        log = logger or self.common_command_logger()
        log.debug(
            f"[{identity}] skipped early .查看闭关; meditation protected for "
            f"{self.compact_duration_text(wait_seconds)}."
        )
        return f"你正在深度闭关，预计还需 **{self.compact_duration_text(wait_seconds)}** 即可功成圆满。"

    def meditation_active_state_values(self, end_time, clear_restart=True):
        values = {
            "in_deep_meditation": True,
            "deep_meditation_end_time": end_time,
            "deep_meditation_guard_until": add_seconds_str(
                end_time, MEDITATION_SETTLEMENT_GRACE_SECONDS
            ) if end_time else "",
            "next_meditation_retry_time": "",
            "next_meditation_time": "",
        }
        if clear_restart:
            values["meditation_restart_pending"] = False
            values["meditation_restart_mode"] = ""
        return values

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

    def custom_command_state_guard_wait(self, identity, command):
        """Return state cooldown wait for a dashboard custom command, or -1 if runnable."""
        identity = str(identity or "主魂").strip() or "主魂"
        command = str(command or "").strip()
        if not command:
            return -1

        if identity == "主魂":
            state = getattr(self, "state", {})
        elif identity in (getattr(self, "avatars", []) or []) and hasattr(self, "get_avatar_state"):
            state = self.get_avatar_state(identity)
        else:
            state = {}
        if not isinstance(state, dict):
            return -1

        waits = []
        for key, mapped_command in STATE_TIME_COMMAND_MAP.items():
            if not self.command_matches_prefix(command, mapped_command):
                continue
            value = state.get(key, "")
            if isinstance(value, str) and value and is_future(value):
                waits.append(seconds_until(value))
        return min(waits) if waits else -1

    def merge_impending_wait(self, base_wait, extra_wait):
        if extra_wait is None or extra_wait < 0:
            return base_wait
        if base_wait is None or base_wait < 0 or base_wait >= 999999:
            return extra_wait
        return min(base_wait, extra_wait)

    def state_time_command_for_key(self, key):
        return STATE_TIME_COMMAND_MAP.get(str(key or ""))

    def retired_auto_command_for_identity(self, command, identity="主魂"):
        """Return whether a legacy schedule must be ignored for this identity."""
        return is_retired_auto_command(
            command,
            actor=self,
            identity=str(identity or "主魂").strip() or "主魂",
        )

    def command_matches_prefix(self, command, prefix):
        command = str(command or "").strip()
        prefix = str(prefix or "").strip()
        return bool(command and prefix and (command == prefix or command.startswith(f"{prefix} ")))

    def is_low_priority_daily_command(self, command):
        return any(
            self.command_matches_prefix(command, prefix)
            for prefix in LOW_PRIORITY_DAILY_COMMANDS
        )

    def iter_identity_states_for_priority(self):
        state = getattr(self, "state", {})
        if isinstance(state, dict):
            yield "主魂", state
        for identity in getattr(self, "avatars", []) or []:
            try:
                avatar_state = self.get_avatar_state(identity)
            except Exception:
                continue
            if isinstance(avatar_state, dict):
                yield identity, avatar_state

    def priority_due_key_enabled(self, identity, key):
        if identity == "主魂":
            return True
        features = (getattr(self, "avatar_features", {}) or {}).get(identity, {})
        if not features:
            return True
        feature_keys = {
            "next_yuanying_out_time": "yuanying_out",
            "next_rift_search_time": "rift_search",
            "next_dream_map_time": "dream_map",
            "next_heart_trial_time": "heart_trial",
            "next_formation_time": "formation",
            "next_formation_retry_time": "formation",
            "next_star_gazing_time": "star_gazing",
            "pending_star_gazing_target_time": "star_gazing",
            "pending_star_shift_target_time": "star_gazing",
            "next_star_check_time": "star_attraction",
            "next_star_appease_time": "star_attraction",
            "next_star_collect_time": "star_attraction",
            "next_star_attraction_time": "star_attraction",
        }
        feature = feature_keys.get(key)
        if not feature:
            return True
        if key in {"next_formation_time", "next_formation_retry_time"}:
            return bool(features.get("formation"))
        return bool(features.get(feature))

    def priority_due_work_summary(self, grace_seconds=0):
        """Return the first non-daily due command, used to make one-shot dailies yield."""
        try:
            grace_seconds = max(0, int(grace_seconds))
        except Exception:
            grace_seconds = 0
        now = datetime.now()

        for identity, state in self.iter_identity_states_for_priority():
            try:
                if self.identity_pause_seconds(identity) > 0:
                    continue
            except Exception:
                pass

            for key in STATE_TIME_COMMAND_MAP:
                command = self.state_time_command_for_key(key)
                if not command:
                    continue
                if self.retired_auto_command_for_identity(command, identity):
                    continue
                if self.is_low_priority_daily_command(command):
                    continue
                if not self.priority_due_key_enabled(identity, key):
                    continue
                if self.state_time_command_paused(key, identity):
                    continue
                value = state.get(key, "")
                if not isinstance(value, str) or not value:
                    continue
                try:
                    target = str_to_dt(value)
                except Exception:
                    continue
                overdue = int((now - target).total_seconds())
                if overdue >= grace_seconds:
                    return {
                        "identity": identity,
                        "key": key,
                        "command": command,
                        "due_at": value,
                        "overdue_seconds": overdue,
                    }

            if identity != "主魂" and hasattr(self, "avatar_meditation_needs_attention"):
                try:
                    if self.avatar_meditation_needs_attention(identity):
                        return {
                            "identity": identity,
                            "key": "avatar_meditation",
                            "command": ".查看闭关",
                            "due_at": "",
                            "overdue_seconds": 0,
                        }
                except Exception:
                    continue
        return {}

    def daily_one_shot_should_defer(self, identity="主魂", command="", logger=None):
        """Let daily one-shot commands run only after cooldown/retry work has caught up."""
        command = str(command or "").strip()
        if command and not self.is_low_priority_daily_command(command):
            return False
        due = self.priority_due_work_summary()
        if not due:
            return False

        now = time.monotonic()
        last = getattr(self, "_daily_low_priority_defer_log_last", 0) or 0
        if logger and now - last >= LOW_PRIORITY_DAILY_LOG_INTERVAL_SECONDS:
            logger.info(
                f"Daily one-shot [{command or 'daily'}] for [{identity}] deferred; "
                f"priority due work [{due.get('identity')}] {due.get('command')} "
                f"({due.get('key')}) is overdue by {int(due.get('overdue_seconds') or 0)}s."
            )
            setattr(self, "_daily_low_priority_defer_log_last", now)
        return True

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
        stale_changed = False
        now = datetime.now()
        for state in states:
            if not isinstance(state, dict):
                continue
            for key, value in state.items():
                command = self.state_time_command_for_key(key)
                if not command or not self.time_critical_identity_command(command):
                    continue
                if self.retired_auto_command_for_identity(command, identity):
                    continue
                if exclude_command and self.command_matches_prefix(exclude_command, command):
                    continue
                if self.state_time_command_paused(key, identity):
                    continue
                if isinstance(value, str) and value:
                    try:
                        target = datetime.strptime(value, TIME_FORMAT)
                    except Exception:
                        target = None
                    if target and target > now:
                        candidates.append(max(0, (target - now).total_seconds()))
                    elif (
                        key in STALE_STAR_TIME_CRITICAL_KEYS
                        and (
                            target is None
                            or (now - target).total_seconds() > STALE_STAR_TIME_CRITICAL_GRACE_SECONDS
                        )
                    ):
                        state[key] = ""
                        stale_changed = True
                        try:
                            self.common_command_logger().info(
                                f"[{identity}] cleared stale star schedule {key}={value!r}."
                            )
                        except Exception:
                            pass
                    else:
                        candidates.append(0)

        if stale_changed:
            try:
                self.save_state()
            except Exception:
                pass
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

    async def prepare_identity_for_time_critical_command(
        self,
        identity,
        command=".观星",
        timeout=20,
        force_fresh=False,
        return_switch_message_id=False,
    ):
        """Pre-switch identity before a narrow-window command without sending the command itself.

        ``force_fresh`` always emits a new ``.切换`` message even when the
        requested identity is already active.  Duel target preparation uses
        that concrete outgoing message as the cross-account reply anchor.
        """
        identity = str(identity or "主魂").strip() or "主魂"
        command = str(command or "").strip() or ".观星"
        log = self.common_command_logger()

        def prepared(success, message_id=None):
            if return_switch_message_id:
                try:
                    message_id = int(message_id) if message_id is not None else None
                except (TypeError, ValueError):
                    message_id = None
                return bool(success), message_id
            return bool(success)

        if hasattr(self, "wait_while_identity_paused"):
            if not await self.wait_while_identity_paused(identity, command):
                return prepared(False)

        if not command_send_precheck(self, command, log, identity=identity):
            return prepared(False)

        while hasattr(self, "should_wait_for_atomic_task") and self.should_wait_for_atomic_task(command):
            await asyncio.sleep(0.5)

        resolver = getattr(self, "resolve_avatar_identity", None)

        current = getattr(self, "current_identity", "主魂") or "主魂"
        main_confirmed = bool(getattr(self, "_main_confirmed", current == "主魂"))
        if not force_fresh and current == identity and (identity != "主魂" or main_confirmed):
            return prepared(True)

        lock = getattr(self, "avatar_send_lock", None)
        if lock is None or not hasattr(self, "_send_and_wait_feedback_raw"):
            return prepared(False)

        async with lock:
            # The rename can happen while waiting for the lock.  Re-resolve
            # again at the actual send boundary; this is the last point at
            # which we can prevent an obsolete `.切换 <old-name>` message.
            if callable(resolver):
                identity = resolver(identity)
            current = getattr(self, "current_identity", "主魂") or "主魂"
            if callable(resolver):
                current = resolver(current)
            main_confirmed = bool(getattr(self, "_main_confirmed", current == "主魂"))
            if not force_fresh and current == identity and (identity != "主魂" or main_confirmed):
                return prepared(True)

            ban_time = (getattr(self, "state", {}) or {}).get("next_switch_allowed_time", "")
            if ban_time and is_future(ban_time):
                log.info(
                    f"Pre-switch for {command} skipped: switch command is cooling until {ban_time}."
                )
                return prepared(False)

            switch_cmd = f".切换 {identity}"
            previous_sent_id = getattr(self, "last_sent_id", None)
            log.info(
                f"Pre-switch for {command}: {current} -> {identity} before time-critical send"
                f"{' (fresh reply anchor)' if force_fresh else ''}."
            )
            switch_resp = await self._send_and_wait_feedback_raw(
                switch_cmd,
                timeout=timeout,
                max_retries=1,
                suppress_no_response_alert=True,
                delete_after=not return_switch_message_id,
            )
            resp_text = self.timed_command_response_text(switch_resp)
            switch_message_id = getattr(self, "last_sent_id", None)
            try:
                switch_message_id = int(switch_message_id)
            except (TypeError, ValueError):
                switch_message_id = None
            try:
                previous_sent_id = int(previous_sent_id)
            except (TypeError, ValueError):
                previous_sent_id = None

            if hasattr(self, "check_and_record_switch_ban") and self.check_and_record_switch_ban(resp_text):
                return prepared(False)
            if not resp_text and hasattr(self, "apply_switch_guard_backoff"):
                if self.apply_switch_guard_backoff(switch_cmd):
                    return prepared(False)

            if force_fresh and (
                switch_message_id is None
                or (previous_sent_id is not None and switch_message_id == previous_sent_id)
            ):
                log.info(
                    f"Pre-switch for {command} to {identity} did not produce a fresh reply anchor."
                )
                return prepared(False)

            passively_confirmed = getattr(self, "current_identity", "") == identity
            confirmed = passively_confirmed or (
                resp_text and any(k in resp_text for k in ["成功", "已切换", "当前操控", identity])
            )
            if not confirmed:
                log.info(
                    f"Pre-switch for {command} to {identity} was not confirmed; "
                    f"response={resp_text[:120]!r}."
                )
                return prepared(False)

            self.current_identity = identity
            self._main_confirmed = (identity == "主魂")
            log.info(f"Pre-switch for {command}: identity ready as {identity}.")
            return prepared(True, switch_message_id)

    async def sleep_then_prepare_time_critical_identity(self, send_dt, identity, command=".观星", lead_seconds=20):
        """Sleep until the pre-switch lead window, switch identity, then return near send time."""
        try:
            lead_seconds = max(0, int(lead_seconds))
        except Exception:
            lead_seconds = 20
        now = datetime.now()
        pre_switch_at = send_dt - timedelta(seconds=lead_seconds)
        wait_before_switch = (pre_switch_at - now).total_seconds()
        if wait_before_switch > 0:
            await asyncio.sleep(wait_before_switch)
        await self.prepare_identity_for_time_critical_command(identity, command=command)
        remaining = (send_dt - datetime.now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def state_time_command_paused(self, key, identity=""):
        command = self.state_time_command_for_key(key)
        if not command:
            return False
        return dashboard_command_disabled(self, command, identity or "主魂")[0]

    def dashboard_command_paused(self, command, identity=""):
        return dashboard_command_disabled(self, command, identity or "主魂")[0]

    def dashboard_command_option(self, command, field, default=None, identity=""):
        return dashboard_command_control_value(
            self,
            command,
            field,
            default=default,
            identity=identity or "主魂",
        )

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

    async def run_second_soul_loop(self):
        """第二元神修炼循环：每 24 小时发送 `.元神修炼`。

        若返回“无法分心修炼”，再查询 `.第二元神` 解析剩余冷却时间，
        按真实剩余时间安排下次执行，而不是固定顺延 24 小时。
        """
        interval_seconds = SECOND_SOUL_INTERVAL_SECONDS
        await self.startup_done.wait()
        log = self.common_command_logger()

        while self.is_running:
            next_time = str(self.state.get("next_second_soul_time") or "")
            now = datetime.now()
            if not next_time:
                next_dt = now + timedelta(seconds=interval_seconds)
                self.state["next_second_soul_time"] = next_dt.strftime(TIME_FORMAT)
                self.save_state()
            else:
                try:
                    next_dt = datetime.strptime(next_time, TIME_FORMAT)
                except (ValueError, TypeError):
                    next_dt = now + timedelta(seconds=interval_seconds)
                    self.state["next_second_soul_time"] = next_dt.strftime(TIME_FORMAT)
                    self.save_state()
                wait = (next_dt - now).total_seconds()
                if wait > 0:
                    await asyncio.sleep(min(wait, 300))
                    continue

            log.info("Second soul cultivation due: sending .元神修炼.")
            response = await self.send_and_wait_feedback(
                SECOND_SOUL_TRAIN_COMMAND, timeout=45, max_retries=1,
            )
            if second_soul_busy(response):
                log.info("Second soul busy; querying .第二元神 for remaining cooldown.")
                check_response = await self.send_and_wait_feedback(
                    SECOND_SOUL_STATUS_COMMAND, timeout=45, max_retries=1,
                )
                remaining = second_soul_cooldown_seconds(check_response)
                if remaining is None:
                    # 状态回复未含时间格式（回复缺失/被截断/格式漂移）——
                    # 不要 fallback 到整个间隔，5 分钟后重新查询。
                    log.info(
                        "Second soul status had no parseable cooldown; "
                        "retrying .第二元神 in %ss.",
                        SECOND_SOUL_RECHECK_SECONDS,
                    )
                    self.state["next_second_soul_time"] = (
                        datetime.now() + timedelta(seconds=SECOND_SOUL_RECHECK_SECONDS)
                    ).strftime(TIME_FORMAT)
                    self.save_state()
                    await asyncio.sleep(5)
                    continue
                remaining += SECOND_SOUL_COOLDOWN_BUFFER_SECONDS
                next_dt = datetime.now() + timedelta(seconds=remaining)
            else:
                next_dt = datetime.now() + timedelta(seconds=interval_seconds)

            self.state["next_second_soul_time"] = next_dt.strftime(TIME_FORMAT)
            self.save_state()
            log.info(
                "Second soul cultivation done; next at %s.",
                next_dt.strftime(TIME_FORMAT),
            )
            await asyncio.sleep(5)

    def record_second_soul_manual_status(self, text, identity="主魂"):
        """同步手动 `.第二元神` 查询结果到排期（无时间格式不动 state）。

        手动查询返回剩余冷却时，把 next_second_soul_time 对齐到真实
        冷却结束时刻（+缓冲），避免脚本按旧排期盲发 `.元神修炼`。
        回复无时间格式时返回 False，不修改 state。
        """
        try:
            remaining = second_soul_cooldown_seconds(text)
        except Exception:
            return False
        if remaining is None:
            return False
        remaining += SECOND_SOUL_COOLDOWN_BUFFER_SECONDS
        next_dt = datetime.now() + timedelta(seconds=remaining)
        old = str(self.state.get("next_second_soul_time") or "")
        new = next_dt.strftime(TIME_FORMAT)
        if old == new:
            return False
        self.state["next_second_soul_time"] = new
        self.save_state()
        self.common_command_logger().info(
            "Second soul schedule synced from manual .第二元神 for %s: next at %s (was %s).",
            identity, new, old or "(none)",
        )
        return True

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

                    state_wait = self.custom_command_state_guard_wait(identity, command)
                    if state_wait > 0:
                        run_state = self.custom_command_run_state(custom_id)
                        run_state["identity"] = identity
                        run_state["command"] = command
                        run_state["last_status"] = "state_cooldown"
                        run_state["next_run_at"] = add_seconds_str(now_str(), state_wait)
                        self.save_state()
                        log.info(
                            f"Custom command deferred by state cooldown "
                            f"[{identity}] {command}: {state_wait:.0f}s."
                        )
                        next_sleep = min(next_sleep, state_wait)
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

    def field_training_plan(self, identity="主魂"):
        """Build the configured field-training command plan for an identity."""
        identity = str(identity or "主魂").strip() or "主魂"
        features = {}
        if identity != "主魂":
            features = (getattr(self, "avatar_features", {}) or {}).get(identity, {})
        return field_training_plan_from_features(
            identity=identity,
            features=features,
            main_command=getattr(self, "field_training_command", FIELD_TRAINING_COMMAND),
        )

    def yuanying_command_for_identity(self, identity="主魂"):
        return yuanying_command_for_identity(
            identity,
            main_command=getattr(self, "yuanying_main_command", ".元婴出窍"),
        )

    def yuanying_out_plan(self, identity="主魂"):
        return yuanying_out_plan(
            identity,
            main_command=getattr(self, "yuanying_main_command", ".元婴出窍"),
        )

    def rift_search_plan(self, identity="主魂"):
        return rift_search_plan(identity)

    def treasure_touch_plan(self, command=None):
        return treasure_touch_plan(command or getattr(self, "treasure_touch_command", None))

    def nurture_spirit_plan(self, command=None):
        return nurture_spirit_plan(command or getattr(self, "nurture_spirit_command", None))

    def ask_dao_plan(self, command=None):
        return ask_dao_plan(command or getattr(self, "ask_dao_command", None))

    async def send_timed_command_plan(self, plan, identity="主魂"):
        """Send a TimedCommandPlan using the right identity-aware sender."""
        identity = str(identity or "主魂").strip() or "主魂"
        if str(plan.command or "").split()[0] == ".野外历练":
            if not await self.ensure_tianxing_destiny_for_action(identity, "exploration"):
                return None
            prefix = await self.send_tianxing_exploration_prefix(identity)
            if not prefix.get("ok"):
                state = self.tianxing_identity_state(identity)
                state[plan.next_key] = add_seconds_str(
                    now_str(), int(prefix.get("wait") or TIANXING_RIFT_PREFIX_RETRY_SECONDS)
                )
                self.save_state()
                return None
        kwargs = {
            "timeout": plan.timeout,
            "max_retries": plan.max_retries,
            "force_identity_check": plan.force_identity_check,
            "return_response_msg": plan.return_response_msg,
        }
        async def send_once():
            if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
                return await self.send_and_wait_feedback_identity(identity, plan.command, **kwargs)
            return await self.send_and_wait_feedback(plan.command, **kwargs)
        return await wind_thunder_send(self, identity, plan.command, send_once)

    def allow_retired_auto_command(self, command):
        """Only revive Tianxing exploration commands inside the rift prefix chain."""
        command = str(command or "").strip()
        active = str(getattr(self, "_tianxing_rift_prefix_command", "") or "").strip()
        return bool(active and command == active and command in TIANXING_RIFT_PREFIX_COMMANDS)

    def tianxing_rift_prefix_commands(self, identity="主魂"):
        identity = str(identity or "主魂").strip() or "主魂"
        sect = ""
        resolver = getattr(self, "identity_sect_name", None)
        if callable(resolver):
            try:
                sect = str(resolver(identity) or "").strip()
            except Exception:
                sect = ""
        return TIANXING_RIFT_PREFIX_COMMANDS if sect == "天星宗" else ()

    async def send_tianxing_rift_prefixes(self, identity="主魂"):
        identity = str(identity or "主魂").strip() or "主魂"
        prefixes = self.tianxing_rift_prefix_commands(identity)
        if not prefixes:
            return {"ok": True, "wait": 0, "response": None}
        log = self.common_command_logger()
        for command in prefixes:
            log.info(f"Tianxing rift prefix [{identity}]: sending {command}.")
            self._tianxing_rift_prefix_command = command
            try:
                if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
                    response = await self.send_and_wait_feedback_identity(
                        identity,
                        command,
                        timeout=60,
                        max_retries=0,
                        force_identity_check=True,
                    )
                else:
                    response = await self.send_and_wait_feedback(
                        command,
                        timeout=60,
                        max_retries=0,
                        force_identity_check=True,
                    )
            finally:
                self._tianxing_rift_prefix_command = ""
            response_text = self.timed_command_response_text(response)
            if not self.tianxing_prefix_response_ok(command, response_text):
                wait_seconds = self.tianxing_prefix_wait_seconds(response_text)
                if wait_seconds > 0:
                    log.info(
                        f"Tianxing rift prefix [{identity}] {command} is cooling down; "
                        f"retrying after {wait_seconds}s."
                    )
                else:
                    log.warning(
                        f"Tianxing rift prefix [{identity}] {command} had no confirmed success; "
                        f"retrying .探寻裂缝 after {TIANXING_RIFT_PREFIX_RETRY_SECONDS}s."
                    )
                return {
                    "ok": False,
                    "wait": wait_seconds or TIANXING_RIFT_PREFIX_RETRY_SECONDS,
                    "response": response,
                }
            await asyncio.sleep(TIANXING_RIFT_PREFIX_DELAY_SECONDS)
        return {"ok": True, "wait": 0, "response": None}

    async def send_tianxing_exploration_prefix(self, identity="主魂"):
        """Send the Tianxing exploration fate change before wild training."""
        identity = str(identity or "主魂").strip() or "主魂"
        if not self.tianxing_identity_enabled(identity):
            return {"ok": True, "wait": 0, "response": None}
        command = ".改命 探索"
        self._tianxing_rift_prefix_command = command
        try:
            response = await self.send_tianxing_identity_command(
                identity,
                command,
                timeout=90,
                max_retries=0,
                force_identity_check=identity != "主魂",
            )
        finally:
            self._tianxing_rift_prefix_command = ""
        text = self.timed_command_response_text(response)
        if not self.tianxing_prefix_response_ok(command, text):
            return {
                "ok": False,
                "wait": self.tianxing_prefix_wait_seconds(text) or TIANXING_RIFT_PREFIX_RETRY_SECONDS,
                "response": response,
            }
        return {"ok": True, "wait": 0, "response": response}

    async def send_rift_search_plan(self, plan, identity="主魂"):
        identity = str(identity or "主魂").strip() or "主魂"
        prefixes = self.tianxing_rift_prefix_commands(identity)
        if not prefixes:
            return await self.send_timed_command_plan(plan, identity)
        async with self.common_atomic_task(f"Tianxing-rift-{identity}"):
            if not await self.ensure_tianxing_destiny_for_action(identity, "exploration"):
                state = self.tianxing_identity_state(identity)
                wait_seconds = max(
                    TIANXING_RIFT_PREFIX_RETRY_SECONDS,
                    self.tianxing_destiny_retry_wait_seconds(identity),
                )
                state[plan.next_key] = add_seconds_str(
                    now_str(), wait_seconds
                )
                self.save_state()
                return None
            prefix_result = await self.send_tianxing_rift_prefixes(identity)
            if not prefix_result.get("ok"):
                wait_seconds = max(
                    TIANXING_RIFT_PREFIX_RETRY_SECONDS,
                    int(prefix_result.get("wait") or 0),
                )
                state = self.tianxing_identity_state(identity)
                state[plan.next_key] = add_seconds_str(now_str(), wait_seconds)
                self.save_state()
                return None
            return await self.send_timed_command_plan(plan, identity)

    def timed_command_response_text(self, resp):
        if hasattr(self, "response_text"):
            return self.response_text(resp)
        if hasattr(resp, "text"):
            return resp.text or ""
        if isinstance(resp, str):
            return resp
        return str(resp) if resp else ""

    def avatar_timed_command_available(self, avatar, plan, action_name="", require_meditation_ready=False):
        """Shared precheck for avatar timed command loops."""
        avatar = self.resolve_avatar_identity(avatar)
        log = self.common_command_logger()
        if not avatar or avatar not in getattr(self, "avatars", []):
            return False
        if self.identity_pause_seconds(avatar) > 0:
            return False
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(plan.command, avatar):
            return False
        if (
            require_meditation_ready
            and hasattr(self, "avatar_meditation_needs_attention")
            and self.avatar_meditation_needs_attention(avatar)
        ):
            label = action_name or plan.command
            log.info(f"Avatar [{avatar}] {label} skipped: meditation needs restart first.")
            return False
        return True

    async def common_avatar_yuanying_out_check(self, avatar, require_meditation_ready=False):
        """Run one shared avatar .元婴出窍 check."""
        avatar = self.resolve_avatar_identity(avatar)
        plan = self.yuanying_out_plan(avatar)
        if not self.avatar_timed_command_available(
            avatar,
            plan,
            action_name="yuanying",
            require_meditation_ready=require_meditation_ready,
        ):
            return False

        a_state = self.get_avatar_state(avatar)
        end_time = a_state.get("yuanying_out_end_time") or a_state.get("next_yuanying_out_time", "")
        active = a_state.get("yuanying_out_active")
        if active and end_time and is_future(end_time):
            return False
        if active:
            repaired = self._repair_yuanying_out_from_last_start("avatar active expiry guard", identity=avatar)
            if repaired:
                self.save_state()
                return False
            a_state["yuanying_out_active"] = False
            a_state["yuanying_out_end_time"] = ""
            self.save_state()

        next_time = a_state.get(plan.next_key, "")
        if next_time and is_future(next_time):
            return False
        repaired = self._repair_yuanying_out_from_last_start("avatar pre-send guard", identity=avatar)
        if repaired:
            self.save_state()
            return False

        self.common_command_logger().info(f"Avatar [{avatar}] yuanying out due: sending {plan.command}.")
        resp = await self.send_timed_command_plan(plan, avatar)
        self.record_yuanying_out_start_response(self.timed_command_response_text(resp), identity=avatar)
        self.save_state()
        return True

    async def common_avatar_rift_search_check(self, avatar, cd_seconds, require_meditation_ready=False):
        """Run one shared avatar .探寻裂缝 check."""
        avatar = self.resolve_avatar_identity(avatar)
        plan = self.rift_search_plan(avatar)
        if not self.avatar_timed_command_available(
            avatar,
            plan,
            action_name="rift search",
            require_meditation_ready=require_meditation_ready,
        ):
            return False

        a_state = self.get_avatar_state(avatar)
        if not self.should_probe_actual_cooldown(avatar, plan.command):
            repaired_next = self.preserve_cooldown_floor(
                a_state,
                plan.last_key,
                plan.next_key,
                cd_seconds,
                f"avatar rift search [{avatar}]",
            )
            if repaired_next and is_future(repaired_next):
                return False
        next_time = a_state.get(plan.next_key, "")
        if next_time and is_future(next_time):
            return False

        self.common_command_logger().info(f"Avatar [{avatar}] rift search due: sending {plan.command}.")
        resp = await self.send_rift_search_plan(plan, avatar)
        resp_text = self.timed_command_response_text(resp)
        if self.is_rift_weakness_response(resp_text):
            await self.stop_for_rift_weakness(resp_text, identity=avatar)
            return True
        recorded = self.record_identity_fixed_cd_command_response(
            avatar,
            resp_text,
            plan.command,
            plan.last_key,
            plan.next_key,
            cd_seconds,
        )
        if recorded and self.rift_needs_actual_cooldown_probe(
            resp_text, plan.command, identity=avatar
        ):
            await self.probe_rift_actual_cooldown(plan, identity=avatar)
        self.save_state()
        return True

    def avatar_yuanying_rift_wait_seconds(self, avatar):
        """Return the next wakeup for avatar yuanying/rift checks."""
        avatar = self.resolve_avatar_identity(avatar)
        pause_left = self.identity_pause_seconds(avatar)
        if pause_left > 0:
            return max(60, min(int(pause_left), 600))

        a_state = self.get_avatar_state(avatar)
        waits = []

        if a_state.get("yuanying_out_active"):
            end_time = a_state.get("yuanying_out_end_time") or a_state.get("next_yuanying_out_time", "")
            if end_time and is_future(end_time):
                waits.append(seconds_until(end_time))
            else:
                waits.append(0 if end_time else 600)
        else:
            next_yuanying = a_state.get("next_yuanying_out_time", "")
            waits.append(seconds_until(next_yuanying) if next_yuanying and is_future(next_yuanying) else 0)

        next_rift = a_state.get("next_rift_search_time", "")
        waits.append(seconds_until(next_rift) if next_rift and is_future(next_rift) else 0)

        return max(60, int(min(waits or [600])))

    def common_scheduler_sleep_seconds(self, seconds, minimum=1, sleep_func=None):
        scheduler = sleep_func or globals().get("scheduler_sleep_seconds")
        if callable(scheduler):
            return scheduler(seconds, minimum=minimum)
        seconds = max(minimum, float(seconds or 0))
        return max(minimum, min(seconds, 600))

    async def run_common_avatar_yuanying_rift_loop(self, avatar, initial_delay=0, sleep_func=None):
        """Run avatar .元婴出窍 and .探寻裂缝 with shared cooldown scheduling."""
        await self.startup_done.wait()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            try:
                avatar = self.resolve_avatar_identity(avatar)
                await self.pause_event.wait()
                if avatar not in getattr(self, "avatars", []):
                    log.warning(f"Avatar yuanying/rift loop disabled: unknown avatar [{avatar}].")
                    return

                if self.identity_pause_seconds(avatar) <= 0:
                    await self._avatar_yuanying_out_check(avatar)
                if self.identity_pause_seconds(avatar) <= 0:
                    await self._avatar_rift_search_check(avatar)

                wait_sec = self.avatar_yuanying_rift_wait_seconds(avatar)
                log.info(f"Avatar [{avatar}] yuanying/rift loop sleeping {int(wait_sec)}s.")
                await asyncio.sleep(
                    self.common_scheduler_sleep_seconds(
                        wait_sec + random.randint(10, 30),
                        minimum=60,
                        sleep_func=sleep_func,
                    )
                )
            except Exception as exc:
                log.error(f"Avatar [{avatar}] yuanying/rift loop error: {exc}", exc_info=True)
                await asyncio.sleep(300)

    def identity_state_for_timed_command(self, identity):
        identity = str(identity or "主魂").strip() or "主魂"
        if identity != "主魂" and identity in getattr(self, "avatars", []):
            return self.get_avatar_state(identity)
        return self.state

    def record_fixed_cd_command_response(self, resp, command, last_key, next_key, cd_seconds):
        return self.record_identity_fixed_cd_command_response(
            "主魂",
            resp,
            command,
            last_key,
            next_key,
            cd_seconds,
        )

    def record_identity_fixed_cd_command_response(self, identity, resp, command, last_key, next_key, cd_seconds):
        """Shared parser for simple fixed-cooldown command responses."""
        identity = str(identity or "主魂").strip() or "主魂"
        state = self.identity_state_for_timed_command(identity)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        if not resp:
            state[next_key] = add_seconds_str(now_str(), 600)
            self.save_state()
            log.warning(f"{prefix}{command}: response missing; retry scheduled at {state[next_key]}.")
            return False

        if command == NODE_SEARCH_COMMAND and any(
            k in resp for k in ("神识不足", "定位需消耗", "在虚空中定位")
        ):
            # 资源门槛失败：神识不足（虚空定位需 100 点神识，经太一门引道/小世界淬炼补充）。
            # 非冷却也非成功——按 1 小时退避重试，避免落入 unrecognized 的 10 分钟空转刷屏。
            state[next_key] = add_seconds_str(now_str(), 3600)
            self.save_state()
            log.warning(
                f"{prefix}{command}: 神识不足（需太一门引道/小世界淬炼补充），1 小时后重试。"
            )
            return False

        success_with_actual_cd = command == ".探寻裂缝" and self.is_rift_success_response(resp)
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]) and not success_with_actual_cd:
            state[next_key] = add_seconds_str(now_str(), cd)
            self.save_state()
            log.info(f"{prefix}{command}: cooldown from response {cd}s, next at {state[next_key]}.")
            return False

        if any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]) and not success_with_actual_cd:
            state[next_key] = add_seconds_str(now_str(), 600)
            self.save_state()
            log.warning(f"{prefix}{command}: unavailable but no cooldown parsed; retry at {state[next_key]}.")
            return False

        if command == ".探寻裂缝" and self.is_rift_weakness_response(resp):
            self.mark_identity_rift_rebirth_pending(identity, resp, source=command)
            self.record_daily_reward_event(identity, command, resp, source=command)
            log.critical(f"{prefix}{command}: rift weakness detected; identity paused until rebirth succeeds.")
            return True

        now = now_str()
        success_keywords = [
            "成功", "探寻", "裂缝", "收获", "空间", "发现",
            "时空异兽", "不敌败退", "身受重创", "元婴险些崩溃",
        ]
        if command == NODE_SEARCH_COMMAND:
            # .搜寻节点 成功响应：神识离体/虚空漫游/虚空尘埃等（日志实测格式）。
            success_keywords = success_keywords + [
                "神识离体", "虚空乱流", "虚空漫游", "虚空尘埃",
                "一无所获", "不虚此行", "进入了无尽",
            ]
        if not any(k in resp for k in success_keywords):
            state[next_key] = add_seconds_str(now, 600)
            self.save_state()
            notify_unrecognized_response(self, command, resp, log, "固定冷却指令")
            log.warning(f"{prefix}{command}: unrecognized response; retry scheduled at {state[next_key]}.")
            return False

        self.record_daily_reward_event(identity, command, resp, source=command)
        state[last_key] = now
        actual_cd = self.fixed_command_success_cooldown_seconds(
            command, resp, cd_seconds, identity=identity
        )
        state[next_key] = add_seconds_str(now, actual_cd)
        self.save_state()
        if actual_cd != cd_seconds:
            log.info(
                f"{prefix}{command}: recorded success/response with actual cooldown "
                f"{actual_cd}s, next at {state[next_key]}."
            )
        else:
            log.info(f"{prefix}{command}: recorded success/response, next at {state[next_key]}.")
        return True

    def is_rift_success_response(self, text):
        clean = str(text or "").replace("**", "")
        if not clean or self.is_rift_weakness_response(clean):
            return False
        success_markers = (
            "探寻裂缝成功", "探寻成功", "撕开一道", "探寻机缘",
            "发现秘藏", "发现一处空间裂缝", "收获", "获得【",
            "时空异兽", "不敌败退", "身受重创", "元婴险些崩溃",
        )
        return any(k in clean for k in success_markers)

    def rift_success_cooldown_seconds(self, text):
        """Parse equipment-adjusted .探寻裂缝 cooldown from a success response."""
        clean = str(text or "").replace("**", "")
        if not clean:
            return -1
        relevant_lines = []
        for line in re.split(r"[\n\r]+", clean):
            if not any(k in line for k in ("冷却", "下次", "再次", "后再", "剩余", "尚需", "请在", "缩短")):
                continue
            if any(k in line for k in ("裂缝", "探寻", "空间", "风雷翅", "冷却", "下次")):
                relevant_lines.append(line)
        if relevant_lines:
            return self.parse_wait_time("\n".join(relevant_lines))
        return -1

    def fixed_command_success_cooldown_seconds(self, command, resp, fallback_seconds, identity="主魂"):
        """Return the next cooldown after a successful fixed-cooldown command."""
        command = str(command or "").strip()
        fallback = int(fallback_seconds or 0)
        if command == ".探寻裂缝":
            actual = self.rift_success_cooldown_seconds(resp)
            if actual > 0:
                return actual
        if command == ".探寻裂缝" and wind_thunder_enabled(self, identity):
            return wind_thunder_target_cooldown(command, fallback)
        return fallback

    def is_rift_cooldown_response(self, text):
        clean = str(text or "").replace("**", "")
        if not clean or self.is_rift_weakness_response(clean):
            return False
        cooldown_markers = (
            "空间裂缝尚未稳定", "空间波动尚未平复", "空间风暴仍在",
            "后再行探寻", "后再来探寻裂缝", "再来探寻裂缝",
            "探寻裂缝尚在冷却", "冷却", "尚未稳定", "请在",
        )
        return any(k in clean for k in cooldown_markers) and any(k in clean for k in ("探寻", "裂缝", "空间", "冷却"))

    def rift_needs_actual_cooldown_probe(self, resp, command=".探寻裂缝", identity="主魂"):
        """Whether to send one follow-up .探寻裂缝 to read an equipment-adjusted cooldown."""
        if not self.should_probe_actual_cooldown(identity, command):
            return False
        if not resp or self.is_rift_weakness_response(resp):
            return False
        if self.rift_success_cooldown_seconds(resp) > 0:
            return False
        if self.is_rift_cooldown_response(resp):
            return False
        return self.is_rift_success_response(resp)

    def record_rift_cooldown_probe_response(self, identity, resp, plan):
        """Use a follow-up .探寻裂缝 cooldown reply to correct next_rift_search_time."""
        identity = str(identity or "主魂").strip() or "主魂"
        state = self.identity_state_for_timed_command(identity)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        if not resp:
            log.warning(f"{prefix}{plan.command}: actual cooldown probe got no response; keeping existing schedule.")
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and self.is_rift_cooldown_response(resp):
            state = self.identity_state_for_timed_command(identity)
            if state.get("wind_thunder_equipped"):
                cd = wind_thunder_target_cooldown(plan.command, cd)
            now = now_str()
            state[plan.next_key] = add_seconds_str(now, cd)
            state["last_rift_search_cooldown_probe_time"] = now
            state["last_rift_search_cooldown_probe_response"] = str(resp)[:200]
            self.save_state()
            log.info(f"{prefix}{plan.command}: actual cooldown probe {cd}s, next at {state[plan.next_key]}.")
            return True
        log.warning(f"{prefix}{plan.command}: actual cooldown probe did not contain a cooldown: {str(resp)[:120]}")
        return False

    def yuanying_out_cd_seconds(self):
        return int(getattr(self, "yuanying_out_cd", YUANYING_OUT_CD_SECONDS) or YUANYING_OUT_CD_SECONDS)

    def yuanying_is_retreat_command(self, identity="主魂"):
        return self.yuanying_command_for_identity(identity) == YUANYING_RETREAT_COMMAND

    def _yuanying_future_from_last_start(self, identity="主魂"):
        """Return inferred out-end time when the last confirmed start is still active."""
        if self.yuanying_is_retreat_command(identity):
            return ""
        state = self.identity_state_for_timed_command(identity)
        last = state.get("last_yuanying_out_time", "")
        if not last:
            return ""
        inferred = add_seconds_str(last, self.yuanying_out_cd_seconds())
        return inferred if inferred and is_future(inferred) else ""

    def _yuanying_existing_future_time(self, identity="主魂"):
        state = self.identity_state_for_timed_command(identity)
        candidates = [
            state.get("yuanying_out_end_time", ""),
            state.get("next_yuanying_out_time", ""),
            self._yuanying_future_from_last_start(identity),
        ]
        futures = [value for value in candidates if value and is_future(value)]
        if not futures:
            return ""
        return max(futures, key=lambda value: str_to_dt(value))

    def _recent_yuanying_start_future(self, window_seconds=180, identity="主魂"):
        state = self.identity_state_for_timed_command(identity)
        last = state.get("last_yuanying_out_time", "")
        if not last:
            return ""
        try:
            age = (datetime.now() - str_to_dt(last)).total_seconds()
        except Exception:
            return ""
        if 0 <= age <= window_seconds:
            return self._yuanying_future_from_last_start(identity)
        return ""

    def _repair_yuanying_out_from_last_start(self, reason="", identity="主魂"):
        if self.yuanying_is_retreat_command(identity):
            return ""
        state = self.identity_state_for_timed_command(identity)
        inferred = self._yuanying_future_from_last_start(identity)
        if not inferred:
            return ""
        changed = (
            state.get("next_yuanying_out_time") != inferred
            or state.get("yuanying_out_end_time") != inferred
            or not state.get("yuanying_out_active")
        )
        state["next_yuanying_out_time"] = inferred
        state["yuanying_out_end_time"] = inferred
        state["yuanying_out_active"] = True
        if changed:
            prefix = f"[{identity}] " if identity != "主魂" else ""
            self.common_command_logger().info(
                f"{prefix}{YUANYING_OUT_COMMAND}: repaired active state from last start ({reason}), "
                f"return due at {inferred}."
            )
        return inferred

    def record_yuanying_out_settlement_response(self, resp, source="passive", identity="主魂"):
        """Record yuanying return/retreat settlement and schedule the next start."""
        if not is_yuanying_out_settlement_response(resp):
            return False
        identity = str(identity or "主魂").strip() or "主魂"
        state = self.identity_state_for_timed_command(identity)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        command = self.yuanying_command_for_identity(identity)
        self.record_daily_reward_event(identity, command, resp, source=source)

        recent_start_due = self._recent_yuanying_start_future(identity=identity)
        if recent_start_due:
            state["next_yuanying_out_time"] = recent_start_due
            state["yuanying_out_end_time"] = recent_start_due
            state["yuanying_out_active"] = True
            log.info(
                f"{prefix}{command}: ignored stale settlement after fresh start ({source}); "
                f"return due at {recent_start_due}."
            )
            return True

        now = now_str()
        if identity == "主魂" and self.yuanying_is_retreat_command(identity):
            state["last_yuanying_return_time"] = now
            state["next_yuanying_out_time"] = add_seconds_str(now, 5)
            state["yuanying_out_active"] = False
            state["yuanying_out_end_time"] = ""
            log.info(f"{prefix}{command}: settlement detected ({source}); retry start at {state['next_yuanying_out_time']}.")
            return True

        if f"{command} response" in str(source) or ".元婴出窍 response" in str(source):
            next_time = add_seconds_str(now, self.yuanying_out_cd_seconds())
            state["last_yuanying_return_time"] = now
            state["last_yuanying_out_time"] = now
            state["next_yuanying_out_time"] = next_time
            state["yuanying_out_end_time"] = next_time
            state["yuanying_out_active"] = True
            log.info(f"{prefix}{command}: settlement response followed by start command; assuming active until {next_time}.")
            return True

        state["last_yuanying_return_time"] = now
        state["next_yuanying_out_time"] = add_seconds_str(now, 90)
        state["yuanying_out_active"] = False
        state["yuanying_out_end_time"] = ""
        log.info(f"{prefix}{command}: return settlement detected ({source}); retry at {state['next_yuanying_out_time']}.")
        return True

    def record_yuanying_out_active_response(self, resp, source="passive", identity="主魂"):
        """Sync yuanying active state from a direct or passive response."""
        if not resp:
            return False
        identity = str(identity or "主魂").strip() or "主魂"
        state = self.identity_state_for_timed_command(identity)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        command = self.yuanying_command_for_identity(identity)
        is_retreat = self.yuanying_is_retreat_command(identity)
        clean = str(resp).replace("**", "")
        if is_yuanying_out_settlement_response(clean):
            return False
        compact = clean.replace(" ", "")
        if any(k in compact for k in ["尚未凝聚元婴", "无法施展此术"]):
            return False
        active_markers = [
            "元婴出窍",
            "元婴闭关",
            "元神出窍",
            "神游",
            "云游",
            "消失在天际",
            "将在外云游",
            "正在执行“元神出窍”",
            "正在执行`元神出窍`",
            "状态: 元神出窍",
            "状态：元神出窍",
            "状态: 元婴闭关",
            "状态：元婴闭关",
            "开始闭关",
            "持续提供修为",
            "归来倒计时",
        ]
        if not any(k in clean for k in active_markers):
            return False

        now = now_str()
        cd = self.parse_wait_time(clean)
        already_active_unknown = any(k in clean for k in ["正在执行", "无法分身", "先使用 `.元婴归窍`", "先使用 .元婴归窍"])
        start_markers = ["心念一动", "消失在天际", "将在外云游", "自动结算收获", "开始闭关", "持续提供修为"]
        if not is_retreat:
            start_markers.append("元婴闭关")
        is_confirmed_start = any(k in clean for k in start_markers) and not already_active_unknown

        if is_confirmed_start:
            if cd > 0:
                next_time = add_seconds_str(now, cd)
            elif is_retreat:
                next_time = ""
            else:
                next_time = add_seconds_str(now, self.yuanying_out_cd_seconds())
            state["last_yuanying_out_time"] = now
        elif cd > 0:
            next_time = add_seconds_str(now, cd)
        else:
            next_time = self._yuanying_existing_future_time(identity)
            if not next_time:
                if is_retreat:
                    next_time = ""
                else:
                    retry_seconds = 3600 if already_active_unknown else self.yuanying_out_cd_seconds()
                    next_time = add_seconds_str(now, retry_seconds)

        state["next_yuanying_out_time"] = next_time
        state["yuanying_out_end_time"] = next_time
        state["yuanying_out_active"] = True
        if next_time:
            log.info(f"{prefix}{command}: active state synced ({source}), return due at {next_time}.")
        else:
            log.info(f"{prefix}{command}: active state synced ({source}); waiting for settlement reply.")
        return True

    def record_yuanying_out_start_response(self, resp, identity="主魂"):
        """Parse .元婴出窍/.元婴闭关 start response and update state."""
        identity = str(identity or "主魂").strip() or "主魂"
        command = self.yuanying_command_for_identity(identity)
        state = self.identity_state_for_timed_command(identity)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        if not resp:
            state["next_yuanying_out_time"] = add_seconds_str(now_str(), 3600)
            log.warning(f"{prefix}{command}: response missing; retry at {state['next_yuanying_out_time']}.")
            return False

        if self.record_yuanying_out_active_response(resp, source=f"{command} response", identity=identity):
            return True
        if self.record_yuanying_out_settlement_response(resp, source=f"{command} response", identity=identity):
            return False

        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            state["next_yuanying_out_time"] = add_seconds_str(now_str(), cd)
            state["yuanying_out_active"] = False
            state["yuanying_out_end_time"] = ""
            log.info(f"{prefix}{command}: cooldown from response {cd}s, next at {state['next_yuanying_out_time']}.")
            return False

        now = now_str()
        if any(k in resp for k in ["尚未凝聚元婴", "无法施展此术"]):
            state["next_yuanying_out_time"] = add_seconds_str(now, 6 * 3600)
            state["yuanying_out_active"] = False
            state["yuanying_out_end_time"] = ""
            log.info(f"{prefix}{command} unavailable: next check at {state['next_yuanying_out_time']}.")
            return False

        success_markers = ["元婴出窍", "元婴闭关", "神游", "云游", "出窍", "自动结算", "开始闭关", "持续提供修为"]
        if not any(k in resp for k in success_markers):
            state["next_yuanying_out_time"] = add_seconds_str(now, 3600)
            state["yuanying_out_active"] = False
            state["yuanying_out_end_time"] = ""
            notify_unrecognized_response(self, command, resp, log, "元婴")
            log.info(f"{prefix}{command}: unrecognized response; skipped until {state['next_yuanying_out_time']}.")
            return False

        cd = cd if cd > 0 else self.yuanying_out_cd_seconds()
        state["last_yuanying_out_time"] = now
        state["next_yuanying_out_time"] = add_seconds_str(now, cd)
        state["yuanying_out_end_time"] = state["next_yuanying_out_time"]
        state["yuanying_out_active"] = True
        log.info(f"{prefix}{command}: started, due at {state['yuanying_out_end_time']}.")
        return True

    def record_identity_yuanying_out_start_response(self, identity, resp):
        return self.record_yuanying_out_start_response(resp, identity=identity)

    def is_rift_weakness_response(self, text):
        if not text:
            return False
        clean = text.replace("**", "").replace(" ", "")
        return (
            "元婴遁逃·虚弱" in clean
            or ("肉体破碎" in clean and ("元婴虚弱" in clean or "虚弱" in clean))
            or ("元婴虚弱" in clean and ("肉体" in clean or "神魂" in clean or "虚弱" in clean))
            or ("虚弱期" in clean and "无法进行夺舍" in clean)
            or ("神魂遭受重创" in clean and "虚弱" in clean)
        )

    def treasure_touch_cd_seconds(self):
        return int(getattr(self, "treasure_touch_cd", TREASURE_TOUCH_CD_SECONDS) or TREASURE_TOUCH_CD_SECONDS)

    async def run_common_treasure_touch_loop(self, command=None, sleep_func=None, pause_label=""):
        """Run the shared main-soul treasure touch cooldown loop."""
        await self.startup_done.wait()
        plan = self.treasure_touch_plan(command)
        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            if (
                pause_label
                and hasattr(self, "sleep_if_main_soul_paused")
                and await self.sleep_if_main_soul_paused(pause_label)
            ):
                continue

            await self._wait_for_main_identity()
            next_time = self.state.get(plan.next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Treasure touch loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))
                continue

            log.info(f"Treasure touch due: sending {plan.command}.")
            resp = await self.send_timed_command_plan(plan, "主魂")
            self.record_treasure_touch_response(resp, plan.command)
            self.save_state()
            wait_time = seconds_until(self.state.get(plan.next_key, "")) or 600
            await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))

    async def run_common_node_search_loop(self, sleep_func=None):
        """Run the shared main-soul .搜寻节点 12h fixed-cooldown loop (Huashen void wandering)."""
        await self.startup_done.wait()
        plan = node_search_plan("主魂")
        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            if self.dashboard_command_paused(NODE_SEARCH_COMMAND, "主魂"):
                await self.wait_for_dashboard_command_control_change(300)
                continue
            if not getattr(self, "enable_node_search", True):
                await asyncio.sleep(600)
                continue

            await self._wait_for_main_identity()
            next_time = self.state.get(plan.next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Node search loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))
                continue

            log.info(f"Node search due: sending {plan.command}.")
            resp = await self.send_timed_command_plan(plan, "主魂")
            self.record_identity_fixed_cd_command_response(
                "主魂",
                self.timed_command_response_text(resp),
                plan.command,
                plan.last_key,
                plan.next_key,
                NODE_SEARCH_CD_SECONDS,
            )
            wait_time = seconds_until(self.state.get(plan.next_key, "")) or 600
            await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))

    def treasure_refine_progress(self, resp):
        """Parse '炼焰 `N/9`' progress from a Xutian Ding refine response. -1 = not found."""
        match = re.search(r"炼焰\s*`?(\d+)\s*/\s*9`?", str(resp or "").replace("**", ""))
        return int(match.group(1)) if match else -1

    async def run_common_treasure_refine_loop(self, sleep_func=None):
        """Run the main-soul .法宝 炼焰 虚天鼎 loop: 8h CD, stop at 9/9.

        响应解析（2026-09-07 日志实测格式）：
        - 成功: 【虚天鼎·炼焰】…炼焰 `N/9` | 通宝 `0/6`…本次消耗：修为 `180000` / 神识 `240`
        - 冷却: 含「冷却/请在/后再」+ 可解析等待时间
        - 资源门槛: 修为/神识不足 → 1 小时退避
        - 圆满: N/9 == 9 → 置 treasure_refine_complete=True，循环自动停止发送
        """
        await self.startup_done.wait()
        plan = treasure_refine_plan()
        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            if self.dashboard_command_paused(TREASURE_REFINE_COMMAND, "主魂"):
                await self.wait_for_dashboard_command_control_change(300)
                continue
            if not getattr(self, "enable_treasure_refine", False):
                await asyncio.sleep(600)
                continue

            await self._wait_for_main_identity()
            if self.state.get("treasure_refine_complete"):
                log.info("Treasure refine complete (9/9); loop sleeping permanently.")
                await asyncio.sleep(3600)
                continue

            next_time = self.state.get(plan.next_key, "")
            if next_time and is_future(next_time):
                wait_time = seconds_until(next_time)
                log.info(f"Treasure refine loop complete. Sleep {int(min(wait_time, 600))}s.")
                await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))
                continue

            log.info(f"Treasure refine due: sending {plan.command}.")
            resp = await self.send_timed_command_plan(plan, "主魂")
            resp_text = self.timed_command_response_text(resp)
            self.record_treasure_refine_response(resp_text, plan)
            wait_time = seconds_until(self.state.get(plan.next_key, "")) or 600
            await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))

    def record_treasure_refine_response(self, resp, plan):
        """Persist refine progress and schedule the next attempt."""
        log = self.common_command_logger()
        now = now_str()
        if not resp:
            self.state[plan.next_key] = add_seconds_str(now, 600)
            self.save_state()
            log.warning(f"{plan.command}: response missing; retry scheduled at {self.state[plan.next_key]}.")
            return
        clean = str(resp).replace("**", "")
        progress = self.treasure_refine_progress(clean)
        if progress >= 9:
            self.state["treasure_refine_complete"] = True
            self.state["treasure_refine_progress"] = progress
            self.state[plan.last_key] = now
            self.state[plan.next_key] = ""
            self.save_state()
            log.info(f"{plan.command}: 炼焰 {progress}/9 圆满，停止发送。")
            return
        if progress >= 0:
            self.state["treasure_refine_progress"] = progress
            self.state[plan.last_key] = now
            self.state[plan.next_key] = add_seconds_str(now, TREASURE_REFINE_CD_SECONDS)
            self.save_state()
            log.info(f"{plan.command}: 炼焰 {progress}/9 记录成功，下次 {self.state[plan.next_key]}。")
            return
        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            self.state[plan.next_key] = add_seconds_str(now, cd)
            self.save_state()
            log.info(f"{plan.command}: cooldown from response {cd}s, next at {self.state[plan.next_key]}.")
            return
        if any(k in clean for k in ["不足", "无法", "缺少"]):
            self.state[plan.next_key] = add_seconds_str(now, 3600)
            self.save_state()
            log.warning(f"{plan.command}: 资源不足（修为/神识），1 小时后重试。")
            return
        self.state[plan.next_key] = add_seconds_str(now, 600)
        self.save_state()
        notify_unrecognized_response(self, plan.command, resp, log, "法宝炼焰")
        log.warning(f"{plan.command}: unrecognized response; retry scheduled at {self.state[plan.next_key]}.")

    def main_level_has_yuanying(self, level):
        return any(k in str(level or "") for k in ["元婴", "化神", "合体", "大乘", "渡劫", "仙"])

    async def ensure_main_yuanying_level_for_command(self, label="Yuanying command"):
        """Fetch cached main level when needed and verify the command is available."""
        log = self.common_command_logger()
        main_level = self.state.get("level", "")
        if not main_level:
            log.info("Main level not cached, sending .状态 to fetch it...")
            await self.send_and_wait_feedback(".状态", timeout=30)
            main_level = self.state.get("level", "")
        if self.main_level_has_yuanying(main_level):
            return True
        log.info(f"Main soul level [{main_level}] has no Yuanying. {label} suspended for 1 hour.")
        return False

    async def common_main_yuanying_out_tick(self, require_yuanying_level=False):
        """Run one main-soul .元婴出窍/.元婴闭关 scheduling step and return next wait seconds."""
        plan = self.yuanying_out_plan("主魂")
        command = plan.command
        log = self.common_command_logger()

        await self._wait_for_main_identity()
        end_time = self.state.get("yuanying_out_end_time") or self.state.get("next_yuanying_out_time", "")
        active = self.state.get("yuanying_out_active")

        if active and end_time and is_future(end_time):
            wait_time = seconds_until(end_time)
            log.debug(f"Yuanying out active. Auto-return due at {end_time}.")
            return wait_time

        if active and not end_time and command == YUANYING_RETREAT_COMMAND:
            log.debug("Yuanying retreat active; waiting for passive settlement reply.")
            return 600

        if active:
            repaired = self._repair_yuanying_out_from_last_start("active expiry guard")
            if repaired:
                self.save_state()
                return seconds_until(repaired)
            log.info("Yuanying out time expired. Auto-resetting state.")
            self.state["yuanying_out_active"] = False
            self.state["yuanying_out_end_time"] = ""
            self.save_state()
            return 5

        if require_yuanying_level and not await self.ensure_main_yuanying_level_for_command("Yuanying out loop"):
            return 3600

        next_time = self.state.get("next_yuanying_out_time", "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)

        repaired = self._repair_yuanying_out_from_last_start("pre-send guard")
        if repaired:
            self.save_state()
            return seconds_until(repaired)

        log.info(f"Yuanying ability due: sending {command}.")
        resp = await self.send_timed_command_plan(plan, "主魂")
        resp_text = self.timed_command_response_text(resp)
        self.record_yuanying_out_start_response(resp_text)
        if command == YUANYING_RETREAT_COMMAND and is_yuanying_out_settlement_response(resp_text):
            self.save_state()
            log.info(f"{command}: settlement consumed trigger message; sending again to start next cycle.")
            await asyncio.sleep(5)
            resp = await self.send_timed_command_plan(plan, "主魂")
            self.record_yuanying_out_start_response(self.timed_command_response_text(resp))
        self.save_state()
        return 5

    def command_response_reply_id(self, resp_msg):
        if hasattr(resp_msg, "reply_to") and getattr(resp_msg, "reply_to", None):
            replied_id = getattr(resp_msg.reply_to, "reply_to_msg_id", None)
            if replied_id:
                return replied_id
        return getattr(resp_msg, "reply_to_msg_id", None)

    async def common_main_rift_search_tick(self, cd_seconds, require_yuanying_level=False):
        """Run one main-soul .探寻裂缝 scheduling step and return next wait seconds."""
        plan = self.rift_search_plan("主魂")
        command = plan.command
        log = self.common_command_logger()

        await self._wait_for_main_identity()
        if require_yuanying_level and not await self.ensure_main_yuanying_level_for_command("Rift search loop"):
            return 3600

        next_time = self.state.get(plan.next_key, "")
        if next_time and is_future(next_time):
            wait_time = seconds_until(next_time)
            log.info(f"Rift search loop complete. Sleep {int(min(wait_time, 600))}s.")
            return wait_time

        log.info(f"Rift search due: sending {command}.")
        resp_msg = await self.send_rift_search_plan(plan, "主魂")
        if resp_msg is None:
            if hasattr(self, "sleep_after_blocked_command") and await self.sleep_after_blocked_command(command, "Rift search"):
                return 0
            log.info("Rift search: no response received, retrying later.")
            return 600

        resp_text = self.timed_command_response_text(resp_msg)
        if self.is_rift_weakness_response(resp_text):
            replied_id = self.command_response_reply_id(resp_msg)
            if replied_id and replied_id != getattr(self, "last_sent_id", None):
                log.warning(
                    f"Rift weakness detected but reply_to #{replied_id} != our sent msg, "
                    "likely someone else's. Skipping."
                )
                return 0
            await self.stop_for_rift_weakness(resp_text, identity="主魂", msg=resp_msg)
            return -1

        recorded = self.record_fixed_cd_command_response(resp_text, command, plan.last_key, plan.next_key, cd_seconds)
        if recorded and self.rift_needs_actual_cooldown_probe(resp_text, command, identity="主魂"):
            await self.probe_rift_actual_cooldown(plan, identity="主魂")
        self.save_state()
        return seconds_until(self.state.get(plan.next_key, "")) or 600

    async def probe_rift_actual_cooldown(self, plan, identity="主魂"):
        """Send one follow-up .探寻裂缝 to read equipment-adjusted cooldown after a success."""
        identity = str(identity or "主魂").strip() or "主魂"
        delay = float(getattr(self, "actual_cooldown_probe_delay_seconds", 3) or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        log.info(f"{prefix}{plan.command}: probing actual cooldown via follow-up command.")
        kwargs = {
            "timeout": min(int(plan.timeout or 45), 45),
            "max_retries": 0,
            "force_identity_check": True,
            "suppress_no_response_alert": True,
        }
        async def send_once():
            if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
                return await self.send_and_wait_feedback_identity(identity, plan.command, **kwargs)
            return await self.send_and_wait_feedback(plan.command, **kwargs)
        resp = await wind_thunder_send(self, identity, plan.command, send_once)
        return self.record_rift_cooldown_probe_response(
            identity,
            self.timed_command_response_text(resp),
            plan,
        )

    def ask_dao_cd_seconds(self):
        return int(getattr(self, "ask_dao_cd", ASK_DAO_CD_SECONDS) or ASK_DAO_CD_SECONDS)

    def ask_dao_retry_seconds(self):
        return int(getattr(self, "ask_dao_retry", ASK_DAO_RETRY_SECONDS) or ASK_DAO_RETRY_SECONDS)

    def should_probe_actual_cooldown(self, identity, command):
        """Whether an identity should query the game again for equipment-adjusted cooldowns."""
        identity = str(identity or "主魂").strip() or "主魂"
        command = str(command or "").strip()
        command_root = command.split()[0] if command else ""
        probes = getattr(self, "actual_cooldown_probe_commands", None)
        if not probes:
            return False
        if isinstance(probes, dict):
            values = probes.get(identity) or probes.get("*") or []
            return command in values or command_root in values
        for item in probes:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                item_identity = str(item[0] or "").strip() or "主魂"
                item_command = str(item[1] or "").strip()
                item_root = item_command.split()[0] if item_command else ""
                if item_identity in {identity, "*"} and item_command in {command, command_root}:
                    return True
                if item_identity in {identity, "*"} and item_root and item_root == command_root:
                    return True
            else:
                item_command = str(item or "").strip()
                item_root = item_command.split()[0] if item_command else ""
                if item_command in {command, command_root} or (item_root and item_root == command_root):
                    return True
        return False

    def ask_dao_success_cooldown_seconds(self, text):
        """Parse an explicit next .问道 cooldown from a success response if present."""
        clean = str(text or "").replace("**", "")
        if not clean:
            return -1
        relevant_lines = []
        for line in re.split(r"[\n\r]+", clean):
            if any(k in line for k in ("冷却", "下次", "再次", "后再", "剩余", "尚需", "请在")):
                relevant_lines.append(line)
        if relevant_lines:
            return self.parse_wait_time("\n".join(relevant_lines))
        return -1

    def is_ask_dao_cooldown_response(self, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        success_markers = ("问道得宝", "获得大道感悟", "获得感悟", "道韵萦绕", "参悟成功")
        if any(k in clean for k in success_markers):
            return False
        return any(k in clean for k in ("天机不可频繁", "问道尚在冷却", "后再来问道", "后再试", "请在", "尚需", "剩余"))

    def is_ask_dao_response(self, text):
        """Return True when text looks like a .问道 bot response."""
        clean = (text or "").replace("**", "")
        if not clean:
            return False
        direct_keywords = ["问道", "元婴宗", "悟道", "论道", "道韵", "大道", "参悟"]
        if any(k in clean for k in direct_keywords):
            return True
        if any(k in clean for k in ["冷却", "后再", "尚需", "剩余", "不足", "无法", "尚未", "未加入"]):
            return True
        return "获得" in clean and any(k in clean for k in ["感悟", "道心", "贡献"])

    def record_ask_dao_response(self, resp, source=None, identity="主魂"):
        """Record .问道 response; success may be followed by an actual-cooldown probe."""
        identity = self.resolve_avatar_identity(identity)
        source = source or ASK_DAO_COMMAND
        now = now_str()
        plan = self.ask_dao_plan(source)
        log = self.common_command_logger()
        state = self.identity_state_for_timed_command(identity)
        next_key = plan.next_key
        last_key = plan.last_key
        if not resp:
            state[next_key] = add_seconds_str(now, self.ask_dao_retry_seconds())
            log.info(f"{source}: no response; retry at {state[next_key]}.")
            return False

        cd = self.parse_wait_time(resp)
        if self.is_ask_dao_cooldown_response(resp):
            delay = cd if cd > 0 else self.ask_dao_retry_seconds()
            state[next_key] = add_seconds_str(now, delay)
            log.info(f"{source}: cooldown from response {delay}s, next at {state[next_key]}.")
            return True

        if any(k in resp for k in ["未加入", "不是元婴宗", "无法问道", "条件不足", "境界不足", "修为不足"]):
            state[next_key] = add_seconds_str(now, 60 * 60)
            state["last_ask_dao_error"] = resp[:200]
            state["last_ask_dao_error_time"] = now
            log.info(f"{source}: unavailable; retry at {state[next_key]}.")
            return True

        if self.is_ask_dao_response(resp):
            actual_cd = self.ask_dao_success_cooldown_seconds(resp)
            self.record_daily_reward_event(identity, plan.command, resp, source=source, final=True)
            state[last_key] = now
            cooldown = actual_cd if actual_cd > 0 else self.ask_dao_cd_seconds()
            if wind_thunder_enabled(self, identity):
                cooldown = wind_thunder_target_cooldown(plan.command, cooldown)
            state[next_key] = add_seconds_str(
                now,
                cooldown,
            )
            state["last_ask_dao_error"] = ""
            if actual_cd > 0:
                log.info(f"{source}: recorded response with actual cooldown {actual_cd}s, next at {state[next_key]}.")
            else:
                log.info(f"{source}: recorded response, next at {state[next_key]}.")
            return True

        state[next_key] = add_seconds_str(now, self.ask_dao_retry_seconds())
        notify_unrecognized_response(self, ASK_DAO_COMMAND, resp, log, source)
        log.info(f"{source}: unrecognized response; retry at {state[next_key]}.")
        return False

    def ask_dao_needs_actual_cooldown_probe(self, resp, command=None):
        if not self.should_probe_actual_cooldown("主魂", command or ASK_DAO_COMMAND):
            return False
        if not resp or self.is_ask_dao_cooldown_response(resp):
            return False
        if any(k in resp for k in ["未加入", "不是元婴宗", "无法问道", "条件不足", "境界不足", "修为不足"]):
            return False
        return self.is_ask_dao_response(resp) and self.ask_dao_success_cooldown_seconds(resp) <= 0

    def record_ask_dao_cooldown_probe_response(self, resp, source=None):
        """Use a follow-up .问道 cooldown reply to correct next_ask_dao_time without recording rewards."""
        source = source or ASK_DAO_COMMAND
        plan = self.ask_dao_plan(source)
        log = self.common_command_logger()
        if not resp:
            log.warning(f"{source}: actual cooldown probe got no response; keeping existing schedule.")
            return False
        cd = self.parse_wait_time(resp)
        if cd > 0 and self.is_ask_dao_cooldown_response(resp):
            if wind_thunder_enabled(self, "主魂"):
                cd = wind_thunder_target_cooldown(source, cd)
            now = now_str()
            self.state[plan.next_key] = add_seconds_str(now, cd)
            self.state["last_ask_dao_cooldown_probe_time"] = now
            self.state["last_ask_dao_cooldown_probe_response"] = str(resp)[:200]
            log.info(f"{source}: actual cooldown probe {cd}s, next at {self.state[plan.next_key]}.")
            return True
        log.warning(f"{source}: actual cooldown probe did not contain a cooldown: {str(resp)[:120]}")
        return False

    async def probe_ask_dao_actual_cooldown(self, plan):
        """Send one follow-up .问道 to read equipment-adjusted cooldown after a success."""
        delay = float(getattr(self, "actual_cooldown_probe_delay_seconds", 3) or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        log = self.common_command_logger()
        log.info(f"{plan.command}: probing actual cooldown via follow-up command.")
        async def send_once():
            return await self.send_and_wait_feedback(
                plan.command,
                timeout=min(int(plan.timeout or 45), 45),
                max_retries=0,
                force_identity_check=True,
                suppress_no_response_alert=True,
            )
        resp = await wind_thunder_send(self, "主魂", plan.command, send_once)
        return self.record_ask_dao_cooldown_probe_response(
            self.timed_command_response_text(resp),
            plan.command,
        )

    async def probe_deep_meditation_actual_cooldown(self, identity="主魂", source=".深度闭关"):
        """Query .查看闭关 once and return the real remaining deep-meditation seconds."""
        identity = str(identity or "主魂").strip() or "主魂"
        delay = float(getattr(self, "actual_cooldown_probe_delay_seconds", 3) or 0)
        if delay > 0:
            await asyncio.sleep(delay)
        log = self.common_command_logger()
        log.info(f"[{identity}] {source}: probing actual deep meditation cooldown via .查看闭关.")
        if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
            resp = await self.send_and_wait_feedback_identity(
                identity,
                ".查看闭关",
                timeout=30,
                max_retries=0,
                force_identity_check=True,
                force_meditation_check=True,
                suppress_no_response_alert=True,
            )
        else:
            resp = await self.send_and_wait_feedback(
                ".查看闭关",
                timeout=30,
                max_retries=0,
                force_identity_check=True,
                force_meditation_check=True,
                suppress_no_response_alert=True,
            )
        text = self.timed_command_response_text(resp)
        cd = self.parse_wait_time(text)
        if cd > 0 and (is_deep_meditation_ongoing_response(text) or "预计还需" in text):
            log.info(f"[{identity}] {source}: actual deep meditation cooldown {cd}s.")
            return cd
        log.warning(f"[{identity}] {source}: .查看闭关 did not return a usable remaining time: {text[:120]}")
        return -1

    async def common_ask_dao_tick(self, command=None, identity="主魂"):
        """Run one identity's .问道 scheduling step and return next wait seconds."""
        identity = self.resolve_avatar_identity(identity)
        plan = self.ask_dao_plan(command)
        if identity == "主魂":
            await self._wait_for_main_identity()
        if self.dashboard_command_paused(plan.command, identity):
            return 300

        state = self.identity_state_for_timed_command(identity)
        next_time = state.get(plan.next_key, "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)

        log = self.common_command_logger()
        log.info(f"Ask Dao due: sending {plan.command}.")
        resp = await self.send_timed_command_plan(plan, identity)
        if resp is None and await self.sleep_after_blocked_command(plan.command, "Ask Dao"):
            return 0
        resp_text = self.timed_command_response_text(resp)
        self.record_ask_dao_response(resp_text, plan.command, identity=identity)
        if identity == "主魂" and self.ask_dao_needs_actual_cooldown_probe(resp_text, plan.command):
            await self.probe_ask_dao_actual_cooldown(plan)
        self.save_state()
        return 5

    async def run_common_ask_dao_loop(self, command=None, sleep_func=None, initial_jitter=True):
        """Run the shared main-soul .问道 loop."""
        await self.startup_done.wait()
        if initial_jitter:
            await asyncio.sleep(random.randint(20, 80))
        while getattr(self, "is_running", True):
            wait_time = await self.common_ask_dao_tick(command)
            await asyncio.sleep(self.common_scheduler_sleep_seconds(wait_time, sleep_func=sleep_func))

    def record_treasure_touch_response(self, resp, command=None, identity="主魂"):
        """Parse .抚摸法宝 response and update the identity's cooldown state."""
        plan = self.treasure_touch_plan(command)
        command = plan.command
        next_key = plan.next_key
        last_key = plan.last_key
        identity = str(identity or "主魂").strip() or "主魂"
        state = self.identity_state_for_timed_command(identity)
        log = self.common_command_logger()
        prefix = f"[{identity}] " if identity != "主魂" else ""
        if not resp:
            state[next_key] = add_seconds_str(now_str(), 600)
            log.warning(f"{prefix}{command}: response missing; retry scheduled at {state[next_key]}.")
            return False

        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["休息", "冷却", "后再", "尚需", "还需", "互动"]):
            state[next_key] = add_seconds_str(now_str(), cd)
            log.info(f"{prefix}{command}: cooldown from response {cd}s, next at {state[next_key]}.")
            return False

        if any(k in resp for k in ["联系更加紧密", "器灵传来了喜悦", "默契", "经验", "与它互动", "微微颤动"]):
            now = now_str()
            state[last_key] = now
            state[next_key] = add_seconds_str(now, self.treasure_touch_cd_seconds())
            log.info(f"{prefix}{command}: recorded success, next at {state[next_key]}.")
            return True

        if (
            "没有这件拥有器灵的法宝" in resp
            or "名字输入错误" in resp
            or ("没有这件" in resp and "器灵" in resp)
        ):
            now = now_str()
            state["last_treasure_touch_error"] = resp[:200]
            state["last_treasure_touch_error_time"] = now
            state[next_key] = add_seconds_str(now, self.treasure_touch_cd_seconds())
            if identity == "主魂" and hasattr(self, "_main_confirmed"):
                self._main_confirmed = False
            log.warning(
                f"{prefix}{command}: definite failure ({resp[:80]}), next at "
                f"{state[next_key]}."
            )
            return False

        state[next_key] = add_seconds_str(now_str(), 600)
        notify_unrecognized_response(self, command, resp, log, "抚摸法宝")
        log.warning(f"{prefix}{command}: unrecognized response; skipped and retry scheduled at {state[next_key]}.")
        return False

    def is_field_training_response(self, text):
        """判断游戏回复是否为野外历练相关的消息"""
        clean = (text or "").replace("**", "")
        return "野外历练" in clean or "山中灵机未复" in clean

    def is_field_training_pending_response(self, text):
        """野外历练初始回复，结果稍后会编辑到同一条消息。"""
        clean = (text or "").replace("**", "")
        return (
            "【野外历练】" in clean
            and "选择【" in clean
            and "正向荒野深处行去" in clean
        )

    def is_field_training_settlement_response(self, text):
        """野外历练已结算的回复。"""
        clean = (text or "").replace("**", "")
        if not clean or "野外历练" not in clean:
            return False
        return any(k in clean for k in (
            "野外历练 ·",
            "灵机暗藏",
            "妖兽遭遇",
            "负伤而归",
            "满载而归",
            "获得修为",
            "修为折损",
            "此战只结算",
            "此为玩家对 NPC",
        ))

    def is_field_training_command_result(self, text):
        """判断已归属到野外历练指令的回复是否代表本轮有结果。"""
        clean = (text or "").replace("**", "")
        return (
            self.is_field_training_settlement_response(text)
            or self.is_field_training_cooldown_response(text)
            or ("卦象" in clean and "修为增加" in clean)
        )

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

        if self.is_field_training_pending_response(text):
            log.info("Field training pending response observed; waiting for edited settlement.")
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
            self.record_daily_reward_event("主魂", FIELD_TRAINING_COMMAND, text, source=context)
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

        if self.is_field_training_pending_response(text):
            log.info(f"Avatar [{identity}] field training pending response observed; waiting for edited settlement.")
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
            self.record_daily_reward_event(identity, FIELD_TRAINING_COMMAND, text, source=context)
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

    def field_training_response_text(self, resp):
        if hasattr(self, "response_text"):
            return self.response_text(resp)
        if hasattr(resp, "text"):
            return resp.text or ""
        if isinstance(resp, str):
            return resp
        return str(resp) if resp else ""

    async def common_avatar_field_training_tick(self, avatar, handle_insufficient_cultivation=False):
        """Run one avatar field-training scheduling step and return next wait seconds."""
        log = self.common_command_logger()
        a_state = self.get_avatar_state(avatar)
        repaired_next = self.preserve_cooldown_floor(
            a_state,
            "last_field_training_time",
            "next_field_training_time",
            FIELD_TRAINING_CD_SECONDS,
            f"avatar field training [{avatar}]",
        )
        if repaired_next and is_future(repaired_next):
            return seconds_until(repaired_next)

        next_time = a_state.get("next_field_training_time", "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)

        plan = self.field_training_plan(avatar)
        async with self.common_atomic_task(f"FieldTraining-{avatar}"):
            for step in plan.pre_steps:
                await self.send_and_wait_feedback_identity(avatar, step.command)
                if step.delay_after:
                    await asyncio.sleep(step.delay_after)

            log.info(f"Avatar [{avatar}] field training due: sending {plan.command}")
            resp = await self.send_and_wait_feedback_identity(
                avatar,
                plan.command,
                timeout=plan.timeout,
                max_retries=plan.max_retries,
                force_identity_check=plan.force_identity_check,
                suppress_no_response_alert=plan.suppress_no_response_alert,
                return_response_msg=plan.return_response_msg,
            )
            resp = await self.wait_for_field_training_settlement(resp, avatar)
            resp_text = self.response_text(resp) if hasattr(self, "response_text") else self.field_training_response_text(resp)

            if (
                handle_insufficient_cultivation
                and "修为不足" in resp_text
                and hasattr(self, "handle_修为不足")
            ):
                async def retry_field_training():
                    retry_resp = await self.send_and_wait_feedback_identity(
                        avatar,
                        plan.command,
                        timeout=plan.timeout,
                        max_retries=plan.max_retries,
                        force_identity_check=plan.force_identity_check,
                        suppress_no_response_alert=plan.suppress_no_response_alert,
                        return_response_msg=plan.return_response_msg,
                    )
                    return await self.wait_for_field_training_settlement(retry_resp, avatar)

                success, retry_text = await self.handle_修为不足(
                    avatar,
                    retry_field_training,
                    cooldown_key="next_field_training_time",
                    cooldown_hours=2,
                )
                resp_text = self.response_text(retry_text) if hasattr(self, "response_text") else self.field_training_response_text(retry_text)
                if not success:
                    self.set_avatar_state(avatar, "next_field_training_time", add_seconds_str(now_str(), FIELD_TRAINING_CD_SECONDS))
                    return 60

            self.record_identity_field_training_response(avatar, resp_text, "野外历练")
        return 5

    async def run_common_avatar_field_training_loop(
        self,
        avatar,
        initial_delay=0,
        sleep_func=None,
        handle_insufficient_cultivation=False,
    ):
        """Run avatar field training on its independent cooldown."""
        await self.startup_done.wait()
        if hasattr(self, "_avatar_loop_count"):
            self._avatar_loop_count += 1
        if initial_delay > 0:
            self.common_command_logger().info(
                f"Avatar [{avatar}] field training loop: waiting {initial_delay}s before start..."
            )
            await asyncio.sleep(initial_delay)

        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                wait_time = await self.common_avatar_field_training_tick(
                    avatar,
                    handle_insufficient_cultivation=handle_insufficient_cultivation,
                )
                await asyncio.sleep(
                    self.common_scheduler_sleep_seconds(wait_time, minimum=60, sleep_func=sleep_func)
                )
            except Exception as exc:
                log.error(f"Avatar [{avatar}] field training loop error: {exc}", exc_info=True)
                await asyncio.sleep(300)

    # ---- 化身闯塔 ----

    def avatar_tower_available(self, avatar, require_meditation_ready=False):
        """Shared precheck for avatar .闯塔 sends."""
        avatar = str(avatar or "").strip()
        log = self.common_command_logger()
        if not avatar or avatar not in getattr(self, "avatars", []):
            return False
        if self.identity_pause_seconds(avatar) > 0:
            return False
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(".闯塔", avatar):
            return False
        if (
            require_meditation_ready
            and hasattr(self, "avatar_meditation_needs_attention")
            and self.avatar_meditation_needs_attention(avatar)
        ):
            log.info(f"Avatar [{avatar}] tower skipped: meditation needs restart first.")
            return False
        return True

    def record_mulan_support_response(self, identity, text, today=None, command=""):
        """Record one identity's configured .支援慕兰 response and retry hint."""
        identity = str(identity or "主魂").strip() or "主魂"
        command = str(command or self.mulan_support_command()).strip()
        today = today or datetime.now().strftime("%Y-%m-%d")
        now = now_str()
        clean = str(text or "").strip()
        updates = {
            "last_mulan_support_time": now,
            "last_mulan_support_response": clean[:500],
        }
        log = self.common_command_logger()

        def persist(values):
            if identity == "主魂":
                self.state.update(values)
                self.save_state()
            else:
                self.update_avatar_states(identity, values)

        if not clean:
            updates.update({
                "next_mulan_support_time": add_seconds_str(now, MULAN_SUPPORT_RETRY_SECONDS),
                "last_mulan_support_error": "no response",
            })
            persist(updates)
            log.warning(
                f"[{identity}] Mulan support: no usable response; "
                f"recorded retry hint at {updates['next_mulan_support_time']}."
            )
            return False

        cd = self.parse_wait_time(clean) if hasattr(self, "parse_wait_time") else -1
        cooldown_words = ["冷却", "后再", "尚需", "剩余", "请在", "还需", "稍后"]
        if any(k in clean for k in cooldown_words):
            retry_seconds = cd if cd and cd > 0 else MULAN_SUPPORT_RETRY_SECONDS
            updates.update({
                "next_mulan_support_time": add_seconds_str(now, retry_seconds),
                "last_mulan_support_error": clean[:200],
            })
            persist(updates)
            log.info(
                f"[{identity}] Mulan support cooling/unavailable; "
                f"next hint at {updates['next_mulan_support_time']}."
            )
            return False

        unavailable_words = ["无法", "不能", "条件不足", "修为不足", "未加入", "不在"]
        if any(k in clean for k in unavailable_words):
            updates.update({
                "next_mulan_support_time": add_seconds_str(now, 60 * 60),
                "last_mulan_support_error": clean[:200],
            })
            persist(updates)
            log.info(
                f"[{identity}] Mulan support unavailable; "
                f"next hint at {updates['next_mulan_support_time']}."
            )
            return False

        updates.update({
            "last_mulan_support_date": today,
            "next_mulan_support_time": "",
            "last_mulan_support_error": "",
        })
        persist(updates)
        if (
            hasattr(self, "record_daily_reward_event")
            and self.daily_reward_is_final_settlement_text(command, clean)
        ):
            self.record_daily_reward_event(
                identity,
                command,
                clean,
                source=command,
                final=True,
            )
        log.info(f"[{identity}] Mulan support recorded for {today}.")
        return True

    async def maybe_run_mulan_support(self, identity="主魂", today=None, timeout=60):
        """Send the configured independent daily .支援慕兰 command once per identity."""
        identity = str(identity or "主魂").strip() or "主魂"
        command = self.mulan_support_command()
        if seconds_until_mulan_support_start(datetime.now()) > 0:
            return False
        today = today or datetime.now().strftime("%Y-%m-%d")
        state = self.state if identity == "主魂" else self.get_avatar_state(identity)
        log = self.common_command_logger()
        if state.get("last_mulan_support_date") == today:
            return False
        retry_at = str(state.get("next_mulan_support_time") or "")
        if retry_at and is_future(retry_at):
            return False
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(
            command,
            identity,
        ):
            log.info(f"[{identity}] Mulan support skipped: dashboard command paused.")
            return False
        if identity == "主魂":
            resp = await self.send_and_wait_feedback(
                command,
                timeout=timeout,
                max_retries=0,
                force_identity_check=True,
                suppress_no_response_alert=True,
            )
        else:
            resp = await self.send_and_wait_feedback_identity(
                identity,
                command,
                timeout=timeout,
                max_retries=0,
                force_identity_check=True,
                suppress_no_response_alert=True,
            )
        return self.record_mulan_support_response(
            identity,
            self.timed_command_response_text(resp),
            today=today,
            command=command,
        )

    async def common_avatar_tower_send(
        self,
        avatar,
        today=None,
        timeout=90,
        handle_insufficient_cultivation=False,
        require_meditation_ready=False,
    ):
        """Send one avatar .闯塔 attempt and record today's completion on usable response."""
        log = self.common_command_logger()
        if not self.avatar_tower_available(
            avatar,
            require_meditation_ready=require_meditation_ready,
        ):
            return False

        today = today or datetime.now().strftime("%Y-%m-%d")
        a_state = self.get_avatar_state(avatar)
        if a_state.get("last_tower_date") == today:
            return False

        # Legacy tower helper retained for state compatibility; retired-command
        # guards prevent this path from sending in production.
        resp = await self.send_and_wait_feedback_identity(
            avatar,
            ".闯塔",
            timeout=timeout,
            return_response_msg=True,
            delete_after=False,
        )
        if resp is None:
            log.warning(f"Avatar [{avatar}] tower: switch/send failed. Retrying later.")
            return False
        resp = await self.wait_for_avatar_tower_settlement(
            resp,
            avatar=avatar,
            timeout_seconds=AVATAR_TOWER_SETTLEMENT_TIMEOUT_SECONDS,
            minimum_wait_seconds=AVATAR_TOWER_SETTLEMENT_WAIT_SECONDS,
        )
        resp_text = self.timed_command_response_text(resp)
        if not resp_text:
            log.info(f"Avatar [{avatar}] tower skipped: no usable response.")
            return False

        if (
            handle_insufficient_cultivation
            and "修为不足" in resp_text
            and hasattr(self, "handle_修为不足")
        ):
            async def retry_tower():
                return await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=timeout)

            success, resp_text = await self.handle_修为不足(
                avatar,
                retry_tower,
                cooldown_key="last_tower_date",
                cooldown_hours=2,
            )
            resp_text = self.timed_command_response_text(resp_text)
            if not success:
                log.warning(f"Avatar [{avatar}] tower: 修为不足 after force exit, will retry later.")
                return False

        self.record_daily_reward_event(
            avatar,
            ".闯塔",
            resp_text,
            source=".闯塔",
            msg=resp,
            final=True,
        )
        self.set_avatar_state(avatar, "last_tower_date", today)
        log.info(f"Avatar [{avatar}] tower completed for {today}.")
        return True

    async def wait_for_avatar_tower_settlement(
        self,
        resp,
        avatar="主魂",
        timeout_seconds=AVATAR_TOWER_SETTLEMENT_TIMEOUT_SECONDS,
        minimum_wait_seconds=AVATAR_TOWER_SETTLEMENT_WAIT_SECONDS,
        poll_seconds=2,
    ):
        """Wait for the bot to edit the initial .闯塔 response into its final battle report."""
        msg_id = getattr(resp, "id", None)
        client = getattr(self, "client", None)
        chat_id = getattr(self, "target_chat_id", None)
        log = self.common_command_logger()
        if not msg_id or not client or chat_id is None:
            log.warning(f"Avatar [{avatar}] tower response has no fetchable message id; waiting {minimum_wait_seconds}s before support.")
            await asyncio.sleep(max(0, minimum_wait_seconds))
            return resp

        initial_text = getattr(resp, "text", "") or ""
        started = time.monotonic()
        deadline = started + max(float(timeout_seconds), float(minimum_wait_seconds))
        minimum_deadline = started + max(0, float(minimum_wait_seconds))
        latest = resp

        # 给机器人至少约 30 秒完成闯塔；期间不发送任何后续支援指令。
        await asyncio.sleep(max(0, float(minimum_wait_seconds)))
        while time.monotonic() < deadline:
            try:
                updated = await client.get_messages(chat_id, ids=msg_id)
            except Exception as exc:
                log.warning(f"Avatar [{avatar}] tower edited-result fetch failed for {msg_id}: {exc}")
                break
            if updated:
                latest = updated
                updated_text = getattr(updated, "text", "") or ""
                if updated_text != initial_text or getattr(updated, "edit_date", None):
                    log.info(f"Avatar [{avatar}] tower edited settlement observed for msg {msg_id}; support may proceed.")
                    return updated
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(0.2, float(poll_seconds)), remaining))

        if time.monotonic() < minimum_deadline:
            await asyncio.sleep(minimum_deadline - time.monotonic())
        log.warning(
            f"Avatar [{avatar}] tower settlement was not observed as edited within "
            f"{int(timeout_seconds)}s; proceeding with the latest response after the safety wait."
        )
        return latest

    async def common_avatar_tower_tick(
        self,
        avatar,
        timeout=90,
        min_hour=23,
        exact_hour=None,
        handle_insufficient_cultivation=False,
        require_meditation_ready=False,
    ):
        """Run one time-gated avatar tower check."""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.get_avatar_state(avatar).get("last_tower_date") == today:
            return False
        if exact_hour is not None and now.hour != exact_hour:
            return False
        if min_hour is not None and now.hour < min_hour:
            return False
        return await self.common_avatar_tower_send(
            avatar,
            today=today,
            timeout=timeout,
            handle_insufficient_cultivation=handle_insufficient_cultivation,
            require_meditation_ready=require_meditation_ready,
        )

    async def run_common_avatar_tower_loop(
        self,
        avatar,
        initial_delay=0,
        timeout=90,
        handle_insufficient_cultivation=False,
        require_meditation_ready=False,
        sleep_func=None,
        delay_range=(10, 600),
    ):
        """Run the daily 23:00 avatar .闯塔 loop."""
        await self.startup_done.wait()
        if hasattr(self, "_avatar_loop_count"):
            self._avatar_loop_count += 1
        if initial_delay > 0:
            self.common_command_logger().info(
                f"Avatar [{avatar}] tower loop: waiting {initial_delay}s before start..."
            )
            await asyncio.sleep(initial_delay)

        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                a_state = self.get_avatar_state(avatar)
                last_date = a_state.get("last_tower_date", "")

                if (
                    last_date != today
                    and now.hour == 23
                    and self.avatar_tower_available(
                        avatar,
                        require_meditation_ready=require_meditation_ready,
                    )
                ):
                    delay = random.randint(*delay_range)
                    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    safe_delay = max(0, int((midnight - now).total_seconds()) - 30)
                    delay = min(delay, safe_delay)
                    log.info(f"Avatar [{avatar}] daily tower due today ({today}). Waiting {delay}s...")
                    await asyncio.sleep(delay)
                    after_delay = datetime.now()
                    if (
                        after_delay.strftime("%Y-%m-%d") == today
                        and after_delay.hour == 23
                        and self.get_avatar_state(avatar).get("last_tower_date", "") != today
                    ):
                        await self.common_avatar_tower_send(
                            avatar,
                            today=today,
                            timeout=timeout,
                            handle_insufficient_cultivation=handle_insufficient_cultivation,
                            require_meditation_ready=require_meditation_ready,
                        )

                await asyncio.sleep(
                    self.common_scheduler_sleep_seconds(600, sleep_func=sleep_func)
                )
            except Exception as exc:
                log.error(f"Avatar [{avatar}] tower loop error: {exc}", exc_info=True)
                await asyncio.sleep(300)

    async def common_avatar_daily_checkin(self, avatar, daily_start_wait_func=None):
        """Run one avatar .宗门点卯 check after the account's daily start time."""
        today = datetime.now().strftime("%Y-%m-%d")
        if daily_start_wait_func is not None and daily_start_wait_func(datetime.now()) > 0:
            return False
        avatar_state = self.get_avatar_state(avatar)
        if avatar_state.get("last_dianmao_date") == today:
            # A check-in is recorded before support so a support timeout never
            # causes the daily check-in itself to be sent twice.
            return await self.maybe_run_mulan_support(avatar, today=today)
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(".宗门点卯", avatar):
            return False
        if self.daily_one_shot_should_defer(
            avatar,
            ".宗门点卯",
            logger=self.common_command_logger(),
        ):
            return False
        resp = await self.send_and_wait_feedback_identity(avatar, ".宗门点卯", timeout=60)
        resp_text = self.timed_command_response_text(resp)
        if resp_text:
            self.set_avatar_state(avatar, "last_dianmao_date", today)
            try:
                await self.maybe_run_mulan_support(avatar, today=today)
            except Exception as exc:
                self.common_command_logger().error(
                    f"Avatar [{avatar}] Mulan support error after check-in: {exc}",
                    exc_info=True,
                )
            return True
        return False

    async def common_avatar_mulan_support(self, avatar, daily_start_wait_func=None):
        """Run the remaining avatar daily support command without sect check-in."""
        if seconds_until_mulan_support_start(datetime.now()) > 0:
            return False
        return await self.maybe_run_mulan_support(
            avatar,
            today=datetime.now().strftime("%Y-%m-%d"),
        )

    async def run_common_mulan_support_loop(
        self,
        daily_start_wait_func=None,
        daily_start_label_func=None,
        pre_loop_func=None,
        sleep_func=None,
    ):
        """Run the valid daily Mulan support command after retired check-in removal."""
        await self.startup_done.wait()
        log = self.common_command_logger()
        daily_start_wait_func = daily_start_wait_func or seconds_until_mulan_support_start
        daily_start_label_func = daily_start_label_func or mulan_support_start_label
        while getattr(self, "is_running", True):
            if pre_loop_func is not None:
                should_continue = await pre_loop_func()
                if should_continue:
                    continue

            now = datetime.now()
            daily_wait = daily_start_wait_func(now)
            if daily_wait > 0:
                next_run = now + timedelta(seconds=daily_wait)
                log.info(
                    f"Daily Mulan support waits until {daily_start_label_func()}. "
                    f"Next check at {dt_to_str(next_run)}."
                )
                await asyncio.sleep(
                    self.common_scheduler_sleep_seconds(
                        daily_wait,
                        sleep_func=sleep_func,
                    )
                )
                continue

            try:
                await self.maybe_run_mulan_support(
                    "主魂",
                    today=now.strftime("%Y-%m-%d"),
                )
            except Exception as exc:
                log.error(f"[主魂] Mulan support loop error: {exc}", exc_info=True)
            await asyncio.sleep(
                self.common_scheduler_sleep_seconds(600, sleep_func=sleep_func)
            )

    async def run_common_daily_tasks_loop(
        self,
        daily_start_wait_func,
        daily_start_label_func,
        task_commands,
        pre_loop_func=None,
        sleep_func=None,
        send_kwargs_func=None,
        mark_done_before_send=False,
        use_lingxiao_tower_buff=False,
        reset_heart_platform_date=False,
    ):
        """Run shared main-soul daily tasks while preserving account-specific send policy."""
        await self.startup_done.wait()
        log = self.common_command_logger()
        while getattr(self, "is_running", True):
            if pre_loop_func is not None:
                should_continue = await pre_loop_func()
                if should_continue:
                    continue

            now = datetime.now()
            daily_wait = daily_start_wait_func(now)
            if daily_wait > 0:
                next_run = now + timedelta(seconds=daily_wait)
                log.info(
                    f"Daily tasks paused before {daily_start_label_func()}. "
                    f"Next check at {dt_to_str(next_run)}."
                )
                await asyncio.sleep(
                    self.common_scheduler_sleep_seconds(
                        daily_wait + random.randint(0, 30),
                        sleep_func=sleep_func,
                    )
                )
                continue

            today = now.strftime("%Y-%m-%d")
            if self.state.get("date") != today:
                log.info(f"New Day Detected ({today}): Resetting state.")
                self.state["date"] = today
                self.state["done"] = []
                self.state["sect_skill_count"] = 0
                self.state["last_dianmao_date"] = ""
                self.state["last_dianmao_msg_id"] = 0
                if reset_heart_platform_date and self.state.get("heart_platform_date") != today:
                    self.state["heart_platform_date"] = ""
                self.save_state()

            done = self.state.setdefault("done", [])
            deferred_daily = False
            for command in task_commands:
                if command in done:
                    continue
                if self.daily_one_shot_should_defer("主魂", command, logger=log):
                    await asyncio.sleep(
                        self.common_scheduler_sleep_seconds(
                            LOW_PRIORITY_DAILY_DEFER_SECONDS,
                            sleep_func=sleep_func,
                        )
                    )
                    deferred_daily = True
                    break

                if mark_done_before_send:
                    done.append(command)
                    self.save_state()

                if (
                    command == ".闯塔"
                    and use_lingxiao_tower_buff
                    and getattr(self, "lingxiao_enabled", False)
                ):
                    await self.send_and_wait_feedback(".借天门势")
                    await asyncio.sleep(5)

                kwargs = send_kwargs_func(command) if callable(send_kwargs_func) else {}
                sent_msg = await self.send_and_wait_feedback(command, **(kwargs or {}))
                if sent_msg:
                    if not mark_done_before_send and command not in done:
                        done.append(command)
                    if command == ".宗门点卯":
                        self.state["last_dianmao_msg_id"] = sent_msg.id
                        self.state["last_dianmao_date"] = today
                    self.save_state()
                    if command == ".宗门点卯":
                        try:
                            await self.maybe_run_mulan_support("主魂", today=today)
                        except Exception as exc:
                            log.error(
                                f"[主魂] Mulan support error after check-in: {exc}",
                                exc_info=True,
                            )
                await asyncio.sleep(5)

            # If support timed out or was cooling down, retry it on a later
            # daily-loop pass without repeating the already-recorded check-in.
            if self.state.get("last_dianmao_date") == today or (
                ".宗门点卯" in done and self.state.get("last_dianmao_msg_id")
            ):
                try:
                    await self.maybe_run_mulan_support("主魂", today=today)
                except Exception as exc:
                    log.error(f"[主魂] Mulan support retry error: {exc}", exc_info=True)

            if deferred_daily:
                continue

            await asyncio.sleep(
                self.common_scheduler_sleep_seconds(600, sleep_func=sleep_func)
            )

    def common_record_sect_skill_response(self, resp, max_daily=SECT_SKILL_MAX_DAILY):
        """Parse .宗门传功 response and update today's sect skill count."""
        if not resp:
            return "unknown"
        resp = str(resp)
        count_match = re.search(r"今日已传功\s*\**\s*(\d+)\s*/\s*3", resp)
        if count_match:
            self.state["sect_skill_count"] = max(
                self.state.get("sect_skill_count", 0),
                int(count_match.group(1)),
            )
            return "counted"
        if any(k in resp for k in ["次数不足", "明日再来", "已经", "过于频繁"]):
            self.state["sect_skill_count"] = max_daily
            return "done"
        if any(k in resp for k in ["失败", "需回复", "主魂"]):
            self.common_command_logger().warning(
                f"Sect skill reply target invalid: {resp[:80]}..."
            )
            return "invalid"
        if any(k in resp for k in ["传功玉简已记录", "今日已传功", "成功", "元神", "传功", "玉简"]):
            self.state["sect_skill_count"] = min(
                max_daily,
                self.state.get("sect_skill_count", 0) + 1,
            )
            return "counted"
        notify_unrecognized_response(self, ".宗门传功", resp, self.common_command_logger(), "宗门传功")
        return "unknown"

    def common_next_star_manifest_dt(self, now=None, interval_hours=3):
        """Calculate the next star manifest boundary."""
        now = now or datetime.now()
        base_hour = (now.hour // interval_hours) * interval_hours
        candidate = now.replace(hour=base_hour, minute=0, second=0, microsecond=0)
        if now >= candidate:
            candidate += timedelta(hours=interval_hours)
        return candidate

    def common_active_star_gazing_target_dt(
        self,
        now=None,
        interval_hours=3,
        monitor_lead_seconds=3 * 60,
        shift_lead_seconds=0,
    ):
        """Return the manifest boundary while the current star-gazing window is active."""
        now = now or datetime.now()
        for target in (
            now.replace(minute=0, second=0, microsecond=0),
            self.next_star_manifest_dt(now),
        ):
            if target.hour % interval_hours != 0:
                continue
            window_start = target - timedelta(seconds=monitor_lead_seconds)
            shift_time = target - timedelta(seconds=shift_lead_seconds)
            if window_start <= now <= shift_time:
                return target
        return None

    def common_next_star_gazing_window_start_dt(
        self,
        now=None,
        interval_hours=3,
        monitor_lead_seconds=3 * 60,
        shift_lead_seconds=0,
    ):
        """Return (window_start, manifest_dt) for the next usable star-gazing window."""
        now = now or datetime.now()
        target = self.next_star_manifest_dt(now)
        window_start = target - timedelta(seconds=monitor_lead_seconds)
        if now > target - timedelta(seconds=shift_lead_seconds):
            target += timedelta(hours=interval_hours)
            window_start = target - timedelta(seconds=monitor_lead_seconds)
        return window_start, target

    def common_star_observatory_needs_calm(self, text):
        return bool(text and ("紊乱" in text or "黯淡" in text))

    def common_compact_seconds(self, seconds):
        seconds = max(0, int(seconds or 0))
        hours, rem = divmod(seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours}小时{minutes}分钟"
        if minutes:
            return f"{minutes}分钟{seconds}秒"
        return f"{seconds}秒"

    def common_response_text(self, resp):
        if hasattr(resp, "text"):
            return resp.text or ""
        if isinstance(resp, str):
            return resp
        return str(resp) if resp else ""

    def common_recent_command_guard_wait(self, command="", max_age_seconds=15):
        block = getattr(self, "_last_command_guard_block", {}) or {}
        at = block.get("at", 0) or 0
        if not at or time.monotonic() - at > max_age_seconds:
            return 0
        key = str(block.get("key", "") or "")
        if command and not key.startswith(str(command).strip()):
            return 0
        wait = int(block.get("wait", 0) or 0)
        blocked_until = float(block.get("blocked_until", 0) or 0)
        if blocked_until > time.monotonic():
            wait = max(wait, int(blocked_until - time.monotonic()))
        return max(0, wait)

    def common_apply_avatar_star_guard_backoff(
        self,
        avatar,
        command="",
        fields=None,
        reason="command guard",
        logger=None,
        log_level="info",
        include_reason=True,
    ):
        wait = self.common_recent_command_guard_wait(command)
        if wait <= 0:
            return False
        retry_at = add_seconds_str(now_str(), max(60, wait + 5))
        updates = {"next_star_check_time": retry_at}
        for field in fields or []:
            updates[field] = retry_at
        if not command or str(command).startswith(".观星台"):
            updates["star_observatory_needs_refresh"] = True
        self.update_avatar_states(avatar, updates)
        key = (getattr(self, "_last_command_guard_block", {}) or {}).get("key", command)
        suffix = f" ({reason})." if include_reason else "."
        message = f"Avatar [{avatar}] star cycle backed off by command guard [{key}] until {retry_at}{suffix}"
        log = logger or self.common_command_logger()
        getattr(log, log_level, log.info)(message)
        return True

    def common_avatar_star_due(self, avatar, key):
        value = self.get_avatar_state(avatar).get(key, "")
        return bool(value and not is_future(value))

    def common_avatar_star_recently_appeased(self, avatar, window_seconds=180, now=None):
        value = self.get_avatar_state(avatar).get("last_star_appease_time", "")
        if not value:
            return False
        try:
            now = now or datetime.now()
            elapsed = (now - datetime.strptime(value, TIME_FORMAT)).total_seconds()
            return 0 <= elapsed <= window_seconds
        except Exception:
            return False

    def common_next_avatar_star_wait_seconds(self, avatar):
        state = self.get_avatar_state(avatar)
        check_time = state.get("next_star_check_time", "")
        if state.get("star_observatory_needs_refresh") and (not check_time or not is_future(check_time)):
            return 0
        if (
            not state.get("last_star_observatory_time")
            and not state.get("next_star_collect_time")
            and not is_future(state.get("next_star_attraction_time", ""))
        ):
            return 0

        waits = []
        for key in [
            "star_attraction_retry_time",
            "next_star_check_time",
            "next_star_appease_time",
            "next_star_collect_time",
            "next_star_attraction_time",
        ]:
            value = state.get(key, "")
            if not value:
                continue
            if is_future(value):
                waits.append(seconds_until(value))
            else:
                waits.append(0)
        if not waits:
            return 0
        return max(0, min(waits))

    def common_star_gazing_sent_on_date(self, date_str=None):
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        return self.state.get("last_gazing_date") == date_str

    def common_star_shift_done_today(self, today=None):
        today = today or datetime.now().strftime("%Y-%m-%d")
        return self.state.get("last_star_shift_date") == today

    def common_star_gazing_good_opportunity(self, text, good_keywords):
        return bool(text and any(keyword in text for keyword in good_keywords))

    def common_star_gazing_manifest_fate_type(self, text):
        match = re.search(r"【((?:Good|Bad|Neutral)\s*-\s*[^】]+)】", text or "")
        return match.group(1).strip() if match else ""

    def common_star_gazing_pending_fate_type(self, text, good_keywords):
        for keyword in good_keywords:
            if text and keyword in text:
                return keyword.strip("【】")
        return ""

    def common_is_star_gazing_final_report(self, text):
        clean = str(text or "").replace("**", "")
        return "【天机阁快报" in clean

    def common_current_star_report_manifest_dt(self, now=None, interval_hours=3):
        now = now or datetime.now()
        base_hour = (now.hour // interval_hours) * interval_hours
        return now.replace(hour=base_hour, minute=0, second=0, microsecond=0)

    def common_star_gazing_final_report_seen(self, target_dt):
        if not target_dt:
            return False
        return self.state.get("last_star_gazing_report_manifest_time", "") == dt_to_str(target_dt)

    def common_is_star_shift_attempt_feedback(self, text):
        clean = str(text or "").replace("**", "")
        return "你开始消耗" in clean and "扭转因果" in clean

    def common_star_shift_target_in_success(self, text, target_username):
        clean = str(text or "").replace("**", "")
        target = str(target_username or "").strip().lstrip("@")
        if not target or "【天机异动】" not in clean or "改换星移" not in clean:
            return False
        pattern = rf"将由\s*@?{re.escape(target)}\b\s*承受"
        return bool(re.search(pattern, clean, flags=re.IGNORECASE))

    def common_star_shift_actor_identity(self, text):
        clean = str(text or "").replace("**", "")
        match = re.search(r"弟子\s*@([A-Za-z0-9_]+)\s*强行施展", clean)
        if not match:
            return ""
        return (getattr(self, "avatar_usernames", {}) or {}).get(match.group(1).lower(), "")

    def common_star_shift_identity_from_text(self, text):
        return (
            self.common_star_shift_actor_identity(text)
            or avatar_marker_identity_from_text(text)
            or self.state.get("star_gazing_claimed_avatar", "")
            or "主魂"
        )

    def common_star_shift_attempted_for_identity(self, identity="主魂", date_str=None):
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        identity = str(identity or "主魂").strip() or "主魂"
        if identity != "主魂" and hasattr(self, "get_avatar_state"):
            return self.get_avatar_state(identity).get("last_star_shift_date") == date_str
        return self.state.get("last_star_shift_date") == date_str

    def common_mark_star_shift_attempt(self, identity="主魂", date_str=None, source="", logger=None):
        """Mark one .改换星移 attempt for a round before any later duplicate task can send."""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        identity = str(identity or "主魂").strip() or "主魂"
        changed = False
        stamp = now_str()
        manifest_key = (
            self.state.get("star_gazing_claimed_manifest_time", "")
            or self.state.get("pending_star_gazing_manifest_time", "")
        )
        if manifest_key:
            self.common_mark_star_gazing_round_assigned(
                manifest_key,
                identity,
                source=source or ".改换星移 attempt",
                logger=logger,
            )

        if identity != "主魂" and hasattr(self, "get_avatar_state") and hasattr(self, "set_avatar_state"):
            state = self.get_avatar_state(identity)
            if state.get("last_star_shift_date") != date_str:
                self.set_avatar_state(identity, "last_star_shift_date", date_str)
                self.set_avatar_state(identity, "last_star_shift_time", stamp)
                changed = True
        else:
            if self.state.get("last_star_shift_date") != date_str:
                self.state["last_star_shift_date"] = date_str
                self.state["last_star_shift_time"] = stamp
                changed = True

        if changed:
            log = logger or self.common_command_logger()
            who = identity or "主魂"
            suffix = f" ({source})" if source else ""
            log.info(f"Star gazing [{who}]: marked .改换星移 attempted for {date_str}{suffix}.")
        return changed

    def common_clear_star_shift_pending_after_attempt(self, identity=""):
        identity = str(identity or "").strip()
        if identity and identity != "主魂" and hasattr(self, "clear_avatar_star_gazing_pending"):
            self.clear_avatar_star_gazing_pending(identity)
        if hasattr(self, "clear_pending_star_shift"):
            self.clear_pending_star_shift()
        else:
            self.state["pending_star_gazing_manifest_time"] = ""
            self.state["pending_star_gazing_fate_type"] = ""
        self.common_clear_star_gazing_round_claim()

    def common_record_star_shift_attempt_message(self, msg, text, target_username, source="", logger=None):
        """Record accepted/successful .改换星移 messages so duplicate scheduled tasks stand down."""
        log = logger or self.common_command_logger()
        identity = ""
        reason = ""

        if self.common_star_shift_target_in_success(text, target_username):
            identity = self.common_star_shift_identity_from_text(text)
            reason = "success broadcast"
        elif self.common_is_star_shift_attempt_feedback(text):
            command = str(tracked_command_text_for_reply(self, msg) or "").strip()
            if command and not command.startswith(".改换星移"):
                return False
            if not command and not avatar_marker_identity_from_text(text):
                return False
            identity = tracked_command_identity_for_reply(self, msg) or avatar_marker_identity_from_text(text) or "主魂"
            reason = "attempt accepted"
        else:
            return False

        date_str = datetime.now().strftime("%Y-%m-%d")
        changed = self.common_mark_star_shift_attempt(
            identity,
            date_str,
            source=f"{reason}; {source}" if source else reason,
            logger=log,
        )
        self.common_clear_star_shift_pending_after_attempt(identity)
        self.save_state()
        return True

    def common_is_star_gazing_forbidden_response(self, text, forbidden_keywords):
        return bool(text and any(keyword in text for keyword in forbidden_keywords))

    def common_is_star_gazing_valid_result(self, text, valid_keywords):
        return bool(text and any(keyword in text for keyword in valid_keywords))

    def common_get_avatar_username(self, avatar):
        for uname, name in getattr(self, "avatar_usernames", {}).items():
            if name == avatar:
                return uname
        return ""

    def common_normalize_username(self, value):
        value = str(value or "").lower().strip()
        value = value.replace("**", "").replace("`", "")
        value = value.strip("@ \t\r\n【】[]（）()：:,，。.!！")
        return re.sub(r"\s+", "", value)

    def common_star_gazing_observer_identity(self, text):
        match = re.search(r"@([A-Za-z0-9_]+)\s+闭目凝神", text or "")
        if not match:
            return ""
        username = self.common_normalize_username(match.group(1))
        for configured, identity in (getattr(self, "avatar_usernames", {}) or {}).items():
            if self.common_normalize_username(configured) == username:
                return identity or ""
        return ""

    def common_remember_star_gazing_response_identity(self, msg_id, identity):
        try:
            msg_id = int(msg_id or 0)
        except Exception:
            msg_id = 0
        if not msg_id:
            return
        cache = getattr(self, "_star_gazing_response_identities", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_star_gazing_response_identities", cache)
        cache[msg_id] = str(identity or "").strip() or "主魂"
        if len(cache) > 120:
            for old_id in list(cache.keys())[:-60]:
                cache.pop(old_id, None)

    def common_star_gazing_response_matches_identity(self, msg, text, identity, logger=None):
        """Return True only when a .观星 result belongs to the expected identity."""
        identity = str(identity or "主魂").strip() or "主魂"
        text = str(text or "")
        msg_id = getattr(msg, "id", 0)
        marker = avatar_marker_identity_from_text(text)
        if marker and marker != identity:
            if logger:
                logger.info(
                    f"Star gazing [{identity}]: rejected response msg {msg_id}; "
                    f"avatar marker is {marker}."
                )
            return False

        replied_id = meaningful_reply_to_msg_id(self, msg)
        if replied_id:
            command = str(tracked_command_text_for_reply(self, msg) or "").strip()
            reply_identity = tracked_command_identity_for_reply(self, msg) or ""
            if command == ".观星" and (not reply_identity or reply_identity == identity):
                self.common_remember_star_gazing_response_identity(msg_id, identity)
                return True
            if logger:
                logger.info(
                    f"Star gazing [{identity}]: rejected response msg {msg_id}; "
                    f"reply target is [{reply_identity or 'unknown'}] {command or 'untracked'}."
                )
            return False

        observer = self.common_star_gazing_observer_identity(text)
        if observer:
            if observer == identity:
                self.common_remember_star_gazing_response_identity(msg_id, identity)
                return True
            if logger:
                logger.info(
                    f"Star gazing [{identity}]: rejected response msg {msg_id}; "
                    f"observer belongs to {observer}."
                )
            return False

        mentions = {self.common_normalize_username(v) for v in text_username_mentions(text)}
        mentions = {v for v in mentions if v}
        known = identity_plain_usernames(self, identity)
        if mentions and known and not (mentions & known):
            if logger:
                logger.info(
                    f"Star gazing [{identity}]: rejected response msg {msg_id}; "
                    "mentions do not include this identity."
                )
            return False
        return True

    def common_star_gazing_reply_target_matches_identity(self, reply_msg_id, identity, logger=None):
        """Validate that a stored .改换星移 reply target is this identity's .观星 response."""
        identity = str(identity or "主魂").strip() or "主魂"
        try:
            reply_msg_id = int(reply_msg_id or 0)
        except Exception:
            reply_msg_id = 0
        if not reply_msg_id:
            return False

        cache = getattr(self, "_star_gazing_response_identities", None)
        if isinstance(cache, dict) and cache.get(reply_msg_id) == identity:
            return True

        try:
            conn = sqlite3.connect(MESSAGE_EVENTS_DB_FILE, timeout=2)
            with conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM command_ledger
                    WHERE account=?
                      AND response_msg_id=?
                      AND command='.观星'
                      AND identity=?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (actor_account_key(self), reply_msg_id, identity),
                ).fetchone()
        except Exception as exc:
            if logger:
                logger.debug(f"Star gazing [{identity}]: reply target ledger check skipped: {exc}")
            row = None

        if row:
            self.common_remember_star_gazing_response_identity(reply_msg_id, identity)
            return True
        if logger:
            logger.warning(
                f"Star gazing [{identity}]: blocked .改换星移 reply target {reply_msg_id}; "
                "not recorded as this identity's .观星 response."
            )
        return False

    def common_star_gazing_schedule_plan(self, now, manifest_dt, command_lead_seconds=60):
        """Return (.观星 send time, immediate_shift flag, consumed gazing date)."""
        min_lead_seconds = 10
        lead_seconds = max(min_lead_seconds, int(command_lead_seconds))
        latest_send_dt = manifest_dt - timedelta(seconds=min_lead_seconds)
        send_dt = manifest_dt - timedelta(seconds=lead_seconds)
        immediate_shift = False
        if send_dt <= now:
            send_dt = min(now + timedelta(seconds=3), latest_send_dt)
        return send_dt, immediate_shift, send_dt.strftime("%Y-%m-%d")

    def common_star_gazing_send_dt(self, target_dt, command_lead_seconds=60):
        return target_dt - timedelta(seconds=command_lead_seconds)

    def common_star_gazing_target_for_opportunity(
        self,
        now=None,
        interval_hours=3,
        min_observe_lead_seconds=60,
    ):
        now = now or datetime.now()
        target_dt = self.next_star_manifest_dt(now)
        latest_observe_dt = target_dt - timedelta(seconds=min_observe_lead_seconds)
        if now > latest_observe_dt:
            target_dt += timedelta(hours=interval_hours)
        return target_dt

    def common_star_gazing_manifest_for_notice(self, now=None, interval_hours=3):
        """Return the manifest round a Good notice may still use, or None after final news."""
        now = now or datetime.now()
        current = now.replace(minute=0, second=0, microsecond=0)
        if current.hour % interval_hours == 0:
            post_boundary_noise_end = current + timedelta(seconds=120)
            if current <= now <= post_boundary_noise_end:
                if self.star_gazing_final_report_seen(current):
                    return None
                return current
            if self.star_gazing_final_report_seen(current):
                return None

        target_dt = self.next_star_manifest_dt(now)
        return None if self.star_gazing_final_report_seen(target_dt) else target_dt

    def common_daily_star_gazing_fallback_dt(self, now=None, hour=23, minute=59):
        now = now or datetime.now()
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def common_pending_daily_star_gazing_fallback_dt(self, now=None):
        """Daily fallback is disabled; only confirmed Good events may schedule .观星."""
        return None

    def common_parse_avatar_star_observatory(self, text):
        clean = (text or "").replace("**", "").replace("`", "")
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        disk_lines = [
            line for line in lines
            if "引星盘" in line
            and ":" in line
            and "观星台" not in line
            and "总数" not in line
            and not line.startswith("使用")
        ]
        valid = "【星宫 · 观星台】" in clean or ("观星台" in clean and bool(disk_lines))
        if not valid and not disk_lines:
            return {"valid": False}

        total_match = re.search(r"总数\s*[:：]\s*(\d+)\s*座", clean)
        total_count = int(total_match.group(1)) if total_match else len(disk_lines)
        empty_count = 0
        occupied_count = 0
        collect_ready = False
        needs_appease = False
        remaining_values = []
        stars = set()

        for line in disk_lines:
            status = line.split(":", 1)[1].strip()
            if "空闲" in status:
                empty_count += 1
                continue
            occupied_count += 1
            star_match = re.search(r":\s*([^-:：\n]+?)\s*(?:-|$)", line)
            if star_match:
                star_name = star_match.group(1).strip()
                if star_name and "空闲" not in star_name:
                    stars.add(star_name)
            if "精华已成" in status:
                collect_ready = True
            if any(k in status for k in ["星光黯淡", "元磁紊乱", "狂暴星力", "黯淡", "紊乱"]):
                needs_appease = True
            cd = self.parse_wait_time(line)
            if cd and cd > 0 and "剩余" in line:
                remaining_values.append(cd)

        min_remaining = min(remaining_values) if remaining_values else None
        if collect_ready:
            summary = "精华已成"
        elif min_remaining is not None:
            summary = f"凝聚中，剩余{self.common_compact_seconds(min_remaining)}"
        elif needs_appease:
            summary = "需安抚"
        elif total_count and empty_count >= total_count:
            summary = "全部空闲"
        elif occupied_count:
            summary = "已占用，待复查"
        else:
            summary = "未知"

        return {
            "valid": True,
            "total_count": total_count,
            "empty_count": empty_count,
            "occupied_count": occupied_count,
            "collect_ready": collect_ready,
            "needs_appease": needs_appease,
            "min_remaining": min_remaining,
            "stars": sorted(stars),
            "summary": summary,
        }

    def common_record_avatar_star_observatory(
        self,
        avatar,
        text,
        source="观星台",
        star_target="天雷星",
        pre_appease_lead_seconds=30 * 60,
        status_retry_seconds=10 * 60,
        logger=None,
    ):
        info = self.parse_avatar_star_observatory(text)
        if not info.get("valid"):
            return False

        now = now_str()
        updates = {
            "last_star_observatory_time": now,
            "star_observatory_summary": info.get("summary", ""),
            "star_observatory_needs_refresh": False,
            "star_target": star_target,
            "star_pre_collect_appeased_for": "",
        }

        min_remaining = info.get("min_remaining")
        if info.get("collect_ready"):
            updates["next_star_collect_time"] = now
            if info.get("needs_appease"):
                updates["next_star_appease_time"] = now
            updates["next_star_check_time"] = now
        elif min_remaining is not None:
            collect_time = add_seconds_str(now, min_remaining)
            appease_delay = max(0, min_remaining - pre_appease_lead_seconds)
            appease_time = add_seconds_str(now, appease_delay)
            updates["next_star_collect_time"] = collect_time
            updates["next_star_attraction_time"] = collect_time
            if info.get("needs_appease"):
                updates["next_star_appease_time"] = now
                updates["next_star_check_time"] = now
            else:
                updates["next_star_appease_time"] = appease_time
                updates["next_star_check_time"] = appease_time
        elif info.get("total_count") and info.get("empty_count", 0) >= info.get("total_count", 0):
            updates["next_star_collect_time"] = ""
            updates["next_star_appease_time"] = ""
            next_attraction = self.get_avatar_state(avatar).get("next_star_attraction_time", "")
            if not is_future(next_attraction):
                updates["next_star_attraction_time"] = now
                updates["next_star_check_time"] = now
            else:
                updates["next_star_check_time"] = next_attraction
        elif info.get("needs_appease"):
            updates["next_star_appease_time"] = now
            updates["next_star_check_time"] = now
        else:
            updates["next_star_check_time"] = add_seconds_str(now, status_retry_seconds)

        self.update_avatar_states(avatar, updates)
        (logger or self.common_command_logger()).info(
            f"Avatar [{avatar}] star observatory synced ({source}): {info.get('summary', '')}"
        )
        return True

    def common_star_cycle_collect_time_from_now(self, cooldown_seconds):
        return add_seconds_str(now_str(), cooldown_seconds)

    def common_record_avatar_star_pull_response(
        self,
        avatar,
        text,
        source,
        star_command,
        star_target="天雷星",
        pre_appease_lead_seconds=30 * 60,
        status_retry_seconds=10 * 60,
        cooldown_seconds=36 * 3600,
        logger=None,
    ):
        log = logger or self.common_command_logger()
        if not text:
            retry_at = add_seconds_str(now_str(), status_retry_seconds)
            self.update_avatar_states(avatar, {
                "star_attraction_retry_time": retry_at,
                "next_star_attraction_time": retry_at,
                "next_star_check_time": retry_at,
            })
            return "empty"

        clean = text.replace("**", "")
        now = now_str()
        if "修为不足" in clean:
            self.update_avatar_states(avatar, {
                "star_attraction_retry_time": now,
                "next_star_attraction_time": now,
            })
            return "insufficient"

        if "已无空闲" in clean and "引星盘" in clean:
            self.update_avatar_states(avatar, {
                "star_observatory_needs_refresh": True,
                "next_star_check_time": now,
            })
            return "no_free"

        cd = self.parse_wait_time(clean)
        if cd and cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余"]):
            due = add_seconds_str(now, cd)
            appease_time = add_seconds_str(now, max(0, cd - pre_appease_lead_seconds))
            self.update_avatar_states(avatar, {
                "next_star_attraction_time": due,
                "next_star_collect_time": due,
                "next_star_appease_time": appease_time,
                "next_star_check_time": appease_time,
                "star_pre_collect_appeased_for": "",
                "star_attraction_retry_time": "",
                "star_attraction_force_exit_tried": False,
            })
            return "cooldown"

        if any(k in clean for k in ["牵引成功", "成功在", "牵引了", "开始牵引"]):
            collect_time = self.common_star_cycle_collect_time_from_now(cooldown_seconds)
            appease_time = add_seconds_str(collect_time, -pre_appease_lead_seconds)
            self.update_avatar_states(avatar, {
                "last_star_attraction_time": now,
                "next_star_attraction_time": collect_time,
                "next_star_collect_time": collect_time,
                "next_star_appease_time": appease_time,
                "next_star_check_time": appease_time,
                "star_pre_collect_appeased_for": "",
                "star_attraction_retry_time": "",
                "star_attraction_force_exit_tried": False,
                "star_observatory_needs_refresh": False,
                "star_observatory_summary": f"{star_target}凝聚中",
            })
            return "success"

        self.update_avatar_states(avatar, {
            "star_observatory_needs_refresh": True,
            "next_star_check_time": now,
        })
        notify_unrecognized_response(self, star_command, text, log, source)
        return "unknown"

    def common_record_avatar_star_appease_response(
        self,
        avatar,
        text,
        source=".安抚星辰",
        status_retry_seconds=10 * 60,
        logger=None,
    ):
        now = now_str()
        clean = (text or "").replace("**", "")
        if clean and any(k in clean for k in ["成功安抚", "安抚了", "没有需要安抚", "无需安抚", "不需要安抚"]):
            state = self.get_avatar_state(avatar)
            next_collect = state.get("next_star_collect_time", "")
            self.update_avatar_states(avatar, {
                "last_star_appease_time": now,
                "next_star_appease_time": "",
                "next_star_check_time": next_collect if next_collect else add_seconds_str(now, status_retry_seconds),
            })
            return "success"

        cd = self.parse_wait_time(clean)
        if cd and cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余"]):
            due = add_seconds_str(now, cd)
            self.update_avatar_states(avatar, {
                "next_star_appease_time": due,
                "next_star_check_time": due,
            })
            return "cooldown"

        self.update_avatar_states(avatar, {
            "star_observatory_needs_refresh": True,
            "next_star_check_time": now,
        })
        if text:
            notify_unrecognized_response(self, ".安抚星辰", text, logger or self.common_command_logger(), source)
        return "unknown"

    def common_record_avatar_star_collect_response(
        self,
        avatar,
        text,
        source=".收集精华",
        pre_appease_lead_seconds=30 * 60,
        logger=None,
    ):
        now = now_str()
        clean = (text or "").replace("**", "")
        if clean and any(k in clean for k in ["收集完成", "成功从", "获得了"]):
            self.record_daily_reward_event(avatar, ".收集精华", text, source=source)
            self.update_avatar_states(avatar, {
                "last_star_collect_time": now,
                "next_star_collect_time": "",
                "next_star_appease_time": "",
                "next_star_attraction_time": now,
                "next_star_check_time": now,
                "star_pre_collect_appeased_for": "",
                "star_observatory_needs_refresh": False,
                "star_attraction_force_exit_tried": False,
            })
            return "success"

        if clean and any(k in clean for k in ["没有已凝聚", "没有可供收集", "暂无精华"]):
            self.update_avatar_states(avatar, {
                "star_observatory_needs_refresh": True,
                "next_star_check_time": now,
                "star_pre_collect_appeased_for": "",
            })
            return "not_ready"

        cd = self.parse_wait_time(clean)
        if cd and cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "剩余"]):
            collect_time = add_seconds_str(now, cd)
            appease_time = add_seconds_str(now, max(0, cd - pre_appease_lead_seconds))
            self.update_avatar_states(avatar, {
                "next_star_collect_time": collect_time,
                "next_star_appease_time": appease_time,
                "next_star_check_time": appease_time,
                "star_pre_collect_appeased_for": "",
            })
            return "cooldown"

        self.update_avatar_states(avatar, {
            "star_observatory_needs_refresh": True,
            "next_star_check_time": now,
            "star_pre_collect_appeased_for": "",
        })
        if text:
            notify_unrecognized_response(self, ".收集精华", text, logger or self.common_command_logger(), source)
        return "unknown"

    def common_record_avatar_star_response_from_text(
        self,
        avatar,
        text,
        source="star sync",
        star_avatars=(),
        star_target="天雷星",
    ):
        if avatar not in star_avatars or not text:
            return False
        clean = text.replace("**", "")
        if "已无空闲" in clean and "引星盘" in clean:
            return self.record_avatar_star_pull_response(avatar, text, source) in {"no_free"}
        if "观星台" in clean and "引星盘" in clean:
            return self.record_avatar_star_observatory(avatar, text, source)
        if (
            any(k in clean for k in ["牵引成功", "牵引了", "开始牵引", "牵引星辰"])
            or ("修为不足" in clean and star_target in clean)
            or ("冷却" in clean and "牵引" in clean)
        ):
            return self.record_avatar_star_pull_response(avatar, text, source) in {"success", "cooldown", "no_free", "insufficient"}
        if "安抚" in clean and ("引星盘" in clean or "星辰" in clean or "狂暴星力" in clean):
            return self.record_avatar_star_appease_response(avatar, text, source) in {"success", "cooldown"}
        if "收集" in clean or "精华" in clean:
            return self.record_avatar_star_collect_response(avatar, text, source) in {"success", "cooldown", "not_ready"}
        return False

    async def wait_for_field_training_settlement(
        self,
        resp,
        identity="主魂",
        timeout_seconds=FIELD_TRAINING_SETTLEMENT_WAIT_SECONDS,
        poll_seconds=1.5,
    ):
        """
        野外历练先回复“正在行进”，随后编辑为结算。
        等到编辑结算后再记录收益和刷新冷却，避免把初始回复当成完成结果。
        """
        text = self.field_training_response_text(resp)
        if not self.is_field_training_pending_response(text):
            return resp

        msg_id = getattr(resp, "id", None)
        client = getattr(self, "client", None)
        chat_id = getattr(self, "target_chat_id", None)
        log = self.common_command_logger()
        if not msg_id or not client or chat_id is None:
            log.warning(f"[{identity}] field training pending response has no fetchable message id; cannot wait for edited settlement.")
            return resp

        deadline = time.monotonic() + max(1, timeout_seconds)
        last_text = text
        while time.monotonic() < deadline:
            await asyncio.sleep(max(0.01, poll_seconds))
            try:
                updated = await client.get_messages(chat_id, ids=msg_id)
            except Exception as exc:
                log.warning(f"[{identity}] field training edited-result fetch failed for {msg_id}: {exc}")
                return resp
            updated_text = self.field_training_response_text(updated)
            if self.is_field_training_settlement_response(updated_text) or self.is_field_training_cooldown_response(updated_text):
                if updated_text and updated_text != last_text:
                    log.info(f"[{identity}] field training edited settlement observed for msg {msg_id}.")
                return updated
            last_text = updated_text or last_text

        log.warning(f"[{identity}] field training settlement was not edited within {timeout_seconds}s; using initial response.")
        return resp

    # ---- 宗门战 — 辅助方法 ----

    def account_sect_name(self):
        """获取本账号的宗门名称"""
        mapping = getattr(self, "identity_sect_names", None) or self.state.get("identity_sect_names") or {}
        return str(mapping.get("主魂") or self.state.get("miniapp_sect_name")
                   or self.state.get("sect_name") or getattr(self, "sect_name", "") or "").strip()

    def sync_identity_sect_from_text(self, identity="主魂", text=""):
        """Learn an identity's current sect from an authoritative game reply.

        Wake the shared sect scheduler after a confirmed membership change.
        """
        identity = self.resolve_avatar_identity(identity or "主魂")
        text = str(text or "").replace("**", "").replace("`", "")
        if not text:
            return ""
        sect = ""
        # Prefer explicit membership/assignment phrases over incidental sect
        # mentions in reward or battle text.
        explicit_patterns = (
            r"(?:所属宗门|当前宗门|宗门)\s*[:：]?\s*[【\[]?([^】\]，,。\s]+)",
            r"(?:你已|你成功|成功|正式|你)(?:拜入|加入|转入)(?:了)?\s*[【\[]?([^】\]，,。\s]+)",
            r"(?:宗门身份|门派)\s*[:：]?\s*[【\[]?([^】\]，,。\s]+)",
        )
        for pattern in explicit_patterns:
            for match in re.finditer(pattern, text):
                line_start = text.rfind("\n", 0, match.start()) + 1
                prefix = text[line_start:match.start()]
                if re.search(r"无法|不能|尚未|未能|未曾|请先|是否|若要|想要|如需|未加入|并非", prefix):
                    continue
                candidate = str(match.group(1) or "").strip("【】[]()（） ：:，,。")
                if candidate in KNOWN_SECTS:
                    sect = candidate
                    break
            if sect:
                break
        if not sect:
            # Defection replies often contain only the old sect and a generic
            # phrase; mark the identity as 散修 so no sect-specific command is
            # accidentally sent while waiting for the next membership sync.
            if (re.search(r"(?:你已|你成功|成功|你)(?:叛出宗门|退出宗门|离开宗门|脱离宗门)|斩断与.{0,12}尘缘", text)
                    and not re.search(r"无法|不能|未能|失败", text)):
                sect = "散修"
        if not sect:
            return ""
        mapping = getattr(self, "identity_sect_names", None)
        if not isinstance(mapping, dict):
            mapping = {}
            setattr(self, "identity_sect_names", mapping)
        changed = mapping.get(identity) != sect
        mapping[identity] = sect
        state = getattr(self, "state", None)
        if isinstance(state, dict):
            state_mapping = state.setdefault("identity_sect_names", {})
            if isinstance(state_mapping, dict):
                state_mapping[identity] = sect
            if identity == "主魂":
                state["sect_name"] = sect
            container = self.identity_state_for_timed_command(identity)
            container["sect_name"] = sect
            container["miniapp_sect_name"] = sect
        if identity == "主魂" and hasattr(self, "sect_name"):
            self.sect_name = sect
        if changed:
            try:
                self.save_state()
            except Exception:
                self.common_command_logger().warning("Failed to persist sect learned from reply.", exc_info=True)
            self.common_command_logger().info("Sect refreshed from reply: %s -> %s", identity, sect)
            self.wake_sect_tasks()
        return sect

    def identity_sect_name(self, identity="主魂"):
        """获取某个身份所属宗门；黄龙山等身份级活动使用。"""
        identity = str(identity or "主魂").strip() or "主魂"
        mapping = getattr(self, "identity_sect_names", None)
        if not isinstance(mapping, dict):
            mapping = self.state.get("identity_sect_names", {})
        if isinstance(mapping, dict):
            sect = str(mapping.get(identity, "") or "").strip()
            if sect and sect not in TRANSIENT_SECT_NAMES:
                return sect
        if identity == "主魂":
            sect = self.account_sect_name()
            return "" if sect in TRANSIENT_SECT_NAMES else sect
        return ""

    def identity_sect_map(self):
        """返回当前脚本所有已知身份的宗门映射。"""
        identities = ["主魂", *list(getattr(self, "avatars", []) or [])]
        result = {}
        for identity in identities:
            sect = self.identity_sect_name(identity)
            if sect:
                result[identity] = sect
        return result

    def huanglong_identities_for_sect(self, sect):
        sect = str(sect or "").strip()
        if not sect:
            return []
        return [identity for identity, identity_sect in self.identity_sect_map().items() if identity_sect == sect]

    def huanglong_identity_sect_map_complete(self):
        """Return whether every configured identity currently has a usable sect mapping."""
        identities = ["主魂", *list(getattr(self, "avatars", []) or [])]
        return all(self.identity_sect_name(identity) for identity in identities)

    def huanglong_rotation_report_complete_for_date(self, date_text):
        if self.state.get("huanglong_rotation_report_date") != date_text:
            return False
        return self.state.get("huanglong_rotation_report_status") in {"matched", "no_matching_identity"}

    def record_huanglong_rotation_report(self, date_text, sect, msg_id, source, status, now_dt=None):
        """Persist the daily rotation report even when this account has no matching identity."""
        now_dt = now_dt or datetime.now()
        values = {
            "huanglong_rotation_report_date": date_text,
            "huanglong_rotation_report_sect": sect,
            "huanglong_rotation_report_msg_id": str(msg_id or ""),
            "huanglong_rotation_report_time": dt_to_str(now_dt),
            "huanglong_rotation_report_source": str(source or "message"),
            "huanglong_rotation_report_status": status,
            "next_huanglong_report_search_time": (
                ""
                if status in {"matched", "no_matching_identity"}
                else dt_to_str(now_dt + timedelta(seconds=HUANGLONG_REPORT_SEARCH_RETRY_SECONDS))
            ),
        }
        if any(self.state.get(key) != value for key, value in values.items()):
            self.state.update(values)
            self.save_state()

    def huanglong_message_local_date(self, msg):
        msg_dt = getattr(msg, "date", None)
        if not msg_dt:
            return ""
        try:
            if getattr(msg_dt, "tzinfo", None) is not None:
                msg_dt = datetime.fromtimestamp(msg_dt.timestamp())
            return msg_dt.strftime("%Y-%m-%d")
        except Exception:
            return ""

    async def backfill_recent_huanglong_report(self, now_dt=None, reason="sect war loop"):
        """Search today's report during the signup window when a NewMessage update was missed."""
        now_dt = now_dt or datetime.now()
        in_window, _ = self.huanglong_signup_window_status(now_dt)
        if not in_window:
            return False

        self.ensure_common_command_state()
        today = now_dt.strftime("%Y-%m-%d")
        if self.huanglong_rotation_report_complete_for_date(today):
            return True

        next_search = str(self.state.get("next_huanglong_report_search_time") or "")
        if next_search:
            try:
                if datetime.strptime(next_search, TIME_FORMAT) > now_dt:
                    return False
            except Exception:
                pass

        client = getattr(self, "client", None)
        chat_id = getattr(self, "target_chat_id", None)
        if client is None or chat_id is None or not hasattr(client, "get_messages"):
            return False

        self.state["next_huanglong_report_search_time"] = dt_to_str(
            now_dt + timedelta(seconds=HUANGLONG_REPORT_SEARCH_RETRY_SECONDS)
        )
        self.save_state()
        log = self.common_command_logger()
        try:
            messages = await client.get_messages(
                chat_id,
                limit=HUANGLONG_REPORT_SEARCH_LIMIT,
                search=HUANGLONG_REPORT_TITLE,
            )
        except Exception as exc:
            log.warning(f"Huanglong report backfill failed ({reason}): {exc}")
            return False

        if messages is None:
            messages = []
        elif not isinstance(messages, (list, tuple)):
            try:
                messages = list(messages)
            except TypeError:
                messages = [messages]

        for msg in messages:
            text = getattr(msg, "text", None) or getattr(msg, "message", None) or ""
            if not self.parse_huanglong_rotation_sect(text):
                continue
            message_date = self.huanglong_message_local_date(msg)
            if message_date and message_date != today:
                continue
            sender = None
            get_sender = getattr(msg, "get_sender", None)
            if callable(get_sender):
                try:
                    sender = await get_sender()
                except Exception:
                    sender = None
            if sender is not None and not is_game_bot_sender(self, sender):
                continue
            log.info(
                "Huanglong rotation report recovered from recent messages "
                f"for {today} ({reason}, msg={getattr(msg, 'id', '')})."
            )
            return self.maybe_handle_huanglong_report_message(
                msg,
                text,
                sender,
                now_dt=now_dt,
                source=f"backfill:{reason}",
            )
        return False

    def huanglong_report_loop_wait_seconds(self, now_dt=None):
        """Sleep toward 12:00 precisely, then honor the short in-window retry schedule."""
        now_dt = now_dt or datetime.now()
        today = now_dt.strftime("%Y-%m-%d")
        start = now_dt.replace(hour=12, minute=0, second=0, microsecond=0)
        end = now_dt.replace(hour=14, minute=0, second=0, microsecond=0)
        if now_dt < start:
            return max(5, min(300, int((start - now_dt).total_seconds()) + 5))
        if now_dt < end and not self.huanglong_rotation_report_complete_for_date(today):
            next_search = str(self.state.get("next_huanglong_report_search_time") or "")
            if next_search:
                try:
                    wait = int((datetime.strptime(next_search, TIME_FORMAT) - now_dt).total_seconds())
                    if wait > 0:
                        return max(5, min(HUANGLONG_REPORT_SEARCH_RETRY_SECONDS, wait))
                except Exception:
                    pass
            return 5
        return 300

    def clean_common_text(self, text):
        """去除 markdown 加粗标记和反引号"""
        return (text or "").replace("**", "").replace("`", "")

    def parse_huanglong_rotation_sect(self, text):
        """从黄龙山轮值军报中解析今日轮值宗门。"""
        clean = self.clean_common_text(text)
        if HUANGLONG_REPORT_TITLE not in clean:
            return ""
        if "黄龙山" not in clean or "轮值宗门" not in clean:
            return ""

        patterns = [
            r"轮值宗门为\s*【([^】]{2,12})】",
            r"轮值宗门为\s*([一-龥]{2,12})",
        ]
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                return match.group(1).strip()

        for sect in KNOWN_SECTS:
            if sect in clean:
                return sect
        return ""

    def huanglong_signup_window_status(self, now_dt=None):
        """黄龙山报名只允许 12:00 <= now < 14:00。"""
        now_dt = now_dt or datetime.now()
        start = now_dt.replace(hour=12, minute=0, second=0, microsecond=0)
        end = now_dt.replace(hour=14, minute=0, second=0, microsecond=0)
        if now_dt < start:
            return False, "too_early"
        if now_dt >= end:
            return False, "window_closed"
        return True, "open"

    def ensure_huanglong_signup_records(self):
        records = self.state.get("huanglong_signup_records")
        if not isinstance(records, dict):
            records = {}
            self.state["huanglong_signup_records"] = records
        return records

    def huanglong_signup_record_key(self, date_text, identity, sect):
        return f"{date_text}|{identity or '主魂'}|{sect}"

    def huanglong_signup_record_matches(self, date_text, sect, identity="主魂"):
        records = self.ensure_huanglong_signup_records()
        record = records.get(self.huanglong_signup_record_key(date_text, identity, sect))
        status = ""
        if isinstance(record, dict):
            status = record.get("status", "")
        if not status and identity == "主魂":
            status = self.state.get("huanglong_signup_status", "")
            if self.state.get("huanglong_signup_date") != date_text or self.state.get("huanglong_signup_sect") != sect:
                status = ""
        return status in {
            "pending", "sent", "no_response", "responded", "window_closed", "paused", "send_error",
        }

    def is_huanglong_signup_success_response(self, text):
        clean = self.clean_common_text(text)
        return bool(
            "黄龙" in clean
            and any(marker in clean for marker in (
                "黄龙征调报名成功",
                "身份加入今日",
                "已加入今日",
                "已报名",
                "已经报名",
            ))
        )

    def observed_huanglong_signup_from_ledger(self, date_text, identity):
        """Recover a confirmed manual signup so report backfill never sends it twice."""
        account = actor_account_key(self)
        if not account or not os.path.exists(MESSAGE_EVENTS_DB_FILE):
            return None
        conn = None
        try:
            conn = sqlite3.connect(MESSAGE_EVENTS_DB_FILE, timeout=3)
            rows = conn.execute(
                """
                SELECT ledger.response_msg_id, events.text
                FROM command_ledger AS ledger
                LEFT JOIN message_events AS events
                  ON events.account = ledger.account
                 AND events.msg_id = ledger.response_msg_id
                WHERE ledger.account = ?
                  AND ledger.identity = ?
                  AND ledger.command = ?
                  AND ledger.status = 'matched'
                  AND substr(ledger.sent_at, 1, 10) = ?
                ORDER BY ledger.sent_at DESC, events.id DESC
                LIMIT 50
                """,
                (account, identity, HUANGLONG_SIGNUP_COMMAND, date_text),
            ).fetchall()
        except (OSError, sqlite3.Error):
            return None
        finally:
            if conn is not None:
                conn.close()
        for response_msg_id, response_text in rows:
            if self.is_huanglong_signup_success_response(response_text):
                return {
                    "response_msg_id": response_msg_id,
                    "response": self.clean_common_text(response_text),
                }
        return None

    def record_huanglong_signup_status(self, date_text, sect, identity, msg_id, status, response=""):
        identity = str(identity or "主魂").strip() or "主魂"
        records = self.ensure_huanglong_signup_records()
        record = {
            "date": date_text,
            "sect": sect,
            "identity": identity,
            "report_msg_id": str(msg_id or ""),
            "time": now_str(),
            "status": status,
            "response": self.clean_common_text(response)[:500],
        }
        records[self.huanglong_signup_record_key(date_text, identity, sect)] = record
        self.state["huanglong_signup_date"] = date_text
        self.state["huanglong_signup_sect"] = sect
        self.state["huanglong_signup_report_msg_id"] = str(msg_id or "")
        self.state["huanglong_signup_time"] = record["time"]
        self.state["huanglong_signup_status"] = status
        self.state["huanglong_signup_response"] = record["response"]
        self.save_state()
        return record

    async def maybe_signup_huanglong_now(self, sect, identity="主魂", msg_id=None, now_dt=None, preclaimed=False):
        """匹配本宗门黄龙山军报后报名；任何结果都不盲目重试。"""
        lock = getattr(self, "_huanglong_signup_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_huanglong_signup_lock", lock)

        async with lock:
            self.ensure_common_command_state()
            log = self.common_command_logger()
            now_dt = now_dt or datetime.now()
            today = now_dt.strftime("%Y-%m-%d")
            sect = str(sect or "").strip()
            identity = str(identity or "主魂").strip() or "主魂"
            if not sect:
                return False
            if self.identity_sect_name(identity) != sect:
                return False

            if not preclaimed and self.huanglong_signup_record_matches(today, sect, identity):
                return False

            in_window, window_status = self.huanglong_signup_window_status(now_dt)
            if not in_window:
                if window_status == "window_closed":
                    self.record_huanglong_signup_status(today, sect, identity, msg_id, "window_closed", "报名窗口已过，跳过")
                    log.info(f"Huanglong signup skipped for {identity}/{sect}: window closed.")
                else:
                    log.info(f"Huanglong signup skipped for {identity}/{sect}: signup window not open yet.")
                return False

            if self.identity_pause_seconds(identity) > 0:
                self.record_huanglong_signup_status(today, sect, identity, msg_id, "paused", f"{identity}暂停，跳过")
                log.info(f"Huanglong signup skipped for {identity}/{sect}: identity paused.")
                return False

            self.record_huanglong_signup_status(today, sect, identity, msg_id, "pending", "")

            try:
                log.info(f"Huanglong rotation report matched {identity}/{sect}; sending {HUANGLONG_SIGNUP_COMMAND}.")
                if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
                    resp = await self.send_and_wait_feedback_identity(
                        identity,
                        HUANGLONG_SIGNUP_COMMAND,
                        timeout=90,
                        max_retries=0,
                        suppress_no_response_alert=True,
                        force_identity_check=True,
                    )
                else:
                    resp = await self.send_and_wait_feedback(
                        HUANGLONG_SIGNUP_COMMAND,
                        timeout=90,
                        max_retries=0,
                        suppress_no_response_alert=True,
                    )
                resp_text = self.common_response_text(resp)
                self.record_huanglong_signup_status(
                    today, sect, identity, msg_id, "responded" if resp_text else "no_response", resp_text
                )
                return bool(resp_text)
            except Exception as exc:
                self.record_huanglong_signup_status(today, sect, identity, msg_id, "send_error", str(exc))
                log.error(f"Huanglong signup send failed for {identity}/{sect}: {exc}", exc_info=True)
                return False

    async def maybe_signup_huanglong_identities_now(self, sect, identities, msg_id=None, now_dt=None, preclaimed=False):
        sent = False
        for identity in identities or []:
            if await self.maybe_signup_huanglong_now(
                sect,
                identity=identity,
                msg_id=msg_id,
                now_dt=now_dt,
                preclaimed=preclaimed,
            ):
                sent = True
        return sent

    def maybe_handle_huanglong_report_message(
        self,
        msg,
        text,
        sender=None,
        *,
        now_dt=None,
        source="new message",
    ):
        """被动检测黄龙山轮值军报，本宗门账号在报名窗口内只报名一次。"""
        if sender is not None and not is_game_bot_sender(self, sender):
            return False
        sect = self.parse_huanglong_rotation_sect(text)
        if not sect:
            return False

        self.ensure_common_command_state()
        log = self.common_command_logger()
        msg_id = getattr(msg, "id", "") if msg is not None else ""
        now_dt = now_dt or datetime.now()
        today = now_dt.strftime("%Y-%m-%d")
        identities = self.huanglong_identities_for_sect(sect)
        report_status = (
            "matched"
            if identities
            else "no_matching_identity"
            if self.huanglong_identity_sect_map_complete()
            else "mapping_incomplete"
        )
        self.record_huanglong_rotation_report(
            today,
            sect,
            msg_id,
            source,
            report_status,
            now_dt=now_dt,
        )
        if not identities:
            log.info(f"Huanglong rotation report for {sect} ignored: no matching identity in {self.identity_sect_map()}.")
            return True

        in_window, window_status = self.huanglong_signup_window_status(now_dt)
        if not in_window:
            if window_status == "window_closed":
                for identity in identities:
                    if not self.huanglong_signup_record_matches(today, sect, identity):
                        self.record_huanglong_signup_status(
                            today, sect, identity, msg_id, "window_closed", "报名窗口已过，跳过"
                        )
                log.info(f"Huanglong rotation report for {sect} ignored: signup window closed.")
            else:
                log.info(f"Huanglong rotation report for {sect} ignored: signup window not open yet.")
            return True

        pending_identities = []
        for identity in identities:
            if self.huanglong_signup_record_matches(today, sect, identity):
                continue
            observed = self.observed_huanglong_signup_from_ledger(today, identity)
            if observed:
                self.record_huanglong_signup_status(
                    today,
                    sect,
                    identity,
                    msg_id,
                    "responded",
                    observed["response"],
                )
                log.info(
                    f"Huanglong signup already confirmed for {identity}/{sect} in command ledger; "
                    "state recovered without sending again."
                )
                continue
            self.record_huanglong_signup_status(today, sect, identity, msg_id, "pending", "")
            pending_identities.append(identity)
        if not pending_identities:
            return True

        task = asyncio.create_task(self.maybe_signup_huanglong_identities_now(
            sect,
            pending_identities,
            msg_id=msg_id,
            preclaimed=True,
        ))
        setattr(self, "_huanglong_signup_task", task)
        return True

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
        if self.maybe_handle_huanglong_report_message(msg, text, sender):
            return True
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
            plan = self.field_training_plan("主魂")
            cmd = plan.command
            async with self.common_atomic_task("FieldTraining-主魂"):
                log.info(f"Field training due: sending {cmd}.")
                resp = await self.send_and_wait_feedback(
                    cmd,
                    timeout=plan.timeout,
                    max_retries=plan.max_retries,
                    suppress_no_response_alert=plan.suppress_no_response_alert,
                    return_response_msg=plan.return_response_msg,
                )
                resp = await self.wait_for_field_training_settlement(resp, "主魂")
                resp_text = self.field_training_response_text(resp)
                self.record_field_training_response(resp_text)
            await asyncio.sleep(5)

    async def run_sect_war_loop(self):
        """宗门战主循环：检测宗门战有效期并在可参战时自动参战"""
        self.ensure_common_command_state()
        await self.startup_done.wait()
        initial_wait = random.randint(30, 90)
        in_signup_window, _ = self.huanglong_signup_window_status()
        if in_signup_window:
            initial_wait = random.randint(3, 10)
        else:
            initial_wait = min(initial_wait, self.huanglong_report_loop_wait_seconds())
        await asyncio.sleep(initial_wait)

        while self.is_running:
            # 只检查本地冷却时不切身份；真正发送主魂命令时由 send_and_wait_feedback 对齐。
            self.ensure_common_command_state()
            # NewMessage updates can occasionally be missed during reconnects.
            # During the 12:00-14:00 signup window, recover today's report once
            # from Telegram search; restricted accounts do the same if their
            # full process starts later in the window.
            await self.backfill_recent_huanglong_report(reason="sect war loop")
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

            await asyncio.sleep(self.huanglong_report_loop_wait_seconds())
