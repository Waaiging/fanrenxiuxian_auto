#!/usr/bin/env python3
"""
【日志工具模块 —— 所有账号脚本共享】

提供修仙脚本所需的通用工具函数，包括：
  1. 日志过滤与管理 —— 保留关键日志，过滤冗余信息
  2. 命令守卫 —— 防止指令发送过于频繁
  3. 机器人健康检测 —— 监控游戏机器人是否活跃
  4. 反机器人挑战检测 —— 识别并处理 anti-bot 验证
  5. 消息记录与格式化 —— 统一的消息入/出日志格式

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 导入使用。
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone
from collections import deque  # 用于手动指令 ID 的固定大小队列


# =====================================================================
# 常量定义
# =====================================================================
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_RETENTION_HOURS = 7 * 24               # 日志保留 7 天
COMMAND_AUTO_DELETE_SECONDS = 120           # 指令发送后 2 分钟自动删除
CLEAR_HISTORY_OLDER_THAN_MINUTES = 35       # 清屏：只删除 35 分钟以前的点号指令
MAX_COMMAND_RETRIES = 3                     # 最大重试次数
DISABLED_AUTO_COMMANDS = {".召回侍妾"}      # 禁用的自动指令（防止误操作）
COMMAND_CONTROL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "command_controls.json")
MESSAGE_EVENTS_DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "message_events.sqlite3")
USERNAME_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{2,64})")

# 命令守卫参数
COMMAND_GUARD_WINDOW_SECONDS = 30 * 60      # 监控窗口 30 分钟
COMMAND_GUARD_BLOCK_SECONDS = 60 * 60       # 触发守卫后拦截 1 小时
# 发送次数守卫只用于显式标记的失败重试场景；正常业务循环由
# dashboard、身份暂停、机器人健康暂停等更具体的保护负责。
COMMAND_GUARD_DEFAULT_TRACK_SENDS = False
COMMAND_GUARD_POLICY_OVERRIDES = {
    ".协同守山": {                           # 守山按机器人回执保护，正常成功/短冷却不按发送次数拦截
        "limit": 9999,
        "window": 30 * 60,
        "block_seconds": 60 * 60,
        "identity_scoped": False,
        "track_sends": False,
    },
    ".宗门传功": {"limit": 6},               # 宗门传功放宽到 6 次
    ".稳": {"limit": 6},                     # 心劫.稳放宽到 6 次
    ".查看闭关": {                           # 闭关状态查询：允许频繁
        "limit": 8,
        "window": 10 * 60,
        "block_seconds": 5 * 60,
        "alert": False,                      # 不发送告警
    },
    ".闭关修炼": {                           # 闭关修炼：空响应时5min重试，放宽限制
        "limit": 8,
        "window": 30 * 60,
        "block_seconds": 10 * 60,
        "alert": False,
    },
    ".切换": {                              # 化身切换：多化身场景下需要频繁切换
        "limit": 10,
        "window": 30 * 60,
        "block_seconds": 10 * 60,
        "alert": False,
    },
    ".渔具铺": {"track_sends": False, "alert": False},
    ".鱼篓": {"track_sends": False, "alert": False},
    ".买鱼饵": {"track_sends": False, "alert": False},
    ".打窝": {"track_sends": False, "alert": False},
    ".钓鱼": {"track_sends": False, "alert": False},
    ".垂钓": {"track_sends": False, "alert": False},
    ".钓鱼状态": {"track_sends": False, "alert": False},
    ".提竿": {"track_sends": False, "alert": False},
}

PARAM_COMMAND_ROOTS = {
    ".放生",
    ".探渊",
    ".灵兽出战",
    ".灵兽休息",
    ".囚禁魂魄",
    ".安抚幡灵",
}
_COMMAND_CONTROLS_CACHE = {"mtime": None, "data": {}}

# 机器人健康检测参数
BOT_HEALTH_FAILURE_THRESHOLD = 2            # 连续 N 次无响应触发暂停
BOT_HEALTH_WINDOW_SECONDS = 15 * 60         # 监控窗口 15 分钟
BOT_HEALTH_PAUSE_SECONDS = 10 * 60          # 暂停 10 分钟
BOT_ACTIVITY_STALE_SECONDS = 120            # 机器人 2 分钟无活动视为"失联"
BOT_ACTIVITY_POLL_SECONDS = 5               # 轮询间隔
BOT_ACTIVITY_LOG_INTERVAL_SECONDS = 60      # 等待日志间隔
BOT_ACTIVITY_ALERT_INTERVAL_SECONDS = 10 * 60  # 失联告警间隔
BOT_ACTIVITY_HISTORY_SCAN_INTERVAL_SECONDS = 30  # 历史消息扫描间隔
BOT_ACTIVITY_HISTORY_SCAN_LIMIT = 40        # 扫描最近 40 条消息
CLIENT_DISCONNECT_ABORT_SECONDS = 2 * 60    # 本地 Telegram 客户端断线超过 2 分钟就释放发送锁

# 游戏机器人账号列表
DEFAULT_GAME_BOT_USERNAMES = {
    "fanrenxiuxian_bot",           # 原始天尊（主机器人）
    "hantianzunhl",                # 天尊频道号
    "hantianz_bot",                # 天尊1号（1个z）
    "hantianzz_bot",               # 天尊2号（2个z）
    "hantianzzz_bot",              # 天尊3号（3个z）
    "hantianzzzz_bot",             # 天尊4号（4个z）
    "hantianzzzzz_bot",            # 天尊5号（5个z）
    "hantianzzzzzz_bot",           # 天尊6号（6个z）
    "hantianzzzzzzz_bot",          # 天尊7号（7个z）
    "hantianzzzzzzzz_bot",         # 天尊8号（8个z）
}

# 反机器人挑战关键词
ANTI_BOT_KEYWORDS = [
    "挂机嫌疑", "天道审判", "自证", "天道裁决",
    "挂机傀儡", "死株", "死牢",
]

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


# =====================================================================
# 1. 日志过滤与管理
# =====================================================================

class CommandLogFilter(logging.Filter):
    """
    自定义日志过滤器：只保留关键信息到文件日志。
    过滤规则：WARNING 及以上全保留；INFO 只保留指令入/出、同步信息。
    """
    def filter(self, record):
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return True
        return msg.startswith((
            "🟢 OUT", "🔵 IN", "⬆️ OUT", "⬇️ IN",
            "📤 OUT", "📥 IN", "OUT", "IN [",
        ))


# ---- 消息格式化与记录 ----

def _message_id(msg):
    """安全获取消息 ID"""
    return getattr(msg, "id", None)


def sender_display_name(sender=None, msg=None):
    """获取发送者的展示名称（优先使用用户名）"""
    if sender is not None:
        title = (getattr(sender, "title", "") or "").strip()
        first = (getattr(sender, "first_name", "") or "").strip()
        last = (getattr(sender, "last_name", "") or "").strip()
        username = (getattr(sender, "username", "") or "").strip()
        name = title or " ".join(p for p in (first, last) if p).strip() or username
        if name and username and username.lower() not in name.lower():
            return f"{name}(@{username})"
        if name:
            return name
    sender_id = getattr(msg, "sender_id", None) if msg is not None else None
    return f"sender:{sender_id}" if sender_id else "unknown"


def format_in_log(label, text, sender=None, msg=None):
    """格式化收到的消息为日志行"""
    return f"🔵 IN [{label}] {sender_display_name(sender, msg)}:\n{text or ''}"


def command_from_log_label(label):
    raw = str(label or "").strip()
    if raw.startswith("manual "):
        raw = raw[len("manual "):]
        if " reply" in raw:
            raw = raw.split(" reply", 1)[0].strip()
    if raw.startswith("."):
        return raw
    return ""


async def log_incoming_message(actor, label, text, msg=None, sender=None, logger=None, identity=None):
    """记录收到的消息到日志"""
    if sender is None and msg is not None:
        try:
            sender = await msg.get_sender()
        except Exception:
            sender = None
    target_logger = logger or logging.getLogger(actor.__class__.__name__)
    current_id = identity or "主魂"
    if not identity and hasattr(actor, "get_identity_from_msg"):
        current_id = actor.get_identity_from_msg(msg) or getattr(actor, "current_identity", "主魂")
    elif not identity:
        current_id = getattr(actor, "current_identity", "主魂")
    if current_id != "主魂":
        text = f"[Avatar: {current_id}]\n{text or ''}"
    target_logger.info(format_in_log(label, text, sender=sender, msg=msg))
    record_message_event(
        actor,
        msg,
        text=text,
        sender=sender,
        event_kind="new",
        direction="bot_in" if sender is not None and is_game_bot_sender(actor, sender) else "in",
        identity=current_id,
        command=command_from_log_label(label),
        logger=target_logger,
    )
    record_command_response_for_reply(actor, msg, text=text, status="matched", logger=target_logger)
    remember_logged_incoming_message(actor, msg, text=text)
    remember_incoming_message_context(actor, msg, command=command_from_log_label(label), identity=current_id)


# ---- 重试限制 ----

def cap_command_retries(max_retries):
    """限制最大重试次数"""
    try:
        return max(0, min(int(max_retries), MAX_COMMAND_RETRIES))
    except Exception:
        return 0


# =====================================================================
# 2. 命令守卫
# =====================================================================

def command_guard_policy(command, limit, window, block_seconds):
    """获取命令守卫策略（支持指令级别的覆盖配置，前缀匹配）"""
    key = str(command or "").strip()
    # 精确匹配优先，否则尝试前缀匹配（如 ".切换" 匹配 ".切换 主魂 (问心子)"）
    policy = COMMAND_GUARD_POLICY_OVERRIDES.get(key, {})
    if not policy:
        for override_key, override_policy in COMMAND_GUARD_POLICY_OVERRIDES.items():
            if key.startswith(override_key):
                policy = override_policy
                break
    return (
        int(policy.get("limit", limit)),
        int(policy.get("window", window)),
        int(policy.get("block_seconds", block_seconds)),
        bool(policy.get("alert", True)),
        bool(policy.get("identity_scoped", True)),
        bool(policy.get("track_sends", COMMAND_GUARD_DEFAULT_TRACK_SENDS)),
    )


def command_guard_key(command, identity="主魂", limit=MAX_COMMAND_RETRIES,
                      window=COMMAND_GUARD_WINDOW_SECONDS,
                      block_seconds=COMMAND_GUARD_BLOCK_SECONDS):
    """Return the in-memory rate-limit key and policy for a command."""
    key = str(command or "").strip()
    current_id = str(identity or "主魂").strip() or "主魂"
    limit, window, block_seconds, should_alert, identity_scoped, track_sends = command_guard_policy(
        key, limit, window, block_seconds
    )
    guard_key = f"{key} ({current_id})" if identity_scoped and current_id != "主魂" else key
    return guard_key, limit, window, block_seconds, should_alert, track_sends


def remember_command_guard_block(actor, key, wait, blocked_until=0, reason="command_guard", identity=None):
    """Record the latest local command-guard block for callers that need to back off."""
    try:
        setattr(actor, "_last_command_guard_block", {
            "key": str(key or ""),
            "wait": max(0, int(wait or 0)),
            "blocked_until": float(blocked_until or 0),
            "reason": reason,
            "identity": str(identity or ""),
            "at": time.monotonic(),
        })
    except Exception:
        pass


def force_command_guard_block(actor, command, wait, logger=None, identity=None,
                              reason="response_error", alert=False, reason_text=""):
    """Set a command-guard block from domain-specific response parsing."""
    try:
        wait = max(0, int(wait or 0))
    except Exception:
        wait = 0
    if wait <= 0:
        return
    guard_key, limit, window, block_seconds, _should_alert, _track_sends = command_guard_key(
        command, identity or getattr(actor, "current_identity", "主魂")
    )
    now = time.monotonic()
    guard = getattr(actor, "_command_send_guard", None)
    if guard is None:
        guard = {}
        setattr(actor, "_command_send_guard", guard)
    entry = guard.get(guard_key, {"times": [], "blocked_until": 0, "last_warn": 0})
    entry["blocked_until"] = max(float(entry.get("blocked_until", 0) or 0), now + wait)
    entry["last_warn"] = now
    guard[guard_key] = entry
    remember_command_guard_block(actor, guard_key, wait, entry["blocked_until"], reason=reason, identity=identity)
    if logger:
        logger.warning(f"Command guard forced block [{guard_key}] for {wait}s ({reason}).")
    if alert:
        _notify_command_guard_blocked(
            actor, guard_key, limit, window, wait, logger, reason_text=reason_text
        )


def clear_command_guard_block(actor, command, logger=None, identity=None, reason=""):
    """Clear a response-driven command-guard block when new valid state arrives."""
    guard_key, _limit, _window, _block_seconds, _should_alert, _track_sends = command_guard_key(
        command, identity or getattr(actor, "current_identity", "主魂")
    )
    guard = getattr(actor, "_command_send_guard", None)
    if not isinstance(guard, dict) or guard_key not in guard:
        return
    entry = guard.get(guard_key) or {}
    if entry.get("blocked_until", 0):
        entry["blocked_until"] = 0
        entry["last_warn"] = 0
        guard[guard_key] = entry
        if logger:
            suffix = f" ({reason})" if reason else ""
            logger.info(f"Command guard cleared [{guard_key}]{suffix}.")


def command_allowed_during_identity_pause(command):
    """Recovery/admin commands that may be sent while an identity is paused."""
    key = str(command or "").strip()
    if not key:
        return True
    return (
        key.startswith(".自证")
        or key.startswith(".切换")
        or key.startswith(".夺舍重生")
        or key.startswith(".重生")
        or key in {".止", ".启"}
    )


def is_yuanying_rebirth_success_response(text):
    """Return True when text confirms a dead/weak Yuanying has been reborn."""
    clean = str(text or "").replace("**", "").replace("`", "").strip()
    compact = re.sub(r"\s+", "", clean)
    if not compact:
        return False
    if any(k in compact for k in ["无法夺舍", "无法进行夺舍", "尚在虚弱", "虚弱期"]):
        return False
    return (
        "已成功夺舍重生" in compact
        or "夺舍重生成功" in compact
        or ("元婴" in compact and "夺舍" in compact and "重生" in compact and "成功" in compact)
    )


def is_yuanying_rebirth_block_response(text):
    """Return True for command replies that mean the identity still needs rebirth."""
    clean = str(text or "").replace("**", "").replace("`", "").strip()
    compact = re.sub(r"\s+", "", clean)
    if not compact or is_yuanying_rebirth_success_response(compact):
        return False
    if "元婴" not in compact and "残婴" not in compact:
        return False
    return (
        "元婴欲散" in compact
        or "元婴离体" in compact
        or "元婴欲夺舍" in compact
        or "残婴飘荡" in compact
        or ("虚弱的元婴" in compact and "夺舍" in compact)
        or ("灵气滞涩" in compact and "元婴" in compact)
        or ("灵气稀薄" in compact and "夺舍" in compact)
        or ("神识飘散" in compact and "夺舍" in compact)
        or ("欲夺舍重生" in compact and ("元婴" in compact or "残婴" in compact))
    )


# =====================================================================
# 3. 游戏机器人检测
# =====================================================================

def get_game_bot_usernames(actor):
    """获取所有游戏机器人的用户名列表（默认 + 配置自定义）"""
    mc = getattr(actor, "mc", {}) or {}
    names = set(DEFAULT_GAME_BOT_USERNAMES)
    watch_bot = mc.get("watch_bot") or getattr(actor, "watch_bot", "")
    if watch_bot:
        names.add(str(watch_bot).lower().lstrip("@"))
    for value in mc.get("watch_bots", []) or []:
        if value:
            names.add(str(value).lower().lstrip("@"))
    return names


def is_game_bot_sender(actor, sender):
    """判断消息是否来自游戏机器人"""
    username = (getattr(sender, "username", "") or "").lower().lstrip("@")
    return bool(username and username in get_game_bot_usernames(actor))


# =====================================================================
# 4. 反机器人挑战检测
# =====================================================================

def is_anti_bot_challenge_text(text):
    """检测文本是否包含反机器人挑战关键词"""
    return bool(text and any(k in text for k in ANTI_BOT_KEYWORDS))


def account_aliases(actor):
    """获取本账号的所有用户名别名（用于匹配消息是否提到本账号）"""
    aliases = []
    me = getattr(actor, "my_info", None)
    if me:
        aliases.extend([
            getattr(me, "username", "") or "",
            getattr(me, "first_name", "") or "",
            getattr(me, "last_name", "") or "",
        ])
    mc = getattr(actor, "mc", {}) or {}
    aliases.extend(mc.get("account_aliases", []) or [])
    return [str(v).lower().lstrip("@").strip() for v in aliases if str(v or "").strip()]


def text_username_mentions(text):
    """Extract plain @username mentions from message text."""
    return [m.group(1).lower() for m in USERNAME_MENTION_RE.finditer(text or "")]


def avatar_marker_identity_from_text(text):
    """Extract `[Avatar: name]` from bot replies, if present."""
    match = re.search(r"\[Avatar:\s*([^\]\r\n]+)\]", str(text or ""))
    return match.group(1).strip() if match else ""


def text_matches_feedback_identity(actor, text, identity):
    """Return whether a bot reply is compatible with the pending command identity."""
    marker = avatar_marker_identity_from_text(text)
    if not marker:
        return True
    expected = str(identity or "").strip() or getattr(actor, "current_identity", "主魂")
    return marker == expected


def identity_plain_usernames(actor, identity=None):
    """Known game/TG usernames that belong to one execution identity."""
    expected = str(identity or "").strip() or getattr(actor, "current_identity", "主魂") or "主魂"
    known = set()
    try:
        mapping = _identity_profile_usernames(actor)
        known.update(mapping.get(expected, set()) or set())
    except Exception:
        pass
    if expected == "主魂":
        known.update(account_aliases(actor))
    return {_normalize_account_name(v) for v in known if _normalize_account_name(v)}


def identity_from_single_username_mention(actor, text):
    """Infer identity only when exactly one @username maps to a managed identity."""
    mentions = {_normalize_account_name(v) for v in text_username_mentions(text or "")}
    mentions = {v for v in mentions if v}
    if len(mentions) != 1:
        return ""
    mention = next(iter(mentions))
    matches = []
    for identity, usernames in _identity_profile_usernames(actor).items():
        if mention in (usernames or set()):
            matches.append(identity)
    return matches[0] if len(matches) == 1 else ""


def mentions_other_user_for_identity(actor, msg, text=None, identity=None):
    """Like mentions_other_user, but account for the pending avatar's game username."""
    text_value = text if text is not None else getattr(msg, "text", "")
    marker = avatar_marker_identity_from_text(text_value)
    if marker:
        expected = str(identity or "").strip() or getattr(actor, "current_identity", "主魂")
        return marker != expected

    mentions = [_normalize_account_name(v) for v in text_username_mentions(text_value)]
    mentions = [v for v in mentions if v]
    if mentions:
        known = identity_plain_usernames(actor, identity)
        if known:
            mentions_identity = any(v in known for v in mentions)
            mentions_other_identity = any(v not in known for v in mentions)
            if mentions_identity:
                return False
            if mentions_other_identity:
                return True
    return mentions_other_user(actor, msg, text_value)


def text_mentions_other_account(actor, text):
    """Return True when text explicitly @mentions another username and not this account."""
    mentions = text_username_mentions(text)
    if not mentions:
        return False
    aliases = set(account_aliases(actor))
    if not aliases:
        return True
    mentions_self_name = any(name in aliases for name in mentions)
    mentions_other_name = any(name not in aliases for name in mentions)
    return mentions_other_name and not mentions_self_name


def command_response_family(command):
    """Return the expected response family for a command, when it is known."""
    cmd = str(command or "").strip()
    if cmd.startswith(".切换 "):
        return "switch"
    if cmd.startswith(".野外历练"):
        return "field_training"
    if cmd == ".入梦寻图":
        return "dream_map"
    if cmd in {".共历心劫", ".坠魔心劫", ".稳", ".狠", ".骗"}:
        return "heart_trial"
    if cmd in {".启阵", ".助阵"}:
        return "formation"
    if cmd.startswith(".抚摸法宝"):
        return "treasure_touch"
    if cmd in {".观星台", ".安抚星辰", ".收集精华"} or cmd.startswith(".牵引星辰") or cmd.startswith(".掌天瓶"):
        return "star"
    if cmd in {".天阶状态", ".登天阶", ".引九天罡风", ".问心台"}:
        return "cloud_stairs"
    if cmd == ".探寻裂缝":
        return "rift"
    if cmd in {".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关"}:
        return "meditation"
    if cmd == ".状态":
        return "status"
    if cmd == ".我的灵根":
        return "spirit_root"
    if cmd == ".我的侍妾":
        return "concubine_status"
    if cmd.startswith(".侍妾远航") or cmd == ".远航归来":
        return "concubine_voyage"
    if cmd == ".天机代卜":
        return "divination"
    if cmd == ".卜筮问天":
        return "bushi_wentian"
    if cmd == ".换取":
        return "bushi_exchange"
    if cmd == ".宗门传功":
        return "sect_skill"
    if cmd in {".灵树灌溉", ".灵树状态", ".采摘灵果", ".协同守山"}:
        return "spirit_tree"
    if (
        cmd == ".我的灵兽"
        or cmd == ".寻觅灵兽"
        or cmd == ".一键放养"
        or cmd.startswith(".灵兽")
        or cmd.startswith(".探渊")
        or cmd.startswith(".放生 ")
    ):
        return "beast"
    if cmd in {".宗门点卯", ".闯塔"}:
        return cmd
    if cmd in {".元婴出窍", ".元婴闭关"}:
        return ".元婴出窍"
    if cmd in {".渔具铺", ".鱼篓", ".钓鱼状态"} or cmd.startswith(".买鱼饵") or cmd.startswith(".钓鱼") or cmd.startswith(".垂钓") or cmd.startswith(".打窝") or cmd == ".提竿":
        return "fishing"
    if (
        cmd in {".我的阴罗幡", ".升级阴罗幡", ".每日献祭", ".血洗山林", ".召唤魔影", ".一键收取精华", ".一键收取"}
        or cmd.startswith(".囚禁魂魄")
        or cmd.startswith(".安抚幡灵")
        or cmd.startswith(".化功为煞")
    ):
        return "yinluo"
    return ""


def text_response_family(text):
    """Classify a bot response by strong content markers."""
    clean = str(text or "").replace("**", "")
    if not clean:
        return ""
    if any(k in clean for k in ["切换成功", "神念已附着", "神念重归主魂", "当前操控"]):
        return "switch"
    if "【野外历练" in clean or ("战力对比" in clean and "妖兽" in clean):
        return "field_training"
    if (
        "【入梦寻图】" in clean
        or "梦兆锁定" in clean
        or "残纹" in clean
        or "残图线路" in clean
        or "梦图感应" in clean
        or "共梦寻图" in clean
        or "同梦寻图" in clean
        or ("仍在远航" in clean and "寻图" in clean)
    ):
        return "dream_map"
    if "坠魔心劫" in clean:
        return "heart_trial"
    if "炼制" in clean or "材料不足" in clean or "缺少：" in clean or "缺少:" in clean:
        return "crafting"
    if any(k in clean for k in [
        "周天星斗大阵", "布设大阵", "启阵冷却", "再次启阵",
        "心神消耗", "参与过布阵", "同门相助", "正在布阵",
        "已在阵中", "请勿重复操作", "没有找到正在召集的大阵", "阵法已过期",
    ]):
        return "formation"
    if "【星宫 · 观星台】" in clean or "引星盘" in clean or "星光黯淡" in clean or "元磁紊乱" in clean or "狂暴星力" in clean:
        return "star"
    if any(k in clean for k in [
        "凌霄云阶", "云阶进度", "当前云阶进度", "登阶冷却",
        "踏上了第", "罡风淬体", "九天罡风", "问心台",
        "云阶禁制不会为你显现", "你并非凌霄宫弟子",
    ]):
        return "cloud_stairs"
    if any(k in clean for k in ["裂缝", "时空异兽", "不敌败退", "身受重创", "元婴险些崩溃", "元婴遁逃"]):
        return "rift"
    if any(k in clean for k in [
        "深度闭关", "闭关修炼", "闭关成功", "闭关失败", "预计还需",
        "未处于深度闭关", "并未处于深度闭关", "灵气尚未平复", "打坐调息",
        "强行中断了神魂神游", "强行出关惩罚", "中断修行", "清点所得",
    ]):
        return "meditation"
    if "器灵" in clean or "本命法宝" in clean or "青竹蜂云剑" in clean:
        return "treasure_touch"
    if "修士状态" in clean and "境界" in clean:
        return "status"
    if "天命玉牒" in clean and "修为" in clean:
        return "spirit_root"
    if (
        "道心侍妾" in clean
        or "【第二期机缘】" in clean
        or "掩月心契" in clean
        or "入梦寻图冷却" in clean
        or "共历心劫冷却" in clean
        or "天机代卜冷却" in clean
        or "侍妾远航冷却" in clean
        or "远航冷却" in clean
    ):
        return "concubine_status"
    if any(k in clean for k in [
        "侍妾远航", "远航归来", "远航", "启航", "返航", "航程", "航海", "冒险",
        "心神未定", "情缘值", "未随行", "无法出航", "无法远航",
    ]):
        return "concubine_voyage"
    if any(k in clean for k in ["卜筮问天", "神物现世", "天道示警"]):
        return "bushi_wentian"
    if "换取" in clean and any(k in clean for k in ["天道认可", "获取此等逆天之物", "机缘"]):
        return "bushi_exchange"
    if "卦象" in clean:
        return "divination"
    if "天机代卜" in clean or "天机链路" in clean:
        return "divination"
    if "宗门传功" in clean or "传功玉简" in clean:
        return "sect_skill"
    if any(k in clean for k in ["灵树", "灵果", "采摘期", "成熟采摘期", "造化青莲果", "协同守山", "古剑门", "护山大阵"]):
        return "spirit_tree"
    if any(k in clean for k in [
        "灵兽", "万兽渊", "万兽谷", "探渊", "偷菜", "菜园",
        "出战", "放养", "寻觅灵兽", "兽栏", "巡游", "亲密", "抚摸",
        "安抚", "心情", "羁绊", "忠诚", "体力不足", "入渊", "六翼",
    ]):
        return "beast"
    if "宗门点卯" in clean:
        return ".宗门点卯"
    if any(k in clean for k in ["闯塔", "塔钥", "试炼古塔", "重置古塔", "道心受挫", "挑战失败"]):
        return ".闯塔"
    if any(k in clean for k in ["元婴出窍", "元婴闭关", "元神回响", "元神归窍总结", "元婴归窍总结", "元婴闭关结算"]):
        return ".元婴出窍"
    if any(k in clean for k in [
        "灵溪垂钓", "鱼篓", "渔具铺", "青竹钓竿", "钓术", "鱼讯",
        "提竿成功", "空竿", "打窝已成", "打窝失败", "窝料已经用尽",
        "已打下", "还可影响", "不可重复叠加",
        "你挂上", "抛竿入水", "今日已垂钓", "鱼获已入鱼篓",
    ]):
        return "fishing"
    if any(k in clean for k in [
        "阴罗幡", "阴罗宗", "阴罗本幡", "血煞幡", "炼化槽",
        "每日献祭", "九幽煞气", "血洗功成", "血洗山林",
        "魔影", "魔域裂隙", "召唤成功", "镇压成功",
        "囚禁魂魄", "被强行打入", "煞气不足", "化功为煞",
        "幡魂谱系精进",
    ]):
        return "yinluo"
    return ""


def feedback_response_matches_command(command, text):
    """Return True when text positively looks like a response for command."""
    expected = command_response_family(command)
    clean = str(text or "").replace("**", "")
    if not expected or not clean:
        return False
    if is_passive_settlement_response(clean):
        return True
    if is_yuanying_rebirth_block_response(clean) or is_yuanying_rebirth_success_response(clean):
        return True

    if expected == "switch":
        cmd = str(command or "").strip()
        target = cmd.replace(".切换", "", 1).strip()
        if target == "主魂":
            return "神念重归主魂" in clean or ("主魂" in clean and any(k in clean for k in ["成功", "已切换", "当前操控"]))
        return bool(target and target in clean and any(k in clean for k in ["切换成功", "神念已附着", "已切换", "当前操控"]))
    if expected == "field_training":
        return "野外历练" in clean or "山中灵机未复" in clean or ("卦象" in clean and "修为增加" in clean)
    if expected == "dream_map":
        return any(k in clean for k in [
            "入梦寻图", "梦兆", "残图", "寻图冷却", "梦图感应", "共梦寻图",
            "同梦寻图",
        ]) or ("仍在远航" in clean and "寻图" in clean)
    if expected == "heart_trial":
        return any(k in clean for k in ["共历心劫", "坠魔心劫", "心劫", "侍妾/道侣", "道侣内容"])
    if expected == "formation":
        return any(k in clean for k in [
            "周天星斗大阵", "布设大阵", "启阵冷却",
            "再次启阵", "心神消耗", "参与过布阵", "助阵",
            "同门相助", "60秒", "正在布阵", "已在阵中",
            "请勿重复操作", "没有找到正在召集的大阵", "阵法已过期",
        ]) or ("修为不足" in clean and any(k in clean for k in ["启阵", "布阵", "阵法"]))
    if expected == "treasure_touch":
        if any(k in clean for k in [
            "闭关成功", "闭关失败", "深度闭关", "闭关修炼",
            "当前境界", "当前修为", "清点所得", "走火入魔",
        ]):
            return False
        return any(k in clean for k in [
            "器灵", "本命法宝", "青竹蜂云剑", "默契", "经验", "互动", "别摸啦",
            "拥有器灵", "名字输入错误", "没有这件",
        ])
    if expected == "star":
        return any(k in clean for k in ["观星台", "引星盘", "牵引星辰", "安抚星辰", "狂暴星力", "星光", "精华"]) or (
            "修为不足" in clean and any(k in clean for k in ["牵引", "星辰"])
        )
    if expected == "cloud_stairs":
        return any(k in clean for k in [
            "凌霄云阶", "云阶进度", "当前云阶进度", "登阶冷却",
            "踏上了第", "罡风淬体", "九天罡风", "问心台",
            "云阶禁制不会为你显现", "你并非凌霄宫弟子",
            "可立即登阶", "未再聚", "后再试",
        ])
    if expected == "rift":
        return any(k in clean for k in [
            "裂缝", "探寻成功", "法则碎片", "法则本源", "元婴遁逃",
            "时空异兽", "不敌败退", "身受重创", "元婴险些崩溃",
        ])
    if expected == "meditation":
        return any(k in clean for k in [
            "深度闭关", "闭关修炼", "闭关成功", "闭关失败", "预计还需",
            "未处于深度闭关", "并未处于深度闭关", "灵气尚未平复",
            "打坐调息", "当前修为", "强行中断了神魂神游",
            "强行出关惩罚", "中断修行", "清点所得",
        ])
    if expected == "status":
        return "修士状态" in clean and "境界" in clean
    if expected == "spirit_root":
        return "天命玉牒" in clean and "修为" in clean
    if expected == "concubine_status":
        return any(k in clean for k in [
            "道心侍妾", "【第二期机缘】", "掩月心契",
            "入梦寻图冷却", "共历心劫冷却", "天机代卜冷却",
            "侍妾远航冷却", "远航冷却",
        ])
    if expected == "concubine_voyage":
        return any(k in clean for k in [
            "侍妾远航", "远航归来", "远航", "归来", "启航", "返航", "冒险",
            "航程", "航海", "带回", "收获", "均衡", "尚无侍妾", "还没有侍妾",
            "心神未定", "情缘值", "未随行", "无法出航", "无法远航",
        ])
    if expected == "divination":
        return any(k in clean for k in ["天机代卜", "天机链路", "卜算", "代卜", "卦象"])
    if expected == "bushi_wentian":
        return any(k in clean for k in [
            "卜筮问天", "神物现世", "天道示警", "天机罗盘", "卦象显示",
            "回复本消息", "换取", "今日次数", "次数已用尽",
        ])
    if expected == "bushi_exchange":
        return any(k in clean for k in [
            "换取成功", "天道认可", "获取此等逆天之物", "机缘消散",
            "材料不足", "超时", "来确认", "献上祭品", "祭品", "收入囊中",
        ]) or ("换取" in clean and any(k in clean for k in ["获得", "消耗", "机缘"]))
    if expected == "sect_skill":
        return any(k in clean for k in ["宗门传功", "传功玉简", "元神", "传功"])
    if expected == "spirit_tree":
        return any(k in clean for k in [
            "灵树", "灵果", "采摘期", "成熟采摘期", "造化青莲果",
            "灵眼之树", "成熟度", "灌溉", "采摘", "修为增长",
            "协同守山", "守山", "古剑门", "护山大阵", "加固",
        ])
    if expected == "beast":
        return any(k in clean for k in [
            "灵兽", "万兽渊", "万兽谷", "探渊", "偷菜", "菜园",
            "出战", "放养", "寻觅灵兽", "兽栏", "无法立刻出战",
            "尚未派遣任何灵兽出战", "巡游", "亲密", "抚摸",
            "安抚", "心情", "羁绊", "忠诚", "体力不足", "入渊", "六翼",
        ])
    if expected == ".宗门点卯":
        return "宗门点卯" in clean or "点卯" in clean
    if expected == ".闯塔":
        return (
            "闯塔" in clean
            or "塔钥" in clean
            or "试炼古塔" in clean
            or "重置古塔" in clean
            or "道心受挫" in clean
            or "挑战失败" in clean
            or ("通关" in clean and "层" in clean)
        )
    if expected == ".元婴出窍":
        return any(k in clean for k in [
            "元婴出窍", "元婴闭关", "神游", "云游", "出窍", "自动结算",
            "尚未凝聚元婴", "无法施展此术", "元神回响",
            "元神归窍总结", "元婴归窍总结", "元婴闭关结算",
        ])
    if expected == "fishing":
        return any(k in clean for k in [
            "灵溪垂钓", "鱼篓", "渔具铺", "青竹钓竿", "钓术", "鱼讯",
            "提竿成功", "空竿", "打窝已成", "打窝失败", "窝料已经用尽",
            "已打下", "还可影响", "不可重复叠加",
            "你挂上", "抛竿入水", "今日已垂钓", "鱼获已入鱼篓",
            "购得 【", "鱼篓中没有", "已有一竿尚未收起", "尚无【青竹钓竿】",
        ])
    if expected == "yinluo":
        return any(k in clean for k in [
            "阴罗幡", "阴罗宗", "阴罗本幡", "血煞幡", "炼化槽",
            "每日献祭", "九幽煞气", "今日已献祭",
            "血洗功成", "血洗山林", "山林的生灵尚未恢复",
            "魔影", "魔域裂隙", "召唤成功", "镇压成功",
            "被强行打入", "煞气不足", "魂魄袋中没有",
            "化功为煞", "转化成功", "开始运转魔功",
            "安抚成功", "收取成功", "幡魂谱系精进",
            "升级成功", "缺少材料",
        ])
    return False


def feedback_response_requires_positive_match(command):
    """Known command families should not accept exact replies with unrelated text."""
    return bool(command_response_family(command))


def feedback_response_conflicts(command, text):
    """Return True when response text clearly belongs to a different command."""
    expected = command_response_family(command)
    actual = text_response_family(text)
    return bool(expected and actual and expected != actual)


def text_targets_current_account(actor, msg, text):
    """判断消息文本是否提到了本账号"""
    if text_mentions_other_account(actor, text):
        return False
    if mentions_self(actor, msg, text):
        return True
    if mentions_other_user(actor, msg, text):
        return False
    lower_text = (text or "").lower()
    for name in account_aliases(actor):
        if f"@{name}" in lower_text or f"【{name}】" in lower_text:
            return True
        if re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", lower_text):
            return True
    return False


def _normalize_account_name(value):
    """标准化账号名称（去格式、去空白）"""
    value = str(value or "").lower().strip()
    value = value.replace("**", "").replace("`", "")
    value = value.strip("@ \\t\\r\\n【】[]（）()：:,，。.!！")
    return re.sub(r"\s+", "", value)


def _name_matches_current_account(actor, value):
    """判断一个名称是否匹配本账号"""
    target = _normalize_account_name(value)
    if not target:
        return False
    for name in account_aliases(actor):
        alias = _normalize_account_name(name)
        if not alias:
            continue
        if target == alias or alias in target:
            return True
    return False


def _anti_bot_explicit_targets(text):
    """从反机器人挑战消息中提取明确的目标用户"""
    clean = (text or "").replace("**", "")
    targets = []
    for pattern in (
        r"对象\s*【([^】]+)】",
        r"对象\s*(@[a-zA-Z0-9_]+)",
        r"道友\s*【([^】]+)】\s*[，,]",
        r"道友\s*(@[a-zA-Z0-9_]+)\s*[，,]",
    ):
        targets.extend(m.group(1).strip() for m in re.finditer(pattern, clean))
    return [t for t in targets if t]


def _anti_bot_reporter_matches_current_account(actor, text):
    """检测反机器人消息中的举报人是否为本账号"""
    clean = (text or "").replace("**", "")
    reporter_patterns = (
        r"你被\s*【([^】]+)】\s*举报",
        r"【([^】]+)】\s*检举有功",
        r"你被\s*(@[a-zA-Z0-9_]+)\s*举报",
        r"(@[a-zA-Z0-9_]+)\s*检举有功",
    )
    for pattern in reporter_patterns:
        for match in re.finditer(pattern, clean):
            if _name_matches_current_account(actor, match.group(1)):
                return True
    return False


def anti_bot_targets_current_account(actor, msg, text):
    """综合判断反机器人挑战是否针对本账号"""
    targets = _anti_bot_explicit_targets(text)
    if targets:
        return any(_name_matches_current_account(actor, target) for target in targets)
    if _anti_bot_reporter_matches_current_account(actor, text):
        return False
    return text_targets_current_account(actor, msg, text)


HAN_SOUL_CHOICE_COMMAND = ".献上魂魄"


def is_han_soul_choice_prompt(text):
    """Detect Han Tianzun's soul-choice prompt that requires replying to the prompt message."""
    clean = str(text or "").replace("**", "").replace("`", "")
    if not clean:
        return False
    return (
        "神魂" in clean
        and "180" in clean
        and "分钟" in clean
        and "回复本消息" in clean
        and ".献上魂魄" in clean
        and ".收敛气息" in clean
        and any(k in clean for k in ["无法抗拒", "意志锁定", "做出抉择"])
    )


def han_soul_choice_target_identity(actor, msg, text):
    """Return the managed identity explicitly targeted by Han Tianzun's soul-choice prompt."""
    if not is_han_soul_choice_prompt(text):
        return ""

    mapping = _identity_profile_usernames(actor)
    mentions = [_normalize_account_name(v) for v in text_username_mentions(text)]
    mentions = [v for v in mentions if v]
    for mention in mentions:
        for identity, usernames in mapping.items():
            if mention in (usernames or set()):
                return identity

    marker = avatar_marker_identity_from_text(text)
    if marker and (marker == "主魂" or marker in (getattr(actor, "avatars", []) or [])):
        return marker

    if mentions_self(actor, msg, text) or text_targets_current_account(actor, msg, text):
        return "主魂"
    return ""


async def maybe_handle_han_soul_choice(actor, msg, text, sender=None, logger=None):
    """Automatically choose the high-risk soul option by replying .献上魂魄 with the targeted identity."""
    identity = han_soul_choice_target_identity(actor, msg, text)
    if not identity:
        return False
    if not claim_message_version(actor, msg, text, purpose=f"han_soul_choice:{identity}", ttl=4 * 3600):
        return True

    reply_to = getattr(msg, "id", None)
    send_identity = getattr(actor, "send_and_wait_feedback_identity", None)
    if not reply_to or not callable(send_identity):
        if logger:
            logger.warning(f"Han soul choice detected for {identity}, but no reply sender is available.")
        return False

    if logger:
        logger.info(f"Han soul choice detected for {identity}; replying {HAN_SOUL_CHOICE_COMMAND} to msg {reply_to}.")
    await send_identity(
        identity,
        HAN_SOUL_CHOICE_COMMAND,
        reply_to=reply_to,
        timeout=30,
        max_retries=0,
        suppress_no_response_alert=True,
    )
    return True


# ---- 告警发送 ----

async def send_text_alert(actor, title, text, logger=None):
    """发送告警消息给用户（优先使用 bot token，降级到客户端）"""
    alert_text = f"【{title}】\n{text}"
    config = getattr(actor, "config", {}) or {}
    target = config.get("notify_target", "Waaiging")
    sent = False

    bot_token = config.get("notify_bot_token")
    if bot_token:
        try:
            final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
            data = json.dumps({"chat_id": final_target, "text": alert_text}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=data, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                sent = True
        except Exception as exc:
            if logger:
                logger.error(f"Alert bot send failed, falling back to client: {exc}")

    if not sent:
        client = getattr(actor, "client", None)
        if client:
            try:
                final_target = target
                if isinstance(target, str) and not target.startswith("@") and not target.lstrip("-").isdigit():
                    final_target = f"@{target}"
                await client.send_message(final_target, alert_text)
                sent = True
            except Exception as exc:
                if logger:
                    logger.error(f"Alert client send failed: {exc}")
    return sent


def _chat_matches_actor_target(actor, msg):
    chat_id = getattr(msg, "chat_id", None)
    target_chat_id = getattr(actor, "target_chat_id", None)
    if chat_id is None or target_chat_id is None:
        return False
    if chat_id == target_chat_id:
        return True
    try:
        return int(chat_id) == int(f"-100{target_chat_id}")
    except Exception:
        return False


def _message_in_topic(msg, topic_id):
    if not topic_id:
        return True
    reply_to = getattr(msg, "reply_to", None)
    candidates = [
        getattr(msg, "reply_to_msg_id", None),
        getattr(msg, "reply_to_top_id", None),
    ]
    if reply_to is not None:
        candidates.extend([
            getattr(reply_to, "reply_to_msg_id", None),
            getattr(reply_to, "reply_to_top_id", None),
        ])
    return topic_id in candidates


def is_clear_history_command(actor, msg, text, sender=None):
    """Return True for the admin-only clear-screen command: plain `c`."""
    if str(text or "").strip().lower() != "c":
        return False
    if sender is not None and is_game_bot_sender(actor, sender):
        return False
    if not _chat_matches_actor_target(actor, msg):
        return False
    sender_id = getattr(msg, "sender_id", None)
    admins = getattr(actor, "pause_admins", set()) or set()
    return bool(sender_id and sender_id in admins)


async def clear_actor_command_history(actor, older_than_minutes=CLEAR_HISTORY_OLDER_THAN_MINUTES, scan_limit=None, topic_only=False, logger=None):
    """Delete this account's old outgoing dot-commands from its configured game chat."""
    client = getattr(actor, "client", None)
    chat_id = getattr(actor, "target_chat_id", None)
    if not client or chat_id is None:
        raise RuntimeError("missing client or target chat")

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0, int(older_than_minutes)))
    scanned = outgoing = game_commands = old_enough = in_topic = selected = deleted = 0
    batch = []
    me = await client.get_me()

    async for msg in client.iter_messages(chat_id, limit=scan_limit, from_user=me):
        scanned += 1
        if not getattr(msg, "out", False):
            continue
        outgoing += 1

        body = (getattr(msg, "raw_text", None) or getattr(msg, "text", None) or "").strip()
        if not body.startswith("."):
            continue
        game_commands += 1

        msg_date = getattr(msg, "date", None)
        if msg_date is None:
            continue
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
        if msg_date > cutoff:
            continue
        old_enough += 1

        msg_in_topic = _message_in_topic(msg, getattr(actor, "topic_id", None))
        if msg_in_topic:
            in_topic += 1
        if topic_only and not msg_in_topic:
            continue

        selected += 1
        batch.append(msg.id)
        if len(batch) >= 100:
            await client.delete_messages(chat_id, batch, revoke=True)
            deleted += len(batch)
            batch.clear()

    if batch:
        await client.delete_messages(chat_id, batch, revoke=True)
        deleted += len(batch)

    if logger:
        logger.info(
            f"Clear history complete: scanned={scanned}, outgoing={outgoing}, "
            f"commands={game_commands}, old={old_enough}, selected={selected}, deleted={deleted}."
        )
    return {
        "scanned": scanned,
        "outgoing": outgoing,
        "game_commands": game_commands,
        "old_enough": old_enough,
        "in_topic": in_topic,
        "selected": selected,
        "deleted": deleted,
    }


def clear_history_account_label(actor):
    key = getattr(actor, "account_key", "")
    return {
        "main": "凌霄宫（主号）",
        "sub": "元婴宗（副号）",
        "xiaohao": "万灵宗（小号）",
    }.get(key, key or actor.__class__.__name__)


async def _run_clear_history_command(actor, label, logger=None):
    try:
        result = await clear_actor_command_history(actor, logger=logger)
        msg = (
            f"{label}清屏完成。\n"
            f"仅处理 {CLEAR_HISTORY_OLDER_THAN_MINUTES} 分钟以前的 . 开头游戏指令。\n"
            f"扫描自己消息={result['scanned']}，自己发言={result['outgoing']}，"
            f"游戏指令={result['game_commands']}，超过阈值={result['old_enough']}，"
            f"选中={result['selected']}，已删除={result['deleted']}。"
        )
        await send_text_alert(actor, "清屏完成", msg, logger)
    except Exception as exc:
        if logger:
            logger.exception(f"Clear history command failed: {exc}")
        await send_text_alert(actor, "清屏失败", f"{label}清屏失败：{exc}", logger)


async def handle_clear_history_command(actor, msg, text, sender=None, logger=None):
    """Handle admin `c` command and start an account-local clear-history job."""
    if not is_clear_history_command(actor, msg, text, sender):
        return False

    client = getattr(actor, "client", None)
    chat_id = getattr(actor, "target_chat_id", None)
    if client and chat_id is not None:
        try:
            await client.delete_messages(chat_id, msg)
        except Exception:
            pass

    label = clear_history_account_label(actor)
    existing = getattr(actor, "_clear_history_task", None)
    if existing and not existing.done():
        await send_text_alert(actor, "清屏进行中", f"{label}已有清屏任务在运行，请稍等。", logger)
        return True

    task = asyncio.create_task(_run_clear_history_command(actor, label, logger))
    setattr(actor, "_clear_history_task", task)
    if logger:
        logger.info(f"Clear history command accepted for {label}.")
    return True


async def handle_anti_bot_challenge(actor, msg, text, sender, logger=None, title="自证告警"):
    """
    处理反机器人挑战的入口函数。
    如果确认目标为本账号：发送告警并停止脚本（防止继续操作被判定为挂机）。
    """
    if not is_anti_bot_challenge_text(text):
        return False
    if not is_game_bot_sender(actor, sender):
        if logger:
            logger.info("Anti-bot-like text ignored: sender is not a configured game bot.")
        return True
    if not anti_bot_targets_current_account(actor, msg, text):
        if logger:
            logger.info(f"Anti-bot challenge ignored: not for this account:\n{text}")
        return True

    await send_text_alert(actor, title, text, logger)
    if logger:
        logger.critical(f"Anti-bot challenge targets this account. Stopping script:\n{text}")
    setattr(actor, "is_running", False)
    return True


# =====================================================================
# 5. 机器人活跃度检测
# =====================================================================

def record_game_bot_activity(actor, sender=None, logger=None):
    """记录游戏机器人有活动"""
    now = time.monotonic()
    setattr(actor, "_last_game_bot_activity_ts", now)
    setattr(actor, "_last_game_bot_activity_wall", datetime.now().strftime(TIME_FORMAT))
    setattr(actor, "_bot_no_response_times", [])
    if getattr(actor, "_bot_unhealthy_until", 0):
        setattr(actor, "_bot_unhealthy_until", 0)
        if logger:
            username = (getattr(sender, "username", "") or "").lstrip("@")
            suffix = f" from @{username}" if username else ""
            logger.warning(f"Bot activity resumed{suffix}; command sending unlocked.")


def bot_activity_age(actor):
    """获取当前机器人沉默时长（秒）"""
    ts = getattr(actor, "_last_game_bot_activity_ts", None)
    if not ts:
        return None
    return max(0, time.monotonic() - ts)


def is_bot_activity_recent(actor, stale_seconds=BOT_ACTIVITY_STALE_SECONDS):
    """判断机器人最近是否有活动"""
    age = bot_activity_age(actor)
    return age is not None and age <= stale_seconds


async def refresh_recent_game_bot_activity(actor, stale_seconds=BOT_ACTIVITY_STALE_SECONDS, logger=None):
    """
    通过扫描最近消息刷新机器人活跃度状态。
    当检测到机器人消息时，更新活跃度时间戳。
    """
    now = time.monotonic()
    last_scan = getattr(actor, "_bot_activity_history_scan_last", 0) or 0
    if now - last_scan < BOT_ACTIVITY_HISTORY_SCAN_INTERVAL_SECONDS:
        return is_bot_activity_recent(actor, stale_seconds)
    setattr(actor, "_bot_activity_history_scan_last", now)

    client = getattr(actor, "client", None)
    chat_id = getattr(actor, "target_chat_id", None)
    if not client or chat_id is None:
        return False

    try:
        messages = await client.get_messages(chat_id, limit=BOT_ACTIVITY_HISTORY_SCAN_LIMIT)
        wall_now = time.time()
        for msg in messages:
            msg_date = getattr(msg, "date", None)
            if msg_date and wall_now - msg_date.timestamp() > stale_seconds:
                continue
            sender = await msg.get_sender()
            if is_game_bot_sender(actor, sender):
                record_game_bot_activity(actor, sender, logger)
                return True
    except Exception as exc:
        if logger:
            logger.warning(f"Bot activity history scan failed: {exc}")
    return False


async def ensure_client_connected_before_send(actor, command="", logger=None):
    client = getattr(actor, "client", None)
    if not client or not hasattr(client, "is_connected"):
        return True
    try:
        if client.is_connected():
            return True
    except Exception:
        return True

    now = time.monotonic()
    last_attempt = getattr(actor, "_client_reconnect_attempt_last", 0) or 0
    if now - last_attempt < BOT_ACTIVITY_POLL_SECONDS:
        return False
    setattr(actor, "_client_reconnect_attempt_last", now)

    if logger:
        logger.warning(f"Telegram client disconnected before [{command}]; attempting reconnect.")
    try:
        await client.connect()
        if hasattr(client, "is_user_authorized") and not await client.is_user_authorized():
            if logger:
                logger.error("Telegram client reconnect failed: session is not authorized.")
            return False
        if client.is_connected():
            if logger:
                logger.info("Telegram client reconnect succeeded.")
            return True
    except Exception as exc:
        if logger:
            logger.error(f"Telegram client reconnect failed before [{command}]: {exc}")
    return False


def is_bot_health_paused(actor):
    """判断是否因机器人不健康而暂停"""
    until = getattr(actor, "_bot_unhealthy_until", 0) or 0
    return until > time.monotonic()


def bot_health_pause_remaining(actor):
    """获取暂停剩余时间"""
    until = getattr(actor, "_bot_unhealthy_until", 0) or 0
    return max(0, int(until - time.monotonic()))


def record_bot_response(actor):
    """记录机器人有响应，解除暂停"""
    setattr(actor, "_last_game_bot_activity_ts", time.monotonic())
    setattr(actor, "_last_game_bot_activity_wall", datetime.now().strftime(TIME_FORMAT))
    setattr(actor, "_bot_no_response_times", [])
    if getattr(actor, "_bot_unhealthy_until", 0):
        setattr(actor, "_bot_unhealthy_until", 0)


async def wait_for_bot_activity_before_send(actor, command, logger=None, stale_seconds=BOT_ACTIVITY_STALE_SECONDS):
    """
    发送指令前等待机器人活跃。
    如果机器人沉默超过 stale_seconds，先尝试扫描最近消息，
    确认机器人确实失联后循环等待（直到机器人重新活跃或超时）。
    .自证 和 .强行出关 不受此限制（紧急指令）。
    """
    key = str(command or "").strip()
    if key in {".自证", ".强行出关"}:
        return True

    disconnected_since = 0
    while getattr(actor, "is_running", True):
        if not await ensure_client_connected_before_send(actor, key, logger):
            now = time.monotonic()
            if not disconnected_since:
                disconnected_since = now
            if now - disconnected_since >= CLIENT_DISCONNECT_ABORT_SECONDS:
                if logger:
                    logger.error(
                        f"Telegram client remained disconnected for "
                        f"{int(now - disconnected_since)}s before [{key}]; aborting send."
                    )
                return False
            await asyncio.sleep(BOT_ACTIVITY_POLL_SECONDS)
            continue
        disconnected_since = 0

        if not is_bot_activity_recent(actor, stale_seconds):
            await refresh_recent_game_bot_activity(actor, stale_seconds, logger)
        if is_bot_activity_recent(actor, stale_seconds):
            if is_bot_health_paused(actor):
                record_bot_response(actor)
            return True

        age = bot_activity_age(actor)
        now = time.monotonic()
        last_log = getattr(actor, "_bot_activity_wait_log_last", 0) or 0
        if logger and now - last_log >= BOT_ACTIVITY_LOG_INTERVAL_SECONDS:
            if age is None:
                logger.info(f"Waiting for bot activity before [{key}]...")
            else:
                logger.info(f"Waiting for bot activity [{int(age)}s stale] before [{key}]...")
            setattr(actor, "_bot_activity_wait_log_last", now)

        if age is not None and age > BOT_ACTIVITY_ALERT_INTERVAL_SECONDS:
            last_alert = getattr(actor, "_bot_activity_stale_alert_last", 0) or 0
            if now - last_alert >= BOT_ACTIVITY_ALERT_INTERVAL_SECONDS:
                setattr(actor, "_bot_activity_stale_alert_last", now)
                config = getattr(actor, "config", {}) or {}
                target = config.get("notify_target", "Waaiging")
                info = getattr(actor, "my_info", None)
                account = (
                    getattr(info, "first_name", None)
                    or getattr(info, "username", None)
                    or actor.__class__.__name__
                )
                alert_text = (
                    "【机器人失联提醒】\\n"
                    f"账号：{account}\\n"
                    f"尝试发送指令：{key}\\n"
                    f"已等待 {int(age // 60)} 分钟，仍在等待..."
                )
                if logger:
                    logger.warning(alert_text)
                bot_token = config.get("notify_bot_token")
                if bot_token:
                    try:
                        final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
                        data = json.dumps({"chat_id": final_target, "text": alert_text}).encode("utf-8")
                        req = urllib.request.Request(
                            f"https://api.telegram.org/bot{bot_token}/sendMessage",
                            data=data, headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(req, timeout=5):
                            pass
                    except Exception:
                        pass

        await asyncio.sleep(BOT_ACTIVITY_POLL_SECONDS)
    return False


# =====================================================================
# 6. 机器人健康检测 — 无响应记录与暂停
# =====================================================================

def _notify_bot_health_paused(actor, command, pause_seconds, logger=None, status=None):
    """当机器人连续无响应达到阈值时，发送暂停告警给用户"""
    now = time.monotonic()
    last = getattr(actor, "_bot_health_alert_last", 0) or 0
    if now - last < pause_seconds:
        return
    setattr(actor, "_bot_health_alert_last", now)

    info = getattr(actor, "my_info", None)
    account = (
        getattr(info, "first_name", None)
        or getattr(info, "username", None)
        or actor.__class__.__name__
    )
    text = (
        "【机器人无响应提醒】\\n"
        f"账号：{account}\\n"
        f"触发指令：{command}\\n"
        f"状态：{status or f'连续未收到机器人回复，自动暂停发送 {int(pause_seconds // 60)} 分钟。'}"
    )

    config = getattr(actor, "config", {}) or {}
    target = config.get("notify_target", "Waaiging")
    bot_token = config.get("notify_bot_token")
    sent = False
    if bot_token:
        try:
            final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
            data = json.dumps({"chat_id": final_target, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=data, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                sent = True
        except Exception as exc:
            if logger:
                logger.error(f"Bot health alert bot send failed: {exc}")
    if not sent:
        client = getattr(actor, "client", None)
        if client:
            try:
                final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
                loop = asyncio.get_running_loop()
                loop.create_task(client.send_message(final_target, text))
            except Exception as exc:
                if logger:
                    logger.error(f"Bot health alert client send failed: {exc}")
    if sent and logger:
        logger.warning(f"Bot health alert sent after no response for [{command}].")


def record_bot_no_response(actor, command, logger=None):
    """
    记录一次机器人无响应事件。
    在 BOT_HEALTH_WINDOW_SECONDS 内超过 BOT_HEALTH_FAILURE_THRESHOLD 次，
    则触发暂停（BOT_HEALTH_PAUSE_SECONDS 内不再发送指令）。
    """
    now = time.monotonic()
    times = getattr(actor, "_bot_no_response_times", None)
    if times is None:
        times = []
    times = [ts for ts in times if now - ts <= BOT_HEALTH_WINDOW_SECONDS]
    times.append(now)
    setattr(actor, "_bot_no_response_times", times)

    if len(times) >= BOT_HEALTH_FAILURE_THRESHOLD:
        setattr(actor, "_bot_unhealthy_until", now + BOT_HEALTH_PAUSE_SECONDS)
        setattr(actor, "_bot_no_response_times", [])
        if logger:
            logger.warning(
                f"Bot health paused after {len(times)} no-response commands; "
                f"holding sends for {BOT_HEALTH_PAUSE_SECONDS}s."
            )
        _notify_bot_health_paused(actor, command, BOT_HEALTH_PAUSE_SECONDS, logger)


# =====================================================================
# 7. 深度闭关状态判断
# =====================================================================

def is_not_deep_meditation_response(text):
    """判断回复是否表示"未处于深度闭关"状态"""
    clean = str(text or "").replace("**", "").replace(" ", "")
    return any(k in clean for k in [
        "未处于深度闭关", "并未处于深度闭关", "你并未处于深度闭关之中",
        "未在深度闭关", "未进行深度闭关", "不在深度闭关",
        "并未", "未处于", "未在", "空闲",
    ])


def is_deep_meditation_settlement_response(text):
    """判断回复是否表示深度闭关已结算"""
    if is_not_deep_meditation_response(text):
        return False
    clean = str(text or "").replace("**", "")
    compact = clean.replace(" ", "")
    if any(k in compact for k in [
        "正在深度闭关",
        "预计还需",
        "还需",
        "已进入深度闭关状态",
        "已在深度闭关",
        "神魂将自行吐纳",
    ]):
        return False
    if any(k in compact for k in [
        "深度闭关总结",
        "闭关总结",
        "闭关结算",
        "闭关结束",
    ]):
        return True
    if "功成圆满" in clean and any(k in clean for k in ["神魂", "归位", "闭关"]):
        return True
    if "神魂" in clean and "归位" in clean and any(k in clean for k in ["功成圆满", "闭关", "总结"]):
        return True
    if "清点所得" in clean and any(k in clean for k in ["闭关", "神魂", "修为"]):
        return True
    return False


def is_yuanying_out_settlement_response(text):
    """判断回复是否表示元婴/元神到期归窍结算。"""
    clean = str(text or "").replace("**", "")
    compact = clean.replace(" ", "")
    if any(k in compact for k in ["尚未凝聚元婴", "无法施展此术"]):
        return False
    if any(k in clean for k in ["元神归窍总结", "元婴归窍总结", "元神回响", "元婴闭关结算"]):
        return True
    return (
        "元婴" in clean
        and any(k in clean for k in ["神游归来", "清点收获", "归窍总结", "带回了以下收获"])
    )


def is_passive_settlement_response(text):
    """任何指令都可能被到期结算截断，统一识别这类被动结算。"""
    return is_yuanying_out_settlement_response(text) or is_deep_meditation_settlement_response(text)


def is_deep_meditation_ongoing_response(text):
    """判断回复是否表示深度闭关进行中"""
    if is_not_deep_meditation_response(text):
        return False
    clean = str(text or "").replace("**", "")
    compact = clean.replace(" ", "")
    if any(k in clean for k in ["总结", "归位", "结算", "最终变化"]):
        return False
    return any(k in compact for k in [
        "正在深度闭关",
        "已在深度闭关",
        "已进入深度闭关状态",
        "预计还需",
    ])


# =====================================================================
# 8. 命令守卫
# =====================================================================

def _notify_command_guard_blocked(actor, command, limit, window, block_seconds, logger=None, reason_text=""):
    """当命令守卫拦截指令时，发送告警给用户"""
    now = time.monotonic()
    cache = getattr(actor, "_command_guard_alert_cache", None)
    if cache is None:
        cache = {}
        setattr(actor, "_command_guard_alert_cache", cache)
    last_alert = cache.get(command, 0)
    if now - last_alert < min(block_seconds, 3600):
        return
    cache[command] = now

    config = getattr(actor, "config", {}) or {}
    target = config.get("notify_target", "Waaiging")
    bot_token = config.get("notify_bot_token")
    info = getattr(actor, "my_info", None)
    account = (
        getattr(info, "first_name", None)
        or getattr(info, "username", None)
        or actor.__class__.__name__
    )
    reason_line = reason_text or (
        f"{int(window // 60)} 分钟内已发送 {limit} 次，"
        f"已暂停该命令 {int(block_seconds // 60)} 分钟。"
    )
    text = (
        "【命令保护提醒】\\n"
        f"账号：{account}\\n"
        f"命令：{command}\\n"
        f"原因：{reason_line}"
    )
    sent = False
    if bot_token:
        try:
            final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
            data = json.dumps({"chat_id": final_target, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=data, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                sent = True
        except Exception as exc:
            if logger:
                logger.error(f"Command guard alert bot send failed: {exc}")
    if not sent:
        client = getattr(actor, "client", None)
        if client:
            try:
                final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
                loop = asyncio.get_running_loop()
                loop.create_task(client.send_message(final_target, text))
            except Exception as exc:
                if logger:
                    logger.error(f"Command guard alert client send failed: {exc}")
    if sent and logger:
        logger.warning(f"Command guard alert sent for [{command}].")


def notify_unrecognized_response(actor, command, response, logger=None, context=""):
    """当业务逻辑无法识别机器人回复时，发送告警（30分钟内同类型只告警一次）"""
    key = f"{command}|{context}"
    now = time.monotonic()
    cache = getattr(actor, "_unrecognized_response_alert_cache", None)
    if cache is None:
        cache = {}
        setattr(actor, "_unrecognized_response_alert_cache", cache)
    last_alert = cache.get(key, 0)
    if now - last_alert < 30 * 60:
        return
    cache[key] = now

    config = getattr(actor, "config", {}) or {}
    target = config.get("notify_target", "Waaiging")
    bot_token = config.get("notify_bot_token")
    info = getattr(actor, "my_info", None)
    account = (
        getattr(info, "first_name", None)
        or getattr(info, "username", None)
        or actor.__class__.__name__
    )
    response_text = (response or "").strip()
    if len(response_text) > 1600:
        response_text = response_text[:1600] + "..."
    text = (
        "【无法识别回复】\\n"
        f"账号：{account}\\n"
        f"指令：{command}\\n"
        f"位置：{context or '未标注'}\\n"
        "已跳过该指令，继续执行其他任务。\\n\\n"
        f"机器人回复：\\n{response_text or '(空回复/超时)'}"
    )
    sent = False
    if bot_token:
        try:
            final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
            data = json.dumps({"chat_id": final_target, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=data, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5):
                sent = True
        except Exception as exc:
            if logger:
                logger.error(f"Unrecognized response alert bot send failed: {exc}")
    if not sent:
        client = getattr(actor, "client", None)
        if client:
            try:
                final_target = int(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
                loop = asyncio.get_running_loop()
                loop.create_task(client.send_message(final_target, text))
            except Exception as exc:
                if logger:
                    logger.error(f"Unrecognized response alert client send failed: {exc}")
    if sent and logger:
        logger.warning(f"Unrecognized response alert sent for [{command}] ({context}).")


def command_root(command):
    text = str(command or "").strip()
    if not text:
        return ""
    return text.split()[0]


def command_control_key(command):
    """标准化 dashboard 指令开关 key。"""
    text = re.sub(r"\s+", " ", str(command or "").strip())
    if not text:
        return ""
    if "<" in text:
        root = text.split("<", 1)[0].strip()
        return f"{root} *" if root else text
    if " / " in text:
        text = text.split(" / ", 1)[0].strip()
    root = command_root(text)
    if root in PARAM_COMMAND_ROOTS:
        return f"{root} *"
    if root == ".灵兽互动":
        parts = text.split()
        if len(parts) >= 2:
            return f"{root} {parts[1]}"
    return text


def command_control_candidate_keys(command):
    text = re.sub(r"\s+", " ", str(command or "").strip())
    if not text:
        return []
    root = command_root(text)
    keys = [text, command_control_key(text)]
    if root in PARAM_COMMAND_ROOTS:
        keys.append(f"{root} *")
    if root == ".灵兽互动":
        parts = text.split()
        if len(parts) >= 2:
            keys.append(f"{root} {parts[1]}")
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(k for k in keys if k))


def load_command_controls():
    """读取 dashboard 的指令开关文件，按 mtime 缓存。"""
    path = COMMAND_CONTROL_FILE
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        _COMMAND_CONTROLS_CACHE["mtime"] = None
        _COMMAND_CONTROLS_CACHE["data"] = {}
        return {}
    except Exception:
        return _COMMAND_CONTROLS_CACHE.get("data") or {}

    mtime = stat.st_mtime
    if _COMMAND_CONTROLS_CACHE.get("mtime") == mtime:
        return _COMMAND_CONTROLS_CACHE.get("data") or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _COMMAND_CONTROLS_CACHE["mtime"] = mtime
    _COMMAND_CONTROLS_CACHE["data"] = data
    return data


def actor_account_key(actor):
    state_file = os.path.basename(str(getattr(actor, "state_file", "") or ""))
    if state_file == "state_main.json":
        return "main"
    if state_file == "state_sub.json":
        return "sub"
    if state_file == "state_xiaohao.json":
        return "xiaohao"
    return str(getattr(actor, "account_key", "") or "")


def command_control_entry_disabled(entry):
    if isinstance(entry, dict):
        return bool(entry.get("disabled"))
    return bool(entry)


def dashboard_command_disabled(actor, command, identity=None):
    """判断 dashboard 是否临时暂停了该账号/身份/指令。"""
    account = actor_account_key(actor)
    if not account:
        return False, "", None
    controls = load_command_controls().get(account, {})
    if not isinstance(controls, dict):
        return False, "", None
    identities = [str(identity or getattr(actor, "current_identity", "主魂") or "主魂"), "*"]
    keys = command_control_candidate_keys(command)
    for ident in identities:
        ident_controls = controls.get(ident, {})
        if not isinstance(ident_controls, dict):
            continue
        for key in keys:
            entry = ident_controls.get(key)
            if command_control_entry_disabled(entry):
                return True, key, entry
    return False, "", None


def command_send_precheck(actor, command, logger=None, identity=None,
                          limit=MAX_COMMAND_RETRIES,
                          window=COMMAND_GUARD_WINDOW_SECONDS,
                          block_seconds=COMMAND_GUARD_BLOCK_SECONDS):
    """Check whether a command would be allowed without recording a send."""
    key = str(command or "").strip()
    if not key:
        return True
    if key in DISABLED_AUTO_COMMANDS:
        if logger:
            logger.info(f"Command [{key}] is disabled by local policy; skipping pre-switch.")
        remember_command_guard_block(actor, key, 0, reason="disabled")
        return False

    current_id = str(identity or "").strip()
    if not current_id:
        if hasattr(actor, "get_identity_from_msg"):
            current_id = actor.get_identity_from_msg(None) or getattr(actor, "current_identity", "主魂")
        else:
            current_id = getattr(actor, "current_identity", "主魂")
    current_id = current_id or "主魂"

    disabled, disabled_key, _disabled_entry = dashboard_command_disabled(actor, key, current_id)
    if disabled:
        now_for_log = time.monotonic()
        cache = getattr(actor, "_dashboard_disabled_precheck_log_cache", None)
        if cache is None:
            cache = {}
            setattr(actor, "_dashboard_disabled_precheck_log_cache", cache)
        log_key = f"{current_id}\u001f{key}"
        if logger and now_for_log - cache.get(log_key, 0) > 300:
            logger.info(
                f"Command [{key}] for [{current_id}] is paused by dashboard; "
                f"skip identity switch (control={disabled_key})."
            )
            cache[log_key] = now_for_log
        remember_command_guard_block(actor, key, 300, reason="dashboard_disabled", identity=current_id)
        return False

    if (
        not command_allowed_during_identity_pause(key)
        and hasattr(actor, "identity_pause_seconds")
    ):
        try:
            pause_wait = int(actor.identity_pause_seconds(current_id))
        except Exception:
            pause_wait = 0
        if pause_wait > 0:
            if logger:
                logger.info(
                    f"Identity [{current_id}] paused; skip pre-switch for [{key}], "
                    f"retry after {pause_wait}s."
                )
            remember_command_guard_block(actor, key, pause_wait, reason="identity_pause", identity=current_id)
            return False

    guard_key, limit, window, block_seconds, _should_alert, track_sends = command_guard_key(
        key, current_id, limit, window, block_seconds
    )

    if not guard_key.startswith(".自证") and is_bot_health_paused(actor):
        wait = bot_health_pause_remaining(actor)
        if logger:
            logger.warning(f"Bot health paused; skip pre-switch for [{guard_key}], retry after {wait}s.")
        remember_command_guard_block(actor, guard_key, wait, reason="bot_health")
        return False

    now = time.monotonic()
    guard = getattr(actor, "_command_send_guard", None) or {}
    entry = guard.get(guard_key, {"times": [], "blocked_until": 0, "last_warn": 0})
    blocked_until = entry.get("blocked_until", 0)
    if blocked_until > now:
        wait = int(blocked_until - now)
        if logger and now - entry.get("last_warn", 0) > 60:
            logger.warning(f"Command guard would block [{guard_key}], skip identity switch; retry after {wait}s.")
            entry["last_warn"] = now
            guard[guard_key] = entry
            setattr(actor, "_command_send_guard", guard)
        remember_command_guard_block(actor, guard_key, wait, blocked_until, reason="command_guard")
        return False

    if not track_sends:
        return True

    times = [ts for ts in entry.get("times", []) if now - ts <= window]
    if len(times) >= limit:
        entry["times"] = times
        entry["blocked_until"] = now + block_seconds
        entry["last_warn"] = now
        guard[guard_key] = entry
        setattr(actor, "_command_send_guard", guard)
        if logger:
            logger.warning(
                f"Command guard would block [{guard_key}] after {limit} sends in "
                f"{int(window)}s; skip identity switch and back off {int(block_seconds)}s."
            )
        remember_command_guard_block(actor, guard_key, block_seconds, entry["blocked_until"], reason="command_guard")
        return False

    return True


def command_send_allowed(actor, command, logger=None, limit=MAX_COMMAND_RETRIES,
                          window=COMMAND_GUARD_WINDOW_SECONDS,
                          block_seconds=COMMAND_GUARD_BLOCK_SECONDS):
    """
    命令守卫核心函数。

    在滚动时间窗口内，同一指令最多发送 limit 次。
    超过限制则拦截 block_seconds 秒。
    支持：
    - 指令级别的策略覆盖（COMMAND_GUARD_POLICY_OVERRIDES）
    - 禁用指令列表（DISABLED_AUTO_COMMANDS）
    - 机器人健康暂停联动
    """
    key = str(command or "").strip()
    if not key:
        return True
    if key in DISABLED_AUTO_COMMANDS:
        if logger:
            logger.info(f"Command [{key}] is disabled by local policy; skipping send.")
        remember_command_guard_block(actor, key, 0, reason="disabled")
        return False

    # 针对化身系统的命令守卫隔离：将不同化身发送的历史统计隔离开来，防止并发拦截
    current_id = "主魂"
    if hasattr(actor, "get_identity_from_msg"):
        current_id = actor.get_identity_from_msg(None) or getattr(actor, "current_identity", "主魂")
    else:
        current_id = getattr(actor, "current_identity", "主魂")
    disabled, disabled_key, disabled_entry = dashboard_command_disabled(actor, key, current_id)
    if disabled:
        now_for_log = time.monotonic()
        cache = getattr(actor, "_dashboard_disabled_log_cache", None)
        if cache is None:
            cache = {}
            setattr(actor, "_dashboard_disabled_log_cache", cache)
        log_key = f"{current_id}\u001f{key}"
        if logger and now_for_log - cache.get(log_key, 0) > 300:
            logger.info(
                f"Command [{key}] for [{current_id}] is paused by dashboard"
                f" (control={disabled_key}); skipping send."
            )
            cache[log_key] = now_for_log
        remember_command_guard_block(actor, key, 300, reason="dashboard_disabled", identity=current_id)
        return False

    if (
        not command_allowed_during_identity_pause(key)
        and hasattr(actor, "identity_pause_seconds")
    ):
        try:
            pause_wait = int(actor.identity_pause_seconds(current_id))
        except Exception:
            pause_wait = 0
        if pause_wait > 0:
            if logger:
                logger.info(
                    f"Identity [{current_id}] paused; skip [{key}], retry after {pause_wait}s."
                )
            remember_command_guard_block(actor, key, pause_wait, reason="identity_pause", identity=current_id)
            return False

    key, limit, window, block_seconds, should_alert, track_sends = command_guard_key(
        key, current_id, limit, window, block_seconds
    )

    if not key.startswith(".自证") and is_bot_health_paused(actor):
        wait = bot_health_pause_remaining(actor)
        guard = getattr(actor, "_bot_health_block_log", 0) or 0
        now_for_log = time.monotonic()
        if logger and now_for_log - guard > 60:
            logger.warning(f"Bot health paused; skip [{key}], retry after {wait}s.")
            setattr(actor, "_bot_health_block_log", now_for_log)
        remember_command_guard_block(actor, key, wait, reason="bot_health")
        return False

    now = time.monotonic()
    guard = getattr(actor, "_command_send_guard", None)
    if guard is None:
        guard = {}
        setattr(actor, "_command_send_guard", guard)

    entry = guard.get(key, {"times": [], "blocked_until": 0, "last_warn": 0})
    blocked_until = entry.get("blocked_until", 0)
    if blocked_until > now:
        wait = int(blocked_until - now)
        if logger and now - entry.get("last_warn", 0) > 60:
            logger.warning(f"Command guard blocked [{key}], retry after {wait}s.")
            entry["last_warn"] = now
        guard[key] = entry
        remember_command_guard_block(actor, key, wait, blocked_until, reason="command_guard")
        return False

    if not track_sends:
        return True

    times = [ts for ts in entry.get("times", []) if now - ts <= window]
    if len(times) >= limit:
        entry["times"] = times
        entry["blocked_until"] = now + block_seconds
        entry["last_warn"] = now
        guard[key] = entry
        if logger:
            logger.warning(f"Command guard blocked [{key}] after {limit} sends in {int(window)}s; backing off {int(block_seconds)}s.")
        if should_alert:
            _notify_command_guard_blocked(actor, key, limit, window, block_seconds, logger)
        remember_command_guard_block(actor, key, block_seconds, entry["blocked_until"], reason="command_guard")
        return False

    times.append(now)
    entry["times"] = times
    entry["blocked_until"] = 0
    guard[key] = entry
    return True


# =====================================================================
# 9. 消息记录与发送管理
# =====================================================================

def remember_script_sent_message(actor, msg):
    """记录脚本已发送的消息 ID（用于去重）"""
    msg_id = _message_id(msg)
    if msg_id is None:
        return
    cache_name = "_script_sent_message_ids"
    sent = getattr(actor, cache_name, None)
    if sent is None:
        sent = set()
        setattr(actor, cache_name, sent)
    sent.add(msg_id)
    if len(sent) > 1000:
        setattr(actor, cache_name, set(list(sent)[-500:]))


def is_command_message_text(text):
    """判断文本是否为指令格式（以 . 开头）"""
    return str(text or "").strip().startswith(".")


async def delete_command_message_later(actor, msg, delay_seconds=COMMAND_AUTO_DELETE_SECONDS, logger=None):
    """延迟删除指令消息（用于清理群聊中的自动指令）"""
    if not msg:
        return
    msg_id = _message_id(msg)
    if msg_id is None:
        return
    await asyncio.sleep(delay_seconds)
    try:
        client = getattr(actor, "client", None)
        chat_id = getattr(actor, "target_chat_id", None)
        if client and chat_id:
            await client.delete_messages(chat_id, [msg_id])
        elif hasattr(msg, "delete"):
            await msg.delete()
    except Exception as exc:
        target_logger = logger or logging.getLogger(actor.__class__.__name__)
        target_logger.warning(f"Command auto delete failed for {msg_id}: {exc}")


def schedule_command_auto_delete(actor, msg, text=None, logger=None, delay_seconds=COMMAND_AUTO_DELETE_SECONDS):
    """安排自动删除指令消息（当前暂停，由用户要求关闭）"""
    return False  # auto-delete paused by user request
    if not is_command_message_text(text if text is not None else getattr(msg, "text", "")):
        return False
    msg_id = _message_id(msg)
    if msg_id is None:
        return False
    cache_name = "_auto_delete_scheduled_message_ids"
    scheduled = getattr(actor, cache_name, None)
    if scheduled is None:
        scheduled = set()
        setattr(actor, cache_name, scheduled)
    if msg_id in scheduled:
        return True
    scheduled.add(msg_id)
    if len(scheduled) > 1000:
        setattr(actor, cache_name, set(list(scheduled)[-500:]))
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(delete_command_message_later(actor, msg, delay_seconds, logger))
        return True
    except RuntimeError:
        return False


def remember_script_send_intent(actor, text, ttl=20):
    """记录脚本即将发送的指令内容（用于区分手动/自动发送）"""
    intents = getattr(actor, "_script_send_intents", None)
    if intents is None:
        intents = []
        setattr(actor, "_script_send_intents", intents)
    now = time.monotonic()
    intents[:] = [(t, ts) for t, ts in intents if now - ts <= ttl]
    intents.append((str(text or ""), now))
    if len(intents) > 100:
        del intents[:-50]


def _consume_script_send_intent(actor, text, ttl=20):
    """消费一个发送意图（标记为已处理）"""
    intents = getattr(actor, "_script_send_intents", None)
    if not intents:
        return False
    now = time.monotonic()
    text = str(text or "")
    kept = []
    consumed = False
    for pending_text, ts in intents:
        if now - ts > ttl:
            continue
        if not consumed and pending_text == text:
            consumed = True
            continue
        kept.append((pending_text, ts))
    setattr(actor, "_script_send_intents", kept)
    return consumed


def _reply_to_msg_id(msg):
    reply_to = getattr(msg, "reply_to", None)
    if not reply_to:
        return None
    return getattr(reply_to, "reply_to_msg_id", None) or getattr(reply_to, "channel_post", None)


def meaningful_reply_to_msg_id(actor, msg):
    """
    Return the command message a bot reply points to.

    Telegram forum-topic messages can carry reply_to=<topic root>. That is
    only a thread marker, not attribution to a game command, so do not use it
    for ownership checks.
    """
    replied_id = _reply_to_msg_id(msg)
    if not replied_id:
        return None
    topic_id = getattr(actor, "topic_id", None)
    try:
        if topic_id is not None and int(replied_id) == int(topic_id):
            return None
    except Exception:
        if str(replied_id) == str(topic_id):
            return None
    return replied_id


def _sender_id_variants(sender_id):
    if sender_id is None:
        return set()
    value = str(sender_id)
    variants = {value}
    if value.startswith("-100"):
        variants.add(value[4:])
    elif value.startswith("-"):
        variants.add(value[1:])
    else:
        variants.add(f"-100{value}")
        variants.add(f"-{value}")
    return variants


def _is_own_outgoing_sender(actor, msg):
    """Return True for messages sent by this account or its configured avatars/admin senders."""
    me = getattr(actor, "my_info", None)
    my_id = getattr(me, "id", None)
    sender_id = getattr(msg, "sender_id", None)
    if getattr(msg, "out", False) or (my_id and sender_id == my_id):
        return True
    sender_variants = _sender_id_variants(sender_id)
    if not sender_variants:
        return False

    avatar_ids = set()
    for key in (getattr(actor, "_avatar_chat_ids", {}) or {}).keys():
        avatar_ids.update(_sender_id_variants(key))
    if sender_variants & avatar_ids:
        return True

    admin_ids = set()
    for value in getattr(actor, "pause_admins", set()) or set():
        admin_ids.update(_sender_id_variants(value))
    return bool(sender_variants & admin_ids)


def log_manual_outgoing_if_needed(actor, msg, text=None):
    """检测并记录手动发送的指令（区分脚本自动发 vs 用户手动发）"""
    if not _is_own_outgoing_sender(actor, msg):
        return False
    msg_id = _message_id(msg)
    sent = getattr(actor, "_script_sent_message_ids", set())
    if msg_id in sent:
        sent.discard(msg_id)
        return True
    text = text if text is not None else (getattr(msg, "text", None) or "")
    if _consume_script_send_intent(actor, text):
        return True
    # 通过 chat_id 识别身份（频道号 = 分身，user account = 主魂）
    # 从 actor 实例读取化身 chat_id 映射（每个脚本在 __init__ 中配置）
    _avatar_chat_ids = getattr(actor, '_avatar_chat_ids', {})
    # 频道发消息到群时，chat_id 是群 ID，频道 ID 在 sender_id 里
    _sender_id_str = str(getattr(msg, "sender_id", ""))
    _identity = _avatar_chat_ids.get(_sender_id_str, "")
    # 检测手动 .切换 命令，更新手动身份追踪
    _stripped = text.strip()
    if _stripped.startswith(".切换 "):
        _target = _stripped[4:].strip()
        if _target == "主魂":
            actor._manual_identity_label = "主魂"
        else:
            actor._manual_identity_label = _target
    # 如果 sender_id 无法确定身份，用手动追踪的身份或 current_identity
    if not _identity:
        _identity = getattr(actor, "_manual_identity_label", None) or getattr(actor, "current_identity", "主魂")
    _label = f"manual {msg_id} | {_identity}" 
    logging.getLogger(actor.__class__.__name__).info(f"🟢 OUT [{_label}]:\\n{text}")
    record_message_event(
        actor,
        msg,
        text=text,
        event_kind="new",
        direction="manual_out",
        identity=_identity,
        command=_stripped if is_command_message_text(_stripped) else "",
    )
    schedule_command_auto_delete(actor, msg, text=text, logger=logging.getLogger(actor.__class__.__name__))
    if not is_command_message_text(_stripped):
        return True
    # 记录手动指令 ID，用于后续匹配 bot 回复
    # 使用 deque(maxlen=50) 自动淘汰旧记录，避免 clear() 导致瞬间丢失全部
    _manual_cmds = getattr(actor, "_manual_command_ids", None)
    if _manual_cmds is None:
        _manual_cmds = deque(maxlen=50)
        actor._manual_command_ids = _manual_cmds
    _manual_cmds.append(msg_id)
    _manual_texts = getattr(actor, "_manual_command_texts", None)
    if _manual_texts is None:
        _manual_texts = {}
        actor._manual_command_texts = _manual_texts
    _manual_texts[msg_id] = _stripped
    _manual_identities = getattr(actor, "_manual_command_identities", None)
    if _manual_identities is None:
        _manual_identities = {}
        actor._manual_command_identities = _manual_identities
    _manual_identities[msg_id] = _identity
    _active_manual_ids = set(_manual_cmds)
    actor._manual_command_texts = {k: v for k, v in _manual_texts.items() if k in _active_manual_ids}
    actor._manual_command_identities = {k: v for k, v in _manual_identities.items() if k in _active_manual_ids}
    if hasattr(actor, "command_avatar_map"):
        actor.command_avatar_map[msg_id] = _identity
    record_command_sent(
        actor,
        msg,
        _stripped,
        identity=_identity,
        source="manual",
        reply_to=meaningful_reply_to_msg_id(actor, msg),
    )
    record_recent_profile_command(actor, msg_id, _stripped, _identity, source="manual")
    return True


def _manual_command_record_from_ledger(actor, msg):
    """
    Return a manual command row for the message this bot reply points to.

    The hot path keeps recent manual command IDs in memory, but a delayed bot
    reply can arrive after a restart or after the deque has rotated. The command
    ledger is the persistent authority for that case.
    """
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return None
    try:
        replied_id = int(replied_id)
    except Exception:
        return None

    account = actor_account_key(actor) or actor.__class__.__name__
    chat_id = _safe_message_int(getattr(msg, "chat_id", None) or getattr(actor, "target_chat_id", None))
    cache = getattr(actor, "_manual_command_ledger_cache", None)
    if cache is None:
        cache = {}
        actor._manual_command_ledger_cache = cache
    cache_key = (account, chat_id, replied_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached or None

    row = None
    try:
        with _message_db_connect() as conn:
            if chat_id is not None:
                row = conn.execute(
                    """
                    SELECT command, identity
                    FROM command_ledger
                    WHERE account=? AND chat_id IS ? AND command_msg_id=? AND source='manual'
                    LIMIT 1
                    """,
                    (account, chat_id, replied_id),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT command, identity
                    FROM command_ledger
                    WHERE account=? AND command_msg_id=? AND source='manual'
                    LIMIT 1
                    """,
                    (account, replied_id),
                ).fetchone()
    except Exception:
        return None

    if row is None:
        return None
    record = {"command": row[0] or "", "identity": row[1] or ""}
    cache[cache_key] = record
    if len(cache) > 300:
        actor._manual_command_ledger_cache = dict(list(cache.items())[-150:])
    return record


def is_reply_to_manual_command(actor, msg):
    """检查消息是否是对手动指令的回复（reply_to 指向手动指令消息）"""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return False
    manual_ids = getattr(actor, "_manual_command_ids", None)
    if manual_ids and replied_id in manual_ids:
        return True
    return bool(_manual_command_record_from_ledger(actor, msg))


def manual_command_text_for_reply(actor, msg):
    """返回被回复的手动指令文本；不是手动指令回复时返回空字符串。"""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return ""
    texts = getattr(actor, "_manual_command_texts", None) or {}
    command = texts.get(replied_id, "")
    if command:
        return command
    record = _manual_command_record_from_ledger(actor, msg)
    return (record or {}).get("command", "")


def manual_command_identity_for_reply(actor, msg):
    """返回被回复的手动指令所属身份（主魂/化身）。"""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return ""
    identities = getattr(actor, "_manual_command_identities", None) or {}
    identity = identities.get(replied_id, "")
    if identity:
        return identity
    command_avatar_map = getattr(actor, "command_avatar_map", None) or {}
    identity = command_avatar_map.get(replied_id, "")
    if identity:
        return identity
    record = _manual_command_record_from_ledger(actor, msg)
    return (record or {}).get("identity", "")


def tracked_command_text_for_reply(actor, msg):
    """Return the manual or automatic command text a bot reply points to."""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return ""
    command = manual_command_text_for_reply(actor, msg)
    if command:
        return command
    feedback_commands = getattr(actor, "feedback_commands", None) or {}
    return feedback_commands.get(replied_id, "")


def tracked_command_identity_for_reply(actor, msg):
    """Return the identity attached to a manual or automatic command reply."""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return ""
    identity = manual_command_identity_for_reply(actor, msg)
    if identity:
        return identity
    command_avatar_map = getattr(actor, "command_avatar_map", None) or {}
    identity = command_avatar_map.get(replied_id, "")
    if identity:
        return identity
    feedback_identities = getattr(actor, "feedback_identities", None) or {}
    return feedback_identities.get(replied_id, "")


def is_reply_to_tracked_command(actor, msg):
    """True when a message replies to one of our manual or automatic commands."""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return False
    if is_reply_to_manual_command(actor, msg):
        return True
    for attr in ("feedback_commands", "feedback_events", "command_avatar_map"):
        mapping = getattr(actor, attr, None) or {}
        if replied_id in mapping:
            return True
    return False


def is_reply_to_untracked_message(actor, msg):
    """
    True when a bot message explicitly replies to a non-topic message we did not
    send/track. Such messages belong to someone else's command and must not be
    consumed by mention/loose/passive state sync.
    """
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    return bool(replied_id and not is_reply_to_tracked_command(actor, msg))


# ---- 消息事件库 / 指令台账 ----

_MESSAGE_EVENTS_SCHEMA_READY = False


def _safe_message_int(value):
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _message_text_hash(text):
    return hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()


@contextmanager
def _message_db_connect():
    global _MESSAGE_EVENTS_SCHEMA_READY
    conn = sqlite3.connect(MESSAGE_EVENTS_DB_FILE, timeout=2)
    try:
        if not _MESSAGE_EVENTS_SCHEMA_READY:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    chat_id INTEGER,
                    msg_id INTEGER,
                    reply_to_msg_id INTEGER,
                    sender_id INTEGER,
                    sender_username TEXT,
                    sender_name TEXT,
                    is_out INTEGER NOT NULL DEFAULT 0,
                    is_game_bot INTEGER NOT NULL DEFAULT 0,
                    identity TEXT,
                    command TEXT,
                    text TEXT,
                    text_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account, event_kind, chat_id, msg_id, text_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS command_ledger (
                    account TEXT NOT NULL,
                    chat_id INTEGER,
                    command_msg_id INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reply_to_msg_id INTEGER,
                    status TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    response_msg_id INTEGER,
                    response_hash TEXT,
                    response_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account, chat_id, command_msg_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_msg ON message_events(account, chat_id, msg_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_reply ON message_events(account, chat_id, reply_to_msg_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_created ON message_events(account, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_kind_created ON message_events(account, event_kind, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_bot_created ON message_events(account, is_game_bot, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_bot_created ON message_events(is_game_bot, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_command_ledger_status ON command_ledger(account, status, updated_at)")
            conn.commit()
            _MESSAGE_EVENTS_SCHEMA_READY = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_message_event(
    actor,
    msg,
    text=None,
    sender=None,
    event_kind="new",
    direction="raw",
    identity="",
    command="",
    logger=None,
):
    """Persist a lightweight raw Telegram event for replay/debugging."""
    if msg is None:
        return False
    text_value = text if text is not None else (getattr(msg, "text", None) or "")
    account = actor_account_key(actor) or actor.__class__.__name__
    msg_id = _safe_message_int(_message_id(msg))
    chat_id = _safe_message_int(getattr(msg, "chat_id", None) or getattr(actor, "target_chat_id", None))
    replied_id = _safe_message_int(meaningful_reply_to_msg_id(actor, msg))
    sender_id = _safe_message_int(getattr(msg, "sender_id", None))
    sender_username = (getattr(sender, "username", "") or "").strip().lstrip("@")
    sender_name = sender_display_name(sender, msg)
    is_out = 1 if _is_own_outgoing_sender(actor, msg) else 0
    is_bot = 1 if (sender is not None and is_game_bot_sender(actor, sender)) else 0

    if not identity:
        identity = tracked_command_identity_for_reply(actor, msg) or ""
    if not command:
        command = tracked_command_text_for_reply(actor, msg) or ""

    try:
        with _message_db_connect() as conn:
            conn.execute(
                """
                INSERT INTO message_events (
                    account, event_kind, direction, chat_id, msg_id, reply_to_msg_id,
                    sender_id, sender_username, sender_name, is_out, is_game_bot,
                    identity, command, text, text_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, event_kind, chat_id, msg_id, text_hash) DO UPDATE SET
                    direction=CASE
                        WHEN message_events.direction='raw' AND excluded.direction!='raw' THEN excluded.direction
                        ELSE message_events.direction
                    END,
                    identity=CASE
                        WHEN excluded.identity!='' THEN excluded.identity
                        ELSE message_events.identity
                    END,
                    command=CASE
                        WHEN excluded.command!='' THEN excluded.command
                        ELSE message_events.command
                    END,
                    reply_to_msg_id=COALESCE(message_events.reply_to_msg_id, excluded.reply_to_msg_id),
                    sender_id=COALESCE(message_events.sender_id, excluded.sender_id),
                    sender_username=CASE
                        WHEN excluded.sender_username!='' THEN excluded.sender_username
                        ELSE message_events.sender_username
                    END,
                    sender_name=CASE
                        WHEN excluded.sender_name!='unknown' THEN excluded.sender_name
                        ELSE message_events.sender_name
                    END,
                    is_out=MAX(message_events.is_out, excluded.is_out),
                    is_game_bot=MAX(message_events.is_game_bot, excluded.is_game_bot)
                """,
                (
                    account,
                    str(event_kind or "new"),
                    str(direction or "raw"),
                    chat_id,
                    msg_id,
                    replied_id,
                    sender_id,
                    sender_username,
                    sender_name,
                    is_out,
                    is_bot,
                    str(identity or ""),
                    str(command or ""),
                    str(text_value or ""),
                    _message_text_hash(text_value),
                    datetime.now().strftime(TIME_FORMAT),
                ),
            )
        return True
    except Exception as exc:
        target_logger = logger or logging.getLogger(actor.__class__.__name__)
        target_logger.debug(f"message event persist skipped: {exc}")
        return False


def record_command_sent(actor, msg, command, identity="", source="auto", reply_to=None, logger=None):
    """Persist the authoritative command send row keyed by Telegram message id."""
    msg_id = _safe_message_int(_message_id(msg))
    if msg_id is None:
        return False
    account = actor_account_key(actor) or actor.__class__.__name__
    chat_id = _safe_message_int(getattr(msg, "chat_id", None) or getattr(actor, "target_chat_id", None))
    reply_to_id = _safe_message_int(reply_to)
    now = datetime.now().strftime(TIME_FORMAT)
    try:
        with _message_db_connect() as conn:
            conn.execute(
                """
                INSERT INTO command_ledger (
                    account, chat_id, command_msg_id, command, identity, source,
                    reply_to_msg_id, status, sent_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'sent', ?, ?)
                ON CONFLICT(account, chat_id, command_msg_id) DO UPDATE SET
                    command=excluded.command,
                    identity=excluded.identity,
                    source=excluded.source,
                    reply_to_msg_id=excluded.reply_to_msg_id,
                    status='sent',
                    updated_at=excluded.updated_at
                """,
                (
                    account,
                    chat_id,
                    msg_id,
                    str(command or "").strip(),
                    str(identity or "主魂").strip() or "主魂",
                    str(source or "auto"),
                    reply_to_id,
                    now,
                    now,
                ),
            )
        record_message_event(
            actor,
            msg,
            text=command,
            event_kind="new",
            direction=f"{source or 'auto'}_out",
            identity=identity or "主魂",
            command=command,
            logger=logger,
        )
        return True
    except Exception as exc:
        target_logger = logger or logging.getLogger(actor.__class__.__name__)
        target_logger.debug(f"command ledger send persist skipped: {exc}")
        return False


def record_command_response_for_reply(actor, msg, text=None, status="matched", logger=None):
    """Attach a bot response to the command row it replies to, if tracked."""
    replied_id = _safe_message_int(meaningful_reply_to_msg_id(actor, msg))
    if replied_id is None:
        return False
    account = actor_account_key(actor) or actor.__class__.__name__
    chat_id = _safe_message_int(getattr(msg, "chat_id", None) or getattr(actor, "target_chat_id", None))
    response_id = _safe_message_int(_message_id(msg))
    text_value = text if text is not None else (getattr(msg, "text", None) or "")
    now = datetime.now().strftime(TIME_FORMAT)
    try:
        with _message_db_connect() as conn:
            cur = conn.execute(
                """
                UPDATE command_ledger
                SET status=?, response_msg_id=?, response_hash=?, response_at=?, updated_at=?
                WHERE account=? AND chat_id IS ? AND command_msg_id=?
                """,
                (
                    str(status or "matched"),
                    response_id,
                    _message_text_hash(text_value),
                    now,
                    now,
                    account,
                    chat_id,
                    replied_id,
                ),
            )
            if cur.rowcount <= 0 and chat_id is not None:
                conn.execute(
                    """
                    UPDATE command_ledger
                    SET status=?, response_msg_id=?, response_hash=?, response_at=?, updated_at=?
                    WHERE account=? AND command_msg_id=?
                    """,
                    (
                        str(status or "matched"),
                        response_id,
                        _message_text_hash(text_value),
                        now,
                        now,
                        account,
                        replied_id,
                    ),
                )
        return True
    except Exception as exc:
        target_logger = logger or logging.getLogger(actor.__class__.__name__)
        target_logger.debug(f"command ledger response persist skipped: {exc}")
        return False


def claim_message_version(actor, msg, text=None, purpose="process", ttl=600):
    """Return True once for each (purpose, chat_id, msg_id, text_hash) version."""
    msg_id = _safe_message_int(_message_id(msg))
    chat_id = _safe_message_int(getattr(msg, "chat_id", None) or getattr(actor, "target_chat_id", None))
    if msg_id is None:
        return True
    cache = getattr(actor, "_message_version_claims", None)
    if cache is None:
        cache = {}
        setattr(actor, "_message_version_claims", cache)
    now = time.monotonic()
    for key, expires_at in list(cache.items()):
        if expires_at <= now:
            cache.pop(key, None)
    key = (str(purpose or "process"), chat_id, msg_id, _message_text_hash(text if text is not None else getattr(msg, "text", "")))
    if key in cache:
        return False
    cache[key] = now + max(60, int(ttl or 600))
    if len(cache) > 1000:
        items = sorted(cache.items(), key=lambda item: item[1])
        setattr(actor, "_message_version_claims", dict(items[-500:]))
    return True


def _feedback_candidate_accepts(candidate_fn, command, text):
    if candidate_fn is None:
        return True
    try:
        return bool(candidate_fn(command, text))
    except Exception:
        return False


def _meditation_feedback_matches_identity(actor, text, identity):
    """Meditation settlements often carry @username; do not claim another identity's summary."""
    if not str(text or "").strip():
        return True
    if not profile_text_matches_identity(actor, text, identity):
        return False
    marker = avatar_marker_identity_from_text(text)
    if marker:
        expected = str(identity or "").strip() or "主魂"
        return marker == expected
    return True


def _set_feedback_match(actor, pending_id, msg, text, command, identity, logger=None, label="[FEEDBACK]", reason="reply_to"):
    evt = (getattr(actor, "feedback_events", None) or {}).get(pending_id)
    if evt is None or evt.is_set():
        return False
    actor.last_feedback_text[pending_id] = text
    actor.last_feedback_msg[pending_id] = msg
    remember_incoming_message_context(actor, msg, command=command, identity=identity, text=text)
    record_command_response_for_reply(actor, msg, text=text, status="matched", logger=logger)
    evt.set()
    if logger:
        logger.info(f"{label} Matched [{command}] by {reason}: command_msg={pending_id}, response_msg={getattr(msg, 'id', None)}")
    return True


def match_pending_feedback_by_id(
    actor,
    pending_id,
    msg,
    text,
    candidate_fn=None,
    logger=None,
    label="[REPLY-FEEDBACK]",
    reason="reply_to",
):
    """Validate and match a bot message to one pending command id."""
    feedback_events = getattr(actor, "feedback_events", {}) or {}
    evt = feedback_events.get(pending_id)
    if evt is None or evt.is_set():
        return False
    command = (getattr(actor, "feedback_commands", {}) or {}).get(pending_id, "")
    identity = (getattr(actor, "feedback_identities", {}) or {}).get(
        pending_id, getattr(actor, "current_identity", "主魂")
    ) or "主魂"
    clean_text = str(text or "").strip()
    if not command:
        return False
    if not claim_message_version(actor, msg, clean_text, purpose=f"feedback:{pending_id}"):
        return False
    command_sender_id = (getattr(actor, "feedback_senders", {}) or {}).get(pending_id)
    actual_sender_id = getattr(msg, "sender_id", None)
    if command_sender_id is not None and actual_sender_id == command_sender_id:
        return False
    if clean_text == str(command or "").strip():
        return False
    if mentions_other_user_for_identity(actor, msg, clean_text, identity):
        if logger:
            logger.info(f"{label} Rejected [{command}]: targets another user for [{identity}] (msg {getattr(msg, 'id', None)}).")
        return False
    if _profile_command_key(command) and not profile_text_matches_identity(actor, clean_text, identity):
        if logger:
            logger.info(f"{label} Rejected [{command}]: profile username mismatches [{identity}] (msg {getattr(msg, 'id', None)}).")
        return False
    if command in {".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关"} and not _meditation_feedback_matches_identity(actor, clean_text, identity):
        if logger:
            logger.info(f"{label} Rejected [{command}]: meditation username mismatches [{identity}] (msg {getattr(msg, 'id', None)}).")
        return False
    if feedback_response_conflicts(command, clean_text) and not feedback_response_matches_command(command, clean_text):
        if logger:
            logger.info(f"{label} Rejected [{command}]: response family conflict (msg {getattr(msg, 'id', None)}).")
        return False
    if feedback_response_requires_positive_match(command) and not feedback_response_matches_command(command, clean_text):
        if logger:
            logger.info(f"{label} Rejected [{command}]: content unrelated (msg {getattr(msg, 'id', None)}).")
        return False
    if command == ".查看闭关" and not _feedback_candidate_accepts(candidate_fn, command, clean_text):
        if logger:
            logger.info(f"{label} Rejected [{command}]: meditation candidate check failed (msg {getattr(msg, 'id', None)}).")
        return False
    return _set_feedback_match(
        actor,
        pending_id,
        msg,
        clean_text,
        command,
        identity,
        logger=logger,
        label=label,
        reason=reason,
    )


def match_pending_feedback_by_reply(actor, msg, text, candidate_fn=None, logger=None, label="[REPLY-FEEDBACK]"):
    """Prefer direct Telegram reply_to attribution for command feedback."""
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if not replied_id:
        return False
    return match_pending_feedback_by_id(
        actor,
        replied_id,
        msg,
        text,
        candidate_fn=candidate_fn,
        logger=logger,
        label=label,
        reason="reply_to",
    )


def match_pending_feedback_by_message_id(actor, msg, text, candidate_fn=None, logger=None, label="[EDITED-FEEDBACK]"):
    """Fallback for responses whose own msg_id was registered as pending."""
    msg_id = _message_id(msg)
    if msg_id is None:
        return False
    return match_pending_feedback_by_id(
        actor,
        msg_id,
        msg,
        text,
        candidate_fn=candidate_fn,
        logger=logger,
        label=label,
        reason="message_id",
    )


def _manual_sync_now_str():
    return datetime.now().strftime(TIME_FORMAT)


def _manual_sync_add_seconds(seconds):
    return (datetime.now() + timedelta(seconds=seconds)).strftime(TIME_FORMAT)


def _manual_set_identity_state(actor, identity, key, value):
    if identity and identity != "主魂" and hasattr(actor, "set_avatar_state"):
        actor.set_avatar_state(identity, key, value)
    else:
        state = getattr(actor, "state", None)
        if isinstance(state, dict):
            state[key] = value


CULTIVATION_LEVEL_TEXT_RE = re.compile(
    r"(?:^|\n)\s*(?:\*\*)?(?:当前)?境界(?:\*\*)?\s*[:：]\s*\**\s*([^\n\r*]+)"
)
CULTIVATION_EXP_TEXT_RE = re.compile(
    r"(?:^|\n)\s*(?:\*\*)?(?:当前)?修为(?:\*\*)?\s*[:：]\s*\**\s*([\d,]+)\s*/\s*\**\s*([\d,]+)"
)
CULTIVATION_SPIRIT_ROOT_TEXT_RE = re.compile(
    r"(?:^|\n)\s*(?:\*\*)?灵根(?:\*\*)?\s*[:：]\s*\**\s*([^\n\r*]+)"
)
CULTIVATION_PROFILE_COMMANDS = {".闭关修炼", ".状态", ".我的灵根"}
PROFILE_SPIRIT_ROOT_USER_RE = re.compile(r"@([A-Za-z0-9_]{2,64})\s*的天命玉牒", re.I)
PROFILE_STATUS_USER_RE = re.compile(r"修士状态\s*[·.]\s*@([A-Za-z0-9_]{2,64})", re.I)
PROFILE_MEDITATION_USER_RE = re.compile(
    r"修士\s*@([A-Za-z0-9_]{2,64})\s*(?:深度闭关总结|闭关总结|元神归窍总结|元婴归窍总结)", re.I
)
PROFILE_BREAKTHROUGH_USER_RE = re.compile(r"检测到\s*@([A-Za-z0-9_]{2,64})\s*功成圆满", re.I)
PROFILE_YUANYING_USER_RE = re.compile(r"感应到\s*@([A-Za-z0-9_]{2,64})\s*的元婴", re.I)


def _cultivation_int(value):
    try:
        return int(str(value or "").replace(",", "").strip())
    except Exception:
        return None


def parse_cultivation_profile_text(text):
    """Parse level/current cultivation from .闭关修炼/.状态/.我的灵根 replies."""
    raw = str(text or "")
    if not raw:
        return {}
    profile = {}
    level_match = CULTIVATION_LEVEL_TEXT_RE.search(raw)
    if level_match:
        level = level_match.group(1).strip()
        if level:
            profile["level"] = level
    exp_match = CULTIVATION_EXP_TEXT_RE.search(raw)
    if exp_match:
        current_exp = _cultivation_int(exp_match.group(1))
        total_exp = _cultivation_int(exp_match.group(2))
        if current_exp is not None and total_exp is not None:
            profile["current_exp"] = current_exp
            profile["total_exp"] = total_exp
    spirit_root_match = CULTIVATION_SPIRIT_ROOT_TEXT_RE.search(raw)
    if spirit_root_match:
        spirit_root = spirit_root_match.group(1).replace("*", "").strip()
        if spirit_root:
            profile["spirit_root"] = spirit_root
    return profile


def profile_username_from_text(text):
    """Extract the game character username from .我的灵根/.状态 replies when present."""
    clean = str(text or "").replace("*", "")
    for pattern in (
        PROFILE_SPIRIT_ROOT_USER_RE,
        PROFILE_STATUS_USER_RE,
        PROFILE_MEDITATION_USER_RE,
        PROFILE_BREAKTHROUGH_USER_RE,
        PROFILE_YUANYING_USER_RE,
    ):
        match = pattern.search(clean)
        if match:
            return match.group(1).lower()
    return ""


def _identity_profile_usernames(actor):
    """Return known game usernames for identities; empty means unknown, not a mismatch."""
    mapping = {}
    for value in account_aliases(actor):
        name = _normalize_account_name(value)
        if name:
            mapping.setdefault("主魂", set()).add(name)
    avatar_usernames = getattr(actor, "avatar_usernames", None) or {}
    if isinstance(avatar_usernames, dict):
        for username, identity in avatar_usernames.items():
            uname = _normalize_account_name(username)
            ident = str(identity or "").strip()
            # Some older configs used bot-like placeholders; do not treat those as game usernames.
            if uname and ident and not uname.endswith("bot"):
                mapping.setdefault(ident, set()).add(uname)
    explicit = getattr(actor, "identity_usernames", None) or {}
    if isinstance(explicit, dict):
        for identity, values in explicit.items():
            ident = str(identity or "").strip()
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                uname = _normalize_account_name(value)
                if ident and uname:
                    mapping.setdefault(ident, set()).add(uname)
    return mapping


def profile_text_matches_identity(actor, text, identity):
    username = profile_username_from_text(text)
    if not username:
        return True
    known = _identity_profile_usernames(actor).get(identity or "主魂", set())
    if not known:
        return True
    return _normalize_account_name(username) in known


_CULTIVATION_REALM_RANK = {
    "筑基": 0,
    "结丹": 1,
    "元婴": 2,
    "化神": 3,
    "炼虚": 4,
    "合体": 5,
    "大乘": 6,
    "渡劫": 7,
}
_CULTIVATION_STAGE_RANK = {"初期": 0, "中期": 1, "后期": 2, "圆满": 2, "大圆满": 2}


def cultivation_level_rank(level):
    text = str(level or "").strip()
    if not text:
        return None
    if "炼气" in text:
        match = re.search(r"炼气\s*([一二三四五六七八九十\d]+)\s*层", text)
        if not match:
            return -1
        raw = match.group(1)
        chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        try:
            layer = int(raw)
        except Exception:
            layer = chinese.get(raw, 1)
        return -10 + max(0, min(layer, 10))
    for realm, realm_rank in _CULTIVATION_REALM_RANK.items():
        if realm not in text:
            continue
        stage_rank = 0
        for stage, value in _CULTIVATION_STAGE_RANK.items():
            if stage in text:
                stage_rank = value
                break
        return realm_rank * 3 + stage_rank
    return None


def cultivation_profile_update_is_plausible(actor, profile, identity, is_avatar, has_username, logger=None, source=""):
    """Reject username-less profile snapshots that jump far away from the known state."""
    if has_username:
        return True
    if profile_source_is_direct_profile_command(source):
        return True
    if is_avatar:
        if not hasattr(actor, "get_avatar_state"):
            return True
        container = actor.get_avatar_state(identity)
    else:
        container = getattr(actor, "state", None)
    if not isinstance(container, dict):
        return True

    old_level = str(container.get("level") or "").strip()
    new_level = str(profile.get("level") or "").strip()
    old_rank = cultivation_level_rank(old_level)
    new_rank = cultivation_level_rank(new_level)
    if old_rank is not None and new_rank is not None and abs(new_rank - old_rank) > 1:
        if logger:
            logger.warning(
                f"Cultivation profile sync skipped for [{identity}]: username-less level jump "
                f"{old_level or '?'} -> {new_level or '?'} ({source})."
            )
        return False

    old_total = _cultivation_int(container.get("total_exp"))
    new_total = _cultivation_int(profile.get("total_exp"))
    if old_total and new_total:
        ratio = max(old_total, new_total) / max(1, min(old_total, new_total))
        if ratio > 2:
            if logger:
                logger.warning(
                    f"Cultivation profile sync skipped for [{identity}]: username-less exp cap jump "
                    f"{old_total} -> {new_total} ({source})."
                )
            return False
        old_current = _cultivation_int(container.get("current_exp"))
        new_current = _cultivation_int(profile.get("current_exp"))
        if old_current is not None and new_current is not None:
            jump_limit = max(50000, min(old_total, new_total))
            if abs(new_current - old_current) > jump_limit:
                if logger:
                    logger.warning(
                        f"Cultivation profile sync skipped for [{identity}]: username-less current jump "
                        f"{old_current} -> {new_current} ({source})."
                    )
                return False
    return True


def _profile_command_key(command):
    cmd = str(command or "").strip()
    if not cmd.startswith("."):
        return ""
    base = cmd.split()[0]
    return base if base in CULTIVATION_PROFILE_COMMANDS else ""


def _profile_source_command(source):
    """Extract the command name embedded in a sync source string."""
    match = re.search(r"(?<![A-Za-z0-9_])(\.[\u4e00-\u9fffA-Za-z0-9_]+)", str(source or ""))
    return match.group(1) if match else ""


def profile_source_allows_usernameless_update(source):
    """Username-less profile values are only safe when tied to a profile command."""
    source_text = str(source or "")
    command = _profile_source_command(source_text)
    if command:
        return bool(_profile_command_key(command))
    return "recent profile" in source_text or "edited profile" in source_text


def profile_source_is_direct_profile_command(source):
    """Return True for a tracked/manual identity profile command response."""
    source_text = str(source or "")
    command = _profile_source_command(source_text)
    if not _profile_command_key(command):
        return False
    lowered = source_text.lower()
    return "passive profile" not in lowered and "recent profile" not in lowered


def record_recent_profile_command(actor, msg_id, command, identity, source=""):
    """Remember commands whose final profile reply may not be a direct reply_to."""
    cmd = _profile_command_key(command)
    if not cmd:
        return False
    pending = getattr(actor, "_recent_cultivation_profile_commands", None)
    if not isinstance(pending, deque):
        pending = deque(maxlen=40)
        actor._recent_cultivation_profile_commands = pending
    pending.append({
        "msg_id": int(msg_id or 0),
        "command": cmd,
        "identity": identity or "主魂",
        "ts": time.monotonic(),
        "source": source or "",
    })
    return True


def _expected_profile_commands_for_text(text):
    clean = str(text or "").replace("**", "")
    if "天命玉牒" in clean and "修为" in clean:
        return [".我的灵根"]
    if "修士状态" in clean and "境界" in clean:
        return [".状态"]
    if "闭关" in clean and ("当前境界" in clean or "当前修为" in clean):
        return [".闭关修炼"]
    if parse_cultivation_profile_text(text):
        return list(CULTIVATION_PROFILE_COMMANDS)
    return []


def recent_profile_identity_for_text(actor, text, msg_id=None, max_age_seconds=90, id_window=120, consume=True):
    """Find the identity for non-reply profile texts using recent .状态/.我的灵根 commands."""
    expected = set(_expected_profile_commands_for_text(text))
    if not expected:
        return ""
    # Non-reply profile fallbacks are only safe when the text carries the
    # character username. Username-less meditation/profile snippets are common
    # in the group and can otherwise be claimed by the nearest recent command.
    if not profile_username_from_text(text):
        return ""
    pending = getattr(actor, "_recent_cultivation_profile_commands", None)
    if not pending:
        return ""
    now = time.monotonic()
    msg_num = int(msg_id or 0)
    kept = deque(maxlen=40)
    chosen = ""
    chosen_index = -1
    for item in pending:
        try:
            age = now - float(item.get("ts", 0) or 0)
        except Exception:
            age = max_age_seconds + 1
        if age > max_age_seconds:
            continue
        item_msg_id = int(item.get("msg_id", 0) or 0)
        if msg_num and item_msg_id and (msg_num <= item_msg_id or msg_num - item_msg_id > id_window):
            kept.append(item)
            continue
        kept.append(item)
        candidate_identity = item.get("identity") or "主魂"
        if item.get("command") in expected and profile_text_matches_identity(actor, text, candidate_identity):
            chosen = candidate_identity
            chosen_index = len(kept) - 1
    if consume and chosen_index >= 0:
        kept_list = list(kept)
        kept_list.pop(chosen_index)
        kept = deque(kept_list, maxlen=40)
    actor._recent_cultivation_profile_commands = kept
    return chosen


def record_cultivation_profile_from_text(actor, text, identity=None, logger=None, source=""):
    """Apply parsed cultivation profile fields to the main soul or a confirmed avatar."""
    profile = parse_cultivation_profile_text(text)
    if not profile:
        return False
    identity = identity or getattr(actor, "current_identity", "主魂") or "主魂"
    if not profile_text_matches_identity(actor, text, identity):
        if logger:
            logger.warning(f"Cultivation profile sync skipped for [{identity}]: profile username mismatch ({source}).")
        return False
    avatars = set(getattr(actor, "avatars", []) or [])
    is_avatar = identity != "主魂" and identity in avatars and hasattr(actor, "set_avatar_state")
    if identity != "主魂" and not is_avatar:
        if logger:
            logger.warning(f"Cultivation profile sync skipped for unknown identity [{identity}] ({source}).")
        return False
    profile_username = profile_username_from_text(text)
    has_profile_username = bool(profile_username)
    if not has_profile_username and not profile_source_allows_usernameless_update(source):
        if logger:
            logger.info(
                f"Cultivation profile sync skipped for [{identity}]: username-less profile from "
                f"non-profile source ({source or 'response'})."
            )
        return False
    if not cultivation_profile_update_is_plausible(
        actor, profile, identity, is_avatar, has_profile_username, logger=logger, source=source
    ):
        return False

    changed = False
    changed_fields = []

    def set_value(key, value):
        nonlocal changed
        if is_avatar:
            current = (actor.get_avatar_state(identity) if hasattr(actor, "get_avatar_state") else {}).get(key)
            if current != value:
                actor.set_avatar_state(identity, key, value)
                changed = True
                changed_fields.append(key)
            return
        state = getattr(actor, "state", None)
        if isinstance(state, dict) and state.get(key) != value:
            state[key] = value
            changed = True
            changed_fields.append(key)

    if "level" in profile:
        set_value("level", profile["level"])
        set_value("cultivation_level", profile["level"])
    if "current_exp" in profile and "total_exp" in profile:
        set_value("current_exp", profile["current_exp"])
        set_value("total_exp", profile["total_exp"])
    if "spirit_root" in profile:
        set_value("spirit_root", profile["spirit_root"])
    if has_profile_username:
        set_value("profile_username", profile_username)
        set_value("profile_verified_at", datetime.now().strftime(TIME_FORMAT))

    if changed and not is_avatar and hasattr(actor, "save_state"):
        actor.save_state()
    if logger and changed:
        logger.info(
            f"Cultivation profile synced [{identity}] from {source or 'response'}: "
            f"{', '.join(changed_fields)}"
        )
    return True


CULTIVATION_DELTA_SKIP_PHRASES = (
    "当前修为", "**修为**", "基础修为:", "基础修为增加", "每日被动修为", "红袖添香", "灵犀双运",
    "同门道友", "可以使用 `.重置古塔`", "今日已挑战失败", "预计消耗",
    "被动将灵气转化为修为", "前路已被", "消耗修为来", "修为惩罚", "修为加成",
    "施展需消耗修为",
)


def parse_cultivation_delta_text(text):
    """Parse cultivation gain/loss deltas from command feedback text."""
    raw = str(text or "")
    if not raw or "修为" not in raw or "修为不足" in raw:
        return []
    # Authoritative profile replies carry the final current/required values;
    # applying deltas on the same text would double count.
    profile = parse_cultivation_profile_text(raw)
    if "current_exp" in profile and "total_exp" in profile:
        return []

    deltas = []

    def add(value):
        amount = _cultivation_int(value)
        if amount:
            deltas.append(amount)

    match = re.search(r"本次深度闭关，你的修为最终变化了\s*\*?\*?([+-]?\d[\d,]*)\*?\*?\s*点", raw)
    if match:
        add(match.group(1))
        return deltas
    match = re.search(r"本次闭关，你的修为最终增加了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", raw)
    if match:
        add(match.group(1))
        return deltas
    match = re.search(r"修为结算[:：]\s*\*?\*?([+-]?\d[\d,]*)\*?\*?", raw)
    if match:
        add(match.group(1))
        return deltas

    for line in raw.splitlines():
        clean = line.strip()
        if not clean or "修为" not in clean:
            continue
        if any(phrase in clean for phrase in CULTIVATION_DELTA_SKIP_PHRASES):
            continue
        line_deltas = []
        for amount in re.findall(r"(?:消耗了|开始消耗)\D{0,40}?\*?\*?(\d[\d,]*)\*?\*?\s*点\s*修为", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"你消耗了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"本次消耗[:：]\s*\*?\*?(\d[\d,]*)\s*修为\*?\*?", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"额外损失了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"修为折损\s*\*?\*?(-?\d[\d,]*)\*?\*?", clean):
            line_deltas.append(-abs(_cultivation_int(amount)))
        for amount in re.findall(r"修为\*?\*?额外倒退了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"修为\*?\*?倒退了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"修为\s*\*?\*?损失了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"修为\*?\*?减少了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"减少了\s*\*?\*?(\d[\d,]*)\s*点\*?\*?\s*修为", clean):
            line_deltas.append(-_cultivation_int(amount))
        for amount in re.findall(r"本次获得\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"获得了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"额外获得\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"(?:获得|额外获得)\s*\*?\*?(\d[\d,]*)\s*修为\*?\*?", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"折算为修为\s*(\d[\d,]*)", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"获得修为\s*\*?\*?([+-]?\d[\d,]*)\*?\*?", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"修为\s*([+-]\d[\d,]*)", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"修为\s*\*?\*?额外增加了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_deltas.append(_cultivation_int(amount))
        for amount in re.findall(r"修为\s*\*?\*?增加了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_deltas.append(_cultivation_int(amount))

        seen = set()
        for delta in line_deltas:
            if delta and delta not in seen:
                seen.add(delta)
                deltas.append(delta)
    return deltas


def record_cultivation_delta_from_text(actor, text, identity=None, logger=None, source="", msg=None, msg_id=None):
    """Apply parsed cultivation deltas to state when a baseline current/total is known."""
    deltas = parse_cultivation_delta_text(text)
    if not deltas:
        return False

    identity = identity or getattr(actor, "current_identity", "主魂") or "主魂"
    avatars = set(getattr(actor, "avatars", []) or [])
    is_avatar = identity != "主魂" and identity in avatars
    if identity != "主魂" and not is_avatar:
        if logger:
            logger.warning(f"Cultivation delta sync skipped for unknown identity [{identity}] ({source}).")
        return False

    if is_avatar:
        if not hasattr(actor, "get_avatar_state"):
            return False
        container = actor.get_avatar_state(identity)
    else:
        container = getattr(actor, "state", None)
    if not isinstance(container, dict):
        return False

    current = _cultivation_int(container.get("current_exp"))
    total = _cultivation_int(container.get("total_exp"))
    if current is None or total is None or total <= 0:
        if logger:
            logger.info(f"Cultivation delta sync skipped [{identity}] from {source or 'response'}: baseline unknown.")
        return False

    delta_total = sum(deltas)
    if not delta_total:
        return False
    delta_to_apply = delta_total
    response_id = msg_id if msg_id is not None else _message_id(msg)
    cache = None
    if response_id is not None:
        cache = getattr(actor, "_cultivation_delta_state_sync_cache", None)
        if cache is None:
            cache = {}
            actor._cultivation_delta_state_sync_cache = cache
        previous = cache.get(response_id)
        if previous:
            if isinstance(previous, dict):
                previous_identity = previous.get("identity", "")
                previous_delta = int(previous.get("delta_total", 0) or 0)
            else:
                previous_identity = previous[0] if isinstance(previous, tuple) and previous else ""
                previous_delta = 0
            if previous_identity and previous_identity != identity:
                if logger:
                    logger.warning(
                        f"Cultivation delta sync skipped for edited msg {response_id}: "
                        f"identity changed {previous_identity} -> {identity}."
                    )
                return False
            delta_to_apply = delta_total - previous_delta
            if not delta_to_apply:
                return True

    container["current_exp"] = max(0, current + delta_to_apply)
    container["total_exp"] = total

    if response_id is not None and cache is not None:
        cache[response_id] = {"identity": identity, "source": str(source or ""), "delta_total": delta_total}
        if len(cache) > 300:
            actor._cultivation_delta_state_sync_cache = dict(list(cache.items())[-150:])
    if hasattr(actor, "save_state"):
        actor.save_state()
    if logger:
        logger.info(
            f"Cultivation delta synced [{identity}] from {source or 'response'}: "
            f"{current} -> {container['current_exp']} ({delta_to_apply:+d}, total {delta_total:+d})"
        )
    return True


def _manual_record_field_training_reply(actor, text, identity, logger):
    clean = str(text or "").replace("**", "")
    cd = actor.parse_wait_time(text) if hasattr(actor, "parse_wait_time") else -1
    now = _manual_sync_now_str()
    is_cooldown = any(k in clean for k in ["山中灵机未复", "冷却", "后再", "尚未", "请在"])
    is_response = "野外历练" in clean or "山中灵机未复" in clean
    if identity and identity != "主魂" and hasattr(actor, "set_avatar_state"):
        if is_cooldown and cd > 0:
            _manual_set_identity_state(actor, identity, "next_field_training_time", _manual_sync_add_seconds(cd))
            return True
        if is_response:
            _manual_set_identity_state(actor, identity, "last_field_training_time", now)
            _manual_set_identity_state(actor, identity, "next_field_training_time", _manual_sync_add_seconds(2 * 3600))
            return True
        return False
    if hasattr(actor, "record_field_training_response"):
        return bool(actor.record_field_training_response(text, "手动野外历练同步"))
    return False


def _manual_record_concubine_task_reply(actor, task_key, text, identity):
    task_map = {
        "dream": ("next_dream_map_time", 8 * 3600),
        "heart_trial": ("next_heart_trial_time", 10 * 3600),
        "divination": ("next_divination_time", 12 * 3600),
        "voyage": ("next_concubine_voyage_time", 12 * 3600),
    }
    state_key, default_cd = task_map[task_key]
    if identity and identity != "主魂" and hasattr(actor, "set_avatar_state"):
        cd = actor.parse_wait_time(text) if hasattr(actor, "parse_wait_time") else -1
        clean = str(text or "").replace("**", "")
        if any(k in clean for k in ["可施展", "可用", "已就绪", "可归来", "可结算"]):
            _manual_set_identity_state(actor, identity, state_key, _manual_sync_now_str())
            return True
        if cd > 0 and any(k in clean for k in ["冷却", "后再", "尚未", "余波未散"]):
            _manual_set_identity_state(actor, identity, state_key, _manual_sync_add_seconds(cd + 60))
            return True
        if any(k in clean for k in ["成功", "完成", "获得", "机缘", "残图", "梦兆", "坠魔心劫", "天机代卜", "卜算"]):
            _manual_set_identity_state(actor, identity, state_key, _manual_sync_add_seconds(default_cd + 60))
            return True
        return False
    if hasattr(actor, "record_concubine_cd"):
        actor.record_concubine_cd(task_key, text)
        return True
    return False


def _manual_record_concubine_status_reply(actor, text, identity):
    if identity and identity != "主魂" and hasattr(actor, "set_avatar_state"):
        clean = str(text or "").replace("**", "")
        if hasattr(actor, "concubine_status_matches_identity") and not actor.concubine_status_matches_identity(clean, identity):
            return False
        updated = False
        labels = {
            "入梦寻图冷却": ("next_dream_map_time", 8 * 3600),
            "共历心劫冷却": ("next_heart_trial_time", 10 * 3600),
            "天机代卜冷却": ("next_divination_time", 12 * 3600),
            "侍妾远航冷却": ("next_concubine_voyage_time", 12 * 3600),
            "远航冷却": ("next_concubine_voyage_time", 12 * 3600),
        }
        for label, (state_key, _) in labels.items():
            match = re.search(rf"{re.escape(label)}\s*[：:]\s*([^\n]+)", clean)
            if not match:
                continue
            value = match.group(1).strip()
            if any(k in value for k in ["无", "可用", "可施展", "已就绪", "可归来", "可结算"]):
                _manual_set_identity_state(actor, identity, state_key, "")
                if state_key == "next_concubine_voyage_time":
                    _manual_set_identity_state(
                        actor,
                        identity,
                        "concubine_voyage_active",
                        "归来" in label or any(k in value for k in ["可归来", "可结算"]),
                    )
                updated = True
                continue
            cd = actor.parse_wait_time(value) if hasattr(actor, "parse_wait_time") else -1
            if cd > 0:
                next_time = _manual_sync_add_seconds(cd + 60)
                _manual_set_identity_state(actor, identity, state_key, next_time)
                if state_key == "next_concubine_voyage_time":
                    _manual_set_identity_state(actor, identity, "concubine_voyage_active", True)
                    if hasattr(actor, "bind_concubine_chain_to_time"):
                        actor.bind_concubine_chain_to_time(identity, next_time, active=True)
                updated = True
        if updated:
            _manual_set_identity_state(actor, identity, "last_concubine_status_time", _manual_sync_now_str())
        return updated
    if hasattr(actor, "parse_concubine_status"):
        return bool(actor.parse_concubine_status(text))
    return False


def _manual_record_meditation_reply(actor, text, identity):
    clean = str(text or "").replace("**", "")
    now = _manual_sync_now_str()
    ongoing = any(k in clean for k in ["预计还需", "还需"])
    real_exit = (
        any(k in clean for k in ["强行出关", "出关成功", "已出关", "闭关结束"])
        or ("功成圆满" in clean and not ongoing and "正在" not in clean)
        or any(k in clean for k in ["未处于深度闭关", "并未处于深度闭关", "归位"])
    )
    if real_exit:
        _manual_set_identity_state(actor, identity, "in_deep_meditation", False)
        _manual_set_identity_state(actor, identity, "deep_meditation_end_time", "")
        return True
    if any(k in clean for k in ["深度闭关", "闭关修炼", "预计还需", "开始闭关", "开启闭关"]):
        cd = actor.parse_wait_time(text) if hasattr(actor, "parse_wait_time") else -1
        if cd > 0:
            _manual_set_identity_state(actor, identity, "in_deep_meditation", True)
            _manual_set_identity_state(actor, identity, "deep_meditation_end_time", _manual_sync_add_seconds(cd))
            return True
    if "闭关冷却" in clean or "闭关剩余" in clean:
        try:
            cd = actor.parse_wait_time(text, line_identifier="闭关")
        except TypeError:
            cd = actor.parse_wait_time(text) if hasattr(actor, "parse_wait_time") else -1
        if cd > 0:
            _manual_set_identity_state(actor, identity, "next_meditation_time", _manual_sync_add_seconds(cd))
            return True
    return False


def _manual_record_tower_reply(actor, text, identity):
    clean = str(text or "").replace("**", "")
    if not any(k in clean for k in ["闯塔", "通关", "塔钥", "今日已闯", "层"]):
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    _manual_set_identity_state(actor, identity, "last_tower_date", today)
    return True


async def record_manual_command_reply_state_if_needed(actor, msg, text=None, sender=None, logger=None):
    """同步手动指令的机器人回复到 state，不参与自动 pending 匹配。"""
    if not is_reply_to_manual_command(actor, msg):
        return False
    text = text if text is not None else (getattr(msg, "text", None) or "")
    command = manual_command_text_for_reply(actor, msg)
    if not command:
        return False
    logger = logger or logging.getLogger(actor.__class__.__name__)
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    msg_id = _message_id(msg)
    cache = getattr(actor, "_manual_reply_state_sync_cache", None)
    if cache is None:
        cache = {}
        actor._manual_reply_state_sync_cache = cache
    cache_key = (replied_id, command, str(text or "").strip())
    if msg_id is not None and cache.get(msg_id) == cache_key:
        return True
    if msg_id is not None:
        cache[msg_id] = cache_key
        if len(cache) > 300:
            actor._manual_reply_state_sync_cache = dict(list(cache.items())[-150:])

    identity = manual_command_identity_for_reply(actor, msg) or getattr(actor, "current_identity", "主魂")
    await log_incoming_message(
        actor,
        f"manual {command} reply",
        text,
        msg=msg,
        sender=sender,
        logger=logger,
        identity=identity,
    )
    remember_manual_reply_logged_message(actor, msg, text=text)
    if mentions_other_user(actor, msg, text) and not _profile_command_key(command):
        logger.info(f"Manual reply sync skipped for [{command}]: targets another user (msg {getattr(msg, 'id', None)}).")
        return False

    cmd = command.strip()
    processed = False
    if hasattr(actor, "record_identity_yuanying_recovery_from_text"):
        processed = bool(actor.record_identity_yuanying_recovery_from_text(
            identity, text, source=f"manual {cmd}", command=cmd
        )) or processed
    if _profile_command_key(cmd):
        processed = record_cultivation_profile_from_text(
            actor, text, identity=identity, logger=logger, source=f"manual {cmd}"
        )

    if cmd.startswith(".野外历练"):
        processed = _manual_record_field_training_reply(actor, text, identity, logger)
    elif cmd == ".探寻裂缝":
        if hasattr(actor, "is_rift_weakness_response") and actor.is_rift_weakness_response(text):
            if hasattr(actor, "stop_for_rift_weakness"):
                await actor.stop_for_rift_weakness(text, identity=identity)
            processed = True
        elif hasattr(actor, "record_identity_fixed_cd_command_response"):
            processed = bool(actor.record_identity_fixed_cd_command_response(
                identity, text, ".探寻裂缝", "last_rift_search_time", "next_rift_search_time", 12 * 3600
            ))
        elif hasattr(actor, "record_fixed_cd_command_response"):
            processed = bool(actor.record_fixed_cd_command_response(
                text, ".探寻裂缝", "last_rift_search_time", "next_rift_search_time", 12 * 3600
            ))
    elif cmd.startswith(".抚摸法宝"):
        if hasattr(actor, "record_treasure_touch_response"):
            processed = bool(actor.record_treasure_touch_response(text))
    elif cmd in {".灵树灌溉", ".灵树状态", ".采摘灵果", ".协同守山"}:
        if hasattr(actor, "maybe_record_spirit_tree_passive_message") and cmd == ".灵树状态":
            processed = bool(actor.maybe_record_spirit_tree_passive_message(
                msg, text, source=f"manual {cmd}", identity=identity
            ))
        elif hasattr(actor, "maybe_record_spirit_tree_passive_message") and cmd == ".灵树灌溉":
            processed = bool(actor.maybe_record_spirit_tree_passive_message(
                msg, text, source=f"manual {cmd}", identity=identity
            ))
            if not processed and hasattr(actor, "record_spirit_tree_irrigation_state"):
                processed = bool(actor.record_spirit_tree_irrigation_state(
                    text, source=f"manual {cmd}", identity=identity
                ))
        elif cmd == ".采摘灵果" and hasattr(actor, "record_spirit_tree_harvest_response"):
            processed = bool(actor.record_spirit_tree_harvest_response(text, identity=identity))
        elif cmd == ".协同守山" and hasattr(actor, "record_spirit_tree_guard_response"):
            processed = bool(actor.record_spirit_tree_guard_response(text, identity=identity))
    elif (
        cmd == ".我的灵兽"
        or cmd == ".灵兽偷菜"
        or cmd.startswith(".探渊 ")
        or cmd.startswith(".灵兽探渊 ")
        or cmd.startswith(".灵兽出战 ")
        or cmd.startswith(".灵兽休息 ")
        or cmd.startswith(".灵兽互动 ")
        or cmd.startswith(".灵兽巡游 ")
    ):
        if hasattr(actor, "record_manual_beast_command_response"):
            processed = bool(actor.record_manual_beast_command_response(cmd, text))
        elif cmd.startswith(".灵兽互动 ") and hasattr(actor, "record_beast_interaction_response"):
            processed = bool(actor.record_beast_interaction_response(text))
        elif cmd.startswith(".灵兽巡游 ") and hasattr(actor, "record_beast_cruise_response"):
            processed = bool(actor.record_beast_cruise_response(text))
    elif cmd in {".元婴出窍", ".元婴闭关"}:
        if hasattr(actor, "record_identity_yuanying_out_start_response"):
            processed = bool(actor.record_identity_yuanying_out_start_response(identity, text))
        elif hasattr(actor, "record_yuanying_out_start_response"):
            processed = bool(actor.record_yuanying_out_start_response(text))
    elif cmd == ".问道":
        if hasattr(actor, "record_ask_dao_response"):
            processed = bool(actor.record_ask_dao_response(text, source="manual .问道"))
    elif cmd == ".我的侍妾":
        processed = _manual_record_concubine_status_reply(actor, text, identity)
    elif cmd == ".入梦寻图":
        processed = _manual_record_concubine_task_reply(actor, "dream", text, identity)
    elif cmd in {".共历心劫", ".坠魔心劫"}:
        processed = _manual_record_concubine_task_reply(actor, "heart_trial", text, identity)
    elif cmd == ".天机代卜":
        processed = _manual_record_concubine_task_reply(actor, "divination", text, identity)
    elif cmd.startswith(".侍妾远航"):
        if hasattr(actor, "record_concubine_voyage_response"):
            processed = bool(actor.record_concubine_voyage_response(text, identity=identity, command=cmd))
        else:
            processed = _manual_record_concubine_task_reply(actor, "voyage", text, identity)
    elif cmd == ".远航归来":
        if hasattr(actor, "record_concubine_voyage_response"):
            processed = bool(actor.record_concubine_voyage_response(text, identity=identity, command=cmd))
        else:
            processed = _manual_record_concubine_task_reply(actor, "voyage", text, identity)
    elif cmd in {".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关"}:
        processed = _manual_record_meditation_reply(actor, text, identity) or processed
    elif cmd in {".状态", ".我的灵根"}:
        processed = processed
    elif cmd == ".闯塔":
        processed = _manual_record_tower_reply(actor, text, identity)
    elif cmd in {".观星台", ".安抚星辰", ".收集精华"} or cmd.startswith(".牵引星辰"):
        if identity != "主魂" and hasattr(actor, "record_avatar_star_response_from_text"):
            processed = bool(actor.record_avatar_star_response_from_text(
                identity, text, source=f"manual {cmd}"
            ))
        elif cmd == ".安抚星辰" and hasattr(actor, "record_star_calm_response"):
            processed = bool(actor.record_star_calm_response(text, "手动安抚星辰同步"))

    processed = record_cultivation_delta_from_text(
        actor, text, identity=identity, logger=logger, source=f"manual {cmd}", msg=msg
    ) or processed

    if processed and hasattr(actor, "save_state"):
        actor.save_state()
        logger.info(f"Manual command reply synced [{cmd}] for {identity} from msg {getattr(msg, 'id', None)}.")
    return processed


# =====================================================================
# 10. 账号提及检测
# =====================================================================

def mentions_self(actor, msg, text):
    """检测消息是否提到了本账号（通过 @用户名、实体 ID、URL 等方式）"""
    me = getattr(actor, "my_info", None)
    if not me:
        return False
    my_id = getattr(me, "id", None)
    if getattr(msg, "out", False) or (my_id and getattr(msg, "sender_id", None) == my_id):
        return False
    if getattr(msg, "mentioned", False):
        return True
    lower_text = (text or "").lower()
    username = (getattr(me, "username", "") or "").lower().lstrip("@")
    if username and f"@{username}" in lower_text:
        return True
    first_name = (getattr(me, "first_name", "") or "").lower().strip()
    if first_name and f"@{first_name}" in lower_text:
        return True
    if my_id:
        for entity in getattr(msg, "entities", None) or []:
            if getattr(entity, "user_id", None) == my_id:
                return True
            url = getattr(entity, "url", "") or ""
            if url.startswith("tg://user"):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(url)
                user_ids = parse_qs(parsed.query).get("id", [])
                if any(str(my_id) == str(user_id) for user_id in user_ids):
                    return True
    return False

def mentions_other_user(actor, msg, text=None):
    """检测消息是否明确提及了其他用户且未提及本账号"""
    text_value = text if text is not None else getattr(msg, "text", "")
    # .我的灵根/.状态 回复里的 @xxx 是游戏角色名，不是 Telegram 指令接收人。
    if profile_username_from_text(text_value):
        return False
    me = getattr(actor, "my_info", None)
    my_id = getattr(me, "id", None) if me else None
    
    has_other_mention = False
    mentions_me = False
    aliases = set(account_aliases(actor))

    for username in text_username_mentions(text_value):
        if username in aliases:
            mentions_me = True
        else:
            has_other_mention = True
    
    if getattr(msg, "entities", None):
        for entity in msg.entities:
            if hasattr(entity, "user_id"):
                if str(entity.user_id) == str(my_id):
                    mentions_me = True
                else:
                    has_other_mention = True
            
            url = getattr(entity, "url", "") or ""
            if url.startswith("tg://user"):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(url)
                user_ids = parse_qs(parsed.query).get("id", [])
                if any(str(my_id) == str(uid) for uid in user_ids):
                    mentions_me = True
                elif user_ids:
                    has_other_mention = True
                    
    return has_other_mention and not mentions_me


def log_mention_if_needed(actor, msg, text=None, label="mention", sender=None):
    """需要时记录提到了本账号的消息"""
    text = text if text is not None else (getattr(msg, "text", None) or "")
    msg_id = _message_id(msg)
    if label != "edited" and was_manual_reply_logged_message(actor, msg):
        return True
    if sender is not None and is_game_bot_sender(actor, sender):
        profile_identity = recent_profile_identity_for_text(actor, text, msg_id=msg_id, consume=False)
        if profile_identity:
            profile_text = text
            if profile_identity != "主魂":
                profile_text = f"[Avatar: {profile_identity}]\n{text or ''}"
            logging.getLogger(actor.__class__.__name__).info(
                format_in_log(f"profile {msg_id}", profile_text, sender=sender, msg=msg)
            )
            record_message_event(
                actor,
                msg,
                text=profile_text,
                sender=sender,
                event_kind="new",
                direction="bot_in",
                identity=profile_identity,
                command="profile",
            )
            record_command_response_for_reply(actor, msg, text=profile_text, status="matched")
            remember_logged_incoming_message(actor, msg, text=profile_text)
            remember_incoming_message_context(actor, msg, command="profile", identity=profile_identity, text=profile_text)
            logger = logging.getLogger(actor.__class__.__name__)
            record_cultivation_profile_from_text(
                actor, text, identity=profile_identity, logger=logger, source="recent profile"
            )
            record_cultivation_delta_from_text(
                actor, text, identity=profile_identity, logger=logger, source="recent profile", msg=msg
            )
            recent_profile_identity_for_text(actor, text, msg_id=msg_id, consume=True)
            return True
    if not mentions_self(actor, msg, text):
        return False
    current_id = avatar_marker_identity_from_text(text) or "主魂"
    if current_id == "主魂" and hasattr(actor, "get_identity_from_msg"):
        current_id = actor.get_identity_from_msg(msg) or getattr(actor, "current_identity", "主魂")
    elif current_id == "主魂":
        current_id = getattr(actor, "current_identity", "主魂")
    if sender is not None and is_game_bot_sender(actor, sender) and hasattr(actor, "record_identity_yuanying_recovery_from_text"):
        if actor.record_identity_yuanying_recovery_from_text(
            current_id, text, source=f"mention {msg_id}", command=""
        ):
            return True
    if current_id != "主魂":
        text = f"[Avatar: {current_id}]\n{text or ''}"
    if label == "edited":
        cache_name = "_logged_edited_message_texts"
        seen_texts = getattr(actor, cache_name, None)
        if seen_texts is None:
            seen_texts = {}
            setattr(actor, cache_name, seen_texts)
        if msg_id is not None and seen_texts.get(msg_id) == text:
            return True
        if msg_id is not None:
            seen_texts[msg_id] = text
            if len(seen_texts) > 300:
                setattr(actor, cache_name, dict(list(seen_texts.items())[-150:]))
    else:
        cache_name = "_logged_mention_message_ids"
        seen = getattr(actor, cache_name, None)
        if seen is None:
            seen = set()
            setattr(actor, cache_name, seen)
        key = (label, msg_id)
        if msg_id is not None and key in seen:
            return True
        if msg_id is not None:
            seen.add(key)
            if len(seen) > 500:
                setattr(actor, cache_name, set(list(seen)[-250:]))

    logging.getLogger(actor.__class__.__name__).info(format_in_log(f"{label} {msg_id}", text, sender=sender, msg=msg))
    record_message_event(
        actor,
        msg,
        text=text,
        sender=sender,
        event_kind="new",
        direction="bot_in" if sender is not None and is_game_bot_sender(actor, sender) else "in",
        identity=current_id,
        command=command_from_log_label(label),
    )
    record_command_response_for_reply(actor, msg, text=text, status="matched")
    remember_logged_incoming_message(actor, msg, text=text)
    remember_incoming_message_context(actor, msg, command=command_from_log_label(label), identity=current_id, text=text)
    return True


def is_edited_message_for_current_account(actor, msg, text):
    """
    判定一条编辑过的消息是否是针对当前挂机账号的。
    通过双重判定防线保障：
      1. 显式名字匹配：消息文本中提到了本账号的别名/用户名。
      2. 隐式引用匹配：该消息引用回复的消息 ID 确实存在于我们自己已发送的指令 ID 缓存中。
    """
    if text_targets_current_account(actor, msg, text):
        return True
    
    # 检查引用回复
    replied_msg_id = meaningful_reply_to_msg_id(actor, msg)
    
    if replied_msg_id:
        sent_ids = getattr(actor, "_script_sent_message_ids", set())
        if replied_msg_id in sent_ids:
            return True
            
    return False


def match_pending_edited_feedback(
    actor,
    msg,
    text,
    candidate_fn,
    logger=None,
    id_window=30,
    max_age_seconds=150,
    label="[EDITED-FEEDBACK]",
):
    """Safely match a bot message to a pending command."""
    feedback_events = getattr(actor, "feedback_events", {}) or {}
    if not feedback_events:
        return False
    if is_reply_to_untracked_message(actor, msg):
        if logger:
            logger.info(
                f"{label} Message {getattr(msg, 'id', None)} replies to an untracked command, skipping."
            )
        return False
    if is_reply_to_manual_command(actor, msg):
        if logger:
            logger.info(f"{label} Manual command response detected (msg {getattr(msg, 'id', None)}), skipping.")
        return False
    msg_id = getattr(msg, "id", 0) or 0
    clean_text = str(text or "").strip()
    feedback_commands = getattr(actor, "feedback_commands", {}) or {}
    feedback_sent_ts = getattr(actor, "feedback_sent_ts", {}) or {}
    feedback_senders = getattr(actor, "feedback_senders", {}) or {}
    feedback_identities = getattr(actor, "feedback_identities", {}) or {}

    for mid, evt in reversed(list(feedback_events.items())):
        if evt.is_set():
            continue
        command = feedback_commands.get(mid, "")
        sent_ts = feedback_sent_ts.get(mid, 0)
        if sent_ts and time.monotonic() - sent_ts > max_age_seconds:
            continue
        if msg_id <= mid or msg_id - mid > id_window:
            continue
        command_sender_id = feedback_senders.get(mid)
        actual_sender_id = getattr(msg, "sender_id", None)
        if command_sender_id is not None and actual_sender_id == command_sender_id:
            continue
        if clean_text == str(command or "").strip():
            continue
        identity = feedback_identities.get(mid, "主魂")
        if mentions_other_user_for_identity(actor, msg, text, identity):
            if logger:
                logger.info(f"{label} Message {getattr(msg, 'id', None)} targets another user for [{identity}], skipping.")
            continue
        if _profile_command_key(command) and not profile_text_matches_identity(actor, clean_text, identity):
            if logger:
                logger.info(
                    f"{label} Message {getattr(msg, 'id', None)} profile username mismatches [{identity}], skipping."
                )
            continue
        if command in {".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关"} and not _meditation_feedback_matches_identity(actor, clean_text, identity):
            if logger:
                logger.info(
                    f"{label} Message {getattr(msg, 'id', None)} meditation username mismatches [{identity}], skipping."
                )
            continue
        if feedback_response_conflicts(command, clean_text) and not feedback_response_matches_command(command, clean_text):
            if logger:
                logger.info(
                    f"{label} Message {getattr(msg, 'id', None)} conflicts with [{command}], skipping."
                )
            continue
        if feedback_response_requires_positive_match(command) and not feedback_response_matches_command(command, clean_text):
            if logger:
                logger.info(
                    f"{label} Message {getattr(msg, 'id', None)} unrelated to [{command}], skipping."
                )
            continue
        if not candidate_fn(command, text):
            continue
        actor.last_feedback_text[mid] = text
        actor.last_feedback_msg[mid] = msg
        evt.set()
        if logger:
            logger.info(f"{label} Loose matched [{command}] message {msg_id} to command {mid}.")
        return True
    return False


# =====================================================================
# 11. 已记录消息去重
# =====================================================================

def remember_manual_reply_logged_message(actor, msg, text=None):
    """Mark a manual command reply that was already written by the manual path."""
    msg_id = _message_id(msg)
    if msg_id is None:
        return
    cache = getattr(actor, "_manual_reply_logged_message_ids", None)
    if cache is None:
        cache = {}
        setattr(actor, "_manual_reply_logged_message_ids", cache)
    cache[msg_id] = {"ts": time.monotonic(), "text": text if text is not None else (getattr(msg, "text", None) or "")}
    if len(cache) > 300:
        items = sorted(cache.items(), key=lambda item: item[1].get("ts", 0))
        setattr(actor, "_manual_reply_logged_message_ids", dict(items[-150:]))


def was_manual_reply_logged_message(actor, msg):
    """Return True when the normal mention logger should skip a manual reply."""
    msg_id = _message_id(msg)
    if msg_id is None:
        return False
    cache = getattr(actor, "_manual_reply_logged_message_ids", None) or {}
    return msg_id in cache


def remember_incoming_message_context(actor, msg, command="", identity="", text=None):
    """Remember command/identity for a bot message so later edits can sync state."""
    msg_id = _message_id(msg)
    if msg_id is None:
        return
    contexts = getattr(actor, "_incoming_message_contexts", None)
    if contexts is None:
        contexts = {}
        setattr(actor, "_incoming_message_contexts", contexts)
    contexts[msg_id] = {
        "command": str(command or "").strip(),
        "identity": str(identity or "").strip() or "主魂",
        "text": text if text is not None else (getattr(msg, "text", None) or ""),
        "ts": time.monotonic(),
    }
    if len(contexts) > 500:
        items = sorted(contexts.items(), key=lambda item: item[1].get("ts", 0))
        setattr(actor, "_incoming_message_contexts", dict(items[-250:]))


def incoming_message_context_for_msg(actor, msg):
    msg_id = _message_id(msg)
    if msg_id is None:
        return {}
    contexts = getattr(actor, "_incoming_message_contexts", None) or {}
    return contexts.get(msg_id, {}) or {}


def remember_logged_incoming_message(actor, msg, text=None):
    """记录已写日志的消息 ID，用于编辑检测"""
    msg_id = _message_id(msg)
    if msg_id is None:
        return
    cache = getattr(actor, "_logged_incoming_message_ids", None)
    if cache is None:
        cache = {}
        setattr(actor, "_logged_incoming_message_ids", cache)
    cache[msg_id] = {"ts": time.monotonic(), "text": text if text is not None else (getattr(msg, "text", None) or "")}
    if len(cache) > 500:
        items = sorted(cache.items(), key=lambda item: item[1].get("ts", 0))
        setattr(actor, "_logged_incoming_message_ids", dict(items[-250:]))


def was_logged_incoming_message(actor, msg):
    """检查消息是否已被记录过"""
    msg_id = _message_id(msg)
    if msg_id is None:
        return False
    cache = getattr(actor, "_logged_incoming_message_ids", None) or {}
    return msg_id in cache


def log_edited_text_once(actor, msg, text=None, sender=None):
    """记录编辑过的消息（每条消息每个版本的文本只记录一次）"""
    text = text if text is not None else (getattr(msg, "text", None) or "")
    msg_id = _message_id(msg)
    record_message_event(
        actor,
        msg,
        text=text,
        sender=sender,
        event_kind="edited",
        direction="bot_edited" if sender is not None and is_game_bot_sender(actor, sender) else "edited",
        identity=tracked_command_identity_for_reply(actor, msg),
        command=tracked_command_text_for_reply(actor, msg),
    )
    record_command_response_for_reply(actor, msg, text=text, status="edited", logger=logging.getLogger(actor.__class__.__name__))
    cache_name = "_logged_edited_message_texts"
    seen_texts = getattr(actor, cache_name, None)
    if seen_texts is None:
        seen_texts = {}
        setattr(actor, cache_name, seen_texts)
    if msg_id is not None and seen_texts.get(msg_id) == text:
        return True
    if msg_id is not None:
        seen_texts[msg_id] = text
        if len(seen_texts) > 300:
            setattr(actor, cache_name, dict(list(seen_texts.items())[-150:]))
    logging.getLogger(actor.__class__.__name__).info(format_in_log(f"edited {msg_id}", text, sender=sender, msg=msg))
    remember_logged_incoming_message(actor, msg, text=text)
    return True


def record_edited_cultivation_state_if_needed(actor, msg, text=None, sender=None, logger=None):
    """Sync cultivation profile/deltas from bot-edited messages when identity is reliable."""
    text = text if text is not None else (getattr(msg, "text", None) or "")
    if "修为" not in str(text or "") and "境界" not in str(text or ""):
        return False
    logger = logger or logging.getLogger(actor.__class__.__name__)
    if is_reply_to_untracked_message(actor, msg):
        logger.info(f"Edited cultivation sync skipped: reply_to is not tracked (msg {getattr(msg, 'id', None)}).")
        return False
    identity = ""
    command = ""

    replied_id = meaningful_reply_to_msg_id(actor, msg)
    if replied_id:
        command = tracked_command_text_for_reply(actor, msg)
        identity = tracked_command_identity_for_reply(actor, msg)

    context = incoming_message_context_for_msg(actor, msg)
    if context:
        if not command:
            command = context.get("command", "")
        if not identity:
            identity = context.get("identity", "")

    if not identity:
        identity = identity_from_single_username_mention(actor, text)
        if identity and not command:
            command = "username"

    if not identity:
        identity = recent_profile_identity_for_text(actor, text, msg_id=_message_id(msg))
        if identity and not command:
            command = "profile"

    if not identity:
        return False

    if mentions_other_user_for_identity(actor, msg, text, identity) and not profile_username_from_text(text):
        logger.info(f"Edited cultivation sync skipped: targets another user (msg {getattr(msg, 'id', None)}).")
        return False
    if not profile_text_matches_identity(actor, text, identity):
        logger.info(f"Edited cultivation sync skipped: profile username mismatches [{identity}] (msg {getattr(msg, 'id', None)}).")
        return False

    source = f"edited {command}".strip()
    profile_done = record_cultivation_profile_from_text(
        actor, text, identity=identity, logger=logger, source=source
    )
    delta_done = record_cultivation_delta_from_text(
        actor, text, identity=identity, logger=logger, source=source, msg=msg
    )
    return bool(profile_done or delta_done)


def is_relevant_game_bot_edited_message(actor, msg, text):
    """Edited bot messages are relevant when they mention us or continue a tracked reply."""
    if is_reply_to_untracked_message(actor, msg):
        return False
    if mentions_self(actor, msg, text):
        return True
    if was_logged_incoming_message(actor, msg):
        return True
    if is_reply_to_tracked_command(actor, msg):
        return True
    if identity_from_single_username_mention(actor, text):
        return True
    if recent_profile_identity_for_text(actor, text, msg_id=_message_id(msg), consume=False):
        return True
    return False


async def log_edited_message_if_needed(actor, event):
    """
    MessageEdited 事件处理器。
    检测游戏机器人编辑消息时，记录编辑后的内容。
    只记录：
    - 提到了本账号的编辑
    - 之前已记录过的游戏机器人消息的编辑
    """
    try:
        msg = event.message
        text = msg.text or ""
        sender = await event.get_sender()
        is_game_bot = is_game_bot_sender(actor, sender)
        logger = logging.getLogger(actor.__class__.__name__)
        record_message_event(
            actor,
            msg,
            text=text,
            sender=sender,
            event_kind="edited",
            direction="bot_edited" if is_game_bot else "edited",
            logger=logger,
        )
        if is_game_bot:
            record_game_bot_activity(actor, sender, logger)
        if is_game_bot and is_relevant_game_bot_edited_message(actor, msg, text):
            record_edited_cultivation_state_if_needed(actor, msg, text=text, sender=sender, logger=logger)
            return log_edited_text_once(actor, msg, text=text, sender=sender)
        if mentions_self(actor, msg, text):
            return log_edited_text_once(actor, msg, text=text, sender=sender)
        return False
    except Exception as e:
        logging.getLogger(actor.__class__.__name__).error(f"Edited message log error: {e}")
        return False


# =====================================================================
# 12. 日志文件管理
# =====================================================================

def _line_allowed(line):
    """判断日志行是否应该保留（过滤冗余 INFO 行只保留指令出入）"""
    if " [WARNING] " in line or " [ERROR] " in line or " [CRITICAL] " in line:
        return True
    return any(marker in line for marker in [
        "] 🟢 OUT", "] 🔵 IN [", "] ⬆️ OUT", "] ⬇️ IN [",
        "] 📤 OUT", "] 📥 IN [", "] OUT", "] IN [",
    ])


def prune_log_file(path, hours=LOG_RETENTION_HOURS):
    """
    清理超出保留时间的日志行。
    旧日志中只保留 WARNING 及以上级别和指令出入行。
    """
    if not os.path.exists(path):
        return
    cutoff = datetime.now() - timedelta(hours=hours)
    kept = []
    current_keep = True
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = _TS_RE.match(line)
                if m:
                    try:
                        current_keep = (datetime.strptime(m.group(1), TIME_FORMAT) >= cutoff and _line_allowed(line))
                    except ValueError:
                        current_keep = _line_allowed(line)
                if current_keep:
                    kept.append(line)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception:
        logging.getLogger(__name__).exception("Failed to prune log file %s", path)


async def periodic_log_prune(path, hours=LOG_RETENTION_HOURS, interval=3600):
    """定期清理日志文件（每小时执行一次）"""
    while True:
        prune_log_file(path, hours=hours)
        await asyncio.sleep(interval)
