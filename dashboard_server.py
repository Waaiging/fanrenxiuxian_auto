"""
【面板服务器模块 —— Web 管理界面后端】

基于 FastAPI 的 Web 面板，提供修仙脚本的图形化管理界面。
功能：
  1. 账号状态查看 —— 实时查看三个账号的运行状态、修为进度
  2. 日志浏览 —— 按指令标签分类/搜索浏览日志
  3. 修为统计 —— 自动从日志中提取修为变化，按日统计
  4. 进程管理 —— 启动/停止/重启账号脚本
  5. 清屏功能 —— 后台清理账号发出的消息

通过 HTTP Basic 认证保护，运行在 0.0.0.0:8000。
"""
import os
import json
import time
import subprocess
import secrets
import sys
import threading
import uuid
import re
import signal
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, status as http_status, Body
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from log_utils import command_control_key
from command_modules import (
    ASK_DAO_COMMAND,
    NURTURE_SPIRIT_COMMAND,
    RIFT_SEARCH_COMMAND,
    YUANYING_OUT_COMMAND,
    field_training_plan_from_features,
    treasure_touch_plan,
    yuanying_out_plan,
)
from fishing_features import FISHING_DAILY_LIMIT, FISHING_MASTER_COMMAND
from yinluo_features import YINLUO_CONVERT_COMMAND, YINLUO_IDENTITY, YINLUO_MASTER_COMMAND, YINLUO_SOUL

app = FastAPI()
security = HTTPBasic()

# =====================================================================
# 安全配置（HTTP Basic 认证）
# =====================================================================
USER_NAMES = ("wg", "admin")       # 允许登录的用户名
USER_PWD = "REMOVED_DASHBOARD_PASSWORD"           # 密码

def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    """HTTP Basic 认证验证"""
    correct_username = any(secrets.compare_digest(credentials.username, name) for name in USER_NAMES)
    correct_password = secrets.compare_digest(credentials.password, USER_PWD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="暗号不对，道友请留步",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def safe_console_print(*args, **kwargs):
    """Best-effort console output for background jobs."""
    try:
        print(*args, **kwargs)
    except (BrokenPipeError, OSError):
        pass

# =====================================================================
# 路径与常量配置
# =====================================================================
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAR_JOBS = {}                          # 清屏任务状态
CLEAR_LOCK = threading.Lock()            # 清屏任务锁
COMMAND_CONTROL_LOCK = threading.Lock()  # 指令开关锁
CUSTOM_COMMAND_LOCK = threading.Lock()   # 自定义指令锁
STATUS_CACHE = {}                        # Dashboard 总状态缓存
STATUS_LOCK = threading.Lock()           # Dashboard 总状态锁
LOG_PAGE_CACHE = {}                      # 日志分页接口短缓存
LOG_PAGE_LOCK = threading.Lock()         # 日志分页接口锁
CULTIVATION_CACHE = {}                   # 修为统计缓存
CULTIVATION_LOCK = threading.Lock()      # 修为统计锁
COMMAND_RECORD_CACHE = {}                # 指令发送记录缓存
COMMAND_RECORD_LOCK = threading.Lock()   # 指令发送记录锁
COMMAND_RECORD_ENDPOINT_CACHE = {}       # 指令发送记录接口短缓存
COMMAND_RECORD_ENDPOINT_LOCK = threading.Lock()
MESSAGE_HEALTH_CACHE = {}                # 消息采集健康缓存
MESSAGE_HEALTH_LOCK = threading.Lock()   # 消息采集健康锁
RESOURCE_STATS_CACHE = {}                # 资源/库存统计缓存
RESOURCE_STATS_LOCK = threading.Lock()   # 资源/库存统计锁
RESOURCE_STATS_BUILD_LOCK = threading.Lock()
CULTIVATION_CACHE_FILE = "cultivation_stats_cache.json"
COMMAND_CONTROL_FILE = "command_controls.json"
CUSTOM_COMMAND_FILE = "dashboard_commands.json"
MESSAGE_EVENTS_DB_FILE = "message_events.sqlite3"
DEPLOY_VERSION_FILE = "deploy_version.json"
MESSAGE_HEALTH_MAX_SCAN_IDS = 12000
STATUS_CACHE_SECONDS = 10
LOG_PAGE_CACHE_SECONDS = 5
COMMAND_RECORD_ENDPOINT_CACHE_SECONDS = 180
MESSAGE_HEALTH_CACHE_SECONDS = 30
RESOURCE_STATS_CACHE_SECONDS = 900
RESOURCE_STATS_MAX_ROWS = 400
CULTIVATION_STATS_VERSION = 14  # rebuilt: merge username-owned profile snapshots
LOG_TAIL_INITIAL_BYTES = 192 * 1024
LOG_TAIL_MAX_BYTES = 4 * 1024 * 1024
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SERVER_START_TS = time.time()
SERVER_STARTED_AT = datetime.fromtimestamp(SERVER_START_TS).strftime(TIME_FORMAT)
GIT_META_CACHE = {}
GIT_META_CACHE_SECONDS = 60
ACCOUNT_DISPLAY_NAMES = {"main": "凌霄宫 (主号)", "sub": "元婴宗 (副号)", "xiaohao": "万灵宗 (小号)"}
ALL_AVATARS = ["问心子", "素心子", "缘生子", "无咎子", "素缘子", "厚土", "寻真子"]
STAR_CONCUBINE_VOYAGE_IDENTITIES = {
    "main": {"素缘子"},
    "sub": {"厚土", "缘生子", "寻真子"},
    "xiaohao": {"素心子", "缘生子"},
}
CONCUBINE_VOYAGE_AUTO_START_ENABLED = True

ACCOUNT_PROFILE_USERNAMES = {
    "main": {
        "主魂": {"waaiging"},
        "无咎子": {"wuxinglinggen"},
        "缘生子": {"kulipabp"},
        "素缘子": {"oldeinstein"},
    },
    "sub": {
        "主魂": {"gamling33"},
        "厚土": {"crayonxxin"},
        "缘生子": {"lvdoumiao"},
        "寻真子": {"ding303"},
    },
    "xiaohao": {
        "主魂": {"titancreeper"},
        "问心子": {"lianqi10000"},
        "素心子": {"hajiimiii"},
        "缘生子": {"adai925"},
    },
}

def account_profile_usernames(account):
    """Return dashboard-safe username mapping for every identity in an account."""
    mapping = ACCOUNT_PROFILE_USERNAMES.get(account) or {}
    return {
        identity: sorted(
            f"@{normalize_profile_username(name)}"
            for name in names
            if normalize_profile_username(name)
        )
        for identity, names in mapping.items()
    }

PROFILE_USERNAME_PATTERNS = (
    re.compile(r"@([A-Za-z0-9_]{2,64})\s*的天命玉牒", re.I),
    re.compile(r"修士状态\s*[·.]\s*@([A-Za-z0-9_]{2,64})", re.I),
    re.compile(r"修士\s*@([A-Za-z0-9_]{2,64})\s*(?:深度闭关总结|闭关总结|元神归窍总结|元婴归窍总结)", re.I),
    re.compile(r"检测到\s*@([A-Za-z0-9_]{2,64})\s*功成圆满", re.I),
    re.compile(r"感应到\s*@([A-Za-z0-9_]{2,64})\s*的元婴", re.I),
)

# 日志解析正则
LOG_ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[[A-Z]+\]")
LOG_COMMAND_RE = re.compile(r"(?<![A-Za-z0-9_])(\.[\u4e00-\u9fffA-Za-z0-9_]+)(?:\s+([^\s`，,。:：)）]+))?")
LOG_OUT_IDENTITY_RE = re.compile(r"OUT(?:\s*\[([^\]]+)\])?\s*:")
LOG_LEVEL_RE = re.compile(r"(?:\*\*)?(?:当前)?境界(?:\*\*)?\s*[:：]\s*\**\s*([^\n\r*]+)")
LOG_CULTIVATION_RE = re.compile(r"(?:\*\*)?(?:当前)?修为(?:\*\*)?\s*[:：]\s*\**\s*([\d,]+)\s*/\s*\**\s*([\d,]+)")
LOG_SPIRIT_ROOT_RE = re.compile(r"(?:\*\*)?灵根(?:\*\*)?\s*[:：]\s*\**\s*([^\n\r*]+)")
MAIN_TREASURE_TOUCH_COMMAND = treasure_touch_plan(".抚摸法宝 玄天斩灵剑").command
SUB_TREASURE_TOUCH_COMMAND = treasure_touch_plan(".抚摸法宝 青竹蜂云剑").command
MAIN_FIELD_TRAINING_COMMAND = field_training_plan_from_features("主魂", main_command=".野外历练 谨慎").command
DEFAULT_AVATAR_FIELD_TRAINING_COMMAND = field_training_plan_from_features("缘生子").command
WUJIUZI_FIELD_TRAINING_COMMAND = field_training_plan_from_features(
    "无咎子",
    {
        "training_cmd": ".野外历练",
        "training_level": "深入",
    },
).command
SUB_MAIN_YUANYING_COMMAND = yuanying_out_plan("主魂", main_command=".元婴闭关").command

# 双段指令（指令+参数需要组合）
TWO_PART_COMMANDS = {
    (".交换", "法宝"), (".抚摸法宝", "青竹蜂云剑"), (".抚摸法宝", "斩灵"),
    (".抚摸法宝", "玄天斩灵剑"),
    (".推命", "探索"), (".改命", "探索"),
    (".野外历练", "谨慎"), (".野外历练", "深入"),
}
OTHER_LOG_TAG = "其他"                    # 未分类日志标签
RELATED_LOG_WINDOW_SECONDS = 180          # 关联日志窗口（秒）
CULTIVATION_DEDUPE_SECONDS = 15           # 修为变更去重窗口

# 游戏机器人标识（用于区分机器人回复和普通消息）
BOT_REPLY_MARKERS = {
    "fanrenxiuxian_bot", "@fanrenxiuxian_bot",
    "hantianzzz_bot", "@hantianzzz_bot",
    "hantianzz_bot", "@hantianzz_bot",
    "hantianz_bot", "@hantianz_bot",
    "hantianzunhl", "@hantianzunhl",
    "韩天尊",
}

# 每个账号的日志标签定义（对应不同的游戏指令）
ACCOUNT_LOG_TAGS = {
    "main": [
        ".闯塔", ".借天门势", ".宗门点卯", ".宗门传功",
        ".登天阶", ".天阶状态", ".引九天罡风", ".问心台",
        ".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关",
        ".召回侍妾", ".安置侍妾", YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        MAIN_TREASURE_TOUCH_COMMAND,
        DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, MAIN_FIELD_TRAINING_COMMAND, WUJIUZI_FIELD_TRAINING_COMMAND, ".宗门战况", ".参战", ".我的侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        ".推命 探索", ".改命 探索",
        OTHER_LOG_TAG,
    ],
    "sub": [
        ".闯塔", ".宗门点卯", ".宗门传功", ASK_DAO_COMMAND,
        ".启阵", ".助阵", ".强行出关",
        ".查看闭关", ".闭关修炼", ".深度闭关",
        ".召回侍妾", ".安置侍妾", ".每日问安",
        ".观星台", ".安抚星辰", ".收集精华", ".牵引星辰", ".观星", ".改换星移",
        SUB_MAIN_YUANYING_COMMAND, YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        SUB_TREASURE_TOUCH_COMMAND,
        DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, MAIN_FIELD_TRAINING_COMMAND, ".野外历练 均衡", ".宗门战况", ".参战", ".我的侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        OTHER_LOG_TAG,
    ],
    "xiaohao": [
        ".闯塔", ".宗门点卯", ".宗门传功",
        ".寻觅灵兽", ".我的灵兽", ".放生", ".灵兽出战", ".灵兽休息",
        ".灵兽偷菜", ".灵兽探渊", ".一键放养", ".灵兽互动", ".灵兽巡游",
        ".查看闭关", ".闭关修炼", ".深度闭关", ".召回侍妾", ".安置侍妾",
        DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, MAIN_FIELD_TRAINING_COMMAND, ".宗门战况", ".参战", SUB_TREASURE_TOUCH_COMMAND,
        YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        ".观星台", ".安抚星辰", ".收集精华", ".牵引星辰",
        ".我的侍妾", ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        OTHER_LOG_TAG,
    ],
}

# 脚本文件名 -> tmux 窗口编号 映射
SCRIPT_MAP = {"main": "intelligent_cultivator.py", "sub": "sub_cultivator.py", "xiaohao": "cultivator_xiaohao.py"}
WINDOW_MAP = {"main": 0, "sub": 1, "xiaohao": 2}


# =====================================================================
# 状态管理
# =====================================================================

def get_state(name):
    """读取账号的状态 JSON 文件，预处理显示字段"""
    path = os.path.join(CONFIG_DIR, f'state_{name}.json')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("deep_meditation_end_time"):
                    from datetime import datetime
                    end_time = data["deep_meditation_end_time"]
                    if datetime.strptime(end_time, TIME_FORMAT) > datetime.now():
                        data["in_deep_meditation"] = True
                apply_common_display_times(data)
                if name == "sub":
                    apply_sub_display_times(data)
                return data
        except:
            return {}
    return {}

def apply_common_display_times(data):
    """处理通用冷却时间的展示（宗门战参战时间）"""
    from datetime import datetime
    data["display_next_sect_war_join_time"] = data.get("next_sect_war_join_time", "")
    active_until = data.get("sect_war_active_until", "")
    next_join = data.get("next_sect_war_join_time", "")
    if not active_until:
        return
    try:
        active_dt = datetime.strptime(active_until, TIME_FORMAT)
        now = datetime.now()
        if active_dt <= now:
            data["display_next_sect_war_join_time"] = "---"
            return
        if not next_join:
            data["display_next_sect_war_join_time"] = "就绪"
            return
        next_dt = datetime.strptime(next_join, TIME_FORMAT)
        if next_dt >= active_dt:
            data["display_next_sect_war_join_time"] = "本轮已无下一次"
    except Exception:
        return

def apply_sub_display_times(data):
    """处理星宫特有时间的展示（牵引星辰的 check_time 覆盖）"""
    from datetime import datetime
    attr_time = data.get("next_star_attraction_time", "")
    check_time = data.get("next_star_check_time", "")
    data["display_next_star_attraction_time"] = attr_time
    try:
        if not attr_time or not check_time:
            return
        attr_dt = datetime.strptime(attr_time, TIME_FORMAT)
        check_dt = datetime.strptime(check_time, TIME_FORMAT)
        if check_dt > attr_dt and check_dt > datetime.now():
            data["display_next_star_attraction_time"] = check_time
    except Exception:
        return


def command_control_path():
    return os.path.join(CONFIG_DIR, COMMAND_CONTROL_FILE)


def custom_command_path():
    return os.path.join(CONFIG_DIR, CUSTOM_COMMAND_FILE)


def load_command_controls():
    path = command_control_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_command_controls(data):
    path = command_control_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_custom_commands():
    path = custom_command_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_custom_commands(data):
    path = custom_command_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clean_custom_text(value, limit=120):
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[:limit].strip()
    return text


def clean_custom_int(value, default=0, min_value=0, max_value=100000):
    try:
        number = int(float(value))
    except Exception:
        number = default
    return max(min_value, min(max_value, number))


def custom_command_interval_minutes(entry):
    try:
        minutes = float(entry.get("interval_minutes") or 0)
    except Exception:
        return 0
    return max(0, minutes)


def custom_command_enabled(entry):
    return bool(entry.get("schedule_enabled") is not False and custom_command_interval_minutes(entry) > 0)


def custom_command_row(entry, root_state=None):
    command = clean_custom_text(entry.get("command"), 160)
    if not command:
        return None
    label = clean_custom_text(entry.get("label"), 64) or clean_custom_text(command, 64)
    group = clean_custom_text(entry.get("group"), 32) or "自定义"
    detail = clean_custom_text(entry.get("detail"), 160) or "dashboard 手动添加"
    custom_id = clean_custom_text(entry.get("id"), 64)
    enabled = custom_command_enabled(entry)
    minutes = custom_command_interval_minutes(entry)
    runs = (root_state or {}).get("custom_command_runs", {})
    run_state = runs.get(custom_id, {}) if isinstance(runs, dict) else {}
    next_run_at = clean_custom_text(run_state.get("next_run_at"), 32)
    last_status = clean_custom_text(run_state.get("last_status"), 32)
    last_run_at = clean_custom_text(run_state.get("last_run_at"), 32)
    last_response = clean_custom_text(run_state.get("last_response"), 80)
    if enabled:
        target = parse_state_time(next_run_at)
        if target and target > datetime.now():
            status = "冷却中"
            tone = "cooldown"
            next_seconds = max(0, int((target - datetime.now()).total_seconds()))
            remaining = format_remaining(next_seconds)
            at = next_run_at
        else:
            status = "待执行"
            tone = "ready"
            remaining = "0秒"
            at = next_run_at
            next_seconds = 0
        schedule_detail = f"每 {minutes:g} 分钟"
        if last_status:
            schedule_detail += f" · 上次 {last_status}"
        if last_response:
            schedule_detail += f" · {last_response}"
        detail = f"{schedule_detail}{f' · {detail}' if detail else ''}"
    else:
        status = "未启用"
        tone = "manual"
        remaining = "--"
        at = last_run_at
        detail = f"未设置自动间隔{f' · {detail}' if detail else ''}"
    row = command_row(command, label, status, tone, remaining=remaining, at=at, detail=detail, group=group)
    if enabled:
        row["schedule_type"] = "cooldown"
        row["next_seconds"] = next_seconds
    row["custom"] = True
    row["custom_id"] = custom_id
    row["created_at"] = clean_custom_text(entry.get("created_at"), 32)
    row["schedule_enabled"] = enabled
    row["interval_minutes"] = minutes
    row["timeout_seconds"] = entry.get("timeout_seconds", 45)
    row["max_retries"] = entry.get("max_retries", 0)
    return row


def append_custom_commands(account, panel, custom_commands, root_state=None):
    account_data = custom_commands.get(account, {}) if isinstance(custom_commands, dict) else {}
    if not isinstance(account_data, dict):
        return panel
    identity = panel.get("identity") or "主魂"
    entries = account_data.get(identity, [])
    if not isinstance(entries, list):
        return panel
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        row = custom_command_row(entry, root_state=root_state)
        if row:
            rows.append(row)
    if rows:
        panel.setdefault("commands", []).extend(rows)
    return panel


def command_control_disabled(controls, account, identity, control_key, default_disabled=False):
    account_controls = controls.get(account, {}) if isinstance(controls, dict) else {}
    if not isinstance(account_controls, dict):
        return bool(default_disabled)
    for ident in (identity or "主魂", "*"):
        ident_controls = account_controls.get(ident, {})
        if not isinstance(ident_controls, dict):
            continue
        if control_key not in ident_controls:
            continue
        entry = ident_controls.get(control_key)
        if isinstance(entry, dict):
            return bool(entry.get("disabled"))
        return bool(entry)
    return bool(default_disabled)


def apply_command_controls(account, panel):
    controls = load_command_controls()
    identity = panel.get("identity") or "主魂"
    commands = panel.get("commands") or []
    for row in commands:
        control_key = command_control_key(row.get("command", ""))
        row["control_key"] = control_key
        row["control_disabled"] = command_control_disabled(
            controls,
            account,
            identity,
            control_key,
            default_disabled=bool(row.get("default_paused")),
        )
        if row["control_disabled"]:
            row["status"] = "已暂停"
            row["tone"] = "paused"
            detail = row.get("detail", "")
            row["detail"] = f"dashboard 临时暂停{f' · {detail}' if detail else ''}"
    return panel


# =====================================================================
# 指令面板聚合
# =====================================================================

def parse_state_time(value):
    """Parse a state timestamp in the common project format."""
    if not value or value in ("0", 0, "---", "本轮已无下一次", "就绪"):
        return None
    if isinstance(value, (int, float)):
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def format_remaining(delta_seconds):
    """Return a compact Chinese remaining-time string."""
    seconds = max(0, int(delta_seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}天 {hours}时"
    if hours:
        return f"{hours}时 {minutes}分"
    if minutes:
        return f"{minutes}分 {seconds}秒"
    return f"{seconds}秒"


def command_row(
    command,
    label=None,
    status="未记录",
    tone="unknown",
    remaining="",
    at="",
    detail="",
    group="",
    schedule_type="",
    next_seconds=None,
    default_paused=False,
):
    """Build one command-row object for the dashboard."""
    row = {
        "command": command,
        "label": label or command,
        "status": status,
        "tone": tone,
        "remaining": remaining,
        "at": at or "",
        "detail": detail or "",
        "group": group or "",
    }
    if schedule_type:
        row["schedule_type"] = schedule_type
    if next_seconds is not None:
        row["next_seconds"] = max(0, int(next_seconds))
    if default_paused:
        row["default_paused"] = True
    return row


def time_command(state, key, command, label=None, waiting="冷却中", ready="就绪", missing="就绪", detail="", group=""):
    """Display a timestamp-backed command as cooldown/ready."""
    raw = state.get(key, "")
    if raw in ("就绪",):
        return command_row(command, label, "就绪", "ready", remaining="0秒", detail=detail, group=group, schedule_type="cooldown", next_seconds=0)
    if raw == "本轮已无下一次":
        return command_row(command, label, "本轮结束", "done", at=raw, detail=detail, group=group, schedule_type="cooldown")
    if raw == "---":
        return command_row(command, label, "未开启", "unknown", at=raw, detail=detail, group=group, schedule_type="cooldown")
    target = parse_state_time(raw)
    if not target:
        next_seconds = 0 if missing == "就绪" else None
        return command_row(
            command, label, missing, "ready" if missing == "就绪" else "unknown",
            remaining="0秒" if missing == "就绪" else "",
            at=str(raw or ""), detail=detail, group=group,
            schedule_type="cooldown", next_seconds=next_seconds,
        )
    now = datetime.now()
    if target > now:
        next_seconds = max(0, int((target - now).total_seconds()))
        return command_row(command, label, waiting, "cooldown", format_remaining(next_seconds), str(raw), detail, group, schedule_type="cooldown", next_seconds=next_seconds)
    return command_row(command, label, ready, "ready", "0秒", str(raw), detail, group, schedule_type="cooldown", next_seconds=0)


def yuanying_retreat_command(state):
    """Display sub main-soul .元婴闭关, whose active reply may not include an end time."""
    raw = state.get("yuanying_out_end_time") or state.get("next_yuanying_out_time", "")
    target = parse_state_time(raw)
    if state.get("yuanying_out_active"):
        if target and target > datetime.now():
            next_seconds = max(0, int((target - datetime.now()).total_seconds()))
            return command_row(
                SUB_MAIN_YUANYING_COMMAND,
                "元婴闭关",
                "闭关中",
                "active",
                format_remaining(next_seconds),
                str(raw),
                "等待自动结算",
                "通用",
                schedule_type="cooldown",
                next_seconds=next_seconds,
            )
        detail = "机器人未返回剩余时间，等待下次发言自动结算"
        last_return = state.get("last_yuanying_return_time", "")
        if last_return:
            detail = f"{detail} · 上次结算 {last_return}"
        return command_row(
            SUB_MAIN_YUANYING_COMMAND,
            "元婴闭关",
            "闭关中",
            "active",
            "等结算",
            str(raw or ""),
            detail,
            "通用",
            schedule_type="cooldown",
        )
    return time_command(state, "next_yuanying_out_time", SUB_MAIN_YUANYING_COMMAND, "元婴闭关", group="通用")


def active_until_command(state, key, command, label=None, active="生效中", ready="未生效", detail="", group=""):
    """Display a buff/event active-until timestamp."""
    raw = state.get(key, "")
    target = parse_state_time(raw)
    if target and target > datetime.now():
        return command_row(command, label, active, "active", format_remaining((target - datetime.now()).total_seconds()), str(raw), detail, group)
    if raw:
        return command_row(command, label, "已结束", "done", at=str(raw), detail=detail, group=group)
    return command_row(command, label, ready, "unknown", detail=detail, group=group)


def daily_done_command(state, command, label=None, date_key="", done_command="", detail="", group=""):
    """Display a daily command from either the done list or a last_*_date field."""
    today = datetime.now().strftime("%Y-%m-%d")
    done = set(state.get("done", []) or [])
    if done_command and done_command in done:
        return command_row(command, label, "今日已执行", "done", remaining="今日", at=today, detail=detail, group=group, schedule_type="daily")
    if date_key:
        value = str(state.get(date_key, "") or "")
        if value == today:
            return command_row(command, label, "今日已执行", "done", remaining="今日", at=value, detail=detail, group=group, schedule_type="daily")
        if value:
            return command_row(command, label, "今日未执行", "ready", remaining="待执行", at=value, detail=detail, group=group, schedule_type="daily", next_seconds=0)
    return command_row(command, label, "今日未执行", "ready", remaining="待执行", detail=detail, group=group, schedule_type="daily", next_seconds=0)


def watch_command(command, label=None, detail="同步/记录回复", group=""):
    return command_row(command, label, "监听中", "watch", detail=detail, group=group)


def manual_command(command, label=None, detail="按需发送", group=""):
    return command_row(command, label, "按需", "manual", detail=detail, group=group)


def flow_command(command, label=None, detail="流程内自动发送", group=""):
    return command_row(command, label, "流程内", "flow", detail=detail, group=group)


def fishing_command(state):
    fishing = state.get("fishing", {}) if isinstance(state, dict) else {}
    if not isinstance(fishing, dict):
        fishing = {}
    today_count = int(fishing.get("today_count") or 0)
    daily_limit = int(fishing.get("daily_limit") or FISHING_DAILY_LIMIT)
    detail_parts = [f"今日 {today_count}/{daily_limit}", f"饵料 {FISHING_MASTER_COMMAND.split()[-1]}"]
    current_nest = str(fishing.get("current_nest") or "").strip()
    nest_remaining = int(fishing.get("current_nest_remaining") or 0)
    if current_nest and nest_remaining > 0:
        detail_parts.append(f"{current_nest} 剩余 {nest_remaining}竿")
    last_detail = str(fishing.get("last_detail") or "").strip()
    if last_detail:
        detail_parts.append(last_detail)

    active_due = parse_state_time(fishing.get("active_due_at", ""))
    if fishing.get("active") and active_due and active_due > datetime.now():
        next_seconds = max(0, int((active_due - datetime.now()).total_seconds()))
        return command_row(
            FISHING_MASTER_COMMAND,
            "钓鱼",
            "等鱼讯",
            "cooldown",
            format_remaining(next_seconds),
            str(fishing.get("active_due_at") or ""),
            " · ".join(detail_parts),
            "钓鱼",
            schedule_type="cooldown",
            next_seconds=next_seconds,
            default_paused=True,
        )

    next_action = parse_state_time(fishing.get("next_action_at", ""))
    if next_action and next_action > datetime.now():
        next_seconds = max(0, int((next_action - datetime.now()).total_seconds()))
        status = "今日已满" if fishing.get("last_status") == "daily_done" else "等待中"
        return command_row(
            FISHING_MASTER_COMMAND,
            "钓鱼",
            status,
            "cooldown",
            format_remaining(next_seconds),
            str(fishing.get("next_action_at") or ""),
            " · ".join(detail_parts),
            "钓鱼",
            schedule_type="cooldown",
            next_seconds=next_seconds,
            default_paused=True,
        )

    status_map = {
        "caught": "提竿成功",
        "empty": "空竿",
        "synced": "已校准",
        "bait_bought": "已买饵",
        "nested": "已打窝",
        "fishing": "等鱼讯",
        "yielding": "让路中",
        "meditation_blocked": "闭关中",
        "no_rod": "无鱼竿",
        "daily_done": "今日已满",
        "paused": "已暂停",
    }
    last_status = str(fishing.get("last_status") or "paused")
    return command_row(
        FISHING_MASTER_COMMAND,
        "钓鱼",
        status_map.get(last_status, "就绪"),
        "ready" if last_status not in {"no_rod", "paused"} else "unknown",
        "0秒",
        str(fishing.get("next_action_at") or ""),
        " · ".join(detail_parts),
        "钓鱼",
        schedule_type="cooldown",
        next_seconds=0,
        default_paused=True,
    )


def yinluo_commands(state):
    yinluo = state.get("yinluo", {}) if isinstance(state, dict) else {}
    if not isinstance(yinluo, dict):
        yinluo = {}
    sha_current = int(yinluo.get("sha_current") or 0)
    sha_max = int(yinluo.get("sha_max") or 0)
    reserves = yinluo.get("reserves", {}) if isinstance(yinluo.get("reserves"), dict) else {}
    slots = yinluo.get("slots", {}) if isinstance(yinluo.get("slots"), dict) else {}
    fierce = int(reserves.get(YINLUO_SOUL, 0) or 0)
    empty = sum(1 for item in slots.values() if isinstance(item, dict) and item.get("status") == "空闲")
    ready = sum(1 for item in slots.values() if isinstance(item, dict) and item.get("status") == "精华已成")
    exhausted = sum(1 for item in slots.values() if isinstance(item, dict) and "魂力枯竭" in str(item.get("status") or ""))
    detail = f"煞气 {sha_current}/{sha_max} · {YINLUO_SOUL} {fierce} · 空槽 {empty} · 可收 {ready}"
    if exhausted:
        detail += f" · 枯竭 {exhausted}"
    if yinluo.get("rank"):
        detail += f" · {yinluo.get('rank')}"
    if yinluo.get("last_detail"):
        detail += f" · {yinluo.get('last_detail')}"

    status_map = {
        "init": "待校准",
        "synced": "已校准",
        "yielding": "让路中",
        "meditation_blocked": "闭关中",
        "sacrificed": "已献祭",
        "sacrifice_done": "今日已献祭",
        "blood_wash": "血洗完成",
        "blood_wash_cd": "血洗冷却",
        "summoned": "魔影成功",
        "summon_cd": "魔影冷却",
        "converted": "已化煞",
        "imprisoned": "炼化中",
        "collected": "已收取",
        "appeased": "已安抚",
    }
    next_action = parse_state_time(yinluo.get("next_action_at", ""))
    tone = "ready"
    remaining = "0秒"
    next_seconds = 0
    if next_action and next_action > datetime.now():
        tone = "cooldown"
        next_seconds = max(0, int((next_action - datetime.now()).total_seconds()))
        remaining = format_remaining(next_seconds)
    rows = [
        command_row(
            YINLUO_MASTER_COMMAND,
            "阴罗幡",
            status_map.get(str(yinluo.get("last_status") or "init"), "就绪"),
            tone,
            remaining,
            str(yinluo.get("next_action_at") or ""),
            detail,
            "阴罗宗",
            schedule_type="cooldown",
            next_seconds=next_seconds,
        )
    ]
    rows.append(daily_done_command(
        yinluo,
        ".每日献祭",
        "每日献祭",
        date_key="last_daily_sacrifice_date",
        detail=f"煞气池 +500，当前 {sha_current}/{sha_max}",
        group="阴罗宗",
    ))
    rows.append(time_command(yinluo, "next_blood_wash_time", ".血洗山林", "血洗山林", group="阴罗宗"))
    rows.append(time_command(yinluo, "next_summon_shadow_time", ".召唤魔影", "召唤魔影", group="阴罗宗"))
    rows.append(command_row(".一键收取精华", "收取精华", "可收取" if ready else "按需", "ready" if ready else "manual", detail=f"精华已成槽 {ready}", group="阴罗宗"))
    rows.append(command_row(f".囚禁魂魄 <槽位> {YINLUO_SOUL}", "囚禁凶兽", "可炼化" if empty and fierce else "等待", "ready" if empty and fierce else "manual", detail=f"只囚禁{YINLUO_SOUL}；空槽 {empty}，储备 {fierce}", group="阴罗宗"))
    rows.append(command_row(YINLUO_CONVERT_COMMAND, "化功为煞", "煞气不足时", "manual", detail="仅囚禁凶兽戾魄且煞气不足时自动使用", group="阴罗宗"))
    return rows


def deep_meditation_command(state, command=".深度闭关", label="深度闭关", group="闭关"):
    raw = state.get("deep_meditation_end_time", "")
    target = parse_state_time(raw)
    if state.get("in_deep_meditation") and target and target > datetime.now():
        next_seconds = max(0, int((target - datetime.now()).total_seconds()))
        return command_row(command, label, "闭关中", "active", format_remaining(next_seconds), str(raw), group=group, schedule_type="cooldown", next_seconds=next_seconds)
    if state.get("in_deep_meditation"):
        return command_row(command, label, "待结算", "active", remaining="0秒", at=str(raw or ""), group=group, schedule_type="cooldown", next_seconds=0)
    return command_row(command, label, "就绪", "ready", remaining="0秒", at=str(raw or ""), group=group, schedule_type="cooldown", next_seconds=0)


def meditation_train_command(state, group="闭关"):
    retry = time_command(state, "next_meditation_retry_time", ".闭关修炼", "闭关修炼", waiting="调息中", ready="就绪", missing="就绪", group=group)
    if retry["tone"] == "cooldown":
        return retry
    next_med = time_command(state, "next_meditation_time", ".闭关修炼", "闭关修炼", waiting="冷却中", ready="就绪", missing="就绪", group=group)
    return next_med if next_med["tone"] == "cooldown" else retry


def force_exit_command(state, group="闭关"):
    active_until = parse_state_time(state.get("formation_active_until", ""))
    display_state = state
    if state.get("next_force_exit_time") and not (active_until and active_until > datetime.now()):
        display_state = dict(state)
        display_state["next_force_exit_time"] = ""
    scheduled = time_command(display_state, "next_force_exit_time", ".强行出关", "强行出关", waiting="已排程", ready="可检查", missing="未排程", group=group)
    if scheduled["tone"] == "cooldown":
        return scheduled
    if state.get("in_deep_meditation"):
        return command_row(
            ".强行出关", "强行出关", "条件触发", "active",
            detail="助阵排程到点或牵引修为不足时自动出关",
            group=group,
        )
    return command_row(".强行出关", "强行出关", "无需出关", "done", group=group, schedule_type="cooldown")


def xiaohao_star_pull_command(state):
    detail = state.get("star_observatory_summary", "")
    retry = time_command(
        state, "star_attraction_retry_time", ".牵引星辰 天雷星", "牵引星辰",
        waiting="修为不足重试", ready="可重试", missing="",
        detail=detail, group="星辰",
    )
    if retry["tone"] == "cooldown":
        return retry
    return time_command(
        state, "next_star_attraction_time", ".牵引星辰 天雷星", "牵引星辰",
        waiting="等待冷却", ready="可牵引", missing="需初始化",
        detail=detail, group="星辰",
    )


def xiaohao_star_attraction_commands(state):
    detail = state.get("star_observatory_summary", "")
    return [
        time_command(state, "next_star_check_time", ".观星台", "观星台", waiting="待对账", ready="需对账", missing="需初始化", detail=detail, group="星辰"),
        time_command(state, "next_star_appease_time", ".安抚星辰", "安抚星辰", waiting="待安抚", ready="可安抚", missing="未排程", detail="到期前1分钟", group="星辰"),
        time_command(state, "next_star_collect_time", ".收集精华", "收集精华", waiting="凝聚中", ready="可收集", missing="未排程", detail=detail, group="星辰"),
        xiaohao_star_pull_command(state),
    ]


def spirit_tree_irrigation_time_for_identity(state, identity="主魂"):
    times = state.get("spirit_tree_irrigation_times")
    if isinstance(times, dict):
        value = times.get(identity or "主魂", "")
        if value:
            return value
    if (identity or "主魂") == "主魂":
        return state.get("next_spirit_tree_irrigation_time", "")
    return ""


def spirit_tree_command(state, group="落云宗", identity="主魂"):
    status = state.get("spirit_tree_status", "灌溉期")
    mature_until = parse_state_time(state.get("spirit_tree_mature_until", ""))
    harvested = state.get("spirit_tree_harvested_in_mature_period")
    attempted = state.get("spirit_tree_harvest_attempted_in_mature_period")
    if status == "成熟采摘期" and mature_until and mature_until > datetime.now():
        detail = "本期已采摘" if harvested else ("本期已处理" if attempted else "等待采摘")
        return command_row(
            ".采摘灵果", "灵树状态", "成熟采摘期", "active",
            format_remaining((mature_until - datetime.now()).total_seconds()),
            str(state.get("spirit_tree_mature_until", "")), detail, group,
            schedule_type="cooldown", next_seconds=(mature_until - datetime.now()).total_seconds(),
        )
    display_state = dict(state)
    display_state["next_spirit_tree_irrigation_time"] = spirit_tree_irrigation_time_for_identity(state, identity)
    irrigation = time_command(display_state, "next_spirit_tree_irrigation_time", ".灵树灌溉", "灵树灌溉", group=group)
    irrigation["detail"] = status or irrigation.get("detail", "")
    return irrigation


def spirit_tree_guard_command(state, group="落云宗"):
    invasion = state.get("spirit_tree_invasion_status", "")
    row = time_command(
        state, "next_spirit_tree_guard_time", ".协同守山", "协同守山",
        waiting="守山冷却", ready="待来袭", missing="待来袭",
        detail=invasion, group=group,
    )
    if invasion:
        row["status"] = "来袭待处理"
        row["tone"] = "active"
    return row


def global_sync_commands():
    return [
        watch_command(".我的灵根", "我的灵根", "同步当前修为和灵根", "资料同步"),
        watch_command(".状态", "状态", "同步当前境界", "资料同步"),
    ]


def meditation_commands(state, include_force_exit=False):
    rows = [
        manual_command(".查看闭关", "查看闭关", "查询闭关状态", "闭关"),
        deep_meditation_command(state),
    ]
    if include_force_exit:
        rows.append(force_exit_command(state))
    return rows


def sect_war_commands(state):
    return [
        manual_command(".宗门战况", "宗门战况", "查询战役状态", "宗门"),
        active_until_command(state, "sect_war_active_until", ".宗门战况", "战役状态", detail="战役有效期", group="宗门"),
        time_command(state, "display_next_sect_war_join_time", ".参战", "参战", waiting="等待参战", ready="就绪", missing="未开启", group="宗门"),
    ]


def concubine_voyage_enabled(account, identity):
    return CONCUBINE_VOYAGE_AUTO_START_ENABLED


def concubine_voyage_detail(state):
    error = str(state.get("last_concubine_voyage_error", "") or "").replace("\n", " ").strip()
    if not error:
        return ""
    when = str(state.get("last_concubine_voyage_error_time", "") or "").strip()
    prefix = f"上次失败 {when}: " if when else "上次失败: "
    return f"{prefix}{error[:80]}"


def concubine_commands(state, include_divination=True, include_voyage=False):
    rows = [
        manual_command(".我的侍妾", "我的侍妾", "查询侍妾/冷却", "侍妾"),
        time_command(state, "next_dream_map_time", ".入梦寻图", "入梦寻图", group="侍妾"),
        time_command(state, "next_heart_trial_time", ".共历心劫", "共历心劫", group="侍妾"),
        flow_command(".稳", "稳", "必须 reply 共历心劫回合消息", "侍妾"),
    ]
    if include_voyage:
        rows.insert(2, time_command(
            state, "next_concubine_voyage_time", ".侍妾远航 冒险", "侍妾远航",
            waiting="12小时冷却", detail=concubine_voyage_detail(state), group="侍妾",
        ))
    if include_divination:
        rows.append(time_command(state, "next_divination_time", ".天机代卜", "天机代卜", group="侍妾"))
    rows.append(manual_command(".拼图", "拼图", "残图满足时发送", "侍妾"))
    return rows


def identity_pause_entry(state, identity):
    """Return active pause metadata for one identity, including legacy main-soul fields."""
    identity = str(identity or "主魂").strip() or "主魂"
    pauses = state.get("identity_pauses", {}) if isinstance(state, dict) else {}
    entry = pauses.get(identity, {}) if isinstance(pauses, dict) else {}
    if not isinstance(entry, dict):
        entry = {}
    if identity == "主魂" and not entry.get("until") and state.get("main_soul_pause_until"):
        entry = {
            "until": state.get("main_soul_pause_until", ""),
            "reason": state.get("main_soul_pause_reason", ""),
        }
    return entry


def apply_identity_pause(panel, state):
    """Show state-level identity pauses without affecting other panels."""
    identity = panel.get("identity") or "主魂"
    entry = identity_pause_entry(state, identity)
    target = parse_state_time(entry.get("until", ""))
    if not target or target <= datetime.now():
        return panel
    seconds = max(0, int((target - datetime.now()).total_seconds()))
    reason = clean_custom_text(entry.get("reason") or "身份暂停", 80)
    remaining = format_remaining(seconds)
    until = entry.get("until", "")
    for row in panel.get("commands") or []:
        old_status = row.get("status", "")
        old_detail = row.get("detail", "")
        row["status"] = "元婴虚弱暂停"
        row["tone"] = "paused"
        row["remaining"] = remaining
        row["at"] = until
        row["next_seconds"] = seconds
        row["detail"] = (
            f"{reason}，暂停至 {until}"
            f"{f' · 原状态：{old_status}' if old_status else ''}"
            f"{f' · {old_detail}' if old_detail else ''}"
        )
    return panel


def main_soul_panel(account, state):
    rows = []
    rows.extend(global_sync_commands())
    if account == "main":
        rows.extend([
            daily_done_command(state, ".闯塔", "闯塔", done_command=".闯塔", group="每日"),
            daily_done_command(state, ".宗门点卯", "宗门点卯", done_command=".宗门点卯", group="每日"),
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_treasure_touch_time", MAIN_TREASURE_TOUCH_COMMAND, "抚摸法宝", group="法宝"),
            time_command(state, "next_nurture_spirit_time", NURTURE_SPIRIT_COMMAND, "温养器灵", waiting="6小时冷却", group="法宝"),
            time_command(state, "nine_heaven_wind_cd_time", ".引九天罡风", "引九天罡风", group="天阶"),
            time_command(state, "next_heart_time", ".问心台", "问心台", group="天阶"),
            manual_command(".天阶状态", "天阶状态", "查询天阶状态", "天阶"),
            time_command(state, "next_stairs_time", ".登天阶", "登天阶", group="天阶"),
        ])
        rows.extend(meditation_commands(state))
        rows.append(fishing_command(state))
        rows.append(time_command(state, "next_field_training_time", MAIN_FIELD_TRAINING_COMMAND, "野外历练", group="通用"))
        rows.extend(sect_war_commands(state))
        rows.extend([
            manual_command(".安置侍妾", "安置侍妾", group="侍妾"),
        ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    elif account == "sub":
        rows.extend([
            daily_done_command(state, ".宗门点卯", "宗门点卯", done_command=".宗门点卯", group="每日"),
            daily_done_command(state, ".闯塔", "闯塔", done_command=".闯塔", group="每日"),
            yuanying_retreat_command(state),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_field_training_time", MAIN_FIELD_TRAINING_COMMAND, "野外历练", group="通用"),
            time_command(state, "next_ask_dao_time", ASK_DAO_COMMAND, "问道", waiting="冷却中", ready="可问道", missing="可问道", group="元婴宗"),
        ])
        rows.extend(sect_war_commands(state))
        rows.extend(meditation_commands(state))
        rows.append(fishing_command(state))
        rows.append(manual_command(".安置侍妾", "安置侍妾", group="侍妾"))
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    elif account == "xiaohao":
        rows.extend([
            daily_done_command(state, ".闯塔", "闯塔", done_command=".闯塔", group="每日"),
            daily_done_command(state, ".宗门点卯", "宗门点卯", done_command=".宗门点卯", group="每日"),
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_treasure_touch_time", SUB_TREASURE_TOUCH_COMMAND, "抚摸法宝", group="法宝"),
            time_command(state, "next_field_training_time", MAIN_FIELD_TRAINING_COMMAND, "野外历练", group="通用"),
        ])
        rows.extend(sect_war_commands(state))
        rows.extend(meditation_commands(state))
        rows.append(fishing_command(state))
        rows.extend([
            manual_command(".安置侍妾", "安置侍妾", group="侍妾"),
            manual_command(".我的灵兽", "我的灵兽", "查询灵兽状态", "灵兽"),
            time_command(state, "next_hunt_time", ".寻觅灵兽", "寻觅灵兽", group="灵兽"),
            manual_command(".放生 <灵兽>", "放生灵兽", "流程内按需", "灵兽"),
            manual_command(".灵兽休息 <灵兽>", "灵兽休息", group="灵兽"),
            manual_command(".灵兽出战 <灵兽>", "灵兽出战", group="灵兽"),
            time_command(state, "next_steal_time", ".灵兽偷菜", "灵兽偷菜", group="灵兽"),
            time_command(state, "next_abyss_time", ".探渊 <灵兽>", "探渊", group="灵兽"),
            time_command(state, "next_pasture_time", ".一键放养", "一键放养", group="灵兽"),
            time_command(state, "next_beast_interaction_time", ".灵兽互动 六翼 / 安抚", "灵兽互动", group="灵兽"),
            time_command(state, "next_beast_cruise_time", ".灵兽巡游 <灵兽>", "灵兽巡游", group="灵兽"),
        ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    return {"identity": "主魂", "role": "主魂", "commands": rows}


def lingxiao_avatar_commands(name, state, root_state=None):
    rows = []
    root_state = root_state or {}
    rows.extend(global_sync_commands())
    rows.extend(meditation_commands(state, include_force_exit=(name == "素缘子")))
    rows.append(fishing_command(state))
    if name == YINLUO_IDENTITY:
        rows.extend(yinluo_commands(state))
    if name == "无咎子":
        rows.extend([
            manual_command(".推命 闭关", "推命闭关", group="推命"),
            manual_command(".推命 探索", "推命探索", group="推命"),
            manual_command(".改命 探索", "改命探索", group="推命"),
            time_command(state, "next_field_training_time", WUJIUZI_FIELD_TRAINING_COMMAND, "野外历练", group="通用"),
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            daily_done_command(
                state,
                ".观命",
                "观命",
                date_key="last_destiny_date",
                detail=f"上次定命：{state.get('last_destiny_choice') or '未记录'}",
                group="每日",
            ),
        ])
    else:
        rows.append(time_command(state, "next_field_training_time", DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, "野外历练", group="通用"))
    rows.append(daily_done_command(state, ".闯塔", "闯塔", date_key="last_tower_date", group="每日"))
    if name == "缘生子":
        tree_state = root_state or state
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            spirit_tree_command(tree_state, identity=name),
            spirit_tree_guard_command(tree_state),
        ])
    if name == "素缘子":
        rows.extend(xiaohao_star_attraction_commands(state))
        star_gazing_state = root_state or state
        rows.extend([
            time_command(state, "next_formation_time", ".助阵", "助阵", group="阵法"),
            time_command(star_gazing_state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
            time_command(
                star_gazing_state,
                "pending_star_gazing_target_time",
                ".观星",
                "待观星",
                waiting="已排程",
                ready="监听中",
                missing="监听中",
                group="星宫",
            ),
            time_command(
                star_gazing_state,
                "pending_star_shift_target_time",
                ".改换星移 @Waaiging",
                "改换星移",
                waiting="已排程",
                ready="监听中",
                missing="监听中",
                group="星宫",
            ),
        ])
    rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled("main", name)))
    rows.append(daily_done_command(state, ".宗门点卯", "宗门点卯", date_key="last_dianmao_date", group="每日"))
    return rows


def star_avatar_commands(name, state):
    rows = []
    rows.extend(global_sync_commands())
    rows.append(time_command(state, "next_field_training_time", DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, "野外历练", group="通用"))
    if name == "缘生子":
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
        ])
    rows.append(fishing_command(state))
    rows.extend(meditation_commands(state, include_force_exit=True))
    rows.extend(xiaohao_star_attraction_commands(state))
    rows.extend([
        time_command(state, "next_formation_time", ".启阵", "启阵", group="阵法"),
        daily_done_command(state, ".闯塔", "闯塔", date_key="last_tower_date", group="每日"),
        time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
        time_command(state, "pending_star_gazing_target_time", ".观星", "待观星", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        time_command(state, "pending_star_shift_target_time", ".改换星移 @Gamling33", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        daily_done_command(state, ".宗门点卯", "宗门点卯", date_key="last_dianmao_date", group="每日"),
    ])
    rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled("sub", name)))
    return rows


def xiaohao_avatar_commands(name, state):
    rows = []
    rows.extend(global_sync_commands())
    rows.extend(meditation_commands(state, include_force_exit=(name in {"素心子", "缘生子"})))
    rows.extend([
        time_command(state, "next_field_training_time", DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, "野外历练", group="通用"),
        daily_done_command(state, ".闯塔", "闯塔", date_key="last_tower_date", group="每日"),
        daily_done_command(state, ".宗门点卯", "宗门点卯", date_key="last_dianmao_date", group="每日"),
    ])
    if name == "缘生子":
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
        ])
    rows.append(fishing_command(state))
    if name == "问心子":
        rows.extend([
            time_command(state, "nine_heaven_wind_cd_time", ".引九天罡风", "引九天罡风", group="天阶"),
            time_command(state, "next_heart_time", ".问心台", "问心台", group="天阶"),
            manual_command(".天阶状态", "天阶状态", "查询天阶状态", "天阶"),
            time_command(state, "next_stairs_time", ".登天阶", "登天阶", group="天阶"),
        ])
    else:
        rows.extend([
            time_command(state, "next_formation_time", ".助阵", "助阵", group="阵法"),
        ])
        rows.extend(xiaohao_star_attraction_commands(state))
        rows.extend([
            time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
            time_command(state, "pending_star_shift_target_time", ".改换星移 @TitanCreeper", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        ])
    rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled("xiaohao", name)))
    return rows


def avatar_commands(account, name, state, root_state=None):
    if account == "main":
        return lingxiao_avatar_commands(name, state, root_state=root_state)
    if account == "sub":
        return star_avatar_commands(name, state)
    if account == "xiaohao":
        return xiaohao_avatar_commands(name, state)
    return global_sync_commands()


def build_command_panels(account, state):
    """Build identity-scoped command status panels from the state JSON."""
    panels = [main_soul_panel(account, state)]
    avatars = state.get("avatars", {}) or {}
    for name, avatar_state in avatars.items():
        panels.append({
            "identity": name,
            "role": "化身",
            "commands": avatar_commands(account, name, avatar_state or {}, root_state=state),
        })
    custom_commands = load_custom_commands()
    result = []
    for panel in panels:
        append_custom_commands(account, panel, custom_commands, root_state=state)
        panel = apply_command_controls(account, panel)
        panel = apply_identity_pause(panel, state)
        result.append(panel)
    return result


# =====================================================================
# 日志解析
# =====================================================================

def get_log_filename(name):
    """根据账号名获取日志文件名"""
    if name == 'main': return 'cultivator.log'
    if name == 'sub': return 'sub_cultivator.log'
    if name == 'xiaohao': return 'cultivator_xiaohao.log'
    return f'{name}_cultivator.log'

def normalize_log_command(first, second=""):
    """标准化指令文本（合并双段指令）"""
    first = (first or "").strip()
    second = (second or "").strip().strip("`，,。:：)）")
    if (first, second) in TWO_PART_COMMANDS:
        return f"{first} {second}"
    return first

def extract_commands_from_text(text):
    """从文本中提取所有指令标签"""
    tags = set()
    for first, second in LOG_COMMAND_RE.findall(text or ""):
        if first:
            tags.add(normalize_log_command(first, second))
    return tags

def extract_command_from_line(line):
    """从日志行中提取指令"""
    line = (line or "").strip()
    if not line.startswith("."):
        return ""
    match = LOG_COMMAND_RE.match(line)
    return normalize_log_command(match.group(1), match.group(2)) if match else ""

def is_incoming_log_entry(entry):
    """判断日志条目是否为收到的消息"""
    header = entry["lines"][0] if entry.get("lines") else ""
    return "IN [" in header

def incoming_log_label(header):
    """从日志头中提取指令标签"""
    labels = re.findall(r"\[([^\]]+)\]", header or "")
    for label in labels:
        label = label.strip()
        if label and label != "INFO":
            return label
    return ""

def incoming_log_sender_text(header):
    """从日志头中提取发送者信息"""
    match = re.search(r"IN \[[^\]]+\]\s*(.*):\s*$", header or "")
    return (match.group(1) if match else "").strip()

def is_outgoing_log_entry(entry):
    header = entry["lines"][0] if entry.get("lines") else ""
    return bool(re.search(r"\bOUT\s+\[", header or ""))

def outgoing_log_identity(header):
    """从 OUT 日志头提取发送身份。"""
    match = LOG_OUT_IDENTITY_RE.search(header or "")
    if not match:
        return ""
    label = (match.group(1) or "主魂").strip()
    if "|" in label:
        label = label.rsplit("|", 1)[1].strip()
    if label.startswith("manual "):
        return "主魂"
    return label or "主魂"

def outgoing_log_command(entry):
    """从 OUT 日志条目提取实际发送的指令。"""
    text = "\n".join(entry.get("lines") or []).replace("\\n", "\n")
    commands = list(LOG_COMMAND_RE.findall(text or ""))
    for first, second in commands:
        command = normalize_log_command(first, second)
        if command:
            return command
    return ""

def outgoing_log_command_full(entry):
    """从 OUT 日志条目提取完整指令行，保留参数。"""
    lines = []
    for raw in entry.get("lines") or []:
        lines.extend(str(raw or "").replace("\\n", "\n").splitlines())
    for raw in lines[1:]:
        line = str(raw or "").strip().strip("`")
        if line.startswith("."):
            return re.sub(r"\s+", " ", line).strip()
    return outgoing_log_command(entry)

def is_probable_bot_reply_log_entry(entry):
    """判断日志条目是否可能是游戏机器人的回复"""
    if not is_incoming_log_entry(entry):
        return False
    header = entry["lines"][0] if entry.get("lines") else ""
    sender_text = incoming_log_sender_text(header)
    marker_text = f"{header} {sender_text}".lower()
    if any(marker.lower() in marker_text for marker in BOT_REPLY_MARKERS):
        return True
    label = incoming_log_label(header)
    return bool(extract_command_from_line(label))

def extract_log_entry_tags(entry):
    """提取日志条目的分类标签"""
    lines = entry.get("lines") or []
    if not lines or not is_probable_bot_reply_log_entry(entry):
        return {OTHER_LOG_TAG}
    header = lines[0]
    for label in re.findall(r"\[([^\]]+)\]", header):
        label = label.strip()
        command = extract_command_from_line(label)
        if command:
            return {command}
    return {OTHER_LOG_TAG}

def parse_log_entry_time(entry):
    """解析日志条目的时间戳"""
    lines = entry.get("lines") or []
    if not lines:
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", lines[0])
    if not match:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(match.group(1), TIME_FORMAT)
    except Exception:
        return None

def parse_log_int(value):
    """安全地将字符串转为整数（去除逗号）"""
    try:
        return int(str(value or "0").replace(",", "").strip())
    except Exception:
        return 0

def normalize_profile_username(value):
    return str(value or "").lower().strip().lstrip("@")

def profile_username_from_text(text):
    clean = str(text or "").replace("*", "").replace("`", "")
    for pattern in PROFILE_USERNAME_PATTERNS:
        match = pattern.search(clean)
        if match:
            return normalize_profile_username(match.group(1))
    return ""

def profile_username_identity(account, username):
    username = normalize_profile_username(username)
    if not account or not username:
        return ""
    for identity, names in (ACCOUNT_PROFILE_USERNAMES.get(account) or {}).items():
        if username in {normalize_profile_username(name) for name in names}:
            return identity
    return "__other__"

def entry_profile_username_identity(account, entry):
    text = entry.get("text") or "\n".join(entry.get("lines") or [])
    return profile_username_identity(account, profile_username_from_text(text))

def entry_belongs_to_main_profile(account, entry):
    """Only main-soul profile snapshots feed the account-level dashboard profile."""
    if not account:
        return True
    identity = entry_profile_username_identity(account, entry)
    if identity:
        return identity == "主魂"
    # Username-less profile snapshots are not authoritative enough for rebuilding
    # the account profile from logs; runtime state sync handles cautious updates.
    return False

def entry_profile_mentions_other_account(account, entry):
    if not account:
        return False
    identity = entry_profile_username_identity(account, entry)
    return identity == "__other__" or (identity and identity != "主魂")

def first_command_tag(entry):
    """获取日志条目的第一个指令标签"""
    for tag in entry.get("tags") or []:
        if tag != OTHER_LOG_TAG:
            return tag
    header = entry["lines"][0] if entry.get("lines") else ""
    command = extract_command_from_line(incoming_log_label(header))
    return command or ""

def log_entry_identity(entry):
    text = entry.get("text") or "\n".join(entry.get("lines") or [])
    avatar_match = re.search(r"\[Avatar:\s*([^\]\n]+)\]", text or "")
    if avatar_match:
        return avatar_match.group(1).strip()
    return (entry.get("identity") or "").strip()

def is_avatar_log_entry(entry):
    identity = log_entry_identity(entry)
    if identity and identity != "主魂":
        return True
    text = entry.get("text") or "\n".join(entry.get("lines") or [])
    return any(name in text for name in ALL_AVATARS) or "[Avatar:" in text

def can_inherit_related_command(entry):
    """判断日志条目是否可以继承上一条关联指令（编辑/提及上下文）"""
    header = entry["lines"][0] if entry.get("lines") else ""
    return is_probable_bot_reply_log_entry(entry) and (
        "IN [edited" in header or "IN [mention" in header
    )

def split_log_entries(lines):
    """将日志行列表按时间戳分割为条目"""
    entries = []
    current = []
    start_line = 0
    for idx, line in enumerate(lines):
        if LOG_ENTRY_RE.match(line) and current:
            entries.append({"start_line": start_line, "end_line": idx, "lines": current})
            current = [line]
            start_line = idx
        else:
            if not current:
                start_line = idx
            current.append(line)
    if current:
        entries.append({"start_line": start_line, "end_line": len(lines), "lines": current})
    return entries

def decorate_log_entries(entries):
    """为日志条目补充标签和关联信息"""
    decorated = []
    last_command_tag = ""
    last_command_time = None
    recent_outgoing = []
    for entry in entries:
        text = "\n".join(entry["lines"])
        tags = extract_log_entry_tags(entry)
        entry_time = parse_log_entry_time(entry)
        related_tags = set()
        identity = ""
        if is_outgoing_log_entry(entry):
            command = outgoing_log_command(entry)
            out_identity = outgoing_log_identity(entry["lines"][0] if entry.get("lines") else "")
            if command and out_identity:
                recent_outgoing.append({"command": command, "identity": out_identity, "time": entry_time})
                if len(recent_outgoing) > 80:
                    recent_outgoing = recent_outgoing[-40:]
        explicit_tags = {tag for tag in tags if tag != OTHER_LOG_TAG}
        if explicit_tags:
            last_command_tag = sorted(explicit_tags)[0]
            last_command_time = entry_time
        elif last_command_tag and can_inherit_related_command(entry):
            if entry_time is None or last_command_time is None:
                related_tags.add(last_command_tag)
            else:
                delta = (entry_time - last_command_time).total_seconds()
                if 0 <= delta <= RELATED_LOG_WINDOW_SECONDS:
                    related_tags.add(last_command_tag)
        if related_tags:
            tags = set(related_tags)
        command_for_identity = next((tag for tag in tags if tag != OTHER_LOG_TAG), "")
        if is_probable_bot_reply_log_entry({**entry, "tags": tags}) and command_for_identity and entry_time is not None:
            for item in reversed(recent_outgoing):
                if item.get("command") != command_for_identity:
                    continue
                out_time = item.get("time")
                if out_time is None:
                    continue
                delta = (entry_time - out_time).total_seconds()
                if 0 <= delta <= RELATED_LOG_WINDOW_SECONDS:
                    identity = item.get("identity") or ""
                    break
        decorated.append({**entry, "text": text, "tags": tags, "related_tags": related_tags, "identity": identity})
    return decorated

def read_log_entries(name):
    """读取账号的日志文件并返回解析后的条目"""
    filename = get_log_filename(name)
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return [], f"日志文件 {filename} 不存在。"
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.read().splitlines()
    except Exception:
        return [], "无法读取日志内容。"
    return decorate_log_entries(split_log_entries(lines)), ""

def log_entries_from_bytes(raw, base_offset, end_offset, trim_start=True):
    """Parse complete log entries from a byte slice and keep byte cursors."""
    if not raw:
        return []
    if trim_start:
        newline_at = raw.find(b"\n")
        if newline_at < 0:
            return []
        raw = raw[newline_at + 1:]
        base_offset += newline_at + 1

    raw_line_chunks = raw.splitlines(keepends=True)
    if not raw_line_chunks:
        return []

    decoded_lines = [
        line.rstrip(b"\r\n").decode("utf-8", errors="ignore")
        for line in raw_line_chunks
    ]
    line_offsets = []
    offset = 0
    for line in raw_line_chunks:
        line_offsets.append(offset)
        offset += len(line)

    first_entry_idx = None
    for idx, line in enumerate(decoded_lines):
        if LOG_ENTRY_RE.match(line):
            first_entry_idx = idx
            break
    if first_entry_idx is None:
        return []
    if first_entry_idx > 0:
        base_offset += line_offsets[first_entry_idx]
        decoded_lines = decoded_lines[first_entry_idx:]
        line_offsets = [value - line_offsets[first_entry_idx] for value in line_offsets[first_entry_idx:]]

    entries = []
    current = []
    current_start_line = 0
    current_start_byte = base_offset
    for idx, line in enumerate(decoded_lines):
        if LOG_ENTRY_RE.match(line) and current:
            entry_end_byte = base_offset + line_offsets[idx]
            entries.append({
                "start_line": current_start_line,
                "end_line": idx,
                "start_byte": current_start_byte,
                "end_byte": entry_end_byte,
                "lines": current,
            })
            current = [line]
            current_start_line = idx
            current_start_byte = base_offset + line_offsets[idx]
        else:
            if not current:
                current_start_line = idx
                current_start_byte = base_offset + line_offsets[idx]
            current.append(line)
    if current:
        entries.append({
            "start_line": current_start_line,
            "end_line": len(decoded_lines),
            "start_byte": current_start_byte,
            "end_byte": end_offset,
            "lines": current,
        })
    return entries

def read_recent_log_entries(name, before_byte=None, limit=80):
    """Read recent log entries from the tail without parsing the whole file."""
    filename = get_log_filename(name)
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return [], f"日志文件 {filename} 不存在。", {"log_size": 0, "partial": True}
    try:
        stat = os.stat(path)
        file_size = int(getattr(stat, "st_size", 0) or 0)
    except OSError:
        return [], "无法读取日志内容。", {"log_size": 0, "partial": True}

    if file_size <= 0:
        return [], "", {"log_size": 0, "partial": True}
    try:
        end_byte = file_size if before_byte is None else max(0, min(int(before_byte), file_size))
    except Exception:
        end_byte = file_size
    if end_byte <= 0:
        return [], "", {"log_size": file_size, "partial": True}

    read_bytes = min(max(LOG_TAIL_INITIAL_BYTES, 16 * 1024), end_byte)
    parsed = []
    try:
        with open(path, "rb") as f:
            while True:
                start_byte = max(0, end_byte - read_bytes)
                f.seek(start_byte)
                raw = f.read(end_byte - start_byte)
                parsed = log_entries_from_bytes(raw, start_byte, end_byte, trim_start=start_byte > 0)
                if len(parsed) >= limit + 1 or start_byte == 0 or read_bytes >= min(LOG_TAIL_MAX_BYTES, end_byte):
                    break
                read_bytes = min(read_bytes * 2, end_byte, LOG_TAIL_MAX_BYTES)
    except Exception:
        return [], "无法读取日志内容。", {"log_size": file_size, "partial": True}

    page = parsed[-limit:]
    decorated = decorate_log_entries(page)
    if page:
        next_before = page[0].get("start_byte")
        has_more = bool(next_before and next_before > 0)
    else:
        next_before = None
        has_more = False
    meta = {
        "log_size": file_size,
        "partial": True,
        "cursor_mode": "byte",
        "has_more": has_more,
        "next_before": next_before if has_more else None,
        "loaded_from_byte": page[0].get("start_byte") if page else end_byte,
        "loaded_to_byte": page[-1].get("end_byte") if page else end_byte,
    }
    return decorated, "", meta


# =====================================================================
# 指令发送记录
# =====================================================================

def command_record_signature(name):
    """返回消息库签名，用于避免重复解析指令发送记录。"""
    path = message_events_db_path()
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "account": name,
        "log_size": int(getattr(stat, "st_size", 0) or 0),
        "log_mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
    }

def command_record_username(account, identity):
    names = account_profile_usernames(account).get(identity or "主魂") or []
    return " / ".join(names)

def build_account_command_records(name, recent_limit=8):
    """按身份+指令聚合已成功发送的指令消息，生成发送记录表数据。"""
    if name not in WINDOW_MAP:
        return {"records": [], "error": "未知账号", "updated_at": datetime.now().strftime(TIME_FORMAT)}
    signature = command_record_signature(name)
    if not signature:
        return {"records": [], "error": f"{MESSAGE_EVENTS_DB_FILE} 不存在。", "updated_at": datetime.now().strftime(TIME_FORMAT)}

    cache_key = json.dumps(signature, sort_keys=True)
    with COMMAND_RECORD_LOCK:
        cached = COMMAND_RECORD_CACHE.get(name)
        if cached and cached.get("signature") == cache_key:
            return cached.get("data") or {"records": [], "error": ""}

    error = ""
    today = datetime.now().strftime("%Y-%m-%d")
    records = {}
    path = message_events_db_path()
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=2)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                cl.chat_id,
                cl.command_msg_id,
                cl.identity,
                cl.command,
                cl.source,
                cl.sent_at
            FROM command_ledger cl
            WHERE cl.account=?
              AND cl.command_msg_id IS NOT NULL
              AND COALESCE(cl.sent_at, '')!=''
              AND COALESCE(cl.command, '') LIKE '.%'
            ORDER BY cl.sent_at ASC, cl.command_msg_id ASC
            """,
            (name,),
        ).fetchall()
    except Exception as exc:
        rows = []
        error = f"读取 {MESSAGE_EVENTS_DB_FILE} 失败: {exc}"
    finally:
        if conn is not None:
            conn.close()

    for item in rows:
        command = str(item["command"] or "").strip()
        if not command or not command.startswith("."):
            continue
        entry_time = parse_state_time(item["sent_at"])
        if not entry_time:
            continue
        send_key = f"{item['chat_id']}:{item['command_msg_id']}"
        if not send_key:
            continue
        identity = str(item["identity"] or "主魂").strip() or "主魂"
        key = (identity, command)
        row = records.setdefault(key, {
            "account": name,
            "account_name": ACCOUNT_DISPLAY_NAMES.get(name, name),
            "identity": identity,
            "username": command_record_username(name, identity),
            "command": command,
            "count": 0,
            "today_count": 0,
            "first_time": "",
            "previous_time": "",
            "last_time": "",
            "last_interval_seconds": None,
            "recent_times": [],
            "_seen_send_keys": set(),
            "manual_count": 0,
            "auto_count": 0,
            "is_switch": command.startswith(".切换"),
        })
        if send_key in row["_seen_send_keys"]:
            continue
        row["_seen_send_keys"].add(send_key)
        time_text = entry_time.strftime(TIME_FORMAT)
        if not row["first_time"]:
            row["first_time"] = time_text
        if row["last_time"]:
            row["previous_time"] = row["last_time"]
            prev_dt = parse_state_time(row["last_time"])
            if prev_dt:
                row["last_interval_seconds"] = int(max(0, (entry_time - prev_dt).total_seconds()))
        row["last_time"] = time_text
        row["count"] += 1
        if time_text.startswith(today):
            row["today_count"] += 1
        source = str(item["source"] or "")
        if source == "manual":
            row["manual_count"] += 1
        else:
            row["auto_count"] += 1
        row["recent_times"].append(time_text)
        if len(row["recent_times"]) > recent_limit:
            row["recent_times"] = row["recent_times"][-recent_limit:]

    rows = sorted(records.values(), key=lambda item: item.get("last_time") or "", reverse=True)
    for row in rows:
        row.pop("_seen_send_keys", None)
    data = {
        "records": rows,
        "error": error,
        "updated_at": datetime.now().strftime(TIME_FORMAT),
        "source": MESSAGE_EVENTS_DB_FILE,
        **signature,
    }
    with COMMAND_RECORD_LOCK:
        COMMAND_RECORD_CACHE[name] = {"signature": cache_key, "data": data}
    return data

def build_all_command_records():
    return {
        "accounts": {
            key: {
                "name": ACCOUNT_DISPLAY_NAMES.get(key, key),
                **build_account_command_records(key),
            }
            for key in WINDOW_MAP
        },
        "server_time": datetime.now().strftime(TIME_FORMAT),
    }


# =====================================================================
# 消息采集健康
# =====================================================================

def message_events_db_path():
    return os.path.join(CONFIG_DIR, MESSAGE_EVENTS_DB_FILE)

def parse_db_time(value):
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except Exception:
        return None

def message_time_age_seconds(value):
    dt = parse_db_time(value)
    if not dt:
        return None
    return int(max(0, (datetime.now() - dt).total_seconds()))

def ensure_message_health_indexes(conn):
    """Keep dashboard health queries fast on existing message_events.sqlite3 files."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_created ON message_events(account, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_kind_created ON message_events(account, event_kind, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_account_bot_created ON message_events(account, is_game_bot, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_message_events_bot_created ON message_events(is_game_bot, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_command_ledger_account_msg ON command_ledger(account, command_msg_id)")

def build_message_health(since_hours=24, min_gap_seconds=60, min_missing_msg_ids=20, limit=8):
    """Audit message_events.sqlite3 for lightweight listener/message-box health."""
    path = message_events_db_path()
    now = datetime.now()
    cutoff = (now - timedelta(hours=max(1, int(since_hours or 24)))).strftime(TIME_FORMAT)
    try:
        stat = os.stat(path)
        signature = {
            "mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
            "size": int(getattr(stat, "st_size", 0) or 0),
            "since_hours": int(since_hours or 24),
            "min_gap_seconds": int(min_gap_seconds or 60),
            "min_missing_msg_ids": int(min_missing_msg_ids or 20),
            "limit": int(limit or 8),
        }
    except OSError:
        stat = None
        signature = None

    if signature:
        cache_key = json.dumps(signature, sort_keys=True)
        with MESSAGE_HEALTH_LOCK:
            cached = MESSAGE_HEALTH_CACHE.get("data")
            if (
                cached
                and MESSAGE_HEALTH_CACHE.get("key") == cache_key
                and time.time() - float(MESSAGE_HEALTH_CACHE.get("at") or 0) < MESSAGE_HEALTH_CACHE_SECONDS
            ):
                return cached

    payload = {
        "ok": False,
        "status": "missing",
        "db": MESSAGE_EVENTS_DB_FILE,
        "updated_at": now.strftime(TIME_FORMAT),
        "since_hours": since_hours,
        "min_gap_seconds": min_gap_seconds,
        "min_missing_msg_ids": min_missing_msg_ids,
        "accounts": {},
        "gap_count": 0,
        "latest_at": "",
        "latest_age_seconds": None,
        "notes": [
            "断层按同账号 msg_id 跳号和写入时间间隔估算，只提示可能漏采。",
            "消息箱来自脚本运行时记录；session 不回拉也不影响此处统计。",
        ],
    }
    if stat is None:
        payload["error"] = "message_events.sqlite3 不存在"
        return payload

    try:
        payload["db_size"] = int(getattr(stat, "st_size", 0) or 0)
        payload["db_mtime"] = datetime.fromtimestamp(stat.st_mtime).strftime(TIME_FORMAT)
        with sqlite3.connect(path, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM message_events LIMIT 1").fetchone()
            ensure_message_health_indexes(conn)
            latest_global = conn.execute(
                "SELECT MAX(created_at) AS latest_at FROM message_events"
            ).fetchone()
            payload["latest_at"] = (latest_global["latest_at"] if latest_global else "") or ""
            payload["latest_age_seconds"] = message_time_age_seconds(payload["latest_at"])

            total_gaps = 0
            stale_accounts = 0
            for account in WINDOW_MAP:
                counts = {
                    "total": 0,
                    "since": 0,
                    "raw": 0,
                    "in": 0,
                    "out": 0,
                    "edited": 0,
                    "bot": 0,
                }
                total_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM message_events WHERE account=?",
                    (account,),
                ).fetchone()
                counts["total"] = int(total_row["c"] if total_row else 0)
                since_row = conn.execute(
                    "SELECT COUNT(*) AS c FROM message_events WHERE account=? AND created_at>=?",
                    (account, cutoff),
                ).fetchone()
                counts["since"] = int(since_row["c"] if since_row else 0)
                for row in conn.execute(
                    """
                    SELECT direction, COUNT(*) AS c
                    FROM message_events
                    WHERE account=? AND created_at>=?
                    GROUP BY direction
                    """,
                    (account, cutoff),
                ).fetchall():
                    key = str(row["direction"] or "raw")
                    if key in counts:
                        counts[key] = int(row["c"] or 0)
                edited_row = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM message_events
                    WHERE account=? AND event_kind='edited' AND created_at>=?
                    """,
                    (account, cutoff),
                ).fetchone()
                counts["edited"] = int(edited_row["c"] if edited_row else 0)
                bot_row = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM message_events
                    WHERE account=? AND is_game_bot=1 AND created_at>=?
                    """,
                    (account, cutoff),
                ).fetchone()
                counts["bot"] = int(bot_row["c"] if bot_row else 0)

                latest = conn.execute(
                    """
                    SELECT msg_id, created_at, event_kind, direction, sender_username, sender_name, substr(text, 1, 80) AS text
                    FROM message_events
                    WHERE account=? AND msg_id IS NOT NULL
                    ORDER BY created_at DESC, msg_id DESC
                    LIMIT 1
                    """,
                    (account,),
                ).fetchone()
                latest_dict = dict(latest) if latest else {}
                latest_age = message_time_age_seconds(latest_dict.get("created_at"))
                if latest_age is not None and latest_age > 30 * 60:
                    stale_accounts += 1

                max_msg_row = conn.execute(
                    "SELECT MAX(msg_id) AS max_msg_id FROM message_events WHERE account=? AND msg_id IS NOT NULL",
                    (account,),
                ).fetchone()
                max_msg_id = int(max_msg_row["max_msg_id"] or 0) if max_msg_row else 0
                min_scan_msg_id = max(0, max_msg_id - MESSAGE_HEALTH_MAX_SCAN_IDS)
                msg_rows = conn.execute(
                    """
                    SELECT msg_id, MIN(created_at) AS first_at, MAX(created_at) AS last_at
                    FROM message_events
                    WHERE account=? AND created_at>=? AND msg_id IS NOT NULL AND msg_id>=?
                    GROUP BY msg_id
                    ORDER BY msg_id
                    """,
                    (account, cutoff, min_scan_msg_id),
                ).fetchall()
                gaps = []
                prev = None
                for row in msg_rows:
                    current = {
                        "msg_id": int(row["msg_id"]),
                        "first_at": row["first_at"],
                        "last_at": row["last_at"],
                    }
                    if prev:
                        missing = current["msg_id"] - prev["msg_id"] - 1
                        if missing >= int(min_missing_msg_ids or 20):
                            prev_dt = parse_db_time(prev["last_at"])
                            current_dt = parse_db_time(current["first_at"])
                            time_gap = int((current_dt - prev_dt).total_seconds()) if prev_dt and current_dt else 0
                            if time_gap >= int(min_gap_seconds or 60):
                                gaps.append({
                                    "from_msg_id": prev["msg_id"],
                                    "to_msg_id": current["msg_id"],
                                    "missing_msg_ids": missing,
                                    "from_time": prev["last_at"],
                                    "to_time": current["first_at"],
                                    "gap_seconds": time_gap,
                                })
                    prev = current
                if len(gaps) > int(limit or 8):
                    gaps = gaps[-int(limit or 8):]
                total_gaps += len(gaps)
                payload["accounts"][account] = {
                    "name": ACCOUNT_DISPLAY_NAMES.get(account, account),
                    "counts": counts,
                    "latest": latest_dict,
                    "latest_age_seconds": latest_age,
                    "gap_count": len(gaps),
                    "gaps": gaps,
                }

            payload["gap_count"] = total_gaps
            payload["ok"] = True
            payload["status"] = "ok"
            if total_gaps:
                payload["status"] = "warn"
            if stale_accounts == len(WINDOW_MAP):
                payload["status"] = "stale"
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = str(exc)
    if signature:
        with MESSAGE_HEALTH_LOCK:
            MESSAGE_HEALTH_CACHE["key"] = json.dumps(signature, sort_keys=True)
            MESSAGE_HEALTH_CACHE["at"] = time.time()
            MESSAGE_HEALTH_CACHE["data"] = payload
    return payload


# =====================================================================
# 资源/库存统计
# =====================================================================

RESOURCE_LINE_SKIP_PHRASES = (
    "当前修为", "**修为**", "修为不足", "修为加成", "修为惩罚", "当前境界",
    "冷却", "剩余", "预计", "可以使用", "新手秘籍", "http://", "https://",
)
RESOURCE_NAME_SKIP_FRAGMENTS = (
    "修为", "境界", "冷却", "剩余", "预计", "道友", "天命玉牒", "修士状态",
    "评分", "体力", "精力", "胜算", "战力", "经验", "奇遇", "次数", "底蕴",
    "心情", "羁绊",
)
RESOURCE_NAME_SKIP_EXACT = {"点", "次", "小时", "分钟", "秒", "轮", "层"}
RESOURCE_UNITS = ("点", "枚", "块", "个", "份", "株", "颗", "瓶", "张", "件", "缕", "滴", "层")
INVENTORY_COMMAND_PREFIXES = (".储物袋", ".背包", ".物品栏", ".物品清单", ".库存")
INVENTORY_HEADER_RE = re.compile(r"(?:^|\n)\s*(?:[【\[]?(?:储物袋|背包|物品栏|物品清单|库存)[】\]]?|[-=]{2,})\s*(?:$|\n|[:：])")
TEXT_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{2,64})")

def normalize_resource_line(value):
    return str(value or "").replace("**", "").replace("`", "").replace(",", "").strip()

def clean_resource_name(value):
    name = re.sub(r"\s+", "", str(value or ""))
    name = name.strip(" ：:，,。.!！?？；;、[]【】()（）*x×+-")
    if not name or len(name) > 32:
        return ""
    if name in RESOURCE_NAME_SKIP_EXACT:
        return ""
    if any(fragment in name for fragment in RESOURCE_NAME_SKIP_FRAGMENTS):
        return ""
    if re.fullmatch(r"\d+", name):
        return ""
    return name

def add_resource_change(changes, name, amount, line="", source=""):
    name = clean_resource_name(name)
    amount = parse_log_int(amount)
    if not name or not amount:
        return
    changes.append({
        "name": name,
        "amount": int(amount),
        "direction": "gain" if int(amount) > 0 else "loss",
        "source": source or ("获得" if int(amount) > 0 else "消耗"),
        "line": str(line or "").strip(),
    })

def known_resource_usernames(account, identity=""):
    mapping = ACCOUNT_PROFILE_USERNAMES.get(account) or {}
    names = set()
    if identity:
        for value in mapping.get(identity, set()) or set():
            normalized = normalize_profile_username(value)
            if normalized:
                names.add(normalized)
    else:
        for values in mapping.values():
            for value in values or set():
                normalized = normalize_profile_username(value)
                if normalized:
                    names.add(normalized)
    return names

def resource_text_matches_identity(account, identity, text):
    """Reject resource rows that explicitly point at a different game username."""
    if account not in WINDOW_MAP:
        return False
    clean = str(text or "")
    profile_username = profile_username_from_text(clean)
    if profile_username:
        profile_identity = profile_username_identity(account, profile_username)
        if profile_identity == "__other__":
            return False
        if identity and profile_identity and profile_identity != identity:
            return False

    mentions = {normalize_profile_username(name) for name in TEXT_USERNAME_RE.findall(clean)}
    mentions.discard("")
    if not mentions:
        return True

    known_for_identity = known_resource_usernames(account, identity)
    if identity and known_for_identity:
        return bool(mentions & known_for_identity)
    known_for_account = known_resource_usernames(account)
    return bool(mentions & known_for_account)

def is_inventory_snapshot_text(text, command=""):
    cmd = str(command or "").strip()
    if any(cmd.startswith(prefix) for prefix in INVENTORY_COMMAND_PREFIXES):
        return True
    raw = str(text or "")
    if not raw:
        return False
    return bool(INVENTORY_HEADER_RE.search(raw.replace("**", "")))

def parse_resource_changes_from_text(text):
    """Extract non-cultivation resource/item gain and loss from bot replies."""
    raw = str(text or "")
    if not raw:
        return []
    changes = []
    for raw_line in raw.splitlines():
        line = normalize_resource_line(raw_line)
        if not line or any(phrase in line for phrase in RESOURCE_LINE_SKIP_PHRASES):
            continue
        if not any(k in line for k in ("获得", "得到", "收获", "带回", "采得", "采摘", "奖励", "消耗", "扣除", "花费", "失去", "减少", "+", "＋", "-")):
            continue

        for match in re.finditer(
            r"(?:获得|得到|收获|带回|采得|采摘|奖励)(?:了)?\s*[【\[]([^】\]]+)[】\]]\s*(?:[x×*]\s*(\d+)|(\d+)\s*(?:个|枚|份|株|件|颗|块|瓶|张|缕|滴))?",
            line,
        ):
            amount = match.group(2) or match.group(3) or 1
            add_resource_change(changes, match.group(1), amount, line=line, source="获得")

        for match in re.finditer(
            r"(?:消耗|扣除|花费|失去)(?:了)?\s*[【\[]([^】\]]+)[】\]]\s*(?:[x×*]\s*(\d+)|(\d+)\s*(?:个|枚|份|株|件|颗|块|瓶|张|缕|滴))?",
            line,
        ):
            amount = match.group(2) or match.group(3) or 1
            add_resource_change(changes, match.group(1), -parse_log_int(amount), line=line, source="消耗")

        for match in re.finditer(
            rf"(?:获得|得到|收获|带回|奖励|增加)(?:了)?\s*([+-]?\d[\d,]*)\s*(?:{'|'.join(RESOURCE_UNITS)})?\s*([\u4e00-\u9fffA-Za-z0-9_·]+)",
            line,
        ):
            add_resource_change(changes, match.group(2), match.group(1), line=line, source="获得")

        for match in re.finditer(
            rf"(?:消耗|扣除|花费|失去|减少)(?:了)?\s*(\d[\d,]*)\s*(?:{'|'.join(RESOURCE_UNITS)})?\s*([\u4e00-\u9fffA-Za-z0-9_·]+)",
            line,
        ):
            add_resource_change(changes, match.group(2), -parse_log_int(match.group(1)), line=line, source="消耗")

        for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9_·]{1,24})\s*[+＋]\s*(\d[\d,]*)", line):
            add_resource_change(changes, match.group(1), match.group(2), line=line, source="获得")
        for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9_·]{1,24})\s*[-－]\s*(\d[\d,]*)", line):
            add_resource_change(changes, match.group(1), -parse_log_int(match.group(2)), line=line, source="消耗")

    deduped = []
    seen = set()
    for item in changes:
        key = (item["name"], item["amount"], item["line"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped

def parse_inventory_items_from_text(text, command=""):
    """Parse a latest inventory snapshot from 储物袋/背包 style replies."""
    raw = str(text or "")
    if not raw or not is_inventory_snapshot_text(raw, command=command):
        return []
    items = {}
    for raw_line in raw.splitlines():
        line = normalize_resource_line(raw_line)
        if not line or any(phrase in line for phrase in RESOURCE_LINE_SKIP_PHRASES):
            continue
        for match in re.finditer(r"[【\[]([^】\]]+)[】\]]\s*(?:[x×*]\s*)?(\d[\d,]*)", line):
            name = clean_resource_name(match.group(1))
            if name:
                items[name] = items.get(name, 0) + parse_log_int(match.group(2))
        match = re.match(r"^\s*[-*]?\s*([^:：\n]{1,32})\s*[:：]\s*(\d[\d,]*)\s*$", line)
        if match:
            name = clean_resource_name(match.group(1))
            if name:
                items[name] = items.get(name, 0) + parse_log_int(match.group(2))
    return [
        {"name": name, "amount": amount}
        for name, amount in sorted(items.items(), key=lambda item: (-item[1], item[0]))
    ]

def empty_resource_account(account):
    return {
        "name": ACCOUNT_DISPLAY_NAMES.get(account, account),
        "resources": [],
        "recent_events": [],
        "inventory": [],
        "_resource_map": {},
        "_inventory_map": {},
    }

def build_resource_stats(since_hours=12, max_rows=RESOURCE_STATS_MAX_ROWS, event_limit=60, inventory_limit=40):
    """Build a lightweight resource/inventory view from message_events.sqlite3."""
    path = message_events_db_path()
    now = datetime.now()
    since_hours = max(1, min(int(since_hours or 12), 168))
    max_rows = max(100, min(int(max_rows or RESOURCE_STATS_MAX_ROWS), 3000))
    event_limit = int(event_limit or 60)
    inventory_limit = int(inventory_limit or 40)
    cache_key = json.dumps({
        "since_hours": since_hours,
        "max_rows": max_rows,
        "event_limit": event_limit,
        "inventory_limit": inventory_limit,
    }, sort_keys=True)
    with RESOURCE_STATS_LOCK:
        cached = RESOURCE_STATS_CACHE.get("data")
        if (
            cached
            and RESOURCE_STATS_CACHE.get("key") == cache_key
            and time.time() - float(RESOURCE_STATS_CACHE.get("at") or 0) < RESOURCE_STATS_CACHE_SECONDS
        ):
            return cached

    cutoff = (now - timedelta(hours=since_hours)).strftime(TIME_FORMAT)
    try:
        stat = os.stat(path)
        signature = {
            "mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
            "size": int(getattr(stat, "st_size", 0) or 0),
            "since_hours": since_hours,
            "max_rows": max_rows,
            "event_limit": int(event_limit or 60),
            "inventory_limit": int(inventory_limit or 40),
        }
    except OSError:
        stat = None
        signature = None

    payload = {
        "ok": False,
        "status": "missing",
        "updated_at": now.strftime(TIME_FORMAT),
        "since_hours": since_hours,
        "accounts": {account: empty_resource_account(account) for account in WINDOW_MAP},
        "recent_events": [],
        "notes": [
            "资源变化从机器人回复里提取，不会改 state。",
            "库存只取最近一次储物袋/背包类回复快照；没发过相关指令就会为空。",
        ],
    }
    if stat is None:
        payload["error"] = "message_events.sqlite3 不存在"
        return payload

    try:
        with sqlite3.connect(path, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM message_events LIMIT 1").fetchone()
            ensure_message_health_indexes(conn)
            rows = conn.execute(
                """
                SELECT
                    me.id,
                    me.account,
                    CASE
                        WHEN COALESCE(me.identity, '') != '' THEN me.identity
                        ELSE COALESCE(cl.identity, '')
                    END AS identity,
                    CASE
                        WHEN COALESCE(me.command, '') != '' THEN me.command
                        ELSE COALESCE(cl.command, '')
                    END AS command,
                    me.direction,
                    me.event_kind,
                    me.msg_id,
                    me.text_hash,
                    me.text,
                    me.created_at,
                    CASE WHEN cl.command_msg_id IS NULL THEN 0 ELSE 1 END AS ledger_match
                FROM message_events me
                LEFT JOIN command_ledger cl INDEXED BY idx_command_ledger_account_msg
                  ON cl.account=me.account
                 AND cl.command_msg_id=me.reply_to_msg_id
                 AND (cl.chat_id IS me.chat_id OR cl.chat_id IS NULL OR me.chat_id IS NULL)
                WHERE me.created_at>=?
                  AND me.is_game_bot=1
                  AND me.text IS NOT NULL
                  AND (
                      COALESCE(me.identity, '') != ''
                      OR COALESCE(me.command, '') != ''
                      OR cl.command_msg_id IS NOT NULL
                  )
                ORDER BY me.created_at DESC, me.id DESC
                LIMIT ?
                """,
                (cutoff, max_rows),
            ).fetchall()

        seen_messages = set()
        for row in rows:
            account = row["account"] if row["account"] in WINDOW_MAP else str(row["account"] or "")
            if not account:
                continue
            account_data = payload["accounts"].setdefault(account, empty_resource_account(account))
            identity = str(row["identity"] or "主魂").strip() or "主魂"
            command = str(row["command"] or "").strip()
            text = row["text"] or ""
            msg_key = (account, row["msg_id"] if row["msg_id"] is not None else row["id"])
            if msg_key in seen_messages:
                continue
            seen_messages.add(msg_key)
            if not resource_text_matches_identity(account, identity, text):
                continue

            inventory_items = parse_inventory_items_from_text(text, command=command)
            if inventory_items:
                inventory_map = account_data["_inventory_map"]
                if identity not in inventory_map:
                    inventory_map[identity] = {
                        "identity": identity,
                        "time": row["created_at"],
                        "command": command,
                        "items": inventory_items[:int(inventory_limit or 40)],
                    }

            changes = parse_resource_changes_from_text(text)
            if not changes:
                continue
            event = {
                "time": row["created_at"],
                "account": account,
                "account_name": ACCOUNT_DISPLAY_NAMES.get(account, account),
                "identity": identity,
                "command": command,
                "changes": changes,
                "line": changes[0].get("line", ""),
            }
            payload["recent_events"].append(event)
            account_data["recent_events"].append(event)
            resource_map = account_data["_resource_map"]
            for change in changes:
                key = (identity, change["name"])
                bucket = resource_map.setdefault(key, {
                    "identity": identity,
                    "name": change["name"],
                    "gain": 0,
                    "loss": 0,
                    "net": 0,
                    "count": 0,
                    "last_time": "",
                    "last_line": "",
                })
                amount = int(change["amount"])
                if amount > 0:
                    bucket["gain"] += amount
                else:
                    bucket["loss"] += abs(amount)
                bucket["net"] = bucket["gain"] - bucket["loss"]
                bucket["count"] += 1
                if not bucket["last_time"] or str(row["created_at"]) > bucket["last_time"]:
                    bucket["last_time"] = row["created_at"]
                    bucket["last_line"] = change.get("line", "")

        payload["recent_events"] = payload["recent_events"][:event_limit]
        total_resource_rows = 0
        total_inventory_rows = 0
        for account_data in payload["accounts"].values():
            resources = list(account_data.pop("_resource_map", {}).values())
            resources.sort(key=lambda item: (item.get("last_time", ""), abs(item.get("net", 0))), reverse=True)
            account_data["resources"] = resources[:40]
            account_data["recent_events"] = account_data["recent_events"][:20]
            inventory = list(account_data.pop("_inventory_map", {}).values())
            inventory.sort(key=lambda item: item.get("time", ""), reverse=True)
            account_data["inventory"] = inventory[:8]
            total_resource_rows += len(resources)
            total_inventory_rows += len(inventory)

        payload["ok"] = True
        payload["status"] = "ok" if (total_resource_rows or total_inventory_rows) else "empty"
        payload["resource_count"] = total_resource_rows
        payload["inventory_count"] = total_inventory_rows
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = str(exc)

    if signature:
        with RESOURCE_STATS_LOCK:
            RESOURCE_STATS_CACHE["key"] = cache_key
            RESOURCE_STATS_CACHE["at"] = time.time()
            RESOURCE_STATS_CACHE["data"] = payload
    return payload


# =====================================================================
# 修为统计
# =====================================================================

def default_cultivation_profile():
    """默认修为档案"""
    return {"level": "", "current": None, "required": None, "current_text": "", "updated_at": "",
            "estimated_current": None, "estimated_current_text": "", "estimated_updated_at": "", "delta_after_snapshot": 0}

def empty_cultivation_day(date_key):
    return {"date": date_key, "gain": 0, "loss": 0, "net": 0}

def empty_cultivation_stats(stat=None):
    return {"version": CULTIVATION_STATS_VERSION, "log_size": int(getattr(stat, "st_size", 0) or 0), "log_mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
            "processed_offset": 0, "processed_until": "", "last_change_at": "", "days": {}, "profile": default_cultivation_profile(), "recent_events": []}

def cultivation_cache_path():
    return os.path.join(CONFIG_DIR, CULTIVATION_CACHE_FILE)

def load_cultivation_cache_store():
    """加载修为统计缓存"""
    if CULTIVATION_CACHE.get("_loaded"):
        return CULTIVATION_CACHE.setdefault("accounts", {})
    path = cultivation_cache_path()
    accounts = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            accounts = raw.get("accounts", raw) if isinstance(raw, dict) else {}
            if not isinstance(accounts, dict):
                accounts = {}
        except Exception:
            accounts = {}
    CULTIVATION_CACHE.clear()
    CULTIVATION_CACHE["_loaded"] = True
    CULTIVATION_CACHE["accounts"] = accounts
    return accounts

def save_cultivation_cache_store():
    """保存修为统计缓存"""
    path = cultivation_cache_path()
    tmp_path = f"{path}.tmp"
    data = {"version": CULTIVATION_STATS_VERSION, "updated_at": time.strftime(TIME_FORMAT), "accounts": CULTIVATION_CACHE.get("accounts", {})}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)

def cultivation_profile_from_entry(entry, account=None):
    """从日志条目中提取修为档案（境界、当前修为/需求）"""
    if not is_probable_bot_reply_log_entry(entry):
        return None
    text = entry.get("text") or "\n".join(entry.get("lines") or [])
    if not entry_belongs_to_main_profile(account, entry):
        return None
    # 过滤掉分身日志，避免主魂境界被分身污染
    if is_avatar_log_entry(entry):
        return None
    level_match = LOG_LEVEL_RE.search(text)
    cultivation_match = LOG_CULTIVATION_RE.search(text)
    spirit_root_match = LOG_SPIRIT_ROOT_RE.search(text)
    if not level_match and not cultivation_match and not spirit_root_match:
        return None
    updated_at = entry["lines"][0][:19] if entry.get("lines") else ""
    profile = {"updated_at": updated_at, "_has_exp_snapshot": False}
    if level_match:
        profile["level"] = level_match.group(1).strip()
    if cultivation_match:
        current = parse_log_int(cultivation_match.group(1))
        required = parse_log_int(cultivation_match.group(2))
        profile.update({
            "current": current,
            "required": required,
            "current_text": f"{current} / {required}" if required else str(current),
            "estimated_current": current,
            "estimated_current_text": f"{current} / {required}" if required else str(current),
            "estimated_updated_at": updated_at,
            "delta_after_snapshot": 0,
            "_has_exp_snapshot": True,
        })
    if spirit_root_match:
        spirit_root = spirit_root_match.group(1).replace("*", "").strip()
        if spirit_root:
            profile["spirit_root"] = spirit_root
    return profile

def apply_cultivation_profile_update(stats, update):
    has_exp_snapshot = bool(update.pop("_has_exp_snapshot", False))
    profile = default_cultivation_profile()
    profile.update(stats.get("profile") or {})
    for key in ("level", "spirit_root"):
        if update.get(key):
            profile[key] = update[key]
    if has_exp_snapshot:
        for key in ("current", "required", "current_text", "estimated_current", "estimated_current_text",
                    "estimated_updated_at", "delta_after_snapshot"):
            profile[key] = update.get(key)
    if update.get("updated_at"):
        profile["updated_at"] = update["updated_at"]
    stats["profile"] = profile
    return has_exp_snapshot

def parse_time_value(value):
    if not value: return None
    try:
        from datetime import datetime
        return datetime.strptime(str(value), TIME_FORMAT)
    except: return None

def recent_event_map(stats):
    events = {}
    for item in stats.get("recent_events", []) or []:
        try:
            source = str(item.get("source", ""))
            delta = int(item.get("delta", 0))
            dt = parse_time_value(item.get("time", ""))
            if source and delta and dt:
                events[(source, delta)] = dt
        except: continue
    return events

def save_recent_event_map(stats, events, latest_time=None):
    if latest_time is None and events:
        latest_time = max(events.values())
    rows = []
    for (source, delta), dt in events.items():
        if latest_time is not None and (latest_time - dt).total_seconds() > 300:
            continue
        rows.append({"source": source, "delta": delta, "time": dt.strftime(TIME_FORMAT)})
    rows.sort(key=lambda item: item["time"], reverse=True)
    stats["recent_events"] = rows[:100]

def apply_cultivation_change(stats, date_key, delta):
    days = stats.setdefault("days", {})
    bucket = days.setdefault(date_key, empty_cultivation_day(date_key))
    if delta > 0: bucket["gain"] = int(bucket.get("gain", 0)) + delta
    else: bucket["loss"] = int(bucket.get("loss", 0)) + abs(delta)
    bucket["net"] = int(bucket.get("gain", 0)) - int(bucket.get("loss", 0))

def apply_cultivation_estimate_change(stats, delta, entry_time):
    profile = stats.get("profile") or {}
    current = profile.get("estimated_current") or profile.get("current")
    if current is None: return
    required = profile.get("required")
    current = int(current) + int(delta)
    profile["estimated_current"] = current
    profile["estimated_current_text"] = f"{current} / {required}" if required else str(current)
    profile["estimated_updated_at"] = entry_time.strftime(TIME_FORMAT)
    profile["delta_after_snapshot"] = int(profile.get("delta_after_snapshot") or 0) + int(delta)
    stats["profile"] = profile

def latest_log_time_from_text(text):
    latest = None
    for line in (text or "").splitlines():
        match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}", line)
        if not match: continue
        dt = parse_time_value(match.group(1))
        if dt and (latest is None or dt > latest): latest = dt
    return latest

def process_cultivation_entries(stats, entries, account=None):
    """处理日志条目，提取修为变化"""
    recent_events = recent_event_map(stats)
    latest_time = parse_time_value(stats.get("processed_until", ""))
    changed = False
    for entry in entries:
        entry_time = parse_log_entry_time(entry)
        if entry_time is None: continue
        if latest_time is None or entry_time > latest_time:
            latest_time = entry_time
        profile = cultivation_profile_from_entry(entry, account=account)
        profile_updated = False
        if profile:
            profile_updated = apply_cultivation_profile_update(stats, profile); changed = True
        for change in extract_cultivation_changes(entry, account=account):
            source = change["source"]; delta = int(change["delta"])
            dedupe_key = (source, delta)
            previous_time = recent_events.get(dedupe_key)
            if previous_time is not None and 0 <= (entry_time - previous_time).total_seconds() <= CULTIVATION_DEDUPE_SECONDS:
                continue
            recent_events[dedupe_key] = entry_time
            apply_cultivation_change(stats, entry_time.strftime("%Y-%m-%d"), delta)
            if not profile_updated: apply_cultivation_estimate_change(stats, delta, entry_time)
            stats["last_change_at"] = entry_time.strftime(TIME_FORMAT); changed = True
    if latest_time is not None: stats["processed_until"] = latest_time.strftime(TIME_FORMAT)
    save_recent_event_map(stats, recent_events, latest_time=latest_time)
    return changed

def process_cultivation_text(stats, text, account=None):
    if not text: return False
    entries = decorate_log_entries(split_log_entries(text.splitlines()))
    return process_cultivation_entries(stats, entries, account=account)

def cultivation_response_from_stats(stats, error=""):
    today_key = time.strftime("%Y-%m-%d")
    days = dict(stats.get("days") or {})
    today = dict(days.get(today_key) or empty_cultivation_day(today_key))
    days[today_key] = today
    ordered_days = sorted(days.values(), key=lambda item: item.get("date", ""), reverse=True)
    return {"today": today, "days": ordered_days, "profile": stats.get("profile") or default_cultivation_profile(),
            "processed_until": stats.get("processed_until", ""), "last_change_at": stats.get("last_change_at", ""),
            "processed_offset": stats.get("processed_offset", 0), "error": error}

def add_cultivation_change(changes, source, delta, line=""):
    delta = parse_log_int(delta)
    if not delta: return
    changes.append({"source": source or "日志", "delta": delta, "line": line.strip()})

def extract_cultivation_changes(entry, account=None):
    """从游戏机器人回复的日志条目中提取修为变化值"""
    if not is_probable_bot_reply_log_entry(entry): return []
    text = entry.get("text") or "\n".join(entry.get("lines") or [])
    if entry_profile_mentions_other_account(account, entry):
        return []
    # 过滤掉分身日志，避免主魂修为收益被分身污染
    if is_avatar_log_entry(entry):
        return []
    if "修为不足" in text: return []
    changes = []
    command = first_command_tag(entry)

    # 优先匹配各种修为变化格式
    match = re.search(r"本次深度闭关，你的修为最终变化了\s*\*?\*?([+-]?\d[\d,]*)\*?\*?\s*点", text)
    if match: add_cultivation_change(changes, "深度闭关", match.group(1), match.group(0)); return changes
    match = re.search(r"本次闭关，你的修为最终增加了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", text)
    if match: add_cultivation_change(changes, "闭关修炼", match.group(1), match.group(0)); return changes
    match = re.search(r"修为结算[:：]\s*\*?\*?([+-]?\d[\d,]*)\*?\*?", text)
    if match: add_cultivation_change(changes, "共历心劫", match.group(1), match.group(0)); return changes
    # 逐行匹配各种消耗/获得
    skip_phrases = ("当前修为", "**修为**", "基础修为:", "基础修为增加", "每日被动修为", "红袖添香", "灵犀双运",
                    "同门道友", "可以使用 `.重置古塔`", "今日已挑战失败", "预计消耗",
                    "被动将灵气转化为修为", "前路已被", "消耗修为来", "修为惩罚", "修为加成",
                    "施展需消耗修为")
    for line in text.splitlines()[1:]:
        clean = line.strip()
        if not clean or "修为" not in clean: continue
        if any(phrase in clean for phrase in skip_phrases): continue
        line_changes = []
        for amount in re.findall(r"(?:消耗了|开始消耗)\D{0,40}?\*?\*?(\d[\d,]*)\*?\*?\s*点\s*修为", clean):
            line_changes.append((command or "消耗", -parse_log_int(amount)))
        for amount in re.findall(r"你消耗了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_changes.append((command or "消耗", -parse_log_int(amount)))
        for amount in re.findall(r"本次消耗[:：]\s*\*?\*?(\d[\d,]*)\s*修为\*?\*?", clean):
            line_changes.append((command or "消耗", -parse_log_int(amount)))
        for amount in re.findall(r"额外损失了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_changes.append((command or "损失", -parse_log_int(amount)))
        for amount in re.findall(r"修为折损\s*\*?\*?(-?\d[\d,]*)\*?\*?", clean):
            line_changes.append((command or "野外历练", -abs(parse_log_int(amount))))
        for amount in re.findall(r"修为\*?\*?额外倒退了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_changes.append((command or "倒退", -parse_log_int(amount)))
        for amount in re.findall(r"修为\*?\*?倒退了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_changes.append((command or "倒退", -parse_log_int(amount)))
        for amount in re.findall(r"修为\s*\*?\*?损失了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_changes.append((command or "损失", -parse_log_int(amount)))
        for amount in re.findall(r"修为\*?\*?减少了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_changes.append((command or "减少", -parse_log_int(amount)))
        for amount in re.findall(r"减少了\s*\*?\*?(\d[\d,]*)\s*点\*?\*?\s*修为", clean):
            line_changes.append((command or "减少", -parse_log_int(amount)))
        for amount in re.findall(r"本次获得\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"获得了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"额外获得\s*\*?\*?(\d[\d,]*)\*?\*?\s*点修为", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"(?:获得|额外获得)\s*\*?\*?(\d[\d,]*)\s*修为\*?\*?", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"折算为修为\s*(\d[\d,]*)", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"获得修为\s*\*?\*?([+-]?\d[\d,]*)\*?\*?", clean):
            line_changes.append((command or "野外历练", parse_log_int(amount)))
        for amount in re.findall(r"修为\s*([+-]\d[\d,]*)", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"修为\s*\*?\*?额外增加了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        for amount in re.findall(r"修为\s*\*?\*?增加了\s*\*?\*?(\d[\d,]*)\*?\*?\s*点", clean):
            line_changes.append((command or "获得", parse_log_int(amount)))
        seen = set()
        for source, delta in line_changes:
            key = (source, delta)
            if delta and key not in seen:
                seen.add(key)
                add_cultivation_change(changes, source, delta, clean)
    return changes

def latest_cultivation_profile(entries, account=None):
    stats = {"profile": default_cultivation_profile()}
    for entry in entries:
        entry_profile = cultivation_profile_from_entry(entry, account=account)
        if entry_profile:
            apply_cultivation_profile_update(stats, entry_profile)
    return stats["profile"]

def get_cultivation_summary(name):
    """获取账号的修为统计摘要（增量读取日志）"""
    # ... (完整实现见原文件)
    filename = get_log_filename(name)
    path = os.path.join(CONFIG_DIR, filename)
    if not os.path.exists(path):
        return {"today": {"date": time.strftime("%Y-%m-%d"), "gain": 0, "loss": 0, "net": 0}, "days": [], "profile": {}, "error": f"日志文件 {filename} 不存在。"}
    with CULTIVATION_LOCK:
        try:
            stat = os.stat(path)
            accounts = load_cultivation_cache_store()
            stats = accounts.get(name)
            if not isinstance(stats, dict) or int(stats.get("version", 0)) < CULTIVATION_STATS_VERSION:
                stats = empty_cultivation_stats(stat)
                with open(path, "rb") as f: text = f.read().decode("utf-8", errors="ignore")
                process_cultivation_text(stats, text, account=name)
                stats["processed_offset"] = int(stat.st_size); stats["log_size"] = int(stat.st_size); stats["log_mtime_ns"] = int(stat.st_mtime_ns)
                accounts[name] = stats; save_cultivation_cache_store()
                return cultivation_response_from_stats(stats)
            processed_offset = int(stats.get("processed_offset", 0) or 0)
            cached_size = int(stats.get("log_size", 0) or 0); cached_mtime_ns = int(stats.get("log_mtime_ns", 0) or 0)
            if int(stat.st_size) == cached_size and int(stat.st_mtime_ns) == cached_mtime_ns:
                return cultivation_response_from_stats(stats)
            if int(stat.st_size) < processed_offset:
                stats = empty_cultivation_stats(stat)
                with open(path, "rb") as f: text = f.read().decode("utf-8", errors="ignore")
                process_cultivation_text(stats, text, account=name)
            else:
                with open(path, "rb") as f: f.seek(processed_offset); text = f.read().decode("utf-8", errors="ignore")
                if "修为" in text: process_cultivation_text(stats, text, account=name)
                else:
                    latest_time = latest_log_time_from_text(text)
                    if latest_time: stats["processed_until"] = latest_time.strftime(TIME_FORMAT)
            stats["processed_offset"] = int(stat.st_size); stats["log_size"] = int(stat.st_size); stats["log_mtime_ns"] = int(stat.st_mtime_ns)
            accounts[name] = stats; save_cultivation_cache_store()
            return cultivation_response_from_stats(stats)
        except Exception as e:
            return {"today": {"date": time.strftime("%Y-%m-%d"), "gain": 0, "loss": 0, "net": 0}, "days": [], "profile": {}, "error": str(e)}


# =====================================================================
# 日志过滤与查询
# =====================================================================

def log_entry_header(entry):
    lines = entry.get("lines") or []
    if lines:
        return str(lines[0] or "")
    text = str(entry.get("text") or "")
    return text.splitlines()[0] if text else ""

def entry_matches_log_kind(entry, kind=""):
    kind = str(kind or "").strip().lower()
    if not kind:
        return True
    header = log_entry_header(entry)
    if kind == "out":
        return " OUT " in f" {header} "
    if kind == "in":
        return " IN " in f" {header} "
    if kind == "warn":
        return "[WARNING]" in header
    if kind == "error":
        return "[ERROR]" in header or "[CRITICAL]" in header
    if kind == "issue":
        return any(marker in header for marker in ("[WARNING]", "[ERROR]", "[CRITICAL]"))
    return True

def filter_log_entries(entries, tag="", q="", kind=""):
    """按标签和关键词过滤日志条目"""
    tag = (tag or "").strip(); q = (q or "").strip().lower()
    filtered = []
    for entry in entries:
        if kind and not entry_matches_log_kind(entry, kind):
            continue
        if tag:
            if tag == OTHER_LOG_TAG:
                if tag not in entry["tags"]: continue
            elif tag not in entry["tags"] and tag not in entry.get("related_tags", set()): continue
        if q and q not in entry["text"].lower(): continue
        filtered.append(entry)
    return filtered

def get_log_tags(name, include_counts=True):
    """获取日志可用的分类标签列表"""
    if not include_counts:
        seen = set()
        ordered = []
        for tag in ACCOUNT_LOG_TAGS.get(name, []):
            if tag in seen:
                continue
            ordered.append({"tag": tag, "count": None})
            seen.add(tag)
        if OTHER_LOG_TAG not in seen:
            ordered.append({"tag": OTHER_LOG_TAG, "count": None})
        return {"tags": ordered, "error": "", "partial": True}

    entries, error = read_log_entries(name)
    counts = {}
    for entry in entries:
        for tag in entry["tags"]: counts[tag] = counts.get(tag, 0) + 1
    seen = set(); ordered = []
    for tag in ACCOUNT_LOG_TAGS.get(name, []):
        ordered.append({"tag": tag, "count": counts.get(tag, 0)}); seen.add(tag)
    extra = [{"tag": tag, "count": count} for tag, count in counts.items() if tag not in seen]
    ordered.extend(extra)
    ordered.sort(key=lambda item: (-item["count"], item["tag"]))
    return {"tags": ordered, "error": error}

def get_log_page(name, before=None, limit=80, tag="", q="", kind=""):
    """获取分页的日志内容"""
    limit = max(20, min(int(limit or 80), 200))
    kind = (kind or "").strip().lower()
    if kind not in {"", "out", "in", "warn", "error", "issue"}:
        kind = ""
    if not (tag or q or kind):
        entries, error, meta = read_recent_log_entries(name, before_byte=before, limit=limit)
        if error:
            return {"content": error, "entries": [], "start": 0, "end": 0, "total": 0, "matched": 0, "has_more": False, "next_before": None, "partial": True}
        return {
            "content": "\n\n".join(entry["text"] for entry in entries),
            "entries": [entry["text"] for entry in entries],
            "start": 0,
            "end": len(entries),
            "total": None,
            "matched": len(entries),
            "loaded": len(entries),
            "has_more": bool(meta.get("has_more")),
            "next_before": meta.get("next_before"),
            "tag": tag,
            "q": q,
            "kind": kind,
            "partial": True,
            "cursor_mode": meta.get("cursor_mode", "byte"),
            "log_size": meta.get("log_size", 0),
        }

    entries, error = read_log_entries(name)
    if error:
        return {"content": error, "start": 0, "end": 0, "total": 0, "matched": 0, "has_more": False, "next_before": None}
    filtered = filter_log_entries(entries, tag=tag, q=q, kind=kind)
    total = len(entries); matched = len(filtered)
    end = matched if before is None else max(0, min(int(before), matched))
    start = max(0, end - limit)
    page_entries = filtered[start:end]
    return {"content": "\n\n".join(entry["text"] for entry in page_entries),
            "entries": [entry["text"] for entry in page_entries],
            "start": start, "end": end, "total": total, "matched": matched,
            "has_more": start > 0, "next_before": start if start > 0 else None, "tag": tag, "q": q, "kind": kind}


# =====================================================================
# 进程管理
# =====================================================================

def get_process_status(account):
    """检测脚本进程是否在运行"""
    script = SCRIPT_MAP.get(account)
    if not script: return False
    try:
        if os.name == 'nt':
            ps_cmd = f'powershell -Command "Get-WmiObject Win32_Process -Filter \\"name=\'python.exe\' or name=\'pythonw.exe\'\\" | Where-Object {{$_.CommandLine -match \'{script}\'}}"'
            return bool(subprocess.check_output(ps_cmd, shell=True, stderr=subprocess.DEVNULL).decode('utf-8', errors='ignore').strip())
        else:
            return bool(account_process_pids(account))
    except: return False

def account_process_pids(account):
    """Return live python process ids for one account script."""
    script = SCRIPT_MAP.get(account)
    if not script:
        return []
    result = subprocess.run(["pgrep", "-af", script], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    pids = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid_text, cmd = parts
        if script not in cmd or "python" not in cmd or "tmux " in cmd:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        pids.append(pid)
    return pids

def process_uptime_seconds(pid):
    """Return process uptime in seconds when ps is available."""
    if os.name == 'nt':
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return None
        return int(str(result.stdout or "").strip())
    except Exception:
        return None

def file_mtime_info(path):
    """Return dashboard-friendly file mtime details."""
    if not os.path.exists(path):
        return {"exists": False, "updated_at": "", "age_seconds": None, "size": 0}
    try:
        stat = os.stat(path)
        return {
            "exists": True,
            "updated_at": datetime.fromtimestamp(stat.st_mtime).strftime(TIME_FORMAT),
            "age_seconds": max(0, int(time.time() - stat.st_mtime)),
            "size": stat.st_size,
        }
    except Exception:
        return {"exists": False, "updated_at": "", "age_seconds": None, "size": 0}

def account_runtime_info(account, pids=None):
    """Build process and state-file metadata for one account."""
    script = SCRIPT_MAP.get(account, "")
    live_pids = list(pids) if pids is not None else (account_process_pids(account) if os.name != 'nt' else [])
    uptimes = [value for value in (process_uptime_seconds(pid) for pid in live_pids) if value is not None]
    state_path = os.path.join(CONFIG_DIR, f"state_{account}.json")
    return {
        "script": script,
        "pids": live_pids,
        "uptime_seconds": max(uptimes) if uptimes else None,
        "state": file_mtime_info(state_path),
    }

def git_command_output(args):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=CONFIG_DIR,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return ""
        return str(result.stdout or "").strip()
    except Exception:
        return ""

def deploy_version_metadata():
    """Read deployment metadata when the VPS deploy directory is not a git checkout."""
    path = os.path.join(CONFIG_DIR, DEPLOY_VERSION_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    commit = str(raw.get("commit") or "").strip()
    branch = str(raw.get("branch") or "").strip()
    return {
        "branch": branch or "unknown",
        "commit": commit,
        "commit_short": str(raw.get("commit_short") or commit[:7] or "unknown"),
        "dirty": False,
        "dirty_count": 0,
        "available": bool(commit),
        "checked_at": datetime.now().strftime(TIME_FORMAT),
        "deployed_at": str(raw.get("deployed_at") or ""),
        "source": "deploy_version",
    }

def git_metadata():
    """Return cached git metadata for the running deploy checkout."""
    now_ts = time.time()
    cached = GIT_META_CACHE.get("data")
    if cached and now_ts - float(GIT_META_CACHE.get("at") or 0) < GIT_META_CACHE_SECONDS:
        return cached
    branch = git_command_output(["branch", "--show-current"])
    commit = git_command_output(["rev-parse", "HEAD"])
    dirty_text = git_command_output(["status", "--short"])
    if commit:
        data = {
            "branch": branch or "unknown",
            "commit": commit,
            "commit_short": commit[:7],
            "dirty": bool(dirty_text),
            "dirty_count": len([line for line in dirty_text.splitlines() if line.strip()]),
            "available": True,
            "checked_at": datetime.now().strftime(TIME_FORMAT),
            "source": "git",
        }
    else:
        data = deploy_version_metadata() or {
            "branch": "unknown",
            "commit": "",
            "commit_short": "unknown",
            "dirty": False,
            "dirty_count": 0,
            "available": False,
            "checked_at": datetime.now().strftime(TIME_FORMAT),
            "source": "unavailable",
        }
    GIT_META_CACHE["at"] = now_ts
    GIT_META_CACHE["data"] = data
    return data

def dashboard_runtime_info(account_infos=None):
    """Build VPS/deploy metadata for the dashboard header."""
    return {
        "dashboard": {
            "pid": os.getpid(),
            "started_at": SERVER_STARTED_AT,
            "uptime_seconds": max(0, int(time.time() - SERVER_START_TS)),
        },
        "git": git_metadata(),
        "accounts": account_infos or {},
    }

def signal_account_processes(account, sig):
    """Signal only the python process for the selected account script."""
    for pid in account_process_pids(account):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

def start_account(account):
    """启动账号脚本"""
    idx = WINDOW_MAP[account]; script = SCRIPT_MAP[account]
    if os.name == 'nt':
        subprocess.Popen(["pythonw", script], cwd=CONFIG_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
    else:
        deploy_dir = os.path.expanduser("~/deploy")
        cmd = f"bash -lc 'cd {deploy_dir} && source {deploy_dir}/venv/bin/activate && exec python3 {script}'"
        subprocess.run(["tmux", "respawn-window", "-k", "-t", f"xiuxian:{idx}", cmd])

def stop_account(account):
    """停止账号脚本"""
    idx = WINDOW_MAP[account]; script = SCRIPT_MAP[account]
    if os.name == 'nt':
        ps_cmd = f'powershell -Command "Get-WmiObject Win32_Process -Filter \\"name=\'python.exe\' or name=\'pythonw.exe\'\\" | Where-Object {{$_.CommandLine -match \'{script}\'}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"'
        subprocess.run(ps_cmd, shell=True)
    else:
        subprocess.run(["tmux", "send-keys", "-t", f"xiuxian:{idx}", "C-c"])
        if not wait_for_status(account, False, timeout=6):
            signal_account_processes(account, signal.SIGINT)
        if not wait_for_status(account, False, timeout=6):
            signal_account_processes(account, signal.SIGTERM)

def wait_for_status(account, expected_alive, timeout=12):
    """等待账号进程达到预期状态"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if get_process_status(account) == expected_alive: return True
        time.sleep(0.5)
    return False


# =====================================================================
# 清屏功能
# =====================================================================

def clear_account_history(account):
    """清理由账号脚本发送的消息"""
    safe_console_print(f"[clear] requested for {account}", flush=True)
    was_alive = get_process_status(account)
    if was_alive:
        stop_account(account)
        if not wait_for_status(account, False, timeout=20):
            return {"success": False, "msg": f"{account_display_name(account)}停止超时，清屏未执行，避免 session 数据库锁。"}
        time.sleep(1.5)
    script_path = os.path.join(CONFIG_DIR, "clear_history.py")
    try:
        result = subprocess.run([sys.executable, script_path, account, "--older-than-minutes", "35"], cwd=CONFIG_DIR, capture_output=True, text=True, timeout=900)
    finally:
        if was_alive: start_account(account); wait_for_status(account, True)
    output = (result.stdout or result.stderr or "").strip()
    safe_console_print(f"[clear] {account}: rc={result.returncode}, {output}", flush=True)
    if result.returncode != 0: return {"success": False, "msg": output or "清屏失败"}
    return {"success": True, "msg": output or "清屏完成"}

def account_display_name(account):
    return {"main": "凌霄宫（主号）", "sub": "元婴宗（副号）", "xiaohao": "万灵宗（小号）"}.get(account, account)

def run_clear_job(job_id, account):
    """后台执行清屏任务"""
    with CLEAR_LOCK:
        CLEAR_JOBS[job_id]["status"] = "running"
        CLEAR_JOBS[job_id]["msg"] = f"{account_display_name(account)}清屏中，后台正在删除 35 分钟以前的游戏指令。"
    try:
        result = clear_account_history(account)
        with CLEAR_LOCK:
            CLEAR_JOBS[job_id]["status"] = "done" if result.get("success") else "failed"
            CLEAR_JOBS[job_id]["success"] = bool(result.get("success"))
            CLEAR_JOBS[job_id]["msg"] = result.get("msg", "清屏完成")
            CLEAR_JOBS[job_id]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        with CLEAR_LOCK:
            CLEAR_JOBS[job_id]["status"] = "failed"
            CLEAR_JOBS[job_id]["success"] = False
            CLEAR_JOBS[job_id]["msg"] = f"清屏失败：{e}"
            CLEAR_JOBS[job_id]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

def start_clear_job(account):
    """启动清屏任务（去重）"""
    with CLEAR_LOCK:
        for job_id, job in CLEAR_JOBS.items():
            if job.get("account") == account and job.get("status") == "running":
                return {"success": True, "job_id": job_id, "msg": f"{account_display_name(account)}已有清屏任务在运行，请稍等。"}
        job_id = uuid.uuid4().hex
        CLEAR_JOBS[job_id] = {"account": account, "status": "running", "success": None,
                               "msg": f"{account_display_name(account)}清屏已开始，完成后会提示结果。",
                               "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    worker = threading.Thread(target=run_clear_job, args=(job_id, account), daemon=True)
    worker.start()
    return {"success": True, "job_id": job_id, "msg": CLEAR_JOBS[job_id]["msg"]}


# =====================================================================
# FastAPI 路由
# =====================================================================

@app.get("/api/status")
def status(username: str = Depends(authenticate)):
    """获取所有账号的实时状态"""
    try:
        now_ts = time.time()
        with STATUS_LOCK:
            cached = STATUS_CACHE.get("data")
            if cached and now_ts - float(STATUS_CACHE.get("at") or 0) < STATUS_CACHE_SECONDS:
                return cached
            result = {}
            runtime_accounts = {}
            for key, info in ACCOUNT_DISPLAY_NAMES.items():
                state = get_state(key)
                pids = account_process_pids(key) if os.name != 'nt' else None
                process_info = account_runtime_info(key, pids=pids)
                runtime_accounts[key] = process_info
                result[key] = {
                    "name": info,
                    "state": state,
                    "is_alive": bool(pids) if os.name != 'nt' else get_process_status(key),
                    "process": process_info,
                    "cultivation": get_cultivation_summary(key),
                    "command_panels": build_command_panels(key, state),
                    "profile_usernames": account_profile_usernames(key),
                }
            payload = {
                "accounts": result,
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "runtime": dashboard_runtime_info(runtime_accounts),
            }
            STATUS_CACHE["at"] = now_ts
            STATUS_CACHE["data"] = payload
            return payload
    except Exception as e: return {"error": str(e)}

@app.get("/api/message-health")
def message_health(since_hours: int = 24, min_gap_seconds: int = 60,
                   min_missing_msg_ids: int = 20, limit: int = 8,
                   username: str = Depends(authenticate)):
    """消息箱水位和疑似断层审计。"""
    return build_message_health(
        since_hours=since_hours,
        min_gap_seconds=min_gap_seconds,
        min_missing_msg_ids=min_missing_msg_ids,
        limit=limit,
    )

@app.get("/api/resource-stats")
def resource_stats(since_hours: int = 12, max_rows: int = RESOURCE_STATS_MAX_ROWS,
                   username: str = Depends(authenticate)):
    """资源/库存统计已从 Dashboard 关闭，保留轻量响应兼容旧页面。"""
    return {
        "ok": False,
        "status": "disabled",
        "error": "资源与库存统计已关闭",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

@app.get("/api/command-records")
def command_records(username: str = Depends(authenticate)):
    """获取各账号按身份/指令聚合的发送记录。"""
    now_ts = time.time()
    with COMMAND_RECORD_ENDPOINT_LOCK:
        cached = COMMAND_RECORD_ENDPOINT_CACHE.get("data")
        if cached and now_ts - float(COMMAND_RECORD_ENDPOINT_CACHE.get("at") or 0) < COMMAND_RECORD_ENDPOINT_CACHE_SECONDS:
            return cached
        payload = build_all_command_records()
        COMMAND_RECORD_ENDPOINT_CACHE["at"] = now_ts
        COMMAND_RECORD_ENDPOINT_CACHE["data"] = payload
        return payload

@app.get("/api/logs/{name}")
def logs(name: str, before: Optional[int] = None, limit: int = 80, tag: str = "", q: str = "", kind: str = "",
         username: str = Depends(authenticate)):
    """获取账号的分页日志"""
    cache_key = json.dumps({
        "name": name,
        "before": before,
        "limit": max(1, min(int(limit or 80), 300)),
        "tag": tag or "",
        "q": q or "",
        "kind": kind or "",
    }, sort_keys=True, ensure_ascii=False)
    now_ts = time.time()
    with LOG_PAGE_LOCK:
        cached = LOG_PAGE_CACHE.get(cache_key)
        if cached and now_ts - float(cached.get("at") or 0) < LOG_PAGE_CACHE_SECONDS:
            return cached.get("data")
        payload = get_log_page(name, before=before, limit=limit, tag=tag, q=q, kind=kind)
        LOG_PAGE_CACHE[cache_key] = {"at": now_ts, "data": payload}
        if len(LOG_PAGE_CACHE) > 24:
            oldest_key = min(LOG_PAGE_CACHE, key=lambda key: LOG_PAGE_CACHE[key].get("at", 0))
            LOG_PAGE_CACHE.pop(oldest_key, None)
        return payload

@app.get("/api/log-tags/{name}")
def log_tags(name: str, counts: bool = True, username: str = Depends(authenticate)):
    """获取账号日志的分类标签"""
    if name not in WINDOW_MAP: return {"tags": [], "error": "未知账号"}
    return get_log_tags(name, include_counts=counts)

@app.get("/api/clear-status/{job_id}")
async def clear_status(job_id: str, username: str = Depends(authenticate)):
    """查询清屏任务状态"""
    with CLEAR_LOCK:
        job = CLEAR_JOBS.get(job_id)
        if not job: return {"success": False, "status": "missing", "msg": "未找到清屏任务。"}
        return dict(job)

@app.post("/api/command-control")
async def set_command_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """临时暂停/恢复某账号某身份的一条自动指令。"""
    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "主魂").strip() or "主魂"
    command = str(payload.get("command") or "").strip()
    label = str(payload.get("label") or command).strip()
    control_key = str(payload.get("control_key") or command_control_key(command)).strip()
    disabled = bool(payload.get("disabled"))
    default_paused = bool(payload.get("default_paused"))
    if account not in WINDOW_MAP:
        return {"success": False, "msg": "未知账号"}
    if not command or not control_key:
        return {"success": False, "msg": "指令为空"}

    with COMMAND_CONTROL_LOCK:
        data = load_command_controls()
        account_controls = data.setdefault(account, {})
        identity_controls = account_controls.setdefault(identity, {})
        if disabled:
            identity_controls[control_key] = {
                "disabled": True,
                "command": command,
                "label": label,
                "updated_at": datetime.now().strftime(TIME_FORMAT),
                "updated_by": username,
            }
        else:
            if default_paused:
                identity_controls[control_key] = {
                    "disabled": False,
                    "command": command,
                    "label": label,
                    "updated_at": datetime.now().strftime(TIME_FORMAT),
                    "updated_by": username,
                }
            else:
                identity_controls.pop(control_key, None)
                if not identity_controls:
                    account_controls.pop(identity, None)
                if not account_controls:
                    data.pop(account, None)
        save_command_controls(data)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "account": account,
        "identity": identity,
        "control_key": control_key,
        "disabled": disabled,
    }

@app.post("/api/custom-command")
async def upsert_custom_command(payload: dict = Body(...), username: str = Depends(authenticate)):
    """给 dashboard 的指定账号/身份添加或更新一条自定义指令展示项。"""
    account = clean_custom_text(payload.get("account"), 32)
    identity = clean_custom_text(payload.get("identity") or "主魂", 32) or "主魂"
    command = clean_custom_text(payload.get("command"), 160)
    label = clean_custom_text(payload.get("label"), 64) or clean_custom_text(command, 64)
    group = clean_custom_text(payload.get("group"), 32) or "自定义"
    detail = clean_custom_text(payload.get("detail"), 160) or "dashboard 手动添加"
    custom_id = clean_custom_text(payload.get("id") or payload.get("custom_id"), 64)
    interval_minutes = clean_custom_int(payload.get("interval_minutes"), default=0, min_value=0, max_value=60 * 24 * 30)
    timeout_seconds = clean_custom_int(payload.get("timeout_seconds"), default=45, min_value=10, max_value=180)
    max_retries = clean_custom_int(payload.get("max_retries"), default=0, min_value=0, max_value=2)
    schedule_enabled = bool(payload.get("schedule_enabled", False)) and interval_minutes > 0
    if account not in WINDOW_MAP:
        return {"success": False, "msg": "未知账号"}
    if not command:
        return {"success": False, "msg": "指令为空"}
    if not command.startswith("."):
        return {"success": False, "msg": "指令需要以 . 开头"}

    now = datetime.now().strftime(TIME_FORMAT)
    with CUSTOM_COMMAND_LOCK:
        data = load_custom_commands()
        account_data = data.setdefault(account, {})
        entries = account_data.get(identity)
        if not isinstance(entries, list):
            entries = []
            account_data[identity] = entries

        if custom_id:
            for idx, old in enumerate(entries):
                if isinstance(old, dict) and str(old.get("id") or "") == custom_id:
                    entries[idx] = {
                        "id": custom_id,
                        "command": command,
                        "label": label,
                        "group": group,
                        "detail": detail,
                        "schedule_enabled": schedule_enabled,
                        "interval_minutes": interval_minutes,
                        "timeout_seconds": timeout_seconds,
                        "max_retries": max_retries,
                        "created_at": old.get("created_at") or now,
                        "created_by": old.get("created_by") or username,
                        "updated_at": now,
                        "updated_by": username,
                    }
                    save_custom_commands(data)
                    return {"success": True, "command": entries[idx]}
            return {"success": False, "msg": "未找到自定义指令"}

        for old in entries:
            if isinstance(old, dict) and clean_custom_text(old.get("command"), 160) == command:
                return {"success": False, "msg": "同一身份下已存在该指令"}

        entry = {
            "id": uuid.uuid4().hex,
            "command": command,
            "label": label,
            "group": group,
            "detail": detail,
            "schedule_enabled": schedule_enabled,
            "interval_minutes": interval_minutes,
            "timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
            "created_at": now,
            "created_by": username,
            "updated_at": now,
            "updated_by": username,
        }
        entries.append(entry)
        save_custom_commands(data)
    return {"success": True, "command": entry}


@app.delete("/api/custom-command")
async def delete_custom_command(payload: dict = Body(...), username: str = Depends(authenticate)):
    """删除 dashboard 的一条自定义指令展示项。"""
    account = clean_custom_text(payload.get("account"), 32)
    identity = clean_custom_text(payload.get("identity") or "主魂", 32) or "主魂"
    custom_id = clean_custom_text(payload.get("id") or payload.get("custom_id"), 64)
    if account not in WINDOW_MAP:
        return {"success": False, "msg": "未知账号"}
    if not custom_id:
        return {"success": False, "msg": "缺少自定义指令 ID"}

    removed = None
    with CUSTOM_COMMAND_LOCK:
        data = load_custom_commands()
        account_data = data.get(account, {})
        if not isinstance(account_data, dict):
            return {"success": False, "msg": "未找到自定义指令"}
        entries = account_data.get(identity, [])
        if not isinstance(entries, list):
            return {"success": False, "msg": "未找到自定义指令"}
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("id") or "") == custom_id:
                removed = entry
            else:
                kept.append(entry)
        if not removed:
            return {"success": False, "msg": "未找到自定义指令"}
        if kept:
            account_data[identity] = kept
        else:
            account_data.pop(identity, None)
        if not account_data:
            data.pop(account, None)
        save_custom_commands(data)
    return {"success": True, "removed": removed, "updated_by": username}

@app.post("/api/action/{account}/{action}")
async def account_action(account: str, action: str, username: str = Depends(authenticate)):
    """执行账号操作（start/stop/restart/clear）"""
    if account not in WINDOW_MAP: return {"success": False, "msg": "未知账号"}
    try:
        if action == "clear": return start_clear_job(account)
        if os.name == 'nt':
            if action == "stop": stop_account(account); return {"success": True, "msg": f"{account} 已尝试停止"}
            elif action in ("start", "restart"): start_account(account); return {"success": True, "msg": f"{account} 已在后台拉起"}
        else:
            if action == "stop": stop_account(account); return {"success": True, "msg": f"{account} 已停止"}
            elif action in ("start", "restart"): start_account(account); return {"success": True, "msg": f"{account} 已启动/重启"}
    except Exception as e: return {"success": False, "msg": str(e)}

@app.get("/", response_class=HTMLResponse)
async def index(username: str = Depends(authenticate)):
    """前端页面"""
    html_path = os.path.join(CONFIG_DIR, 'dashboard.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})
    return "<h1>Dashboard UI File Missing</h1>"

if __name__ == "__main__":
    """启动服务（0.0.0.0:8000）"""
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
