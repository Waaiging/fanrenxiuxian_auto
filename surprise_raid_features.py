#!/usr/bin/env python3
"""Shared surprise-raid treasure scheduler.

The raider temporarily equips 风雷翅, the target account emits a fresh
.切换 anchor, the raider replies to that exact message with .奇袭 夺宝,
and then the wing is scattered and returned to 万宝阁.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

try:
    import fcntl
except ImportError:  # Windows development/tests
    fcntl = None

from duel_features import (
    DUEL_IDENTITIES,
    duel_identity_config,
    duel_identity_options,
    refresh_duel_identity_roster,
)
from log_utils import send_text_alert


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
SURPRISE_RAID_STATE_FILE = os.path.join(CONFIG_DIR, "surprise_raid_state.json")
SURPRISE_RAID_LOCK_FILE = os.path.join(
    CONFIG_DIR, "surprise_raid_state.lock"
)

SURPRISE_RAID_INTERVAL_SECONDS = 2 * 60 * 60
SURPRISE_RAID_INTERVAL_MIN_SECONDS = 10 * 60
SURPRISE_RAID_INTERVAL_MAX_SECONDS = 7 * 24 * 3600
SURPRISE_RAID_LEASE_SECONDS = 8 * 60
SURPRISE_RAID_RESULT_WAIT_SECONDS = 90
SURPRISE_RAID_POLL_SECONDS = 5
SURPRISE_RAID_STATE_VERSION = 1

SURPRISE_RAID_ITEM = "风雷翅"
SURPRISE_RAID_COMMAND = ".奇袭 夺宝"

_COOLDOWN_PATTERNS = (
    re.compile(r"(\d+)\s*(?:小时|h)"),
    re.compile(r"(\d+)\s*(?:分钟|分|m)"),
    re.compile(r"(\d+)\s*(?:秒|s)"),
)


def parse_surprise_raid_cooldown_seconds(text):
    """Parse bot cooldown text such as ``请在 **3分钟15秒** 后再试``."""
    clean = str(text or "").replace("**", "")
    if not any(keyword in clean for keyword in ("后再试", "后再", "冷却", "尚需", "还需")):
        return 0
    total = 0
    found = False
    for pattern in _COOLDOWN_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        found = True
        if pattern is _COOLDOWN_PATTERNS[0]:
            total += int(match.group(1)) * 3600
        elif pattern is _COOLDOWN_PATTERNS[1]:
            total += int(match.group(1)) * 60
        else:
            total += int(match.group(1))
    return total if found else 0


def classify_surprise_raid_step(command, text):
    """Classify a known command response without inventing retries."""
    command = str(command or "").strip()
    clean = str(text or "").replace("**", "").strip()
    if not clean:
        return "timeout", "无回复", ""
    if "请在" in clean and ("后再试" in clean or "后再" in clean):
        seconds = parse_surprise_raid_cooldown_seconds(clean)
        return "cooldown", (f"冷却 {seconds} 秒" if seconds else "冷却中"), clean
    if command == ".从万宝阁取下 风雷翅":
        if "你已将【风雷翅】从万宝阁收回储物袋" in clean:
            return "success", "已取下风雷翅", clean
        if "你的万宝阁中并未陈列【风雷翅】" in clean:
            return "needs_equip", "风雷翅不在万宝阁，跳过取下直接装备", clean
        if any(k in clean for k in ("没有", "不存在", "未找到", "并未拥有")) and "风雷翅" in clean:
            return "missing_item", "储物袋没有风雷翅，跳过本轮", clean
    if command == ".装备 风雷翅":
        if "当前祭出：【风雷翅】" in clean or "你已祭出【风雷翅】" in clean:
            return "success", "风雷翅已装备", clean
        if "你的储物袋中没有【风雷翅】" in clean:
            return "missing_item", "储物袋没有风雷翅，无法装备，停止任务", clean
        if any(k in clean for k in ("没有", "不存在", "未找到", "并未拥有")) and "风雷翅" in clean:
            return "missing_item", "没有可装备的风雷翅，跳过本轮", clean
        if "并未稳定祭出【风雷翅】" in clean:
            return "not_equipped", "风雷翅装备未确认，跳过本轮", clean
    if command == SURPRISE_RAID_COMMAND:
        if "你的万宝阁中并未陈列【风雷翅】" in clean:
            return "needs_equip", "万宝阁未陈列风雷翅，需先装备", clean
        if "催动【风雷翅】，对 @" in clean and "施展【夺宝】之术" in clean:
            return "pending", "奇袭已发出，等待结算", clean
        if "【惊雷一击·夺宝】" in clean:
            return "success", "奇袭夺宝完成", clean
        # 奇袭失败是正常结果（对方识破）：视为本次已结算，本轮结束，不停止任务
        if "奇袭失败" in clean or "未能建功" in clean or "护体灵光" in clean:
            return "failure", "奇袭失败（对方识破），本轮结束", clean
        # 蓄势阶段：雷光闪烁→仍是等待最终结果的中间状态.不算未知回复
        if "雷光闪烁" in clean or "寻找对方的破绽" in clean or "奇袭成功率" in clean:
            return "pending", "蓄势待发，等待判定结果", clean
        if "并未稳定祭出【风雷翅】" in clean:
            return "not_equipped", "风雷翅不在装备状态，跳过本轮", clean
        if "消耗过巨" in clean and "请再" in clean:
            seconds = parse_surprise_raid_cooldown_seconds(clean)
            return "cooldown", (f"奇袭冷却 {seconds} 秒" if seconds else "奇袭冷却中"), clean
    if command == ".散念 风雷翅":
        if "你已散去对【风雷翅】的祭炼联系" in clean:
            return "success", "已散念风雷翅", clean
    if command == ".上架至万宝阁 风雷翅":
        if "放置在万宝阁" in clean or "上架成功" in clean or "交易挂单" in clean:
            return "success", "风雷翅已归还万宝阁", clean
    return "unknown", "未知回复", clean


def surprise_raid_now():
    return datetime.now()


def surprise_raid_time(value=None):
    return (value or surprise_raid_now()).strftime(TIME_FORMAT)


def parse_surprise_raid_time(value):
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except Exception:
        return None


def surprise_raid_default_state():
    now = surprise_raid_time()
    return {
        "version": SURPRISE_RAID_STATE_VERSION,
        "enabled": False,
        "interval_seconds": SURPRISE_RAID_INTERVAL_SECONDS,
        "raider_account": "",
        "raider_identity": "",
        "target_account": "",
        "target_identity": "",
        "next_run_at": "",
        "phase": "idle",
        "attempt_id": "",
        "attempt_started_at": "",
        "owner": "",
        "status": "",
        "reply_to_msg_id": 0,
        "lease_until": "",
        "last_run_at": "",
        "last_result": "尚未配置",
        "last_error": "",
        "last_success_at": "",
        "updated_at": now,
        "updated_by": "",
    }


@contextmanager
def surprise_raid_state_lock():
    directory = os.path.dirname(SURPRISE_RAID_LOCK_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    with open(SURPRISE_RAID_LOCK_FILE, "a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _read_surprise_raid_state_unlocked():
    try:
        with open(SURPRISE_RAID_STATE_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _ensure_surprise_raid_state_shape(raw):
    default = surprise_raid_default_state()
    data = dict(default)
    data.update({key: value for key, value in raw.items() if key in default})

    try:
        interval = int(data.get("interval_seconds") or SURPRISE_RAID_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        interval = SURPRISE_RAID_INTERVAL_SECONDS
    data["interval_seconds"] = max(
        SURPRISE_RAID_INTERVAL_MIN_SECONDS,
        min(SURPRISE_RAID_INTERVAL_MAX_SECONDS, interval),
    )

    # 化身重生改名后静态 DUEL_IDENTITIES 道号会滞后，解析身份前先按实时道号刷新，
    # 否则「寒续尘」这类改名身份 match 失败会被误判为无效 → 强制 enabled=false。
    refresh_duel_identity_roster()
    for account_field, identity_field in (
        ("raider_account", "raider_identity"),
        ("target_account", "target_identity"),
    ):
        config = duel_identity_config(
            data.get(account_field), data.get(identity_field)
        )
        if not config:
            data[account_field] = ""
            data[identity_field] = ""
            data["enabled"] = False
            data.setdefault("last_error", "")
            data["last_result"] = data.get("last_result") or "尚未配置"
        else:
            data[account_field] = config["account"]
            data[identity_field] = config["identity"]

    if str(data.get("raider_account") or "") == str(data.get("target_account") or ""):
        allowed_same = bool(data.get("raider_identity") and data.get("target_identity"))
        if not allowed_same:
            data["enabled"] = False
    if data.get("enabled") and not (
        data.get("raider_account") and data.get("raider_identity")
        and data.get("target_account") and data.get("target_identity")
    ):
        data["enabled"] = False

    if str(data.get("phase") or "idle") not in {
        "idle", "target_prepare", "execution"
    }:
        data["phase"] = "idle"
    for field in ("attempt_id", "attempt_started_at", "owner", "status"):
        if not isinstance(data.get(field), str):
            data[field] = "" if field != "status" else ""
    try:
        data["reply_to_msg_id"] = int(data.get("reply_to_msg_id") or 0)
    except (TypeError, ValueError):
        data["reply_to_msg_id"] = 0
    expires = parse_surprise_raid_time(data.get("lease_until"))
    if not expires or expires <= surprise_raid_now():
        if data.get("phase") != "idle":
            data.update({
                "phase": "idle",
                "attempt_id": "",
                "attempt_started_at": "",
                "owner": "",
                "status": "",
                "reply_to_msg_id": 0,
                "lease_until": "",
            })
        else:
            data["lease_until"] = ""
    return data


def load_surprise_raid_state(write_back=False):
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if write_back or not os.path.exists(SURPRISE_RAID_STATE_FILE):
            data["updated_at"] = surprise_raid_time()
            _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return data


def set_surprise_raid_config(
    enabled,
    interval_seconds=SURPRISE_RAID_INTERVAL_SECONDS,
    raider_account="",
    raider_identity="",
    target_account="",
    target_identity="",
    updated_by="",
):
    try:
        interval_seconds = int(round(float(interval_seconds)))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid surprise raid interval") from exc
    if (
        interval_seconds < SURPRISE_RAID_INTERVAL_MIN_SECONDS
        or interval_seconds > SURPRISE_RAID_INTERVAL_MAX_SECONDS
    ):
        raise ValueError("invalid surprise raid interval")
    enabled = bool(enabled)
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        old = {key: data.get(key) for key in (
            "enabled", "interval_seconds", "raider_account", "raider_identity",
            "target_account", "target_identity",
        )}
        data.update({
            "enabled": enabled,
            "interval_seconds": interval_seconds,
            "raider_account": str(raider_account or "").strip().lower(),
            "raider_identity": str(raider_identity or "").strip(),
            "target_account": str(target_account or "").strip().lower(),
            "target_identity": str(target_identity or "").strip(),
        })
        normalized = _ensure_surprise_raid_state_shape(data)
        changed = any(normalized.get(key) != value for key, value in old.items())
        if enabled and (changed or not normalized.get("next_run_at")):
            normalized["next_run_at"] = surprise_raid_time()
        if not enabled:
            normalized.update({
                "phase": "idle",
                "attempt_id": "",
                "attempt_started_at": "",
                "owner": "",
                "status": "",
                "reply_to_msg_id": 0,
                "lease_until": "",
            })
        normalized["last_result"] = "已启用" if enabled else "已暂停"
        normalized["updated_at"] = surprise_raid_time()
        normalized["updated_by"] = str(updated_by or "")
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, normalized)
        return normalized


def disable_surprise_raid(reason, updated_by="runtime"):
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        data["enabled"] = False
        data.update({
            "phase": "idle",
            "attempt_id": "",
            "attempt_started_at": "",
            "owner": "",
            "status": "",
            "reply_to_msg_id": 0,
            "lease_until": "",
        })
        data["last_result"] = "已停止"
        data["last_error"] = str(reason or "未知错误")
        data["updated_at"] = surprise_raid_time()
        data["updated_by"] = updated_by
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return data


def _lease_active(record, now=None):
    if not isinstance(record, dict) or not record:
        return False
    expires = parse_surprise_raid_time(record.get("lease_until"))
    return bool(expires and expires > (now or surprise_raid_now()))


def reserve_surprise_raid_for_account(account):
    """Claim target preparation when a cycle becomes due."""
    account = str(account or "").strip().lower()
    now = surprise_raid_now()
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if (
            not data.get("enabled")
            or str(data.get("phase") or "idle") != "idle"
        ):
            return None
        next_at = parse_surprise_raid_time(data.get("next_run_at"))
        if not next_at or next_at > now:
            return None
        attempt_id = uuid.uuid4().hex
        started = surprise_raid_time(now)
        data.update({
            "phase": "target_prepare",
            "attempt_id": attempt_id,
            "attempt_started_at": started,
            "owner": str(data.get("target_account") or ""),
            "status": "pending",
            "reply_to_msg_id": 0,
            "lease_until": surprise_raid_time(now + timedelta(seconds=SURPRISE_RAID_LEASE_SECONDS)),
            "last_run_at": started,
            "last_result": "目标身份准备中",
            "last_error": "",
            "updated_at": started,
        })
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return {
            "attempt_id": attempt_id,
            "raider_account": data["raider_account"],
            "raider_identity": data["raider_identity"],
            "target_account": data["target_account"],
            "target_identity": data["target_identity"],
            "target_username": duel_identity_config(
                data["target_account"], data["target_identity"]
            )["username"],
            "started_at": started,
        }


def claim_surprise_raid_target_preparation(account):
    account = str(account or "").strip().lower()
    now = surprise_raid_now()
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if (
            not data.get("enabled")
            or str(data.get("phase")) != "target_prepare"
            or str(data.get("owner")) != account
            or str(data.get("status")) != "pending"
            or not _lease_active(data, now)
        ):
            return None
        data["status"] = "claimed"
        data["lease_until"] = surprise_raid_time(
            now + timedelta(seconds=SURPRISE_RAID_LEASE_SECONDS)
        )
        data["updated_at"] = surprise_raid_time(now)
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return {
            "attempt_id": data["attempt_id"],
            "target_account": str(data.get("target_account") or ""),
            "target_identity": data["target_identity"],
            "target_username": duel_identity_config(
                data["target_account"], data["target_identity"]
            )["username"],
        }


def finish_surprise_raid_target_preparation(claim, success, detail="", reply_to_msg_id=0):
    if not claim:
        return False
    now = surprise_raid_now()
    try:
        reply_to_msg_id = int(reply_to_msg_id or 0)
    except (TypeError, ValueError):
        reply_to_msg_id = 0
    if success and reply_to_msg_id <= 0:
        success = False
        detail = detail or "目标身份切换消息缺失"
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if (
            str(data.get("phase")) != "target_prepare"
            or str(data.get("attempt_id")) != str(claim.get("attempt_id"))
        ):
            return False
        if success:
            data["status"] = "ready"
            data["reply_to_msg_id"] = reply_to_msg_id
            data["last_result"] = detail or "目标切换锚点就绪"
        else:
            data.update({
                "phase": "idle",
                "attempt_id": "",
                "attempt_started_at": "",
                "owner": "",
                "status": "",
                "reply_to_msg_id": 0,
                "lease_until": "",
                "next_run_at": surprise_raid_time(now + timedelta(minutes=10)),
                "last_result": detail or "目标身份准备失败",
            })
        data["updated_at"] = surprise_raid_time(now)
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return True


def surprise_target_preparation_held(claim):
    if not claim:
        return False
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        return (
            str(data.get("phase")) == "target_prepare"
            and str(data.get("attempt_id")) == str(claim.get("attempt_id"))
            and str(data.get("status")) in {"claimed", "ready"}
            and _lease_active(data)
        )


def release_surprise_target_preparation(claim, detail=""):
    if not claim:
        return False
    now = surprise_raid_now()
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if (
            str(data.get("phase")) != "target_prepare"
            or str(data.get("attempt_id")) != str(claim.get("attempt_id"))
        ):
            return False
        if str(data.get("status")) == "ready" and _lease_active(data):
            return False
        data.update({
            "phase": "idle",
            "attempt_id": "",
            "attempt_started_at": "",
            "owner": "",
            "status": "",
            "reply_to_msg_id": 0,
            "lease_until": "",
        })
        if detail:
            data["last_result"] = detail
        data["updated_at"] = surprise_raid_time(now)
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return True


def claim_surprise_raid_execution(account):
    account = str(account or "").strip().lower()
    now = surprise_raid_now()
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if (
            not data.get("enabled")
            or str(data.get("phase")) != "target_prepare"
            or str(data.get("status")) != "ready"
            or str(data.get("raider_account")) != account
            or int(data.get("reply_to_msg_id") or 0) <= 0
            or not _lease_active(data, now)
        ):
            return None
        reservation = {
            "attempt_id": data["attempt_id"],
            "identity": data["raider_identity"],
            "reply_to_msg_id": int(data["reply_to_msg_id"]),
            "raider_username": duel_identity_config(
                data["raider_account"], data["raider_identity"]
            )["username"],
            "target_account": data["target_account"],
            "target_identity": data["target_identity"],
            "target_username": duel_identity_config(
                data["target_account"], data["target_identity"]
            )["username"],
        }
        data.update({
            "phase": "execution",
            "owner": account,
            "status": "claimed",
            "lease_until": surprise_raid_time(
                now + timedelta(seconds=SURPRISE_RAID_LEASE_SECONDS)
            ),
            "last_result": "奇袭执行中",
            "updated_at": surprise_raid_time(now),
        })
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return reservation


def finish_surprise_raid_execution(reservation, result):
    if not reservation:
        return False
    now = surprise_raid_now()
    status = str((result or {}).get("status") or "unknown")
    outcome = str((result or {}).get("outcome") or status)
    with surprise_raid_state_lock():
        data = _ensure_surprise_raid_state_shape(_read_surprise_raid_state_unlocked())
        if (
            str(data.get("attempt_id")) != str(reservation.get("attempt_id"))
            or str(data.get("phase")) not in {"execution", "target_prepare"}
        ):
            return False
        if status == "unknown":
            # The caller must already have alerted; shared state remains stopped
            # so no other runner blindly resumes this task.
            pass
        else:
            delay = int(data.get("interval_seconds") or SURPRISE_RAID_INTERVAL_SECONDS)
            remaining = int((result or {}).get("cooldown_seconds") or 0)
            if remaining > 0:
                # Bot reported an explicit cooldown; retry as soon as it lapses.
                delay = remaining
            data.update({
                "phase": "idle",
                "attempt_id": "",
                "attempt_started_at": "",
                "owner": "",
                "status": "",
                "reply_to_msg_id": 0,
                "lease_until": "",
                "next_run_at": surprise_raid_time(now + timedelta(seconds=delay)),
                "last_result": outcome,
                "last_error": str((result or {}).get("error") or ""),
            })
            if status == "success":
                data["last_success_at"] = surprise_raid_time(now)
        data["updated_at"] = surprise_raid_time(now)
        _atomic_write_json(SURPRISE_RAID_STATE_FILE, data)
        return True


def surprise_raid_dashboard_payload():
    # Refresh the shared identity roster before normalizing the saved raid
    # state.  A rebirth can change only the Dao name; loading the old state
    # first would otherwise clear a valid commission before the dashboard gets
    # a chance to migrate it.
    duel_identity_options(refresh=True)
    state = load_surprise_raid_state()
    runnable_accounts = {"main", "sub", "xiaohao", "waaiging"}
    options = [
        option for option in duel_identity_options(refresh=False)
        if option.get("account") in runnable_accounts
    ]
    return {
        **state,
        "identity_options": options,
        "item": SURPRISE_RAID_ITEM,
        "command": SURPRISE_RAID_COMMAND,
    }


def _surprise_raid_step_result(command, response):
    text = ""
    if hasattr(response, "text"):
        text = response.text or ""
    elif isinstance(response, str):
        text = response
    status, outcome, clean = classify_surprise_raid_step(command, text)
    return {
        "status": status,
        "outcome": outcome,
        "text": clean,
        "cooldown_seconds": parse_surprise_raid_cooldown_seconds(clean),
    }


class SurpriseRaidMixin:
    """Actor methods for the shared surprise-raid lifecycle."""

    def surprise_raid_logger(self):
        logger = getattr(self, "log", None)
        return logger if logger is not None else logging.getLogger(
            self.__class__.__name__
        )

    async def _prepare_surprise_raid_target_identity(self, claim):
        account = str(getattr(self, "account_key", "") or "")
        if str(claim.get("target_account") or "") != account:
            return False, "目标身份准备账号不匹配", None
        identity = str(claim.get("target_identity") or "").strip()
        resolver = getattr(self, "resolve_avatar_identity", None)
        if callable(resolver):
            identity = str(resolver(identity) or identity).strip()
        pause_seconds = getattr(self, "identity_pause_seconds", None)
        if callable(pause_seconds):
            try:
                remaining = int(pause_seconds(identity) or 0)
            except (TypeError, ValueError):
                remaining = 0
            if remaining > 0:
                return False, f"{identity} 身份暂停，跳过目标准备", None
        known_identities = {"主魂", *getattr(self, "avatars", [])}
        if not identity or identity not in known_identities:
            return False, "被夺宝者不是可切换身份", None
        prepare = getattr(
            self, "prepare_identity_for_time_critical_command", None
        )
        if not callable(prepare):
            return False, "账号缺少身份预切换能力", None
        # Even 主魂 gets a fresh outgoing .切换 message because it is the
        # explicit reply anchor required by .奇袭 夺宝.
        prepared = await prepare(
            identity,
            command=SURPRISE_RAID_COMMAND,
            timeout=30,
            force_fresh=True,
            return_switch_message_id=True,
        )
        if isinstance(prepared, tuple):
            success, reply_to_msg_id = prepared
        else:
            success = bool(prepared)
            reply_to_msg_id = getattr(self, "last_sent_id", None) if success else None
        try:
            reply_to_msg_id = int(reply_to_msg_id or 0)
        except (TypeError, ValueError):
            reply_to_msg_id = 0
        if not success or str(getattr(self, "current_identity", "") or "") != identity:
            return False, f"{identity} 激活未确认", None
        if reply_to_msg_id <= 0:
            return False, f"{identity} 的 .切换 消息 ID 未取得", None
        return (
            True,
            f"@{claim.get('target_username')} · 已发送 .切换 {identity} 锚点",
            reply_to_msg_id,
        )

    async def _send_surprise_raid_identity_command(
        self, identity, command, reply_to=None
    ):
        sender = getattr(self, "send_and_wait_feedback_identity", None)
        if not callable(sender):
            raise RuntimeError("账号缺少身份指令发送能力")
        return await sender(
            identity,
            command,
            timeout=60,
            max_retries=0,
            reply_to=reply_to,
            return_response_msg=True,
            delete_after=False,
            force_identity_check=True,
        )

    _surprise_raid_step_result = staticmethod(_surprise_raid_step_result)

    async def _wait_for_surprise_raid_result(self, response_msg, anchor_msg_id):
        current = response_msg
        text = ""
        if hasattr(self, "response_text"):
            text = self.response_text(current)
        else:
            text = getattr(current, "text", "") if current is not None else ""
        classified = classify_surprise_raid_step(SURPRISE_RAID_COMMAND, text)
        if classified[0] in {"success", "cooldown"}:
            return current, classified[2]
        msg_id = getattr(current, "id", None)
        if not msg_id:
            return current, text
        deadline = time.monotonic() + SURPRISE_RAID_RESULT_WAIT_SECONDS
        while time.monotonic() < deadline and getattr(self, "is_running", True):
            await asyncio.sleep(2)
            try:
                fresh = await self.client.get_messages(
                    self.target_chat_id, ids=msg_id
                )
                if isinstance(fresh, (list, tuple)):
                    fresh = fresh[0] if fresh else None
                if fresh is not None:
                    current = fresh
                    text = getattr(fresh, "text", "") or text
                    classified = classify_surprise_raid_step(
                        SURPRISE_RAID_COMMAND, text
                    )
                    if classified[0] in {"success", "cooldown"}:
                        return current, classified[2]
            except Exception:
                pass
            try:
                messages = await self.client.get_messages(
                    self.target_chat_id, limit=40
                )
                if not isinstance(messages, (list, tuple)):
                    messages = [messages] if messages is not None else []
                for candidate in reversed(messages):
                    candidate_anchor = 0
                    reply_to = getattr(candidate, "reply_to", None)
                    if reply_to is not None:
                        candidate_anchor = getattr(
                            reply_to, "reply_to_msg_id", 0
                        ) or 0
                    if int(candidate_anchor or 0) != int(anchor_msg_id):
                        continue
                    candidate_text = getattr(candidate, "text", "") or ""
                    classified = classify_surprise_raid_step(
                        SURPRISE_RAID_COMMAND, candidate_text
                    )
                    if classified[0] in {"pending"}:
                        continue
                    if classified[0] in {"success", "cooldown"}:
                        return candidate, classified[2]
                    # "蓄势待发" 也是正常的中间状态，继续等待最终结果
                    if classified[0] == "failure":
                        return candidate, classified[2]
            except Exception:
                pass
        return current, text

    def _surprise_raid_step_result(self, command, response):
        text = ""
        if hasattr(response, "text"):
            text = response.text or ""
        elif isinstance(response, str):
            text = response
        status, outcome, clean = classify_surprise_raid_step(command, text)
        # 蓄势待发也是正常的中间状态，不算未知回复
        if status == "unknown" and isinstance(text, str):
            if "雷光闪烁" in text or "寻找对方的破绽" in text or "奇袭成功率" in text:
                status = "pending"
                outcome = "蓄势待发，等待判定结果"
                clean = text
        return {
            "status": status,
            "outcome": outcome,
            "text": clean,
            "cooldown_seconds": parse_surprise_raid_cooldown_seconds(clean),
        }

    async def _disable_surprise_raid_for_unknown(self, command, result, logger):
        detail = (result.get("text") or result.get("outcome") or "无内容").replace("`", "'")
        reason = f"{command} 收到未知回复：{detail[:500]}"
        logger.error("Surprise raid stopped by unknown response: %s", reason)
        disable_surprise_raid(reason, updated_by="runtime-unknown-response")
        try:
            await send_text_alert(
                self,
                "奇袭夺宝已停止",
                f"收到未知回复，任务已停止，请人工确认后再启用。\n指令：{command}\n回复：{detail[:800]}",
                logger=logger,
            )
        except Exception:
            logger.exception("Surprise raid unknown-response alert failed")

    async def execute_surprise_raid_reservation(self, reservation):
        logger = self.surprise_raid_logger()
        identity = str(reservation.get("identity") or "主魂")
        resolver = getattr(self, "resolve_avatar_identity", None)
        if callable(resolver):
            identity = str(resolver(identity) or identity)
        result = {
            "status": "unknown",
            "outcome": "执行异常",
            "text": "",
            "error": "",
        }
        username_map = getattr(self, "identity_usernames", None)
        original_usernames = None
        created_mapping_key = False
        if isinstance(username_map, dict):
            if identity not in username_map:
                username_map[identity] = []
                created_mapping_key = True
            original_usernames = list(username_map[identity])
            target_username = str(reservation.get("target_username") or "")
            if target_username.lower() not in {
                value.lower() for value in original_usernames
            }:
                # The attack result mentions the victim's game username.  Add
                # it temporarily so feedback matching does not reject it as a
                # foreign-user reply.
                username_map[identity].append(target_username)
        try:
            retrieve_response = await self._send_surprise_raid_identity_command(
                identity, ".从万宝阁取下 风雷翅"
            )
            result = self._surprise_raid_step_result(
                ".从万宝阁取下 风雷翅", retrieve_response
            )
            logger.info(
                "Surprise raid retrieve: %s/%s status=%s detail=%s",
                getattr(self, "account_key", ""), identity, result["status"],
                result["outcome"],
            )
            if result["status"] == "missing_item":
                result.update(status="skipped", outcome="没有风雷翅，自动跳过")
                return result
            # 风雷翅不在万宝阁（needs_equip）也继续进入装备步骤，不中断奇袭流程
            if result["status"] not in {"success", "needs_equip"}:
                return result

            equip_response = await self._send_surprise_raid_identity_command(
                identity, ".装备 风雷翅"
            )
            result = self._surprise_raid_step_result(".装备 风雷翅", equip_response)
            logger.info(
                "Surprise raid equip: %s/%s status=%s detail=%s",
                getattr(self, "account_key", ""), identity, result["status"],
                result["outcome"],
            )
            if result["status"] in {"missing_item", "not_equipped"}:
                result.update(status="skipped", outcome=result["outcome"])
                return result
            if result["status"] != "success":
                return result

            attack_response = await self._send_surprise_raid_identity_command(
                identity,
                SURPRISE_RAID_COMMAND,
                reply_to=int(reservation.get("reply_to_msg_id") or 0) or None,
            )
            result = self._surprise_raid_step_result(
                SURPRISE_RAID_COMMAND, attack_response
            )
            if result["status"] == "pending":
                final_msg, final_text = await self._wait_for_surprise_raid_result(
                    attack_response,
                    int(reservation.get("reply_to_msg_id") or 0),
                )
                result = self._surprise_raid_step_result(
                    SURPRISE_RAID_COMMAND, final_text
                )
                if hasattr(final_msg, "id"):
                    result["response_msg_id"] = final_msg.id
            if result["status"] == "needs_equip":
                logger.info(
                    "Surprise raid attack: %s/%s needs equip first, retrying...",
                    getattr(self, "account_key", ""), identity,
                )
                # 回到装备步骤：重新装备后继续奇袭
                equip_response = await self._send_surprise_raid_identity_command(
                    identity, ".装备 风雷翅"
                )
                result = self._surprise_raid_step_result(
                    ".装备 风雷翅", equip_response
                )
                logger.info(
                    "Surprise raid equip (retry): %s/%s status=%s detail=%s",
                    getattr(self, "account_key", ""), identity, result["status"],
                    result["outcome"],
                )
                if result["status"] in {"missing_item", "not_equipped"}:
                    result.update(status="skipped", outcome=result["outcome"])
                    return result
                if result["status"] != "success":
                    return result
                # 装备成功，重新发起奇袭
                attack_response = await self._send_surprise_raid_identity_command(
                    identity,
                    SURPRISE_RAID_COMMAND,
                    reply_to=int(reservation.get("reply_to_msg_id") or 0) or None,
                )
                result = self._surprise_raid_step_result(
                    SURPRISE_RAID_COMMAND, attack_response
                )
                if result["status"] == "pending":
                    final_msg, final_text = await self._wait_for_surprise_raid_result(
                        attack_response,
                        int(reservation.get("reply_to_msg_id") or 0),
                    )
                    result = self._surprise_raid_step_result(
                        SURPRISE_RAID_COMMAND, final_text
                    )
                    if hasattr(final_msg, "id"):
                        result["response_msg_id"] = final_msg.id
            logger.info(
                "Surprise raid attack: %s/%s -> @%s status=%s detail=%s",
                getattr(self, "account_key", ""), identity,
                reservation.get("target_username"), result["status"],
                result["outcome"],
            )
            if result["status"] == "unknown":
                return result

            for command in (".散念 风雷翅", ".上架至万宝阁 风雷翅"):
                cleanup_response = await self._send_surprise_raid_identity_command(
                    identity, command
                )
                cleanup = self._surprise_raid_step_result(command, cleanup_response)
                logger.info(
                    "Surprise raid cleanup [%s]: status=%s detail=%s",
                    command, cleanup["status"], cleanup["outcome"],
                )
                if cleanup["status"] == "unknown":
                    result = cleanup
                    return result
            if result["status"] == "cooldown":
                result["status"] = "settled"
                result["outcome"] = f"冷却跳过（{result['cooldown_seconds']} 秒）"
            elif result["status"] in {"success", "timeout"}:
                result["status"] = "settled"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = {
                "status": "unknown",
                "outcome": "执行异常",
                "text": str(exc),
                "error": str(exc),
            }
            logger.exception("Surprise raid execution crashed")
        finally:
            if isinstance(username_map, dict) and original_usernames is not None:
                if created_mapping_key:
                    if not original_usernames:
                        username_map.pop(identity, None)
                    else:
                        username_map[identity] = original_usernames
                else:
                    username_map[identity] = original_usernames
            if result.get("status") == "unknown":
                try:
                    await self._disable_surprise_raid_for_unknown(
                        SURPRISE_RAID_COMMAND, result, logger
                    )
                except Exception:
                    logger.exception("Surprise raid unknown-response shutdown failed")
            finish_surprise_raid_execution(reservation, result)
        return result

    async def _run_surprise_raid_target_preparation_if_due(self):
        account = str(getattr(self, "account_key", "") or "")
        reserved = reserve_surprise_raid_for_account(account)
        if reserved:
            # The reserving process may be the same account as the target;
            # let the normal target-prep claim below handle it.
            pass
        claim = claim_surprise_raid_target_preparation(account)
        if not claim:
            return bool(reserved)
        logger = self.surprise_raid_logger()

        async def prepare_and_hold_or_execute():
            success, detail, reply_to_msg_id = (
                await self._prepare_surprise_raid_target_identity(claim)
            )
            finish_surprise_raid_target_preparation(
                claim, success, detail, reply_to_msg_id=reply_to_msg_id
            )
            logger.info(
                "Surprise raid target preparation: identity=%s success=%s reply=%s detail=%s",
                claim.get("target_identity"), success, reply_to_msg_id, detail,
            )
            if not success:
                return
            reservation = claim_surprise_raid_execution(account)
            if reservation:
                await self.execute_surprise_raid_reservation(reservation)
                return
            deadline = time.monotonic() + SURPRISE_RAID_LEASE_SECONDS
            while (
                getattr(self, "is_running", True)
                and time.monotonic() < deadline
                and surprise_target_preparation_held(claim)
            ):
                await asyncio.sleep(1)
            release_surprise_target_preparation(
                claim,
                detail=(
                    "奇袭者未及时领取，稍后重试"
                    if time.monotonic() >= deadline else ""
                ),
            )

        atomic = getattr(self, "common_atomic_task", None)
        if callable(atomic):
            async with atomic(f"SurpriseRaid-target-{claim.get('target_identity')}"):
                await prepare_and_hold_or_execute()
        else:
            await prepare_and_hold_or_execute()
        return True

    async def run_surprise_raid_scheduler(self, initial_delay=35):
        logger = self.surprise_raid_logger()
        startup_done = getattr(self, "startup_done", None)
        if startup_done is not None:
            await startup_done.wait()
        if initial_delay:
            await asyncio.sleep(max(0, int(initial_delay)))
        logger.info(
            "Shared surprise raid scheduler started for account=%s",
            getattr(self, "account_key", ""),
        )
        while getattr(self, "is_running", True):
            try:
                executed = await self._run_surprise_raid_target_preparation_if_due()
                if not executed:
                    reservation = claim_surprise_raid_execution(
                        str(getattr(self, "account_key", "") or "")
                    )
                    if reservation:
                        await self.execute_surprise_raid_reservation(reservation)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shared surprise raid scheduler iteration failed")
            await asyncio.sleep(SURPRISE_RAID_POLL_SECONDS)
