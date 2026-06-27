import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta

from common_command_features import add_seconds_str, is_future, now_str, seconds_until, str_to_dt
from log_utils import (
    COMMAND_CONTROL_FILE,
    _is_own_outgoing_sender,
    feedback_response_matches_command,
    is_game_bot_sender,
    log_incoming_message,
    meaningful_reply_to_msg_id,
    record_bot_response,
    send_text_alert,
    tracked_command_identity_for_reply,
    tracked_command_text_for_reply,
)


FISHING_BAIT = "灵米饵"
FISHING_MASTER_COMMAND = f".钓鱼 {FISHING_BAIT}"
FISHING_LEGACY_MASTER_COMMANDS = (".钓鱼 灵虫饵",)
FISHING_CONTROL_BAITS = ("凡饵", "灵虫饵", "灵米饵", "妖血饵")
FISHING_CONTROL_COMMANDS = tuple(f".钓鱼 {bait}" for bait in FISHING_CONTROL_BAITS)
FISHING_AUTO_CONTROL_COMMAND = ".全自动钓鱼"
FISHING_AUTO_CONTROL_COMMANDS = (FISHING_AUTO_CONTROL_COMMAND,)
FISHING_AUTO_LEGACY_CONTROL_COMMANDS = tuple(f".全自动钓鱼 {bait}" for bait in FISHING_CONTROL_BAITS)
FISHING_AUTO_CONTROL_ACCOUNTS = ("main", "sub", "xiaohao")
FISHING_AUTO_ACCOUNT_IDENTITIES = {
    "main": ("主魂", "无咎子", "缘生子", "素缘子"),
    "sub": ("主魂", "厚土", "缘生子", "寻真子"),
    "xiaohao": ("主魂", "问心子", "素心子", "缘生子"),
}
FISHING_DAILY_LIMIT = 20
FISHING_ROUND_BUFFER_SECONDS = 5
FISHING_IMPENDING_GUARD_SECONDS = 120
FISHING_CROSS_IDENTITY_YIELD_SECONDS = 45
FISHING_CROSS_IDENTITY_YIELD_COOLDOWN_SECONDS = 120
FISHING_ACTIVE_SWITCH_BUFFER_SECONDS = 10
FISHING_RETRY_SECONDS = 2 * 60
FISHING_DISABLED_SLEEP_SECONDS = 60
FISHING_AUTO_DISABLED_SLEEP_SECONDS = 60
FISHING_AUTO_RETRY_SECONDS = 5 * 60
FISHING_ROD_ITEM = "青竹钓竿"
FISHING_ROD_LISTING_MATERIAL = "凝血草"

FISHING_NEST_PLAN = (
    ("灵草窝", 2),
    ("米糠小窝", 2),
)

FISHING_NEST_BAIT_REQUIREMENTS = {
    "灵草窝": ("灵米饵", 3),
    "米糠小窝": ("凡饵", 2),
}

FISHING_BAIT_NAMES = {"凡饵", "灵虫饵", "灵米饵", "妖血饵"}
FISHING_EXCHANGEABLE_MATERIALS = {"凝血草"}
FISHING_DAILY_STALE_STATUSES = {
    "daily_done",
    "synced",
    "bait_bought",
    "nested",
    "caught",
    "empty",
    "yielding",
    "yielding_identity",
    "auto_managed",
}


def fishing_default_state():
    return {
        "last_sync_date": "",
        "last_status": "paused",
        "last_detail": "",
        "last_response": "",
        "next_action_at": "",
        "preferred_bait": FISHING_BAIT,
        "rod_owned": None,
        "skill": "",
        "skill_exp": 0,
        "today_count": 0,
        "daily_limit": FISHING_DAILY_LIMIT,
        "baits": {},
        "catches": {},
        "daily_catches": {},
        "daily_loot": {},
        "recorded_rod_message_ids": [],
        "current_nest": "",
        "current_nest_remaining": 0,
        "nest_plan_date": "",
        "nest_counts": {},
        "nest_blocked": {},
        "bait_purchase_date": "",
        "bait_purchase_done": False,
        "daily_done_basket_sync_date": "",
        "daily_done_auto_paused_date": "",
        "daily_done_notified_date": "",
        "active": False,
        "active_bait": "",
        "active_started_at": "",
        "active_due_at": "",
        "last_catch": "",
        "last_round_at": "",
        "consecutive_empty": 0,
        "last_cross_identity_yield_at": "",
    }


def fishing_auto_default_state():
    return {
        "last_sync_date": "",
        "last_status": "paused",
        "last_detail": "",
        "last_response": "",
        "next_action_at": "",
        "preferred_bait": FISHING_BAIT,
        "active_identity": "",
        "rod_holder": "",
        "completed": {},
        "transfer_from": "",
        "transfer_to": "",
        "transfer_listing_id": "",
        "transfer_started_at": "",
        "daily_done_auto_paused_date": "",
    }


def _strip_markdown(text):
    return str(text or "").replace("**", "").replace("`", "")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _next_day_action_time():
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=5, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def fishing_daily_count_is_current(state, today=None):
    if not isinstance(state, dict):
        return False
    today = today or _today()
    dated_fields = ("last_sync_date", "daily_done_basket_sync_date")
    if any(str(state.get(key) or "") == today for key in dated_fields):
        return True
    timestamp_fields = ("last_round_at", "active_started_at", "active_due_at")
    return any(str(state.get(key) or "").startswith(today) for key in timestamp_fields)


def reset_stale_fishing_daily_state(state, today=None):
    if not isinstance(state, dict):
        return False
    today = today or _today()
    if fishing_daily_count_is_current(state, today):
        return False

    has_stale_daily_data = any([
        int(state.get("today_count") or 0) > 0,
        state.get("daily_done_basket_sync_date"),
        state.get("daily_done_auto_paused_date"),
        state.get("daily_done_notified_date"),
        state.get("daily_catches"),
        state.get("daily_loot"),
        state.get("current_nest"),
        int(state.get("current_nest_remaining") or 0) > 0,
        state.get("last_status") in FISHING_DAILY_STALE_STATUSES,
    ])
    if not has_stale_daily_data:
        return False

    state["today_count"] = 0
    state["daily_done_basket_sync_date"] = ""
    state["daily_done_auto_paused_date"] = ""
    state["daily_done_notified_date"] = ""
    state["daily_catches"] = {}
    state["daily_loot"] = {}
    state["recorded_rod_message_ids"] = []
    state["current_nest"] = ""
    state["current_nest_remaining"] = 0
    if state.get("last_status") in FISHING_DAILY_STALE_STATUSES:
        state["last_status"] = "waiting"
    if "今日" in str(state.get("last_detail") or "") or "剩余" in str(state.get("last_detail") or ""):
        state["last_detail"] = "等待今日钓鱼"
    next_action = str(state.get("next_action_at") or "")
    if next_action and not is_future(next_action):
        state["next_action_at"] = ""
    return True


def fishing_daily_done_for_today(state, today=None):
    if not isinstance(state, dict):
        return False
    today = today or _today()
    if not fishing_daily_count_is_current(state, today):
        return False
    daily_limit = int(state.get("daily_limit") or FISHING_DAILY_LIMIT)
    return daily_limit > 0 and int(state.get("today_count") or 0) >= daily_limit


def fishing_dashboard_state(state, today=None):
    view = dict(state or {}) if isinstance(state, dict) else {}
    reset_stale_fishing_daily_state(view, today=today)
    return view


def fishing_command_for_bait(bait):
    bait = str(bait or "").strip()
    if bait not in FISHING_BAIT_NAMES:
        bait = FISHING_BAIT
    return f".钓鱼 {bait}"


def fishing_bait_from_command(command):
    match = re.fullmatch(r"\.钓鱼\s+(\S+)", str(command or "").strip())
    if not match:
        return ""
    bait = match.group(1).strip()
    return bait if bait in FISHING_BAIT_NAMES else ""


def fishing_bait_for_state(state):
    if not isinstance(state, dict):
        return FISHING_BAIT
    bait = str(state.get("preferred_bait") or "").strip()
    if bait not in FISHING_BAIT_NAMES:
        bait = FISHING_BAIT
    return bait


def fishing_dashboard_bait(state):
    return fishing_bait_for_state(fishing_dashboard_state(state))


def fishing_dashboard_command(state):
    return fishing_command_for_bait(fishing_dashboard_bait(state))


def fishing_auto_command_for_bait(bait):
    return FISHING_AUTO_CONTROL_COMMAND


def fishing_auto_bait_from_command(command):
    match = re.fullmatch(r"\.全自动钓鱼\s+(\S+)", str(command or "").strip())
    if not match:
        return ""
    bait = match.group(1).strip()
    return bait if bait in FISHING_CONTROL_BAITS else ""


def fishing_auto_bait_from_entry(entry, fallback=FISHING_BAIT):
    if isinstance(entry, dict):
        bait = str(entry.get("bait") or "").strip()
        if bait in FISHING_CONTROL_BAITS:
            return bait
        bait = fishing_auto_bait_from_command(entry.get("command", ""))
        if bait in FISHING_CONTROL_BAITS:
            return bait
    bait = str(fallback or "").strip()
    return bait if bait in FISHING_CONTROL_BAITS else FISHING_BAIT


def fishing_auto_bait_for_state(state):
    if not isinstance(state, dict):
        return FISHING_BAIT
    bait = str(state.get("preferred_bait") or "").strip()
    return bait if bait in FISHING_CONTROL_BAITS else FISHING_BAIT


def fishing_auto_dashboard_state(state, today=None):
    view = dict(state or {}) if isinstance(state, dict) else {}
    defaults = fishing_auto_default_state()
    for key, value in defaults.items():
        view.setdefault(key, value)
    today = today or _today()
    if str(view.get("last_sync_date") or "") != today:
        view["completed"] = {}
        view["daily_done_auto_paused_date"] = ""
        if view.get("last_status") == "done":
            view["last_status"] = "waiting"
            view["last_detail"] = "等待今日全自动钓鱼"
    return view


def fishing_catch_summary(catches):
    if not isinstance(catches, dict):
        return ""
    parts = []
    for name, count in catches.items():
        name = str(name or "").strip()
        try:
            count = int(count or 0)
        except Exception:
            count = 0
        if name and count > 0:
            parts.append(f"{name} x{count}")
    return "、".join(parts)


def parse_fishing_loot_lines(text):
    clean = _strip_markdown(text)
    loot = {}
    for name, count in re.findall(r"伴生机缘[:：]\s*【([^】]+)】\s*x\s*(\d+)", clean):
        name = name.strip()
        if name:
            loot[name] = loot.get(name, 0) + int(count)
    return loot


def parse_fishing_control_text(text):
    match = re.fullmatch(r"钓鱼\s+(\S+)", str(text or "").strip())
    if not match:
        return ""
    bait = match.group(1).strip()
    return bait if bait in FISHING_CONTROL_BAITS else ""


def parse_fishing_auto_control_text(text):
    match = re.fullmatch(r"全自动钓鱼\s+(\S+)", str(text or "").strip())
    if not match:
        return ""
    bait = match.group(1).strip()
    return bait if bait in FISHING_CONTROL_BAITS else ""


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


def _load_command_controls_uncached():
    try:
        with open(COMMAND_CONTROL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_command_controls_uncached(data):
    directory = os.path.dirname(COMMAND_CONTROL_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{COMMAND_CONTROL_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, COMMAND_CONTROL_FILE)


def _acquire_command_control_lock(timeout=5):
    lock_path = f"{COMMAND_CONTROL_FILE}.lock"
    deadline = time.monotonic() + max(0.5, float(timeout or 0.5))
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            return fd, lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.remove(lock_path)
                    continue
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return None, lock_path
            time.sleep(0.05)


def _release_command_control_lock(lock):
    fd, lock_path = lock
    try:
        if fd is not None:
            os.close(fd)
    except Exception:
        pass
    try:
        if fd is not None:
            os.remove(lock_path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _fishing_auto_global_file():
    return os.path.join(os.path.dirname(COMMAND_CONTROL_FILE), "fishing_auto_global.json")


def _fishing_auto_state_file(account):
    return os.path.join(os.path.dirname(COMMAND_CONTROL_FILE), f"state_{account}.json")


def _fishing_auto_identity_key(account, identity):
    return f"{account}|{str(identity or '主魂').strip() or '主魂'}"


def _fishing_auto_default_global_state():
    return {
        "date": _today(),
        "preferred_bait": FISHING_BAIT,
        "active": {},
        "rod_holder": {},
        "completed": {},
        "transfer": {},
        "updated_at": now_str(),
    }


def _load_fishing_auto_global_state():
    path = _fishing_auto_global_file()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except FileNotFoundError:
        data = {}
    except Exception:
        data = {}
    defaults = _fishing_auto_default_global_state()
    if data.get("date") != _today():
        keep_bait = data.get("preferred_bait") if data.get("preferred_bait") in FISHING_CONTROL_BAITS else FISHING_BAIT
        keep_holder = data.get("rod_holder") if isinstance(data.get("rod_holder"), dict) else {}
        data = defaults
        data["preferred_bait"] = keep_bait
        if keep_holder.get("account") and keep_holder.get("identity"):
            data["rod_holder"] = keep_holder
        return data
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def _save_fishing_auto_global_state(data):
    path = _fishing_auto_global_file()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    data = data if isinstance(data, dict) else {}
    data["date"] = _today()
    data["updated_at"] = now_str()
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _acquire_fishing_auto_global_lock(timeout=5):
    lock_path = f"{_fishing_auto_global_file()}.lock"
    deadline = time.monotonic() + max(0.5, float(timeout or 0.5))
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            return fd, lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.remove(lock_path)
                    continue
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return None, lock_path
            time.sleep(0.05)


def _load_fishing_auto_account_root_state(account):
    try:
        with open(_fishing_auto_state_file(account), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fishing_auto_identity_fishing_state(root_state, identity):
    identity = str(identity or "主魂").strip() or "主魂"
    if identity == "主魂":
        state = root_state.get("fishing", {}) if isinstance(root_state, dict) else {}
    else:
        avatars = root_state.get("avatars", {}) if isinstance(root_state, dict) else {}
        avatar_state = avatars.get(identity, {}) if isinstance(avatars, dict) else {}
        state = avatar_state.get("fishing", {}) if isinstance(avatar_state, dict) else {}
    return state if isinstance(state, dict) else {}


def parse_fishing_basket(text):
    clean = _strip_markdown(text)
    result = {
        "matched": "【鱼篓】" in clean,
        "rod_owned": None,
        "skill": "",
        "skill_exp": 0,
        "today_count": None,
        "daily_limit": None,
        "current_nest": "",
        "current_nest_remaining": 0,
        "baits": {},
        "catches": {},
    }
    if not result["matched"]:
        return result

    if "青竹钓竿：" in clean:
        result["rod_owned"] = "已持有" in clean

    skill_match = re.search(r"钓术：\s*(Lv\.\d+\s*[^\n（(]+)\s*[（(](\d+)熟练度", clean)
    if skill_match:
        result["skill"] = skill_match.group(1).strip()
        result["skill_exp"] = int(skill_match.group(2))

    count_match = re.search(r"今日竿数：\s*(\d+)\s*/\s*(\d+)", clean)
    if count_match:
        result["today_count"] = int(count_match.group(1))
        result["daily_limit"] = int(count_match.group(2))

    nest_match = re.search(r"当前窝料：\s*(?:【)?([^（\n]+?)(?:】)?(?:（剩余\s*(\d+)\s*竿）)?\s*(?:\n|$)", clean)
    if nest_match:
        nest = nest_match.group(1).strip()
        if nest and nest != "无":
            result["current_nest"] = nest
            result["current_nest_remaining"] = int(nest_match.group(2) or 0)

    section = ""
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if line == "鱼饵":
            section = "baits"
            continue
        if line == "鱼获":
            section = "catches"
            continue
        if not line.startswith("- "):
            continue
        item_match = re.match(r"-\s*(.+?)\s*x\s*(\d+)\s*$", line)
        if not item_match:
            continue
        name = item_match.group(1).strip()
        count = int(item_match.group(2))
        if section == "baits":
            result["baits"][name] = count
        elif section == "catches":
            result["catches"][name] = count

    return result


def parse_fishing_start(text):
    clean = _strip_markdown(text)
    result = {
        "matched": False,
        "status": "",
        "bait": "",
        "wait_seconds": 0,
        "missing_bait": "",
        "today_count": None,
        "daily_limit": None,
    }
    if "你挂上" in clean and "抛竿入水" in clean:
        result["matched"] = True
        result["status"] = "started"
        bait_match = re.search(r"你挂上\s*【([^】]+)】", clean)
        if bait_match:
            result["bait"] = bait_match.group(1).strip()
        wait_match = re.search(r"预计\s*(\d+)\s*秒\s*内会有鱼讯", clean)
        countdown_match = re.search(r"鱼讯倒计时：\s*(\d+)\s*秒", clean)
        result["wait_seconds"] = int((wait_match or countdown_match).group(1)) if (wait_match or countdown_match) else 60
        return result

    missing_match = re.search(r"鱼篓中没有【([^】]+)】", clean)
    if missing_match:
        result["matched"] = True
        result["status"] = "missing_bait"
        result["missing_bait"] = missing_match.group(1).strip()
        return result

    limit_match = re.search(r"今日已垂钓\s*(\d+)\s*/\s*(\d+)", clean)
    if limit_match:
        result["matched"] = True
        result["status"] = "daily_limit"
        result["today_count"] = int(limit_match.group(1))
        result["daily_limit"] = int(limit_match.group(2))
        return result

    if "尚无【青竹钓竿】" in clean:
        result["matched"] = True
        result["status"] = "no_rod"
        return result

    if "已有一竿尚未收起" in clean:
        result["matched"] = True
        result["status"] = "already_active"
        return result

    return result


def parse_missing_resources(text):
    clean = _strip_markdown(text)
    match = re.search(r"资源不足：\s*([^。\n]+)", clean)
    if not match:
        return []
    resources = []
    for name, count in re.findall(r"([^,，、\sx]+?)\s*x\s*(\d+)", match.group(1)):
        name = name.strip()
        if name:
            resources.append({"name": name, "count": int(count)})
    return resources


def parse_buy_bait(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": "", "bait": "", "count": 0, "missing_resources": []}
    buy_match = re.search(r"购得\s*【([^】]+)】x(\d+)", clean)
    if buy_match:
        result.update({
            "matched": True,
            "status": "success",
            "bait": buy_match.group(1).strip(),
            "count": int(buy_match.group(2)),
        })
        return result
    if "渔具铺中并无此等鱼饵" in clean:
        result.update({"matched": True, "status": "invalid_bait"})
    elif "资源不足" in clean or "灵石不足" in clean or "材料不足" in clean:
        result.update({
            "matched": True,
            "status": "insufficient_resource",
            "missing_resources": parse_missing_resources(clean),
        })
    return result


def parse_exchange_response(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": "", "material": "", "count": 0}
    success_match = re.search(r"获得了【([^】]+)】x(\d+)", clean)
    if "兑换成功" in clean or success_match:
        result["matched"] = True
        result["status"] = "success"
        if success_match:
            result["material"] = success_match.group(1).strip()
            result["count"] = int(success_match.group(2))
        return result
    if "贡献不足" in clean or "资源不足" in clean or "无法兑换" in clean:
        result.update({"matched": True, "status": "failed"})
    return result


def parse_trade_listing_response(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": "", "listing_id": "", "missing_resources": []}
    if any(k in clean for k in ["上架成功", "挂单成功", "交易挂单", "成功上架", "已上架"]):
        result["matched"] = True
        result["status"] = "success"
        id_match = re.search(r"(?:挂单|订单|交易|编号|ID|id)[^\d]{0,8}(\d{3,})", clean)
        if not id_match:
            id_match = re.search(r"#\s*(\d{3,})", clean)
        if not id_match:
            id_match = re.search(r"\b(\d{3,})\b", clean)
        if id_match:
            result["listing_id"] = id_match.group(1)
        return result
    if "资源不足" in clean or "材料不足" in clean or "凝血草不足" in clean:
        result.update({
            "matched": True,
            "status": "insufficient_resource",
            "missing_resources": parse_missing_resources(clean),
        })
        if not result["missing_resources"] and "凝血草" in clean:
            count_match = re.search(r"凝血草\s*x?\s*(\d+)", clean)
            result["missing_resources"] = [{
                "name": FISHING_ROD_LISTING_MATERIAL,
                "count": int(count_match.group(1)) if count_match else 1,
            }]
    elif any(k in clean for k in ["上架失败", "挂单失败", "无法上架"]):
        result.update({"matched": True, "status": "failed"})
    return result


def parse_trade_purchase_response(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": ""}
    if any(k in clean for k in ["购买成功", "交易成功", "成交", "获得了"]):
        result["matched"] = True
        result["status"] = "success"
        return result
    if any(k in clean for k in ["挂单不存在", "已被购买", "购买失败", "交易失败", "资源不足", "灵石不足"]):
        result["matched"] = True
        result["status"] = "failed"
    return result


def parse_nest_response(text):
    clean = _strip_markdown(text)
    result = {
        "matched": False,
        "status": "",
        "nest": "",
        "remaining": 0,
        "missing_name": "",
        "missing_count": 0,
        "missing_resources": [],
    }
    success_match = re.search(r"撒下\s*【([^】]+)】.*接下来\s*(\d+)\s*竿", clean)
    if "【打窝已成】" in clean or success_match:
        result["matched"] = True
        result["status"] = "success"
        if success_match:
            result["nest"] = success_match.group(1).strip()
            result["remaining"] = int(success_match.group(2))
        return result

    active_match = re.search(r"已打下\s*【([^】]+)】.*?还可影响\s*(\d+)\s*竿.*?不可重复叠加", clean)
    if active_match:
        result.update({
            "matched": True,
            "status": "already_active",
            "nest": active_match.group(1).strip(),
            "remaining": int(active_match.group(2)),
        })
        return result

    missing_match = re.search(r"资源不足：\s*([^x。\n]+)x(\d+)", clean)
    if missing_match:
        missing_resources = parse_missing_resources(clean)
        result.update({
            "matched": True,
            "status": "missing_resource",
            "missing_name": missing_match.group(1).strip(),
            "missing_count": int(missing_match.group(2)),
            "missing_resources": missing_resources,
        })
        return result

    if "今日此类窝料已经用尽" in clean:
        result.update({"matched": True, "status": "daily_used_up"})
    elif "打窝失败" in clean:
        result.update({"matched": True, "status": "failed"})
    return result


def parse_rod_response(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": "", "catch": "", "loot": {}}
    if "【提竿成功】" in clean:
        result["matched"] = True
        result["status"] = "success"
        fish_match = re.search(r"一尾\s*【([^】]+)】", clean)
        if fish_match:
            result["catch"] = fish_match.group(1).strip()
        result["loot"] = parse_fishing_loot_lines(clean)
        return result
    if "【空竿】" in clean or "提竿太急" in clean or "时机差了一线" in clean or "收竿起身" in clean:
        result["matched"] = True
        result["status"] = "empty"
    return result


class FishingMixin:
    def get_fishing_state(self, identity="主魂"):
        target = self.state if identity == "主魂" else self.get_avatar_state(identity)
        state = target.get("fishing")
        if not isinstance(state, dict):
            state = fishing_default_state()
            target["fishing"] = state
        else:
            defaults = fishing_default_state()
            for key, value in defaults.items():
                state.setdefault(key, value)
        today = _today()
        reset_stale_fishing_daily_state(state, today=today)
        if state.get("nest_plan_date") != today:
            state["nest_plan_date"] = today
            state["nest_counts"] = {}
            state["nest_blocked"] = {}
        if state.get("bait_purchase_date") != today:
            state["bait_purchase_date"] = today
            state["bait_purchase_done"] = False
        return state

    def get_fishing_auto_state(self):
        state = self.state.get("fishing_auto") if isinstance(getattr(self, "state", None), dict) else None
        if not isinstance(state, dict):
            state = fishing_auto_default_state()
            self.state["fishing_auto"] = state
        else:
            defaults = fishing_auto_default_state()
            for key, value in defaults.items():
                state.setdefault(key, value)
        today = _today()
        if state.get("last_sync_date") != today:
            state["last_sync_date"] = today
            state["completed"] = {}
            state["daily_done_auto_paused_date"] = ""
            if state.get("last_status") == "done":
                state["last_status"] = "waiting"
                state["last_detail"] = "等待今日全自动钓鱼"
                state["next_action_at"] = ""
        return state

    def fishing_logger(self):
        return getattr(self, "log", None)

    def fishing_account_key(self):
        return str(getattr(self, "account_key", "") or "").strip()

    def fishing_preferred_bait(self, identity):
        return fishing_bait_for_state(self.get_fishing_state(identity))

    def fishing_master_command(self, identity):
        return fishing_command_for_bait(self.fishing_preferred_bait(identity))

    def fishing_control_keys(self, identity):
        keys = [
            self.fishing_master_command(identity),
            FISHING_MASTER_COMMAND,
            *FISHING_LEGACY_MASTER_COMMANDS,
            *FISHING_CONTROL_COMMANDS,
        ]
        return tuple(dict.fromkeys(key for key in keys if key))

    def fishing_identity_from_control_message(self, msg):
        sender_variants = _sender_id_variants(getattr(msg, "sender_id", None))
        avatar_chat_ids = getattr(self, "_avatar_chat_ids", {}) or {}
        for raw_id, identity in avatar_chat_ids.items():
            if sender_variants & _sender_id_variants(raw_id):
                return identity or "主魂"
        return (
            getattr(self, "_manual_identity_label", None)
            or getattr(self, "current_identity", None)
            or "主魂"
        )

    def fishing_wakeup_event(self, identity):
        events = getattr(self, "_fishing_wakeup_events", None)
        if events is None:
            events = {}
            self._fishing_wakeup_events = events
        identity = str(identity or "主魂").strip() or "主魂"
        event = events.get(identity)
        if event is None:
            event = asyncio.Event()
            events[identity] = event
        return event

    def fishing_wake(self, identity):
        self.fishing_wakeup_event(identity).set()

    async def fishing_sleep(self, identity, seconds):
        seconds = max(1, min(int(seconds or 1), 300))
        event = self.fishing_wakeup_event(identity)
        if event.is_set():
            event.clear()
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
            event.clear()
            return True
        except asyncio.TimeoutError:
            return False

    def fishing_pause_dashboard_command(self, identity, reason="daily limit reached"):
        account = self.fishing_account_key()
        if not account:
            return False
        state = self.get_fishing_state(identity)
        if not fishing_daily_done_for_today(state):
            log = self.fishing_logger()
            if log:
                log.info(
                    f"Fishing [{identity}] skip dashboard auto-pause because daily count "
                    "is not current or not complete."
                )
            return False
        identity = str(identity or "主魂").strip() or "主魂"
        control_keys = self.fishing_control_keys(identity)
        changed = False
        lock = _acquire_command_control_lock()
        try:
            data = _load_command_controls_uncached()
            account_controls = data.setdefault(account, {})
            identity_controls = account_controls.setdefault(identity, {})
            for key in control_keys:
                current = identity_controls.get(key)
                current_disabled = bool(current.get("disabled")) if isinstance(current, dict) else bool(current)
                if not current_disabled:
                    changed = True
                identity_controls[key] = {
                    "disabled": True,
                    "command": key,
                    "label": "钓鱼",
                    "updated_at": now_str(),
                    "updated_by": "auto-fishing",
                    "reason": reason,
                }
            _save_command_controls_uncached(data)
        finally:
            _release_command_control_lock(lock)

        state["daily_done_auto_paused_date"] = _today()
        self.save_state()
        log = self.fishing_logger()
        if log:
            log.info(
                f"Fishing [{identity}] auto-paused dashboard command "
                f"{self.fishing_master_command(identity)} ({reason})."
            )
        return changed

    def fishing_enable_dashboard_command(self, identity, bait, reason="chat control"):
        account = self.fishing_account_key()
        if not account:
            return False
        identity = str(identity or "主魂").strip() or "主魂"
        bait = bait if bait in FISHING_CONTROL_BAITS else FISHING_BAIT
        selected_key = fishing_command_for_bait(bait)
        changed = False
        lock = _acquire_command_control_lock()
        try:
            data = _load_command_controls_uncached()
            account_controls = data.setdefault(account, {})
            identity_controls = account_controls.setdefault(identity, {})
            for key in self.fishing_control_keys(identity):
                disabled = key != selected_key
                current = identity_controls.get(key)
                current_disabled = bool(current.get("disabled")) if isinstance(current, dict) else bool(current)
                if key not in identity_controls or current_disabled != disabled:
                    changed = True
                identity_controls[key] = {
                    "disabled": disabled,
                    "command": key,
                    "label": "钓鱼",
                    "updated_at": now_str(),
                    "updated_by": "chat-control",
                    "reason": reason,
                }
            _save_command_controls_uncached(data)
        finally:
            _release_command_control_lock(lock)
        return changed

    def fishing_auto_control_keys(self):
        return tuple(FISHING_AUTO_CONTROL_COMMANDS)

    def fishing_auto_master_command(self):
        return fishing_auto_command_for_bait(fishing_auto_bait_for_state(self.get_fishing_auto_state()))

    def fishing_auto_write_dashboard_commands(self, disabled, bait=None, reason="dashboard control"):
        bait = bait if bait in FISHING_CONTROL_BAITS else fishing_auto_bait_for_state(self.get_fishing_auto_state())
        changed = False
        lock = _acquire_command_control_lock()
        try:
            data = _load_command_controls_uncached()
            for account in FISHING_AUTO_CONTROL_ACCOUNTS:
                account_controls = data.setdefault(account, {})
                identity_controls = account_controls.setdefault("主魂", {})
                current = identity_controls.get(FISHING_AUTO_CONTROL_COMMAND)
                current_disabled = bool(current.get("disabled")) if isinstance(current, dict) else bool(current)
                current_bait = fishing_auto_bait_from_entry(current, fallback=bait)
                if (
                    FISHING_AUTO_CONTROL_COMMAND not in identity_controls
                    or current_disabled != bool(disabled)
                    or current_bait != bait
                ):
                    changed = True
                identity_controls[FISHING_AUTO_CONTROL_COMMAND] = {
                    "disabled": bool(disabled),
                    "command": FISHING_AUTO_CONTROL_COMMAND,
                    "label": "全自动钓鱼",
                    "bait": bait,
                    "updated_at": now_str(),
                    "updated_by": "auto-fishing",
                    "reason": reason,
                }
                for legacy_key in FISHING_AUTO_LEGACY_CONTROL_COMMANDS:
                    if legacy_key in identity_controls:
                        identity_controls.pop(legacy_key, None)
                        changed = True
            _save_command_controls_uncached(data)
        finally:
            _release_command_control_lock(lock)
        return changed

    def fishing_auto_enable_dashboard_command(self, bait, reason="chat control"):
        account = self.fishing_account_key()
        if not account:
            return False
        bait = bait if bait in FISHING_CONTROL_BAITS else FISHING_BAIT
        return self.fishing_auto_write_dashboard_commands(False, bait=bait, reason=reason)

    def fishing_auto_pause_dashboard_command(self, reason="daily limit reached"):
        account = self.fishing_account_key()
        if not account:
            return False
        changed = self.fishing_auto_write_dashboard_commands(
            True,
            bait=fishing_auto_bait_for_state(self.get_fishing_auto_state()),
            reason=reason,
        )
        state = self.get_fishing_auto_state()
        state["daily_done_auto_paused_date"] = _today()
        self.save_state()
        return changed

    def fishing_auto_apply_control(self, bait, reason="chat control"):
        bait = bait if bait in FISHING_CONTROL_BAITS else ""
        if not bait:
            return False
        state = self.get_fishing_auto_state()
        old_bait = fishing_auto_bait_for_state(state)
        state["preferred_bait"] = bait
        if old_bait != bait:
            state["last_detail"] = f"全自动钓鱼鱼饵切换为 {bait}"
        else:
            state["last_detail"] = f"全自动钓鱼已启用，鱼饵 {bait}"
        state["last_status"] = "enabled"
        state["next_action_at"] = ""
        self.fishing_auto_enable_dashboard_command(bait, reason=reason)
        self.save_state()
        for identity in self.fishing_auto_identities():
            self.fishing_wake(identity)
        return True

    def fishing_apply_control(self, identity, bait, reason="chat control"):
        identity = str(identity or "主魂").strip() or "主魂"
        bait = bait if bait in FISHING_CONTROL_BAITS else ""
        if not bait:
            return False
        state = self.get_fishing_state(identity)
        old_bait = fishing_bait_for_state(state)
        state["preferred_bait"] = bait
        if old_bait != bait:
            state["bait_purchase_done"] = False
        if fishing_daily_done_for_today(state):
            state["last_status"] = "daily_done"
            state["last_detail"] = (
                f"今日已垂钓 {state.get('today_count')}/{state.get('daily_limit')}，"
                f"饵料已切换为 {bait}"
            )
        else:
            state["last_status"] = "enabled"
            state["last_detail"] = f"手动启用钓鱼，饵料 {bait}"
            if not state.get("active"):
                state["next_action_at"] = ""
        self.fishing_enable_dashboard_command(identity, bait, reason=reason)
        self.save_state()
        self.fishing_wake(identity)
        log = self.fishing_logger()
        if log:
            log.info(f"Fishing [{identity}] enabled by chat control with bait {bait}.")
        return True

    async def maybe_handle_fishing_control_message(self, msg, text, sender=None):
        if sender is not None and is_game_bot_sender(self, sender):
            return False
        auto_bait = parse_fishing_auto_control_text(text)
        if auto_bait:
            if not _is_own_outgoing_sender(self, msg):
                return False
            self.fishing_auto_apply_control(auto_bait, reason="chat control")
            return True
        bait = parse_fishing_control_text(text)
        if not bait:
            return False
        if not _is_own_outgoing_sender(self, msg):
            return False
        identity = self.fishing_identity_from_control_message(msg)
        self.fishing_apply_control(identity, bait, reason="chat control")
        return True

    async def fishing_notify_daily_done(self, identity, pause_changed=False):
        state = self.get_fishing_state(identity)
        today = _today()
        if not fishing_daily_done_for_today(state, today=today):
            log = self.fishing_logger()
            if log:
                log.info(
                    f"Fishing [{identity}] skip daily-done notice because daily count "
                    "is not current or not complete."
                )
            return False
        if state.get("daily_done_notified_date") == today:
            return False
        account = self.fishing_account_key()
        account_label = {
            "main": "主号",
            "sub": "副号",
            "xiaohao": "小号",
        }.get(account, account or "账号")
        today_count = int(state.get("today_count") or 0)
        daily_limit = int(state.get("daily_limit") or FISHING_DAILY_LIMIT)
        command = self.fishing_master_command(identity)
        text = (
            f"{account_label} [{identity}] 今日钓鱼已完成 {today_count}/{daily_limit} 竿。\n"
            f"已自动暂停 dashboard 指令：{command}。\n"
            "明天需要继续钓鱼时，请在 dashboard 手动启用。"
        )
        catches_summary = fishing_catch_summary(state.get("daily_catches", {}))
        if catches_summary:
            text += f"\n今日鱼获：{catches_summary}"
        loot_summary = fishing_catch_summary(state.get("daily_loot", {}))
        if loot_summary:
            text += f"\n今日伴生机缘：{loot_summary}"
        if state.get("last_catch"):
            text += f"\n最后一竿：{state.get('last_catch')}"
        if not pause_changed:
            text += "\n备注：dashboard 开关此前已处于暂停状态。"
        log = self.fishing_logger()
        sent = await send_text_alert(self, "钓鱼完成", text, log)
        state["daily_done_notified_date"] = today
        self.save_state()
        if log:
            if sent:
                log.info(f"Fishing [{identity}] daily-done notice sent.")
            else:
                log.warning(f"Fishing [{identity}] daily-done notice could not be sent.")
        return sent

    async def fishing_finish_daily_done(self, identity, reason="daily limit reached"):
        pause_changed = self.fishing_pause_dashboard_command(identity, reason=reason)
        await self.fishing_notify_daily_done(identity, pause_changed=pause_changed)

    def fishing_command_is_enabled(self, identity):
        controls = _load_command_controls_uncached().get(getattr(self, "account_key", ""), {})
        if not isinstance(controls, dict):
            return False
        keys = self.fishing_control_keys(identity)
        for ident in (identity, "*"):
            ident_controls = controls.get(ident, {})
            if not isinstance(ident_controls, dict):
                continue
            for key in keys:
                if key not in ident_controls:
                    continue
                entry = ident_controls.get(key)
                if isinstance(entry, dict):
                    disabled = bool(entry.get("disabled"))
                else:
                    disabled = bool(entry)
                if not disabled:
                    bait = fishing_bait_from_command(key)
                    if bait in FISHING_CONTROL_BAITS and bait != self.fishing_preferred_bait(identity):
                        state = self.get_fishing_state(identity)
                        state["preferred_bait"] = bait
                        state["bait_purchase_done"] = False
                        self.save_state()
                return not disabled
        return False

    def fishing_auto_command_is_enabled(self):
        controls = _load_command_controls_uncached().get(getattr(self, "account_key", ""), {})
        if not isinstance(controls, dict):
            return False
        ident_controls = controls.get("主魂", {})
        if not isinstance(ident_controls, dict):
            ident_controls = {}
        wildcard_controls = controls.get("*", {})
        if not isinstance(wildcard_controls, dict):
            wildcard_controls = {}
        entry = ident_controls.get(FISHING_AUTO_CONTROL_COMMAND, wildcard_controls.get(FISHING_AUTO_CONTROL_COMMAND))
        if entry is not None:
            disabled = bool(entry.get("disabled")) if isinstance(entry, dict) else bool(entry)
            if not disabled:
                state = self.get_fishing_auto_state()
                bait = fishing_auto_bait_from_entry(entry, fallback=fishing_auto_bait_for_state(state))
                if state.get("preferred_bait") != bait:
                    state["preferred_bait"] = bait
                    state["last_detail"] = f"dashboard 选择鱼饵 {bait}"
                    self.save_state()
                return True
            return False
        for key in FISHING_AUTO_LEGACY_CONTROL_COMMANDS:
            entry = ident_controls.get(key, wildcard_controls.get(key))
            if entry is None:
                continue
            disabled = bool(entry.get("disabled")) if isinstance(entry, dict) else bool(entry)
            if disabled:
                return False
            bait = fishing_auto_bait_from_command(key)
            if bait in FISHING_CONTROL_BAITS:
                state = self.get_fishing_auto_state()
                if state.get("preferred_bait") != bait:
                    state["preferred_bait"] = bait
                    state["last_detail"] = f"dashboard 选择鱼饵 {bait}"
                    self.save_state()
            return True
        return False

    async def send_fishing_command(self, identity, command, timeout=60):
        previous_last_sent_id = getattr(self, "last_sent_id", None)
        if identity == "主魂":
            response = await self.send_and_wait_feedback(
                command,
                timeout=timeout,
                max_retries=0,
                suppress_no_response_alert=True,
                return_response_msg=True,
            )
        else:
            response = await self.send_and_wait_feedback_identity(
                identity,
                command,
                timeout=timeout,
                max_retries=0,
                suppress_no_response_alert=True,
                return_response_msg=True,
            )
        if self.fishing_response_text(response):
            return response
        sent_id = getattr(self, "last_sent_id", None)
        if sent_id and sent_id != previous_last_sent_id:
            polled = await self.fishing_poll_reply_to_sent_command(identity, command, sent_id)
            if polled:
                return polled
        return response

    async def fishing_poll_reply_to_sent_command(self, identity, command, sent_id, timeout=8):
        client = getattr(self, "client", None)
        if client is None or not sent_id:
            return ""
        log = self.fishing_logger()
        deadline = time.monotonic() + max(1, float(timeout or 1))
        while time.monotonic() < deadline:
            try:
                messages = await client.get_messages(getattr(self, "target_chat_id"), limit=40)
            except Exception as exc:
                if log:
                    log.info(f"Fishing poll [{identity}] failed for {command}: {exc}")
                return ""
            for msg in reversed(messages or []):
                try:
                    if (getattr(msg, "id", 0) or 0) <= int(sent_id):
                        continue
                    if meaningful_reply_to_msg_id(self, msg) != sent_id:
                        continue
                    text = self.fishing_response_text(msg)
                    if not text or not feedback_response_matches_command(command, text):
                        continue
                    sender = await msg.get_sender()
                    if sender is not None and not is_game_bot_sender(self, sender):
                        continue
                    record_bot_response(self)
                    await log_incoming_message(
                        self,
                        command,
                        text,
                        msg=msg,
                        logger=log,
                        identity=identity,
                    )
                    if log:
                        log.info(
                            f"Fishing poll [{identity}] matched {command} reply: "
                            f"command_msg={sent_id}, response_msg={getattr(msg, 'id', None)}."
                        )
                    return msg
                except Exception:
                    continue
            await asyncio.sleep(1)
        return ""

    def fishing_response_text(self, response):
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        return str(getattr(response, "text", "") or getattr(response, "raw_text", "") or "")

    def fishing_set_status(self, identity, status, detail="", next_seconds=None, response=""):
        state = self.get_fishing_state(identity)
        state["last_status"] = status
        state["last_detail"] = str(detail or "")[:300]
        if response:
            state["last_response"] = str(response or "")[:500]
        if next_seconds is not None:
            state["next_action_at"] = add_seconds_str(now_str(), next_seconds)
        self.save_state()

    def fishing_wait_from_state(self, identity, default_seconds=FISHING_RETRY_SECONDS):
        state = self.get_fishing_state(identity)
        active_due = state.get("active_due_at", "")
        if state.get("active") and active_due and is_future(active_due):
            return max(1, min(seconds_until(active_due), 300))
        next_action = state.get("next_action_at", "")
        if next_action and is_future(next_action):
            return max(1, min(seconds_until(next_action), 300))
        return default_seconds

    def fishing_impending_wait(self, identity):
        return self.fishing_impending_wait_for_identity(identity)

    def fishing_impending_wait_for_identity(self, identity):
        try:
            if hasattr(self, "_state_impending_command_wait"):
                target = self.state if identity == "主魂" else self.get_avatar_state(identity)
                state_without_meditation = dict(target or {})
                state_without_meditation["in_deep_meditation"] = True
                state_without_meditation["deep_meditation_end_time"] = ""
                state_without_meditation["next_meditation_retry_time"] = ""
                wait = self._state_impending_command_wait(state_without_meditation, identity=identity)
                if hasattr(self, "custom_command_impending_wait"):
                    custom_wait = self.custom_command_impending_wait(identity)
                    if hasattr(self, "merge_impending_wait"):
                        wait = self.merge_impending_wait(wait, custom_wait)
                    else:
                        wait = min(wait, custom_wait)
            else:
                wait = self.get_identity_impending_command_wait(identity)
        except Exception:
            return -1
        if wait is None:
            return -1
        try:
            return float(wait)
        except Exception:
            return -1

    def fishing_other_identity_impending_wait(self, identity):
        identities = ["主魂"]
        identities.extend(list(getattr(self, "avatars", []) or []))
        best_identity = ""
        best_wait = None
        for other in identities:
            if other == identity:
                continue
            try:
                if self.identity_pause_seconds(other) > 0:
                    continue
            except Exception:
                pass
            wait = self.fishing_impending_wait_for_identity(other)
            if wait is None or wait < 0:
                continue
            if best_wait is None or wait < best_wait:
                best_wait = wait
                best_identity = other
        if best_wait is None:
            return "", -1
        return best_identity, float(best_wait)

    def fishing_recently_yielded_to_other_identity(self, identity):
        state = self.get_fishing_state(identity)
        last_at = state.get("last_cross_identity_yield_at", "")
        if not last_at:
            return False
        try:
            return seconds_until(add_seconds_str(last_at, FISHING_CROSS_IDENTITY_YIELD_COOLDOWN_SECONDS)) > 0
        except Exception:
            return False

    def fishing_active_switch_wait(self, identity, target_identity="", command=""):
        """Return seconds to delay switching away from an unfinished fishing round."""
        identity = str(identity or "主魂").strip() or "主魂"
        target_identity = str(target_identity or "").strip()
        if target_identity and target_identity == identity:
            return 0
        if str(command or "").strip() == ".提竿":
            return 0
        try:
            state = self.get_fishing_state(identity)
        except Exception:
            return 0
        if not state.get("active"):
            return 0
        due_at = state.get("active_due_at", "")
        if due_at and is_future(due_at):
            return max(1, min(
                seconds_until(due_at) + FISHING_ACTIVE_SWITCH_BUFFER_SECONDS,
                300,
            ))
        return FISHING_ACTIVE_SWITCH_BUFFER_SECONDS

    def fishing_active_due_for_switch(self, identity, target_identity="", command=""):
        """Return True when a switch is waiting on a fishing round that is already due."""
        identity = str(identity or "主魂").strip() or "主魂"
        target_identity = str(target_identity or "").strip()
        if target_identity and target_identity == identity:
            return False
        if str(command or "").strip() == ".提竿":
            return False
        try:
            state = self.get_fishing_state(identity)
        except Exception:
            return False
        if not state.get("active"):
            return False
        due_at = state.get("active_due_at", "")
        return bool(due_at and not is_future(due_at))

    async def fishing_switch_wait_or_raise_due(self, identity, target_identity="", command=""):
        """
        Return switch wait seconds, raising an overdue rod first when the current
        identity is already allowed to finish the active fishing round.

        Call this only while the caller holds avatar_send_lock and current_identity
        is still the fishing identity; the raw send path intentionally avoids
        reacquiring the same lock.
        """
        wait = self.fishing_active_switch_wait(identity, target_identity=target_identity, command=command)
        if wait <= 0:
            return wait
        if not self.fishing_active_due_for_switch(identity, target_identity=target_identity, command=command):
            return wait
        log = self.fishing_logger()
        if log:
            log.info(
                f"Fishing [{identity}] is overdue before switching to {target_identity or 'unknown'}; "
                "raising rod first."
            )
        await self.fishing_raise_rod_current_identity(identity)
        return self.fishing_active_switch_wait(identity, target_identity=target_identity, command=command)

    async def fishing_sync_basket(self, identity):
        resp = await self.send_fishing_command(identity, ".鱼篓", timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_fishing_basket(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if not parsed.get("matched"):
            self.fishing_set_status(identity, "sync_failed", "鱼篓回复未识别", FISHING_RETRY_SECONDS, text)
            return False
        for key in ("rod_owned", "skill", "skill_exp", "current_nest", "current_nest_remaining"):
            if parsed.get(key) is not None:
                state[key] = parsed.get(key)
        if parsed.get("today_count") is not None:
            state["today_count"] = parsed["today_count"]
        if parsed.get("daily_limit") is not None:
            state["daily_limit"] = parsed["daily_limit"]
        state["baits"] = parsed.get("baits", {})
        state["catches"] = parsed.get("catches", {})
        state["last_sync_date"] = _today()
        state["last_status"] = "synced"
        state["last_detail"] = f"今日竿数 {state.get('today_count', 0)}/{state.get('daily_limit', FISHING_DAILY_LIMIT)}"
        self.save_state()
        return True

    async def fishing_exchange_material(self, identity, material, count):
        material = str(material or "").strip()
        count = max(1, int(count or 1))
        if material not in FISHING_EXCHANGEABLE_MATERIALS:
            return False
        command = f".兑换 {material}*{count}"
        resp = await self.send_fishing_command(identity, command, timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_exchange_response(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if parsed.get("status") == "success":
            state["last_status"] = "material_exchanged"
            state["last_detail"] = f"兑换 {material} x{parsed.get('count') or count}"
            self.save_state()
            return True
        self.fishing_set_status(identity, "exchange_failed", f"{material} x{count} 兑换失败", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_resolve_missing_resources(self, identity, resources):
        resources = resources or []
        if not resources:
            return False
        resolved = False
        for item in resources:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            count = int(item.get("count") or 0)
            if not name or count <= 0:
                continue
            if name in FISHING_BAIT_NAMES:
                if not await self.fishing_buy_bait(identity, name, count):
                    return False
                resolved = True
                continue
            if name in FISHING_EXCHANGEABLE_MATERIALS:
                if not await self.fishing_exchange_material(identity, name, count):
                    return False
                resolved = True
                continue
            return False
        return resolved

    async def fishing_buy_bait(self, identity, bait, count, _resolved_resources=False):
        count = max(1, int(count or 1))
        command = f".买鱼饵 {bait} {count}"
        resp = await self.send_fishing_command(identity, command, timeout=90)
        text = self.fishing_response_text(resp)
        parsed = parse_buy_bait(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if parsed.get("status") == "success":
            baits = state.setdefault("baits", {})
            baits[bait] = int(baits.get(bait, 0)) + int(parsed.get("count") or count)
            state["last_status"] = "bait_bought"
            state["last_detail"] = f"购入 {bait} x{parsed.get('count') or count}"
            self.save_state()
            return True
        if (
            parsed.get("status") == "insufficient_resource"
            and not _resolved_resources
            and await self.fishing_resolve_missing_resources(identity, parsed.get("missing_resources") or [])
        ):
            return await self.fishing_buy_bait(identity, bait, count, _resolved_resources=True)
        self.fishing_set_status(identity, "bait_buy_failed", f"{bait} x{count} 购买失败", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_ensure_daily_bait(self, identity):
        state = self.get_fishing_state(identity)
        bait = fishing_bait_for_state(state)
        today_count = int(state.get("today_count") or 0)
        daily_limit = int(state.get("daily_limit") or FISHING_DAILY_LIMIT)
        needed = max(0, daily_limit - today_count)
        if needed <= 0:
            return True
        if state.get("bait_purchase_done") and int(state.get("baits", {}).get(bait, 0)) > 0:
            return True
        current = int(state.get("baits", {}).get(bait, 0))
        buy_count = max(0, needed - current)
        if buy_count <= 0:
            state["bait_purchase_done"] = True
            self.save_state()
            return True
        if not await self.fishing_buy_bait(identity, bait, buy_count):
            return False
        state = self.get_fishing_state(identity)
        state["bait_purchase_date"] = _today()
        state["bait_purchase_done"] = True
        self.save_state()
        return True

    def fishing_next_nest(self, identity):
        state = self.get_fishing_state(identity)
        if state.get("current_nest") and int(state.get("current_nest_remaining") or 0) > 0:
            return ""
        counts = state.setdefault("nest_counts", {})
        blocked = state.setdefault("nest_blocked", {})
        today = _today()
        for nest, limit in FISHING_NEST_PLAN:
            if blocked.get(nest) == today:
                continue
            if int(counts.get(nest, 0)) < limit:
                return nest
        return ""

    async def fishing_try_nest(self, identity):
        nest = self.fishing_next_nest(identity)
        if not nest:
            return True
        resp = await self.send_fishing_command(identity, f".打窝 {nest}", timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_nest_response(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if parsed.get("status") == "success":
            nest_name = parsed.get("nest") or nest
            state["current_nest"] = nest_name
            state["current_nest_remaining"] = int(parsed.get("remaining") or 0)
            counts = state.setdefault("nest_counts", {})
            counts[nest_name] = int(counts.get(nest_name, 0)) + 1
            state["last_status"] = "nested"
            state["last_detail"] = f"{nest_name} 剩余 {state['current_nest_remaining']} 竿"
            self.save_state()
            return True
        if parsed.get("status") == "already_active":
            nest_name = parsed.get("nest") or nest
            state["current_nest"] = nest_name
            state["current_nest_remaining"] = int(parsed.get("remaining") or 0)
            counts = state.setdefault("nest_counts", {})
            counts[nest_name] = max(int(counts.get(nest_name, 0)), 1)
            state["last_status"] = "nested"
            state["last_detail"] = f"{nest_name} 剩余 {state['current_nest_remaining']} 竿（已有窝料）"
            self.save_state()
            return True
        if parsed.get("status") == "missing_resource":
            missing_name = parsed.get("missing_name", "")
            missing_count = int(parsed.get("missing_count") or 0)
            resources = parsed.get("missing_resources") or []
            if not resources and missing_name and missing_count > 0:
                resources = [{"name": missing_name, "count": missing_count}]
            if await self.fishing_resolve_missing_resources(identity, resources):
                return await self.fishing_try_nest(identity)
            if any((item.get("name") in FISHING_BAIT_NAMES or item.get("name") in FISHING_EXCHANGEABLE_MATERIALS) for item in resources if isinstance(item, dict)):
                return False
            state.setdefault("nest_blocked", {})[nest] = _today()
            self.fishing_set_status(identity, "nest_blocked", f"{nest} 缺少 {missing_name}x{missing_count}", 60, text)
            return False
        if parsed.get("status") == "daily_used_up":
            limit = dict(FISHING_NEST_PLAN).get(nest, 1)
            state.setdefault("nest_counts", {})[nest] = limit
            state["last_status"] = "nest_used_up"
            state["last_detail"] = f"{nest} 今日次数已尽"
            self.save_state()
            return True
        if parsed.get("matched"):
            state.setdefault("nest_blocked", {})[nest] = _today()
            self.fishing_set_status(identity, "nest_failed", f"{nest} 打窝失败", 60, text)
            return False
        self.fishing_set_status(identity, "nest_unrecognized", f"{nest} 回复未识别", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_start_round(self, identity):
        command = self.fishing_master_command(identity)
        bait = self.fishing_preferred_bait(identity)
        resp = await self.send_fishing_command(identity, command, timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_fishing_start(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        status = parsed.get("status")
        if status == "started":
            wait_seconds = max(5, int(parsed.get("wait_seconds") or 60)) + FISHING_ROUND_BUFFER_SECONDS
            state["active"] = True
            state["active_bait"] = parsed.get("bait") or bait
            state["active_started_at"] = now_str()
            state["active_due_at"] = add_seconds_str(now_str(), wait_seconds)
            baits = state.setdefault("baits", {})
            baits[bait] = max(0, int(baits.get(bait, 0)) - 1)
            state["last_status"] = "fishing"
            state["last_detail"] = f"{state['active_bait']} 等鱼讯 {wait_seconds}秒"
            self.save_state()
            return True
        if status == "missing_bait":
            missing = parsed.get("missing_bait") or bait
            remaining = max(1, int(state.get("daily_limit") or FISHING_DAILY_LIMIT) - int(state.get("today_count") or 0))
            if await self.fishing_buy_bait(identity, missing, remaining):
                return await self.fishing_start_round(identity)
            return False
        if status == "daily_limit":
            state["today_count"] = parsed.get("today_count") or state.get("today_count", FISHING_DAILY_LIMIT)
            state["daily_limit"] = parsed.get("daily_limit") or state.get("daily_limit", FISHING_DAILY_LIMIT)
            state["active"] = False
            state["next_action_at"] = _next_day_action_time()
            state["last_status"] = "daily_done"
            state["last_detail"] = f"今日已垂钓 {state['today_count']}/{state['daily_limit']}"
            self.save_state()
            await self.fishing_sync_daily_done_basket(identity)
            return True
        if status == "no_rod":
            state["rod_owned"] = False
            state["active"] = False
            self.fishing_set_status(identity, "no_rod", "尚无青竹钓竿", 3600, text)
            return False
        if status == "already_active":
            state["active"] = True
            state["active_due_at"] = add_seconds_str(now_str(), 60)
            state["last_status"] = "recover_active"
            state["last_detail"] = "已有一竿，60秒后尝试提竿"
            self.save_state()
            return True
        self.fishing_set_status(identity, "start_unrecognized", "钓鱼回复未识别", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_sync_daily_done_basket(self, identity):
        state = self.get_fishing_state(identity)
        today = _today()
        if state.get("daily_done_basket_sync_date") == today:
            if fishing_daily_done_for_today(state, today=today):
                await self.fishing_finish_daily_done(identity, reason="daily limit already synced")
                return True
            state["daily_done_basket_sync_date"] = ""
            state["last_status"] = "synced"
            state["last_detail"] = (
                f"鱼篓校准 {state.get('today_count')}/{state.get('daily_limit')}，继续钓鱼"
            )
            state["next_action_at"] = ""
            self.save_state()
            return False
        ok = await self.fishing_sync_basket(identity)
        state = self.get_fishing_state(identity)
        if not ok:
            return False
        if not fishing_daily_done_for_today(state, today=today):
            state["daily_done_basket_sync_date"] = ""
            state["last_status"] = "synced"
            state["last_detail"] = (
                f"鱼篓校准 {state.get('today_count')}/{state.get('daily_limit')}，继续钓鱼"
            )
            state["next_action_at"] = ""
            self.save_state()
            return False
        state["daily_done_basket_sync_date"] = today
        state["last_status"] = "daily_done"
        state["last_detail"] = f"今日已垂钓 {state.get('today_count')}/{state.get('daily_limit')}"
        state["next_action_at"] = _next_day_action_time()
        self.save_state()
        await self.fishing_finish_daily_done(identity, reason="daily limit reached")
        return True

    def fishing_auto_identities(self):
        identities = ["主魂"]
        identities.extend(list(getattr(self, "avatars", []) or []))
        return list(dict.fromkeys(str(item or "主魂").strip() or "主魂" for item in identities))

    def fishing_auto_set_status(self, status, detail="", next_seconds=None, response=""):
        state = self.get_fishing_auto_state()
        state["last_status"] = status
        state["last_detail"] = str(detail or "")[:300]
        if response:
            state["last_response"] = str(response or "")[:500]
        if next_seconds is not None:
            state["next_action_at"] = add_seconds_str(now_str(), next_seconds)
        self.save_state()

    def fishing_auto_global_account_states(self):
        states = {}
        current_account = self.fishing_account_key()
        for account in FISHING_AUTO_CONTROL_ACCOUNTS:
            if account == current_account:
                states[account] = getattr(self, "state", {}) if isinstance(getattr(self, "state", None), dict) else {}
            else:
                states[account] = _load_fishing_auto_account_root_state(account)
        return states

    def fishing_auto_global_snapshot(self):
        states = self.fishing_auto_global_account_states()
        completed = {}
        pending = []
        today = _today()
        for account in FISHING_AUTO_CONTROL_ACCOUNTS:
            root = states.get(account, {})
            identities = FISHING_AUTO_ACCOUNT_IDENTITIES.get(account, ("主魂",))
            if account == self.fishing_account_key():
                identities = tuple(self.fishing_auto_identities())
            for identity in identities:
                fishing = _fishing_auto_identity_fishing_state(root, identity)
                key = _fishing_auto_identity_key(account, identity)
                if fishing_daily_done_for_today(fishing, today=today):
                    completed[key] = today
                else:
                    pending.append({"account": account, "identity": identity, "key": key})
        return {"completed": completed, "pending": pending}

    def fishing_auto_update_global_progress(self, bait=None, rod_holder=None):
        snapshot = self.fishing_auto_global_snapshot()
        lock = _acquire_fishing_auto_global_lock()
        try:
            data = _load_fishing_auto_global_state()
            data["completed"] = snapshot["completed"]
            if bait in FISHING_CONTROL_BAITS:
                data["preferred_bait"] = bait
            if isinstance(rod_holder, dict) and rod_holder.get("account") and rod_holder.get("identity"):
                data["rod_holder"] = {
                    "account": rod_holder.get("account"),
                    "identity": rod_holder.get("identity"),
                    "updated_at": now_str(),
                }
            active = data.get("active") if isinstance(data.get("active"), dict) else {}
            active_key = _fishing_auto_identity_key(active.get("account"), active.get("identity"))
            pending_keys = {item["key"] for item in snapshot["pending"]}
            if active_key not in pending_keys:
                holder = data.get("rod_holder") if isinstance(data.get("rod_holder"), dict) else {}
                holder_key = _fishing_auto_identity_key(holder.get("account"), holder.get("identity"))
                next_item = next(
                    (item for item in snapshot["pending"] if item["key"] == holder_key),
                    snapshot["pending"][0] if snapshot["pending"] else {},
                )
                data["active"] = {
                    "account": next_item.get("account", ""),
                    "identity": next_item.get("identity", ""),
                    "key": next_item.get("key", ""),
                    "updated_at": now_str(),
                } if next_item else {}
            _save_fishing_auto_global_state(data)
            return data, snapshot
        finally:
            _release_command_control_lock(lock)

    def fishing_auto_global_active_target(self, bait=None):
        data, snapshot = self.fishing_auto_update_global_progress(bait=bait)
        active = data.get("active") if isinstance(data.get("active"), dict) else {}
        if not active and snapshot["pending"]:
            active = snapshot["pending"][0]
        return data, snapshot, active

    def fishing_auto_current_global_transfer(self):
        data = _load_fishing_auto_global_state()
        transfer = data.get("transfer") if isinstance(data.get("transfer"), dict) else {}
        return data, transfer

    def fishing_auto_clear_global_transfer(self):
        lock = _acquire_fishing_auto_global_lock()
        try:
            data = _load_fishing_auto_global_state()
            data["transfer"] = {}
            _save_fishing_auto_global_state(data)
        finally:
            _release_command_control_lock(lock)

    def fishing_auto_pending_identities(self):
        pending = []
        completed = {}
        today = _today()
        for identity in self.fishing_auto_identities():
            state = self.get_fishing_state(identity)
            if fishing_daily_done_for_today(state, today=today):
                completed[identity] = today
                continue
            pending.append(identity)
        auto_state = self.get_fishing_auto_state()
        auto_state["completed"] = completed
        return pending

    async def fishing_auto_find_rod_holder(self, identities=None, scan=False):
        identities = identities or self.fishing_auto_identities()
        auto_state = self.get_fishing_auto_state()
        candidate = str(auto_state.get("rod_holder") or "").strip()
        if candidate and candidate in identities:
            try:
                if self.get_fishing_state(candidate).get("rod_owned") is True:
                    return candidate
            except Exception:
                pass
        for identity in identities:
            try:
                if self.get_fishing_state(identity).get("rod_owned") is True:
                    auto_state["rod_holder"] = identity
                    self.save_state()
                    return identity
            except Exception:
                continue
        if not scan:
            return ""
        for identity in identities:
            try:
                await self.fishing_sync_basket(identity)
                if self.get_fishing_state(identity).get("rod_owned") is True:
                    auto_state["rod_holder"] = identity
                    self.save_state()
                    return identity
            except Exception as exc:
                log = self.fishing_logger()
                if log:
                    log.info(f"Fishing auto rod scan [{identity}] failed: {exc}")
        return ""

    async def fishing_auto_publish_global_listing(self, holder, target, _resolved_resources=False):
        if not isinstance(holder, dict) or not isinstance(target, dict):
            return False
        if target.get("account") != self.fishing_account_key():
            return False
        target_identity = str(target.get("identity") or "").strip()
        if not target_identity:
            return False
        data, transfer = self.fishing_auto_current_global_transfer()
        if (
            transfer.get("status") in {"listed", "purchased"}
            and transfer.get("from_account") == holder.get("account")
            and transfer.get("from_identity") == holder.get("identity")
            and transfer.get("to_account") == target.get("account")
            and transfer.get("to_identity") == target_identity
            and transfer.get("listing_id")
        ):
            return True

        self.fishing_auto_set_status(
            "transferring",
            f"{holder.get('account')}[{holder.get('identity')}] -> {target_identity} 转移鱼竿：等待挂单",
            FISHING_AUTO_RETRY_SECONDS,
        )
        listing_command = f".上架 {FISHING_ROD_LISTING_MATERIAL} 换 {FISHING_ROD_ITEM}*1"
        listing_resp = await self.send_fishing_command(target_identity, listing_command, timeout=90)
        listing_text = self.fishing_response_text(listing_resp)
        listing = parse_trade_listing_response(listing_text)
        if (
            listing.get("status") == "insufficient_resource"
            and not _resolved_resources
            and await self.fishing_resolve_missing_resources(target_identity, listing.get("missing_resources") or [])
        ):
            return await self.fishing_auto_publish_global_listing(holder, target, _resolved_resources=True)
        listing_id = str(listing.get("listing_id") or "").strip()
        if listing.get("status") != "success" or not listing_id:
            self.fishing_auto_set_status(
                "transfer_failed",
                f"{target_identity} 上架换鱼竿失败或未识别挂单ID",
                FISHING_AUTO_RETRY_SECONDS,
                listing_text,
            )
            return False

        lock = _acquire_fishing_auto_global_lock()
        try:
            data = _load_fishing_auto_global_state()
            data["transfer"] = {
                "status": "listed",
                "from_account": holder.get("account", ""),
                "from_identity": holder.get("identity", ""),
                "to_account": target.get("account", ""),
                "to_identity": target_identity,
                "listing_id": listing_id,
                "started_at": transfer.get("started_at") or now_str(),
                "updated_at": now_str(),
            }
            _save_fishing_auto_global_state(data)
        finally:
            _release_command_control_lock(lock)
        self.fishing_auto_set_status(
            "transferring",
            f"已上架换鱼竿挂单 {listing_id}，等待 {holder.get('account')}[{holder.get('identity')}] 购买",
            FISHING_AUTO_RETRY_SECONDS,
            listing_text,
        )
        return True

    async def fishing_auto_handle_global_purchase(self):
        account = self.fishing_account_key()
        data, transfer = self.fishing_auto_current_global_transfer()
        if transfer.get("status") != "listed" or transfer.get("from_account") != account:
            return False
        holder = str(transfer.get("from_identity") or "").strip()
        listing_id = str(transfer.get("listing_id") or "").strip()
        if not holder or not listing_id:
            return False
        self.fishing_auto_set_status(
            "transferring",
            f"{holder} 购买跨账号鱼竿挂单 {listing_id}",
            FISHING_AUTO_RETRY_SECONDS,
        )
        purchase_resp = await self.send_fishing_command(holder, f".购买 {listing_id}", timeout=90)
        purchase_text = self.fishing_response_text(purchase_resp)
        purchase = parse_trade_purchase_response(purchase_text)
        if purchase.get("status") != "success":
            lock = _acquire_fishing_auto_global_lock()
            try:
                data = _load_fishing_auto_global_state()
                data["transfer"] = {
                    **transfer,
                    "status": "purchase_failed",
                    "failed_at": now_str(),
                    "updated_at": now_str(),
                }
                _save_fishing_auto_global_state(data)
            finally:
                _release_command_control_lock(lock)
            self.fishing_auto_set_status(
                "transfer_failed",
                f"{holder} 购买挂单 {listing_id} 失败",
                FISHING_AUTO_RETRY_SECONDS,
                purchase_text,
            )
            return True

        self.get_fishing_state(holder)["rod_owned"] = False
        lock = _acquire_fishing_auto_global_lock()
        try:
            data = _load_fishing_auto_global_state()
            data["rod_holder"] = {
                "account": transfer.get("to_account", ""),
                "identity": transfer.get("to_identity", ""),
                "updated_at": now_str(),
            }
            data["transfer"] = {
                **transfer,
                "status": "purchased",
                "purchased_at": now_str(),
                "updated_at": now_str(),
            }
            _save_fishing_auto_global_state(data)
        finally:
            _release_command_control_lock(lock)
        self.save_state()
        self.fishing_auto_set_status(
            "transferred",
            f"鱼竿已购买给 {transfer.get('to_account')}[{transfer.get('to_identity')}]",
            5,
            purchase_text,
        )
        return True

    def fishing_auto_adopt_purchased_global_rod(self, target):
        if not isinstance(target, dict) or target.get("account") != self.fishing_account_key():
            return False
        data, transfer = self.fishing_auto_current_global_transfer()
        if (
            transfer.get("status") != "purchased"
            or transfer.get("to_account") != target.get("account")
            or transfer.get("to_identity") != target.get("identity")
        ):
            return False
        identity = str(target.get("identity") or "").strip()
        if not identity:
            return False
        state = self.get_fishing_state(identity)
        state["rod_owned"] = True
        state["last_sync_date"] = _today()
        auto_state = self.get_fishing_auto_state()
        auto_state["rod_holder"] = identity
        auto_state["active_identity"] = identity
        auto_state["last_status"] = "transferred"
        auto_state["last_detail"] = f"跨账号鱼竿已转入 {identity}"
        auto_state["next_action_at"] = ""
        self.save_state()
        self.fishing_auto_clear_global_transfer()
        self.fishing_auto_update_global_progress(
            bait=fishing_auto_bait_for_state(auto_state),
            rod_holder={"account": self.fishing_account_key(), "identity": identity},
        )
        return True

    async def fishing_auto_transfer_rod(self, holder, target, _resolved_resources=False):
        holder = str(holder or "").strip()
        target = str(target or "").strip()
        if not holder or not target or holder == target:
            return False
        auto_state = self.get_fishing_auto_state()
        auto_state["transfer_from"] = holder
        auto_state["transfer_to"] = target
        auto_state["transfer_started_at"] = now_str()
        self.fishing_auto_set_status(
            "transferring",
            f"{holder} -> {target} 转移鱼竿：等待挂单",
            FISHING_AUTO_RETRY_SECONDS,
        )
        listing_command = f".上架 {FISHING_ROD_LISTING_MATERIAL} 换 {FISHING_ROD_ITEM}*1"
        listing_resp = await self.send_fishing_command(target, listing_command, timeout=90)
        listing_text = self.fishing_response_text(listing_resp)
        listing = parse_trade_listing_response(listing_text)
        auto_state["last_response"] = listing_text[:500]
        if (
            listing.get("status") == "insufficient_resource"
            and not _resolved_resources
            and await self.fishing_resolve_missing_resources(target, listing.get("missing_resources") or [])
        ):
            return await self.fishing_auto_transfer_rod(holder, target, _resolved_resources=True)
        listing_id = str(listing.get("listing_id") or "").strip()
        if listing.get("status") != "success" or not listing_id:
            self.fishing_auto_set_status(
                "transfer_failed",
                f"{target} 上架换鱼竿失败或未识别挂单ID",
                FISHING_AUTO_RETRY_SECONDS,
                listing_text,
            )
            return False

        auto_state["transfer_listing_id"] = listing_id
        self.fishing_auto_set_status(
            "transferring",
            f"{holder} -> {target} 转移鱼竿：购买挂单 {listing_id}",
            FISHING_AUTO_RETRY_SECONDS,
        )
        purchase_resp = await self.send_fishing_command(holder, f".购买 {listing_id}", timeout=90)
        purchase_text = self.fishing_response_text(purchase_resp)
        purchase = parse_trade_purchase_response(purchase_text)
        auto_state["last_response"] = purchase_text[:500]
        if purchase.get("status") != "success":
            self.fishing_auto_set_status(
                "transfer_failed",
                f"{holder} 购买挂单 {listing_id} 失败",
                FISHING_AUTO_RETRY_SECONDS,
                purchase_text,
            )
            return False

        self.get_fishing_state(holder)["rod_owned"] = False
        target_state = self.get_fishing_state(target)
        target_state["rod_owned"] = True
        target_state["last_sync_date"] = _today()
        auto_state["rod_holder"] = target
        auto_state["active_identity"] = target
        auto_state["transfer_from"] = ""
        auto_state["transfer_to"] = ""
        auto_state["transfer_listing_id"] = ""
        auto_state["last_status"] = "transferred"
        auto_state["last_detail"] = f"鱼竿已从 {holder} 转移给 {target}"
        auto_state["next_action_at"] = ""
        self.save_state()
        return True

    async def fishing_raise_rod_current_identity(self, identity):
        previous_last_sent_id = getattr(self, "last_sent_id", None)
        resp = await self._send_and_wait_feedback_raw(
            ".提竿",
            timeout=60,
            max_retries=0,
            suppress_no_response_alert=True,
            return_response_msg=True,
        )
        if not self.fishing_response_text(resp):
            sent_id = getattr(self, "last_sent_id", None)
            if sent_id and sent_id != previous_last_sent_id:
                polled = await self.fishing_poll_reply_to_sent_command(identity, ".提竿", sent_id)
                if polled:
                    resp = polled
        return await self.fishing_record_rod_response(identity, resp)

    async def fishing_raise_rod(self, identity):
        resp = await self.send_fishing_command(identity, ".提竿", timeout=60)
        return await self.fishing_record_rod_response(identity, resp)

    async def fishing_record_rod_response(self, identity, resp, finish_daily=True):
        text = self.fishing_response_text(resp)
        parsed = parse_rod_response(text)
        state = self.get_fishing_state(identity)
        msg_id = getattr(resp, "id", None)
        if msg_id is not None:
            try:
                msg_id = int(msg_id)
            except Exception:
                msg_id = str(msg_id)
            recorded = state.setdefault("recorded_rod_message_ids", [])
            if msg_id in recorded:
                if (
                    finish_daily
                    and parsed.get("matched")
                    and fishing_daily_done_for_today(state)
                ):
                    await self.fishing_sync_daily_done_basket(identity)
                return parsed.get("matched", False)
            if parsed.get("matched"):
                recorded.append(msg_id)
                if len(recorded) > 120:
                    del recorded[:-80]
        state["last_response"] = text[:500]
        state["active"] = False
        state["active_due_at"] = ""
        state["active_started_at"] = ""
        state["active_bait"] = ""
        counted_rod = parsed.get("status") in {"success", "empty"}
        if counted_rod:
            state["last_round_at"] = now_str()
            state["today_count"] = min(
                int(state.get("daily_limit") or FISHING_DAILY_LIMIT),
                int(state.get("today_count") or 0) + 1,
            )
            if state.get("current_nest") and int(state.get("current_nest_remaining") or 0) > 0:
                state["current_nest_remaining"] = max(0, int(state.get("current_nest_remaining") or 0) - 1)
                if state["current_nest_remaining"] <= 0:
                    state["current_nest"] = ""
        if parsed.get("status") == "success":
            state["last_status"] = "caught"
            state["last_catch"] = parsed.get("catch", "")
            if state["last_catch"]:
                daily_catches = state.setdefault("daily_catches", {})
                daily_catches[state["last_catch"]] = int(daily_catches.get(state["last_catch"], 0)) + 1
            if parsed.get("loot"):
                daily_loot = state.setdefault("daily_loot", {})
                for name, count in parsed.get("loot", {}).items():
                    daily_loot[name] = int(daily_loot.get(name, 0)) + int(count or 0)
            state["last_detail"] = f"提竿成功{('：' + state['last_catch']) if state['last_catch'] else ''}"
            state["consecutive_empty"] = 0
        elif parsed.get("status") == "empty":
            state["last_status"] = "empty"
            state["last_detail"] = "空竿"
            state["consecutive_empty"] = int(state.get("consecutive_empty") or 0) + 1
        else:
            state["last_status"] = "raise_unrecognized"
            state["last_detail"] = "提竿回复未识别"
            state["consecutive_empty"] = int(state.get("consecutive_empty") or 0) + 1
        if int(state.get("today_count") or 0) >= int(state.get("daily_limit") or FISHING_DAILY_LIMIT):
            state["next_action_at"] = _next_day_action_time()
        else:
            state["next_action_at"] = add_seconds_str(now_str(), 5)
        self.save_state()
        if finish_daily and int(state.get("today_count") or 0) >= int(state.get("daily_limit") or FISHING_DAILY_LIMIT):
            await self.fishing_sync_daily_done_basket(identity)
        return parsed.get("matched", False)

    async def maybe_record_fishing_rod_message(self, msg, text, sender=None):
        if sender is not None and not is_game_bot_sender(self, sender):
            return False
        parsed = parse_rod_response(text)
        if not parsed.get("matched"):
            return False
        command = tracked_command_text_for_reply(self, msg)
        if str(command or "").strip() != ".提竿":
            return False
        identity = (
            tracked_command_identity_for_reply(self, msg)
            or getattr(self, "current_identity", None)
            or "主魂"
        )
        return await self.fishing_record_rod_response(identity, msg, finish_daily=False)

    async def fishing_tick(self, identity, ignore_dashboard=False):
        if not ignore_dashboard and self.fishing_auto_command_is_enabled():
            state = self.get_fishing_state(identity)
            if state.get("last_status") != "auto_managed":
                self.fishing_set_status(identity, "auto_managed", "由全自动钓鱼队列接管", None)
            return FISHING_DISABLED_SLEEP_SECONDS
        if not ignore_dashboard and not self.fishing_command_is_enabled(identity):
            state = self.get_fishing_state(identity)
            if state.get("last_status") != "paused" or state.get("last_detail") != "dashboard 默认暂停/未启用":
                self.fishing_set_status(identity, "paused", "dashboard 默认暂停/未启用", None)
            return FISHING_DISABLED_SLEEP_SECONDS
        if self.identity_pause_seconds(identity) > 0:
            return 60

        state = self.get_fishing_state(identity)
        if state.get("last_status") == "meditation_blocked":
            state["last_status"] = "enabled"
            state["last_detail"] = "深度闭关不阻塞钓鱼"
            state["next_action_at"] = ""
            self.save_state()

        today = _today()
        if state.get("last_sync_date") != today or state.get("rod_owned") is None:
            await self.fishing_sync_basket(identity)
            return 5

        if state.get("rod_owned") is False:
            self.fishing_set_status(identity, "no_rod", "尚无青竹钓竿", 3600)
            return 3600

        if int(state.get("today_count") or 0) >= int(state.get("daily_limit") or FISHING_DAILY_LIMIT):
            await self.fishing_sync_daily_done_basket(identity)
            return self.fishing_wait_from_state(identity, 3600)

        if state.get("active"):
            due_at = state.get("active_due_at", "")
            if due_at and is_future(due_at):
                return max(1, min(seconds_until(due_at), 300))
            await self.fishing_raise_rod(identity)
            return 5

        next_action = state.get("next_action_at", "")
        if next_action and is_future(next_action):
            return max(1, min(seconds_until(next_action), 300))

        other_identity, other_wait = self.fishing_other_identity_impending_wait(identity)
        if (
            other_identity
            and 0 <= other_wait <= FISHING_IMPENDING_GUARD_SECONDS
            and not self.fishing_recently_yielded_to_other_identity(identity)
        ):
            state["last_cross_identity_yield_at"] = now_str()
            self.fishing_set_status(
                identity,
                "yielding_identity",
                f"本轮提竿后让路给 {other_identity} 的到期指令",
                FISHING_CROSS_IDENTITY_YIELD_SECONDS,
            )
            return FISHING_CROSS_IDENTITY_YIELD_SECONDS

        impending = self.fishing_impending_wait(identity)
        if 0 <= impending <= FISHING_IMPENDING_GUARD_SECONDS:
            self.fishing_set_status(
                identity,
                "yielding",
                f"让路给 {impending:.0f}秒内到期的其他指令",
                max(10, min(int(impending) + 10, 120)),
            )
            return max(10, min(int(impending) + 10, 120))

        if not await self.fishing_ensure_daily_bait(identity):
            return FISHING_RETRY_SECONDS
        if not await self.fishing_try_nest(identity):
            return 60
        await self.fishing_start_round(identity)
        return self.fishing_wait_from_state(identity, 60)

    async def fishing_auto_tick(self):
        if not self.fishing_auto_command_is_enabled():
            state = self.get_fishing_auto_state()
            if state.get("last_status") != "paused":
                self.fishing_auto_set_status("paused", "dashboard 默认暂停/未启用", None)
            return FISHING_AUTO_DISABLED_SLEEP_SECONDS

        auto_state = self.get_fishing_auto_state()
        bait = fishing_auto_bait_for_state(auto_state)
        identities = self.fishing_auto_identities()

        if await self.fishing_auto_handle_global_purchase():
            return 5

        global_state, global_snapshot, target = self.fishing_auto_global_active_target(bait=bait)

        self.fishing_auto_pending_identities()
        if not global_snapshot.get("pending"):
            auto_state["last_status"] = "done"
            auto_state["last_detail"] = "今日全自动钓鱼全部完成"
            auto_state["active_identity"] = ""
            auto_state["next_action_at"] = _next_day_action_time()
            self.save_state()
            self.fishing_auto_pause_dashboard_command(reason="all identities daily limit reached")
            return self.fishing_wait_from_state("主魂", 3600)

        account = self.fishing_account_key()
        target_account = str(target.get("account") or "").strip()
        target_identity = str(target.get("identity") or "").strip()
        if not target_account or not target_identity:
            self.fishing_auto_set_status("waiting", "等待全局钓鱼队列", 60)
            return 60
        if target_account != account:
            self.fishing_auto_set_status(
                "waiting",
                f"全局队列当前轮到 {target_account}[{target_identity}]",
                60,
            )
            return 60

        global_holder = global_state.get("rod_holder") if isinstance(global_state.get("rod_holder"), dict) else {}
        if (
            global_holder.get("account")
            and global_holder.get("identity")
            and global_holder.get("account") != account
        ):
            await self.fishing_auto_publish_global_listing(global_holder, target)
            return 5

        try:
            if self.identity_pause_seconds(target_identity) > 0:
                self.fishing_auto_set_status("waiting", f"等待 {target_identity} 暂停结束", 300)
                return 300
        except Exception:
            pass

        target_state = self.get_fishing_state(target_identity)
        if fishing_bait_for_state(target_state) != bait:
            target_state["preferred_bait"] = bait
            target_state["bait_purchase_done"] = False
            self.save_state()

        if target_state.get("rod_owned") is not True:
            self.fishing_auto_adopt_purchased_global_rod(target)
        if target_state.get("rod_owned") is not True:
            local_holder = await self.fishing_auto_find_rod_holder(identities, scan=False)
            if not local_holder:
                local_holder = await self.fishing_auto_find_rod_holder(identities, scan=True)
            if local_holder:
                global_state, global_snapshot = self.fishing_auto_update_global_progress(
                    bait=bait,
                    rod_holder={"account": account, "identity": local_holder},
                )
            holder = local_holder
            if holder and holder != target_identity:
                if not await self.fishing_auto_transfer_rod(holder, target_identity):
                    return FISHING_AUTO_RETRY_SECONDS
                self.fishing_auto_update_global_progress(
                    bait=bait,
                    rod_holder={"account": account, "identity": target_identity},
                )
            elif holder == target_identity:
                target_state["rod_owned"] = True
                self.fishing_auto_update_global_progress(
                    bait=bait,
                    rod_holder={"account": account, "identity": target_identity},
                )
            else:
                if (
                    global_holder.get("account")
                    and global_holder.get("identity")
                    and global_holder.get("account") != account
                ):
                    await self.fishing_auto_publish_global_listing(global_holder, target)
                    return 60
                self.fishing_auto_set_status("no_rod_holder", "未找到当前鱼竿持有者", FISHING_AUTO_RETRY_SECONDS)
                return FISHING_AUTO_RETRY_SECONDS

        auto_state = self.get_fishing_auto_state()
        auto_state["active_identity"] = target_identity
        auto_state["rod_holder"] = target_identity
        auto_state["last_status"] = "running"
        auto_state["last_detail"] = f"当前 {target_identity} 使用 {bait} 钓鱼"
        auto_state["next_action_at"] = ""
        self.save_state()
        wait = await self.fishing_tick(target_identity, ignore_dashboard=True)
        if fishing_daily_done_for_today(self.get_fishing_state(target_identity)):
            auto_state = self.get_fishing_auto_state()
            auto_state.setdefault("completed", {})[target_identity] = _today()
            auto_state["last_detail"] = f"{target_identity} 今日钓鱼完成，准备下一个身份"
            auto_state["next_action_at"] = ""
            self.save_state()
            self.fishing_auto_update_global_progress(bait=bait)
            return 5
        return wait

    async def run_fishing_auto_loop(self, initial_delay=0):
        await self.startup_done.wait()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        log = self.fishing_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                wait_seconds = await self.fishing_auto_tick()
                if log:
                    log.info(f"Fishing auto loop sleeping {int(wait_seconds)}s.")
                if (
                    not self.fishing_auto_command_is_enabled()
                    and hasattr(self, "wait_for_dashboard_command_control_change")
                ):
                    changed = await self.wait_for_dashboard_command_control_change(
                        max(10, min(int(wait_seconds), FISHING_AUTO_DISABLED_SLEEP_SECONDS))
                    )
                    if not changed:
                        await asyncio.sleep(1)
                else:
                    await asyncio.sleep(max(1, min(int(wait_seconds), 300)))
            except Exception as exc:
                if log:
                    log.error(f"Fishing auto loop error: {exc}", exc_info=True)
                await asyncio.sleep(FISHING_AUTO_RETRY_SECONDS)

    async def run_fishing_loop(self, identity="主魂", initial_delay=0):
        await self.startup_done.wait()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        log = self.fishing_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                wait_seconds = await self.fishing_tick(identity)
                if log:
                    log.info(f"Fishing loop [{identity}] sleeping {int(wait_seconds)}s.")
                if (
                    not self.fishing_command_is_enabled(identity)
                    and hasattr(self, "wait_for_dashboard_command_control_change")
                ):
                    changed = await self.wait_for_dashboard_command_control_change(
                        max(10, min(int(wait_seconds), FISHING_DISABLED_SLEEP_SECONDS))
                    )
                    if not changed:
                        await self.fishing_sleep(identity, 1)
                else:
                    await self.fishing_sleep(identity, wait_seconds)
            except Exception as exc:
                if log:
                    log.error(f"Fishing loop [{identity}] error: {exc}", exc_info=True)
                await asyncio.sleep(FISHING_RETRY_SECONDS)
