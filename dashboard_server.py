"""
【面板服务器模块 —— Web 管理界面后端】

基于 FastAPI 的 Web 面板，提供修仙脚本的图形化管理界面。
功能：
  1. 账号状态查看 —— 实时查看四个账号的运行状态、修为进度
  2. 日志浏览 —— 按指令标签分类/搜索浏览日志
  3. 修为统计 —— 自动从日志中提取修为变化，按日统计
  4. 进程管理 —— 启动/停止/重启账号脚本
  5. 清屏功能 —— 后台清理账号发出的消息

通过 HTTP Basic 认证保护，运行在 0.0.0.0:8000。

【阅读导览】
- 顶部常量：账号、身份、日志标签、缓存时间和 state 文件路径。
- get_status / collect_*：从 state、日志、SQLite 事件库汇总展示数据。
- /api/* 路由：Dashboard 前端调用的接口，通常只做参数校验和结果包装。
- start/stop/restart 相关函数：通过进程命令管理远端脚本，改动前要确认部署环境。
- command_controls.json / dashboard_commands.json：面板指令开关和自定义排程的持久化文件。
"""
import os
import json
import time

# Workers persist naive Beijing timestamps. Match their clock before importing
# shared scheduling modules, even when a migrated VPS defaults to UTC/JST.
DASHBOARD_TIMEZONE = "Asia/Shanghai"
os.environ["TZ"] = DASHBOARD_TIMEZONE
if hasattr(time, "tzset"):
    time.tzset()

import subprocess
import secrets
import base64
import hashlib
import hmac
import html
import sys
import threading
import uuid
import re
import signal
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, status as http_status, Body, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from common_command_features import MULAN_SUPPORT_START_HOUR, MULAN_SUPPORT_START_MINUTE
from automation_settings import (
    DEFAULT_SUB_YINLUO_IDENTITY,
    MINIAPP_FISHING_BAITS,
    MINIAPP_FISHING_CHUMS,
    MINIAPP_FISHING_PONDS,
    MULAN_SUPPORT_MODES,
    SUB_YINLUO_IDENTITY,
    automation_dashboard_payload,
    canonical_automation_identity,
    current_sub_yinluo_identity,
    current_xiaohao_taiyi_identity,
    miniapp_beast_abyss_settings,
    miniapp_fishing_settings,
    miniapp_journey_identities_for_account,
    mulan_support_command,
    mulan_support_mode,
    save_automation_settings,
)
from log_utils import (
    command_control_key, command_control_identity_candidates,
    command_control_matches, command_control_entry_disabled,
)
from dashboard_command_catalog import apply_command_classifications, command_catalog_payload
from xuangu_quiz_features import confirm_quiz_answer, quiz_dashboard_payload
from command_modules import (
    ASK_DAO_COMMAND,
    SMALL_WORLD_MIRACLE_ACTIONS,
    SMALL_WORLD_MIRACLE_CONTROL_KEY,
    normalize_small_world_miracle_mode,
    small_world_miracle_command,
    DEFAULT_WAAIGING_FIELD_TRAINING_COMMAND,
    NODE_SEARCH_COMMAND,
    NURTURE_SPIRIT_COMMAND,
    RIFT_SEARCH_COMMAND,
    TREASURE_REFINE_COMMAND,
    YUANYING_OUT_COMMAND,
    field_training_plan_from_features,
    treasure_touch_plan,
    yuanying_out_plan,
)
from duel_features import (
    DUEL_ROTATION_QUEUE_KEY,
    configure_duel_multi_plan,
    duel_dashboard_payload,
    duel_multi_schedule_status,
    duel_multi_target_switch_enabled,
    set_duel_control,
    set_duel_intervals,
    set_duel_multi_control,
    set_duel_multi_schedule,
    set_duel_participant_control,
    set_duel_target_switch,
    set_titan_beast_mode,
    titan_target_status,
)
from surprise_raid_features import (
    set_surprise_raid_config,
    surprise_raid_dashboard_payload,
)
from red_packet_features import red_packet_dashboard_payload, save_red_packet_settings
from miniapp_beast import write_refresh_request
from world_boss_turnstile import (
    TurnstileRequestError,
    list_world_boss_turnstile_requests,
    record_world_boss_turnstile_browser_event,
    submit_world_boss_turnstile_token,
)
from miniapp_dwelling import miniapp_command_allowed, normalize_miniapp_command
from miniapp_fishing import (
    fishing_display_error_text,
    miniapp_fishing_global_snapshot,
    request_miniapp_fishing_force_retry,
)
from miniapp_inventory import (
    inventory_account_identities,
    read_inventory_cache,
    search_inventory_caches,
    write_inventory_request,
)
from state_io import load_json_state, save_json_state, update_json_state
from sect_rules import command_sect
from hehuan_features import (
    DUAL_CULTIVATION_COMMAND, dual_cultivation_default_target, normalize_dual_cultivation_target,
)
from reward_parsing import (
    compact_reward_summary,
    daily_reward_items_for_command,
    is_mulan_settlement_text,
    normalize_reward_items,
    reward_command_root,
    trust_empty_reward_reparse,
)
from fishing_features import (
    FISHING_AUTO_ACCOUNT_IDENTITIES,
    FISHING_AUTOMATION_ENABLED,
    FISHING_AUTO_CONTROL_COMMAND,
    FISHING_AUTO_CONTROL_ACCOUNTS,
    FISHING_AUTO_CONTROL_COMMANDS,
    FISHING_BAIT,
    FISHING_CONTROL_BAITS,
    FISHING_CONTROL_COMMANDS,
    FISHING_DAILY_LIMIT,
    fishing_auto_bait_from_entry,
    fishing_auto_bait_for_state,
    fishing_auto_dashboard_state,
    fishing_daily_done_for_today,
    fishing_dashboard_bait,
    fishing_dashboard_command,
    fishing_dashboard_state,
)
from concubine_features import CONCUBINE_DISMISS_COMMAND, CONCUBINE_SEARCH_COMMAND, TARGET_CONCUBINE_NAME
from soul_curse_features import (
    SOUL_CURSE_AVATAR_PUBLISHERS,
    SOUL_CURSE_ACCEPT_COMMAND,
    SOUL_CURSE_CO_STUDY_COMMAND,
    SOUL_CURSE_IDENTIFY_COMMAND,
    SOUL_CURSE_INFER_COMMAND,
    SOUL_CURSE_PROTECT_COMMAND,
    SOUL_CURSE_PUBLISH_COMMAND,
    SOUL_CURSE_PUBLISHERS,
    SOUL_CURSE_SETTINGS_FILE,
    SOUL_CURSE_STRIP_COMMAND,
    SOUL_CURSE_SUPPRESS_COMMAND,
    SOUL_CURSE_VISIT_COMMAND,
    SOUL_CURSE_WANYING_GREETING_COMMAND,
)
from yinluo_features import YINLUO_APPEASE_COMMAND, YINLUO_CONVERT_COMMAND, YINLUO_IDENTITY, YINLUO_MASTER_COMMAND, YINLUO_SOUL


def _load_dashboard_dotenv():
    """Load deployment-local Dashboard settings without requiring a shell export."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in os.environ:
            continue
        if not key.startswith("DASHBOARD_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


_load_dashboard_dotenv()
app = FastAPI()
security = HTTPBasic(auto_error=False)

# =====================================================================
# 安全配置（HTTP Basic 认证）
# =====================================================================
USER_NAMES = tuple(
    item.strip()
    for item in os.environ.get("DASHBOARD_USERS", "admin").split(",")
    if item.strip()
)
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
DASHBOARD_ACCESS_TOKEN = os.environ.get("DASHBOARD_ACCESS_TOKEN", "")
DASHBOARD_PASSWORD = DASHBOARD_ACCESS_TOKEN or DASHBOARD_PASSWORD
DASHBOARD_SESSION_SECRET = os.environ.get("DASHBOARD_SESSION_SECRET", "")
DASHBOARD_SESSION_DAYS = max(1, min(3650, int(os.environ.get("DASHBOARD_SESSION_DAYS", "180") or 180)))
DASHBOARD_COOKIE_SECURE = os.environ.get("DASHBOARD_COOKIE_SECURE", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
DASHBOARD_SESSION_COOKIE = "fanren_dashboard_session"
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_SECONDS = 300
LOGIN_FAILURE_MAX_CLIENTS = 4096
LOGIN_FAILURES = OrderedDict()
LOGIN_FAILURE_LOCK = threading.Lock()


def _session_signing_key():
    return (DASHBOARD_SESSION_SECRET or DASHBOARD_PASSWORD).encode("utf-8")


def create_dashboard_session(username, now=None):
    """Create a signed, expiring session token without storing server-side secrets."""
    if not DASHBOARD_PASSWORD or not _session_signing_key():
        return ""
    issued_at = int(time.time() if now is None else now)
    payload = json.dumps(
        {
            "u": str(username or ""),
            "iat": issued_at,
            "exp": issued_at + DASHBOARD_SESSION_DAYS * 86400,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(_session_signing_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def dashboard_session_user(token, now=None):
    """Return the authenticated user from a valid session token."""
    if not DASHBOARD_PASSWORD or not token or len(str(token)) > 4096:
        return ""
    try:
        encoded, signature = str(token or "").rsplit(".", 1)
        expected = hmac.new(
            _session_signing_key(), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not secrets.compare_digest(signature, expected):
            return ""
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if not isinstance(payload, dict) or not isinstance(payload.get("u"), str):
            return ""
        username = payload["u"]
        current = int(time.time() if now is None else now)
        issued, expires = payload.get("iat"), payload.get("exp")
        if type(issued) is not int or type(expires) is not int:
            return ""
        if not (0 <= issued <= current < expires) or expires - issued > DASHBOARD_SESSION_DAYS * 86400:
            return ""
        if not any(_constant_time_text_equal(username, name) for name in USER_NAMES):
            return ""
        return username
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return ""


def _constant_time_text_equal(first, second):
    try:
        return secrets.compare_digest(str(first).encode("utf-8"), str(second).encode("utf-8"))
    except UnicodeError:
        return False


def dashboard_credentials_valid(username, password):
    if not DASHBOARD_PASSWORD:
        return False
    supplied_username = str(username or "").strip()
    correct_username = not supplied_username or any(
        _constant_time_text_equal(supplied_username, name) for name in USER_NAMES
    )
    correct_password = _constant_time_text_equal(password or "", DASHBOARD_PASSWORD)
    return correct_username and correct_password


def _check_dashboard_credentials(request, username, password):
    # Uvicorn resolves the trusted local proxy. Do not trust raw forwarded
    # headers here: direct callers could rotate them to bypass the limit.
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with LOGIN_FAILURE_LOCK:
        expired = [key for key, (started, _) in LOGIN_FAILURES.items()
                   if now - started >= LOGIN_FAILURE_WINDOW_SECONDS]
        for key in expired:
            LOGIN_FAILURES.pop(key, None)
        started, failures = LOGIN_FAILURES.get(client, (now, 0))
        if failures >= LOGIN_FAILURE_LIMIT:
            retry_after = max(1, int(LOGIN_FAILURE_WINDOW_SECONDS - (now - started)) + 1)
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试",
                                headers={"Retry-After": str(retry_after)})
        if dashboard_credentials_valid(username, password):
            LOGIN_FAILURES.pop(client, None)
            return True
        LOGIN_FAILURES[client] = (started, failures + 1)
        LOGIN_FAILURES.move_to_end(client)
        while len(LOGIN_FAILURES) > LOGIN_FAILURE_MAX_CLIENTS:
            LOGIN_FAILURES.popitem(last=False)
    return False


def authenticate(
    request: Request,
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
):
    """Accept a persistent signed browser session or legacy HTTP Basic credentials."""
    if not DASHBOARD_PASSWORD:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dashboard authentication is not configured",
        )
    session_user = dashboard_session_user(request.cookies.get(DASHBOARD_SESSION_COOKIE))
    if session_user:
        return session_user
    if credentials and _check_dashboard_credentials(request, credentials.username, credentials.password):
        return credentials.username
    raise HTTPException(
        status_code=http_status.HTTP_401_UNAUTHORIZED,
        detail="暗号不对，道友请留步",
    )

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
RED_PACKET_CONTROL_LOCK = threading.Lock()  # 抢红包设置锁
AUTOMATION_SETTINGS_LOCK = threading.Lock()  # Boss 身份与慕兰参数设置锁
MINIAPP_INVENTORY_REQUEST_LOCK = threading.Lock()  # 储物袋主动刷新请求锁
MINIAPP_INVENTORY_NON_TRADABLE_LOCK = threading.Lock()  # 储物袋不可交易物品持久化锁
STATUS_CACHE = {}                        # Dashboard 总状态缓存，避免前端轮询时反复读大日志
STATUS_LOCK = threading.Lock()           # Dashboard 总状态锁
LOG_PAGE_CACHE = {}                      # 日志分页接口短缓存
LOG_PAGE_LOCK = threading.Lock()         # 日志分页接口锁
CULTIVATION_CACHE = {}                   # 修为统计缓存
CULTIVATION_LOCK = threading.Lock()      # 修为统计锁
COMMAND_RECORD_CACHE = {}                # 指令发送记录缓存
COMMAND_RECORD_LOCK = threading.Lock()   # 指令发送记录锁
COMMAND_RECORD_ENDPOINT_CACHE = {}       # 指令发送记录接口短缓存
COMMAND_RECORD_ENDPOINT_LOCK = threading.Lock()
DAILY_REWARD_ENDPOINT_CACHE = {}         # 周期收益日志接口短缓存
DAILY_REWARD_ENDPOINT_LOCK = threading.Lock()
MESSAGE_HEALTH_CACHE = {}                # 消息采集健康缓存
MESSAGE_HEALTH_LOCK = threading.Lock()   # 消息采集健康锁
RESOURCE_STATS_CACHE = {}                # 资源/库存统计缓存，构建成本较高所以单独限时缓存
RESOURCE_STATS_LOCK = threading.Lock()   # 资源/库存统计锁
RESOURCE_STATS_BUILD_LOCK = threading.Lock()
CULTIVATION_CACHE_FILE = "cultivation_stats_cache.json"
COMMAND_CONTROL_FILE = "command_controls.json"
CUSTOM_COMMAND_FILE = "dashboard_commands.json"
BEAST_BORDER_PATROL_CONTROL_KEY = ".灵兽巡边 *"
BEAST_BORDER_PATROL_MODES = ("斥候", "护粮", "袭营")
BEAST_BORDER_PATROL_DEFAULT_MODE = "袭营"
MESSAGE_EVENTS_DB_FILE = "message_events.sqlite3"
DEPLOY_VERSION_FILE = "deploy_version.json"
MINIAPP_INVENTORY_NON_TRADABLE_FILE = "miniapp_inventory_non_tradable.json"
MESSAGE_HEALTH_MAX_SCAN_IDS = 12000
STATUS_CACHE_SECONDS = 10
LOG_PAGE_CACHE_SECONDS = 5
COMMAND_RECORD_ENDPOINT_CACHE_SECONDS = 180
DAILY_REWARD_ENDPOINT_CACHE_SECONDS = 30
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
ACCOUNT_DISPLAY_NAMES = {
    "main": "天星宗 (主号)",
    "sub": "元婴宗 (副号)",
    "xiaohao": "万灵宗 (小号)",
    "waaiging": "天星宗 (@Waaiging)",
}
ACCOUNT_SHORT_NAMES = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}
ALL_AVATARS = ["问心子", "素心子", "缘生子", "无咎子", "素缘子", "厚土", "玄续玄", SUB_YINLUO_IDENTITY, "寻真子"]
STAR_CONCUBINE_VOYAGE_IDENTITIES = {
    "main": {"素缘子"},
    "sub": {"厚土", SUB_YINLUO_IDENTITY, "寻真子"},
    "xiaohao": {"素心子", "缘生子"},
}
SUB_STAR_PALACE_AVATARS = {"厚土"}
CONCUBINE_VOYAGE_AUTO_START_ENABLED = True

ACCOUNT_PROFILE_USERNAMES = {
    "main": {
        "主魂": {"weeguu"},
        "无咎子": {"wuxinglinggen"},
        "玄续子": {"kulipabp"},
        "素缘子": {"oldeinstein"},
    },
    "sub": {
        "主魂": {"gamling33"},
        "厚土": {"crayonxxin"},
        SUB_YINLUO_IDENTITY: {"lvdoumiao"},
        "寻真子": {"ding303"},
    },
    "xiaohao": {
        "主魂": {"titancreeper"},
        "问心子": {"lianqi10000"},
        "素心子": {"hajiimiii"},
        "缘生子": {"adai925"},
    },
    "waaiging": {
        "主魂": {"waaiging"},
    },
}
MULAN_SUPPORT_COMMANDS = tuple(f".支援慕兰 {mode}" for mode in MULAN_SUPPORT_MODES)

def stable_sub_avatar_identity(state, slot_name):
    """Resolve the live Dao name for a stable sub avatar slot (寻真子 -> 寒续尘)."""
    from automation_settings import STABLE_SUB_AVATAR_SLOTS
    for player_id, slot in STABLE_SUB_AVATAR_SLOTS.items():
        if slot != slot_name:
            continue
        if not isinstance(state, dict):
            return ""
        player_names = state.get("avatar_dao_names_by_player_id")
        if isinstance(player_names, dict):
            current = str(player_names.get(str(player_id)) or "").strip()
            if current and current != "一缕残魂":
                return current
    return ""


def account_profile_usernames(account, state=None):
    """Return dashboard-safe username mapping for every identity in an account."""
    mapping = dict(ACCOUNT_PROFILE_USERNAMES.get(account) or {})
    if account == "main":
        # 主号化身的道号可能重生改名（缘生子->玄续子），从 state 的 aliases 解新名
        if isinstance(state, dict):
            aliases = state.get("avatar_dao_name_aliases") or {}
            for old_name in list(mapping.keys()):
                current = old_name
                seen = set()
                while current in aliases and current not in seen:
                    seen.add(current)
                    mapped = str(aliases.get(current) or "").strip()
                    if not mapped or mapped == current or mapped == "一缕残魂":
                        break
                    current = mapped
                if current != old_name and current not in mapping:
                    mapping[current] = mapping[old_name]
    elif account == "sub":
        current_identity = current_sub_yinluo_identity(state)
        if current_identity and current_identity != SUB_YINLUO_IDENTITY:
            usernames = mapping.pop(SUB_YINLUO_IDENTITY, {"lvdoumiao"})
            mapping[current_identity] = usernames
        # Stable avatar slot follows its live Dao name after rebirth
        # (e.g. 寻真子 -> 寒续尘), keeping username lookups matched.
        stable_current = stable_sub_avatar_identity(state, "寻真子")
        if stable_current and stable_current != "寻真子":
            usernames = mapping.pop("寻真子", {"ding303"})
            mapping[stable_current] = usernames
    elif account == "xiaohao":
        current_identity = current_xiaohao_taiyi_identity(state)
        usernames = mapping.pop("缘生子", {"adai925"})
        mapping[current_identity] = usernames
    return {
        identity: sorted(
            f"@{normalize_profile_username(name)}"
            for name in names
            if normalize_profile_username(name)
        )
        for identity, names in mapping.items()
    }


def miniapp_inventory_non_tradable_path(base_dir=None):
    return os.path.join(base_dir or CONFIG_DIR, MINIAPP_INVENTORY_NON_TRADABLE_FILE)


def load_miniapp_inventory_non_tradable_items(base_dir=None):
    """Read the persisted set of item names excluded from trading."""
    path = miniapp_inventory_non_tradable_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    raw_items = value.get("items") if isinstance(value, dict) else value
    if not isinstance(raw_items, list):
        return []
    return sorted(
        {str(item).strip() for item in raw_items if str(item or "").strip()},
        key=str.casefold,
    )


def save_miniapp_inventory_non_tradable_items(items, base_dir=None):
    cleaned = sorted(
        {str(item).strip() for item in (items or []) if str(item or "").strip()},
        key=str.casefold,
    )
    save_json_atomic(
        miniapp_inventory_non_tradable_path(base_dir),
        {
            "version": 1,
            "items": cleaned,
            "updated_at": time.strftime(TIME_FORMAT),
        },
    )
    return cleaned


def aggregate_miniapp_inventory_items(caches, account_identities=None):
    """Merge all cached identities into one name-deduplicated item list."""
    account_identities = account_identities or inventory_account_identities()
    merged = {}
    for account, identities in account_identities.items():
        cache = caches.get(account) if isinstance(caches, dict) else {}
        snapshots = cache.get("snapshots") if isinstance(cache, dict) else {}
        if not isinstance(snapshots, dict):
            snapshots = {}
        for identity in identities:
            snapshot = snapshots.get(identity)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
                continue
            for row in snapshot["items"]:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or row.get("item_id") or "").strip()
                if not name:
                    continue
                key = name.casefold()
                current = merged.get(key)
                if current is None:
                    current = {
                        "name": name,
                        "item_id": str(row.get("item_id") or "").strip(),
                        "type": str(row.get("type") or "物品").strip() or "物品",
                        "quantity": 0,
                        "detail": str(row.get("detail") or "").strip(),
                        "_sources": set(),
                    }
                    merged[key] = current
                try:
                    quantity = float(str(row.get("quantity") or 0).replace(",", ""))
                except (TypeError, ValueError):
                    quantity = 0
                current["quantity"] += quantity
                current["_sources"].add((account, identity))
                if not current["detail"] and row.get("detail"):
                    current["detail"] = str(row.get("detail")).strip()
    rows = []
    for row in merged.values():
        quantity = row.pop("quantity")
        row["quantity"] = int(quantity) if quantity.is_integer() else quantity
        row["source_count"] = len(row.pop("_sources"))
        rows.append(row)
    return sorted(rows, key=lambda row: str(row.get("name") or "").casefold())


def miniapp_inventory_dashboard_payload(query=""):
    """Return cached inventory snapshots, aggregate items, and search results."""
    account_identities = inventory_account_identities()
    caches = {
        account: read_inventory_cache(account, CONFIG_DIR)
        for account in account_identities
    }
    accounts = []
    snapshot_count = 0
    item_count = 0
    latest_update = ""
    for account, identities in account_identities.items():
        cache = caches[account]
        snapshots = cache.get("snapshots") if isinstance(cache.get("snapshots"), dict) else {}
        clean_snapshots = {}
        for identity in identities:
            snapshot = snapshots.get(identity)
            if not isinstance(snapshot, dict):
                continue
            clean_snapshots[identity] = snapshot
            snapshot_count += 1
            item_count += len(snapshot.get("items") or []) if isinstance(snapshot.get("items"), list) else 0
            latest_update = max(latest_update, str(snapshot.get("updated_at") or ""))
        accounts.append({
            "account": account,
            "name": ACCOUNT_DISPLAY_NAMES.get(account, account),
            "short_name": ACCOUNT_SHORT_NAMES.get(account, account),
            "identities": list(identities),
            "snapshots": clean_snapshots,
            "updated_at": str(cache.get("updated_at") or ""),
            "last_request": cache.get("last_request") if isinstance(cache.get("last_request"), dict) else {},
            "request_progress": cache.get("request_progress") if isinstance(cache.get("request_progress"), dict) else {},
        })
    inventory_totals = aggregate_miniapp_inventory_items(caches, account_identities)
    non_tradable_items = load_miniapp_inventory_non_tradable_items(CONFIG_DIR)
    search_results = search_inventory_caches(caches, query, account_identities)
    match_quantity = 0.0
    for row in search_results:
        try:
            match_quantity += float(row.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
    if match_quantity.is_integer():
        match_quantity = int(match_quantity)
    return {
        "ok": True,
        "accounts": accounts,
        "inventory_totals": inventory_totals,
        "non_tradable_items": non_tradable_items,
        "search_query": str(query or "").strip(),
        "search_results": search_results,
        "summary": {
            "snapshot_count": snapshot_count,
            "item_count": item_count,
            "total_unique_count": len(inventory_totals),
            "non_tradable_count": len(non_tradable_items),
            "match_count": len(search_results),
            "match_quantity": match_quantity,
            "updated_at": latest_update,
        },
        "server_time": time.strftime(TIME_FORMAT),
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
} | {(".支援慕兰", mode) for mode in MULAN_SUPPORT_MODES}
OTHER_LOG_TAG = "其他"                    # 未分类日志标签
FISHING_LOG_TAG = "钓鱼"
RELATED_LOG_WINDOW_SECONDS = 180          # 关联日志窗口（秒）
CULTIVATION_DEDUPE_SECONDS = 15           # 修为变更去重窗口

# 游戏机器人标识（用于区分机器人回复和普通消息）
BOT_REPLY_MARKERS = {
    "fanrenxiuxian_bot", "@fanrenxiuxian_bot",
    "hantianzzz_bot", "@hantianzzz_bot",
    "hantianzz_bot", "@hantianzz_bot",
    "hantianz_bot", "@hantianz_bot",
    "hantianzunhl", "@hantianzunhl",
    "hantianzun05_bot", "@hantianzun05_bot",
    "hantianzun06_bot", "@hantianzun06_bot",
    "hantianzun07_bot", "@hantianzun07_bot",
    "hantianzun08_bot", "@hantianzun08_bot",
    "韩天尊",
} | {
    marker
    for index in range(10, 51)
    for marker in (f"hantianzun{index}_bot", f"@hantianzun{index}_bot")
}

# 每个账号的日志标签定义（对应不同的游戏指令）
ACCOUNT_LOG_TAGS = {
    "main": [
        FISHING_LOG_TAG,
        ".宗门点卯", *MULAN_SUPPORT_COMMANDS, ".宗门传功",
        ".推命", ".改命", ".观命", ".定命",
        ".登天阶", ".天阶状态", ".引九天罡风", ".问心台",
        ".寻觅灵兽", ".我的灵兽", ".放生", ".灵兽出战",
        ".探渊", ".一键放养", ".灵兽互动", ".灵兽巡边", ".巡边状态", ".巡边归来",
        ".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关",
        ".召回侍妾", ".安置侍妾", YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        MAIN_TREASURE_TOUCH_COMMAND,
        DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, MAIN_FIELD_TRAINING_COMMAND, WUJIUZI_FIELD_TRAINING_COMMAND, ".宗门战况", ".参战", ".我的侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        CONCUBINE_SEARCH_COMMAND, CONCUBINE_DISMISS_COMMAND,
        SOUL_CURSE_VISIT_COMMAND, SOUL_CURSE_WANYING_GREETING_COMMAND,
        SOUL_CURSE_CO_STUDY_COMMAND, SOUL_CURSE_INFER_COMMAND,
        SOUL_CURSE_PROTECT_COMMAND, SOUL_CURSE_PUBLISH_COMMAND,
        SOUL_CURSE_ACCEPT_COMMAND, SOUL_CURSE_IDENTIFY_COMMAND,
        SOUL_CURSE_SUPPRESS_COMMAND, SOUL_CURSE_STRIP_COMMAND,
        OTHER_LOG_TAG,
    ],
    "sub": [
        FISHING_LOG_TAG,
        ".宗门点卯", *MULAN_SUPPORT_COMMANDS, ".宗门传功", ASK_DAO_COMMAND,
        ".启阵", ".助阵", ".强行出关",
        ".查看闭关", ".闭关修炼", ".深度闭关",
        ".召回侍妾", ".安置侍妾", ".每日问安",
        ".观星台", ".安抚星辰", ".收集精华", ".牵引星辰", ".观星", ".改换星移",
        SUB_MAIN_YUANYING_COMMAND, YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        SUB_TREASURE_TOUCH_COMMAND,
        DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, MAIN_FIELD_TRAINING_COMMAND, ".野外历练 均衡", ".宗门战况", ".参战", ".我的侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        SOUL_CURSE_ACCEPT_COMMAND, SOUL_CURSE_IDENTIFY_COMMAND,
        SOUL_CURSE_SUPPRESS_COMMAND, SOUL_CURSE_STRIP_COMMAND,
        OTHER_LOG_TAG,
    ],
    "xiaohao": [
        FISHING_LOG_TAG,
        ".宗门点卯", *MULAN_SUPPORT_COMMANDS, ".宗门传功",
        ".寻觅灵兽", ".我的灵兽", ".放生", ".灵兽出战",
        ".灵兽偷菜", ".灵兽探渊", ".一键放养", ".灵兽互动", ".灵兽巡游", ".灵兽巡边", ".巡边状态", ".巡边归来",
        ".查看闭关", ".闭关修炼", ".深度闭关", ".召回侍妾", ".安置侍妾",
        DEFAULT_AVATAR_FIELD_TRAINING_COMMAND, MAIN_FIELD_TRAINING_COMMAND, ".宗门战况", ".参战", SUB_TREASURE_TOUCH_COMMAND,
        YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        ".观星台", ".安抚星辰", ".收集精华", ".牵引星辰", ".引道",
        ".我的侍妾", ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        SOUL_CURSE_VISIT_COMMAND, SOUL_CURSE_INFER_COMMAND,
        SOUL_CURSE_PROTECT_COMMAND, SOUL_CURSE_PUBLISH_COMMAND,
        OTHER_LOG_TAG,
    ],
    "waaiging": [
        FISHING_LOG_TAG,
        ".拜入宗门 天星宗",
        ".宗门点卯", *MULAN_SUPPORT_COMMANDS, ".宗门传功",
        ".查看闭关", ".闭关修炼", ".深度闭关", ".强行出关",
        YUANYING_OUT_COMMAND, ".元婴归窍", RIFT_SEARCH_COMMAND,
        DEFAULT_WAAIGING_FIELD_TRAINING_COMMAND,
        ".宗门战况", ".参战", ".我的侍妾", ".安置侍妾", ".召回侍妾",
        ".入梦寻图", ".共历心劫", ".稳", ".天机代卜", ".侍妾远航", ".远航归来",
        OTHER_LOG_TAG,
    ],
}

# 脚本文件名 -> tmux 窗口编号 映射
SCRIPT_MAP = {
    "main": "intelligent_cultivator.py",
    "sub": "sub_cultivator.py",
    "xiaohao": "cultivator_xiaohao.py",
    "waaiging": "cultivator_waaiging.py",
}
# Restricted accounts may be represented by either their full automation
# script or the red-packet/Mini App standby worker, depending on group access.
ACCOUNT_PROCESS_SIGNATURES = {
    "main": (("intelligent_cultivator.py", ""),),
    "sub": (("sub_cultivator.py", ""),),
    "xiaohao": (
        ("cultivator_xiaohao.py", ""),
        ("red_packet_account.py", "xiaohao"),
    ),
    "waaiging": (
        ("cultivator_waaiging.py", ""),
        ("red_packet_account.py", "waaiging"),
    ),
}
WINDOW_MAP = {"main": 0, "sub": 1, "xiaohao": 2, "waaiging": 3}


# =====================================================================
# 状态管理
# =====================================================================

def get_state(name):
    """读取账号的状态 JSON 文件，预处理显示字段"""
    path = os.path.join(CONFIG_DIR, f'state_{name}.json')
    if os.path.exists(path) or os.path.exists(f"{path}.bak"):
        try:
            data = load_json_state(path, expected_type=dict, default={}) or {}
            # Waaiging is a single-soul account. Ignore stale avatar data
            # copied from another account until its next clean save.
            if name == "waaiging":
                data.pop("avatars", None)
            if data.get("deep_meditation_end_time"):
                from datetime import datetime
                end_time = data["deep_meditation_end_time"]
                if datetime.strptime(end_time, TIME_FORMAT) > datetime.now():
                    data["in_deep_meditation"] = True
            apply_common_display_times(data)
            if name == "sub":
                apply_sub_display_times(data)
            return data
        except Exception:
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


def command_control_entry(controls, account, identity, control_key, root_state=None):
    matches = command_control_matches(controls, account, identity, control_key, root_state)
    return matches[0][1] if matches else None


def command_control_disabled(controls, account, identity, control_key, default_disabled=False, root_state=None):
    matches = command_control_matches(controls, account, identity, control_key, root_state)
    return any(command_control_entry_disabled(entry) for _, entry in matches) if matches else bool(default_disabled)


def normalize_beast_border_patrol_mode(mode):
    mode = str(mode or "").strip()
    return mode if mode in BEAST_BORDER_PATROL_MODES else BEAST_BORDER_PATROL_DEFAULT_MODE


def fishing_auto_control_entry(account):
    controls = load_command_controls()
    account_controls = controls.get(account, {}) if isinstance(controls, dict) else {}
    if not isinstance(account_controls, dict):
        return {}
    identity_controls = account_controls.get("主魂", {})
    if not isinstance(identity_controls, dict):
        return {}
    entry = identity_controls.get(FISHING_AUTO_CONTROL_COMMAND)
    return entry if isinstance(entry, dict) else {}


def apply_command_controls(account, panel, root_state=None):
    controls = load_command_controls()
    identity = panel.get("identity") or "主魂"
    commands = panel.get("commands") or []
    for row in commands:
        control_key = command_control_key(row.get("command", ""))
        row["control_key"] = control_key
        entry = command_control_entry(controls, account, identity, control_key, root_state)
        row["control_disabled"] = command_control_disabled(
            controls,
            account,
            identity,
            control_key,
            default_disabled=bool(row.get("default_paused")),
            root_state=root_state,
        )
        if row["control_disabled"] and row.get("actionable", True):
            row["status"] = "已暂停"
            row["tone"] = "paused"
            detail = row.get("detail", "")
            row["detail"] = f"dashboard 临时暂停{f' · {detail}' if detail else ''}"
        if control_key == BEAST_BORDER_PATROL_CONTROL_KEY and row.get("actionable", True):
            mode = normalize_beast_border_patrol_mode(
                entry.get("patrol_mode") if isinstance(entry, dict) else ""
            )
            row["patrol_mode_options"] = list(BEAST_BORDER_PATROL_MODES)
            row["patrol_mode_value"] = mode
            row["command"] = f".灵兽巡边 <灵兽> {mode}"
            detail = row.get("detail", "")
            patrol_detail = f"灵兽按体力自动选择 · 路线：{mode}"
            row["detail"] = f"{patrol_detail}{f' · {detail}' if detail else ''}"
        if (control_key == SMALL_WORLD_MIRACLE_CONTROL_KEY and not row.get("custom")
                and account in {"main", "waaiging"} and identity == "主魂"):
            mode = normalize_small_world_miracle_mode(
                entry.get("miracle_mode") if isinstance(entry, dict) else ""
            )
            row["miracle_mode_options"] = list(SMALL_WORLD_MIRACLE_ACTIONS)
            row["miracle_mode_value"] = mode
            row["command"] = small_world_miracle_command(mode)
            row["label"] = f"神迹 {mode}"
    return panel


def apply_command_execution_channels(panel, root_state=None):
    """Label each row with the channel that will currently execute it."""
    state = root_state if isinstance(root_state, dict) else {}
    route_active = bool(state.get("miniapp_route_active"))
    restricted_active = bool(state.get("restricted_miniapp_active"))
    miniapp_active = route_active or restricted_active
    for row in panel.get("commands") or []:
        command = normalize_miniapp_command(row.get("command", ""))
        miniapp_only = command.startswith("miniapp:")
        miniapp_scheduler = command == ".寻觅灵兽"
        miniapp_capable = miniapp_command_allowed(command) or miniapp_scheduler
        if miniapp_only or miniapp_capable:
            row["execution_channel"] = "miniapp"
            if miniapp_only:
                row["execution_channel_detail"] = "仅通过 Mini App 执行"
            elif miniapp_scheduler:
                row["execution_channel_detail"] = (
                    "由 Mini App 定时任务执行，不发送群指令，也不回退群内"
                )
            elif restricted_active:
                row["execution_channel_detail"] = "当前受限模式，仅通过 Mini App 执行"
            elif route_active:
                row["execution_channel_detail"] = "Mini App 固定执行"
            else:
                row["execution_channel_detail"] = "Mini App 固定执行；路由当前不可用时阻止发送，不回退群内"
        else:
            row["execution_channel"] = "group"
            row["execution_channel_detail"] = "Mini App 不支持，继续在群内发送"
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
    actionable=True,
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
    if not actionable:
        row["actionable"] = False
    return row


def time_command(
    state,
    key,
    command,
    label=None,
    waiting="冷却中",
    ready="就绪",
    missing="就绪",
    detail="",
    group="",
    default_paused=False,
):
    """Display a timestamp-backed command as cooldown/ready."""
    raw = state.get(key, "")
    if raw in ("就绪",):
        return command_row(command, label, "就绪", "ready", remaining="0秒", detail=detail, group=group, schedule_type="cooldown", next_seconds=0, default_paused=default_paused)
    if raw == "本轮已无下一次":
        return command_row(command, label, "本轮结束", "done", at=raw, detail=detail, group=group, schedule_type="cooldown", default_paused=default_paused)
    if raw == "---":
        return command_row(command, label, "未开启", "unknown", at=raw, detail=detail, group=group, schedule_type="cooldown", default_paused=default_paused)
    target = parse_state_time(raw)
    if not target:
        next_seconds = 0 if missing == "就绪" else None
        return command_row(
            command, label, missing, "ready" if missing == "就绪" else "unknown",
            remaining="0秒" if missing == "就绪" else "",
            at=str(raw or ""), detail=detail, group=group,
            schedule_type="cooldown", next_seconds=next_seconds,
            default_paused=default_paused,
        )
    now = datetime.now()
    if target > now:
        next_seconds = max(0, int((target - now).total_seconds()))
        return command_row(command, label, waiting, "cooldown", format_remaining(next_seconds), str(raw), detail, group, schedule_type="cooldown", next_seconds=next_seconds, default_paused=default_paused)
    return command_row(command, label, ready, "ready", "0秒", str(raw), detail, group, schedule_type="cooldown", next_seconds=0, default_paused=default_paused)


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


def mulan_support_daily_command(state):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    mode = mulan_support_mode()
    command = mulan_support_command()
    label = f"支援慕兰 {mode}"
    target = now.replace(
        hour=MULAN_SUPPORT_START_HOUR,
        minute=MULAN_SUPPORT_START_MINUTE,
        second=0,
        microsecond=0,
    )
    detail = f"每日 {MULAN_SUPPORT_START_HOUR:02d}:{MULAN_SUPPORT_START_MINUTE:02d} 独立执行"
    if str(state.get("last_mulan_support_date") or "") != today and now < target:
        wait_seconds = max(1, int((target - now).total_seconds()))
        return command_row(
            command,
            label,
            "等待 10:00",
            "waiting",
            remaining=format_remaining(wait_seconds),
            at=target.strftime(TIME_FORMAT),
            detail=detail,
            group="每日",
            schedule_type="daily",
            next_seconds=wait_seconds,
        )
    return daily_done_command(
        state,
        command,
        label,
        date_key="last_mulan_support_date",
        detail=detail,
        group="每日",
    )


def watch_command(command, label=None, detail="同步/记录回复", group=""):
    return command_row(command, label, "监听中", "watch", detail=detail, group=group)


def manual_command(command, label=None, detail="按需发送", group=""):
    return command_row(command, label, "按需", "manual", detail=detail, group=group, actionable=False)


def miniapp_beast_sync_command(state):
    last_sync = str(state.get("beast_miniapp_last_sync_time") or "").strip()
    last_attempt = str(state.get("beast_miniapp_last_attempt_time") or "").strip()
    error = clean_custom_text(state.get("beast_miniapp_last_error") or "", 80)
    count = len(state.get("beasts_cache") or [])
    if error:
        status = "同步异常"
        tone = "paused"
        detail = f"{error} · 保留缓存 {count} 只"
    elif last_sync:
        status = "已同步"
        tone = "active"
        detail = f"Mini App 实时缓存 {count} 只"
    elif last_attempt:
        status = "等待同步"
        tone = "cooldown"
        detail = "已提交 Mini App 同步，等待接口返回"
    else:
        status = "待同步"
        tone = "unknown"
        detail = "从固定 Mini App 入口读取灵兽状态"
    row = command_row(
        "miniapp:spirit-beast",
        "万兽谷同步",
        status,
        tone,
        at=last_sync or last_attempt,
        detail=detail,
        group="灵兽",
        actionable=False,
    )
    row["dashboard_action"] = "miniapp-beast-refresh"
    return row


def miniapp_beast_contract_command(state):
    last_completed = str(state.get("beast_contract_interaction_last_completed_time") or "").strip()
    last_attempt = str(state.get("beast_contract_interaction_last_attempt_time") or "").strip()
    next_time = str(state.get("beast_contract_interaction_next_time") or "").strip()
    error = clean_custom_text(state.get("beast_contract_interaction_last_error") or "", 120)
    results = state.get("beast_contract_interaction_results") or {}
    succeeded = sum(
        1 for item in results.values()
        if isinstance(item, dict) and item.get("status") == "success"
    ) if isinstance(results, dict) else 0
    total = int(state.get("beast_contract_roster_count") or len(state.get("beasts_cache") or []))
    if error:
        status = "部分失败"
        tone = "paused"
        detail = f"{error} · 5 分钟后仅重试失败灵兽"
    elif last_completed:
        status = "已完成"
        tone = "active"
        detail = f"每 2 小时 · 最近覆盖 {succeeded}/{total} 只"
    elif last_attempt:
        status = "执行中"
        tone = "cooldown"
        detail = f"已处理 {succeeded}/{total} 只"
    else:
        status = "等待首次执行"
        tone = "unknown"
        detail = "Mini App 逐只执行灵契互动·安抚"
    return command_row(
        "miniapp:spirit-beast-contract",
        "灵契互动·安抚",
        status,
        tone,
        at=next_time or last_completed or last_attempt,
        detail=detail,
        group="灵兽",
        actionable=False,
        schedule_type="cooldown",
    )


def miniapp_beast_abyss_command(state):
    """Display the server-authoritative six-hour abyss cooldown."""
    last_time = str(state.get("beast_abyss_miniapp_last_time") or "").strip()
    last_attempt = str(state.get("beast_abyss_miniapp_last_attempt_time") or "").strip()
    next_time = str(state.get("beast_abyss_miniapp_next_time") or "").strip()
    error = clean_custom_text(state.get("beast_abyss_miniapp_last_error") or "", 100)
    result = clean_custom_text(state.get("beast_abyss_miniapp_last_result") or "", 120)
    beast = clean_custom_text(state.get("beast_abyss_miniapp_last_beast") or "", 40)
    ready = bool(state.get("beast_abyss_miniapp_ready"))
    abyss_settings = miniapp_beast_abyss_settings()
    power_min = int(abyss_settings.get("power_min") or 0)
    power_max = int(abyss_settings.get("power_max") or 0)
    target = parse_state_time(next_time)
    now = datetime.now()
    next_seconds = max(0, int((target - now).total_seconds())) if target and target > now else 0
    detail_parts = ["每 6 小时", "以 Mini App 页面冷却为准", "按当前万灵宗身份执行"]
    if power_min > 0 or power_max > 0:
        detail_parts.append(f"战力 {power_min or 0}-{power_max or '∞'}")
    else:
        detail_parts.append("战力不限")
    if beast:
        detail_parts.append(f"上次：{beast}")
    if result:
        detail_parts.append(result)
    if error:
        status = "等待重试" if error == "beast_abyss_no_available_beast" else "执行异常"
        tone = "cooldown" if error == "beast_abyss_no_available_beast" else "error"
        detail_parts.insert(0, f"Mini App：{error}")
    elif ready:
        status = "可探渊"
        tone = "ready"
    elif next_seconds > 0:
        status = "冷却中"
        tone = "cooldown"
    elif last_time:
        status = "等待页面刷新"
        tone = "cooldown"
    elif last_attempt:
        status = "同步中"
        tone = "cooldown"
    else:
        status = "等待首次同步"
        tone = "unknown"
    return command_row(
        "miniapp:spirit-beast-abyss",
        "万兽谷探渊",
        status,
        tone,
        remaining=format_remaining(next_seconds) if next_seconds else "0秒",
        at=next_time or last_time or last_attempt,
        detail=" · ".join(detail_parts),
        group="灵兽",
        schedule_type="cooldown",
        next_seconds=next_seconds,
        actionable=False,
    )


def small_world_calamity_command(state):
    """Display the event-driven main-soul calamity watcher."""
    pending = bool(state.get("small_world_calamity_pending"))
    next_time = str(state.get("next_small_world_calamity_time") or "").strip()
    target = parse_state_time(next_time)
    next_seconds = max(0, int((target - datetime.now()).total_seconds())) if target and target > datetime.now() else 0
    hazard = clean_custom_text(state.get("small_world_calamity_last_type") or "", 50)
    try:
        loss = max(0, int(state.get("small_world_calamity_last_incense_loss") or 0))
    except (TypeError, ValueError):
        loss = 0
    error = clean_custom_text(state.get("small_world_calamity_last_error") or "", 100)
    handled_at = str(state.get("small_world_calamity_last_handled_time") or "").strip()
    detail_parts = ["监听机器人关键字【小世界·天降浩劫】", "仅匹配主号 @Weeguu"]
    if hazard:
        detail_parts.append(f"最近：{hazard}")
    if loss:
        detail_parts.append(f"香火损失 {loss}")
    if handled_at:
        detail_parts.append(f"上次处理 {handled_at}")
    if error:
        detail_parts.append(f"Mini App：{error}")
    if pending:
        status = "等待神谕冷却" if next_seconds else "待安抚"
        tone = "error" if error else ("cooldown" if next_seconds else "ready")
    else:
        status = "监听中"
        tone = "watch"
    return command_row(
        ".安抚信徒",
        "天降浩劫安抚",
        status,
        tone,
        remaining=format_remaining(next_seconds) if next_seconds else "",
        at=next_time or handled_at,
        detail=" · ".join(detail_parts),
        group="化神",
        schedule_type="event",
        next_seconds=next_seconds if pending else None,
        actionable=False,
    )


def miniapp_tianxing_journey_command(state):
    """Display the server-backed twice-daily journey automation."""
    today = datetime.now().strftime("%Y-%m-%d")
    count = max(0, int(state.get("miniapp_journey_daily_count") or 0))
    limit = max(1, int(state.get("miniapp_journey_daily_limit") or 2))
    count = min(count, limit)
    error = clean_custom_text(state.get("miniapp_journey_last_error") or "", 100)
    result = clean_custom_text(state.get("miniapp_journey_last_result") or "", 120)
    next_time = str(state.get("miniapp_journey_next_run_time") or "").strip()
    target = parse_state_time(next_time)
    detail_parts = [
        f"今日 {count}/{limit}",
        "天星宗先推命/改命，其他宗门直接历练",
        "固定选择深入",
    ]
    if result:
        detail_parts.append(result)
    if error:
        status = "执行异常"
        tone = "error"
        detail_parts.insert(0, f"Mini App 错误：{error}")
    elif str(state.get("miniapp_journey_last_date") or "") == today or count >= limit:
        status = f"今日已完成 {count}/{limit}"
        tone = "done"
    elif target and target > datetime.now():
        status = "等待执行" if count <= 0 else "等待下一次"
        tone = "cooldown"
    else:
        status = f"待执行 {count}/{limit}"
        tone = "ready"
    next_seconds = (
        max(0, int((target - datetime.now()).total_seconds()))
        if target and target > datetime.now()
        else 0
    )
    return command_row(
        "miniapp:journey-deep",
        "游历·深入",
        status,
        tone,
        remaining=format_remaining(next_seconds) if next_seconds else "0秒",
        at=next_time,
        detail=" · ".join(detail_parts),
        group="游历",
        schedule_type="daily",
        next_seconds=next_seconds,
        actionable=False,
    )


def miniapp_fishing_command(state):
    """Display the shared multi-identity Mini App fishing loop."""
    settings = miniapp_fishing_settings()
    enabled = bool(settings.get("enabled"))
    try:
        runtime = miniapp_fishing_global_snapshot(settings)
    except Exception:
        runtime = {}
    status_key = str(runtime.get("status") or state.get("miniapp_fishing_status") or "waiting")
    error = clean_custom_text(fishing_display_error_text(state.get("miniapp_fishing_last_error")), 100)
    result = clean_custom_text(state.get("miniapp_fishing_last_result") or "", 180)
    next_time = str(state.get("miniapp_fishing_next_run_time") or "").strip()
    target = parse_state_time(next_time)
    next_seconds = (
        max(0, int((target - datetime.now()).total_seconds()))
        if target and target > datetime.now()
        else 0
    )
    def option_name(options, key):
        wanted = str(key or "").strip()
        return dict(options).get(wanted, wanted)

    pond = option_name(MINIAPP_FISHING_PONDS, settings.get("pond"))
    bait = option_name(MINIAPP_FISHING_BAITS, settings.get("bait"))
    chum = option_name(MINIAPP_FISHING_CHUMS, settings.get("chum"))
    last_bait = clean_custom_text(state.get("miniapp_fishing_bait") or "", 40)
    grade = clean_custom_text(state.get("miniapp_fishing_last_grade") or "", 20)
    score = int(state.get("miniapp_fishing_last_score") or 0)
    participants = runtime.get("participant_labels") or []
    current_label = clean_custom_text(runtime.get("current_label") or "", 60)
    runtime_detail = clean_custom_text(fishing_display_error_text(runtime.get("detail")), 180)
    detail_parts = [f"参与 {len(participants)} 个身份", pond, bait, chum, "自动购饵每次 10 份"]
    start_time = clean_custom_text(settings.get("start_time") or "", 10)
    detail_parts.append(f"开始 {start_time}" if start_time else "立即开始")
    if current_label:
        detail_parts.append(f"下一位 {current_label}")
    if runtime_detail:
        detail_parts.append(runtime_detail)
    if last_bait and last_bait != bait:
        detail_parts.append(f"上竿 {last_bait}")
    if grade or score:
        detail_parts.append(f"上次 {grade or '-'} {score}分")
    if result:
        detail_parts.append(result)
    status_map = {
        "waiting": ("等鱼讯", "cooldown"),
        "reeling": ("自动收线", "active"),
        "caught": ("提竿成功", "active"),
        "empty": ("本竿空竿", "cooldown"),
        "settling": ("鱼获结算中", "cooldown"),
        "daily_done": ("今日竿数已尽", "done"),
        "no_rod": ("无鱼竿", "error"),
        "auth_refresh": ("刷新入口", "cooldown"),
        "scanning": ("扫描鱼竿", "cooldown"),
        "ready": ("准备下一竿", "ready"),
        "fishing": ("自动垂钓", "active"),
        "active_round": ("完成当前鱼讯", "active"),
        "waiting_start": ("等待开钓", "cooldown"),
        "shop_unavailable": ("等待鱼饵商店恢复", "cooldown"),
        "waiting_resources": ("等待鱼饵材料", "cooldown"),
        "force_retry": ("强制重试中", "active"),
        "waiting_force_retry": ("等待其他账号重试", "cooldown"),
        "identity_paused": ("身份暂停", "paused"),
        "no_participants": ("未选身份", "paused"),
        "unavailable": ("状态不可用", "error"),
        "paused": ("已暂停", "paused"),
    }
    if not enabled:
        status = "已暂停"
        tone = "paused"
    elif status_key in status_map:
        status, tone = status_map[status_key]
    elif error:
        status = "等待重试"
        tone = "error"
        detail_parts.insert(0, f"Mini App：{error}")
    else:
        status, tone = status_map.get(status_key, ("等待执行", "ready"))
    return command_row(
        "miniapp:fishing",
        "灵溪自动垂钓",
        status,
        tone,
        remaining=format_remaining(next_seconds) if next_seconds else "0秒",
        at=next_time or str(state.get("miniapp_fishing_last_round_time") or ""),
        detail=" · ".join(part for part in detail_parts if part),
        group="游历",
        schedule_type="cooldown",
        next_seconds=next_seconds,
        actionable=False,
    )


def flow_command(command, label=None, detail="流程内自动发送", group=""):
    return command_row(command, label, "流程内", "flow", detail=detail, group=group, actionable=False)


def fishing_command(state):
    fishing = state.get("fishing", {}) if isinstance(state, dict) else {}
    if not isinstance(fishing, dict):
        fishing = {}
    fishing = fishing_dashboard_state(fishing)
    command = fishing_dashboard_command(fishing)
    bait = fishing_dashboard_bait(fishing)
    today_count = int(fishing.get("today_count") or 0)
    daily_limit = int(fishing.get("daily_limit") or FISHING_DAILY_LIMIT)
    detail_parts = [f"今日 {today_count}/{daily_limit}", f"饵料 {bait}"]
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
            command,
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
            command,
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
        command,
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


def fishing_auto_commands(account, state):
    auto_state = state.get("fishing_auto", {}) if isinstance(state, dict) else {}
    auto_state = fishing_auto_dashboard_state(auto_state)
    control_entry = fishing_auto_control_entry(account)
    preferred_bait = fishing_auto_bait_from_entry(
        control_entry,
        fallback=fishing_auto_bait_for_state(auto_state),
    )
    active_identity = str(auto_state.get("active_identity") or "").strip()
    rod_holder = str(auto_state.get("rod_holder") or "").strip()
    completed = auto_state.get("completed", {}) if isinstance(auto_state.get("completed"), dict) else {}
    detail_parts = [f"鱼饵 {preferred_bait}"]
    if active_identity:
        detail_parts.append(f"当前 {active_identity}")
    if rod_holder:
        detail_parts.append(f"鱼竿 {rod_holder}")
    if completed:
        detail_parts.append(f"已完成 {len(completed)}")
    last_detail = str(auto_state.get("last_detail") or "").strip()
    if last_detail:
        detail_parts.append(last_detail)

    status_map = {
        "enabled": "已启用",
        "running": "运行中",
        "transferring": "转移鱼竿",
        "transferred": "已转移",
        "transfer_failed": "转移失败",
        "no_rod_holder": "找鱼竿",
        "handoff_wait": "接力等待",
        "done": "今日已满",
        "paused": "已暂停",
        "waiting": "等待中",
    }
    last_status = str(auto_state.get("last_status") or "paused")
    next_action = parse_state_time(auto_state.get("next_action_at", ""))
    if next_action and next_action > datetime.now():
        next_seconds = max(0, int((next_action - datetime.now()).total_seconds()))
        row = command_row(
            FISHING_AUTO_CONTROL_COMMAND,
            "全自动钓鱼",
            "等待中",
            "cooldown",
            format_remaining(next_seconds),
            str(auto_state.get("next_action_at") or ""),
            " · ".join(detail_parts),
            "钓鱼",
            schedule_type="cooldown",
            next_seconds=next_seconds,
            default_paused=True,
        )
    else:
        row = command_row(
            FISHING_AUTO_CONTROL_COMMAND,
            "全自动钓鱼",
            status_map.get(last_status, "就绪"),
            "ready" if last_status not in {"paused", "done"} else "unknown",
            "0秒",
            str(auto_state.get("next_action_at") or ""),
            " · ".join(detail_parts),
            "钓鱼",
            schedule_type="cooldown",
            next_seconds=0,
            default_paused=True,
        )
    row["bait_options"] = list(FISHING_CONTROL_BAITS)
    row["bait_value"] = preferred_bait
    return [row]


def fishing_auto_identity_label(account, identity):
    identity = str(identity or "主魂").strip() or "主魂"
    return f"{ACCOUNT_SHORT_NAMES.get(account, account)}[{identity}]"


def fishing_auto_global_path():
    return os.path.join(CONFIG_DIR, "fishing_auto_global.json")


def fishing_auto_identity_key(account, identity):
    return f"{account}|{str(identity or '主魂').strip() or '主魂'}"


def save_json_atomic(path, data):
    save_json_state(path, data or {}, backup=False)


def account_state_path(account):
    return os.path.join(CONFIG_DIR, f"state_{account}.json")


def load_account_state_raw(account):
    path = account_state_path(account)
    return load_json_state(path, expected_type=dict, default={}) or {}


def fishing_identity_state_raw(root, identity, create=False):
    identity = str(identity or "主魂").strip() or "主魂"
    if identity == "主魂":
        if create:
            return root.setdefault("fishing", {})
        return root.get("fishing", {}) if isinstance(root.get("fishing"), dict) else {}
    avatars = root.setdefault("avatars", {}) if create else root.get("avatars", {})
    if not isinstance(avatars, dict):
        return {}
    avatar = avatars.setdefault(identity, {}) if create else avatars.get(identity, {})
    if not isinstance(avatar, dict):
        return {}
    if create:
        return avatar.setdefault("fishing", {})
    return avatar.get("fishing", {}) if isinstance(avatar.get("fishing"), dict) else {}


def set_fishing_auto_holder_state(account, identity, username="dashboard"):
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime(TIME_FORMAT)
    holder = {
        "account": account,
        "identity": identity,
        "updated_at": now,
        "updated_by": username,
    }

    global_path = fishing_auto_global_path()
    try:
        with open(global_path, "r", encoding="utf-8") as f:
            global_state = json.load(f)
        if not isinstance(global_state, dict):
            global_state = {}
    except Exception:
        global_state = {}
    if global_state.get("date") != today:
        bait = global_state.get("preferred_bait") if global_state.get("preferred_bait") in FISHING_CONTROL_BAITS else FISHING_BAIT
        global_state = {"date": today, "preferred_bait": bait, "completed": {}}
    global_state.setdefault("preferred_bait", FISHING_BAIT)
    global_state.setdefault("completed", {})
    global_state["date"] = today
    global_state["rod_holder"] = holder
    global_state["active"] = {
        "account": account,
        "identity": identity,
        "key": fishing_auto_identity_key(account, identity),
        "updated_at": now,
        "updated_by": username,
    }
    global_state["transfer"] = {}
    global_state["updated_at"] = now
    save_json_atomic(global_path, global_state)

    for account_name in FISHING_AUTO_CONTROL_ACCOUNTS:
        state_path = account_state_path(account_name)
        if not os.path.exists(state_path) and not os.path.exists(f"{state_path}.bak"):
            continue

        def update_account(root, account_name=account_name):
            if not isinstance(root, dict) or not root:
                return root
            for candidate in FISHING_AUTO_ACCOUNT_IDENTITIES.get(account_name, ("主魂",)):
                fishing = fishing_identity_state_raw(
                    root,
                    candidate,
                    create=(account_name == account and candidate == identity),
                )
                if isinstance(fishing, dict):
                    fishing["rod_owned"] = bool(
                        account_name == account and candidate == identity
                    )
            auto_state = root.setdefault("fishing_auto", {})
            if account_name == account:
                auto_state["rod_holder"] = identity
                auto_state["active_identity"] = identity
                auto_state["last_status"] = "holder_set"
                auto_state["last_detail"] = f"dashboard 指定鱼竿持有者：{identity}"
                auto_state["next_action_at"] = ""
            else:
                auto_state["rod_holder"] = ""
                auto_state["active_identity"] = ""
            return root

        update_json_state(
            state_path,
            update_account,
            expected_type=dict,
            default={},
        )
    return holder


def load_fishing_auto_global_dashboard_state():
    path = fishing_auto_global_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    today = datetime.now().strftime("%Y-%m-%d")
    view = {
        "date": data.get("date") or today,
        "preferred_bait": data.get("preferred_bait") if data.get("preferred_bait") in FISHING_CONTROL_BAITS else FISHING_BAIT,
        "active": data.get("active") if isinstance(data.get("active"), dict) else {},
        "rod_holder": data.get("rod_holder") if isinstance(data.get("rod_holder"), dict) else {},
        "completed": data.get("completed") if isinstance(data.get("completed"), dict) else {},
        "transfer": data.get("transfer") if isinstance(data.get("transfer"), dict) else {},
        "updated_at": data.get("updated_at") or "",
    }
    if view["date"] != today:
        view["active"] = {}
        view["completed"] = {}
        view["transfer"] = {}
    return view


def fishing_state_for_identity(root_state, identity):
    root_state = root_state if isinstance(root_state, dict) else {}
    identity = str(identity or "主魂").strip() or "主魂"
    if identity == "主魂":
        fishing = root_state.get("fishing", {})
    else:
        avatars = root_state.get("avatars", {}) if isinstance(root_state.get("avatars"), dict) else {}
        avatar = avatars.get(identity, {}) if isinstance(avatars.get(identity), dict) else {}
        fishing = avatar.get("fishing", {})
    return fishing_dashboard_state(fishing if isinstance(fishing, dict) else {})


def fishing_auto_control_summary(controls=None):
    controls = controls if isinstance(controls, dict) else load_command_controls()
    entries = []
    baits = []
    for account in FISHING_AUTO_CONTROL_ACCOUNTS:
        account_controls = controls.get(account, {})
        if not isinstance(account_controls, dict):
            continue
        identity_controls = account_controls.get("主魂", {})
        if not isinstance(identity_controls, dict):
            continue
        entry = identity_controls.get(FISHING_AUTO_CONTROL_COMMAND)
        if isinstance(entry, dict):
            entries.append({"account": account, "entry": entry})
            bait = str(entry.get("bait") or "").strip()
            if bait in FISHING_CONTROL_BAITS:
                baits.append(bait)
    enabled_accounts = [
        item["account"]
        for item in entries
        if not bool(item["entry"].get("disabled"))
    ]
    return {
        "enabled": bool(enabled_accounts),
        "partial": 0 < len(enabled_accounts) < len(FISHING_AUTO_CONTROL_ACCOUNTS),
        "enabled_accounts": enabled_accounts,
        "bait": baits[0] if baits else FISHING_BAIT,
    }


def fishing_auto_dashboard_summary(states):
    states = states if isinstance(states, dict) else {}
    today = datetime.now().strftime("%Y-%m-%d")
    controls = load_command_controls()
    control = fishing_auto_control_summary(controls)
    global_state = load_fishing_auto_global_dashboard_state()
    bait = control.get("bait") or global_state.get("preferred_bait") or FISHING_BAIT
    if bait not in FISHING_CONTROL_BAITS:
        bait = FISHING_BAIT

    identities = []
    active_round = None
    holder = global_state.get("rod_holder") if isinstance(global_state.get("rod_holder"), dict) else {}
    for account in FISHING_AUTO_CONTROL_ACCOUNTS:
        root = states.get(account, {}) if isinstance(states.get(account), dict) else {}
        for identity in FISHING_AUTO_ACCOUNT_IDENTITIES.get(account, ("主魂",)):
            fishing = fishing_state_for_identity(root, identity)
            count = int(fishing.get("today_count") or 0)
            limit = int(fishing.get("daily_limit") or FISHING_DAILY_LIMIT)
            done = fishing_daily_done_for_today(fishing, today=today)
            active_due_at = str(fishing.get("active_due_at") or "")
            active_due = parse_state_time(active_due_at)
            remaining = max(0, int((active_due - datetime.now()).total_seconds())) if active_due else None
            is_active = bool(fishing.get("active")) and (
                not active_due or active_due.strftime("%Y-%m-%d") == today
            )
            item = {
                "account": account,
                "account_name": ACCOUNT_SHORT_NAMES.get(account, account),
                "identity": identity,
                "label": fishing_auto_identity_label(account, identity),
                "today_count": count,
                "daily_limit": limit,
                "done": bool(done),
                "status": str(fishing.get("last_status") or "paused"),
                "detail": str(fishing.get("last_detail") or ""),
                "active": is_active,
                "active_due_at": active_due_at,
                "remaining_seconds": remaining,
                "rod_owned": fishing.get("rod_owned"),
            }
            identities.append(item)
            if not holder and fishing.get("rod_owned") is True:
                holder = {"account": account, "identity": identity, "updated_at": ""}
            if item["active"] and active_round is None:
                active_round = item

    by_key = {f"{item['account']}|{item['identity']}": item for item in identities}
    active = global_state.get("active") if isinstance(global_state.get("active"), dict) else {}
    target = None
    active_key = str(active.get("key") or "")
    if active_key and active_key in by_key and not by_key[active_key].get("done"):
        target = by_key[active_key]
    if target is None:
        for item in identities:
            if not item.get("done"):
                target = item
                break

    holder_label = ""
    if isinstance(holder, dict) and holder.get("account") and holder.get("identity"):
        holder_label = fishing_auto_identity_label(holder.get("account"), holder.get("identity"))

    transfer = global_state.get("transfer") if isinstance(global_state.get("transfer"), dict) else {}
    transfer_view = {}
    if transfer:
        transfer_view = {
            "status": str(transfer.get("status") or ""),
            "listing_id": str(transfer.get("listing_id") or ""),
            "from_label": fishing_auto_identity_label(transfer.get("from_account"), transfer.get("from_identity")),
            "to_label": fishing_auto_identity_label(transfer.get("to_account"), transfer.get("to_identity")),
            "updated_at": str(transfer.get("updated_at") or transfer.get("started_at") or ""),
        }

    completed_count = sum(1 for item in identities if item.get("done"))
    total_count = len(identities)
    enabled = bool(control.get("enabled"))
    partial = bool(control.get("partial"))
    current = active_round or target
    if not enabled:
        status_text = "已暂停"
        status_tone = "paused"
    elif completed_count >= total_count and total_count:
        status_text = "今日已全部完成"
        status_tone = "done"
    elif transfer_view:
        status_text = "鱼竿转移中"
        status_tone = "flow"
    elif active_round:
        due_text = format_remaining(active_round["remaining_seconds"]) if active_round.get("remaining_seconds") is not None else ""
        status_text = f"{active_round['label']} 钓鱼中{f'，{due_text} 后提竿' if due_text else ''}"
        status_tone = "active"
    elif target:
        status_text = f"等待 {target['label']}"
        status_tone = "cooldown"
    else:
        status_text = "等待队列同步"
        status_tone = "unknown"
    if partial:
        status_text = f"部分启用 · {status_text}"

    return {
        "enabled": enabled,
        "partial": partial,
        "bait": bait,
        "bait_options": list(FISHING_CONTROL_BAITS),
        "command": FISHING_AUTO_CONTROL_COMMAND,
        "control_key": FISHING_AUTO_CONTROL_COMMAND,
        "label": "全自动钓鱼",
        "status": status_text,
        "status_tone": status_tone,
        "current": current or {},
        "target": target or {},
        "rod_holder": holder_label,
        "rod_holder_detail": holder if isinstance(holder, dict) else {},
        "holder_options": [
            {"account": item["account"], "identity": item["identity"], "label": item["label"]}
            for item in identities
        ],
        "transfer": transfer_view,
        "completed_count": completed_count,
        "pending_count": max(0, total_count - completed_count),
        "total_count": total_count,
        "identities": identities,
        "updated_at": global_state.get("updated_at") or "",
        "date": today,
    }


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
    ready_slots = sorted(
        int(slot)
        for slot, item in slots.items()
        if (
            isinstance(item, dict)
            and item.get("status") == "精华已成"
            and str(item.get("soul") or "").strip() == YINLUO_SOUL
        )
    )
    ready = len(ready_slots)
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
        "summon_flow_complete": "流程完成",
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
    rows.append(command_row(YINLUO_APPEASE_COMMAND, "安抚幡灵", "需安抚" if exhausted else "按需", "ready" if exhausted else "manual", detail=f"魂力枯竭槽 {exhausted}", group="阴罗宗"))
    rows.append(command_row(".收取精华 <槽位>", "收取精华", "可收取" if ready else "按需", "ready" if ready else "manual", detail=f"凶兽戾魄精华已成槽 {ready_slots}", group="阴罗宗"))
    rows.append(command_row(f".囚禁魂魄 <槽位> {YINLUO_SOUL}", "囚禁凶兽", "可炼化" if empty and fierce else "等待", "ready" if empty and fierce else "manual", detail=f"只囚禁{YINLUO_SOUL}；空槽 {empty}，储备 {fierce}", group="阴罗宗"))
    rows.append(command_row(YINLUO_CONVERT_COMMAND, "化功为煞", "煞气不足时", "manual", detail="仅囚禁凶兽戾魄且煞气不足时自动使用", group="阴罗宗"))
    return rows


def _soul_curse_dashboard_identity_candidates(account, identity, state=None):
    """Return current and legacy names for one dashboard identity switch.

    Rebirth changes a Dao name while ``soul_curse_settings.json`` may still
    contain the retired name.  Keep the current name first so an explicit
    current-name value overrides a legacy value, then follow persisted alias
    links to retain old settings during the migration window.
    """
    account = str(account or "").strip()
    original = str(identity or "主魂").strip() or "主魂"
    candidates = []

    def add(value):
        value = str(value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    try:
        current = canonical_automation_identity(account, original)
    except Exception:
        current = original
    add(current)
    add(original)

    alias_state = state if isinstance(state, dict) else None
    # Most avatar panels pass only the avatar subsection, while aliases and
    # identity->sect mappings live at the account root.  Load the root as a
    # supplement when needed so a renamed Yinluo avatar is still recognized.
    root_state = None
    if account in WINDOW_MAP:
        try:
            root_state = load_account_state_raw(account)
        except Exception:
            root_state = None
    if alias_state is None:
        alias_state = root_state
    aliases = alias_state.get("avatar_dao_name_aliases") if isinstance(alias_state, dict) else None
    if not isinstance(aliases, dict) and isinstance(root_state, dict):
        aliases = root_state.get("avatar_dao_name_aliases")
    if isinstance(aliases, dict):
        changed = True
        while changed:
            changed = False
            for old_name, new_name in aliases.items():
                old_name = str(old_name or "").strip()
                new_name = str(new_name or "").strip()
                if not old_name or not new_name:
                    continue
                if new_name in candidates and old_name not in candidates:
                    add(old_name)
                    changed = True
                elif old_name in candidates and new_name not in candidates:
                    add(new_name)
                    changed = True

    # A legacy file without aliases can still use the historical Yinluo name.
    # Only add these aliases when the requested identity itself is one of the
    # known Yinluo names; this prevents a non-Yinluo avatar inheriting its flag.
    state_sect = ""
    if isinstance(state, dict):
        state_sect = str(
            state.get("miniapp_sect_name") or state.get("sect_name") or ""
        ).strip()
        state_identity_sects = state.get("identity_sect_names")
        if not state_sect and isinstance(state_identity_sects, dict):
            state_sect = str(
                state_identity_sects.get(original)
                or state_identity_sects.get(current)
                or ""
            ).strip()
    if not state_sect and isinstance(root_state, dict):
        root_identity_sects = root_state.get("identity_sect_names")
        if isinstance(root_identity_sects, dict):
            state_sect = str(
                root_identity_sects.get(original)
                or root_identity_sects.get(current)
                or ""
            ).strip()

    yinluo_names = set()
    if account == "main":
        yinluo_names.add("缘生子")
    elif account == "sub":
        yinluo_names.update({SUB_YINLUO_IDENTITY, DEFAULT_SUB_YINLUO_IDENTITY})
    legacy_yinluo_names = []
    if account == "main":
        legacy_yinluo_names.append("缘生子")
    elif account == "sub":
        legacy_yinluo_names.append("缘生子")
        legacy_yinluo_names.extend([SUB_YINLUO_IDENTITY, DEFAULT_SUB_YINLUO_IDENTITY])
    if any(name in yinluo_names for name in candidates):
        for name in legacy_yinluo_names:
            add(name)
    return candidates


def soul_curse_identity_enabled_for_dashboard(account, identity, state=None, *, assistant=False):
    """Dashboard 侧读运行时同一份 soul_curse_settings.json 开关。"""
    import json

    try:
        with open(SOUL_CURSE_SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return False
    if not isinstance(data, dict) or not data.get("enabled", True):
        return False
    identities = data.get("identities")
    if not isinstance(identities, dict):
        return False
    account_map = identities.get(str(account or ""))
    if isinstance(account_map, dict):
        for candidate in _soul_curse_dashboard_identity_candidates(account, identity, state=state):
            if candidate in account_map:
                return bool(account_map.get(candidate))
    if isinstance(account_map, list):
        return any(candidate in account_map for candidate in
                   _soul_curse_dashboard_identity_candidates(account, identity, state=state))
    return bool(assistant)


def soul_curse_switch_row(identity, enabled, group="南宫婉", account=None):
    """封魂咒链路的身份开关状态行（dashboard 一键启停）。"""
    row = command_row(
        f"封魂咒链路 [{identity}]",
        f"封魂咒链路 · {identity}",
        "已启用" if enabled else "已关闭",
        "ready" if enabled else "done",
        detail=(
            "整条链（探望→推演→护持→委托/接取）按排期自动执行"
            if enabled
            else "点击右侧按钮启用：探望→推演→护持→委托/接取整链自动化"
        ),
        group=group,
        schedule_type="daily",
        actionable=False,
    )
    row["dashboard_action"] = "soul-curse-toggle"
    row["soul_curse_enabled"] = bool(enabled)
    row["soul_curse_identity"] = identity
    return row


def soul_curse_yinluo_publisher_for_dashboard(account, identity, root_state=None):
    candidates = _soul_curse_dashboard_identity_candidates(account, identity, state=root_state)
    return any(
        profile.get("allow_yinluo_publisher") and profile.get("identity") in candidates
        for profile in SOUL_CURSE_AVATAR_PUBLISHERS.get(account, ())
    )


def soul_curse_publisher_commands(state, account=None, identity="主魂", *, root_state=None):
    curse = state.get("soul_curse", {}) if isinstance(state, dict) else {}
    if not isinstance(curse, dict):
        curse = {}
    if account and not soul_curse_identity_enabled_for_dashboard(
        account, identity, state=root_state if root_state is not None else state
    ):
        return [soul_curse_switch_row(identity, False)]
    detail = clean_custom_text(curse.get("last_detail") or "", 120)
    commission_id = str(curse.get("commission_id") or "").strip()
    target = str(curse.get("commission_target") or "").strip()
    commission_detail = " · ".join(part for part in [f"委托 {commission_id}" if commission_id else "", target, detail] if part)
    chain_target = parse_state_time(curse.get("next_chain_time", ""))
    chain_locked = (
        chain_target
        and chain_target > datetime.now()
        and str(curse.get("chain_stage") or "") in {"", "done"}
    )
    chain_detail = "整条解咒链按最后成功剥离咒源时间冷却"

    if chain_locked:
        infer_row = time_command(
            curse,
            "next_chain_time",
            SOUL_CURSE_INFER_COMMAND,
            "封魂咒推演",
            waiting="整链冷却",
            ready="可推演",
            missing="可推演",
            detail=chain_detail,
            group="南宫婉",
        )
        protect_row = time_command(
            curse,
            "next_chain_time",
            SOUL_CURSE_PROTECT_COMMAND,
            "护持神魂",
            waiting="整链冷却",
            ready="链路内执行",
            missing="链路内执行",
            detail=chain_detail,
            group="南宫婉",
        )
        publish_row = time_command(
            curse,
            "next_chain_time",
            SOUL_CURSE_PUBLISH_COMMAND,
            "解咒委托",
            waiting="整链冷却",
            ready="可检查",
            missing="可检查",
            detail=commission_detail or chain_detail,
            group="南宫婉",
        )
    else:
        infer_row = time_command(
            curse,
            "next_chain_time",
            SOUL_CURSE_INFER_COMMAND,
            "封魂咒推演",
            waiting="8小时冷却",
            ready="可推演",
            missing="可推演",
            detail=detail,
            group="南宫婉",
        )
        protect_row = time_command(
            curse,
            "next_protect_time",
            SOUL_CURSE_PROTECT_COMMAND,
            "护持神魂",
            waiting="8小时冷却",
            ready="链路内执行",
            missing="链路内执行",
            detail=detail,
            group="南宫婉",
        )
        publish_row = time_command(
            curse,
            "next_action_at",
            SOUL_CURSE_PUBLISH_COMMAND,
            "解咒委托",
            waiting="等待下一步",
            ready="可检查",
            missing="可检查",
            detail=commission_detail,
            group="南宫婉",
        )
    rows = [
        daily_done_command(
            curse,
            SOUL_CURSE_VISIT_COMMAND,
            "探望南宫婉",
            date_key="last_visit_date",
            detail=detail,
            group="南宫婉",
        ),
    ]
    publisher_profile = SOUL_CURSE_PUBLISHERS.get(str(account or ""), {}) if identity == "主魂" else {}
    if publisher_profile.get("wanying_greeting_enabled"):
        wanying_row = daily_done_command(
            curse,
            SOUL_CURSE_WANYING_GREETING_COMMAND,
            "婉影问安",
            date_key="last_wanying_greeting_date",
            detail=detail,
            group="南宫婉",
        )
        if wanying_row.get("tone") != "done":
            wanying_row = time_command(
                curse,
                "next_wanying_greeting_time",
                SOUL_CURSE_WANYING_GREETING_COMMAND,
                "婉影问安",
                waiting="今日稍后",
                ready="可问安",
                missing="可问安",
                detail=detail,
                group="南宫婉",
            )
        rows.append(wanying_row)
    rows.extend([infer_row, protect_row, publish_row])
    return rows


def soul_curse_assist_commands(state, account=None, identity=None, *, root_state=None, include_switch=True):
    assist = state.get("soul_curse_assist", {}) if isinstance(state, dict) else {}
    if not isinstance(assist, dict):
        assist = {}
    if identity and not soul_curse_identity_enabled_for_dashboard(
        account, identity, state=root_state if root_state is not None else state, assistant=True
    ):
        return [soul_curse_switch_row(identity, False, group="阴罗宗")] if include_switch else []
    commission_id = str(assist.get("commission_id") or "").strip()
    target = str(assist.get("target_username") or "").strip()
    detail = clean_custom_text(assist.get("last_detail") or "", 120)
    base_detail = " · ".join(part for part in [f"委托 {commission_id}" if commission_id else "", target, detail] if part)
    accept_command = f"{SOUL_CURSE_ACCEPT_COMMAND} <ID>"
    target_suffix = target or "<@委托用户>"
    return [
        time_command(
            assist,
            "next_action_at",
            accept_command,
            "接取解咒委托",
            waiting="等待处理",
            ready="待委托",
            missing="待委托",
            detail=base_detail,
            group="阴罗宗",
        ),
        time_command(
            assist,
            "next_identify_time",
            f"{SOUL_CURSE_IDENTIFY_COMMAND} {target_suffix}",
            "辨认咒纹",
            waiting="4小时冷却",
            ready="可辨认",
            missing="可辨认",
            detail=base_detail,
            group="阴罗宗",
        ),
        time_command(
            assist,
            "next_suppress_time",
            f"{SOUL_CURSE_SUPPRESS_COMMAND} {target_suffix}",
            "借幡镇魂",
            waiting="6小时冷却",
            ready="可镇魂",
            missing="可镇魂",
            detail=base_detail,
            group="阴罗宗",
        ),
        time_command(
            assist,
            "next_strip_time",
            f"{SOUL_CURSE_STRIP_COMMAND} {target_suffix}",
            "剥离咒源",
            waiting="8小时冷却",
            ready="可剥离",
            missing="可剥离",
            detail=base_detail,
            group="阴罗宗",
        ),
    ]


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
    # 观星台/安抚/收集/牵引已迁入 miniapp，Dashboard 不再展示聊天指令入口。
    return []


def taiyi_guide_command(state):
    return time_command(
        state,
        "next_taiyi_guide_time",
        ".引道 水",
        "引道 水",
        waiting="12小时冷却",
        ready="可引道",
        missing="可引道",
        detail=clean_custom_text(state.get("last_taiyi_guide_response") or "", 80),
        group="太一门",
    )


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
    return (
        CONCUBINE_VOYAGE_AUTO_START_ENABLED
        and account == "main"
        and (identity or "主魂") == "主魂"
    )


def concubine_voyage_detail(state):
    error = str(state.get("last_concubine_voyage_error", "") or "").replace("\n", " ").strip()
    if not error:
        return ""
    when = str(state.get("last_concubine_voyage_error_time", "") or "").strip()
    prefix = f"上次失败 {when}: " if when else "上次失败: "
    return f"{prefix}{error[:80]}"


def target_concubine_detail(state):
    target = str(state.get("target_concubine_name") or "").strip() or TARGET_CONCUBINE_NAME
    current = str(state.get("concubine_name") or "").strip()
    found = bool(state.get("target_concubine_found") and current == target)
    pieces = [f"目标 {target}"]
    if current:
        pieces.append(f"当前 {current}")
    pieces.append("已找到" if found else "寻找中")
    return " · ".join(pieces)


def concubine_commands(state, include_divination=True, include_voyage=False, include_status=True):
    rows = []
    if include_status:
        rows.append(manual_command(".我的侍妾", "我的侍妾", "查询侍妾/冷却", "侍妾"))
    rows.append(time_command(state, "next_dream_map_time", ".入梦寻图", "入梦寻图", group="侍妾"))
    rows.append(time_command(
        state,
        "next_heart_trial_time",
        ".共历心劫",
        "共历心劫",
        detail="仅在 Telegram 群聊执行",
        group="侍妾",
        default_paused=True,
    ))
    if include_voyage:
        rows.append(time_command(
            state, "next_concubine_voyage_time", ".侍妾远航 月殿寻痕", "侍妾远航",
            waiting="6小时冷却", detail=concubine_voyage_detail(state), group="侍妾",
        ))
    if include_divination:
        rows.append(time_command(state, "next_divination_time", ".天机代卜", "天机代卜", group="侍妾"))
    if state.get("target_concubine_name"):
        rows.append(time_command(
            state,
            "next_concubine_search_time",
            CONCUBINE_SEARCH_COMMAND,
            "红尘寻缘",
            waiting="2小时冷却",
            ready="可寻缘",
            missing="可寻缘",
            detail=target_concubine_detail(state),
            group="南宫婉",
        ))
        rows.append(manual_command(CONCUBINE_DISMISS_COMMAND, "遣散侍妾", "非南宫婉时使用", "南宫婉"))
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
    wait_for_rebirth = bool(entry.get("wait_for_rebirth"))
    target = parse_state_time(entry.get("until", ""))
    if not wait_for_rebirth and (not target or target <= datetime.now()):
        return panel
    seconds = max(0, int((target - datetime.now()).total_seconds())) if target else 365 * 24 * 3600
    reason = clean_custom_text(entry.get("reason") or "身份暂停", 80)
    remaining = "等待重生" if wait_for_rebirth else format_remaining(seconds)
    until = entry.get("until", "") or ("等待 .重生 1 / .重生 2 / .重生 3 任一成功" if wait_for_rebirth else "")
    for row in panel.get("commands") or []:
        old_status = row.get("status", "")
        old_detail = row.get("detail", "")
        row["status"] = "元婴虚弱暂停"
        row["tone"] = "paused"
        row["remaining"] = remaining
        row["at"] = until
        row["next_seconds"] = seconds
        row["detail"] = (
            f"{reason}，{('恢复条件：' + until) if wait_for_rebirth else ('暂停至 ' + until)}"
            f"{f' · 原状态：{old_status}' if old_status else ''}"
            f"{f' · {old_detail}' if old_detail else ''}"
        )
    return panel


def main_soul_panel(account, state):
    rows = []
    rows.extend(global_sync_commands())
    if account == "main":
        identity_sects = state.get("identity_sect_names") or {}
        main_soul_sect = str(
            (identity_sects.get("主魂") if isinstance(identity_sects, dict) else "")
            or state.get("sect_name")
            or "天星宗"
        ).strip()
        hunt_stopped = bool(state.get("beast_hunt_stopped"))
        hunt_reason = clean_custom_text(state.get("beast_hunt_stopped_reason") or "已按策略停止寻觅灵兽", 120)
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_node_search_time", NODE_SEARCH_COMMAND, "搜寻节点", waiting="12小时冷却", group="化神"),
            time_command(
                state,
                "next_treasure_refine_time",
                TREASURE_REFINE_COMMAND,
                "虚天鼎炼焰",
                waiting="8小时冷却",
                ready="可炼焰",
                missing="待祭炼",
                detail=f"炼焰进度 {state.get('treasure_refine_progress') or 0}/9" + (" · 已圆满" if state.get("treasure_refine_complete") else ""),
                group="法宝",
            ),
            time_command(state, "next_treasure_touch_time", MAIN_TREASURE_TOUCH_COMMAND, "抚摸法宝", group="法宝"),
            time_command(state, "next_small_world_time", ".小世界", "小世界", waiting="6小时冷却", group="化神"),
            manual_command(".显灵", "显灵", "凡人祈愿时自动响应", "化神"),
            small_world_calamity_command(state),
            time_command(state, "next_miracle_preach_time", ".神迹 布道", "神迹 布道", waiting="3小时冷却", group="化神"),
        ])
        rows.extend(meditation_commands(state))
        rows.extend(soul_curse_publisher_commands(state, account=account))
        rows.extend(sect_war_commands(state))
        rows.extend([
            manual_command(".安置侍妾", "安置侍妾", group="侍妾"),
        ])
        rows.append(miniapp_fishing_command(state))
        if main_soul_sect == "天星宗":
            rows.extend([
                manual_command(".推命 闭关", "推命闭关", group="天星宗"),
                daily_done_command(
                    state,
                    ".观命",
                    "观命",
                    date_key="last_destiny_date",
                    detail=f"上次定命：{state.get('last_destiny_choice') or '未记录'}",
                    group="天星宗",
                ),
            ])
        if main_soul_sect == "万灵宗":
            rows.extend([
                miniapp_beast_sync_command(state),
                miniapp_beast_contract_command(state),
                miniapp_beast_abyss_command(state),
                (
                    command_row(
                        ".寻觅灵兽", "寻觅灵兽", "已停止", "done",
                        detail=hunt_reason, group="灵兽", schedule_type="cooldown", actionable=False,
                    )
                    if hunt_stopped
                    else time_command(state, "next_hunt_time", ".寻觅灵兽", "寻觅灵兽", group="灵兽")
                ),
                time_command(state, "next_beast_border_patrol_time", ".灵兽巡边 <灵兽> <斥候/护粮/袭营>", "灵兽巡边", group="灵兽"),
                manual_command(".巡边状态", "巡边状态", group="灵兽"),
                manual_command(".巡边归来", "巡边归来", group="灵兽"),
            ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    elif account == "sub":
        rows.extend([
            yuanying_retreat_command(state),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_ask_dao_time", ASK_DAO_COMMAND, "问道", waiting="冷却中", ready="可问道", missing="可问道", group="元婴宗"),
        ])
        rows.extend(meditation_commands(state))
        rows.extend(soul_curse_publisher_commands(state, account=account))
        rows.extend(concubine_commands(
            state,
            include_divination=True,
            include_voyage=concubine_voyage_enabled(account, "主魂"),
            include_status=False,
        ))
    elif account == "xiaohao":
        restricted_miniapp = bool(state.get("restricted_miniapp_active"))
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_treasure_touch_time", SUB_TREASURE_TOUCH_COMMAND, "抚摸法宝", group="法宝"),
        ])
        rows.extend(sect_war_commands(state))
        rows.extend(meditation_commands(state))
        rows.extend([
            manual_command(".安置侍妾", "安置侍妾", group="侍妾"),
        ])
        rows.extend(soul_curse_publisher_commands(state, account=account))
        if restricted_miniapp:
            beast_sync = str(state.get("beast_miniapp_last_sync_time") or "")
            beast_error = clean_custom_text(state.get("beast_miniapp_last_error") or "", 80)
            beast_count = len(state.get("beasts_cache") or [])
            rows.append(command_row(
                "miniapp:spirit-beast:xiaohao",
                "万兽谷同步",
                "异常" if beast_error else ("已同步" if beast_sync else "等待同步"),
                "error" if beast_error else ("done" if beast_sync else "pending"),
                detail=(
                    f"Mini App 错误：{beast_error}"
                    if beast_error
                    else f"每天两次 · {beast_count} 只 · {beast_sync or '尚未完成首次同步'}"
                ),
                group="灵兽",
                actionable=False,
            ))
            rows.append(miniapp_beast_contract_command(state))
            rows.append(miniapp_beast_abyss_command(state))
            rows.append(
                time_command(
                    state,
                    "next_hunt_time",
                    ".寻觅灵兽",
                    "寻觅灵兽",
                    group="灵兽",
                )
            )
            unsupported_detail = "公开群受限；该手动旧指令不属于 Mini App 自动寻觅流程"
            for command, label in (
                (".放生 <灵兽>", "放生灵兽"),
                (".灵兽出战 <灵兽>", "灵兽出战"),
            ):
                rows.append(command_row(
                    command,
                    label,
                    "Mini App 不支持",
                    "done",
                    detail=unsupported_detail,
                    group="灵兽",
                    actionable=False,
                ))
            rows.extend([
                time_command(
                    state,
                    "next_beast_border_patrol_time",
                    ".灵兽巡边 <灵兽> <斥候/护粮/袭营>",
                    "灵兽巡边",
                    group="灵兽",
                ),
                manual_command(".巡边状态", "巡边状态", group="灵兽"),
                manual_command(".巡边归来", "巡边归来", group="灵兽"),
            ])
        else:
            rows.extend([
                command_row(
                    "miniapp:spirit-beast:xiaohao",
                    "万兽谷同步",
                    "需独立入口",
                    "unknown",
                    detail="固定入口与 Telegram 账号绑定，未配置前不发送废弃的 .我的灵兽",
                    group="灵兽",
                    actionable=False,
                ),
                miniapp_beast_contract_command(state),
                miniapp_beast_abyss_command(state),
                time_command(state, "next_hunt_time", ".寻觅灵兽", "寻觅灵兽", group="灵兽"),
                manual_command(".放生 <灵兽>", "放生灵兽", "流程内按需", "灵兽"),
                manual_command(".灵兽出战 <灵兽>", "灵兽出战", group="灵兽"),
                time_command(state, "next_beast_border_patrol_time", ".灵兽巡边 <灵兽> <斥候/护粮/袭营>", "灵兽巡边", group="灵兽"),
                manual_command(".巡边状态", "巡边状态", group="灵兽"),
                manual_command(".巡边归来", "巡边归来", group="灵兽"),
            ])
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled(account, "主魂")))
    elif account == "waaiging":
        sect_joined = bool(state.get("sect_join_confirmed"))
        rows.extend([
            (
                command_row(
                    ".拜入宗门 天星宗",
                    "拜入天星宗",
                    "已入宗",
                    "done",
                    detail="机器人已确认天星宗弟子身份",
                    group="天星宗",
                    schedule_type="once",
                    actionable=False,
                )
                if sect_joined
                else time_command(
                    state,
                    "next_sect_join_time",
                    ".拜入宗门 天星宗",
                    "拜入天星宗",
                    waiting="叛宗冷却",
                    ready="待入宗",
                    missing="待确认",
                    group="天星宗",
                )
            ),
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(state, "next_node_search_time", NODE_SEARCH_COMMAND, "搜寻节点", waiting="12小时冷却", group="化神"),
            time_command(state, "next_small_world_time", ".小世界", "小世界", waiting="6小时冷却", group="化神"),
            manual_command(".显灵", "显灵", "凡人祈愿时自动响应", "化神"),
            small_world_calamity_command(state),
            time_command(state, "next_miracle_preach_time", ".神迹 布道", "神迹 布道", waiting="3小时冷却", group="化神"),
        ])
        rows.extend(sect_war_commands(state))
        rows.extend(meditation_commands(state))
        rows.append(manual_command(".安置侍妾", "安置侍妾", group="侍妾"))
        rows.extend(concubine_commands(state, include_divination=True, include_voyage=False))
        rows.extend(soul_curse_publisher_commands(state, account="waaiging", identity="主魂"))
    if "主魂" in miniapp_journey_identities_for_account(account):
        rows.append(miniapp_tianxing_journey_command(state))
    rows.append(mulan_support_daily_command(state))
    return {"identity": "主魂", "role": "主魂", "commands": rows}


def lingxiao_avatar_commands(name, state, root_state=None, account="main"):
    rows = []
    root_state = root_state if isinstance(root_state, dict) else {}
    sect_names = root_state.get("identity_sect_names") or {}
    avatar_sect = ""
    if isinstance(state, dict):
        avatar_sect = str(
            state.get("miniapp_sect_name") or state.get("sect_name") or ""
        ).strip()
    # 主号阴罗化身会在夺舍/重生后改道号（例如 缘生子 -> 玄续子）。
    # 前置链与接取链分别按候选名单和实时宗门展示，共用身份开关。
    is_yinluo = (
        name == YINLUO_IDENTITY
        or str(sect_names.get(name) or "").strip() == "阴罗宗"
        or avatar_sect == "阴罗宗"
    )
    has_publisher = not is_yinluo or soul_curse_yinluo_publisher_for_dashboard(account, name, root_state)
    rows.extend(global_sync_commands())
    rows.extend(meditation_commands(state, include_force_exit=(name == "素缘子")))
    if is_yinluo:
        rows.extend(yinluo_commands(state))
        rows.extend(soul_curse_assist_commands(
            state, account=account, identity=name, root_state=root_state, include_switch=not has_publisher
        ))
    if name == "无咎子":
        rows.extend([
            manual_command(".推命 闭关", "推命闭关", group="推命"),
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            time_command(
                state,
                "next_treasure_touch_time",
                ".抚摸法宝 风雷翅",
                "抚摸法宝",
                group="法宝",
            ),
            daily_done_command(
                state,
                ".观命",
                "观命",
                date_key="last_destiny_date",
                detail=f"上次定命：{state.get('last_destiny_choice') or '未记录'}",
                group="每日",
            ),
        ])
    if is_yinluo:
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
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
                ".改换星移 @Weeguu",
                "改换星移",
                waiting="已排程",
                ready="监听中",
                missing="监听中",
                group="星宫",
            ),
        ])
    rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled("main", name)))
    rows.append(mulan_support_daily_command(state))
    if has_publisher:
        rows.extend(soul_curse_publisher_commands(state, account="main", identity=name, root_state=root_state))
    return rows


def star_avatar_commands(name, state, root_state=None, account="sub"):
    rows = []
    rows.extend(global_sync_commands())
    root_state = root_state if isinstance(root_state, dict) else {}
    sect_names = root_state.get("identity_sect_names") or {}
    is_yinluo = (
        name == current_sub_yinluo_identity(root_state)
        or str(sect_names.get(name) or "").strip() == "阴罗宗"
    )
    has_publisher = not is_yinluo or soul_curse_yinluo_publisher_for_dashboard(account, name, root_state)
    if is_yinluo:
        rows.extend(yinluo_commands(state))
        rows.extend(soul_curse_assist_commands(
            state, account=account, identity=name, root_state=root_state, include_switch=not has_publisher
        ))
    if is_yinluo or name == "寻真子" or name == stable_sub_avatar_identity(root_state, "寻真子"):
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
        ])
    rows.extend(meditation_commands(state, include_force_exit=not is_yinluo))
    if name in SUB_STAR_PALACE_AVATARS:
        rows.extend(xiaohao_star_attraction_commands(state))
        rows.extend([
            time_command(state, "next_formation_time", ".启阵", "启阵", group="阵法"),
            time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
            time_command(state, "pending_star_gazing_target_time", ".观星", "待观星", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
            time_command(state, "pending_star_shift_target_time", ".改换星移 @Gamling33", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        ])
    rows.append(mulan_support_daily_command(state))
    rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled("sub", name)))
    if has_publisher:
        rows.extend(soul_curse_publisher_commands(state, account="sub", identity=name, root_state=root_state))
    return rows


def xiaohao_avatar_commands(name, state, root_state=None):
    rows = []
    rows.extend(global_sync_commands())
    root_state = root_state if isinstance(root_state, dict) else {}
    sect_names = root_state.get("identity_sect_names") or {}
    sect = str(sect_names.get(name) or state.get("sect_name") or "").strip()
    # Legacy state files may predate sect snapshots; retain the historical
    # default only for the still-present key.  Once the stable player ID has
    # migrated to a new Dao name, the renamed panel uses its explicit sect.
    is_taiyi = sect == "太一门" or (name == "缘生子" and not sect)
    is_yinluo = sect == "阴罗宗"
    is_star_palace = sect == "星宫"
    rows.extend(meditation_commands(state, include_force_exit=not is_yinluo))
    if is_yinluo:
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
        ])
    if is_taiyi:
        rows.extend([
            time_command(state, "next_yuanying_out_time", YUANYING_OUT_COMMAND, "元婴出窍", group="通用"),
            time_command(state, "next_rift_search_time", RIFT_SEARCH_COMMAND, "探寻裂缝", group="通用"),
            taiyi_guide_command(state),
        ])
    if name == "问心子":
        rows.extend([
            time_command(state, "nine_heaven_wind_cd_time", ".引九天罡风", "引九天罡风", group="天阶"),
            time_command(state, "next_heart_time", ".问心台", "问心台", group="天阶"),
            manual_command(".天阶状态", "天阶状态", "查询天阶状态", "天阶"),
            time_command(state, "next_stairs_time", ".登天阶", "登天阶", group="天阶"),
        ])
    elif is_star_palace:
        rows.extend([
            time_command(state, "next_formation_time", ".助阵", "助阵", group="阵法"),
        ])
        rows.extend(xiaohao_star_attraction_commands(state))
        rows.extend([
            time_command(state, "next_star_gazing_time", ".观星", "观星", group="星宫"),
            time_command(state, "pending_star_shift_target_time", ".改换星移 @TitanCreeper", "改换星移", waiting="已排程", ready="监听中", missing="监听中", group="星宫"),
        ])
    rows.extend(concubine_commands(state, include_divination=True, include_voyage=concubine_voyage_enabled("xiaohao", name)))
    rows.append(mulan_support_daily_command(state))
    rows.extend(soul_curse_publisher_commands(state, account="xiaohao", identity=name))
    return rows


def avatar_commands(account, name, state, root_state=None):
    if account == "main":
        rows = lingxiao_avatar_commands(name, state, root_state=root_state, account=account)
    elif account == "sub":
        rows = star_avatar_commands(name, state, root_state=root_state, account=account)
    elif account == "xiaohao":
        rows = xiaohao_avatar_commands(name, state, root_state=root_state)
    else:
        rows = global_sync_commands()
    if name in miniapp_journey_identities_for_account(account):
        rows.append(miniapp_tianxing_journey_command(state))
    return rows


def current_sect_commands(account, identity, state, root_state):
    sect = str((root_state.get("identity_sect_names") or {}).get(identity)
               or state.get("sect_name") or state.get("miniapp_sect_name") or "").strip()
    if sect == "太一门":
        return [taiyi_guide_command(state)]
    if sect == "元婴宗":
        return [time_command(state, "next_ask_dao_time", ".问道", "问道",
                             ready="可问道", missing="可问道", group=sect)]
    if sect == "凌霄宫":
        return [
            time_command(state, "next_stairs_time", ".登天阶", "登天阶", group=sect),
            time_command(state, "nine_heaven_wind_cd_time", ".引九天罡风", "引九天罡风", group=sect),
            time_command(state, "next_heart_time", ".问心台", "问心台", group=sect,
                         detail="优先在 8–11 阶使用；23:50 起每日兜底"),
            flow_command(".天阶状态", "天阶状态", "首次或缓存缺失时同步", sect),
        ]
    if sect == "阴罗宗":
        rows = yinluo_commands(state)
        has_publisher = soul_curse_yinluo_publisher_for_dashboard(account, identity, root_state)
        if has_publisher:
            rows.extend(soul_curse_publisher_commands(state, account=account, identity=identity, root_state=root_state))
        rows.extend(soul_curse_assist_commands(
            state, account=account, identity=identity, root_state=root_state, include_switch=not has_publisher
        ))
        if not any(row.get("dashboard_action") == "soul-curse-toggle" for row in rows):
            rows.insert(0, soul_curse_switch_row(identity, True, group=sect, account=account))
        for row in rows:
            if row.get("dashboard_action") == "soul-curse-toggle":
                row["detail"] = (
                    "探望→推演→护持→发布；同时接取其他身份的委托，执行辨认→借幡→剥离"
                    if has_publisher else "阴罗宗解咒：接取→辨认→借幡→剥离；保留原有身份开关"
                )
        return rows
    if sect == "天星宗":
        return [
            daily_done_command(state, ".观命", "观命", date_key="last_destiny_observation_date",
                               detail="每日观命，按后续动作选择命星", group=sect),
            flow_command(".定命 <命星>", "定命", "闭关、游历或炼器前按命星候选自动选择", sect),
            flow_command(".推命 闭关", "推命闭关", "服从当前闭关模式与指令开关", sect),
            flow_command(".推命 炼制", "刷天机值", "服从自动化设置中的身份选择、目标次数和开关", sect),
            flow_command(".改命 探索", "改命探索", "服从野外历练参与设置", sect),
        ]
    if sect == "万灵宗":
        contract = miniapp_beast_contract_command(state)
        contract["actionable"] = True
        return [
            time_command(state, "beast_seek_miniapp_next_time", ".寻觅灵兽", "寻觅灵兽", group=sect),
            contract, miniapp_beast_abyss_command(state),
            flow_command("miniapp:spirit-beast-rest", "灵兽休息", "探渊等流程按灵兽状态处理", sect),
        ]
    if sect == "合欢宗":
        entry = command_control_entry(load_command_controls(), account, identity, DUAL_CULTIVATION_COMMAND, root_state)
        target = (entry.get("target_username") if isinstance(entry, dict) and "target_username" in entry
                  else dual_cultivation_default_target(account, identity, state))
        target = str(target or "").strip().lstrip("@")
        row = time_command(state, "next_dual_cultivation_time", DUAL_CULTIVATION_COMMAND, "温养双修",
                           waiting="1小时冷却", ready="可双修", missing="可双修", group=sect,
                           detail=f"双修对象：@{target}" if target else "请配置双修对象")
        row["dual_cultivation_target"] = target
        if not target:
            row.update(status="待配置对象", tone="pending", next_seconds=None)
        return [row]
    return []


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
        identity = panel["identity"]
        identity_state = state if identity == "主魂" else avatars.get(identity, {}) or {}
        sect = str((state.get("identity_sect_names") or {}).get(identity)
                   or identity_state.get("sect_name") or "").strip()
        panel["commands"] = [row for row in panel["commands"]
                             if (not command_sect(row.get("command"))
                                 or (command_sect(row.get("command")) == "星宫" and sect == "星宫"))
                             and row.get("group") != "阴罗宗"
                             and not (sect == "阴罗宗" and row.get("group") == "南宫婉")]
        panel["commands"].extend(current_sect_commands(account, identity, identity_state, state))
        append_custom_commands(account, panel, custom_commands, root_state=state)
        panel = apply_command_execution_channels(panel, root_state=state)
        panel = apply_command_controls(account, panel, root_state=state)
        panel = apply_identity_pause(panel, state)
        panel = apply_command_classifications(panel)
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
    if name == 'waaiging': return 'cultivator_waaiging.log'
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
    return re.sub(r"^\[TG [^\]]+\]\s*", "", (match.group(1) if match else "").strip())


def telegram_log_metadata(entry):
    """Read only our header marker, never sender-supplied message body text."""
    match = re.search(
        r"\bIN \[[^\]]+\] \[TG sender=(bot|human|unknown) chat=(-?\d+|None) "
        r"msg=(\d+|None) attention=([a-z,-]+)\] ", log_entry_header(entry),
    )
    if not match:
        return {}
    return {"sender": match[1], "chat": match[2], "msg": match[3],
            "relations": set(match[4].split(",")) - {"-"}}


def log_sender_kind(entry):
    if not is_incoming_log_entry(entry) or is_miniapp_transport_log_entry(entry):
        return ""
    metadata = telegram_log_metadata(entry)
    if metadata:
        return metadata["sender"]
    # Only known historical bot signatures are recoverable; absence is unknown.
    sender = incoming_log_sender_text(log_entry_header(entry)).casefold()
    return "bot" if any(marker.casefold() in sender for marker in BOT_REPLY_MARKERS) else "unknown"


def log_attention_relations(entry):
    metadata = telegram_log_metadata(entry)
    if metadata:
        return metadata["relations"]
    if not is_incoming_log_entry(entry):
        return set()
    label = incoming_log_label(log_entry_header(entry)).casefold()
    return {"mention"} if label.startswith("mention ") else set()

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
    metadata = telegram_log_metadata(entry)
    if metadata and metadata["sender"] != "bot":
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
    text = str(entry.get("text") or "\n".join(lines))
    if "Mini App fishing" in text or "灵溪垂钓汇总" in text:
        return {FISHING_LOG_TAG}
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
    for identity, names in account_profile_usernames(account).items():
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
    dynamic = set(ALL_AVATARS)
    try:
        dynamic.update(account_profile_usernames(entry.get("account") or "").keys())
    except Exception:
        pass
    return any(name in text for name in dynamic) or "[Avatar:" in text

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
        elif (
            last_command_tag
            and last_command_tag != FISHING_LOG_TAG
            and can_inherit_related_command(entry)
        ):
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

def read_recent_log_entries(name, before_byte=None, limit=80, entry_filter=None):
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
    decorated = []
    matched = []
    start_byte = end_byte
    try:
        with open(path, "rb") as f:
            while True:
                start_byte = max(0, end_byte - read_bytes)
                f.seek(start_byte)
                raw = f.read(end_byte - start_byte)
                parsed = log_entries_from_bytes(raw, start_byte, end_byte, trim_start=start_byte > 0)
                decorated = decorate_log_entries(parsed)
                matched = [entry for entry in decorated if entry_filter(entry)] if entry_filter else decorated
                if len(matched) >= limit + 1 or start_byte == 0 or read_bytes >= min(LOG_TAIL_MAX_BYTES, end_byte):
                    break
                read_bytes = min(read_bytes * 2, end_byte, LOG_TAIL_MAX_BYTES)
    except Exception:
        return [], "无法读取日志内容。", {"log_size": file_size, "partial": True}

    page = matched[-limit:]
    if page:
        next_before = page[0].get("start_byte")
        has_more = bool(len(matched) > limit or (start_byte > 0 and next_before and next_before > start_byte))
    else:
        next_before = start_byte if start_byte > 0 else None
        has_more = bool(next_before)
    meta = {
        "log_size": file_size,
        "partial": True,
        "cursor_mode": "byte",
        "has_more": has_more,
        "next_before": next_before if has_more else None,
        "loaded_from_byte": page[0].get("start_byte") if page else end_byte,
        "loaded_to_byte": page[-1].get("end_byte") if page else end_byte,
    }
    return page, "", meta


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
# 周期收益日志
# =====================================================================

def ensure_daily_reward_events_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_reward_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_time TEXT NOT NULL,
            identity TEXT NOT NULL,
            command TEXT NOT NULL,
            source TEXT,
            outcome TEXT,
            final INTEGER NOT NULL DEFAULT 0,
            rewards_json TEXT,
            reward_summary TEXT,
            excerpt TEXT,
            clean TEXT,
            text_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account, event_key)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_reward_events_account_date ON daily_reward_events(account, event_date, event_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_reward_events_identity ON daily_reward_events(account, identity, event_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_reward_events_command ON daily_reward_events(account, command, event_date)")


def compact_reward_summary_from_json(rewards):
    return compact_reward_summary(rewards)


def build_daily_reward_log(date="", account="", identity="", command="", limit=300):
    path = message_events_db_path()
    query_date = str(date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    account = str(account or "").strip()
    identity = str(identity or "").strip()
    command = str(command or "").strip()
    try:
        limit = max(1, min(int(limit or 300), 1000))
    except Exception:
        limit = 300

    payload = {
        "date": query_date,
        "account": account,
        "identity": identity,
        "command": command,
        "rows": [],
        "filters": {"accounts": [], "identities": [], "commands": []},
        "summary": {"count": 0, "outcomes": {}, "rewards": {}, "updated_at": datetime.now().strftime(TIME_FORMAT)},
        "error": "",
    }
    if account and account not in WINDOW_MAP:
        payload["error"] = "未知账号"
        return payload

    conn = None
    try:
        conn = sqlite3.connect(path, timeout=2)
        conn.row_factory = sqlite3.Row
        ensure_daily_reward_events_schema(conn)
        conn.commit()

        filter_where = ["event_date=?"]
        filter_args = [query_date]
        if account:
            filter_where.append("account=?")
            filter_args.append(account)
        filter_sql = " AND ".join(filter_where)
        account_rows = conn.execute(
            f"""
            SELECT DISTINCT account
            FROM daily_reward_events
            WHERE {filter_sql}
            ORDER BY account
            """,
            filter_args,
        ).fetchall()
        identity_rows = conn.execute(
            f"""
            SELECT DISTINCT account, identity
            FROM daily_reward_events
            WHERE {filter_sql}
            ORDER BY account, CASE WHEN identity='主魂' THEN 0 ELSE 1 END, identity
            """,
            filter_args,
        ).fetchall()
        command_rows = conn.execute(
            f"""
            SELECT DISTINCT command
            FROM daily_reward_events
            WHERE {filter_sql}
            ORDER BY command
            """,
            filter_args,
        ).fetchall()
        payload["filters"] = {
            "accounts": [
                {"account": row["account"], "name": ACCOUNT_DISPLAY_NAMES.get(row["account"], row["account"])}
                for row in account_rows
            ],
            "identities": [
                {
                    "account": row["account"],
                    "account_name": ACCOUNT_DISPLAY_NAMES.get(row["account"], row["account"]),
                    "identity": row["identity"] or "主魂",
                }
                for row in identity_rows
            ],
            "commands": [row["command"] for row in command_rows],
        }

        where = ["event_date=?"]
        args = [query_date]
        if account:
            where.append("account=?")
            args.append(account)
        if identity:
            where.append("identity=?")
            args.append(identity)
        if command:
            where.append("command=?")
            args.append(command)
        sql = " AND ".join(where)
        rows = conn.execute(
            f"""
            SELECT *
            FROM daily_reward_events
            WHERE {sql}
            ORDER BY event_time DESC, id DESC
            LIMIT ?
            """,
            args + [limit],
        ).fetchall()
    except Exception as exc:
        payload["error"] = f"读取周期收益日志失败: {exc}"
        rows = []
    finally:
        if conn is not None:
            conn.close()

    summary_rewards = {}
    outcomes = {}
    result_rows = []
    for item in rows:
        command_text = str(item["command"] or "")
        command_root = reward_command_root(command_text)
        raw_text = str(item["clean"] or item["excerpt"] or "")
        try:
            stored_rewards = json.loads(item["rewards_json"] or "{}")
            if not isinstance(stored_rewards, dict):
                stored_rewards = {}
        except Exception:
            stored_rewards = {}
        if command_root == ".支援慕兰" and raw_text and not is_mulan_settlement_text(raw_text):
            # Historical rows sometimes persisted the short departure acknowledgement
            # ("正赶往天南边境") as if it were a reward settlement.
            continue
        reparsed_rewards = daily_reward_items_for_command(command_text, raw_text) if raw_text else {}
        trust_empty_reparse = trust_empty_reward_reparse(command_text, raw_text)
        rewards = reparsed_rewards if reparsed_rewards or trust_empty_reparse else normalize_reward_items(stored_rewards)
        for name, value in rewards.items():
            try:
                summary_rewards[name] = int(summary_rewards.get(name, 0) or 0) + int(value or 0)
            except Exception:
                continue
        outcome = str(item["outcome"] or "")
        if outcome:
            outcomes[outcome] = int(outcomes.get(outcome, 0) or 0) + 1
        result_rows.append({
            "id": item["id"],
            "account": item["account"],
            "account_name": ACCOUNT_DISPLAY_NAMES.get(item["account"], item["account"]),
            "date": item["event_date"],
            "time": item["event_time"],
            "identity": item["identity"] or "主魂",
            "username": command_record_username(item["account"], item["identity"] or "主魂"),
            "command": command_text,
            "source": item["source"] or "",
            "outcome": outcome,
            "final": bool(item["final"]),
            "rewards": rewards,
            "reward_summary": compact_reward_summary_from_json(rewards) or item["reward_summary"] or "",
            "excerpt": item["excerpt"] or "",
        })
    payload["rows"] = result_rows
    payload["summary"] = {
        "count": len(result_rows),
        "outcomes": outcomes,
        "rewards": summary_rewards,
        "reward_summary": compact_reward_summary_from_json(summary_rewards),
        "updated_at": datetime.now().strftime(TIME_FORMAT),
    }
    return payload


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


def is_miniapp_transport_log_entry(entry, direction=""):
    """Return whether an entry is a semantic Mini App request/response log."""
    header = log_entry_header(entry)
    marker = f"{str(direction or '').strip().upper()} [Mini App |"
    if marker.strip() == "[Mini App |":
        return "OUT [Mini App |" in header or "IN [Mini App |" in header
    return marker in header


def is_suppressed_miniapp_transport_log_entry(entry):
    """Hide routine polling and superseded per-item Mini App audit entries."""
    if not is_miniapp_transport_log_entry(entry):
        return False
    text = str(entry.get("text") or "")
    return any(marker in text for marker in (
        "同步洞府首页",
        "读取宗门灵圃",
        "万兽谷灵兽安抚（ID ",
        "洞府寻宝入府",
        "洞府寻宝探查第 ",
        "洞府寻宝见好就收",
    ))


def is_command_reply_log_entry(entry):
    """Keep concrete command replies while hiding duplicate mention/edit mirrors."""
    if is_miniapp_transport_log_entry(entry, "in"):
        return True
    if not is_incoming_log_entry(entry):
        return False
    label = incoming_log_label(log_entry_header(entry))
    if extract_command_from_line(label):
        return True
    label_lower = label.casefold()
    if label_lower.startswith("edited "):
        return True
    return label_lower.startswith("manual ") and " reply" in label_lower and "." in label


def is_dashboard_visible_log_entry(entry):
    """Show command traffic, mentions/replies to us, and error diagnostics."""
    header = log_entry_header(entry)
    text = str(entry.get("text") or "")
    if is_suppressed_miniapp_transport_log_entry(entry):
        return False
    if is_outgoing_log_entry(entry) or is_command_reply_log_entry(entry) or log_attention_relations(entry):
        return True
    if "刷天机值完成：总数" in text:
        return True
    return any(marker in header for marker in ("[ERROR]", "[CRITICAL]"))


def entry_matches_log_kind(entry, kind=""):
    kind = str(kind or "").strip().lower()
    if not kind:
        return True
    header = log_entry_header(entry)
    if kind == "out":
        return is_outgoing_log_entry(entry)
    if kind == "in":
        return is_command_reply_log_entry(entry) or bool(log_attention_relations(entry))
    if kind in {"attention", "mention", "reply"}:
        relations = log_attention_relations(entry)
        return bool(relations) if kind == "attention" else kind in relations
    if kind == "warn":
        return "[WARNING]" in header
    if kind == "error":
        return "[ERROR]" in header or "[CRITICAL]" in header
    if kind == "issue":
        return any(marker in header for marker in ("[ERROR]", "[CRITICAL]"))
    return True

def filter_log_entries(entries, tag="", q="", kind="", sender=""):
    """按 Dashboard 可见范围、标签和关键词过滤日志条目"""
    tag = (tag or "").strip(); q = (q or "").strip().lower()
    filtered = []
    for entry in entries:
        if not is_dashboard_visible_log_entry(entry):
            continue
        if kind and not entry_matches_log_kind(entry, kind):
            continue
        if sender and log_sender_kind(entry) != sender:
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
    entries = [entry for entry in entries if is_dashboard_visible_log_entry(entry)]
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

def get_log_page(name, before=None, limit=80, tag="", q="", kind="", sender=""):
    """获取分页的日志内容"""
    limit = max(20, min(int(limit or 80), 200))
    kind = (kind or "").strip().lower()
    if kind not in {"", "out", "in", "warn", "error", "issue", "attention", "mention", "reply"}:
        kind = ""
    sender = (sender or "").strip().lower()
    if sender not in {"", "bot", "human", "unknown"}:
        sender = ""
    if not (tag or q or kind or sender):
        entries, error, meta = read_recent_log_entries(
            name,
            before_byte=before,
            limit=limit,
            entry_filter=is_dashboard_visible_log_entry,
        )
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
            "sender": sender,
            "partial": True,
            "cursor_mode": meta.get("cursor_mode", "byte"),
            "log_size": meta.get("log_size", 0),
        }

    entries, error = read_log_entries(name)
    if error:
        return {"content": error, "start": 0, "end": 0, "total": 0, "matched": 0, "has_more": False, "next_before": None}
    filtered = filter_log_entries(entries, tag=tag, q=q, kind=kind, sender=sender)
    total = len(entries); matched = len(filtered)
    end = matched if before is None else max(0, min(int(before), matched))
    start = max(0, end - limit)
    page_entries = filtered[start:end]
    return {"content": "\n\n".join(entry["text"] for entry in page_entries),
            "entries": [entry["text"] for entry in page_entries],
            "start": start, "end": end, "total": total, "matched": matched,
            "has_more": start > 0, "next_before": start if start > 0 else None, "tag": tag, "q": q, "kind": kind, "sender": sender}


# =====================================================================
# 进程管理
# =====================================================================

def account_process_command_matches(account, command):
    """Return whether one process command line belongs to an account."""
    clean = re.sub(r"\s+", " ", str(command or "")).strip()
    if not clean:
        return False
    for script, restricted_account in ACCOUNT_PROCESS_SIGNATURES.get(account, ()):
        if script not in clean:
            continue
        if not restricted_account:
            return True
        account_pattern = rf"(?:^|\s)--account(?:=|\s+){re.escape(restricted_account)}(?:\s|$)"
        if re.search(account_pattern, clean):
            return True
    return False


def get_process_status(account):
    """检测脚本进程是否在运行"""
    if not SCRIPT_MAP.get(account):
        return False
    try:
        if os.name == 'nt':
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -in @('python.exe','pythonw.exe') } | "
                    "Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                return False
            return any(account_process_command_matches(account, line) for line in result.stdout.splitlines())
        return bool(account_process_pids(account))
    except Exception:
        return False

def account_process_pids(account):
    """Return live python process ids for one account launch shape."""
    signatures = ACCOUNT_PROCESS_SIGNATURES.get(account, ())
    if not signatures:
        return []
    script_pattern = "|".join(sorted({re.escape(script) for script, _ in signatures}))
    result = subprocess.run(["pgrep", "-af", script_pattern], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    pids = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid_text, cmd = parts
        if "python" not in cmd.lower() or "tmux " in cmd or not account_process_command_matches(account, cmd):
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
    return {
        "main": "天星宗（主号）",
        "sub": "元婴宗（副号）",
        "xiaohao": "万灵宗（小号）",
        "waaiging": "天星宗（@Waaiging）",
    }.get(account, account)

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

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 · 凡人修仙监控台</title>
    <style>
        :root { color-scheme: dark; --bg:#0b0e13; --panel:#151a22; --border:#303846; --text:#e7edf5; --muted:#95a1b2; --accent:#21b8d7; --danger:#ff7b7b; }
        * { box-sizing:border-box; }
        body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,"Segoe UI",sans-serif; letter-spacing:0; }
        main { width:min(100%,380px); }
        .brand { margin-bottom:24px; }
        h1 { margin:0 0 6px; font-size:24px; letter-spacing:0; }
        .subtitle { margin:0; color:var(--muted); font-size:14px; }
        form { border:1px solid var(--border); border-radius:8px; background:var(--panel); padding:22px; display:grid; gap:16px; }
        label { display:grid; gap:7px; color:var(--muted); font-size:13px; font-weight:600; }
        input { width:100%; height:42px; border:1px solid var(--border); border-radius:6px; background:#0d1117; color:var(--text); padding:0 12px; font:inherit; outline:none; }
        input:focus { border-color:var(--accent); box-shadow:0 0 0 3px rgba(33,184,215,.14); }
        button { height:42px; border:0; border-radius:6px; background:var(--accent); color:#061015; font:inherit; font-weight:800; cursor:pointer; }
        button:hover { filter:brightness(1.08); }
        .error { margin:0; color:var(--danger); font-size:13px; }
    </style>
</head>
<body>
<main>
    <div class="brand"><h1>凡人修仙监控台</h1><p class="subtitle">登录后将在此设备保持会话</p></div>
    <form id="login-form">
        <input type="hidden" name="next_path" value="__NEXT_PATH__">
        <label>访问密码<input name="access_token" type="password" autocomplete="current-password" required autofocus></label>
        <p class="error" id="login-error" role="alert">__ERROR__</p>
        <button type="submit">登录</button>
    </form>
</main>
<script>
    const form = document.getElementById('login-form');
    const error = document.getElementById('login-error');
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        button.disabled = true;
        error.textContent = '';
        const fields = new FormData(form);
        try {
            const response = await fetch('/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(Object.fromEntries(fields.entries())),
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const payload = await response.json().catch(() => ({}));
            error.textContent = payload.detail || '登录失败';
        } catch (_) {
            error.textContent = '暂时无法连接 Dashboard';
        } finally {
            button.disabled = false;
        }
    });
</script>
</body>
</html>"""


def _safe_next_path(value):
    value = str(value or "/").strip()
    if (
        not value.startswith("/") or value.startswith("//")
        or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/"
    return value


def dashboard_login_page(next_path="/", username="admin", error=""):
    return (
        LOGIN_PAGE.replace("__NEXT_PATH__", html.escape(_safe_next_path(next_path), quote=True))
        .replace("__USERNAME__", html.escape(str(username or "admin"), quote=True))
        .replace("__ERROR__", html.escape(str(error or "")))
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if dashboard_session_user(request.cookies.get(DASHBOARD_SESSION_COOKIE)):
        return RedirectResponse(_safe_next_path(next), status_code=303)
    return HTMLResponse(dashboard_login_page(next_path=next), headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login(request: Request, payload: dict = Body(...)):
    username = str(payload.get("username") or (USER_NAMES[0] if USER_NAMES else "admin"))
    password = str(payload.get("access_token") or payload.get("password") or "")
    next_path = str(payload.get("next_path") or "/")
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="Dashboard 认证尚未配置")
    if not _check_dashboard_credentials(request, username, password):
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    response = RedirectResponse(_safe_next_path(next_path), status_code=303)
    response.set_cookie(
        DASHBOARD_SESSION_COOKIE,
        create_dashboard_session(username),
        max_age=DASHBOARD_SESSION_DAYS * 86400,
        httponly=True,
        secure=DASHBOARD_COOKIE_SECURE,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")
    return response

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
            states = {}
            for key, info in ACCOUNT_DISPLAY_NAMES.items():
                state = get_state(key)
                states[key] = state
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
                    "profile_usernames": account_profile_usernames(key, state),
                }
            payload = {
                "accounts": result,
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "server_timezone": DASHBOARD_TIMEZONE,
                "runtime": dashboard_runtime_info(runtime_accounts),
                "fishing_auto": None if not FISHING_AUTOMATION_ENABLED else fishing_auto_dashboard_summary(states),
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


@app.get("/api/miniapp-inventory")
def miniapp_inventory(query: str = "", username: str = Depends(authenticate)):
    """Read the latest per-identity Mini App inventory caches."""
    return miniapp_inventory_dashboard_payload(query=query)


@app.post("/api/miniapp-inventory/non-tradable")
def update_miniapp_inventory_non_tradable(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Persist item names that should be excluded from inventory trade copies."""
    action = str(payload.get("action") or "").strip().lower()
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return {"success": False, "msg": "物品列表格式不正确"}
    items = {str(item).strip() for item in raw_items if str(item or "").strip()}
    if not items:
        return {"success": False, "msg": "请选择至少一个物品"}
    if action not in {"add", "remove"}:
        return {"success": False, "msg": "未知操作"}
    with MINIAPP_INVENTORY_NON_TRADABLE_LOCK:
        current = set(load_miniapp_inventory_non_tradable_items(CONFIG_DIR))
        if action == "add":
            current.update(items)
        else:
            current.difference_update(items)
        saved = save_miniapp_inventory_non_tradable_items(current, CONFIG_DIR)
    return {
        "success": True,
        "items": saved,
        "msg": "已移入不可交易表" if action == "add" else "已移出不可交易表",
    }


@app.post("/api/miniapp-inventory/refresh")
def refresh_miniapp_inventory(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Ask one or all account processes to refresh inventory through their live session."""
    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "").strip()
    account_identities = inventory_account_identities()
    if account == "all":
        if identity not in ("", "*"):
            return {"success": False, "msg": "全部账号刷新只支持全部身份"}
        with MINIAPP_INVENTORY_REQUEST_LOCK:
            requests = [
                write_inventory_request(
                    target_account,
                    "*",
                    requested_by=username,
                    base_dir=CONFIG_DIR,
                )
                for target_account in account_identities
            ]
        return {"success": True, "requests": requests, "msg": "已请求刷新全部身份"}
    if account not in account_identities:
        return {"success": False, "msg": "未知账号"}
    with MINIAPP_INVENTORY_REQUEST_LOCK:
        try:
            request = write_inventory_request(
                account,
                identity,
                requested_by=username,
                base_dir=CONFIG_DIR,
            )
        except ValueError:
            return {"success": False, "msg": "未知身份"}
    return {
        "success": True,
        "request": request,
        "msg": "已请求刷新全部身份" if identity == "*" else f"已请求刷新 {request['identity']}",
    }

@app.get("/api/command-catalog")
def command_catalog(username: str = Depends(authenticate)):
    return command_catalog_payload()


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

@app.get("/api/daily-rewards")
def daily_rewards(date: str = "", account: str = "", identity: str = "", command: str = "",
                  limit: int = 300, username: str = Depends(authenticate)):
    """获取周期收益结构化日志，支持按日期、账号、身份、指令筛选。"""
    try:
        safe_limit = max(1, min(int(limit or 300), 1000))
    except Exception:
        safe_limit = 300
    cache_key = json.dumps({
        "date": str(date or "").strip(),
        "account": str(account or "").strip(),
        "identity": str(identity or "").strip(),
        "command": str(command or "").strip(),
        "limit": safe_limit,
    }, sort_keys=True, ensure_ascii=False)
    now_ts = time.time()
    with DAILY_REWARD_ENDPOINT_LOCK:
        cached = DAILY_REWARD_ENDPOINT_CACHE.get(cache_key)
        if cached and now_ts - float(cached.get("at") or 0) < DAILY_REWARD_ENDPOINT_CACHE_SECONDS:
            return cached.get("data")
        payload = build_daily_reward_log(
            date=date,
            account=account,
            identity=identity,
            command=command,
            limit=safe_limit,
        )
        DAILY_REWARD_ENDPOINT_CACHE[cache_key] = {"at": now_ts, "data": payload}
        if len(DAILY_REWARD_ENDPOINT_CACHE) > 24:
            oldest_key = min(DAILY_REWARD_ENDPOINT_CACHE, key=lambda key: DAILY_REWARD_ENDPOINT_CACHE[key].get("at", 0))
            DAILY_REWARD_ENDPOINT_CACHE.pop(oldest_key, None)
        return payload


@app.get("/api/duels")
def duels(date: str = "", limit: int = 200, username: str = Depends(authenticate)):
    """Return shared duel queues, per-identity chances, and structured results."""
    try:
        safe_limit = max(1, min(int(limit or 200), 1000))
    except Exception:
        safe_limit = 200
    return duel_dashboard_payload(date=date, limit=safe_limit)


@app.get("/api/surprise-raids")
def surprise_raids(username: str = Depends(authenticate)):
    """Return the shared surprise-raid configuration and runtime state."""
    return surprise_raid_dashboard_payload()


@app.post("/api/surprise-raids")
async def surprise_raid_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Update surprise-raid participants, cadence, and enabled state."""
    try:
        state = set_surprise_raid_config(
            enabled=bool(payload.get("enabled")),
            interval_seconds=payload.get("interval_seconds"),
            raider_account=str(payload.get("raider_account") or ""),
            raider_identity=str(payload.get("raider_identity") or ""),
            target_account=str(payload.get("target_account") or ""),
            target_identity=str(payload.get("target_identity") or ""),
            updated_by=username,
        )
    except ValueError as exc:
        message = "循环间隔必须是 10 分钟到 7 天"
        if str(exc) != "invalid surprise raid interval":
            message = "奇袭夺宝设置无效"
        return {"success": False, "msg": message}
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "state": state,
        "updated_by": username,
    }


@app.get("/api/red-packets")
def red_packets(username: str = Depends(authenticate)):
    """Return shared red-packet settings and per-account listener status."""
    return red_packet_dashboard_payload()


@app.get("/api/automation-settings")
def automation_settings_dashboard(username: str = Depends(authenticate)):
    """Return shared Dashboard-controlled automation settings."""
    payload = automation_dashboard_payload()
    fishing = payload.get("miniapp_fishing") if isinstance(payload.get("miniapp_fishing"), dict) else {}
    try:
        fishing["runtime"] = miniapp_fishing_global_snapshot(
            (payload.get("settings") or {}).get("miniapp_fishing") or {}
        )
    except Exception:
        fishing["runtime"] = {
            "status": "unavailable",
            "detail": "共享垂钓状态暂时不可用",
            "participant_labels": [],
            "current_label": "",
            "account_current_labels": {},
            "account_schedules": [],
            "rod_holder_label": "",
            "transfer": {},
        }
    payload["miniapp_fishing"] = fishing
    try:
        payload["xuangu_quiz"]["runtime"] = quiz_dashboard_payload()
    except Exception:
        payload["xuangu_quiz"]["runtime"] = {"error": "暂时无法读取玄骨答题状态"}
    return payload


@app.post("/api/xuangu-quiz/answers")
def xuangu_quiz_answer_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Confirm an observed unknown answer for subsequent quiz events."""
    try:
        saved = confirm_quiz_answer(payload.get("question_key"), payload.get("answer"), username)
    except ValueError as exc:
        messages = {"invalid quiz question": "题目编号无效", "quiz question not found": "未找到待补题目",
                    "invalid quiz answer": "答案无效", "answer must be an observed option": "请从题目已有选项中选择答案"}
        return {"success": False, "msg": messages.get(str(exc), "答案保存失败")}
    return {"success": True, "answer": saved, "runtime": quiz_dashboard_payload()}


@app.get("/api/world-boss/turnstile")
def world_boss_turnstile_requests(
    include_finished: bool = False,
    username: str = Depends(authenticate),
):
    """Return pending browser-verification requests without token contents."""

    return {
        "success": True,
        "requests": list_world_boss_turnstile_requests(
            include_finished=bool(include_finished)
        ),
    }


@app.post("/api/world-boss/turnstile")
def world_boss_turnstile_submit(
    payload: dict = Body(...),
    username: str = Depends(authenticate),
):
    """Accept one browser token through the authenticated Dashboard only."""

    try:
        request = submit_world_boss_turnstile_token(
            payload.get("request_id"),
            payload.get("token"),
        )
    except TurnstileRequestError as exc:
        messages = {
            "turnstile_request_invalid": "验证请求无效",
            "turnstile_token_invalid": "Turnstile 令牌格式无效",
            "turnstile_request_not_found": "验证请求已不存在或已过期",
            "turnstile_request_expired": "验证请求已过期，请重新等待机器人提示",
            "turnstile_request_already_submitted": "该验证请求已经提交过令牌",
        }
        return {"success": False, "msg": messages.get(exc.code, "验证令牌提交失败")}
    return {"success": True, "request": request}


@app.post("/api/world-boss/turnstile/browser-event")
def world_boss_turnstile_browser_event(
    payload: dict = Body(...),
    username: str = Depends(authenticate),
):
    """Accept bounded widget diagnostics without tokens or arbitrary browser text."""
    try:
        request = record_world_boss_turnstile_browser_event(
            payload.get("request_id"), payload.get("event"), payload.get("error_code", ""),
        )
    except TurnstileRequestError as exc:
        return {"success": False, "code": exc.code, "msg": "验证状态未更新，请刷新请求列表"}
    return {"success": True, "request": request}


@app.post("/api/automation-settings")
async def automation_settings_control(
    payload: dict = Body(...),
    username: str = Depends(authenticate),
):
    participants = payload.get("world_boss_participants")
    mode = payload.get("mulan_support_mode")
    star_gazing = payload.get("star_gazing")
    star_gazing = star_gazing if isinstance(star_gazing, dict) else {}
    abyss = payload.get("miniapp_beast_abyss")
    abyss = abyss if isinstance(abyss, dict) else {}
    wind_thunder = payload.get("wind_thunder")
    wind_thunder = wind_thunder if isinstance(wind_thunder, dict) else {}
    quiz = payload.get("xuangu_quiz")
    quiz = quiz if isinstance(quiz, dict) else {}
    fishing = payload.get("miniapp_fishing")
    fishing = fishing if isinstance(fishing, dict) else {}
    journey = payload.get("miniapp_journey")
    journey = journey if isinstance(journey, dict) else {}
    trial = payload.get("miniapp_tianji_trial")
    trial = trial if isinstance(trial, dict) else {}
    fate_cards = payload.get("miniapp_fate_cards")
    fate_cards = fate_cards if isinstance(fate_cards, dict) else {}
    tianxing = payload.get("tianxing")
    tianxing = tianxing if isinstance(tianxing, dict) else {}
    meditation = payload.get("meditation")
    try:
        if meditation is not None and (
            not isinstance(meditation, dict) or not isinstance(meditation.get("identities"), dict)
        ):
            raise ValueError("meditation identities must be an object")
        with AUTOMATION_SETTINGS_LOCK:
            settings = save_automation_settings(
                world_boss_participants=participants,
                mulan_support_mode=mode,
                star_gazing_lead_seconds=star_gazing.get("lead_seconds"),
                miniapp_beast_abyss_power_min=abyss.get("power_min"),
                miniapp_beast_abyss_power_max=abyss.get("power_max"),
                wind_thunder_enabled=wind_thunder.get("enabled"),
                wind_thunder_participants=wind_thunder.get("participants"),
                xuangu_quiz_enabled=quiz.get("enabled"),
                xuangu_quiz_participants=quiz.get("participants"),
                miniapp_fishing_enabled=fishing.get("enabled"),
                miniapp_fishing_pond=fishing.get("pond"),
                miniapp_fishing_bait=fishing.get("bait"),
                miniapp_fishing_chum=fishing.get("chum"),
                miniapp_fishing_participants=fishing.get("participants"),
                miniapp_fishing_rod=fishing.get("rod"),
                miniapp_fishing_rod_owner=fishing.get("rod_owner"),
                miniapp_fishing_start_time=fishing.get("start_time"),
                miniapp_journey_enabled=journey.get("enabled"),
                miniapp_journey_participants=journey.get("participants"),
                miniapp_tianji_trial_enabled=trial.get("enabled"),
                miniapp_tianji_trial_participants=trial.get("participants"),
                miniapp_fate_cards_enabled=fate_cards.get("enabled"),
                miniapp_fate_cards_participants=fate_cards.get("participants"),
                tianxing_meditation_mode=tianxing.get("meditation_mode"),
                tianxing_use_heqi_pill=tianxing.get("use_heqi_pill"),
                tianxing_tianji_grind_enabled=tianxing.get("tianji_grind_enabled"),
                tianxing_tianji_grind_target=tianxing.get("tianji_grind_target"),
                tianxing_tianji_grind_participants=tianxing.get("tianji_grind_participants"),
                meditation_identities=meditation["identities"] if meditation is not None else None,
                updated_by=username,
            )
    except ValueError as exc:
        messages = {
            "meditation identities must be an object": "闭关身份设置格式错误",
            "invalid meditation identity": "闭关参与身份无效",
            "invalid meditation mode": "请选择深度闭关或日常闭关",
            "invalid meditation enabled": "闭关参与开关必须是启用或关闭",
            "invalid meditation use_heqi_pill": "合气丹选项必须是启用或关闭",
            "invalid Xuangu quiz enabled flag": "玄骨答题开关必须是启用或关闭",
            "Xuangu quiz participants must be a list": "玄骨答题参与身份列表格式错误",
            "invalid Xuangu quiz participant": "玄骨答题参与身份无效",
            "Xuangu quiz participants required": "启用玄骨答题时至少选择一个身份",
            "world boss participants must be a list": "Boss 参战身份列表格式错误",
            "invalid world boss participant": "Boss 参战身份无效",
            "multiple world boss identities per account": "每个账号最多选择一个 Boss 参战身份",
            "invalid Mulan support mode": "慕兰支援参数必须是斥候、破灯、奇袭或护阵",
            "invalid star gazing lead seconds": "观星提前量必须是 -120 至 120 秒的整数（负数表示显化后发送）",
            "invalid Mini App beast abyss power range": "万兽谷探渊战力区间必须是非负整数，且最大值不能小于最小值",
            "invalid Mini App fishing pond": "灵溪垂钓地点无效",
            "invalid Mini App fishing bait": "灵溪垂钓鱼饵无效",
            "invalid Mini App fishing chum": "灵溪垂钓窝料无效",
            "Mini App fishing participants must be a list": "灵溪垂钓参与身份列表格式错误",
            "invalid Mini App fishing participant": "灵溪垂钓参与身份无效",
            "Mini App fishing participants required": "启用灵溪垂钓时至少选择一个身份",
            "invalid Mini App fishing rod": "灵溪垂钓鱼竿无效",
            "invalid Mini App fishing rod owner": "手动指定的钓竿持有者无效",
            "invalid Mini App fishing start time": "灵溪垂钓开始时间必须是 HH:MM",
            "Mini App journey participants must be a list": "深入历练参与身份列表格式错误",
            "invalid Mini App journey participant": "深入历练参与身份无效",
            "Mini App journey participants required": "启用深入历练时至少选择一个身份",
            "Mini App Tianji trial participants must be a list": "天机试炼参与身份列表格式错误",
            "invalid Mini App Tianji trial participant": "天机试炼参与身份无效",
            "Mini App Tianji trial participants required": "启用天机试炼时至少选择一个身份",
            "Mini App Fate Cards participants must be a list": "天机命脉参与身份列表格式错误",
            "invalid Mini App Fate Cards participant": "天机命脉参与身份无效",
            "Mini App Fate Cards participants required": "启用天机命脉时至少选择一个身份",
            "Wind Thunder Wings participants must be a list": "风雷翅加速参与身份列表格式错误",
            "invalid Wind Thunder Wings participant": "风雷翅加速参与身份无效",
            "Wind Thunder Wings participants required": "启用风雷翅加速时至少选择一个身份",
            "Tianxing Tianji grind participants must be a list": "刷天机值参与身份列表格式错误",
            "invalid Tianxing Tianji grind participant": "刷天机值参与身份无效",
            "Tianxing Tianji grind participants required": "启用刷天机值时至少选择一个身份",
        }
        return {"success": False, "msg": messages.get(str(exc), "自动化设置无效")}
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    with COMMAND_RECORD_LOCK:
        COMMAND_RECORD_CACHE.clear()
    with COMMAND_RECORD_ENDPOINT_LOCK:
        COMMAND_RECORD_ENDPOINT_CACHE.clear()
    with DAILY_REWARD_ENDPOINT_LOCK:
        DAILY_REWARD_ENDPOINT_CACHE.clear()
    return {
        "success": True,
        "settings": settings,
        "updated_by": username,
    }


@app.post("/api/automation-settings/fishing-force-retry")
async def automation_fishing_force_retry(username: str = Depends(authenticate)):
    """Reset saved fishing completion/error gates and run each participant once."""
    settings = miniapp_fishing_settings()
    try:
        runtime = request_miniapp_fishing_force_retry(
            settings,
            requested_by=username,
        )
    except ValueError as exc:
        messages = {
            "Mini App fishing is disabled": "请先启用灵溪自动垂钓并保存设置",
            "Mini App fishing participants required": "请先选择垂钓身份并保存设置",
        }
        return {"success": False, "msg": messages.get(str(exc), "无法启动强制重试")}
    return {
        "success": True,
        "msg": "已忽略旧状态，所选身份将各强制重试一次",
        "runtime": runtime,
    }


@app.post("/api/red-packets")
async def red_packet_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Update red-packet accounts, amount, delay, and active schedule."""
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return {"success": False, "msg": "账号列表格式错误"}
    try:
        with RED_PACKET_CONTROL_LOCK:
            settings = save_red_packet_settings(
                enabled=bool(payload.get("enabled")),
                accounts=accounts,
                minimum_amount=payload.get("minimum_amount", "0"),
                delay_seconds=payload.get("delay_seconds", "0"),
                schedule_enabled=bool(payload.get("schedule_enabled")),
                schedule_start=payload.get("schedule_start", "00:00"),
                schedule_end=payload.get("schedule_end", "00:00"),
                updated_by=username,
            )
    except ValueError as exc:
        if str(exc) == "at least one account is required":
            message = "启用抢红包时至少选择一个账号"
        elif str(exc) == "invalid delay seconds":
            message = "领取延迟必须是 0 到 300 秒之间的有效数字"
        elif str(exc) == "invalid schedule time":
            message = "生效时段必须是有效的 24 小时时间"
        else:
            message = "最低金额必须是有效的非负数"
        return {"success": False, "msg": message}
    return {"success": True, "settings": settings, "updated_by": username}


@app.post("/api/duels/control")
async def duel_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Update duel automation, an identity, the duel method, or the Titan beast mode."""
    queue_key = str(payload.get("queue") or "").strip().lower()
    participant_key = str(payload.get("participant") or "").strip()
    beast_mode = payload.get("beast_mode")
    target_switch = payload.get("target_switch")
    enabled = bool(payload.get("enabled"))
    try:
        if beast_mode is not None:
            data = set_titan_beast_mode(beast_mode)
        elif target_switch is not None:
            data = set_duel_target_switch(bool(target_switch))
        elif participant_key:
            data = set_duel_participant_control(
                enabled,
                participant_key,
                payload.get("target"),
            )
        else:
            data = set_duel_control(enabled, queue_key=queue_key)
    except ValueError as exc:
        message = str(exc)
        if message == "unknown duel participant":
            message = "未知斗法身份"
        elif message == "invalid duel target username":
            message = "挑战对象必须是有效的 Telegram 用户名"
        elif message == "invalid titan beast mode":
            message = "小号主魂灵兽状态仅支持出战"
        elif message == "same account duel target":
            message = "同一账号内无法同时保持挑战身份和目标身份激活"
        else:
            message = "未知斗法队列"
        return {"success": False, "msg": message}
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    participant_state = None
    if participant_key:
        for queue in data.get("queues", {}).values():
            if participant_key in (queue.get("participants") or {}):
                participant_state = queue["participants"][participant_key]
                break
    return {
        "success": True,
        "enabled": bool(data.get("enabled")),
        "target_switch_enabled": bool(data.get("target_switch_enabled", True)),
        "queue": queue_key,
        "participant": participant_key,
        "participant_enabled": bool(participant_state.get("enabled")) if participant_state else None,
        "target": participant_state.get("target_username", "") if participant_state else None,
        "beast_mode": data.get("queues", {}).get(DUEL_ROTATION_QUEUE_KEY, {}).get("beast_mode"),
        "target_status": titan_target_status(
            data.get("queues", {}).get(DUEL_ROTATION_QUEUE_KEY, {}).get("beast_mode")
        ) if beast_mode is not None else None,
        "queue_enabled": (
            bool(data.get("queues", {}).get(queue_key, {}).get("enabled"))
            if queue_key else None
        ),
        "updated_by": username,
    }


@app.post("/api/duels/settings")
async def duel_settings_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Update the global queue cadence and same-target duel interval."""
    try:
        data = set_duel_intervals(
            payload.get("interval_seconds"),
            payload.get("target_interval_seconds"),
        )
    except ValueError:
        return {
            "success": False,
            "msg": "斗法间隔必须是 1 秒到 7 天之间的有效数字",
        }
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "interval_seconds": int(data.get("interval_seconds") or 0),
        "target_interval_seconds": int(data.get("target_interval_seconds") or 0),
        "updated_by": username,
    }


@app.post("/api/duels/multi")
async def duel_multi_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Create, replace, pause, or resume the active one-to-many duel plan."""
    try:
        if "targets" in payload:
            multi_target_switch = payload.get("target_switch_enabled")
            if multi_target_switch is None and "duel_method" in payload:
                legacy_method = str(payload.get("duel_method") or "").strip().lower()
                # Older Dashboard clients called the direct username mode
                # ``username`` and the reply/switch mode ``reply_switch``.
                # Accept both names while retaining switch as the safe default
                # for unknown values.
                multi_target_switch = legacy_method not in {"direct", "username"}
            data = configure_duel_multi_plan(
                payload.get("initiator_account"),
                payload.get("initiator_identity"),
                payload.get("targets"),
                enabled=bool(payload.get("enabled", True)),
                target_switch_enabled=multi_target_switch,
                schedule=payload.get("schedule"),
            )
        elif "schedule" in payload:
            data = set_duel_multi_schedule(payload["schedule"])
        else:
            data = set_duel_multi_control(bool(payload.get("enabled")))
    except ValueError as exc:
        messages = {
            "unknown duel initiator": "请选择有效的斗法发起身份",
            "duel targets required": "请至少添加一个斗法对象",
            "too many duel targets": "斗法对象最多 20 个",
            "invalid duel target": "斗法对象格式错误",
            "invalid duel target username": "斗法对象必须是有效的 Telegram 用户名",
            "duplicate duel target": "同一个斗法对象不能重复添加",
            "invalid duel count": "每个对象的斗法次数必须是 1 到 999",
            "duel target matches initiator": "不能把发起身份自己设为斗法对象",
            "same account duel target": "同一账号内无法同时保持发起身份和目标分身激活",
            "duel multi plan is not configured": "请先保存一对多斗法计划",
            "invalid duel schedule": "每日斗法定时格式错误",
            "invalid duel schedule time": "请选择有效的开始和结束时间（HH:MM）",
            "equal duel schedule times": "开始和结束时间不能相同；全天运行请关闭每日定时",
        }
        return {"success": False, "msg": messages.get(str(exc), str(exc))}
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    multi = data.get("multi") or {}
    multi_target_switch_enabled_value = duel_multi_target_switch_enabled(
        multi, fallback=bool(data.get("target_switch_enabled", True))
    )
    return {
        "success": True,
        "enabled": bool(multi.get("enabled")),
        "initiator_account": multi.get("initiator_account") or "",
        "initiator_identity": multi.get("initiator_identity") or "",
        "target_switch_enabled": multi_target_switch_enabled_value,
        "duel_method": "reply_switch" if multi_target_switch_enabled_value else "username",
        "schedule": duel_multi_schedule_status(multi),
        "target_count": len(multi.get("targets") or []),
        "updated_by": username,
    }

@app.get("/api/logs/{name}")
def logs(name: str, before: Optional[int] = None, limit: int = 80, tag: str = "", q: str = "", kind: str = "",
         sender: str = "",
         username: str = Depends(authenticate)):
    """获取账号的分页日志"""
    cache_key = json.dumps({
        "name": name,
        "before": before,
        "limit": max(1, min(int(limit or 80), 300)),
        "tag": tag or "",
        "q": q or "",
        "kind": kind or "",
        "sender": sender or "",
    }, sort_keys=True, ensure_ascii=False)
    now_ts = time.time()
    with LOG_PAGE_LOCK:
        cached = LOG_PAGE_CACHE.get(cache_key)
        if cached and now_ts - float(cached.get("at") or 0) < LOG_PAGE_CACHE_SECONDS:
            return cached.get("data")
        payload = get_log_page(name, before=before, limit=limit, tag=tag, q=q, kind=kind, sender=sender)
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


@app.post("/api/beast-miniapp/refresh")
async def refresh_beast_miniapp(payload: dict = Body(default={}), username: str = Depends(authenticate)):
    """Ask the running main account to refresh its Mini App beast roster."""
    account = str(payload.get("account") or "main").strip()
    if account != "main":
        return {"success": False, "msg": "当前仅主号配置了万兽谷固定入口"}
    request = write_refresh_request(CONFIG_DIR, requested_by=username)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "msg": "已提交万兽谷同步，主号将在 30 秒内处理",
        "request_id": request.get("request_id"),
        "requested_at": request.get("requested_at"),
    }


@app.post("/api/soul-curse-control")
async def set_soul_curse_control(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Dashboard 一键开关某账号某身份的封魂咒链路（写 soul_curse_settings.json，热生效）。"""
    import json as _json

    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "主魂").strip() or "主魂"
    enabled = bool(payload.get("enabled"))
    if account not in WINDOW_MAP:
        return {"success": False, "msg": "未知账号"}

    path = SOUL_CURSE_SETTINGS_FILE
    try:
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("enabled", True)
    identities = data.get("identities")
    if not isinstance(identities, dict):
        identities = {}
        data["identities"] = identities
    account_map = identities.get(account)
    if not isinstance(account_map, dict):
        account_map = {}
        identities[account] = account_map
    account_map[identity] = enabled
    data["updated_at"] = datetime.now().strftime(TIME_FORMAT)
    data["updated_by"] = username
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        _json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp, path)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "account": account,
        "identity": identity,
        "enabled": enabled,
        "msg": f"封魂咒链路 [{account}/{identity}] 已{'启用' if enabled else '关闭'}，下一轮检测即生效",
    }


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
    root_state = get_state(account)
    if not FISHING_AUTOMATION_ENABLED and (
        command in FISHING_AUTO_CONTROL_COMMANDS
        or command in FISHING_CONTROL_COMMANDS
        or control_key in FISHING_AUTO_CONTROL_COMMANDS
        or control_key in FISHING_CONTROL_COMMANDS
    ):
        return {"success": False, "msg": "自动钓鱼已停用"}

    with COMMAND_CONTROL_LOCK:
        data = load_command_controls()
        if command in FISHING_AUTO_CONTROL_COMMANDS:
            bait = str(payload.get("bait") or "").strip()
            if bait not in FISHING_CONTROL_BAITS:
                for auto_account in WINDOW_MAP:
                    old_entry = (
                        data.get(auto_account, {})
                        .get("主魂", {})
                        .get(FISHING_AUTO_CONTROL_COMMAND, {})
                    )
                    if isinstance(old_entry, dict) and old_entry.get("bait") in FISHING_CONTROL_BAITS:
                        bait = old_entry.get("bait")
                        break
            if bait not in FISHING_CONTROL_BAITS:
                bait = FISHING_BAIT
            for auto_account in WINDOW_MAP:
                account_controls = data.setdefault(auto_account, {})
                identity_controls = account_controls.setdefault("主魂", {})
                identity_controls[FISHING_AUTO_CONTROL_COMMAND] = {
                    "disabled": disabled,
                    "command": FISHING_AUTO_CONTROL_COMMAND,
                    "label": "全自动钓鱼",
                    "bait": bait,
                    "updated_at": datetime.now().strftime(TIME_FORMAT),
                    "updated_by": username,
                }
            save_command_controls(data)
            with STATUS_LOCK:
                STATUS_CACHE.clear()
            return {
                "success": True,
                "account": account,
                "identity": identity,
                "control_key": FISHING_AUTO_CONTROL_COMMAND,
                "disabled": disabled,
                "bait": bait,
                "global": True,
            }
        account_controls = data.setdefault(account, {})
        identity_controls = account_controls.setdefault(identity, {})
        old_entry = command_control_entry(data, account, identity, control_key, root_state)
        stored_entry = dict(old_entry) if isinstance(old_entry, dict) else {}
        persist_entry = (control_key in {BEAST_BORDER_PATROL_CONTROL_KEY, DUAL_CULTIVATION_COMMAND,
                                        SMALL_WORLD_MIRACLE_CONTROL_KEY}
                         or len(command_control_identity_candidates(identity, root_state)) > 1)
        if disabled or default_paused or persist_entry:
            stored_entry.update({
                "disabled": disabled,
                "command": command,
                "label": label,
                "updated_at": datetime.now().strftime(TIME_FORMAT),
                "updated_by": username,
            })
            if control_key == BEAST_BORDER_PATROL_CONTROL_KEY:
                stored_entry["patrol_mode"] = normalize_beast_border_patrol_mode(
                    payload.get("patrol_mode") or stored_entry.get("patrol_mode")
                )
            identity_controls[control_key] = stored_entry
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


@app.post("/api/small-world-miracle-mode")
async def set_small_world_miracle_mode(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Select a miracle without changing the account's pause or shared cooldown."""
    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "主魂").strip() or "主魂"
    mode = str(payload.get("mode") or "").strip()
    if account not in {"main", "waaiging"}:
        return {"success": False, "msg": "该账号没有自动小世界神迹"}
    if identity != "主魂":
        return {"success": False, "msg": "小世界神迹仅由主魂执行"}
    if mode not in SMALL_WORLD_MIRACLE_ACTIONS:
        return {"success": False, "msg": "神迹方式必须是布道或赈灾"}
    with COMMAND_CONTROL_LOCK:
        data = load_command_controls()
        controls = data.setdefault(account, {}).setdefault(identity, {})
        old = controls.get(SMALL_WORLD_MIRACLE_CONTROL_KEY, {})
        entry = dict(old) if isinstance(old, dict) else {"disabled": bool(old)}
        entry.setdefault("disabled", False)
        entry.update(command=small_world_miracle_command(mode), label=f"神迹 {mode}", miracle_mode=mode,
                     updated_at=datetime.now().strftime(TIME_FORMAT), updated_by=username)
        controls[SMALL_WORLD_MIRACLE_CONTROL_KEY] = entry
        save_command_controls(data)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {"success": True, "account": account, "identity": identity, "mode": mode}


@app.post("/api/dual-cultivation-target")
async def set_dual_cultivation_target(payload: dict = Body(...), username: str = Depends(authenticate)):
    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "主魂").strip() or "主魂"
    if account not in WINDOW_MAP:
        return {"success": False, "msg": "未知账号"}
    state = get_state(account)
    if identity != "主魂" and identity not in (state.get("avatars") or {}):
        return {"success": False, "msg": "身份已变化，请刷新后重试"}
    try:
        target = normalize_dual_cultivation_target(payload.get("target_username"))
    except ValueError as exc:
        return {"success": False, "msg": str(exc)}
    own_names = account_profile_usernames(account, state).get("主魂") or []
    if target and identity == "主魂" and target.casefold() in {str(name).lstrip("@").casefold() for name in own_names}:
        return {"success": False, "msg": "双修对象不能是当前主魂本人"}
    with COMMAND_CONTROL_LOCK:
        data = load_command_controls()
        controls = data.setdefault(account, {}).setdefault(identity, {})
        old = command_control_entry(data, account, identity, DUAL_CULTIVATION_COMMAND, state)
        entry = dict(old) if isinstance(old, dict) else {"disabled": bool(old)}
        entry.update(command=DUAL_CULTIVATION_COMMAND, label="温养双修", target_username=target,
                     updated_at=datetime.now().strftime(TIME_FORMAT), updated_by=username)
        controls[DUAL_CULTIVATION_COMMAND] = entry
        save_command_controls(data)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {"success": True, "account": account, "identity": identity, "target_username": target}


@app.post("/api/beast-border-patrol-mode")
async def set_beast_border_patrol_mode(
    payload: dict = Body(...),
    username: str = Depends(authenticate),
):
    """Set the patrol route while keeping stamina-based beast selection automatic."""
    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "主魂").strip() or "主魂"
    mode = str(payload.get("mode") or "").strip()
    if account not in {"main", "xiaohao"}:
        return {"success": False, "msg": "该账号没有自动灵兽巡边"}
    if identity != "主魂":
        return {"success": False, "msg": "灵兽巡边仅由主魂执行"}
    if mode not in BEAST_BORDER_PATROL_MODES:
        return {"success": False, "msg": "巡边路线必须是斥候、护粮或袭营"}

    with COMMAND_CONTROL_LOCK:
        data = load_command_controls()
        identity_controls = data.setdefault(account, {}).setdefault(identity, {})
        old_entry = identity_controls.get(BEAST_BORDER_PATROL_CONTROL_KEY, {})
        entry = dict(old_entry) if isinstance(old_entry, dict) else {
            "disabled": bool(old_entry),
        }
        entry.update({
            "command": f".灵兽巡边 <灵兽> {mode}",
            "label": "灵兽巡边",
            "patrol_mode": mode,
            "updated_at": datetime.now().strftime(TIME_FORMAT),
            "updated_by": username,
        })
        entry.setdefault("disabled", False)
        identity_controls[BEAST_BORDER_PATROL_CONTROL_KEY] = entry
        save_command_controls(data)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "account": account,
        "identity": identity,
        "mode": mode,
        "updated_by": username,
    }


@app.post("/api/fishing-auto-bait")
async def set_fishing_auto_bait(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Update the global auto-fishing bait without changing the enabled/paused switch."""
    if not FISHING_AUTOMATION_ENABLED:
        raise HTTPException(status_code=410, detail="自动钓鱼已停用")
    bait = str(payload.get("bait") or "").strip()
    if bait not in FISHING_CONTROL_BAITS:
        return {"success": False, "msg": "未知鱼饵"}
    with COMMAND_CONTROL_LOCK:
        data = load_command_controls()
        enabled = False
        for auto_account in WINDOW_MAP:
            entry = (
                data.get(auto_account, {})
                .get("主魂", {})
                .get(FISHING_AUTO_CONTROL_COMMAND, {})
            )
            if isinstance(entry, dict) and not bool(entry.get("disabled")):
                enabled = True
                break
        for auto_account in WINDOW_MAP:
            account_controls = data.setdefault(auto_account, {})
            identity_controls = account_controls.setdefault("主魂", {})
            old_entry = identity_controls.get(FISHING_AUTO_CONTROL_COMMAND, {})
            disabled = bool(old_entry.get("disabled")) if isinstance(old_entry, dict) else not enabled
            identity_controls[FISHING_AUTO_CONTROL_COMMAND] = {
                "disabled": disabled,
                "command": FISHING_AUTO_CONTROL_COMMAND,
                "label": "全自动钓鱼",
                "bait": bait,
                "updated_at": datetime.now().strftime(TIME_FORMAT),
                "updated_by": username,
            }
        save_command_controls(data)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {"success": True, "bait": bait, "enabled": enabled}


@app.post("/api/fishing-auto-holder")
async def set_fishing_auto_holder(payload: dict = Body(...), username: str = Depends(authenticate)):
    """Manually correct the global auto-fishing rod holder."""
    if not FISHING_AUTOMATION_ENABLED:
        raise HTTPException(status_code=410, detail="自动钓鱼已停用")
    account = str(payload.get("account") or "").strip()
    identity = str(payload.get("identity") or "主魂").strip() or "主魂"
    if account not in FISHING_AUTO_CONTROL_ACCOUNTS:
        return {"success": False, "msg": "未知账号"}
    if identity not in FISHING_AUTO_ACCOUNT_IDENTITIES.get(account, ("主魂",)):
        return {"success": False, "msg": "未知身份"}
    holder = set_fishing_auto_holder_state(account, identity, username=username)
    with STATUS_LOCK:
        STATUS_CACHE.clear()
    return {
        "success": True,
        "account": holder["account"],
        "identity": holder["identity"],
        "label": fishing_auto_identity_label(holder["account"], holder["identity"]),
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
async def index(request: Request):
    """前端页面"""
    if not dashboard_session_user(request.cookies.get(DASHBOARD_SESSION_COOKIE)):
        return RedirectResponse("/login", status_code=303)
    html_path = os.path.join(CONFIG_DIR, 'dashboard.html')
    if os.path.exists(html_path):
        with open(html_path, 'r', encoding='utf-8') as f:
            return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})
    return "<h1>Dashboard UI File Missing</h1>"

if __name__ == "__main__":
    """启动服务（仅监听本机 127.0.0.1:8000，由 HTTPS 反向代理对外提供）。"""
    uvicorn.run(app, host="127.0.0.1", port=8000, access_log=False)
