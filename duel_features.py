#!/usr/bin/env python3
"""Shared duel schedulers for the all-identity rotation and one-to-many plans."""

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

from automation_settings import current_sub_yinluo_identity

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
DUEL_INTERVAL_MIN_SECONDS = 1
DUEL_INTERVAL_MAX_SECONDS = 7 * 24 * 3600
DUEL_DAILY_LIMIT = 10
DUEL_LEASE_SECONDS = 4 * 60
DUEL_RESULT_WAIT_SECONDS = 90
DUEL_POLL_SECONDS = 3
DUEL_RETENTION_DAYS = 30
DUEL_RESTRICTED_ACCOUNT_RETRY_SECONDS = 60
DUEL_BUSY_RETRY_SECONDS = 60
DUEL_IDENTITY_PAUSE_RETRY_SECONDS = 5 * 60
DUEL_ROLLING_TARGET_COOLDOWN_SECONDS = 24 * 3600
DUEL_MULTI_MAX_TARGETS = 20
DUEL_MULTI_MAX_COUNT = 999
DUEL_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,64}$")
DUEL_STATE_VERSION = 4
DUEL_ROTATION_QUEUE_KEY = "rotation"
DUEL_ROTATION_DEFAULT_TARGET = "Waaiging"
DUEL_LEGACY_QUEUE_TARGETS = {
    "waaiging": "Waaiging",
    "titan": "TitanCreeper",
}
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
        {"identity": current_sub_yinluo_identity(), "username": "Lvdoumiao"},
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
    DUEL_ROTATION_QUEUE_KEY: {
        "label": "斗法轮换",
        "target": DUEL_ROTATION_DEFAULT_TARGET,
        "participants": tuple(
            {
                "account": account,
                "identity": item["identity"],
                "username": item["username"],
            }
            for account, identities in DUEL_IDENTITIES.items()
            for item in identities
        ),
    },
}


def duel_now():
    return datetime.now()


def duel_time(value=None):
    return (value or duel_now()).strftime(TIME_FORMAT)


def duel_date(value=None):
    return (value or duel_now()).strftime("%Y-%m-%d")


def duel_duration_label(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds and seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    minutes, remainder = divmod(seconds, 60)
    if minutes and not remainder:
        return f"{minutes} 分钟"
    if minutes:
        return f"{minutes}分{remainder}秒"
    return f"{seconds} 秒"


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


def refresh_duel_identity_name(account, old_identity, new_identity):
    """Update a reborn avatar's Dao name in duel routing and pending plans."""
    account = str(account or "").strip().lower()
    old_identity = str(old_identity or "").strip()
    new_identity = str(new_identity or "").strip()
    if not account or not old_identity or not new_identity or old_identity == new_identity:
        return False

    changed = False
    for item in DUEL_IDENTITIES.get(account, ()):
        if item.get("identity") == old_identity:
            item["identity"] = new_identity
            changed = True
    for queue in DUEL_QUEUES.values():
        for participant in queue.get("participants", ()):
            if participant.get("account") == account and participant.get("identity") == old_identity:
                participant["identity"] = new_identity
                changed = True
    if not changed:
        return False

    old_key = duel_participant_key(account, old_identity)
    new_key = duel_participant_key(account, new_identity)

    def update_plan_references(value):
        if isinstance(value, list):
            for item in value:
                update_plan_references(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("account") == account and value.get("identity") == old_identity:
            value["identity"] = new_identity
        if value.get("target_account") == account and value.get("target_identity") == old_identity:
            value["target_identity"] = new_identity
        if value.get("initiator_account") == account and value.get("initiator_identity") == old_identity:
            value["initiator_identity"] = new_identity
        if value.get("owner") == account and value.get("target_identity") == old_identity:
            value["target_identity"] = new_identity
        if value.get("participant_key") == old_key:
            value["participant_key"] = new_key
        for key, child in tuple(value.items()):
            if key == old_key:
                value[new_key] = value.pop(key)
                child = value[new_key]
            update_plan_references(child)

    with duel_state_lock():
        data = _read_duel_state_unlocked()
        if isinstance(data, dict):
            update_plan_references(data)
            data = _ensure_duel_state_shape(data)
            data["updated_at"] = duel_time()
            _atomic_write_json(DUEL_STATE_FILE, data)
    return True


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


def duel_default_target_for_participant(account, identity="主魂"):
    """Return a safe cross-account default target for a rotation identity."""
    account = str(account or "").strip().lower()
    if account in {"sub", "waaiging"}:
        return "TitanCreeper"
    return DUEL_ROTATION_DEFAULT_TARGET


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
    participants = {}
    for item in DUEL_QUEUES[queue_key]["participants"]:
        participant = _new_participant_state()
        participant["target_username"] = duel_default_target_for_participant(
            item["account"], item["identity"]
        )
        participants[duel_participant_key(item["account"], item["identity"])] = participant
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
        "target_preparation": {},
        "participants": participants,
    }
    if queue_key == DUEL_ROTATION_QUEUE_KEY:
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
        "version": DUEL_STATE_VERSION,
        "enabled": True,
        "target_switch_enabled": True,
        "date": duel_date(),
        "updated_at": duel_time(),
        "interval_seconds": DUEL_INTERVAL_SECONDS,
        "target_interval_seconds": DUEL_TARGET_INTERVAL_SECONDS,
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


def _latest_duel_time(values):
    parsed = [parse_duel_time(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    return duel_time(max(parsed)) if parsed else ""


def _earliest_duel_time(values):
    parsed = [parse_duel_time(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    return duel_time(min(parsed)) if parsed else ""


def _migrate_legacy_duel_queues(data):
    """Merge the former Waaiging/Titan groups into the all-identity rotation."""
    queues = data.get("queues")
    if not isinstance(queues, dict):
        queues = {}
        data["queues"] = queues
    if DUEL_ROTATION_QUEUE_KEY in queues:
        return data

    legacy = {
        key: queues.get(key)
        for key in DUEL_LEGACY_QUEUE_TARGETS
        if isinstance(queues.get(key), dict)
    }
    if not legacy:
        return data

    rotation = _new_queue_state(DUEL_ROTATION_QUEUE_KEY)
    legacy_enabled = {
        key: bool(queue.get("enabled", True))
        for key, queue in legacy.items()
    }
    rotation["enabled"] = any(legacy_enabled.values())
    mixed_queue_controls = len(set(legacy_enabled.values())) > 1
    rotation["next_at"] = _earliest_duel_time(
        queue.get("next_at") for queue in legacy.values()
    )
    rotation["last_attempt_at"] = _latest_duel_time(
        queue.get("last_attempt_at") for queue in legacy.values()
    )
    rotation["last_success_at"] = _latest_duel_time(
        queue.get("last_success_at") for queue in legacy.values()
    )
    latest_queue = max(
        legacy.values(),
        key=lambda queue: parse_duel_time(queue.get("last_attempt_at")) or datetime.min,
    )
    rotation["last_result"] = str(latest_queue.get("last_result") or "已合并原 A/B 轮换")

    titan_queue = legacy.get("titan") or {}
    rotation["beast_mode"] = titan_queue.get("beast_mode", TITAN_BEAST_MODE_DEFAULT)
    rotation["target_ready_at"] = str(titan_queue.get("target_ready_at") or "")

    for item in DUEL_QUEUES[DUEL_ROTATION_QUEUE_KEY]["participants"]:
        participant_key = duel_participant_key(item["account"], item["identity"])
        for legacy_key, legacy_queue in legacy.items():
            raw = (legacy_queue.get("participants") or {}).get(participant_key)
            if not isinstance(raw, dict):
                continue
            participant = _new_participant_state()
            participant.update(raw)
            if mixed_queue_controls and not legacy_enabled[legacy_key]:
                participant["enabled"] = False
            try:
                participant["target_username"] = (
                    normalize_duel_target(participant.get("target_username"))
                    or DUEL_LEGACY_QUEUE_TARGETS[legacy_key]
                )
            except ValueError:
                participant["target_username"] = DUEL_LEGACY_QUEUE_TARGETS[legacy_key]
            rotation["participants"][participant_key] = participant
            break

    queues[DUEL_ROTATION_QUEUE_KEY] = rotation
    return data


def _ensure_duel_state_shape(data, reset_daily=True):
    if not isinstance(data, dict):
        data = duel_default_state()
    data = _migrate_legacy_duel_queues(data)
    data["version"] = DUEL_STATE_VERSION
    data.setdefault("enabled", True)
    data["target_switch_enabled"] = bool(data.get("target_switch_enabled", True))
    for field, default in (
        ("interval_seconds", DUEL_INTERVAL_SECONDS),
        ("target_interval_seconds", DUEL_TARGET_INTERVAL_SECONDS),
    ):
        try:
            seconds = int(round(float(data.get(field, default))))
        except (TypeError, ValueError):
            seconds = default
        if seconds < DUEL_INTERVAL_MIN_SECONDS or seconds > DUEL_INTERVAL_MAX_SECONDS:
            seconds = default
        data[field] = seconds
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
    if not isinstance(queues, dict):
        queues = {}
        data["queues"] = queues
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
        queue.setdefault("target_preparation", {})
        if queue_key == DUEL_ROTATION_QUEUE_KEY:
            try:
                queue["beast_mode"] = normalize_titan_beast_mode(queue.get("beast_mode"))
            except ValueError:
                queue["beast_mode"] = TITAN_BEAST_MODE_DEFAULT
        participants = queue.setdefault("participants", {})
        expected_keys = set()
        for item in config["participants"]:
            key = duel_participant_key(item["account"], item["identity"])
            expected_keys.add(key)
            default_participant = _new_participant_state()
            default_participant["target_username"] = duel_default_target_for_participant(
                item["account"], item["identity"]
            )
            participant = participants.setdefault(key, default_participant)
            for field, value in default_participant.items():
                participant.setdefault(field, value)
            participant["enabled"] = bool(participant.get("enabled", True))
            try:
                participant["target_username"] = (
                    normalize_duel_target(participant.get("target_username"))
                    or default_participant["target_username"]
                )
            except ValueError:
                participant["target_username"] = default_participant["target_username"]
        for key in list(participants):
            if key not in expected_keys:
                participants.pop(key, None)
    for queue_key in list(queues):
        if queue_key not in DUEL_QUEUES:
            queues.pop(queue_key, None)

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
            queue["target_preparation"] = {}
            for key in list(queue.get("participants", {})):
                previous = queue["participants"].get(key) or {}
                reset_state = _new_participant_state()
                reset_state["enabled"] = bool(previous.get("enabled", True))
                account, _, identity = key.partition("|")
                try:
                    reset_state["target_username"] = (
                        normalize_duel_target(previous.get("target_username"))
                        or duel_default_target_for_participant(account, identity)
                    )
                except ValueError:
                    reset_state["target_username"] = duel_default_target_for_participant(
                        account, identity
                    )
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


def duel_interval_seconds(data):
    try:
        return int(data.get("interval_seconds", DUEL_INTERVAL_SECONDS))
    except (AttributeError, TypeError, ValueError):
        return DUEL_INTERVAL_SECONDS


def duel_target_interval_seconds(data):
    try:
        return int(data.get("target_interval_seconds", DUEL_TARGET_INTERVAL_SECONDS))
    except (AttributeError, TypeError, ValueError):
        return DUEL_TARGET_INTERVAL_SECONDS


def duel_target_switch_enabled(data):
    """Whether known duel targets must switch to and hold the configured identity."""
    try:
        return bool(data.get("target_switch_enabled", True))
    except AttributeError:
        return True


def _validated_duel_interval_seconds(value):
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid duel interval") from exc
    if seconds < DUEL_INTERVAL_MIN_SECONDS or seconds > DUEL_INTERVAL_MAX_SECONDS:
        raise ValueError("invalid duel interval")
    return seconds


def set_duel_intervals(interval_seconds, target_interval_seconds):
    """Persist the queue cadence and per-target cooldown used by all duel plans."""
    interval_seconds = _validated_duel_interval_seconds(interval_seconds)
    target_interval_seconds = _validated_duel_interval_seconds(target_interval_seconds)
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        data["interval_seconds"] = interval_seconds
        data["target_interval_seconds"] = target_interval_seconds
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
                queue["target_preparation"] = {}
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
                for queue in data["queues"].values():
                    queue["in_flight"] = {}
                    queue["preparation"] = {}
                    queue["target_preparation"] = {}
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def set_duel_target_switch(enabled):
    """Choose between identity-switch duels and direct ``.斗法 @用户名`` duels.

    When disabled, known target identities are no longer pre-switched or held;
    every duel is sent as ``.斗法 @用户名`` against whatever identity the target
    account currently controls.  Pending preparations that are not tied to an
    in-flight duel are dropped so schedulers stop waiting on them.
    """
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        data["target_switch_enabled"] = bool(enabled)
        if not enabled:
            note = "已改为直接 .斗法 @用户名"
            for queue_key, container, field in _duel_target_preparation_contexts(data):
                preparation = container.get(field) or {}
                if not preparation:
                    continue
                in_flight = container.get("in_flight") or {}
                if (
                    _lease_active(in_flight, now)
                    and in_flight.get("preparation_run_id") == preparation.get("run_id")
                ):
                    continue
                container[field] = {}
                container["last_result"] = note
                _set_preparation_subject_status(queue_key, container, preparation, "ready", note)
        for queue in data["queues"].values():
            queue["next_at"] = ""
        data["multi"]["next_at"] = ""
        data["updated_at"] = duel_time(now)
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
        if enabled:
            data["enabled"] = True
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
        if enabled:
            data["enabled"] = True
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
        participant = duel_participant_config(queue_key, participant_key)
        state["enabled"] = bool(enabled)
        if target is not None:
            target = target or duel_default_target_for_participant(
                participant["account"], participant["identity"]
            )
            target_identity = duel_identity_for_username(target)
            if target_identity and target_identity["account"] == participant["account"]:
                raise ValueError("same account duel target")
            state["target_username"] = target
        if not state["enabled"] and (queue.get("in_flight") or {}).get("participant_key") == participant_key:
            queue["in_flight"] = {}
            state["status"] = "paused"
        elif state["enabled"] and state.get("status") == "paused":
            state["status"] = "ready"
        target_preparation = queue.get("target_preparation") or {}
        if target_preparation.get("participant_key") == participant_key:
            queue["target_preparation"] = {}
        queue["next_at"] = ""
        data["updated_at"] = duel_time()
        _atomic_write_json(DUEL_STATE_FILE, data)
        return data


def set_titan_beast_mode(mode):
    mode = normalize_titan_beast_mode(mode)
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue = data["queues"][DUEL_ROTATION_QUEUE_KEY]
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


def _is_rolling_target_cooldown(result):
    return (
        str((result or {}).get("cooldown_kind") or "") == "rolling_target"
        or str((result or {}).get("outcome") or "") == "目标24小时冷却"
    )


def _rolling_target_ready_at(reservation, now=None):
    """Return 24 hours after the oldest settled duel still in the window."""
    now = now or duel_now()
    fallback = now + timedelta(seconds=DUEL_ROLLING_TARGET_COOLDOWN_SECONDS)
    if not os.path.exists(DUEL_DB_FILE):
        return fallback
    try:
        account = str((reservation or {}).get("account") or "").strip()
        target = normalize_duel_target((reservation or {}).get("target_username"))
    except ValueError:
        return fallback
    if not account or not target:
        return fallback
    cutoff = now - timedelta(seconds=DUEL_ROLLING_TARGET_COOLDOWN_SECONDS)
    conn = sqlite3.connect(DUEL_DB_FILE, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute(
            """
            SELECT MIN(event_time)
            FROM duel_events
            WHERE account=?
              AND target_username=? COLLATE NOCASE
              AND status='settled'
              AND event_time>?
              AND event_time<=?
            """,
            (account, target, duel_time(cutoff), duel_time(now)),
        ).fetchone()
    except sqlite3.Error:
        return fallback
    finally:
        conn.close()
    first_at = parse_duel_time(row[0] if row else "")
    return (
        first_at + timedelta(seconds=DUEL_ROLLING_TARGET_COOLDOWN_SECONDS)
        if first_at else fallback
    )


def _multi_target_by_id(multi, target_id):
    target_id = str(target_id or "")
    for target in multi.get("targets") or []:
        if str(target.get("id") or "") == target_id:
            return target
    return None


def _duel_target_activation(target_username):
    # Every known target identity must be aligned and held before the duel, not
    # only avatars.  ``.斗法 @main_username`` resolves to whatever identity that
    # Telegram account currently controls, so a fishing/other temporary switch
    # can otherwise redirect a planned main-soul duel to an avatar.
    return duel_identity_for_username(target_username)


def _duel_target_preparation_contexts(data):
    multi = data.get("multi") or {}
    yield "multi", multi, "preparation"
    for queue_key in DUEL_QUEUES:
        queue = (data.get("queues") or {}).get(queue_key) or {}
        yield queue_key, queue, "target_preparation"


def _duel_target_preparation_context(data, claim):
    run_id = str((claim or {}).get("run_id") or "")
    preferred = str((claim or {}).get("queue_key") or "")
    contexts = list(_duel_target_preparation_contexts(data))
    if preferred:
        contexts.sort(key=lambda item: 0 if item[0] == preferred else 1)
    for queue_key, container, field in contexts:
        preparation = container.get(field) or {}
        if run_id and preparation.get("run_id") == run_id:
            return queue_key, container, field, preparation
    return None, None, None, None


def _set_preparation_subject_status(queue_key, container, preparation, status, detail=""):
    if queue_key == "multi":
        subject = _multi_target_by_id(container, preparation.get("target_id"))
    else:
        subject = (container.get("participants") or {}).get(
            preparation.get("participant_key")
        )
    if not isinstance(subject, dict):
        return
    subject["status"] = status
    if detail:
        subject["last_result"] = detail


def claim_duel_target_preparation(account):
    account = str(account or "").strip().lower()
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        if not data.get("enabled") or not duel_target_switch_enabled(data):
            return None
        for queue_key, container, field in _duel_target_preparation_contexts(data):
            preparation = container.get(field) or {}
            if (
                not container.get("enabled")
                or str(preparation.get("owner") or "") != account
                or str(preparation.get("status") or "") != "pending"
                or not _lease_active(preparation, now)
            ):
                continue
            preparation["queue_key"] = queue_key
            preparation["status"] = "claimed"
            preparation["claimed_at"] = duel_time(now)
            preparation["lease_until"] = duel_time(
                now + timedelta(seconds=DUEL_LEASE_SECONDS)
            )
            container[field] = preparation
            data["updated_at"] = duel_time(now)
            _atomic_write_json(DUEL_STATE_FILE, data)
            return dict(preparation)
        return None


def finish_duel_target_preparation(claim, success, detail="", reply_to_msg_id=None):
    if not claim:
        return False
    now = duel_now()
    try:
        reply_to_msg_id = int(reply_to_msg_id or 0)
    except (TypeError, ValueError):
        reply_to_msg_id = 0
    if success and reply_to_msg_id <= 0:
        success = False
        detail = detail or "目标身份切换消息缺失"
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue_key, container, field, preparation = _duel_target_preparation_context(data, claim)
        if preparation is None:
            return False
        if success:
            preparation["status"] = "ready"
            preparation["ready_at"] = duel_time(now)
            preparation["reply_to_msg_id"] = reply_to_msg_id
            preparation["lease_until"] = duel_time(
                now + timedelta(seconds=DUEL_LEASE_SECONDS)
            )
            container["last_result"] = detail or (
                f"@{preparation.get('target_username')} · {preparation.get('target_identity')} "
                "已发送切换锚点"
            )
            _set_preparation_subject_status(
                queue_key, container, preparation, "prepared", container["last_result"]
            )
        else:
            container[field] = {}
            container["next_at"] = duel_time(
                now + timedelta(seconds=DUEL_BUSY_RETRY_SECONDS)
            )
            container["last_result"] = detail or "目标身份激活失败"
            _set_preparation_subject_status(
                queue_key, container, preparation, "ready", container["last_result"]
            )
        data["updated_at"] = duel_time(now)
        _atomic_write_json(DUEL_STATE_FILE, data)
        return True


def duel_target_preparation_held(claim):
    if not claim:
        return False
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        _queue_key, _container, _field, preparation = _duel_target_preparation_context(data, claim)
        if preparation is None:
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
        queue_key, container, field, preparation = _duel_target_preparation_context(data, claim)
        if preparation is None:
            return False
        in_flight = container.get("in_flight") or {}
        if in_flight and in_flight.get("preparation_run_id") == claim.get("run_id"):
            return False
        container[field] = {}
        if detail:
            container["last_result"] = detail
        _set_preparation_subject_status(
            queue_key, container, preparation, "ready", detail
        )
        data["updated_at"] = duel_time(now)
        _atomic_write_json(DUEL_STATE_FILE, data)
        return True


def _reserve_multi_duel_locked(data, account, now):
    multi = data["multi"]
    interval_seconds = duel_interval_seconds(data)
    target_interval_seconds = duel_target_interval_seconds(data)
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
            multi["last_result"] = (
                f"等待 @{target_username} 满足 {duel_duration_label(target_interval_seconds)}间隔"
            )
            return None, True
        return None, dirty

    target_username = normalize_duel_target(selected.get("username"))
    require_target_switch = duel_target_switch_enabled(data)
    activation = _duel_target_activation(target_username) if require_target_switch else None
    preparation = multi.get("preparation") or {}
    if preparation and (not require_target_switch or not _lease_active(preparation, now)):
        multi["preparation"] = {}
        preparation = {}
        dirty = True
    if activation:
        matching_preparation = (
            preparation.get("target_id") == selected.get("id")
            and preparation.get("target_username", "").lower() == target_username.lower()
            and preparation.get("target_identity") == activation["identity"]
        )
        if matching_preparation and str(preparation.get("status") or "") == "ready":
            try:
                reply_to_msg_id = int(preparation.get("reply_to_msg_id") or 0)
            except (TypeError, ValueError):
                reply_to_msg_id = 0
            if reply_to_msg_id <= 0:
                multi["preparation"] = {}
                preparation = {}
                matching_preparation = False
                dirty = True
        if not matching_preparation:
            run_id = uuid.uuid4().hex
            multi["preparation"] = {
                "queue_key": "multi",
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
                f"等待 {activation['account_name']} · {activation['identity']} "
                "发送新鲜的 .切换 消息"
            )
            return None, True
        if str(preparation.get("status") or "") != "ready":
            multi["last_result"] = (
                f"正在生成 {activation['account_name']} · {activation['identity']} 的切换锚点"
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
    multi["next_at"] = duel_time(now + timedelta(seconds=interval_seconds))
    multi["last_attempt_at"] = duel_time(now)
    data.setdefault("target_next_at", {})[target_username.lower()] = duel_time(
        now + timedelta(seconds=target_interval_seconds)
    )
    preparation_run_id = ""
    reply_to_msg_id = None
    if activation:
        preparation = multi.get("preparation") or {}
        preparation["status"] = "holding"
        preparation["lease_until"] = duel_time(
            now + timedelta(seconds=DUEL_LEASE_SECONDS)
        )
        preparation_run_id = str(preparation.get("run_id") or "")
        reply_to_msg_id = int(preparation.get("reply_to_msg_id") or 0) or None
        if activation.get("identity") == "主魂":
            # Main souls continue to use the explicit username command.  The
            # fresh switch message is still required as proof/alignment and its
            # owner keeps the identity under the atomic preparation hold.
            reply_to_msg_id = None
    multi["in_flight"] = {
        "run_id": run_id,
        "owner": account,
        "target_id": selected["id"],
        "target_username": target_username,
        "preparation_run_id": preparation_run_id,
        "reply_to_msg_id": reply_to_msg_id,
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
        "target_account": (activation or {}).get("account", ""),
        "target_identity": (activation or {}).get("identity", ""),
        "preparation_run_id": preparation_run_id,
        "reply_to_msg_id": reply_to_msg_id,
        "command": (
            ".斗法"
            if activation and activation.get("identity") != "主魂"
            else f".斗法 @{target_username}"
        ),
        "reserved_at": duel_time(now),
    }, True


def _next_day_time(now=None):
    now = now or duel_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return duel_time(tomorrow)


def _configured_titan_beast_mode():
    try:
        data = _read_duel_state_unlocked()
        queues = data.get("queues") or {}
        queue = queues.get(DUEL_ROTATION_QUEUE_KEY) or queues.get("titan") or {}
        return normalize_titan_beast_mode(
            queue.get("beast_mode")
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


def _next_rotation_target(data, queue, now=None):
    """Return the target the unified rotation would currently select."""
    now = now or duel_now()
    participants = list(DUEL_QUEUES[DUEL_ROTATION_QUEUE_KEY]["participants"])
    if not participants:
        return ""
    cursor = int(queue.get("cursor") or 0) % len(participants)
    xiaohao_send = xiaohao_duel_send_status()
    for offset in range(len(participants)):
        item = participants[(cursor + offset) % len(participants)]
        key = duel_participant_key(item["account"], item["identity"])
        participant = (queue.get("participants") or {}).get(key) or {}
        if not bool(participant.get("enabled", True)):
            continue
        if int(participant.get("remaining", DUEL_DAILY_LIMIT) or 0) <= 0:
            continue
        if xiaohao_send.get("restricted") and item["account"] == "xiaohao":
            continue
        target_username = (
            normalize_duel_target(participant.get("target_username"))
            or duel_default_target_for_participant(item["account"], item["identity"])
        )
        target_identity = duel_identity_for_username(target_username)
        if target_identity and target_identity["account"] == item["account"]:
            continue
        if _target_blocked_until(data, target_username, now):
            continue
        return target_username
    return ""


def claim_titan_preparation(account):
    if str(account or "") != "xiaohao":
        return None
    now = duel_now()
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue = data["queues"][DUEL_ROTATION_QUEUE_KEY]
        if titan_target_status(queue.get("beast_mode"))["ready"]:
            return None
        if (
            not data.get("enabled")
            or not duel_target_switch_enabled(data)
            or not queue.get("enabled")
            or not _queue_due(queue, now, 30)
        ):
            return None
        if _next_rotation_target(data, queue, now).lower() != "titancreeper":
            return None
        if _lease_active(queue.get("in_flight"), now) or _lease_active(queue.get("preparation"), now):
            return None
        run_id = uuid.uuid4().hex
        queue["preparation"] = {
            "kind": "titan",
            "run_id": run_id,
            "owner": "xiaohao",
            "started_at": duel_time(now),
            "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
        }
        queue["last_result"] = "正在准备 TitanCreeper"
        _atomic_write_json(DUEL_STATE_FILE, data)
        return {"queue_key": DUEL_ROTATION_QUEUE_KEY, "run_id": run_id}


def finish_titan_preparation(claim, success, detail=""):
    if not claim:
        return
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        queue = data["queues"][DUEL_ROTATION_QUEUE_KEY]
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
        interval_seconds = duel_interval_seconds(data)
        target_interval_seconds = duel_target_interval_seconds(data)
        require_target_switch = duel_target_switch_enabled(data)
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
            target_preparation = queue.get("target_preparation") or {}
            if target_preparation and not _lease_active(target_preparation, now):
                queue["target_preparation"] = {}
                target_preparation = {}
                dirty = True
            participants = list(config["participants"])
            xiaohao_send = xiaohao_duel_send_status()
            cursor = int(queue.get("cursor") or 0) % len(participants)
            selected = None
            selected_index = None
            selected_target = ""
            selected_activation = None
            blocked_targets = []
            invalid_targets = []
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
                target_username = (
                    normalize_duel_target(pstate.get("target_username"))
                    or duel_default_target_for_participant(item["account"], item["identity"])
                )
                target_identity = duel_identity_for_username(target_username)
                activation = (
                    _duel_target_activation(target_username)
                    if require_target_switch
                    else None
                )
                if target_identity and target_identity["account"] == item["account"]:
                    detail = f"{item['identity']} 不能挑战同账号身份 @{target_username}"
                    invalid_targets.append(detail)
                    if pstate.get("status") != "invalid_target" or pstate.get("last_result") != detail:
                        pstate["status"] = "invalid_target"
                        pstate["last_result"] = detail
                        dirty = True
                    continue
                blocked_until = _target_blocked_until(data, target_username, now)
                if blocked_until:
                    blocked_targets.append((blocked_until, target_username))
                    continue
                selected = item
                selected_index = index
                selected_target = target_username
                selected_activation = activation
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
                if invalid_targets:
                    queue["next_at"] = ""
                    queue["last_result"] = invalid_targets[0]
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
            key = duel_participant_key(selected["account"], selected["identity"])
            pstate = queue["participants"][key]
            preparation_run_id = ""
            reply_to_msg_id = None

            # TitanCreeper also needs its beast preparation.  Do this before
            # creating the main-soul identity hold; otherwise the target
            # scheduler would sit inside that hold and could not run the beast
            # preparation until the lease expired.  Direct-username mode skips
            # every target-side preparation, including this one.
            if require_target_switch and target_username.lower() == "titancreeper":
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

            if selected_activation:
                target_preparation = queue.get("target_preparation") or {}
                matching_preparation = (
                    target_preparation.get("participant_key") == key
                    and str(target_preparation.get("target_username") or "").lower()
                    == target_username.lower()
                    and target_preparation.get("target_identity") == selected_activation["identity"]
                )
                if matching_preparation and str(target_preparation.get("status") or "") == "ready":
                    try:
                        reply_to_msg_id = int(target_preparation.get("reply_to_msg_id") or 0)
                    except (TypeError, ValueError):
                        reply_to_msg_id = 0
                    if reply_to_msg_id <= 0:
                        queue["target_preparation"] = {}
                        target_preparation = {}
                        matching_preparation = False
                        dirty = True
                if not matching_preparation:
                    preparation_run_id = uuid.uuid4().hex
                    queue["target_preparation"] = {
                        "queue_key": queue_key,
                        "run_id": preparation_run_id,
                        "owner": selected_activation["account"],
                        "status": "pending",
                        "participant_key": key,
                        "target_username": target_username,
                        "target_identity": selected_activation["identity"],
                        "started_at": duel_time(now),
                        "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
                    }
                    pstate["status"] = "preparing"
                    queue["last_result"] = (
                        f"等待 {selected_activation['account_name']} · "
                        f"{selected_activation['identity']} 发送新鲜的 .切换 消息"
                    )
                    data["updated_at"] = duel_time(now)
                    _atomic_write_json(DUEL_STATE_FILE, data)
                    return None
                if str(target_preparation.get("status") or "") != "ready":
                    waiting_result = (
                        f"正在生成 {selected_activation['account_name']} · "
                        f"{selected_activation['identity']} 的切换锚点"
                    )
                    if queue.get("last_result") != waiting_result:
                        queue["last_result"] = waiting_result
                        dirty = True
                    if dirty:
                        data["updated_at"] = duel_time(now)
                        _atomic_write_json(DUEL_STATE_FILE, data)
                    return None
                preparation_run_id = str(target_preparation.get("run_id") or "")
                reply_to_msg_id = int(target_preparation.get("reply_to_msg_id") or 0) or None
                if selected_activation.get("identity") == "主魂":
                    reply_to_msg_id = None
            elif target_preparation:
                queue["target_preparation"] = {}
                dirty = True

            run_id = uuid.uuid4().hex
            queue["cursor"] = (selected_index + 1) % len(participants)
            queue["next_at"] = duel_time(now + timedelta(seconds=interval_seconds))
            queue["last_attempt_at"] = duel_time(now)
            data.setdefault("target_next_at", {})[target_username.lower()] = duel_time(
                now + timedelta(seconds=target_interval_seconds)
            )
            queue["in_flight"] = {
                "run_id": run_id,
                "participant_key": key,
                "owner": account,
                "started_at": duel_time(now),
                "lease_until": duel_time(now + timedelta(seconds=DUEL_LEASE_SECONDS)),
                "target_username": target_username,
                "preparation_run_id": preparation_run_id,
                "reply_to_msg_id": reply_to_msg_id,
            }
            if selected_activation:
                target_preparation = queue.get("target_preparation") or {}
                target_preparation["status"] = "holding"
                target_preparation["lease_until"] = duel_time(
                    now + timedelta(seconds=DUEL_LEASE_SECONDS)
                )
                queue["target_preparation"] = target_preparation
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
                "target_account": (selected_activation or {}).get("account", ""),
                "target_identity": (selected_activation or {}).get("identity", ""),
                "preparation_run_id": preparation_run_id,
                "reply_to_msg_id": reply_to_msg_id,
                "command": (
                    ".斗法"
                    if selected_activation and selected_activation.get("identity") != "主魂"
                    else f".斗法 @{target_username}"
                ),
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
        interval_seconds = duel_interval_seconds(data)
        target_interval_seconds = duel_target_interval_seconds(data)
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
                    now + timedelta(seconds=interval_seconds)
                )

            target_key = normalize_duel_target(
                reservation.get("target_username")
            ).lower()
            target_next_at = data.setdefault("target_next_at", {})
            if status == "cooldown":
                if _is_rolling_target_cooldown(result):
                    target_ready = max(
                        now + timedelta(seconds=target_interval_seconds),
                        _rolling_target_ready_at(reservation, now),
                    )
                else:
                    target_ready = now + timedelta(
                        seconds=max(
                            target_interval_seconds,
                            int(result.get("wait_seconds") or 0),
                        )
                    )
            else:
                target_ready = now + timedelta(seconds=target_interval_seconds)
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
        if status == "identity_paused":
            pstate["status"] = "identity_paused"
        else:
            pstate["status"] = "exhausted" if int(pstate.get("remaining", 0) or 0) <= 0 else "ready"
        pstate["last_result"] = outcome
        queue["last_result"] = f"{reservation['identity']} · {outcome}"
        queue["in_flight"] = {}
        target_preparation = queue.get("target_preparation") or {}
        if target_preparation.get("run_id") == reservation.get("preparation_run_id"):
            queue["target_preparation"] = {}
        rolling_target_ready = None
        if status == "identity_paused":
            retry_seconds = DUEL_IDENTITY_PAUSE_RETRY_SECONDS
        else:
            retry_seconds = DUEL_BUSY_RETRY_SECONDS if status == "busy" else interval_seconds
        if status == "cooldown" and _is_rolling_target_cooldown(result):
            rolling_target_ready = _rolling_target_ready_at(reservation, now)
        elif status == "cooldown":
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
            seconds=max(target_interval_seconds, retry_seconds)
        )
        if rolling_target_ready is not None:
            target_completed_at = max(target_completed_at, rolling_target_ready)
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
        "天机繁忙", "因果纠缠", "侥幸逃脱", "已交锋过多",
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
    if "已交锋过多" in clean and "24小时" in clean:
        result.update(
            status="cooldown",
            outcome="目标24小时冷却",
            cooldown_kind="rolling_target",
            wait_seconds=max(
                DUEL_ROLLING_TARGET_COOLDOWN_SECONDS,
                duel_wait_seconds(clean),
            ),
        )
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


def repair_duel_rolling_cooldowns(now=None):
    """Correct legacy now-plus-24h blocks from the authoritative duel history."""
    now = now or duel_now()
    if not os.path.exists(DUEL_DB_FILE):
        return {"updated": False, "targets": {}}
    conn = sqlite3.connect(DUEL_DB_FILE, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        ensure_duel_events_schema(conn)
        rows = conn.execute(
            """
            SELECT event.account,event.target_username,event.event_time
            FROM duel_events AS event
            WHERE event.outcome='目标24小时冷却'
              AND event.event_time>?
              AND NOT EXISTS (
                  SELECT 1
                  FROM duel_events AS newer
                  WHERE newer.account=event.account
                    AND newer.target_username=event.target_username COLLATE NOCASE
                    AND (
                        newer.event_time>event.event_time
                        OR (newer.event_time=event.event_time AND newer.id>event.id)
                    )
              )
            """,
            (duel_time(now - timedelta(days=2)),),
        ).fetchall()
    except sqlite3.Error:
        return {"updated": False, "targets": {}}
    finally:
        conn.close()

    pair_ready = {}
    target_ready = {}
    for row in rows:
        cooldown_at = parse_duel_time(row["event_time"])
        if cooldown_at is None:
            continue
        account = str(row["account"] or "").strip()
        try:
            target = normalize_duel_target(row["target_username"])
        except ValueError:
            continue
        ready_at = _rolling_target_ready_at(
            {"account": account, "target_username": target},
            cooldown_at,
        )
        pair_ready[(account, target.lower())] = ready_at
        current = target_ready.get(target.lower())
        target_ready[target.lower()] = max(current, ready_at) if current else ready_at
    if not target_ready:
        return {"updated": False, "targets": {}}

    changed = False
    with duel_state_lock():
        data = _ensure_duel_state_shape(_read_duel_state_unlocked())
        target_next_at = data.setdefault("target_next_at", {})
        for target_key, ready_at in target_ready.items():
            ready_text = duel_time(ready_at)
            if target_next_at.get(target_key) != ready_text:
                target_next_at[target_key] = ready_text
                changed = True

        multi = data.get("multi") or {}
        multi_waits = [
            pair_ready[(str(multi.get("initiator_account") or ""), target_key)]
            for target_key in (
                normalize_duel_target(target.get("username")).lower()
                for target in multi.get("targets") or []
                if int(target.get("remaining") or 0) > 0
            )
            if (str(multi.get("initiator_account") or ""), target_key) in pair_ready
        ]
        multi_next = parse_duel_time(multi.get("next_at"))
        if (
            multi_waits
            and str(multi.get("last_result") or "").startswith("等待 @")
            and multi_next
            and multi_next > min(multi_waits)
        ):
            ready_at = min(multi_waits)
            multi["next_at"] = "" if ready_at <= now else duel_time(ready_at)
            changed = True

        for queue_key, queue in (data.get("queues") or {}).items():
            if "24小时冷却" not in str(queue.get("last_result") or ""):
                continue
            queue_waits = []
            for participant_key, participant in (queue.get("participants") or {}).items():
                config = duel_participant_config(queue_key, participant_key)
                if not config:
                    continue
                try:
                    target_key = (
                        normalize_duel_target(participant.get("target_username"))
                        or duel_default_target_for_participant(
                            config["account"], config["identity"]
                        )
                    ).lower()
                except ValueError:
                    continue
                ready_at = pair_ready.get((config["account"], target_key))
                if ready_at:
                    queue_waits.append(ready_at)
            queue_next = parse_duel_time(queue.get("next_at"))
            if queue_waits and queue_next and queue_next > min(queue_waits):
                ready_at = min(queue_waits)
                queue["next_at"] = "" if ready_at <= now else duel_time(ready_at)
                changed = True

        if changed:
            data["updated_at"] = duel_time(now)
            _atomic_write_json(DUEL_STATE_FILE, data)
    return {
        "updated": changed,
        "targets": {key: duel_time(value) for key, value in target_ready.items()},
    }


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
    target_switch_enabled = duel_target_switch_enabled(data)
    query_date = str(date or "").strip() or duel_date()
    titan_mode = data["queues"][DUEL_ROTATION_QUEUE_KEY].get(
        "beast_mode", TITAN_BEAST_MODE_DEFAULT
    )
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
            configured_target = normalize_duel_target(state.get("target_username"))
            default_target = duel_default_target_for_participant(
                participant["account"], participant["identity"]
            )
            effective_target = configured_target or default_target
            target_identity = duel_identity_for_username(effective_target)
            activation_required = bool(
                target_switch_enabled
                and target_identity
                and target_identity.get("identity") != "主魂"
            )
            state["enabled"] = bool(state.get("enabled", True))
            state["target_username"] = effective_target
            state["target_is_default"] = effective_target.lower() == default_target.lower()
            titan_required = titan_required or (
                target_switch_enabled
                and state["enabled"] and int(state.get("remaining", DUEL_DAILY_LIMIT) or 0) > 0
                and effective_target.lower() == "titancreeper"
            )
            participant_rows.append({
                **participant,
                "key": key,
                "account_name": DUEL_ACCOUNT_LABELS.get(participant["account"], participant["account"]),
                "is_next": index == next_index,
                "known_target_identity": bool(target_identity),
                "activation_required": activation_required,
                "duel_method": "reply_switch" if activation_required else "username",
                "target_account": (target_identity or {}).get("account", ""),
                "target_account_name": (target_identity or {}).get("account_name", ""),
                "target_identity": (target_identity or {}).get("identity", ""),
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
            "target_status": titan_status if titan_required else {"ready": True},
            "requires_titan_preparation": titan_required,
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
        multi_activation_required = bool(
            target_switch_enabled
            and target_identity
            and target_identity.get("identity") != "主魂"
        )
        multi_targets.append({
            **target,
            "is_next": index == next_multi_index,
            "known_identity": bool(target_identity),
            "activation_required": multi_activation_required,
            "duel_method": "reply_switch" if multi_activation_required else "username",
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
        "target_switch_enabled": target_switch_enabled,
        "date": query_date,
        "interval_seconds": duel_interval_seconds(data),
        "target_interval_seconds": duel_target_interval_seconds(data),
        "interval_min_seconds": DUEL_INTERVAL_MIN_SECONDS,
        "interval_max_seconds": DUEL_INTERVAL_MAX_SECONDS,
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
                return False, f"{identity} 身份已暂停，跳过斗法目标切换", None
        username = str(claim.get("target_username") or "").strip()
        known_identities = {"主魂", *getattr(self, "avatars", [])}
        if not identity or identity not in known_identities:
            return False, "目标不是可切换身份", None
        prepare = getattr(self, "prepare_identity_for_time_critical_command", None)
        if not callable(prepare):
            return False, "账号缺少身份预切换能力", None
        prepared = await prepare(
            identity,
            command=".斗法",
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
            (
                f"@{username} · 已切换并锁定 {identity}，等待发起者按用户名斗法"
                if identity == "主魂"
                else f"@{username} · 已发送 .切换 {identity}，等待发起者引用该消息斗法"
            ),
            reply_to_msg_id,
        )

    async def _run_duel_target_preparation_if_due(self):
        claim = claim_duel_target_preparation(getattr(self, "account_key", ""))
        if not claim:
            return False
        logger = self.duel_logger()

        async def prepare_and_hold():
            success, detail, reply_to_msg_id = await self.prepare_duel_target_identity(claim)
            finish_duel_target_preparation(
                claim,
                success,
                detail,
                reply_to_msg_id=reply_to_msg_id,
            )
            logger.info(
                "Duel target preparation: target=@%s identity=%s success=%s reply_to=%s detail=%s",
                claim.get("target_username"), claim.get("target_identity"), success,
                reply_to_msg_id, detail,
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
                    f"@{claim.get('target_username')} 分身切换消息等待超时，稍后重试"
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
                    rest_action = getattr(self, "rest_beast_for_abyss", None)
                    if not callable(rest_action):
                        return False, "小号缺少万兽谷灵兽休息能力"
                    rest_status, rest_text = await rest_action("六翼")
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
            identity = str(reservation.get("identity") or "主魂").strip() or "主魂"
            resolver = getattr(self, "resolve_avatar_identity", None)
            if callable(resolver):
                identity = str(resolver(identity) or identity).strip() or "主魂"
            pause_seconds = getattr(self, "identity_pause_seconds", None)
            if callable(pause_seconds):
                try:
                    remaining = int(pause_seconds(identity) or 0)
                except (TypeError, ValueError):
                    remaining = 0
                if remaining > 0:
                    result = {
                        "status": "identity_paused",
                        "outcome": "身份暂停",
                        "text": f"{identity} 身份暂停，跳过斗法",
                    }
                    logger.info("Duel skipped for paused identity [%s] (%ss remaining)", identity, remaining)
                    return result
            logger.info(
                "Duel turn [%s]: %s/%s -> @%s reply_to=%s",
                reservation["queue_key"], reservation["account"], reservation["identity"],
                reservation["target_username"], reservation.get("reply_to_msg_id"),
            )
            response_msg = await self.send_and_wait_feedback_identity(
                reservation["identity"],
                reservation["command"],
                reply_to=reservation.get("reply_to_msg_id"),
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
                if result.get("status") != "identity_paused":
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
        repaired = repair_duel_rolling_cooldowns()
        if repaired.get("updated"):
            logger.info(
                "Duel rolling cooldowns repaired from first settled attempts: %s",
                repaired.get("targets"),
            )
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
