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
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, status as http_status, Body
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from log_utils import command_control_key

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

# =====================================================================
# 路径与常量配置
# =====================================================================
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAR_JOBS = {}                          # 清屏任务状态
CLEAR_LOCK = threading.Lock()            # 清屏任务锁
COMMAND_CONTROL_LOCK = threading.Lock()  # 指令开关锁
CUSTOM_COMMAND_LOCK = threading.Lock()   # 自定义指令锁
CULTIVATION_CACHE = {}                   # 修为统计缓存
CULTIVATION_LOCK = threading.Lock()      # 修为统计锁
CULTIVATION_CACHE_FILE = "cultivation_stats_cache.json"
COMMAND_CONTROL_FILE = "command_controls.json"
CUSTOM_COMMAND_FILE = "dashboard_commands.json"
CULTIVATION_STATS_VERSION = 14  # rebuilt: merge username-owned profile snapshots
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
ALL_AVATARS = ["问心子", "素心子", "缘生子", "无咎子", "素缘子", "厚土", "寻真子"]
STAR_CONCUBINE_VOYAGE_IDENTITIES = {
    "main": {"素缘子"},
    "sub": {"厚土", "缘生子", "寻真子"},
    "xiaohao": {"素心子", "缘生子"},
}

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

# 双段指令（指令+参数需要组合）
TWO_PART_COMMANDS = {
    (".交换", "法宝"), (".抚摸法宝", "青竹蜂云剑"),
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
        ".召回侍妾", ".安置侍妾", ".元婴出窍", ".元婴归窍", ".探寻裂缝",
        ".抚摸法宝 青竹蜂云剑",
        ".野外历练 谨慎", ".宗门战况", ".参战", ".我的侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        OTHER_LOG_TAG,
    ],
    "sub": [
        ".闯塔", ".宗门点卯", ".宗门传功",
        ".启阵", ".助阵", ".强行出关",
        ".查看闭关", ".闭关修炼", ".深度闭关",
        ".召回侍妾", ".安置侍妾", ".每日问安",
        ".观星台", ".安抚星辰", ".收集精华", ".牵引星辰", ".观星", ".改换星移",
        ".元婴出窍", ".元婴归窍", ".探寻裂缝",
        ".抚摸法宝 青竹蜂云剑",
        ".野外历练 均衡", ".宗门战况", ".参战", ".我的侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        OTHER_LOG_TAG,
    ],
    "xiaohao": [
        ".闯塔", ".宗门点卯", ".宗门传功",
        ".寻觅灵兽", ".我的灵兽", ".放生", ".灵兽出战", ".灵兽休息",
        ".灵兽偷菜", ".灵兽探渊", ".一键放养", ".灵兽互动", ".灵兽巡游",
        ".查看闭关", ".闭关修炼", ".深度闭关", ".召回侍妾", ".安置侍妾",
        ".野外历练 谨慎", ".宗门战况", ".参战", ".抚摸法宝 青竹蜂云剑",
        ".元婴出窍", ".元婴归窍", ".探寻裂缝",
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


def command_control_disabled(controls, account, identity, control_key):
    account_controls = controls.get(account, {}) if isinstance(controls, dict) else {}
    if not isinstance(account_controls, dict):
        return False
    for ident in (identity or "主魂", "*"):
        ident_controls = account_controls.get(ident, {})
        if not isinstance(ident_controls, dict):
            continue
        entry = ident_controls.get(control_key)
        if isinstance(entry, dict):
            if entry.get("disabled"):
                return True
        elif entry:
            return True
    return False


def apply_command_controls(account, panel):
    controls = load_command_controls()
    identity = panel.get("identity") or "主魂"
    commands = panel.get("commands") or []
    for row in commands:
        control_key = command_control_key(row.get("command", ""))
        row["control_key"] = control_key
        row["control_disabled"] = command_control_disabled(controls, account, identity, control_key)
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
    scheduled = time_command(state, "next_force_exit_time", ".强行出关", "强行出关", waiting="已排程", ready="可检查", missing="未排程", group=group)
    if scheduled["tone"] == "cooldown":
        return scheduled
    if state.get("in_deep_meditation"):
        return command_row(".强行出关", "强行出关", "可出关", "ready", remaining="0秒", detail="当前处于深度闭关", group=group, schedule_type="cooldown", next_seconds=0)
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


def spirit_tree_command(state):
    status = state.get("spirit_tree_status", "灌溉期")
    mature_until = parse_state_time(state.get("spirit_tree_mature_until", ""))
    harvested = state.get("spirit_tree_harvested_in_mature_period") or state.get("spirit_tree_harvest_attempted_in_mature_period")
    if status == "成熟采摘期" and mature_until and mature_until > datetime.now():
        detail = "本期已采摘" if harvested else "等待采摘"
        return command_row(
            ".采摘灵果", "灵树状态", "成熟采摘期", "active",
            format_remaining((mature_until - datetime.now()).total_seconds()),
            str(state.get("spirit_tree_mature_until", "")), detail, "凌霄宫",
            schedule_type="cooldown", next_seconds=(mature_until - datetime.now()).total_seconds(),
        )
    irrigation = time_command(state, "next_spirit_tree_irrigation_time", ".灵树灌溉", "灵树灌溉", group="凌霄宫")
    irrigation["detail"] = status or irrigation.get("detail", "")
    return irrigation


def spirit_tree_guard_command(state):
    invasion = state.get("spirit_tree_invasion_status", "")
    row = time_command(
        state, "next_spirit_tree_guard_time", ".协同守山", "协同守山",
        waiting="守山冷却", ready="待来袭", missing="待来袭",
        detail=invasion, group="凌霄宫",
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
    return (identity or "主魂") in STAR_CONCUBINE_VOYAGE_IDENTITIES.get(account, set())


def concubine_commands(state, include_divination=True, include_voyage=False):
    rows = [
        manual_command(".我的侍妾", "我的侍妾", "查询侍妾/冷却", "侍妾"),
        time_command(state, "next_dream_map_time", ".入梦寻图", "入梦寻图", group="侍妾"),
        time_command(state, "next_heart_trial_time", ".共历心劫", "共历心劫", group="侍妾"),
        flow_command(".稳", "稳", "必须 reply 共历心劫回合消息", "侍妾"),
    ]
    if include_voyage:
        rows.insert(2, time_command(
            state, "next_concubine_voyage_time", ".侍妾远航 均衡", "侍妾远航",
            waiting="8小时冷却", group="侍妾",
        ))
    if include_divination:
        rows.append(time_command(state, "next_divination_time", ".天机代卜", "天机代卜", group="侍妾"))
    rows.append(manual_command(".拼图", "拼图", "残图满足时发送", "侍妾"))
    return rows


def main_soul_panel(account, state):
    rows = []
    rows.extend(global_sync_commands())
    if account == "main":
        rows.extend([
            manual_command(".借天门势", "借天门势", group="凌霄宫"),
            daily_done_command(state, ".闯塔", "闯塔", done_command=".闯塔", group="每日"),
            daily_done_command(state, ".宗门点卯", "宗门点卯", done_command=".宗门点卯", group="每日"),
            manual_command(".天阶状态", "天阶状态", "查询云阶状态", "凌霄宫"),
            time_command(state, "next_stairs_time", ".登天阶", "登天阶", group="凌霄宫"),
            time_command(state, "nine_heaven_wind_cd_time", ".引九天罡风", "引九天罡风", group="凌霄宫"),
            time_command(state, "next_heart_time", ".问心台", "问心台", group="凌霄宫"),
            time_command(state, "next_yuanying_out_time", ".元婴出窍", "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", ".探寻裂缝", "探寻裂缝", group="通用"),
            time_command(state, "next_treasure_touch_time", ".抚摸法宝 青竹蜂云剑（神雷版）", "抚摸法宝", group="法宝"),
            time_command(state, "next_nurture_spirit_time", ".温养器灵 青竹蜂云剑（神雷版）", "温养器灵", waiting="6小时冷却", group="法宝"),
        ])
        rows.extend(meditation_commands(state))
        rows.append(time_command(state, "next_field_training_time", ".野外历练 谨慎", "野外历练", group="通用"))
        rows.extend(sect_war_commands(state))
        rows.extend([
            manual_command(".安置侍妾", "安置侍妾", group="侍妾"),
        ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    elif account == "sub":
        rows.extend([
            daily_done_command(state, ".宗门点卯", "宗门点卯", done_command=".宗门点卯", group="每日"),
            daily_done_command(state, ".闯塔", "闯塔", done_command=".闯塔", group="每日"),
            time_command(state, "next_yuanying_out_time", ".元婴出窍", "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", ".探寻裂缝", "探寻裂缝", group="通用"),
            time_command(state, "next_treasure_touch_time", ".抚摸法宝 青竹蜂云剑", "抚摸法宝", group="法宝"),
            time_command(state, "next_field_training_time", ".野外历练 谨慎", "野外历练", group="通用"),
        ])
        rows.extend(sect_war_commands(state))
        rows.extend(meditation_commands(state, include_force_exit=True))
        rows.extend([
            time_command(state, "next_star_check_time", ".观星台", "观星台", group="星宫"),
            time_command(state, "next_star_check_time", ".安抚星辰", "安抚星辰", group="星宫"),
            manual_command(".收集精华", "收集精华", "随观星台状态触发", "星宫"),
            time_command(state, "display_next_star_attraction_time", ".牵引星辰 天雷星", "牵引星辰", group="星宫"),
            time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
            time_command(state, "pending_star_shift_target_time", ".改换星移 @Gamling33", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
            time_command(state, "next_formation_time", ".启阵", "启阵", group="阵法"),
            manual_command(".助阵", "助阵", "监听阵法邀请", "阵法"),
            manual_command(".每日问安", "每日问安", "按日问安", "侍妾"),
            manual_command(".安置侍妾", "安置侍妾", group="侍妾"),
        ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    elif account == "xiaohao":
        rows.extend([
            daily_done_command(state, ".闯塔", "闯塔", done_command=".闯塔", group="每日"),
            daily_done_command(state, ".宗门点卯", "宗门点卯", done_command=".宗门点卯", group="每日"),
            time_command(state, "next_yuanying_out_time", ".元婴出窍", "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", ".探寻裂缝", "探寻裂缝", group="通用"),
            time_command(state, "next_treasure_touch_time", ".抚摸法宝 青竹蜂云剑", "抚摸法宝", group="法宝"),
            time_command(state, "next_field_training_time", ".野外历练 谨慎", "野外历练", group="通用"),
        ])
        rows.extend(sect_war_commands(state))
        rows.extend(meditation_commands(state))
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
            time_command(state, "next_beast_cruise_time", ".灵兽巡游 六翼", "灵兽巡游", group="灵兽"),
        ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    return {"identity": "主魂", "role": "主魂", "commands": rows}


def lingxiao_avatar_commands(name, state):
    rows = []
    rows.extend(global_sync_commands())
    rows.extend(meditation_commands(state, include_force_exit=(name == "素缘子")))
    if name == "无咎子":
        rows.extend([
            manual_command(".推命 闭关", "推命闭关", group="推命"),
            manual_command(".推命 探索", "推命探索", group="推命"),
            time_command(state, "next_field_training_time", ".野外历练 深入", "野外历练", group="通用"),
        ])
    else:
        rows.append(time_command(state, "next_field_training_time", ".野外历练 谨慎", "野外历练", group="通用"))
    rows.append(daily_done_command(state, ".闯塔", "闯塔", date_key="last_tower_date", group="每日"))
    if name == "缘生子":
        rows.extend([spirit_tree_command(state), spirit_tree_guard_command(state)])
    if name == "素缘子":
        rows.extend([
            manual_command(".助阵", "助阵", "监听阵法邀请", "阵法"),
            daily_done_command(state, ".开启血色试炼", "血色试炼", date_key="last_blood_trial_date", group="血色"),
            flow_command(".进入血色试炼", "进入血色试炼", "血色试炼流程内", "血色"),
            flow_command(".血色抉择 2/2/3/2/2/4", "血色抉择", "血色试炼流程内", "血色"),
            manual_command(".观星", "观星", group="星宫"),
            manual_command(".改换星移 @Waaiging", "改换星移", group="星宫"),
        ])
    rows.extend(concubine_commands(state, include_divination=False, include_voyage=concubine_voyage_enabled("main", name)))
    rows.append(daily_done_command(state, ".宗门点卯", "宗门点卯", date_key="last_dianmao_date", group="每日"))
    return rows


def star_avatar_commands(name, state):
    rows = []
    rows.extend(global_sync_commands())
    rows.append(time_command(state, "next_field_training_time", ".野外历练 谨慎", "野外历练", group="通用"))
    rows.extend(meditation_commands(state, include_force_exit=True))
    rows.extend([
        time_command(state, "next_formation_time", ".启阵", "启阵", group="阵法"),
        manual_command(".助阵", "助阵", "监听阵法邀请", "阵法"),
        daily_done_command(state, ".闯塔", "闯塔", date_key="last_tower_date", group="每日"),
        daily_done_command(state, ".开启血色试炼", "血色试炼", date_key="last_blood_trial_date", group="血色"),
        flow_command(".进入血色试炼", "进入血色试炼", "血色试炼流程内", "血色"),
        flow_command(".血色抉择 2/2/3/2/2/4", "血色抉择", "血色试炼流程内", "血色"),
        time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
        time_command(state, "pending_star_gazing_target_time", ".观星", "待观星", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        time_command(state, "pending_star_shift_target_time", ".改换星移 @Gamling33", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        daily_done_command(state, ".宗门点卯", "宗门点卯", date_key="last_dianmao_date", group="每日"),
    ])
    rows.extend(concubine_commands(state, include_divination=False, include_voyage=concubine_voyage_enabled("sub", name)))
    return rows


def xiaohao_avatar_commands(name, state):
    rows = []
    rows.extend(global_sync_commands())
    rows.extend(meditation_commands(state, include_force_exit=(name in {"素心子", "缘生子"})))
    rows.extend([
        time_command(state, "next_field_training_time", ".野外历练 谨慎", "野外历练", group="通用"),
        daily_done_command(state, ".闯塔", "闯塔", date_key="last_tower_date", group="每日"),
        daily_done_command(state, ".宗门点卯", "宗门点卯", date_key="last_dianmao_date", group="每日"),
    ])
    if name == "问心子":
        rows.extend([
            time_command(state, "nine_heaven_wind_cd_time", ".引九天罡风", "引九天罡风", group="天阶"),
            time_command(state, "next_heart_time", ".问心台", "问心台", group="天阶"),
            manual_command(".天阶状态", "天阶状态", "查询天阶状态", "天阶"),
            time_command(state, "next_stairs_time", ".登天阶", "登天阶", group="天阶"),
        ])
    else:
        rows.extend([
            manual_command(".助阵", "助阵", "监听阵法邀请", "阵法"),
        ])
        rows.extend(xiaohao_star_attraction_commands(state))
        rows.extend([
            time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
            time_command(state, "pending_star_shift_target_time", ".改换星移 @TitanCreeper", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
            time_command(state, "next_formation_time", ".启阵", "启阵", group="阵法"),
        ])
    rows.extend(concubine_commands(state, include_divination=False, include_voyage=concubine_voyage_enabled("xiaohao", name)))
    return rows


def avatar_commands(account, name, state):
    if account == "main":
        return lingxiao_avatar_commands(name, state)
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
            "commands": avatar_commands(account, name, avatar_state or {}),
        })
    custom_commands = load_custom_commands()
    result = []
    for panel in panels:
        append_custom_commands(account, panel, custom_commands, root_state=state)
        result.append(apply_command_controls(account, panel))
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
    return "OUT" in header

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
    text = "\n".join(entry.get("lines") or [])
    commands = list(LOG_COMMAND_RE.findall(text or ""))
    for first, second in commands:
        command = normalize_log_command(first, second)
        if command:
            return command
    return ""

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

def filter_log_entries(entries, tag="", q=""):
    """按标签和关键词过滤日志条目"""
    tag = (tag or "").strip(); q = (q or "").strip().lower()
    filtered = []
    for entry in entries:
        if tag:
            if tag == OTHER_LOG_TAG:
                if tag not in entry["tags"]: continue
            elif tag not in entry["tags"] and tag not in entry.get("related_tags", set()): continue
        if q and q not in entry["text"].lower(): continue
        filtered.append(entry)
    return filtered

def get_log_tags(name):
    """获取日志可用的分类标签列表"""
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

def get_log_page(name, before=None, limit=80, tag="", q=""):
    """获取分页的日志内容"""
    limit = max(20, min(int(limit or 80), 200))
    entries, error = read_log_entries(name)
    if error:
        return {"content": error, "start": 0, "end": 0, "total": 0, "matched": 0, "has_more": False, "next_before": None}
    filtered = filter_log_entries(entries, tag=tag, q=q)
    total = len(entries); matched = len(filtered)
    end = matched if before is None else max(0, min(int(before), matched))
    start = max(0, end - limit)
    page_entries = filtered[start:end]
    return {"content": "\n\n".join(entry["text"] for entry in page_entries),
            "entries": [entry["text"] for entry in page_entries],
            "start": start, "end": end, "total": total, "matched": matched,
            "has_more": start > 0, "next_before": start if start > 0 else None, "tag": tag, "q": q}


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
            result = subprocess.run(["pgrep", "-af", script], capture_output=True, text=True)
            if result.returncode != 0: return False
            for line in result.stdout.splitlines():
                if script in line and "python" in line and "tmux " not in line: return True
            return False
    except: return False

def start_account(account):
    """启动账号脚本"""
    idx = WINDOW_MAP[account]; script = SCRIPT_MAP[account]
    if os.name == 'nt':
        subprocess.Popen(["pythonw", script], cwd=CONFIG_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
    else:
        cmd = f"bash -c 'cd ~/deploy && source ~/deploy/venv/bin/activate && python3 {script}'"
        subprocess.run(["tmux", "respawn-window", "-k", "-t", f"xiuxian:{idx}", cmd])

def stop_account(account):
    """停止账号脚本"""
    idx = WINDOW_MAP[account]; script = SCRIPT_MAP[account]
    if os.name == 'nt':
        ps_cmd = f'powershell -Command "Get-WmiObject Win32_Process -Filter \\"name=\'python.exe\' or name=\'pythonw.exe\'\\" | Where-Object {{$_.CommandLine -match \'{script}\'}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"'
        subprocess.run(ps_cmd, shell=True)
    else:
        subprocess.run(["tmux", "send-keys", "-t", f"xiuxian:{idx}", "C-c"])

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
    print(f"[clear] requested for {account}", flush=True)
    was_alive = get_process_status(account)
    if was_alive: stop_account(account); wait_for_status(account, False)
    script_path = os.path.join(CONFIG_DIR, "clear_history.py")
    try:
        result = subprocess.run([sys.executable, script_path, account], cwd=CONFIG_DIR, capture_output=True, text=True, timeout=900)
    finally:
        if was_alive: start_account(account); wait_for_status(account, True)
    output = (result.stdout or result.stderr or "").strip()
    print(f"[clear] {account}: rc={result.returncode}, {output}", flush=True)
    if result.returncode != 0: return {"success": False, "msg": output or "清屏失败"}
    return {"success": True, "msg": output or "清屏完成"}

def account_display_name(account):
    return {"main": "凌霄宫（主号）", "sub": "星宫（副号）", "xiaohao": "万灵宗（小号）"}.get(account, account)

def run_clear_job(job_id, account):
    """后台执行清屏任务"""
    with CLEAR_LOCK:
        CLEAR_JOBS[job_id]["status"] = "running"
        CLEAR_JOBS[job_id]["msg"] = f"{account_display_name(account)}清屏中，后台正在删除自己发出的消息。"
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
async def status(username: str = Depends(authenticate)):
    """获取所有账号的实时状态"""
    try:
        result = {}
        for key, info in {"main": "凌霄宫 (主号)", "sub": "星宫 (副号)", "xiaohao": "万灵宗 (小号)"}.items():
            state = get_state(key)
            result[key] = {
                "name": info,
                "state": state,
                "is_alive": get_process_status(key),
                "cultivation": get_cultivation_summary(key),
                "command_panels": build_command_panels(key, state),
            }
        return {"accounts": result, "server_time": time.strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e: return {"error": str(e)}

@app.get("/api/logs/{name}")
async def logs(name: str, before: Optional[int] = None, limit: int = 80, tag: str = "", q: str = "",
               username: str = Depends(authenticate)):
    """获取账号的分页日志"""
    return get_log_page(name, before=before, limit=limit, tag=tag, q=q)

@app.get("/api/log-tags/{name}")
async def log_tags(name: str, username: str = Depends(authenticate)):
    """获取账号日志的分类标签"""
    if name not in WINDOW_MAP: return {"tags": [], "error": "未知账号"}
    return get_log_tags(name)

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
            identity_controls.pop(control_key, None)
            if not identity_controls:
                account_controls.pop(identity, None)
            if not account_controls:
                data.pop(account, None)
        save_command_controls(data)
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
    schedule_enabled = bool(payload.get("schedule_enabled", interval_minutes > 0)) and interval_minutes > 0
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
    uvicorn.run(app, host="0.0.0.0", port=8000)
