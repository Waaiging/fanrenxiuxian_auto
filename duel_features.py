#!/usr/bin/env python3
"""Shared duel schedulers for legacy rotations and active one-to-many plans."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows test fallback
    fcntl = None


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
DUEL_STATE_FILE = os.path.join(CONFIG_DIR, "duel_state.json")
DUEL_LOCK_FILE = os.path.join(CONFIG_DIR, "duel_state.lock")
DUEL_DB_FILE = os.path.join(CONFIG_DIR, "message_events.sqlite3")
XIAOHAO_STATE_FILE = os.path.join(CONFIG_DIR, "state_xiaohao.json")
DUEL_INTERVAL_SECONDS = 6 * 60
DUEL_TARGET_INTERVAL_SECONDS = 11 * 60
DUEL_DAILY_LIMIT = 10
DUEL_LEASE_SECONDS = 4 * 60
DUEL_RESULT_WAIT_SECONDS = 90
DUEL_POLL_SECONDS = 3
DUEL_RETENTION_DAYS = 30
DUEL_RESTRICTED_ACCOUNT_RETRY_SECONDS = 60
DUEL_BUSY_RETRY_SECONDS = 60
DUEL_MULTI_MAX_TARGETS = 20
DUEL_MULTI_MAX_COUNT = 999
DUEL_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,64}$")
TITAN_BEAST_MODE_DEFAULT = "deploy"
TITAN_BEAST_MODE_LABELS = {
    "deploy": "出战",
}

DUEL_ACCOUNT_LABELS = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}

DUEL_IDENTITIES = {
    "main": (
        {"identity": "主魂", "username": "Weeguu"},
        {"identity": "无咎子", "username": "wuxinglinggen"},
        {"identity": "缘生子", "username": "kulipabp"},
        {"identity": "素缘子", "username": "OldEinstein"},
    ),
    "sub": (
        {"identity": "主魂", "username": "Gamling33"},
        {"identity": "厚土", "username": "crayonxxin"},
        {"identity": "缘生子", "username": "Lvdoumiao"},
        {"identity": "寻真子", "username": "ding303"},
    ),
    "xiaohao": (
        {"identity": "主魂", "username": "TitanCreeper"},
        {"identity": "问心子", "username": "lianqi10000"},
        {"identity": "素心子", "username": "hajiimiii"},
        {"identity": "缘生子", "username": "adai925"},
    ),
    "waaiging": (
        {"identity": "主魂", "username": "Waaiging"},
    ),
}

DUEL_QUEUES = {
    "waaiging": {
        "label": "斗法轮换 A组",
        "target": "Waaiging",
        "participants": (
            {"account": "main", "identity": "无咎子", "username": "wuxinglinggen"},
            {"account": "main", "identity": "缘生子", "username": "kulipabp"},
            {"account": "xiaohao", "identity": "素心子", "username": "hajiimiii"},
            {"account": "xiaohao", "identity": "缘生子", "username": "adai925"},
        ),
    },
    "titan": {
        "label": "斗法轮换 B组",
        "target": "TitanCreeper",
        "participants": (
            {"account": "main", "identity": "素缘子", "username": "oldeinstein"},
            {"account": "sub", "identity": "厚土", "username": "crayonxxin"},
            {"account": "sub", "identity": "缘生子", "username": "lvdoumiao"},
            {"account": "sub", "identity": "寻真子", "username": "ding303"},
        ),
    },
}


def duel_now():
    return datetime.now()


def duel_time(value=None):
    return (value or duel_now()).strftime(TIME_FORMAT)


def duel_date(value=None):
    return (value or duel_now()).strftime("%Y-%m-%d")


def parse_duel_time(value):
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except Exception:
        return None


def duel_participant_key(account, identity):
    return f"{str(account or '').strip()}|{str(identity or '主魂').strip() or '主魂'}"


def duel_participant_config(queue_key, participant_key):
    for participant in DUEL_QUEUES.get(queue_key, {}).get("participants", ()):
        if duel_participant_key(participant["account"], participant["identity"]) == participant_key:
            return dict(participant)
    return None


def duel_identity_config(account, identity):
    account = str(account or "").strip().lower()
    identity = str(identity or "主魂").strip() or "主魂"
    for item in DUEL_IDENTITIES.get(account, ()):
        if item["identity"] == identity:
            return {
                **item,
                "account": account,
                "account_name": DUEL_ACCOUNT_LABELS.get(account, account),
                "key": duel_participant_key(account, identity),
            }
    return None


def duel_identity_for_username(username):
    try:
        target = normalize_duel_target(username)
    except ValueError:
        return None
    for account, identities in DUEL_IDENTITIES.items():
        for item in identities:
            if item["username"].lower() == target.lower():
                return duel_identity_config(account, item["identity"])
    return None


def duel_identity_options():
    rows = []
    for account, identities in DUEL_IDENTITIES.items():
        for item in identities:
            config = duel_identity_config(account, item["identity"])
            rows.append({
                **config,
                "label": f"{config['account_name']} · {config['identity']} · @{config['username']}",
            })
    return rows


def normalize_duel_target(value, fallback=""):
    """Return a Telegram username without @, or an empty value for the default."""
    target = str(value or "").strip().lstrip("@").strip()
    if not target:
        return ""
    if not DUEL_TARGET_PATTERN.fullmatch(target):
        raise ValueError("invalid duel target username")
    return target


def normalize_titan_beast_mode(value):
    mode = str(value or TITAN_BEAST_MODE_DEFAULT).strip().lower()
    aliases = {"出战": "deploy"}
    mode = aliases.get(mode, mode)
    if mode not in TITAN_BEAST_MODE_LABELS:
        raise ValueError("invalid titan beast mode")
    return mode


def _new_participant_state():
    return {
        "enabled": True,
        "target_username": "",
        "attempts": 0,
        "remaining": DUEL_DAILY_LIMIT,
        "wins": 0,
        "losses": 0,
        "status": "ready",
        "last_attempt_at": "",
        "last_result": "",
    }


def _new_queue_state(queue_key):
    state = {
        "enabled": True,
        "target": DUEL_QUEUES[queue_key]["target"],
        "cursor": 0,
        "next_at": "",
        "last_attempt_at": "",
        "last_success_at": "",
        "last_result": "",
        "in_flight": {},
        "preparation": {},
        "participants": {
            duel_participant_key(item["account"], item["identity"]): _new_participant_state()
            for item in DUEL_QUEUES[queue_key]["participants"]
        },
    }
    if queue_key == "titan":
        state["beast_mode"] = TITAN_BEAST_MODE_DEFAULT
    return state


def _new_multi_target_state(username, count, target_id=None):
    count = max(1, min(DUEL_MULTI_MAX_COUNT, int(count)))
    return {
        "id": str(target_id or uuid.uuid4().hex),
        "username": normalize_duel_target(username),
        "count": count,
        "attempts": 0,
        "remaining": count,
        "status": "ready",
        "last_attempt_at": "",
        "last_result": "",
    }


def _new_multi_state():
    return {
        "enabled": False,
        "initiator_account": "",
        "initiator_identity": "",
        "cursor": 0,
        "next_at": "",
        "last_attempt_at": "",
        "last_success_at": "",
        "last_result": "尚未配置",
        "daily_remaining": DUEL_DAILY_LIMIT,
        "daily_exhausted_date": "",
        "in_flight": {},
        "preparation": {},
        "targets": [],
    }


def duel_default_state():
    return {
        "version": 2,
        "enabled": True,
        "date": duel_date(),
        "updated_at": duel_time(),
        "target_next_at": {},
        "queues": {key: _new_queue_state(key) for key in DUEL_QUEUES},
        "multi": _new_multi_state(),
    }


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


@contextmanager
def duel_state_lock():
    directory = os.path.dirname(DUEL_LOCK_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    with open(DUEL_LOCK_FILE, "a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_duel_state_unlocked():
    try:
        with open(DUEL_STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return duel_default_state()
        return data
    except Exception:
        return duel_default_state()


def _ensure_duel_state_shape(data, reset_daily=True):
    if not isinstance(data, dict):
        data = duel_default_state()
    data["version"] = 2
    data.setdefault("enabled", True)
    target_next_at = data.setdefault("target_next_at", {})
    if not isinstance(target_next_at, dict):
        target_next_at = {}
        data["target_next_at"] = target_next_at
    normalized_targets = {}
    for target, value in target_next_at.items():
        try:
            target_key = normalize_duel_target(target).lower()
        except ValueError:
            continue
        ready_at = parse_duel_time(value)
        if target_key and ready_at:
            normalized_targets[target_key] = duel_time(ready_at)
    data["target_next_at"] = normalized_targets
    queues = data.setdefault("queues", {})
    for queue_key, config in DUEL_QUEUES.items():
        queue = queues.setdefault(queue_key, _new_queue_state(queue_key))
        queue.setdefault("enabled", True)
        queue["target"] = config["target"]
        queue.setdefault("cursor", 0)
        queue.setdefault("next_at", "")
        queue.setdefault("last_attempt_at", "")
        queue.setdefault("last_success_at", "")
        queue.setdefault("last_result", "")
        queue.setdefault("in_flight", {})
        queue.setdefault("preparation", {})
        if queue_key == "titan":
            try:
                queue["beast_mode"] = normalize_titan_beast_mode(queue.get("beast_mode"))
            except ValueError:
                queue["beast_mode"] = TITAN_BEAST_MODE_DEFAULT
        participants = queue.setdefault("participants", {})
        expected_keys = set()
        for item in config["participants"]:
            key = duel_participant_key(item["account"], item["identity"])
            expected_keys.add(key)
            participant = participants.setdefault(key, _new_participant_state())
            for field, value in _new_participant_state().items():
                participant.setdefault(field, value)
            participant["enabled"] = bool(participant.get("enabled", True))
            try:
                participant["target_username"] = normalize_duel_target(participant.get("target_username"))
            except ValueError:
                participant["target_username"] = ""
        for key in list(participants):
            if key not in expected_keys:
                participants.pop(key, None)

    multi = data.setdefault("multi", _new_multi_state())
    if not isinstance(multi, dict):
        multi = _new_multi_state()
        data["multi"] = multi
    for field, value in _new_multi_state().items():
        multi.setdefault(field, value)
    multi["enabled"] = bool(multi.get("enabled", False))
    initiator_account = str(multi.get("initiator_account") or "").strip().lower()
    initiator_identity = str(multi.get("initiator_identity") or "").strip()
    if duel_identity_config(initiator_account, initiator_identity):
        multi["initiator_account"] = initiator_account
        multi["initiator_identity"] = initiator_identity
    else:
        multi["enabled"] = False
        multi["initiator_account"] = ""
        multi["initiator_identity"] = ""
    try:
        multi["cursor"] = max(0, int(multi.get("cursor") or 0))
    except Exception:
        multi["cursor"] = 0
    try:
        multi["daily_remaining"] = max(
            0,
            min(DUEL_DAILY_LIMIT, int(multi.get("daily_remaining", DUEL_DAILY_LIMIT))),
        )
    except Exception:
        multi["daily_remaining"] = DUEL_DAILY_LIMIT
    for field in ("in_flight", "preparation"):
        if not isinstance(multi.get(field), dict):
            multi[field] = {}
    normalized_multi_targets = []
    seen_ids = set()
    seen_usernames = set()
    raw_targets = multi.get("targets") if isinstance(multi.get("targets"), list) else []
    for raw in raw_targets[:DUEL_MULTI_MAX_TARGETS]:
        if not isinstance(raw, dict):
            continue
        try:
            username = normalize_duel_target(raw.get("username"))
            count = max(1, min(DUEL_MULTI_MAX_COUNT, int(raw.get("count") or 1)))
        except (TypeError, ValueError):
            continue
        if not username:
            continue
        target_key = username.lower()
        if target_key in seen_usernames:
            continue
        seen_usernames.add(target_key)
        target_id = str(raw.get("id") or uuid.uuid4().hex)
        if target_id in seen_ids:
            target_id = uuid.uuid4().hex
        seen_ids.add(target_id)
        try:
            attempts = max(0, min(count, int(raw.get("attempts") or 0)))
        except Exception:
            attempts = 0
        try:
            remaining = max(0, min(count, int(raw.get("remaining", count - attempts))))
        except Exception:
            remaining = max(0, count - attempts)
        attempts = max(attempts, count - remaining)
        normalized_multi_targets.append({
            "id": target_id,
            "username": username,
            "count": count,
            "attempts": attempts,
            "remaining": remaining,
            "status": str(raw.get("status") or ("completed" if remaining <= 0 else "ready")),
            "last_attempt_at": str(raw.get("last_attempt_at") or ""),
            "last_result": str(raw.get("last_result") or ""),
        })
    multi["targets"] = normalized_multi_targets
    if not normalized_multi_targets:
        multi["enabled"] = False
        multi["in_flight"] = {}
        multi["preparation"] = {}
        multi["last_result"] = "尚未配置"

    today = duel_date()
    if reset_daily and str(data.get("date") or "") != today:
        data["date"] = today
        for queue in queues.values():
            queue["cursor"] = 0
            queue["next_at"] = ""
            queue["in_flight"] = {}
            queue["preparation"] = {}
            for key in list(queue.get("participants", {})):
                previous = queue["participants"].get(key) or {}
                reset_state = _new_participant_state()
                reset_state["enabled"] = bool(previous.get("enabled", True))
                reset_state["target_username"] = normalize_duel_target(previous.get("target_username"))
                queue["participants"][key] = reset_state
        multi["daily_remaining"] = DUEL_DAILY_LIMIT
        multi["daily_exhausted_date"] = ""
        multi["next_at"] = ""
        multi["in_flight"] = {}
        multi["preparation"] = {}
        for target in multi["targets"]:
            target["status"] = "completed" if int(target.get("remaining") or 0) <= 0 else "ready"
    return data


def load_duel_state(write_back=False):
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        if write_back or not os.path.exists(DUEL_STATE_FILE):
            data["updated_at"] = duel_time()
            _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def set_duel_control(enabled, queue_key=""):
    queue_key = str(queue_key or "").strip().lower()
    if queue_key and queue_key not in DUEL_QUEUES:
        raise ValueError("unknown duel queue")
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        if queue_key:
            queue = data["queues"][queue_key]
            queue["enabled"] = bool(enabled)
            if enabled:
                queue["next_at"] = ""
                queue["last_result"] = "已恢复"
            else:
                queue["in_flight"] = {}
                queue["preparation"] = {}
                queue["last_result"] = "已暂停"
        else:
            data["enabled"] = bool(enabled)
            if enabled:
                for queue in data["queues"].values():
                    queue["next_at"] = ""
                data["multi"]["next_at"] = ""
            else:
                data["multi"]["in_flight"] = {}
                data["multi"]["preparation"] = {}
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def configure_duel_multi_plan(initiator_account, initiator_identity, targets, enabled=True):
    initiator = duel_identity_config(initiator_account, initiator_identity)
    if not initiator:
        raise ValueError("unknown duel initiator")
    if not isinstance(targets, list) or not targets:
        raise ValueError("duel targets required")
    if len(targets) > DUEL_MULTI_MAX_TARGETS:
        raise ValueError("too many duel targets")

    normalized = []
    seen = set()
    for raw in targets:
        if not isinstance(raw, dict):
            raise ValueError("invalid duel target")
        username = normalize_duel_target(raw.get("username") or raw.get("target"))
        if not username:
            raise ValueError("invalid duel target username")
        target_key = username.lower()
        if target_key in seen:
            raise ValueError("duplicate duel target")
        seen.add(target_key)
        try:
            count = int(raw.get("count") or 0)
        except Exception as exc:
            raise ValueError("invalid duel count") from exc
        if count < 1 or count > DUEL_MULTI_MAX_COUNT:
            raise ValueError("invalid duel count")
        target_identity = duel_identity_for_username(username)
        if target_key == initiator["username"].lower():
            raise ValueError("duel target matches initiator")
        if target_identity and target_identity["account"] == initiator["account"]:
            raise ValueError("same account duel target")
        normalized.append(_new_multi_target_state(username, count))

    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        multi = _new_multi_state()
        multi.update({
            "enabled": bool(enabled),
            "initiator_account": initiator["account"],
            "initiator_identity": initiator["identity"],
            "last_result": "已保存，等待调度" if enabled else "已保存并暂停",
            "targets": normalized,
        })
        data["multi"] = multi
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def set_duel_multi_control(enabled):
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        multi = data["multi"]
        if enabled and (
            not duel_identity_config(multi.get("initiator_account"), multi.get("initiator_identity"))
            or not multi.get("targets")
        ):
            raise ValueError("duel multi plan is not configured")
        multi["enabled"] = bool(enabled)
        multi["next_at"] = ""
        if not enabled:
            multi["in_flight"] = {}
            multi["preparation"] = {}
        multi["last_result"] = "已恢复" if enabled else "已暂停"
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def set_duel_participant_control(enabled, participant_key, target_username=None):
    participant_key = str(participant_key or "").strip()
    if not participant_key:
        raise ValueError("missing duel participant")
    target = None if target_username is None else normalize_duel_target(target_username)
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        found = None
        for queue_key, config in DUEL_QUEUES.items():
            if duel_participant_config(queue_key, participant_key):
                found = (queue_key, config)
                break
        if not found:
            raise ValueError("unknown duel participant")
        queue_key, config = found
        queue = data["queues"][queue_key]
        state = queue["participants"].setdefault(participant_key, _new_participant_state())
        state["enabled"] = bool(enabled)
        if target is not None:
            state["target_username"] = target
        if not state["enabled"] and (queue.get("in_flight") or {}).get("participant_key") == participant_key:
            queue["in_flight"] = {}
            state["status"] = "paused"
        elif state["enabled"] and state.get("status") == "paused":
            state["status"] = "ready"
        queue["next_at"] = ""
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def set_titan_beast_mode(mode):
    mode = normalize_titan_beast_mode(mode)
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue = data["queues"]["titan"]
        queue["beast_mode"] = mode
        queue["next_at"] = ""
        queue["target_ready_at"] = ""
        queue["last_result"] = f"已指定小号主魂六翼{TITAN_BEAST_MODE_LABELS[mode]}"
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def _lease_active(record, now=None):
    if not isinstance(record, dict) or not record:
        return False
    expires = parse_duel_time(record.get("lease_until"))
    return bool(expires and expires > (now or duel_now()))


def _queue_in_flight_target(queue_key, queue):
    in_flight = queue.get("in_flight") or {}
    try:
        explicit_target = normalize_duel_target(in_flight.get("target_username"))
    except ValueError:
        explicit_target = ""
    if explicit_target:
        return explicit_target
    participant_key = str(in_flight.get("participant_key") or "")
    participant = duel_participant_config(queue_key, participant_key)
    if not participant:
        return ""
    participant_state = (queue.get("participants") or {}).get(participant_key) or {}
    try:
        return (
            normalize_duel_target(participant_state.get("target_username"))
            or DUEL_QUEUES[queue_key]["target"]
        )
    except ValueError:
        return DUEL_QUEUES[queue_key]["target"]


def _target_blocked_until(data, target_username, now=None):
    now = now or duel_now()
    target_key = normalize_duel_target(target_username).lower()
    blocked_until = parse_duel_time((data.get("target_next_at") or {}).get(target_key))
    for queue_key, queue in (data.get("queues") or {}).items():
        in_flight = queue.get("in_flight") or {}
        if not _lease_active(in_flight, now):
            continue
        active_target = _queue_in_flight_target(queue_key, queue)
        if active_target.lower() != target_key:
            continue
        lease_until = parse_duel_time(in_flight.get("lease_until"))
        if lease_until and (blocked_until is None or lease_until > blocked_until):
            blocked_until = lease_until
    multi_in_flight = ((data.get("multi") or {}).get("in_flight") or {})
    if _lease_active(multi_in_flight, now):
        try:
            active_target = normalize_duel_target(
                multi_in_flight.get("target_username")
            )
        except ValueError:
            active_target = ""
        if active_target.lower() == target_key:
            lease_until = parse_duel_time(multi_in_flight.get("lease_until"))
            if lease_until and (blocked_until is None or lease_until > blocked_until):
                blocked_until = lease_until
    return blocked_until if blocked_until and blocked_until > now else None


def _multi_target_by_id(multi, target_id):
    target_id = str(target_id or "")
    for target in multi.get("targets") or []:
        if str(target.get("id") or "") == target_id:
            return target
    return None


def _multi_target_activation(target_username):
    target = duel_identity_for_username(target_username)
    if not target or target.get("identity") == "主魂":
        return None
    return target


def claim_duel_target_preparation(account):
    account = str(account or "").strip().lower()
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        multi = data["multi"]
        preparation = multi.get("preparation") or {}
        if (
            not data.get("enabled")
            or not multi.get("enabled")
            or str(preparation.get("owner") or "") != account
            or str(preparation.get("status") or "") != "pending"
            or not _lease_active(preparation, now)
        ):
            return None
        preparation["status"] = "claimed"
        preparation["claimed_at"] = duel_time(now)
        preparation["lease_until"] = duel_time(
            now + timedelta(seconds=DUEL_LEASE_SECONDS)
        )
        data["updated_at"] = duel_time(now)
        _atomic_write_json(DUEL_STATE_FILE, data)
        return dict(preparation)


def finish_duel_target_preparation(claim, success, detail=""):
    if not claim:
        return False
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        multi = data["multi"]
        preparation = multi.get("preparation") or {}
        if preparation.get("run_id") != claim.get("run_id"):
            return False
        if success:
            preparation["status"] = "ready"
            preparation["ready_at"] = duel_time(now)
            preparation["lease_until"] = duel_time(
                now + timedelta(seconds=DUEL_LEASE_SECONDS)
            )
            multi["last_result"] = detail or (
                f"@{preparation.get('target_username')} · {preparation.get('target_identity')} 已激活"
            )
        else:
            multi["preparation"] = {}
            multi["next_at"] = duel_time(
                now + timedelta(seconds=DUEL_BUSY_RETRY_SECONDS)
            )
            multi["last_result"] = detail or "目标身份激活失败"
        data["updated_at"] = duel_time(now)
        _atomic_write_json(DUEL_STATE_FILE, data)
        return True


def duel_target_preparation_held(claim):
    if not claim:
        return False
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        multi = data["multi"]
        preparation = multi.get("preparation") or {}
        if preparation.get("run_id") != claim.get("run_id"):
            return False
        if not _lease_active(preparation):
            return False
        return str(preparation.get("status") or "") in {"claimed", "ready", "holding"}


def release_duel_target_preparation(claim, detail=""):
    if not claim:
        return False
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        multi = data["multi"]
        preparation = multi.get("preparation") or {}
        if preparation.get("run_id") != claim.get("run_id"):
            return False
        in_flight = multi.get("in_flight") or {}
        if in_flight and in_flight.get("preparation_run_id") == claim.get("run_id"):
            return False
        multi["preparation"] = {}
        if detail:
            multi["last_result"] = detail
        data["updated_at"] = duel_time(now)
        _atomic_write_json(DUEL_STATE_FILE, data)
        return True


def _reserve_multi_duel_locked(data, account, now):
    multi = data["multi"]
    dirty = False
    if (
        not multi.get("enabled")
        or multi.get("initiator_account") != account
        or not _queue_due(multi, now)
        or _lease_active(multi.get("in_flight"), now)
    ):
        return None, dirty
    if int(multi.get("daily_remaining", DUEL_DAILY_LIMIT) or 0) <= 0:
        multi["next_at"] = _next_day_time(now)
        multi["last_result"] = "发起身份今日主动斗法次数已用尽"
        return None, True

    targets = multi.get("targets") or []
    if not targets:
        multi["enabled"] = False
        multi["last_result"] = "尚未配置目标"
        return None, True
    cursor = int(multi.get("cursor") or 0) % len(targets)
    selected = None
    selected_index = None
    blocked_targets = []
    for offset in range(len(targets)):
        index = (cursor + offset) % len(targets)
        target = targets[index]
        if int(target.get("remaining") or 0) <= 0:
            if target.get("status") != "completed":
                target["status"] = "completed"
                dirty = True
            continue
        target_username = normalize_duel_target(target.get("username"))
        blocked_until = _target_blocked_until(data, target_username, now)
        if blocked_until:
            blocked_targets.append((blocked_until, target_username))
            continue
        selected = target
        selected_index = index
        break

    if selected is None:
        if all(int(target.get("remaining") or 0) <= 0 for target in targets):
            multi["enabled"] = False
            multi["next_at"] = ""
            multi["in_flight"] = {}
            multi["preparation"] = {}
            multi["last_result"] = "一对多计划已全部完成"
            return None, True
        if blocked_targets:
            blocked_until, target_username = min(blocked_targets, key=lambda item: item[0])
            multi["next_at"] = duel_time(blocked_until)
            multi["last_result"] = f"等待 @{target_username} 满足 11 分钟间隔"
            return None, True
        return None, dirty

    target_username = normalize_duel_target(selected.get("username"))
    activation = _multi_target_activation(target_username)
    preparation = multi.get("preparation") or {}
    if preparation and not _lease_active(preparation, now):
        multi["preparation"] = {}
        preparation = {}
        dirty = True
    if activation:
        matching_preparation = (
            preparation.get("target_id") == selected.get("id")
            and preparation.get("target_username", "").lower() == target_username.lower()
            and preparation.get("target_identity") == activation["identity"]
        )
        if not matching_preparation:
            run_id = uuid.uuid4().hex
            multi["preparation"] = {
                "run_id": run_id,
                "owner": activation["account"],
                "status": "pending",
                "target_id": selected["id"],
                "target_username": target_username,
                "target_identity": activation["identity"],
                "started_at": duel_time(now),
                "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
            }
            selected["status"] = "preparing"
            multi["last_result"] = (
                f"等待 {activation['account_name']} · {activation['identity']} 激活后斗法"
            )
            return None, True
        if str(preparation.get("status") or "") != "ready":
            multi["last_result"] = (
                f"正在激活 {activation['account_name']} · {activation['identity']}"
            )
            return None, True

    initiator = duel_identity_config(
        multi.get("initiator_account"),
        multi.get("initiator_identity"),
    )
    if not initiator:
        multi["enabled"] = False
        multi["last_result"] = "发起身份配置无效"
        return None, True

    run_id = uuid.uuid4().hex
    multi["cursor"] = (selected_index + 1) % len(targets)
    multi["next_at"] = duel_time(now + timedelta(seconds=DUEL_INTERVAL_SECONDS))
    multi["last_attempt_at"] = duel_time(now)
    data.setdefault("target_next_at", {})[target_username.lower()] = duel_time(
        now + timedelta(seconds=DUEL_TARGET_INTERVAL_SECONDS)
    )
    preparation_run_id = ""
    if activation:
        preparation = multi.get("preparation") or {}
        preparation["status"] = "holding"
        preparation["lease_until"] = duel_time(
            now + timedelta(seconds=DUEL_LEASE_SECONDS)
        )
        preparation_run_id = str(preparation.get("run_id") or "")
    multi["in_flight"] = {
        "run_id": run_id,
        "owner": account,
        "target_id": selected["id"],
        "target_username": target_username,
        "preparation_run_id": preparation_run_id,
        "started_at": duel_time(now),
        "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
    }
    selected["status"] = "in_flight"
    selected["last_attempt_at"] = duel_time(now)
    return {
        "queue_key": "multi",
        "run_id": run_id,
        "participant_key": initiator["key"],
        "account": account,
        "identity": initiator["identity"],
        "challenger_username": initiator["username"],
        "target_id": selected["id"],
        "target_username": target_username,
        "preparation_run_id": preparation_run_id,
        "command": f".斗法 @{target_username}",
        "reserved_at": duel_time(now),
    }, True


def _next_day_time(now=None):
    now = now or duel_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return duel_time(tomorrow)


def _configured_titan_beast_mode():
    try:
        data = _read_duel_state_unlocked()
        return normalize_titan_beast_mode(
            ((data.get("queues") or {}).get("titan") or {}).get("beast_mode")
        )
    except Exception:
        return TITAN_BEAST_MODE_DEFAULT


def titan_target_status(desired_mode=None):
    try:
        desired_mode = normalize_titan_beast_mode(
            desired_mode if desired_mode is not None else _configured_titan_beast_mode()
        )
    except ValueError:
        desired_mode = TITAN_BEAST_MODE_DEFAULT
    result = {
        "ready": False,
        "current_identity": "",
        "beast": "六翼",
        "beast_status": "",
        "desired_mode": desired_mode,
        "desired_label": TITAN_BEAST_MODE_LABELS[desired_mode],
        "state_updated_at": "",
        "preparation_blocked": False,
        "preparation_reason": "",
    }
    try:
        with open(XIAOHAO_STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        result["current_identity"] = str(state.get("current_identity") or "")
        beast_status = ""
        for beast in state.get("beasts_cache") or []:
            if str(beast.get("full_name") or "").split("（", 1)[0].strip() == "六翼":
                beast_status = str(beast.get("status") or "")
                break
        if not beast_status and str(state.get("best_beast_name") or "").startswith("六翼"):
            beast_status = str(state.get("best_beast_status") or "")
        result["beast_status"] = beast_status
        desired_status = "出战"
        result["ready"] = result["current_identity"] == "主魂" and desired_status in beast_status
        result["state_updated_at"] = duel_time(datetime.fromtimestamp(os.path.getmtime(XIAOHAO_STATE_FILE)))
    except Exception:
        pass
    if not result["ready"]:
        send_status = xiaohao_duel_send_status()
        if send_status.get("restricted"):
            result["preparation_blocked"] = True
            result["preparation_reason"] = "小号当前无群组发送权限"
    return result


def xiaohao_duel_send_status():
    """Read the restricted account's persisted Telegram send availability."""
    result = {
        "restricted": False,
        "reason": "",
        "monitor_status": "unknown",
        "checked_at": "",
    }
    try:
        with open(XIAOHAO_STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        stop = state.get("telegram_send_protection_stop") or {}
        monitor = state.get("telegram_write_permission_monitor") or {}
        recovered = state.get("telegram_send_protection_last_recovered") or {}
        result["monitor_status"] = str(monitor.get("status") or "unknown")
        result["checked_at"] = str(monitor.get("checked_at") or "")
        if str(stop.get("reason") or "") == "write_restricted":
            result["restricted"] = True
            result["reason"] = str(stop.get("error") or stop.get("reason") or "write_restricted")
            return result
        if result["monitor_status"] == "blocked":
            checked_at = parse_duel_time(result["checked_at"])
            recovered_at = parse_duel_time(recovered.get("recovered_at"))
            if not recovered_at or not checked_at or recovered_at <= checked_at:
                result["restricted"] = True
                result["reason"] = str(monitor.get("reason") or "permission monitor blocked")
    except Exception:
        pass
    return result


def _queue_due(queue, now=None, lead_seconds=0):
    now = now or duel_now()
    next_at = parse_duel_time(queue.get("next_at"))
    return not next_at or next_at <= now + timedelta(seconds=max(0, int(lead_seconds or 0)))


def claim_titan_preparation(account):
    if str(account or "") != "xiaohao":
        return None
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue = data["queues"]["titan"]
        if titan_target_status(queue.get("beast_mode"))["ready"]:
            return None
        if not data.get("enabled") or not queue.get("enabled") or not _queue_due(queue, now, 30):
            return None
        if not any(
            bool((queue.get("participants", {}).get(duel_participant_key(item["account"], item["identity"])) or {}).get("enabled", True))
            and int((queue.get("participants", {}).get(duel_participant_key(item["account"], item["identity"])) or {}).get("remaining", DUEL_DAILY_LIMIT) or 0) > 0
            and (normalize_duel_target((queue.get("participants", {}).get(duel_participant_key(item["account"], item["identity"])) or {}).get("target_username")) or "TitanCreeper").lower() == "titancreeper"
            for item in DUEL_QUEUES["titan"]["participants"]
        ):
            return None
        if _lease_active(queue.get("in_flight"), now) or _lease_active(queue.get("preparation"), now):
            return None
        run_id = uuid.uuid4().hex
        queue["preparation"] = {
            "run_id": run_id,
            "owner": "xiaohao",
            "started_at": duel_time(now),
            "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
        }
        queue["last_result"] = "正在准备 TitanCreeper"
        _atomic_write_json(DUEL_STATE_FILE, data)
        return {"queue_key": "titan", "run_id": run_id}


def finish_titan_preparation(claim, success, detail=""):
    if not claim:
        return
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue = data["queues"]["titan"]
        preparation = queue.get("preparation") or {}
        if preparation.get("run_id") != claim.get("run_id"):
            return
        queue["preparation"] = {}
        queue["target_ready_at"] = duel_time() if success else ""
        queue["last_result"] = detail or ("TitanCreeper 已就绪" if success else "TitanCreeper 未就绪")
        if not success:
            queue["next_at"] = duel_time(duel_now() + timedelta(seconds=60))
        _atomic_write_json(DUEL_STATE_FILE, data)


def reserve_duel_for_account(account):
    account = str(account or "").strip()
    if account not in DUEL_ACCOUNT_LABELS:
        return None
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        if not data.get("enabled"):
            return None
        multi_reservation, dirty = _reserve_multi_duel_locked(data, account, now)
        if multi_reservation:
            data["updated_at"] = duel_time(now)
            _atomic_write_json(DUEL_STATE_FILE, data)
            return multi_reservation
        multi = data["multi"]
        if (
            multi.get("enabled")
            and multi.get("initiator_account") == account
            and _queue_due(multi, now)
            and _lease_active(multi.get("preparation"), now)
        ):
            if dirty:
                data["updated_at"] = duel_time(now)
                _atomic_write_json(DUEL_STATE_FILE, data)
            return None
        for queue_key, config in DUEL_QUEUES.items():
            queue = data["queues"][queue_key]
            if not queue.get("enabled") or not _queue_due(queue, now):
                continue
            if _lease_active(queue.get("in_flight"), now) or _lease_active(queue.get("preparation"), now):
                continue
            participants = list(config["participants"])
            xiaohao_send = xiaohao_duel_send_status() if queue_key == "waaiging" else {"restricted": False}
            cursor = int(queue.get("cursor") or 0) % len(participants)
            selected = None
            selected_index = None
            selected_target = ""
            blocked_targets = []
            skipped_restricted_xiaohao = False
            for offset in range(len(participants)):
                index = (cursor + offset) % len(participants)
                item = participants[index]
                key = duel_participant_key(item["account"], item["identity"])
                pstate = queue["participants"].setdefault(key, _new_participant_state())
                if not bool(pstate.get("enabled", True)):
                    if pstate.get("status") != "paused":
                        pstate["status"] = "paused"
                        dirty = True
                    continue
                if int(pstate.get("remaining", DUEL_DAILY_LIMIT) or 0) <= 0:
                    if pstate.get("status") != "exhausted":
                        pstate["status"] = "exhausted"
                        dirty = True
                    continue
                if xiaohao_send.get("restricted") and item["account"] == "xiaohao":
                    skipped_restricted_xiaohao = True
                    continue
                target_username = normalize_duel_target(pstate.get("target_username")) or config["target"]
                blocked_until = _target_blocked_until(data, target_username, now)
                if blocked_until:
                    blocked_targets.append((blocked_until, target_username))
                    continue
                selected = item
                selected_index = index
                selected_target = target_username
                break
            if selected is None:
                if skipped_restricted_xiaohao:
                    queue["next_at"] = duel_time(
                        now + timedelta(seconds=DUEL_RESTRICTED_ACCOUNT_RETRY_SECONDS)
                    )
                    queue["last_result"] = "等待小号发送权限恢复"
                    dirty = True
                    continue
                if blocked_targets:
                    blocked_until, target_username = min(blocked_targets, key=lambda item: item[0])
                    queue["next_at"] = duel_time(blocked_until)
                    queue["last_result"] = f"等待 @{target_username} 可再次斗法"
                    dirty = True
                    continue
                if any(not bool(state.get("enabled", True)) for state in queue.get("participants", {}).values()):
                    queue["next_at"] = ""
                    queue["last_result"] = "全部可用身份已暂停"
                    dirty = True
                    continue
                queue["next_at"] = _next_day_time(now)
                queue["last_result"] = "今日次数均已用尽"
                dirty = True
                continue
            if selected["account"] != account:
                continue

            target_username = selected_target
            if (
                queue_key == "titan"
                and target_username.lower() == "titancreeper"
            ):
                titan_status = titan_target_status(queue.get("beast_mode"))
                if titan_status["ready"]:
                    titan_status = None
            else:
                titan_status = None
            if titan_status is not None:
                desired_label = TITAN_BEAST_MODE_LABELS[queue.get("beast_mode", TITAN_BEAST_MODE_DEFAULT)]
                waiting_result = (
                    f"等待小号群组发送权限恢复后切换六翼{desired_label}"
                    if titan_status.get("preparation_blocked")
                    else f"等待小号主魂与六翼{desired_label}"
                )
                if queue.get("last_result") != waiting_result:
                    queue["last_result"] = waiting_result
                    queue["next_at"] = duel_time(now + timedelta(seconds=60))
                    dirty = True
                continue

            key = duel_participant_key(selected["account"], selected["identity"])
            run_id = uuid.uuid4().hex
            queue["cursor"] = (selected_index + 1) % len(participants)
            queue["next_at"] = duel_time(now + timedelta(seconds=DUEL_INTERVAL_SECONDS))
            queue["last_attempt_at"] = duel_time(now)
            data.setdefault("target_next_at", {})[target_username.lower()] = duel_time(
                now + timedelta(seconds=DUEL_TARGET_INTERVAL_SECONDS)
            )
            queue["in_flight"] = {
                "run_id": run_id,
                "participant_key": key,
                "owner": account,
                "started_at": duel_time(now),
                "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
                "target_username": target_username,
            }
            pstate = queue["participants"][key]
            pstate["status"] = "in_flight"
            pstate["last_attempt_at"] = duel_time(now)
            data["updated_at"] = duel_time(now)
            _atomic_write_json(DUEL_STATE_FILE, data)
            return {
                "queue_key": queue_key,
                "run_id": run_id,
                "participant_key": key,
                "account": account,
                "identity": selected["identity"],
                "challenger_username": selected["username"],
                "target_username": target_username,
                "command": f".斗法 @{target_username}",
                "reserved_at": duel_time(now),
            }
        if dirty:
            data["updated_at"] = duel_time(now)
            _atomic_write_json(DUEL_STATE_FILE, data)
    return None


def finish_duel_reservation(reservation, result):
    if not reservation:
        return
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        if reservation.get("queue_key") == "multi":
            multi = data["multi"]
            in_flight = multi.get("in_flight") or {}
            if in_flight.get("run_id") != reservation.get("run_id"):
                return
            target = _multi_target_by_id(multi, reservation.get("target_id"))
            status = str(result.get("status") or "unknown")
            outcome = str(result.get("outcome") or status)
            if target is not None:
                target["last_result"] = outcome
                if status == "settled":
                    target["attempts"] = min(
                        int(target.get("count") or 0),
                        int(target.get("attempts") or 0) + 1,
                    )
                    target["remaining"] = max(0, int(target.get("remaining") or 0) - 1)
                    target["status"] = "completed" if target["remaining"] <= 0 else "ready"
                    multi["last_success_at"] = duel_time(now)
                else:
                    target["status"] = "ready"

            reported_remaining = result.get("remaining")
            if status == "settled":
                if reported_remaining is None:
                    reported_remaining = max(
                        0,
                        int(multi.get("daily_remaining", DUEL_DAILY_LIMIT) or 0) - 1,
                    )
                multi["daily_remaining"] = max(
                    0,
                    min(DUEL_DAILY_LIMIT, int(reported_remaining)),
                )
            elif status == "exhausted":
                multi["daily_remaining"] = 0
                multi["daily_exhausted_date"] = duel_date(now)

            multi["last_result"] = f"@{reservation['target_username']} · {outcome}"
            multi["in_flight"] = {}
            preparation = multi.get("preparation") or {}
            if preparation.get("run_id") == reservation.get("preparation_run_id"):
                multi["preparation"] = {}

            all_completed = bool(multi.get("targets")) and all(
                int(item.get("remaining") or 0) <= 0
                for item in multi.get("targets") or []
            )
            if all_completed:
                multi["enabled"] = False
                multi["next_at"] = ""
                multi["last_result"] = "一对多计划已全部完成"
            elif status == "exhausted" or int(multi.get("daily_remaining") or 0) <= 0:
                multi["next_at"] = _next_day_time(now)
            else:
                multi["next_at"] = duel_time(
                    now + timedelta(seconds=DUEL_INTERVAL_SECONDS)
                )

            target_key = normalize_duel_target(
                reservation.get("target_username")
            ).lower()
            target_next_at = data.setdefault("target_next_at", {})
            if status == "cooldown":
                target_ready = now + timedelta(
                    seconds=max(
                        DUEL_TARGET_INTERVAL_SECONDS,
                        int(result.get("wait_seconds") or 0),
                    )
                )
            else:
                target_ready = now + timedelta(seconds=DUEL_TARGET_INTERVAL_SECONDS)
            reserved_ready = parse_duel_time(target_next_at.get(target_key))
            target_next_at[target_key] = duel_time(
                max(target_ready, reserved_ready) if reserved_ready else target_ready
            )
            data["updated_at"] = duel_time(now)
            _atomic_write_json(DUEL_STATE_FILE, data)
            return

        queue = data["queues"].get(reservation.get("queue_key"), {})
        in_flight = queue.get("in_flight") or {}
        if in_flight.get("run_id") != reservation.get("run_id"):
            return
        key = reservation["participant_key"]
        pstate = queue.get("participants", {}).setdefault(key, _new_participant_state())
        status = str(result.get("status") or "unknown")
        outcome = str(result.get("outcome") or status)
        remaining = result.get("remaining")
        if status == "settled":
            if remaining is None:
                remaining = max(0, int(pstate.get("remaining", DUEL_DAILY_LIMIT) or 0) - 1)
            remaining = max(0, min(DUEL_DAILY_LIMIT, int(remaining)))
            pstate["remaining"] = remaining
            pstate["attempts"] = max(int(pstate.get("attempts") or 0), DUEL_DAILY_LIMIT - remaining)
            if outcome == "胜利":
                pstate["wins"] = int(pstate.get("wins") or 0) + 1
            elif outcome == "失败":
                pstate["losses"] = int(pstate.get("losses") or 0) + 1
            queue["last_success_at"] = duel_time(now)
        elif status == "exhausted":
            pstate["remaining"] = 0
            pstate["attempts"] = DUEL_DAILY_LIMIT
        pstate["status"] = "exhausted" if int(pstate.get("remaining", 0) or 0) <= 0 else "ready"
        pstate["last_result"] = outcome
        queue["last_result"] = f"{reservation['identity']} · {outcome}"
        queue["in_flight"] = {}
        retry_seconds = DUEL_BUSY_RETRY_SECONDS if status == "busy" else DUEL_INTERVAL_SECONDS
        if status == "cooldown":
            retry_seconds = max(retry_seconds, int(result.get("wait_seconds") or 0))
        completed_next_at = now + timedelta(seconds=retry_seconds)
        reserved_next_at = parse_duel_time(queue.get("next_at"))
        if status == "busy":
            queue["next_at"] = duel_time(completed_next_at)
        else:
            queue["next_at"] = (
                duel_time(max(completed_next_at, reserved_next_at))
                if reserved_next_at else duel_time(completed_next_at)
            )
        target_key = normalize_duel_target(reservation.get("target_username")).lower()
        target_next_at = data.setdefault("target_next_at", {})
        reserved_target_next_at = parse_duel_time(target_next_at.get(target_key))
        target_completed_at = now + timedelta(
            seconds=max(DUEL_TARGET_INTERVAL_SECONDS, retry_seconds)
        )
        target_next_at[target_key] = (
            duel_time(max(target_completed_at, reserved_target_next_at))
            if reserved_target_next_at else duel_time(target_completed_at)
        )
        data["updated_at"] = duel_time(now)
        _atomic_write_json(DUEL_STATE_FILE, data)


def _clean_duel_text(text):
    return str(text or "").replace("**", "").replace("`", "").strip()


def parse_duel_amount(value):
    text = str(value or "").replace(",", "").replace("＋", "+").strip()
    match = re.search(r"([+-]?\d+(?:\.\d+)?)(万|亿)?", text)
    if not match:
        return 0
    number = float(match.group(1))
    if match.group(2) == "万":
        number *= 10000
    elif match.group(2) == "亿":
        number *= 100000000
    return int(round(number))


def duel_wait_seconds(text):
    clean = _clean_duel_text(text)
    total = 0
    for value, unit in re.findall(r"(\d+)\s*(小时|分钟|秒)", clean):
        multiplier = 3600 if unit == "小时" else 60 if unit == "分钟" else 1
        total += int(value) * multiplier
    return total


def duel_text_is_pending(text):
    clean = _clean_duel_text(text)
    if not clean:
        return False
    if "正在锁定对手天机" in clean or "天机阁正在推演战局" in clean:
        return True
    if "战斗结束" in clean and "正在整理天道战报" in clean:
        return True
    return "法宝齐出" in clean and "胜者：" not in clean and "胜负已分" not in clean


def duel_text_is_final(text):
    clean = _clean_duel_text(text)
    return bool(clean and any(marker in clean for marker in (
        "胜者：", "胜者:", "胜负已分", "今日神念", "今日剩余神念",
        "元神尚未平复", "每日可主动斗法", "无力再战",
        "尚未踏入仙途", "无法再次斗法", "不可斗法", "不能斗法",
        "天机繁忙", "因果纠缠", "侥幸逃脱",
    )) and not duel_text_is_pending(clean))


def _duel_user_line_value(clean, username, marker):
    username = str(username or "").lower().lstrip("@")
    for line in clean.splitlines():
        if f"@{username}" not in line.lower() or marker not in line:
            continue
        match = re.search(rf"{re.escape(marker)}\s*([+＋-]?\d+(?:\.\d+)?\s*(?:万|亿)?)", line)
        if match:
            return parse_duel_amount(match.group(1))
    return 0


def parse_duel_result(text, challenger_username, target_username):
    clean = _clean_duel_text(text)
    result = {
        "status": "unknown",
        "outcome": "未识别",
        "winner": "",
        "loser": "",
        "remaining": None,
        "cultivation_delta": 0,
        "artifact_wear": 0,
        "wait_seconds": 0,
        "text": clean,
    }
    remaining = re.search(r"今日(?:剩余)?神念\s*[:：]?\s*(\d+)\s*/\s*10", clean)
    if remaining:
        result["remaining"] = int(remaining.group(1))
    if "每日可主动斗法" in clean or "已无力再战" in clean or "神念消耗过剧" in clean:
        result.update(status="exhausted", outcome="次数用尽", remaining=0)
        return result
    if "元神尚未平复" in clean or "无法再次斗法" in clean:
        result.update(status="cooldown", outcome="目标冷却", wait_seconds=duel_wait_seconds(clean))
        return result
    if "天机繁忙" in clean or "因果纠缠" in clean:
        result.update(status="busy", outcome="目标繁忙", wait_seconds=DUEL_BUSY_RETRY_SECONDS)
        return result
    escaped = re.search(r"@([A-Za-z0-9_]+)[^\n]*侥幸逃脱", clean, re.I)
    if escaped:
        result["status"] = "settled"
        challenger = str(challenger_username or "").lower().lstrip("@")
        result["outcome"] = "逃脱" if escaped.group(1).lower() == challenger else "目标逃脱"
        return result
    if "尚未踏入仙途" in clean or "不可斗法" in clean or "不能斗法" in clean:
        result.update(status="unavailable", outcome="目标不可用")
        return result
    if duel_text_is_pending(clean):
        result.update(status="pending", outcome="结算中")
        return result

    winner = re.search(r"胜者[：:]\s*@([A-Za-z0-9_]+)", clean, re.I)
    loser = re.search(r"败者[：:]\s*@([A-Za-z0-9_]+)", clean, re.I)
    if winner:
        result["winner"] = winner.group(1)
    if loser:
        result["loser"] = loser.group(1)
    challenger = str(challenger_username or "").lower().lstrip("@")
    if winner:
        result["status"] = "settled"
        result["outcome"] = "胜利" if winner.group(1).lower() == challenger else "失败"
        result["cultivation_delta"] = (
            _duel_user_line_value(clean, challenger, "净得修为")
            or _duel_user_line_value(clean, challenger, "损失修为")
        )
        if result["outcome"] == "失败" and result["cultivation_delta"] > 0:
            result["cultivation_delta"] *= -1
        wear = _duel_user_line_value(clean, challenger, "法宝磨损")
        result["artifact_wear"] = -abs(wear) if wear else 0
        return result
    if not clean:
        result.update(status="timeout", outcome="无回复")
    return result


def duel_reply_to_message_id(message):
    reply_to = getattr(message, "reply_to", None)
    value = getattr(reply_to, "reply_to_msg_id", None) if reply_to is not None else None
    if value is None:
        value = getattr(message, "reply_to_msg_id", None)
    try:
        return int(value) if value is not None else None
    except Exception:
        return None


async def wait_for_duel_result(actor, response_msg, command_msg_id=None, timeout=DUEL_RESULT_WAIT_SECONDS):
    current = response_msg
    text = getattr(current, "text", "") if current is not None else ""
    if duel_text_is_final(text):
        return current, text
    msg_id = getattr(current, "id", None)
    if not msg_id:
        return current, text
    command_msg_id = command_msg_id or duel_reply_to_message_id(current)
    deadline = time.monotonic() + max(1, int(timeout or DUEL_RESULT_WAIT_SECONDS))
    while time.monotonic() < deadline and getattr(actor, "is_running", True):
        await asyncio.sleep(1)
        try:
            fresh = await actor.client.get_messages(actor.target_chat_id, ids=msg_id)
            if isinstance(fresh, (list, tuple)):
                fresh = fresh[0] if fresh else None
            if fresh is not None:
                current = fresh
                text = getattr(fresh, "text", "") or text
        except Exception:
            pass
        if duel_text_is_final(text):
            break
        if command_msg_id:
            try:
                messages = await actor.client.get_messages(
                    actor.target_chat_id,
                    limit=40,
                    min_id=max(0, int(msg_id) - 1),
                )
                if not isinstance(messages, (list, tuple)):
                    messages = [messages] if messages is not None else []
                for candidate in messages:
                    candidate_text = getattr(candidate, "text", "") or ""
                    if duel_reply_to_message_id(candidate) != int(command_msg_id):
                        continue
                    if duel_text_is_final(candidate_text):
                        return candidate, candidate_text
            except Exception:
                pass
    return current, text


def ensure_duel_events_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS duel_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            event_date TEXT NOT NULL,
            event_time TEXT NOT NULL,
            queue_key TEXT NOT NULL,
            account TEXT NOT NULL,
            identity TEXT NOT NULL,
            challenger_username TEXT NOT NULL,
            target_username TEXT NOT NULL,
            command TEXT NOT NULL,
            command_msg_id INTEGER,
            response_msg_id INTEGER,
            status TEXT NOT NULL,
            outcome TEXT NOT NULL,
            winner TEXT,
            loser TEXT,
            remaining INTEGER,
            cultivation_delta INTEGER NOT NULL DEFAULT 0,
            artifact_wear INTEGER NOT NULL DEFAULT 0,
            result_text TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_duel_events_date_time ON duel_events(event_date,event_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_duel_events_queue_date ON duel_events(queue_key,event_date,event_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_duel_events_account_identity ON duel_events(account,identity,event_date)")


def record_duel_event(reservation, result, command_msg_id=None, response_msg_id=None):
    now = duel_now()
    event_key = (
        f"{reservation['account']}:{int(command_msg_id)}"
        if command_msg_id is not None
        else f"lease:{reservation['run_id']}"
    )
    conn = sqlite3.connect(DUEL_DB_FILE, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_duel_events_schema(conn)
        conn.execute(
            """
            INSERT INTO duel_events (
                event_key,event_date,event_time,queue_key,account,identity,
                challenger_username,target_username,command,command_msg_id,response_msg_id,
                status,outcome,winner,loser,remaining,cultivation_delta,artifact_wear,
                result_text,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_key) DO UPDATE SET
                response_msg_id=excluded.response_msg_id,
                status=excluded.status,
                outcome=excluded.outcome,
                winner=excluded.winner,
                loser=excluded.loser,
                remaining=excluded.remaining,
                cultivation_delta=excluded.cultivation_delta,
                artifact_wear=excluded.artifact_wear,
                result_text=excluded.result_text,
                updated_at=excluded.updated_at
            """,
            (
                event_key, duel_date(now), duel_time(now), reservation["queue_key"],
                reservation["account"], reservation["identity"],
                reservation["challenger_username"], reservation["target_username"],
                reservation["command"], command_msg_id, response_msg_id,
                result.get("status") or "unknown", result.get("outcome") or "未识别",
                result.get("winner") or "", result.get("loser") or "", result.get("remaining"),
                int(result.get("cultivation_delta") or 0), int(result.get("artifact_wear") or 0),
                result.get("text") or "", duel_time(now), duel_time(now),
            ),
        )
        cutoff = duel_date(now - timedelta(days=DUEL_RETENTION_DAYS))
        conn.execute("DELETE FROM duel_events WHERE event_date<?", (cutoff,))
        conn.commit()
        return True
    finally:
        conn.close()


def _duel_event_rows(query_date, limit):
    if not os.path.exists(DUEL_DB_FILE):
        return []
    conn = sqlite3.connect(DUEL_DB_FILE, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        ensure_duel_events_schema(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM duel_events WHERE event_date=? ORDER BY event_time DESC,id DESC LIMIT ?",
            (query_date, max(1, min(int(limit or 200), 1000))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def duel_dashboard_payload(date="", limit=200):
    data = load_duel_state(write_back=False)
    query_date = str(date or "").strip() or duel_date()
    titan_mode = data["queues"]["titan"].get("beast_mode", TITAN_BEAST_MODE_DEFAULT)
    titan_status = titan_target_status(titan_mode)
    queues = []
    identity_options = duel_identity_options()
    target_options = sorted(
        {
            *(config["target"] for config in DUEL_QUEUES.values()),
            *(item["username"] for item in identity_options),
        },
        key=str.lower,
    )
    for queue_key, config in DUEL_QUEUES.items():
        queue_state = data["queues"][queue_key]
        cursor = int(queue_state.get("cursor") or 0) % len(config["participants"])
        next_index = None
        for offset in range(len(config["participants"])):
            candidate_index = (cursor + offset) % len(config["participants"])
            candidate = config["participants"][candidate_index]
            candidate_key = duel_participant_key(candidate["account"], candidate["identity"])
            candidate_state = queue_state.get("participants", {}).get(candidate_key) or {}
            if bool(candidate_state.get("enabled", True)) and int(candidate_state.get("remaining", DUEL_DAILY_LIMIT) or 0) > 0:
                next_index = candidate_index
                break
        participant_rows = []
        titan_required = False
        for index, participant in enumerate(config["participants"]):
            key = duel_participant_key(participant["account"], participant["identity"])
            state = dict(queue_state.get("participants", {}).get(key) or _new_participant_state())
            custom_target = normalize_duel_target(state.get("target_username"))
            effective_target = custom_target or config["target"]
            state["enabled"] = bool(state.get("enabled", True))
            state["target_username"] = effective_target
            state["target_is_default"] = not bool(custom_target)
            titan_required = titan_required or (
                state["enabled"] and int(state.get("remaining", DUEL_DAILY_LIMIT) or 0) > 0
                and effective_target.lower() == "titancreeper"
            )
            participant_rows.append({
                **participant,
                "key": key,
                "account_name": DUEL_ACCOUNT_LABELS.get(participant["account"], participant["account"]),
                "is_next": index == next_index,
                **state,
            })
        queues.append({
            "key": queue_key,
            "label": config["label"],
            "target": f"@{config['target']}",
            "enabled": bool(queue_state.get("enabled")),
            "next_at": queue_state.get("next_at") or "",
            "last_attempt_at": queue_state.get("last_attempt_at") or "",
            "last_success_at": queue_state.get("last_success_at") or "",
            "last_result": queue_state.get("last_result") or "",
            "in_flight": queue_state.get("in_flight") or {},
            "participants": participant_rows,
            "target_status": titan_status if queue_key == "titan" else {"ready": True},
            "requires_titan_preparation": queue_key == "titan" and titan_required,
        })

    multi_state = data["multi"]
    initiator = duel_identity_config(
        multi_state.get("initiator_account"),
        multi_state.get("initiator_identity"),
    )
    multi_targets = []
    raw_multi_targets = multi_state.get("targets") or []
    next_multi_index = None
    if raw_multi_targets:
        cursor = int(multi_state.get("cursor") or 0) % len(raw_multi_targets)
        for offset in range(len(raw_multi_targets)):
            index = (cursor + offset) % len(raw_multi_targets)
            if int(raw_multi_targets[index].get("remaining") or 0) > 0:
                next_multi_index = index
                break
    for index, target in enumerate(raw_multi_targets):
        target_identity = duel_identity_for_username(target.get("username"))
        multi_targets.append({
            **target,
            "is_next": index == next_multi_index,
            "known_identity": bool(target_identity),
            "activation_required": bool(
                target_identity and target_identity.get("identity") != "主魂"
            ),
            "target_account": (target_identity or {}).get("account", ""),
            "target_account_name": (target_identity or {}).get("account_name", ""),
            "target_identity": (target_identity or {}).get("identity", ""),
            "target_label": (
                f"{target_identity['account_name']} · {target_identity['identity']}"
                if target_identity else "外部用户名"
            ),
        })
    total_planned = sum(int(item.get("count") or 0) for item in multi_targets)
    total_remaining = sum(int(item.get("remaining") or 0) for item in multi_targets)
    multi_payload = {
        "configured": bool(initiator and multi_targets),
        "enabled": bool(multi_state.get("enabled")),
        "initiator_account": multi_state.get("initiator_account") or "",
        "initiator_identity": multi_state.get("initiator_identity") or "",
        "initiator": initiator or {},
        "next_at": multi_state.get("next_at") or "",
        "last_attempt_at": multi_state.get("last_attempt_at") or "",
        "last_success_at": multi_state.get("last_success_at") or "",
        "last_result": multi_state.get("last_result") or "",
        "daily_remaining": int(multi_state.get("daily_remaining", DUEL_DAILY_LIMIT) or 0),
        "in_flight": multi_state.get("in_flight") or {},
        "preparation": multi_state.get("preparation") or {},
        "targets": multi_targets,
        "total_planned": total_planned,
        "total_completed": total_planned - total_remaining,
        "total_remaining": total_remaining,
    }
    rows = _duel_event_rows(query_date, limit)
    summary = {
        "count": len(rows),
        "wins": sum(1 for row in rows if row.get("outcome") == "胜利"),
        "losses": sum(1 for row in rows if row.get("outcome") == "失败"),
        "blocked": sum(1 for row in rows if row.get("status") not in {"settled"}),
    }
    return {
        "enabled": bool(data.get("enabled")),
        "date": query_date,
        "interval_seconds": DUEL_INTERVAL_SECONDS,
        "target_interval_seconds": DUEL_TARGET_INTERVAL_SECONDS,
        "daily_limit": DUEL_DAILY_LIMIT,
        "target_options": target_options,
        "identity_options": identity_options,
        "updated_at": data.get("updated_at") or duel_time(),
        "multi": multi_payload,
        "queues": queues,
        "rows": rows,
        "summary": summary,
    }


class DuelMixin:
    """Actor methods for claiming and executing this account's shared duel turns."""

    def duel_logger(self):
        logger = getattr(self, "log", None)
        return logger if logger is not None else logging.getLogger(self.__class__.__name__)

    async def prepare_duel_target_identity(self, claim):
        account = str(getattr(self, "account_key", "") or "")
        if str(claim.get("owner") or "") != account:
            return False, "目标身份准备账号不匹配"
        identity = str(claim.get("target_identity") or "").strip()
        username = str(claim.get("target_username") or "").strip()
        if not identity or identity == "主魂" or identity not in getattr(self, "avatars", []):
            return False, "目标不是可切换分身"
        prepare = getattr(self, "prepare_identity_for_time_critical_command", None)
        if not callable(prepare):
            return False, "账号缺少分身预切换能力"
        success = await prepare(
            identity,
            command=f".斗法 @{username}",
            timeout=30,
        )
        if not success or str(getattr(self, "current_identity", "") or "") != identity:
            return False, f"{identity} 激活未确认"
        return True, f"@{username} · {identity} 已激活并保持等待斗法"

    async def _run_duel_target_preparation_if_due(self):
        claim = claim_duel_target_preparation(getattr(self, "account_key", ""))
        if not claim:
            return False
        logger = self.duel_logger()

        async def prepare_and_hold():
            success, detail = await self.prepare_duel_target_identity(claim)
            finish_duel_target_preparation(claim, success, detail)
            logger.info(
                "Duel target preparation: target=@%s identity=%s success=%s detail=%s",
                claim.get("target_username"), claim.get("target_identity"), success, detail,
            )
            if not success:
                return
            deadline = time.monotonic() + DUEL_LEASE_SECONDS
            while (
                getattr(self, "is_running", True)
                and time.monotonic() < deadline
                and duel_target_preparation_held(claim)
            ):
                await asyncio.sleep(1)
            release_duel_target_preparation(
                claim,
                detail=(
                    f"@{claim.get('target_username')} 分身激活等待超时，稍后重试"
                    if time.monotonic() >= deadline else ""
                ),
            )

        atomic = getattr(self, "common_atomic_task", None)
        if callable(atomic):
            async with atomic(f"Duel-target-{claim.get('target_identity')}"):
                await prepare_and_hold()
        else:
            await prepare_and_hold()
        return True

    async def prepare_titan_target_for_duel(self):
        if str(getattr(self, "account_key", "") or "") != "xiaohao":
            return False, "仅小号可准备 TitanCreeper"
        logger = self.duel_logger()
        status = titan_target_status()
        if status["ready"]:
            return True, f"TitanCreeper 主魂与六翼已{status['desired_label']}"

        beast_lock = getattr(self, "beast_lock", None)

        async def prepare():
            if str(getattr(self, "current_identity", "") or "") != "主魂":
                switch_back = getattr(self, "switch_back_to_main", None)
                if not callable(switch_back):
                    return False, "小号缺少主魂切换能力"
                await switch_back(force=True)
                if str(getattr(self, "current_identity", "") or "") != "主魂":
                    return False, "小号主魂切换未确认"

            desired = titan_target_status()
            if desired["ready"]:
                return True, f"TitanCreeper 主魂与六翼已{desired['desired_label']}"

            focus = self.get_cached_beast_by_name("六翼") if hasattr(self, "get_cached_beast_by_name") else None
            beast_status = str((focus or {}).get("status") or self.state.get("best_beast_status") or "")
            last_response = ""
            if "出战" not in beast_status:
                if "放养" in beast_status:
                    rest_resp = await self.send_and_wait_feedback(
                        ".灵兽休息 六翼", timeout=60, max_retries=0, force_identity_check=True
                    )
                    rest_text = self.response_text(rest_resp) if hasattr(self, "response_text") else str(rest_resp or "")
                    rest_status = self.parse_rest_response_status(rest_text) if hasattr(self, "parse_rest_response_status") else ""
                    if rest_status and hasattr(self, "set_best_beast_status"):
                        self.set_best_beast_status("六翼", rest_status)
                    if not rest_status:
                        return False, f"六翼召回失败：{rest_text[:80] or '无回复'}"
                    await asyncio.sleep(2)
                deploy_resp = await self.send_and_wait_feedback(
                    ".灵兽出战 六翼", timeout=60, max_retries=0, force_identity_check=True
                )
                last_response = self.response_text(deploy_resp) if hasattr(self, "response_text") else str(deploy_resp or "")
                if not self.is_beast_deploy_success(last_response):
                    if hasattr(self, "record_beast_current_status_response"):
                        self.record_beast_current_status_response(last_response, "六翼", source="斗法准备")
                    return False, f"六翼出战失败：{last_response[:80] or '无回复'}"
                if hasattr(self, "set_best_beast_status"):
                    self.set_best_beast_status("六翼", "出战中")

            current = titan_target_status()
            if current["ready"]:
                return True, f"TitanCreeper 主魂与六翼已{current['desired_label']}"
            actual = current.get("beast_status") or "状态未知"
            detail = f"六翼未达到{current['desired_label']}状态（当前：{actual}）"
            if last_response:
                detail = f"{detail}：{last_response[:80]}"
            return False, detail

        try:
            if beast_lock is None:
                return await prepare()
            async with beast_lock:
                return await prepare()
        except Exception as exc:
            logger.exception("Duel Titan preparation failed")
            return False, f"TitanCreeper 准备异常：{exc}"

    async def _run_titan_preparation_if_due(self):
        claim = claim_titan_preparation(getattr(self, "account_key", ""))
        if not claim:
            return False
        success, detail = await self.prepare_titan_target_for_duel()
        finish_titan_preparation(claim, success, detail)
        self.duel_logger().info("Duel Titan preparation: success=%s detail=%s", success, detail)
        return True

    async def execute_duel_reservation(self, reservation):
        logger = self.duel_logger()
        command_msg_id = None
        response_msg_id = None
        result = {"status": "error", "outcome": "执行异常", "text": ""}
        try:
            logger.info(
                "Duel turn [%s]: %s/%s -> @%s",
                reservation["queue_key"], reservation["account"], reservation["identity"],
                reservation["target_username"],
            )
            response_msg = await self.send_and_wait_feedback_identity(
                reservation["identity"],
                reservation["command"],
                timeout=60,
                max_retries=0,
                return_response_msg=True,
                delete_after=False,
                force_identity_check=True,
            )
            command_msg_id = duel_reply_to_message_id(response_msg) or getattr(self, "last_sent_id", None)
            final_msg, final_text = await wait_for_duel_result(
                self,
                response_msg,
                command_msg_id=command_msg_id,
            )
            response_msg_id = getattr(final_msg, "id", None)
            result = parse_duel_result(
                final_text,
                reservation["challenger_username"],
                reservation["target_username"],
            )
            logger.info(
                "Duel result [%s]: %s/%s outcome=%s remaining=%s",
                reservation["queue_key"], reservation["account"], reservation["identity"],
                result.get("outcome"), result.get("remaining"),
            )
        except Exception as exc:
            result = {"status": "error", "outcome": "执行异常", "text": str(exc)}
            logger.exception("Duel turn crashed: %s", reservation)
        finally:
            try:
                record_duel_event(
                    reservation,
                    result,
                    command_msg_id=command_msg_id,
                    response_msg_id=response_msg_id,
                )
            finally:
                finish_duel_reservation(reservation, result)
        return result

    async def run_duel_scheduler(self, initial_delay=20):
        logger = self.duel_logger()
        startup_done = getattr(self, "startup_done", None)
        if startup_done is not None:
            await startup_done.wait()
        if initial_delay:
            await asyncio.sleep(max(0, int(initial_delay)))
        logger.info("Shared duel scheduler started for account=%s", getattr(self, "account_key", ""))
        while getattr(self, "is_running", True):
            try:
                if await self._run_duel_target_preparation_if_due():
                    await asyncio.sleep(1)
                if str(getattr(self, "account_key", "") or "") == "xiaohao":
                    if await self._run_titan_preparation_if_due():
                        await asyncio.sleep(1)
                reservation = reserve_duel_for_account(getattr(self, "account_key", ""))
                if reservation:
                    await self.execute_duel_reservation(reservation)
                    await asyncio.sleep(1)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shared duel scheduler iteration failed")
            await asyncio.sleep(DUEL_POLL_SECONDS)
