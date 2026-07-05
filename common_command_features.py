#!/usr/bin/env python3
"""
【通用固定冷却指令模块 —— 所有账号脚本共享】

提供 CommonCommandMixin 混入类，封装所有账号通用的固定冷却指令：
  1. 野外历练 —— 定时外出历练，策略可配置（谨慎/均衡/深入）
  2. 宗门战况/参战 —— 自动检测宗门战役，参战获取军勋
  3. 固定冷却指令的记录与重试逻辑

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 继承使用。

【阅读导览】
- common_command_default_state：三套脚本都会合并进去的通用 state 字段。
- CommonCommandMixin 前半段：解析、状态读写、日报统计、身份暂停。
- 中段：Dashboard 自定义指令、野外历练、元婴/裂缝/法宝等通用循环。
- 后半段：黄龙山、卜筮问天、宗门战、主循环辅助。

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
import time
from datetime import datetime, timedelta

from log_utils import (
    actor_account_key,
    avatar_marker_identity_from_text,
    dashboard_command_disabled,
    identity_from_single_username_mention,
    identity_plain_usernames,
    is_deep_meditation_ongoing_response,
    is_deep_meditation_settlement_response,
    is_game_bot_sender,
    is_not_deep_meditation_response,
    is_reply_to_untracked_message,
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
    nurture_spirit_plan,
    rift_search_plan,
    treasure_touch_plan,
    YUANYING_OUT_COMMAND,
    YUANYING_RETREAT_COMMAND,
    yuanying_command_for_identity,
    yuanying_out_plan,
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
FIELD_TRAINING_MISSING_RESPONSE_RETRY_SECONDS = 5 * 60  # 空回复短退避，避免 dashboard 长时间显示 0 秒到期
FIELD_TRAINING_SETTLEMENT_WAIT_SECONDS = 15    # 等待野外历练初始回复编辑为结算
BUSHI_WENTIAN_COMMAND = ".卜筮问天"
BUSHI_WENTIAN_EXCHANGE_COMMAND = ".换取"
BUSHI_WENTIAN_DAILY_LIMIT = 8
YUANYING_REBIRTH_PENDING_PAUSE_SECONDS = 30 * 60  # 已可夺舍但未重生时，短暂停自动主魂指令
YUANYING_REBIRTH_WAIT_SECONDS = 365 * 24 * 3600   # 探寻裂缝失败后等待手动 .重生 成功
YUANYING_OUT_CD_SECONDS = 8 * 3600
TREASURE_TOUCH_CD_SECONDS = 2 * 3600
ASK_DAO_CD_SECONDS = 12 * 3600
ASK_DAO_RETRY_SECONDS = 10 * 60
SECT_SKILL_MAX_DAILY = 3
MEDITATION_SETTLEMENT_GRACE_SECONDS = 3 * 60      # 闭关到点后给机器人结算状态留 3 分钟余量
SECT_WAR_STATUS_COMMAND = ".宗门战况"           # 查询宗门战况
SECT_WAR_JOIN_COMMAND = ".参战"                 # 参战指令
SECT_WAR_JOIN_CD_SECONDS = 2 * 3600            # 参战冷却 2 小时
SECT_WAR_RETRY_SECONDS = 10 * 60               # 宗门战重试间隔 10 分钟
HUANGLONG_REPORT_TITLE = "黄龙山轮值军报"
HUANGLONG_SIGNUP_COMMAND = ".报名黄龙山"
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
STALE_STAR_TIME_CRITICAL_KEYS = {
    "next_star_gazing_time",
    "pending_star_gazing_target_time",
    "pending_star_shift_target_time",
}
STALE_STAR_TIME_CRITICAL_GRACE_SECONDS = 180

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
    "next_taiyi_guide_time": ".引道 水",
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
    """返回通用命令的默认状态字典。

    这些键会被合并进每个账号 state。新增字段时要考虑旧 state 迁移：
    旧文件里没有的键应通过 setdefault 补齐，不要直接假设存在。
    """
    return {
        "last_field_training_time": "",
        "next_field_training_time": "",
        "bushi_wentian_date": "",
        "bushi_wentian_count": 0,
        "bushi_wentian_exchange_count": 0,
        "bushi_wentian_kunwu_exchanged": False,
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
        "custom_command_runs": {},
        "identity_pauses": {},
        "star_gazing_assigned_manifest_time": "",
        "star_gazing_assigned_avatar": "",
        "star_gazing_assigned_time": "",
        "daily_reward_events": [],
        "daily_reward_last_sent_date": "",
    }


# =====================================================================
# CommonCommandMixin 混入类
# =====================================================================

class _CommonAtomicTask:
    """共享原子任务门闩。

    多步骤链（如野外历练后的卜筮问天、宗门战、化身任务批次）会短暂占用
    active_atomic_task，普通发送会等待它释放，避免中途被其他循环切身份。
    """

    def __init__(self, actor, label):
        self.actor = actor
        self.label = str(label or "Task")
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
        self.acquired = True
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
            try:
                self.actor.common_command_logger().info(f"Atomic task released by {self.label}.")
            except Exception:
                pass
        return False


class CommonCommandMixin:
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

    def common_atomic_task(self, label):
        return _CommonAtomicTask(self, label)

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
        }

    def daily_reward_account_label(self):
        key = getattr(self, "account_key", "") or ""
        return {
            "main": "主号",
            "sub": "副号",
            "xiaohao": "小号",
        }.get(key, key or self.__class__.__name__)

    def clean_reward_text(self, text):
        return re.sub(r"[ \t]+", " ", str(text or "").replace("**", "").replace("`", "")).strip()

    def daily_reward_edited_settlement_commands(self):
        return {
            ".探寻裂缝",
            ".野外历练",
            ".探渊",
            ".灵兽探渊",
            ".元婴闭关",
            ".元婴出窍",
            ".问道",
        }

    def daily_reward_parse_text_for_command(self, command, text):
        """Return the text segment that should be counted for daily rewards."""
        clean = self.clean_reward_text(text)
        root = str(command or "").split()[0] if command else ""
        if root == ".野外历练":
            return self.daily_reward_field_training_settlement_text(clean)
        if root == ".探寻裂缝":
            return self.daily_reward_rift_settlement_text(clean)
        return clean

    def daily_reward_field_training_settlement_text(self, text):
        clean = self.clean_reward_text(text)
        if not clean:
            return ""

        title = ""
        title_match = re.search(r"【野外历练[^】]{0,30}】", clean)
        if title_match:
            title = title_match.group(0)

        candidate_starts = []
        for pattern in (
            r"@[A-Za-z0-9_]{2,}\s*(?:遭遇|在|采得|发现|误入|寻得|负伤|一时|本已|选择)",
            r"@\S{2,40}\s*遭遇",
            r"战力对比\s*[:：]",
            r"一番斗法后",
            r"本已要负伤折返",
        ):
            match = re.search(pattern, clean)
            if match and (not title_match or match.start() > title_match.end()):
                candidate_starts.append(match.start())

        if not candidate_starts:
            return clean

        start = min(candidate_starts)
        tail = clean[start:].strip()
        if not tail:
            return clean
        if title and title not in tail[:80]:
            return f"{title} {tail}".strip()
        return tail

    def daily_reward_rift_settlement_text(self, text):
        clean = self.clean_reward_text(text)
        if not clean:
            return ""

        candidate_starts = []
        for pattern in (
            r"你的元婴满载而归",
            r"一番斗法后",
            r"获得修为\s*[+＋-]?\d",
            r"【遭遇风暴】",
            r"【不敌败退】",
            r"【大凶·虚空噬体】",
            r"【元婴遁逃·虚弱】",
            r"身受重创",
            r"遭受重创",
            r"虚弱期",
            r"肉身破碎",
            r"肉身化为",
            r"修为倒退",
        ):
            match = re.search(pattern, clean)
            if match:
                candidate_starts.append(match.start())

        if not candidate_starts:
            return clean
        return clean[min(candidate_starts):].strip()

    def parse_reward_items_from_text(self, text):
        """Best-effort parser for command rewards used by the daily summary."""
        clean = self.clean_reward_text(text)
        if not clean:
            return {}
        rewards = {}
        bracket_reward_stop_names = {
            "深度闭关总结", "元婴闭关结算", "元婴归窍总结", "元神归窍总结", "元婴成长",
            "探寻成功", "不敌败退", "遭遇风暴", "激战得胜", "大凶·虚空噬体", "元婴遁逃·虚弱",
            "凌霄云阶", "天门洞开", "周天巡天", "天门余韵", "罡风淬体",
            "天人感应", "推命命中", "改命待发", "天星偏转", "改命回天",
            "战斗加成", "凌霄神通",
        }

        def add(name, amount):
            name = str(name or "").strip(" ：:，,。.;；-+")
            if not name:
                return
            if name in {"x", "X", "本次", "额外", "收益", "奖励", "获得", "收获", "共计"}:
                return
            if name in bracket_reward_stop_names:
                return
            if any(ch in name for ch in "【】[]"):
                return
            try:
                value = int(str(amount).replace(",", ""))
            except Exception:
                return
            if value == 0:
                return
            rewards[name] = int(rewards.get(name, 0)) + value

        for name, amount in re.findall(r"【([^】]{1,30})】\s*[xX*＊]\s*([+-]?\d[\d,]*)", clean):
            add(name, amount)

        for match in re.finditer(r"【([^】]{1,30})】(?!\s*[xX*＊]\s*[+-]?\d)", clean):
            name = match.group(1).strip()
            if not name or name in bracket_reward_stop_names or name.startswith("野外历练"):
                continue
            before = clean[max(0, match.start() - 16):match.start()]
            after = clean[match.end():min(len(clean), match.end() + 24)]
            if "灵兽" in before and re.match(r"\s*(?:成功|击败|出战|休息|已)", after):
                continue
            if re.match(r"\s*(?:因与|，?斗法|照命|成功击败|已助阵|正在|尚需)", after):
                continue
            context_before = clean[max(0, match.start() - 44):match.start()]
            context_after = clean[match.end():min(len(clean), match.end() + 20)]
            before_reward = any(marker in context_before for marker in (
                "为你带来了", "带来了", "带回了", "获得了", "获得", "得到", "收获",
                "发现", "意外之喜", "奖励", "战利品", "至宝", "额外收获",
            ))
            after_reward = any(marker in context_after for marker in ("x", "X", "＊", "*"))
            if before_reward or after_reward:
                add(name, 1)

        for name, amount in re.findall(
            r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·（）()]{0,20})\s*[xX*＊]\s*([+-]?\d[\d,]*)",
            clean,
        ):
            add(name, amount)

        for name, amount in re.findall(
            r"(修为|灵石|宗门贡献|贡献|神识|气血|煞气|道韵|感悟|经验|星辰精华|精华)\s*(?:额外)?\s*(?:增加了?|提升了?|获得了?|得到了?|为|:|：)?\s*\+?\s*([+-]?\d[\d,]*)",
            clean,
        ):
            add(name, amount)

        for name, amount in re.findall(
            r"(修为|灵石|宗门贡献|贡献|神识|气血|煞气|道韵|感悟|经验|星辰精华|精华)\s*(?:减少|降低|扣除|扣了?|损失|折损|倒退了?|消耗)\s*\+?\s*([+-]?\d[\d,]*)",
            clean,
        ):
            try:
                value = -abs(int(str(amount).replace(",", "")))
            except Exception:
                continue
            add(name, value)

        negative_before_amount = ("减少", "降低", "扣除", "扣了", "损失", "折损", "倒退", "消耗")
        for match in re.finditer(
            r"([+-]?\d[\d,]*)\s*(点|枚|份|缕|颗|个)?\s*(修为|灵石|宗门贡献|贡献|神识|气血|煞气|道韵|感悟|经验|星辰精华|精华)",
            clean,
        ):
            prefix = clean[max(0, match.start() - 12):match.start()]
            if any(word in prefix for word in negative_before_amount):
                continue
            amount, _unit, name = match.groups()
            add(name, amount)

        for amount in re.findall(r"修为最终(?:增加|变化)了?\s*\+?\s*([+-]?\d[\d,]*)\s*点", clean):
            add("修为", amount)

        return rewards

    def daily_reward_outcome_from_text(self, command, text, rewards=None):
        root = str(command or "").split()[0] if command else ""
        clean = self.daily_reward_parse_text_for_command(root or command, text)
        rewards = rewards if isinstance(rewards, dict) else {}
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
        if root == ".问道":
            if any(k in clean for k in ("冷却", "后再", "尚需", "剩余", "请在")):
                return False
            return self.is_ask_dao_response(clean)
        return bool(self.parse_reward_items_from_text(clean))

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
            "灵石": 3,
            "神识": 4,
            "气血": 5,
            "煞气": 6,
            "道韵": 7,
            "感悟": 8,
            "经验": 9,
            "星辰精华": 10,
            "精华": 11,
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
        clean = self.clean_reward_text(text)
        if not clean:
            return {}
        rewards = {}

        def add(name, amount):
            try:
                value = int(str(amount).replace(",", ""))
            except Exception:
                return
            if value:
                rewards[name] = int(rewards.get(name, 0) or 0) + value

        command_root = str(command or "").split()[0] if command else ""
        if command_root == ".问道":
            if "大道感悟" in clean or "获得感悟" in clean:
                add("感悟", 1)
            elif "道韵" in clean and not re.search(r"道韵\s*\+?\s*\d", clean):
                add("道韵", 1)

        context_lines = []
        for line in re.split(r"[\n\r]+", clean):
            if any(marker in line for marker in ("推命命中", "司命演算", "天机值")):
                context_lines.append(line)
        context = "\n".join(context_lines)
        if not context:
            return rewards
        for amount in re.findall(r"天机值\s*\+?\s*([+-]?\d[\d,]*)", context):
            add("天机", amount)
        for _name, amount in re.findall(r"(宗门贡献|贡献)\s*\+?\s*([+-]?\d[\d,]*)", context):
            add("贡献", amount)
        return rewards

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
        }.get(root, root.lstrip(".") or "未知")

    def daily_reward_command_counts_compact_text(self, command_counts):
        if not command_counts:
            return ""
        ordered = ["历练", "裂缝", "出窍", "闭关", "探渊", "问道", "登阶", "精华"]
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
            "修为", "天机", "贡献", "神识", "气血", "煞气",
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

        reward_clean = self.daily_reward_parse_text_for_command(root or command, clean)
        rewards = self.parse_reward_items_from_text(reward_clean)
        self.daily_reward_merge_rewards(
            rewards,
            self.daily_reward_context_reward_items(root or command, clean),
        )
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
            rewards = {}
            if raw_text:
                reward_text = self.daily_reward_parse_text_for_command(command, raw_text)
                rewards = self.parse_reward_items_from_text(reward_text)
            if command_root == ".野外历练" and rewards:
                rewards.pop("宗门贡献", None)
            if not rewards and isinstance(event.get("rewards"), dict):
                rewards = dict(event.get("rewards") or {})
                if command_root == ".野外历练":
                    rewards.pop("宗门贡献", None)
            if markdown:
                if command_root == ".野外历练":
                    rewards.pop("宗门贡献", None)
                    rewards.pop("贡献", None)
                for name, value in self.daily_reward_context_reward_items(command, raw_text).items():
                    if int(rewards.get(name, 0) or 0) == 0:
                        rewards[name] = int(value or 0)
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

    def set_identity_rebirth_pending_pause(self, identity, reason="肉体破碎/元婴虚弱，等待 .重生"):
        identity = str(identity or "主魂").strip() or "主魂"
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
        log.info(
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
        stale_changed = False
        now = datetime.now()
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

    async def prepare_identity_for_time_critical_command(self, identity, command=".观星", timeout=20):
        """Pre-switch identity before a narrow-window command without sending the command itself."""
        identity = str(identity or "主魂").strip() or "主魂"
        command = str(command or "").strip() or ".观星"
        log = self.common_command_logger()

        if hasattr(self, "wait_while_identity_paused"):
            if not await self.wait_while_identity_paused(identity, command):
                return False

        while hasattr(self, "should_wait_for_atomic_task") and self.should_wait_for_atomic_task(command):
            await asyncio.sleep(0.5)

        current = getattr(self, "current_identity", "主魂") or "主魂"
        main_confirmed = bool(getattr(self, "_main_confirmed", current == "主魂"))
        if current == identity and (identity != "主魂" or main_confirmed):
            return True

        lock = getattr(self, "avatar_send_lock", None)
        if lock is None or not hasattr(self, "_send_and_wait_feedback_raw"):
            return False

        async with lock:
            current = getattr(self, "current_identity", "主魂") or "主魂"
            main_confirmed = bool(getattr(self, "_main_confirmed", current == "主魂"))
            if current == identity and (identity != "主魂" or main_confirmed):
                return True

            ban_time = (getattr(self, "state", {}) or {}).get("next_switch_allowed_time", "")
            if ban_time and is_future(ban_time):
                log.info(
                    f"Pre-switch for {command} skipped: switch command is cooling until {ban_time}."
                )
                return False

            switch_cmd = f".切换 {identity}"
            log.info(
                f"Pre-switch for {command}: {current} -> {identity} before time-critical send."
            )
            switch_resp = await self._send_and_wait_feedback_raw(
                switch_cmd,
                timeout=timeout,
                max_retries=1,
                suppress_no_response_alert=True,
            )
            resp_text = self.timed_command_response_text(switch_resp)

            if hasattr(self, "check_and_record_switch_ban") and self.check_and_record_switch_ban(resp_text):
                return False
            if not resp_text and hasattr(self, "apply_switch_guard_backoff"):
                if self.apply_switch_guard_backoff(switch_cmd):
                    return False

            passively_confirmed = getattr(self, "current_identity", "") == identity
            confirmed = passively_confirmed or (
                resp_text and any(k in resp_text for k in ["成功", "已切换", "当前操控", identity])
            )
            if not confirmed:
                log.info(
                    f"Pre-switch for {command} to {identity} was not confirmed; "
                    f"response={resp_text[:120]!r}."
                )
                return False

            self.current_identity = identity
            self._main_confirmed = (identity == "主魂")
            log.info(f"Pre-switch for {command}: identity ready as {identity}.")
            return True

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
        kwargs = {
            "timeout": plan.timeout,
            "max_retries": plan.max_retries,
            "force_identity_check": plan.force_identity_check,
            "return_response_msg": plan.return_response_msg,
        }
        if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
            return await self.send_and_wait_feedback_identity(identity, plan.command, **kwargs)
        return await self.send_and_wait_feedback(plan.command, **kwargs)

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
        avatar = str(avatar or "").strip()
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
        plan = self.rift_search_plan(avatar)
        if not self.avatar_timed_command_available(
            avatar,
            plan,
            action_name="rift search",
            require_meditation_ready=require_meditation_ready,
        ):
            return False

        a_state = self.get_avatar_state(avatar)
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
        resp = await self.send_timed_command_plan(plan, avatar)
        resp_text = self.timed_command_response_text(resp)
        if self.is_rift_weakness_response(resp_text):
            await self.stop_for_rift_weakness(resp_text, identity=avatar)
            return True
        self.record_identity_fixed_cd_command_response(
            avatar,
            resp_text,
            plan.command,
            plan.last_key,
            plan.next_key,
            cd_seconds,
        )
        self.save_state()
        return True

    def avatar_yuanying_rift_wait_seconds(self, avatar):
        """Return the next wakeup for avatar yuanying/rift checks."""
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

        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
            state[next_key] = add_seconds_str(now_str(), cd)
            self.save_state()
            log.info(f"{prefix}{command}: cooldown from response {cd}s, next at {state[next_key]}.")
            return False

        if any(k in resp for k in ["冷却", "后再", "尚未", "剩余", "请在"]):
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
        if not any(k in resp for k in success_keywords):
            state[next_key] = add_seconds_str(now, 600)
            self.save_state()
            notify_unrecognized_response(self, command, resp, log, "固定冷却指令")
            log.warning(f"{prefix}{command}: unrecognized response; retry scheduled at {state[next_key]}.")
            return False

        self.record_daily_reward_event(identity, command, resp, source=command)
        state[last_key] = now
        state[next_key] = add_seconds_str(now, cd_seconds)
        self.save_state()
        log.info(f"{prefix}{command}: recorded success/response, next at {state[next_key]}.")
        return True

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
            log.info(f"Yuanying out active. Auto-return due at {end_time}.")
            return wait_time

        if active and not end_time and command == YUANYING_RETREAT_COMMAND:
            log.info("Yuanying retreat active; waiting for passive settlement reply.")
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
        resp_msg = await self.send_timed_command_plan(plan, "主魂")
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

        self.record_fixed_cd_command_response(resp_text, command, plan.last_key, plan.next_key, cd_seconds)
        self.save_state()
        return seconds_until(self.state.get(plan.next_key, "")) or 600

    def ask_dao_cd_seconds(self):
        return int(getattr(self, "ask_dao_cd", ASK_DAO_CD_SECONDS) or ASK_DAO_CD_SECONDS)

    def ask_dao_retry_seconds(self):
        return int(getattr(self, "ask_dao_retry", ASK_DAO_RETRY_SECONDS) or ASK_DAO_RETRY_SECONDS)

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

    def record_ask_dao_response(self, resp, source=None):
        """Record .问道 response; success uses 12h cooldown, cooldown text uses parsed remaining time."""
        source = source or ASK_DAO_COMMAND
        now = now_str()
        plan = self.ask_dao_plan(source)
        log = self.common_command_logger()
        next_key = plan.next_key
        last_key = plan.last_key
        if not resp:
            self.state[next_key] = add_seconds_str(now, self.ask_dao_retry_seconds())
            log.info(f"{source}: no response; retry at {self.state[next_key]}.")
            return False

        cd = self.parse_wait_time(resp)
        if any(k in resp for k in ["冷却", "后再", "尚需", "剩余", "请在"]):
            delay = cd if cd > 0 else self.ask_dao_retry_seconds()
            self.state[next_key] = add_seconds_str(now, delay)
            log.info(f"{source}: cooldown from response {delay}s, next at {self.state[next_key]}.")
            return True

        if any(k in resp for k in ["未加入", "不是元婴宗", "无法问道", "条件不足", "境界不足", "修为不足"]):
            self.state[next_key] = add_seconds_str(now, 60 * 60)
            self.state["last_ask_dao_error"] = resp[:200]
            self.state["last_ask_dao_error_time"] = now
            log.info(f"{source}: unavailable; retry at {self.state[next_key]}.")
            return True

        if self.is_ask_dao_response(resp):
            self.record_daily_reward_event("主魂", plan.command, resp, source=source, final=True)
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, self.ask_dao_cd_seconds())
            self.state["last_ask_dao_error"] = ""
            log.info(f"{source}: recorded response, next at {self.state[next_key]}.")
            return True

        self.state[next_key] = add_seconds_str(now, self.ask_dao_retry_seconds())
        notify_unrecognized_response(self, ASK_DAO_COMMAND, resp, log, source)
        log.info(f"{source}: unrecognized response; retry at {self.state[next_key]}.")
        return False

    async def common_ask_dao_tick(self, command=None):
        """Run one main-soul .问道 scheduling step and return next wait seconds."""
        plan = self.ask_dao_plan(command)
        await self._wait_for_main_identity()
        if self.dashboard_command_paused(plan.command, "主魂"):
            return 300

        next_time = self.state.get(plan.next_key, "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)

        log = self.common_command_logger()
        log.info(f"Ask Dao due: sending {plan.command}.")
        resp = await self.send_timed_command_plan(plan, "主魂")
        if resp is None and await self.sleep_after_blocked_command(plan.command, "Ask Dao"):
            return 0
        self.record_ask_dao_response(self.timed_command_response_text(resp), plan.command)
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

    def record_treasure_touch_response(self, resp, command=None):
        """Parse .抚摸法宝 response and update the shared main-soul cooldown state."""
        plan = self.treasure_touch_plan(command)
        command = plan.command
        next_key = plan.next_key
        last_key = plan.last_key
        log = self.common_command_logger()
        if not resp:
            self.state[next_key] = add_seconds_str(now_str(), 600)
            log.warning(f"{command}: response missing; retry scheduled at {self.state[next_key]}.")
            return False

        cd = self.parse_wait_time(resp)
        if cd > 0 and any(k in resp for k in ["休息", "冷却", "后再", "尚需", "还需", "互动"]):
            self.state[next_key] = add_seconds_str(now_str(), cd)
            log.info(f"{command}: cooldown from response {cd}s, next at {self.state[next_key]}.")
            return False

        if any(k in resp for k in ["联系更加紧密", "器灵传来了喜悦", "默契", "经验", "与它互动", "微微颤动"]):
            now = now_str()
            self.state[last_key] = now
            self.state[next_key] = add_seconds_str(now, self.treasure_touch_cd_seconds())
            log.info(f"{command}: recorded success, next at {self.state[next_key]}.")
            return True

        if (
            "没有这件拥有器灵的法宝" in resp
            or "名字输入错误" in resp
            or ("没有这件" in resp and "器灵" in resp)
        ):
            now = now_str()
            self.state["last_treasure_touch_error"] = resp[:200]
            self.state["last_treasure_touch_error_time"] = now
            self.state[next_key] = add_seconds_str(now, self.treasure_touch_cd_seconds())
            if hasattr(self, "_main_confirmed"):
                self._main_confirmed = False
            log.warning(
                f"{command}: definite failure ({resp[:80]}), next at "
                f"{self.state[next_key]}; main identity will be re-confirmed."
            )
            return False

        self.state[next_key] = add_seconds_str(now_str(), 600)
        notify_unrecognized_response(self, command, resp, log, "抚摸法宝")
        log.warning(f"{command}: unrecognized response; skipped and retry scheduled at {self.state[next_key]}.")
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
        """野外历练已结算的回复，可以触发后置卜筮问天。"""
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
            await self.maybe_run_bushi_wentian_after_field_training(avatar, resp_text)
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

        resp = await self.send_and_wait_feedback_identity(avatar, ".闯塔", timeout=timeout)
        if resp is None:
            log.warning(f"Avatar [{avatar}] tower: switch/send failed. Retrying later.")
            return False
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

        self.set_avatar_state(avatar, "last_tower_date", today)
        log.info(f"Avatar [{avatar}] tower completed for {today}.")
        return True

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
                    log.info(f"Avatar [{avatar}] daily tower due today ({today}). Waiting {delay}s...")
                    await asyncio.sleep(delay)
                    if self.get_avatar_state(avatar).get("last_tower_date", "") != today:
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
        if self.get_avatar_state(avatar).get("last_dianmao_date") == today:
            return False
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(".宗门点卯", avatar):
            return False
        resp = await self.send_and_wait_feedback_identity(avatar, ".宗门点卯", timeout=60)
        resp_text = self.timed_command_response_text(resp)
        if resp_text:
            self.set_avatar_state(avatar, "last_dianmao_date", today)
            return True
        return False

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
                if reset_heart_platform_date and self.state.get("heart_platform_date") != today:
                    self.state["heart_platform_date"] = ""
                self.save_state()

            done = self.state.setdefault("done", [])
            for command in task_commands:
                if command in done:
                    continue

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
                    self.save_state()
                await asyncio.sleep(5)

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

    def common_star_gazing_observer_identity(self, text):
        match = re.search(r"@([A-Za-z0-9_]+)\s+闭目凝神", text or "")
        if not match:
            return ""
        return (getattr(self, "avatar_usernames", {}) or {}).get(match.group(1).lower(), "")

    def common_star_gazing_schedule_plan(self, now, manifest_dt, command_lead_seconds=60):
        """Return (.观星 send time, immediate_shift flag, consumed gazing date)."""
        min_lead_seconds = 60
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
        """Return today's fallback .观星 time when the account still needs one."""
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.star_gazing_sent_on_date(today):
            return None
        if self.state.get("last_star_gazing_fallback_date") == today:
            return None
        if self.star_shift_done_today(today):
            return None
        if self.has_pending_star_gazing_action():
            return None
        fallback_dt = self.daily_star_gazing_fallback_dt(now)
        if now >= fallback_dt + timedelta(minutes=1):
            return None
        return fallback_dt

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
        只有等到编辑结算后，才适合发后置的 .卜筮问天。
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

        log.warning(f"[{identity}] field training settlement was not edited within {timeout_seconds}s; skipping post-training bushi.")
        return resp

    # ---- 卜筮问天 ----

    def _bushi_wentian_state(self, identity="主魂"):
        identity = str(identity or "").strip() or "主魂"
        if identity != "主魂" and hasattr(self, "get_avatar_state"):
            return self.get_avatar_state(identity)
        return self.state

    def ensure_bushi_wentian_state(self, identity="主魂"):
        state = self._bushi_wentian_state(identity)
        today = datetime.now().strftime("%Y-%m-%d")
        changed = False
        if state.get("bushi_wentian_date") != today:
            state["bushi_wentian_date"] = today
            state["bushi_wentian_count"] = 0
            state["bushi_wentian_exchange_count"] = 0
            state["bushi_wentian_kunwu_exchanged"] = False
            changed = True
        else:
            for key, default in (
                ("bushi_wentian_count", 0),
                ("bushi_wentian_exchange_count", 0),
                ("bushi_wentian_kunwu_exchanged", False),
            ):
                if key not in state:
                    if key == "bushi_wentian_kunwu_exchanged":
                        state[key] = int(state.get("bushi_wentian_exchange_count", 0) or 0) > 0
                    else:
                        state[key] = default
                    changed = True
        if changed:
            self.save_state()
        return state

    def bushi_wentian_response_text(self, resp):
        if hasattr(self, "response_text"):
            return self.response_text(resp)
        if hasattr(resp, "text"):
            return resp.text or ""
        if isinstance(resp, str):
            return resp
        return str(resp) if resp else ""

    def is_bushi_wentian_exchange_offer(self, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        return (
            "换取" in clean
            and "回复本消息" in clean
            and "消耗" in clean
            and any(k in clean for k in ["天道示警", "机缘", "逆天之物"])
        )

    def is_bushi_wentian_kunwu_offer(self, text):
        clean = str(text or "").replace("**", "")
        return self.is_bushi_wentian_exchange_offer(clean) and "昆吾通行令" in clean

    def is_bushi_wentian_exchange_success(self, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            return False
        if any(k in clean for k in ("失败", "超时", "消散", "不足", "无法", "没有")):
            return False
        return any(k in clean for k in ("天道认可", "换取成功", "已换取", "收入囊中", "献上祭品"))

    def is_bushi_wentian_daily_limit_response(self, text):
        clean = str(text or "").replace("**", "")
        return (
            "卜筮问天" in clean
            and "今日" in clean
            and any(k in clean for k in ["次数", "上限", "明日", "已用尽"])
        )

    async def maybe_run_bushi_wentian_after_field_training(self, identity="主魂", field_training_text=""):
        """Run .卜筮问天 after a real field-training result, up to the daily limit."""
        if not self.is_field_training_settlement_response(field_training_text):
            return False
        identity = str(identity or "").strip() or "主魂"
        if hasattr(self, "dashboard_command_paused") and self.dashboard_command_paused(BUSHI_WENTIAN_COMMAND, identity):
            return False
        state = self.ensure_bushi_wentian_state(identity)
        if state.get("bushi_wentian_kunwu_exchanged"):
            return False
        current_count = int(state.get("bushi_wentian_count", 0) or 0)
        if current_count >= BUSHI_WENTIAN_DAILY_LIMIT:
            return False
        state["bushi_wentian_count"] = min(BUSHI_WENTIAN_DAILY_LIMIT, current_count + 1)
        self.save_state()

        log = self.common_command_logger()
        log.info(
            f"[{identity}] Bushi Wentian after field training: sending {BUSHI_WENTIAN_COMMAND} "
            f"({state['bushi_wentian_count']}/{BUSHI_WENTIAN_DAILY_LIMIT})."
        )
        if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
            resp_msg = await self.send_and_wait_feedback_identity(
                identity,
                BUSHI_WENTIAN_COMMAND,
                timeout=90,
                max_retries=0,
                force_identity_check=True,
                suppress_no_response_alert=True,
                return_response_msg=True,
            )
        else:
            resp_msg = await self.send_and_wait_feedback(
                BUSHI_WENTIAN_COMMAND,
                timeout=90,
                max_retries=0,
                suppress_no_response_alert=True,
                return_response_msg=True,
            )

        text = self.bushi_wentian_response_text(resp_msg)
        if not text:
            return False

        state = self.ensure_bushi_wentian_state(identity)
        if self.is_bushi_wentian_daily_limit_response(text):
            state["bushi_wentian_count"] = BUSHI_WENTIAN_DAILY_LIMIT
            self.save_state()
            return False

        is_kunwu_offer = self.is_bushi_wentian_kunwu_offer(text)
        if not self.is_bushi_wentian_exchange_offer(text):
            return True

        reply_to = getattr(resp_msg, "id", None)
        if not reply_to:
            log.warning(f"[{identity}] Bushi Wentian exchange offer has no message id; cannot reply .换取.")
            return True

        log.info(f"[{identity}] Bushi Wentian exchange offer detected; replying {BUSHI_WENTIAN_EXCHANGE_COMMAND}.")
        if identity != "主魂" and hasattr(self, "send_and_wait_feedback_identity"):
            exchange_resp = await self.send_and_wait_feedback_identity(
                identity,
                BUSHI_WENTIAN_EXCHANGE_COMMAND,
                reply_to=reply_to,
                timeout=60,
                max_retries=0,
                force_identity_check=True,
                suppress_no_response_alert=True,
            )
        else:
            exchange_resp = await self.send_and_wait_feedback(
                BUSHI_WENTIAN_EXCHANGE_COMMAND,
                reply_to=reply_to,
                timeout=60,
                max_retries=0,
                suppress_no_response_alert=True,
            )
        state = self.ensure_bushi_wentian_state(identity)
        state["bushi_wentian_exchange_count"] = int(state.get("bushi_wentian_exchange_count", 0) or 0) + 1
        exchange_text = self.bushi_wentian_response_text(exchange_resp)
        if is_kunwu_offer and self.is_bushi_wentian_exchange_success(exchange_text):
            state["bushi_wentian_kunwu_exchanged"] = True
        self.save_state()
        return True

    # ---- 宗门战 — 辅助方法 ----

    def account_sect_name(self):
        """获取本账号的宗门名称"""
        return (getattr(self, "sect_name", "") or self.state.get("sect_name", "") or "").strip()

    def identity_sect_name(self, identity="主魂"):
        """获取某个身份所属宗门；黄龙山等身份级活动使用。"""
        identity = str(identity or "主魂").strip() or "主魂"
        mapping = getattr(self, "identity_sect_names", None)
        if not isinstance(mapping, dict):
            mapping = self.state.get("identity_sect_names", {})
        if isinstance(mapping, dict):
            sect = str(mapping.get(identity, "") or "").strip()
            if sect:
                return sect
        if identity == "主魂":
            return self.account_sect_name()
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

    def maybe_handle_huanglong_report_message(self, msg, text, sender=None):
        """被动检测黄龙山轮值军报，本宗门账号在报名窗口内只报名一次。"""
        if sender is not None and not is_game_bot_sender(self, sender):
            return False
        sect = self.parse_huanglong_rotation_sect(text)
        if not sect:
            return False

        self.ensure_common_command_state()
        log = self.common_command_logger()
        msg_id = getattr(msg, "id", "") if msg is not None else ""
        today = datetime.now().strftime("%Y-%m-%d")
        identities = self.huanglong_identities_for_sect(sect)
        if not identities:
            log.info(f"Huanglong rotation report for {sect} ignored: no matching identity in {self.identity_sect_map()}.")
            return True

        in_window, window_status = self.huanglong_signup_window_status()
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
                await self.maybe_run_bushi_wentian_after_field_training("主魂", resp_text)
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
