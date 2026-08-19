#!/usr/bin/env python3
"""Automated Lingxi fishing through the authenticated Telegram Mini App."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from automation_settings import (
    ACCOUNT_IDENTITIES,
    ACCOUNT_NAMES,
    DEFAULT_MINIAPP_FISHING_ROD,
    MINIAPP_FISHING_RODS,
    MINIAPP_FISHING_SUPPORTED_ACCOUNTS,
    automation_participant_key,
    canonical_automation_identity,
    miniapp_fishing_settings,
)
from fishing_features import parse_trade_listing_response, parse_trade_purchase_response
from miniapp_beast import (
    MiniAppBeastError,
    MiniAppCircuitOpenError,
    miniapp_circuit_preflight,
    miniapp_circuit_wait_seconds,
)
from miniapp_dwelling import identity_state
from state_io import (
    StateFileLockTimeout,
    load_json_state,
    save_json_state,
    update_json_state,
)


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_RETRY_SECONDS = 60
DEFAULT_DISABLED_SECONDS = 30
DEFAULT_RESULT_ATTEMPTS = 18
BAIT_PURCHASE_QUANTITY = 10
FISHING_ROD_LISTING_MATERIAL = "凝血草"
FISHING_ROD_ITEMS = frozenset(key for key, _ in MINIAPP_FISHING_RODS if key != "auto")
FISHING_ROD_SCAN_SECONDS = 300
FISHING_TRANSFER_RETRY_SECONDS = 60
FISHING_TRANSFER_FAILURE_RETRY_SECONDS = 3600
FISHING_SHOP_RETRY_SECONDS = 3600
FISHING_MATERIAL_NOTICE_RETRY_SECONDS = 3600
FISHING_GLOBAL_LOCK_TIMEOUT_SECONDS = 8.0
FISHING_GLOBAL_LOCK_RETRY_SECONDS = (5 * 60, 15 * 60, 60 * 60, 6 * 60 * 60)
FISHING_TRANSFER_FAILURE_STATUSES = {
    "listing_failed",
    "listing_unknown",
    "purchase_failed",
    "purchase_unknown",
}
MINIAPP_FISHING_GLOBAL_FILE = Path(__file__).resolve().parent / "miniapp_fishing_global.json"


class MiniAppFishingGlobalStateBusy(RuntimeError):
    """The shared fishing ledger is temporarily owned by another process."""

    code = "miniapp_fishing_global_lock_timeout"

    def __init__(self) -> None:
        super().__init__(self.code)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def _today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fishing_start_wait(value: Any, now: datetime | None = None) -> tuple[int, str]:
    """Return seconds and timestamp until today's configured fishing start."""
    text = str(value or "").strip()
    if not text:
        return 0, ""
    current = now or datetime.now()
    try:
        target = datetime.strptime(
            f"{current.strftime('%Y-%m-%d')} {text}",
            "%Y-%m-%d %H:%M",
        )
    except ValueError:
        return 0, ""
    if target <= current:
        return 0, target.strftime(TIME_FORMAT)
    return max(1, math.ceil((target - current).total_seconds())), target.strftime(TIME_FORMAT)


def fishing_participant_parts(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if "|" not in text:
        return "", ""
    account, identity = (part.strip() for part in text.split("|", 1))
    if account not in MINIAPP_FISHING_SUPPORTED_ACCOUNTS:
        return "", ""
    identity = canonical_automation_identity(account, identity)
    if identity not in ACCOUNT_IDENTITIES.get(account, ()):
        return "", ""
    return account, identity


def fishing_participant_label(value: Any) -> str:
    account, identity = fishing_participant_parts(value)
    if not account:
        return ""
    return f"{ACCOUNT_NAMES.get(account, account)}｜{identity}"


def configured_fishing_rod(settings: dict[str, Any]) -> str:
    value = str(settings.get("rod") or DEFAULT_MINIAPP_FISHING_ROD).strip()
    return value if value == "auto" or value in FISHING_ROD_ITEMS else DEFAULT_MINIAPP_FISHING_ROD


def fishing_rod_matches(settings: dict[str, Any], rod_name: Any) -> bool:
    actual = str(rod_name or "").strip()
    configured = configured_fishing_rod(settings)
    return actual in FISHING_ROD_ITEMS and (configured == "auto" or actual == configured)


def fishing_rod_listing_command(rod_name: Any) -> str:
    name = str(rod_name or "").strip()
    if name not in FISHING_ROD_ITEMS:
        raise ValueError("invalid Mini App fishing rod")
    return f".上架 {FISHING_ROD_LISTING_MATERIAL} 换 {name}1"


def fishing_rod_name_in_text(value: Any) -> str:
    text = str(value or "")
    return next((name for name in FISHING_ROD_ITEMS if name in text), "")


def fishing_scan_keys(settings: dict[str, Any]) -> list[str]:
    """Identities the automation is allowed to inspect or use for the shared rod."""
    keys = []
    for item in settings.get("participants") or []:
        account, identity = fishing_participant_parts(item)
        if account and identity:
            keys.append(automation_participant_key(account, identity))
    owner = str(settings.get("rod_owner") or "auto").strip()
    owner_account, owner_identity = fishing_participant_parts(owner)
    if owner != "auto" and owner_account and owner_identity:
        # A transfer interrupted mid-flight can leave the configured owner's rod
        # on another identity of the same Telegram account.  Keep that account
        # searchable for recovery without re-enabling unrelated accounts.
        keys.extend(
            automation_participant_key(owner_account, identity)
            for identity in ACCOUNT_IDENTITIES.get(owner_account, ())
        )
    return list(dict.fromkeys(keys))


def _global_default_state() -> dict[str, Any]:
    return {
        "version": 3,
        "date": _today_text(),
        "participants": [],
        "current_key": "",
        "rod_holder": "",
        "rod_name": "",
        "configured_rod": "",
        "rod_holder_source": "",
        "rod_holder_verified_at": "",
        "scans": {},
        "completed_today": {},
        "round_records": {},
        "summary_emitted_ids": {},
        "transfer": {},
        "last_transfer": {},
        "last_round": {},
        "force_retry": {},
        "last_force_retry": {},
        "force_retry_request_id": "",
        "status": "scanning",
        "detail": "等待扫描鱼竿",
        "updated_at": _now_text(),
    }


def _canonical_fishing_key(value: Any) -> str:
    account, identity = fishing_participant_parts(value)
    if not account:
        return str(value or "").strip()
    return automation_participant_key(account, identity)


def _migrate_global_participant_keys(data: dict[str, Any]) -> None:
    """Migrate stable-account fishing state without reopening completed rounds."""
    for field in ("participants",):
        values = data.get(field)
        if isinstance(values, list):
            data[field] = list(dict.fromkeys(_canonical_fishing_key(item) for item in values))

    for field in ("current_key", "rod_holder"):
        if data.get(field):
            data[field] = _canonical_fishing_key(data[field])

    for field in ("scans", "completed_today", "round_records", "summary_emitted_ids"):
        values = data.get(field)
        if not isinstance(values, dict):
            continue
        migrated: dict[str, Any] = {}
        for raw_key, value in values.items():
            key = _canonical_fishing_key(raw_key)
            if key not in migrated or str(raw_key) == key:
                migrated[key] = value
        data[field] = migrated

    for field in ("transfer", "last_transfer"):
        value = data.get(field)
        if not isinstance(value, dict):
            continue
        for endpoint in ("from", "to"):
            if value.get(endpoint):
                value[endpoint] = _canonical_fishing_key(value[endpoint])

    for field in ("force_retry", "last_force_retry"):
        value = data.get(field)
        if not isinstance(value, dict):
            continue
        for list_field in ("participants", "pending", "attempted"):
            items = value.get(list_field)
            if isinstance(items, list):
                value[list_field] = list(
                    dict.fromkeys(_canonical_fishing_key(item) for item in items)
                )
        for map_field in ("completed_before", "confirmed_daily_done"):
            items = value.get(map_field)
            if isinstance(items, dict):
                value[map_field] = {
                    _canonical_fishing_key(key): item for key, item in items.items()
                }


def _normalize_global_state(data: Any) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    defaults = _global_default_state()
    for key, value in defaults.items():
        data.setdefault(key, value)
    if str(data.get("date") or "") != _today_text():
        data["date"] = _today_text()
        data["completed_today"] = {}
        data["round_records"] = {}
        data["summary_emitted_ids"] = {}
        data["last_round"] = {}
        if not _mapping(data.get("transfer")):
            data["current_key"] = ""
    for key in (
        "scans",
        "completed_today",
        "round_records",
        "summary_emitted_ids",
        "transfer",
        "last_transfer",
        "last_round",
        "force_retry",
        "last_force_retry",
    ):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    if not isinstance(data.get("participants"), list):
        data["participants"] = []
    _migrate_global_participant_keys(data)
    return data


def _load_global_state() -> dict[str, Any]:
    """Read the last atomically published ledger without taking the writer lock."""

    try:
        with MINIAPP_FISHING_GLOBAL_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        # Normal writes are atomic, so this path is only expected for a legacy
        # truncated file. Recover its last valid backup under the shared lock.
        data = load_json_state(
            str(MINIAPP_FISHING_GLOBAL_FILE),
            expected_type=dict,
            lock_timeout=FISHING_GLOBAL_LOCK_TIMEOUT_SECONDS,
            default={},
        )
    return _normalize_global_state(data)


def _save_global_state(data: dict[str, Any]) -> None:
    data["version"] = 3
    data["date"] = _today_text()
    data["updated_at"] = _now_text()
    save_json_state(
        str(MINIAPP_FISHING_GLOBAL_FILE),
        data,
        lock_timeout=FISHING_GLOBAL_LOCK_TIMEOUT_SECONDS,
    )


def _next_participant(
    participants: list[str],
    current: str,
    completed: dict[str, Any],
) -> str:
    available = [item for item in participants if not completed.get(item)]
    if not available:
        return ""
    if current not in participants:
        return available[0]
    start = participants.index(current)
    for offset in range(1, len(participants) + 1):
        candidate = participants[(start + offset) % len(participants)]
        if candidate in available:
            return candidate
    return available[0]


def _reconcile_global_state(data: dict[str, Any], settings: dict[str, Any]) -> None:
    # Configuration reconciliation never creates a force-retry request or
    # clears completion gates. Only request_miniapp_fishing_force_retry does.
    configured_rod = configured_fishing_rod(settings)
    previous_rod = str(data.get("configured_rod") or "").strip()
    if previous_rod and previous_rod != configured_rod:
        data["rod_holder"] = ""
        data["rod_name"] = ""
        data["rod_holder_source"] = ""
        data["rod_holder_verified_at"] = ""
        data["scans"] = {}
        data["transfer"] = {}
        data["status"] = "scanning"
        data["detail"] = "鱼竿设置已变更，等待重新扫描"
    data["configured_rod"] = configured_rod

    participants = []
    for item in settings.get("participants") or []:
        account, identity = fishing_participant_parts(item)
        if account and identity:
            participants.append(automation_participant_key(account, identity))
    participants = list(dict.fromkeys(participants))
    scan_keys = fishing_scan_keys(settings)
    scan_key_set = set(scan_keys)
    participant_set = set(participants)
    old_participants = [str(item) for item in data.get("participants") or []]
    data["participants"] = participants

    # Keep today's completion journals even when a participant is temporarily
    # absent from a settings snapshot.  Workers can observe a partially loaded
    # settings file during a dashboard update; filtering these maps by the
    # instantaneous participant list would permanently reopen already-finished
    # identities when the full list returns.  Scheduling below still considers
    # only the currently selected ``participants`` list.
    completed = _mapping(data.get("completed_today"))
    data["completed_today"] = {
        str(key): value
        for key, value in completed.items()
        if fishing_participant_parts(key) != ("", "")
    }
    round_records = _mapping(data.get("round_records"))
    data["round_records"] = {
        str(key): value
        for key, value in round_records.items()
        if fishing_participant_parts(key) != ("", "") and isinstance(value, list)
    }
    emitted_ids = _mapping(data.get("summary_emitted_ids"))
    data["summary_emitted_ids"] = {
        str(key): value
        for key, value in emitted_ids.items()
        if fishing_participant_parts(key) != ("", "") and isinstance(value, list)
    }

    scans = {
        str(key): value
        for key, value in _mapping(data.get("scans")).items()
        if str(key) in scan_key_set and isinstance(value, dict)
    }
    data["scans"] = scans

    holder_key = str(data.get("rod_holder") or "").strip()
    if holder_key and holder_key not in scan_key_set:
        data["rod_holder"] = ""
        data["rod_name"] = ""
        data["rod_holder_source"] = ""
        data["rod_holder_verified_at"] = ""
        holder_key = ""
        data["status"] = "scanning"
        data["detail"] = "原持竿身份已取消勾选，等待在当前参与身份中重新识别"

    transfer = _mapping(data.get("transfer"))
    holder_scan = _mapping(scans.get(holder_key))
    holder_rod = str(holder_scan.get("rod_name") or "").strip()
    if transfer and not str(transfer.get("rod_name") or "").strip():
        inferred_rod = fishing_rod_name_in_text(transfer.get("response"))
        if not inferred_rod:
            inferred_rod = str(_mapping(scans.get(transfer.get("from"))).get("rod_name") or "")
        if inferred_rod in FISHING_ROD_ITEMS:
            transfer["rod_name"] = inferred_rod
    transfer_rod = str(transfer.get("rod_name") or "").strip()
    if (
        not previous_rod
        and transfer
        and transfer_rod
        and holder_rod
        and transfer_rod != holder_rod
    ):
        superseded_transfer = dict(transfer)
        superseded_transfer.update(
            status="superseded_rod_mismatch",
            superseded_at=_now_text(),
        )
        data["last_transfer"] = superseded_transfer
        data["transfer"] = {}
        transfer = {}
        data["status"] = "scanning"
        data["detail"] = (
            f"旧挂单索要{transfer_rod}，但持竿者实际使用{holder_rod}；"
            "已停止错误重试并重新识别"
        )
    if transfer:
        transfer_from = str(transfer.get("from") or "").strip()
        transfer_to = str(transfer.get("to") or "").strip()
        if transfer_from not in scan_key_set or transfer_to not in participant_set:
            cancelled = dict(transfer)
            cancelled.update(
                status="cancelled_participant_removed",
                cancelled_at=_now_text(),
            )
            data["last_transfer"] = cancelled
            data["transfer"] = {}
            transfer = {}
            data["status"] = "scanning"
            data["detail"] = "转竿涉及已取消勾选的身份，已停止旧流程并重新识别"
    if holder_rod and fishing_rod_matches(settings, holder_rod):
        data["rod_name"] = holder_rod
    current = str(data.get("current_key") or "")
    if old_participants != participants or current not in participants:
        holder = str(data.get("rod_holder") or "")
        if holder in participants and not data["completed_today"].get(holder):
            current = holder
        else:
            current = _next_participant(participants, "", data["completed_today"])
    elif data["completed_today"].get(current):
        current = _next_participant(participants, current, data["completed_today"])
    data["current_key"] = current
    if not participants:
        data["status"] = "no_participants"
        data["detail"] = "尚未选择垂钓身份"
    elif not current:
        data["status"] = "daily_done"
        data["detail"] = "所选身份今日垂钓均已完成"
    elif str(data.get("status") or "") in {"no_participants", "daily_done"}:
        data["status"] = "ready"
        data["detail"] = f"下一位 {fishing_participant_label(current)}"


def _update_global_state(
    updater: Any = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def transaction(current: Any) -> dict[str, Any]:
        data = _normalize_global_state(current)
        if settings is not None:
            _reconcile_global_state(data, settings)
        if callable(updater):
            updater(data)
        data["version"] = 3
        data["date"] = _today_text()
        data["updated_at"] = _now_text()
        return data

    try:
        return update_json_state(
            str(MINIAPP_FISHING_GLOBAL_FILE),
            transaction,
            expected_type=dict,
            default={},
            lock_timeout=FISHING_GLOBAL_LOCK_TIMEOUT_SECONDS,
        )
    except StateFileLockTimeout as exc:
        raise MiniAppFishingGlobalStateBusy() from exc


def miniapp_fishing_global_snapshot(
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fishing_settings = (
        miniapp_fishing_settings()
        if settings is None
        else miniapp_fishing_settings({"miniapp_fishing": dict(settings)})
    )
    data = _update_global_state(settings=fishing_settings)
    result = dict(data)
    result["current_label"] = fishing_participant_label(data.get("current_key"))
    result["rod_holder_label"] = fishing_participant_label(data.get("rod_holder"))
    result["rod_name"] = str(data.get("rod_name") or "")
    transfer = _mapping(data.get("transfer"))
    result["transfer_from_label"] = fishing_participant_label(transfer.get("from"))
    result["transfer_to_label"] = fishing_participant_label(transfer.get("to"))
    result["participant_labels"] = [
        fishing_participant_label(item) for item in data.get("participants") or []
    ]
    return result


def request_miniapp_fishing_force_retry(
    settings: dict[str, Any] | None = None,
    *,
    requested_by: str = "dashboard",
) -> dict[str, Any]:
    """Reset the shared queue so every selected identity is checked again once."""
    fishing_settings = (
        miniapp_fishing_settings()
        if settings is None
        else miniapp_fishing_settings({"miniapp_fishing": dict(settings)})
    )
    if not fishing_settings.get("enabled"):
        raise ValueError("Mini App fishing is disabled")
    participants = [
        str(item).strip()
        for item in fishing_settings.get("participants") or []
        if fishing_participant_parts(item) != ("", "")
    ]
    participants = list(dict.fromkeys(participants))
    if not participants:
        raise ValueError("Mini App fishing participants required")

    requested_at = _now_text()
    request_id = f"{time.time_ns()}-{os.getpid()}"

    def reset(data: dict[str, Any]) -> None:
        transfer = _mapping(data.get("transfer"))
        active_force = _mapping(data.get("force_retry"))
        completed_before = dict(
            _mapping(active_force.get("completed_before"))
            if active_force.get("pending")
            else _mapping(data.get("completed_today"))
        )
        if str(transfer.get("status") or "") in FISHING_TRANSFER_FAILURE_STATUSES:
            transfer["next_retry_at"] = ""
            transfer["force_retry_requested_at"] = requested_at
        data["current_key"] = participants[0]
        data["status"] = "force_retry"
        data["detail"] = "已忽略今日完成与错误冷却记录，正在强制重试"
        data["force_retry_request_id"] = request_id
        data["force_retry"] = {
            "id": request_id,
            "requested_at": requested_at,
            "requested_by": str(requested_by or "dashboard")[:100],
            "participants": participants,
            "pending": list(participants),
            "completed_before": completed_before,
            "confirmed_daily_done": {},
        }
        data["completed_today"] = {}

    _update_global_state(reset, settings=fishing_settings)
    return miniapp_fishing_global_snapshot(fishing_settings)


def fishing_shop(payload: Any) -> dict[str, Any]:
    return _mapping(_mapping(payload).get("shop"))


def fishing_option(items: Any, key: Any) -> dict[str, Any]:
    wanted = str(key or "").strip()
    for item in _items(items):
        if str(item.get("key") or "").strip() == wanted:
            return item
    return {}


def fishing_bait_by_name(shop: Any, name: Any) -> dict[str, Any]:
    wanted = str(name or "").strip()
    for item in _items(_mapping(shop).get("baits")):
        if str(item.get("name") or "").strip() == wanted:
            return item
    return {}


def affordable_bait_quantity(bait: dict[str, Any], requested: int, minimum: int = 1) -> int:
    """Limit a batch purchase to the quantity the shop costs can afford."""
    requested = max(1, _integer(requested, 1))
    minimum = max(1, _integer(minimum, 1))
    maximum = requested
    costs = _items(bait.get("cost"))
    for cost in costs:
        required = max(0, _integer(cost.get("qty"), 0))
        owned = max(0, _integer(cost.get("owned"), 0))
        if required:
            maximum = min(maximum, owned // required)
    if maximum < minimum:
        raise MiniAppBeastError("fishing_bait_unaffordable")
    return max(minimum, maximum)


def cost_shortages(costs: Any, quantity: int = 1) -> list[str]:
    """Describe shop materials whose owned quantity cannot cover a purchase."""
    multiplier = max(1, _integer(quantity, 1))
    shortages = []
    for cost in _items(costs):
        required = max(0, _integer(cost.get("qty"), 0)) * multiplier
        owned = max(0, _integer(cost.get("owned"), 0))
        if required > owned:
            name = str(cost.get("name") or cost.get("itemId") or "材料")
            shortages.append(f"{name}（当前 {owned}，需要 {required}）")
    return shortages


def _javascript_char_code_sum(value: Any) -> int:
    encoded = str(value or "seed").encode("utf-16-le", errors="surrogatepass")
    return sum(int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2))


def _js_round(value: float) -> int:
    return math.floor(float(value) + 0.5)


def build_fishing_proof(challenge: Any) -> dict[str, Any]:
    """Replay the page's 20 ms control model and keep tension inside its safe band."""

    data = _mapping(challenge)
    challenge_id = str(data.get("challengeId") or "").strip()
    if not challenge_id:
        raise MiniAppBeastError("fishing_challenge_missing")
    target_low = _number(data.get("targetLow"), 41)
    target_high = _number(data.get("targetHigh"), 68)
    if target_high <= target_low + 2:
        raise MiniAppBeastError("fishing_challenge_band_invalid")
    fish_power = max(0.1, _number(data.get("fishPower"), 1.7))
    min_duration_ms = max(0, _integer(data.get("minDurationMs"), 5200))
    max_duration_ms = max(min_duration_ms + 1000, _integer(data.get("maxDurationMs"), 70000))
    seed_offset = _javascript_char_code_sum(data.get("fishSeed") or "seed") / 19

    tension = (target_low + target_high) / 2 - 8
    progress = 0.0
    elapsed_ms = 0
    holding = False
    danger_ms = 0.0
    slack_ms = 0.0
    samples = 0
    stable_samples = 0
    actions = 0
    events: list[dict[str, Any]] = []
    width = target_high - target_low
    hold_below = target_low + width * 0.54
    release_above = target_low + width * 0.72

    while elapsed_ms < max_duration_ms and (progress < 100 or elapsed_ms < min_duration_ms):
        if tension < target_low:
            desired_holding = True
        elif tension > target_high:
            desired_holding = False
        elif holding:
            desired_holding = tension < release_above
        else:
            desired_holding = tension <= hold_below
        if desired_holding != holding:
            holding = desired_holding
            actions += 1
            events.append({"t": elapsed_ms + 20, "holding": holding})

        dt = 0.02
        elapsed_ms += 20
        pulse = math.sin(elapsed_ms * 0.0027 * fish_power + seed_offset)
        surge = max(0.0, math.sin(elapsed_ms * 0.0041 + seed_offset * 1.7))
        fish_pull = fish_power * (0.72 + pulse * 0.24 + surge * 0.42)
        if holding:
            tension += (24 + fish_pull * 3.1) * dt
        else:
            tension += (fish_pull * 4.8 - 24) * dt
        tension += math.sin(elapsed_ms * 0.012 + seed_offset) * 0.24
        tension = max(0.0, min(100.0, tension))

        in_band = target_low <= tension <= target_high
        if in_band:
            stable_samples += 1
            progress += (8.2 + fish_power * 0.7 + (2.2 if holding else 0.5)) * dt
        elif tension > target_high:
            danger_ms += dt * 1000
            progress -= (1.5 + fish_power * 0.25) * dt
        else:
            slack_ms += dt * 1000
            progress -= 0.9 * dt
        if holding and tension < target_low:
            progress += 1.1 * dt
        progress = max(0.0, min(100.0, progress))
        samples += 1

    if progress < 100:
        raise MiniAppBeastError("fishing_proof_not_landed")
    stability = stable_samples / samples if samples else 0.0
    penalty = danger_ms / 430 + slack_ms / 520 + max(0.0, 100 - progress) * 0.04
    score = max(55, min(100, _js_round(72 + stability * 28 - penalty)))
    return {
        "proof": {
            "mode": "xianxiaFishingV2",
            "challengeId": challenge_id,
            "durationMs": elapsed_ms,
            "events": events,
        },
        "predicted_score": score,
        "stability": stability,
        "danger_ms": danger_ms,
        "slack_ms": slack_ms,
        "actions": actions,
        "progress": progress,
    }


def fishing_result_summary(finish_payload: Any, catch_payload: Any) -> str:
    score_result = _mapping(_mapping(finish_payload).get("result"))
    details = _mapping(score_result.get("details"))
    catch_result = _mapping(_mapping(catch_payload).get("result"))
    grade = str(score_result.get("grade") or "-")
    score = _integer(score_result.get("score"), 0)
    stability = round(_number(details.get("stability"), 0) * 100)
    quality = round(_number(score_result.get("quality_bonus"), 0) * 100)
    parts = [f"评分 {grade} {score}分", f"稳定 {stability}%", f"品质 +{quality}%"]
    if not catch_result.get("ready"):
        parts.append("鱼获结算中")
        return "，".join(parts)
    if catch_result.get("caught") and isinstance(catch_result.get("fish"), dict):
        fish = catch_result["fish"]
        parts.append(
            f"提竿成功：【{str(fish.get('name') or '灵鱼')}】"
            f" {str(catch_result.get('rarityLabel') or catch_result.get('rarity') or '')}"
            f" {float(fish.get('weight') or 0):.2f}斤"
        )
    else:
        parts.append(f"空竿：{str(catch_result.get('message') or '水下灵影擦钩而过')}" )
    exp_gain = _integer(catch_result.get("expGain"), 0)
    if exp_gain:
        parts.append(f"钓术经验 +{exp_gain}")
    loot = []
    for item in _items(catch_result.get("bonusLoot")):
        name = str(item.get("name") or "物品")
        loot.append(f"{name}x{max(1, _integer(item.get('qty'), 1))}")
    if loot:
        parts.append("伴生机缘 " + "、".join(loot))
    return "，".join(part for part in parts if part)


def fishing_rounds_summary(records: Any, *, daily_limit_reached: bool = False) -> str:
    """Build one readable log entry from all buffered rounds for an identity."""
    rounds = _items(records)
    if not rounds:
        return ""

    configurations = []
    for item in rounds:
        config = (
            str(item.get("pond") or "灵溪"),
            str(item.get("bait") or "鱼饵"),
            str(item.get("chum") or "不打窝"),
        )
        if config not in configurations:
            configurations.append(config)

    if len(configurations) == 1:
        pond, bait, chum = configurations[0]
        count_text = (
            f"本次记录 {len(rounds)} 竿，服务端今日竿数已尽"
            if daily_limit_reached
            else f"共 {len(rounds)} 竿"
        )
        title = f"灵溪垂钓汇总（{pond} · {bait} · {chum}，{count_text}）"
    else:
        title = (
            f"灵溪垂钓汇总（本次记录 {len(rounds)} 竿，服务端今日竿数已尽）"
            if daily_limit_reached
            else f"灵溪垂钓汇总（共 {len(rounds)} 竿）"
        )

    purchase_totals: dict[str, int] = {}
    loot_totals: dict[str, int] = {}
    caught_count = 0
    total_weight = 0.0
    total_exp = 0
    lines = []

    for index, item in enumerate(rounds, 1):
        for purchase in _items(item.get("purchases")):
            name = str(purchase.get("name") or "鱼饵")
            purchase_totals[name] = purchase_totals.get(name, 0) + max(
                1, _integer(purchase.get("quantity"), 1)
            )
        for loot in _items(item.get("bonus_loot")):
            name = str(loot.get("name") or "物品")
            loot_totals[name] = loot_totals.get(name, 0) + max(
                1, _integer(loot.get("qty"), 1)
            )

        caught = bool(item.get("caught"))
        caught_count += int(caught)
        total_weight += _number(item.get("weight"), 0)
        total_exp += _integer(item.get("exp_gain"), 0)
        detail = str(item.get("summary") or "本竿结果未记录").strip()
        if len(configurations) > 1:
            detail = (
                f"[{str(item.get('pond') or '灵溪')} · "
                f"{str(item.get('bait') or '鱼饵')} · "
                f"{str(item.get('chum') or '不打窝')}] {detail}"
            )
        lines.append(f"{index}. {detail}")

    if purchase_totals:
        title += "｜自动购饵 " + "、".join(
            f"{name}x{quantity}" for name, quantity in purchase_totals.items()
        )

    total_parts = [f"成功 {caught_count}/{len(rounds)} 竿"]
    if total_weight > 0:
        total_parts.append(f"总重 {total_weight:.2f}斤")
    if total_exp:
        total_parts.append(f"钓术经验 +{total_exp}")
    if loot_totals:
        total_parts.append(
            "伴生机缘 "
            + "、".join(f"{name}x{quantity}" for name, quantity in loot_totals.items())
        )
    if daily_limit_reached:
        lines.append("服务端状态：今日竿数已尽；重启或人工垂钓产生的未记录鱼获不计入下方合计。")
    lines.append("合计：" + "，".join(total_parts))
    return "\n".join([title, *lines])


class MiniAppFishingAutomation:
    """Coordinate server-verified fishing and one shared rod across accounts."""

    def __init__(self, actor: Any, transport: Any, account: str, logger: Any) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        config = getattr(actor, "config", {}) or {}
        settings = config.get("miniapp_beast") or {}
        self.retry_seconds = max(
            15,
            _integer(settings.get("fishing_retry_seconds"), DEFAULT_RETRY_SECONDS),
        )
        self._current_identity = "主魂"
        self._last_round_completed = False
        self._scan_started = False
        self._force_retry_request_id = ""
        self._global_state_busy_failures = 0

    @property
    def supported(self) -> bool:
        return self.account in MINIAPP_FISHING_SUPPORTED_ACCOUNTS

    def settings(self) -> dict[str, Any]:
        return miniapp_fishing_settings()

    def _state(self, identity: str | None = None) -> dict[str, Any]:
        return identity_state(self.actor, str(identity or self._current_identity or "主魂"))

    def _save(self) -> None:
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App fishing state save failed", exc_info=True)

    async def _notify_material_shortage(
        self,
        identity: str,
        *,
        kind: str,
        name: str,
        shortages: list[str],
    ) -> None:
        """Send one daily direct notice when fishing materials are insufficient."""
        if not shortages:
            return
        state = self._state(identity)
        notice_key = f"{kind}|{name}"
        today = _today_text()
        last_notice_time = str(state.get("miniapp_fishing_material_notice_time") or "").strip()
        if last_notice_time:
            try:
                elapsed = (datetime.now() - datetime.strptime(last_notice_time, TIME_FORMAT)).total_seconds()
                if elapsed < FISHING_MATERIAL_NOTICE_RETRY_SECONDS:
                    return
            except ValueError:
                pass
        if (
            str(state.get("miniapp_fishing_material_notice_date") or "") == today
            and str(state.get("miniapp_fishing_material_notice_key") or "") == notice_key
            and bool(state.get("miniapp_fishing_material_notice_sent"))
        ):
            return
        failure_already_logged = (
            str(state.get("miniapp_fishing_material_notice_error_date") or "") == today
            and str(state.get("miniapp_fishing_material_notice_error_key") or "") == notice_key
        )
        self._record(
            identity,
            miniapp_fishing_material_notice_date=today,
            miniapp_fishing_material_notice_key=notice_key,
            miniapp_fishing_material_notice_time=_now_text(),
            miniapp_fishing_material_notice_sent=False,
        )
        client = getattr(self.actor, "client", None)
        if client is None or not hasattr(client, "send_message"):
            return
        config = getattr(self.actor, "config", {}) or {}
        target = config.get("notify_target_username") or config.get("notify_target") or "@Waaiging"
        if isinstance(target, str) and target.lstrip("-").isdigit():
            target = "@Waaiging"
        if isinstance(target, str) and not target.startswith("@") and not target.lstrip("-").isdigit():
            target = f"@{target}"
        message = (
            "⚠️ 灵溪自动垂钓材料不足\n"
            f"账号：{ACCOUNT_NAMES.get(self.account, self.account)}；身份：{identity}\n"
            f"{kind}：{name}\n"
            "缺少：" + "、".join(shortages) + "\n"
            "本轮已暂停，1小时后自动重试。"
        )
        try:
            await asyncio.wait_for(client.send_message(target, message), timeout=12)
        except Exception as exc:
            self._record(
                identity,
                miniapp_fishing_material_notice_error_date=today,
                miniapp_fishing_material_notice_error_key=notice_key,
                miniapp_fishing_material_notice_error=str(exc)[:300],
                miniapp_fishing_material_notice_error_time=_now_text(),
            )
            if not failure_already_logged:
                self.log.warning("Mini App fishing material notification failed: %s", exc)
            return
        self._record(
            identity,
            miniapp_fishing_material_notice_date=today,
            miniapp_fishing_material_notice_key=notice_key,
            miniapp_fishing_material_notice_time=_now_text(),
            miniapp_fishing_material_notice_sent=True,
            miniapp_fishing_material_notice_error_date="",
            miniapp_fishing_material_notice_error_key="",
            miniapp_fishing_material_notice_error="",
            miniapp_fishing_material_notice_error_time="",
        )

    def _record(self, identity: str | None = None, **updates: Any) -> None:
        state = self._state(identity)
        state.update(updates)
        state["miniapp_fishing_account"] = self.account
        state["miniapp_fishing_identity"] = str(identity or self._current_identity or "主魂")
        state["miniapp_fishing_updated_at"] = _now_text()
        self._save()

    def _local_selected_identities(self, settings: dict[str, Any]) -> list[str]:
        identities = []
        for key in settings.get("participants") or []:
            account, identity = fishing_participant_parts(key)
            if account == self.account and identity not in identities:
                identities.append(identity)
        return identities

    def _local_relevant_identities(self, settings: dict[str, Any]) -> list[str]:
        identities = []
        for key in fishing_scan_keys(settings):
            account, identity = fishing_participant_parts(key)
            if account == self.account and identity not in identities:
                identities.append(identity)
        return identities

    def _status_identity(self, settings: dict[str, Any]) -> str:
        relevant = self._local_relevant_identities(settings)
        if self._current_identity in relevant:
            return self._current_identity
        selected = self._local_selected_identities(settings)
        return (selected or relevant or ["主魂"])[0]

    def _clear_irrelevant_local_statuses(self, settings: dict[str, Any]) -> None:
        relevant = set(self._local_relevant_identities(settings))
        changed = False
        for identity in ACCOUNT_IDENTITIES.get(self.account, ()):
            if identity in relevant:
                continue
            state = self._state(identity)
            if (
                str(state.get("miniapp_fishing_status") or "") == "not_selected"
                and not state.get("miniapp_fishing_next_run_time")
                and not state.get("miniapp_fishing_last_error")
            ):
                continue
            state.update(
                miniapp_fishing_status="not_selected",
                miniapp_fishing_last_error="",
                miniapp_fishing_next_run_time="",
                miniapp_fishing_account=self.account,
                miniapp_fishing_identity=identity,
                miniapp_fishing_updated_at=_now_text(),
            )
            changed = True
        if changed:
            self._save()

    def _apply_force_retry_request(self, settings: dict[str, Any]) -> dict[str, Any]:
        runtime = miniapp_fishing_global_snapshot(settings)
        force = _mapping(runtime.get("force_retry"))
        request_id = str(force.get("id") or runtime.get("force_retry_request_id") or "")
        if not request_id or request_id == self._force_retry_request_id:
            return force
        self._force_retry_request_id = request_id
        if not force.get("pending"):
            return force
        self._scan_started = False
        for identity in self._local_relevant_identities(settings):
            self._record(
                identity,
                miniapp_fishing_status="force_retry",
                miniapp_fishing_last_error="",
                miniapp_fishing_last_error_time="",
                miniapp_fishing_next_run_time="",
            )
        return force

    async def _sleep_until_next_cycle(
        self,
        wait: int,
        settings: dict[str, Any],
        *,
        uncapped: bool = False,
    ) -> None:
        """Keep normal backoff while allowing a new dashboard retry to wake the loop."""
        remaining = max(1, int(wait))
        if not uncapped:
            remaining = min(remaining, 300)
        while remaining > 0 and getattr(self.actor, "is_running", True):
            interval = min(5, remaining)
            await asyncio.sleep(interval)
            remaining -= interval
            request_id = str(_load_global_state().get("force_retry_request_id") or "")
            if request_id and request_id != self._force_retry_request_id:
                return

    def _journal_round_record(self, identity: str, record: dict[str, Any]) -> None:
        participant_key = automation_participant_key(self.account, identity)
        record_id = str(record.get("id") or "").strip()

        def update(data: dict[str, Any]) -> None:
            records_by_participant = data.setdefault("round_records", {})
            records = [
                dict(item)
                for item in _items(records_by_participant.get(participant_key))
            ]
            if record_id and any(str(item.get("id") or "") == record_id for item in records):
                return
            records.append(dict(record))
            records_by_participant[participant_key] = records[-50:]

        _update_global_state(update)

    def _merged_round_records(self, identity: str) -> tuple[list[dict[str, Any]], set[str]]:
        participant_key = automation_participant_key(self.account, identity)
        runtime = _update_global_state()
        global_records = [
            dict(item)
            for item in _items(_mapping(runtime.get("round_records")).get(participant_key))
        ]
        state = self._state(identity)
        local_records = (
            [dict(item) for item in _items(state.get("miniapp_fishing_round_records"))]
            if str(state.get("miniapp_fishing_summary_date") or "") == _today_text()
            else []
        )
        merged: dict[str, dict[str, Any]] = {}
        for index, item in enumerate([*global_records, *local_records]):
            record_id = str(item.get("id") or "").strip()
            key = record_id or (
                f"legacy:{str(item.get('completed_at') or '')}:"
                f"{str(item.get('summary') or '')}:{index}"
            )
            merged[key] = item
        records = sorted(
            merged.values(),
            key=lambda item: str(item.get("completed_at") or ""),
        )[-50:]
        emitted_ids = {
            str(item)
            for item in _mapping(runtime.get("summary_emitted_ids")).get(participant_key, [])
            if str(item).strip()
        }
        local_emitted_count = max(
            0,
            min(
                len(local_records),
                _integer(state.get("miniapp_fishing_summary_emitted_count"), 0),
            ),
        )
        emitted_ids.update(
            str(item.get("id") or "")
            for item in local_records[:local_emitted_count]
            if str(item.get("id") or "").strip()
        )
        return records, emitted_ids

    def _mark_global_summary_emitted(self, identity: str, record_ids: list[str]) -> None:
        participant_key = automation_participant_key(self.account, identity)
        normalized = [str(item).strip() for item in record_ids if str(item).strip()]

        def update(data: dict[str, Any]) -> None:
            emitted_by_participant = data.setdefault("summary_emitted_ids", {})
            emitted = [
                str(item)
                for item in emitted_by_participant.get(participant_key) or []
                if str(item).strip()
            ]
            emitted_by_participant[participant_key] = list(
                dict.fromkeys([*emitted, *normalized])
            )[-50:]

        _update_global_state(update)

    def _append_round_summary(
        self,
        identity: str,
        *,
        record_id: str,
        pond: str,
        bait: str,
        chum: str,
        purchases: list[dict[str, Any]],
        summary: str,
        caught: bool,
        weight: float,
        exp_gain: int,
        bonus_loot: list[dict[str, Any]],
    ) -> None:
        state = self._state(identity)
        today = _today_text()
        same_day = str(state.get("miniapp_fishing_summary_date") or "") == today
        records = [
            dict(item)
            for item in _items(state.get("miniapp_fishing_round_records"))
        ] if same_day else []
        emitted_count = (
            max(0, _integer(state.get("miniapp_fishing_summary_emitted_count"), 0))
            if same_day
            else 0
        )
        normalized_id = str(record_id or "").strip()
        existing = next(
            (
                item
                for item in records
                if normalized_id and str(item.get("id") or "") == normalized_id
            ),
            None,
        )
        if existing is not None:
            try:
                self._journal_round_record(identity, existing)
            except Exception:
                self.log.warning("Mini App fishing round journal write failed", exc_info=True)
            return
        record = {
            "id": normalized_id or f"{_now_text()}-{len(records) + 1}",
            "completed_at": _now_text(),
            "pond": str(pond or "灵溪"),
            "bait": str(bait or "鱼饵"),
            "chum": str(chum or "不打窝"),
            "purchases": [dict(item) for item in _items(purchases)],
            "summary": str(summary or "本竿结果未记录")[:1200],
            "caught": bool(caught),
            "weight": max(0.0, _number(weight, 0)),
            "exp_gain": max(0, _integer(exp_gain, 0)),
            "bonus_loot": [dict(item) for item in _items(bonus_loot)],
        }
        try:
            self._journal_round_record(identity, record)
        except Exception:
            self.log.warning("Mini App fishing round journal write failed", exc_info=True)
        records.append(record)
        records = records[-50:]
        self._record(
            identity,
            miniapp_fishing_summary_date=today,
            miniapp_fishing_round_records=records,
            miniapp_fishing_round_count_today=len(records),
            miniapp_fishing_summary_emitted_count=min(emitted_count, len(records)),
        )

    def _emit_daily_summary(self, identity: str, *, daily_limit_reached: bool = False) -> bool:
        state = self._state(identity)
        try:
            records, emitted_ids = self._merged_round_records(identity)
        except Exception:
            self.log.warning("Mini App fishing round journal read failed", exc_info=True)
            if str(state.get("miniapp_fishing_summary_date") or "") != _today_text():
                return False
            records = [dict(item) for item in _items(state.get("miniapp_fishing_round_records"))]
            emitted_count = max(
                0,
                min(
                    len(records),
                    _integer(state.get("miniapp_fishing_summary_emitted_count"), 0),
                ),
            )
            emitted_ids = {
                str(item.get("id") or "")
                for item in records[:emitted_count]
                if str(item.get("id") or "").strip()
            }
        if not records:
            return False
        pending = [
            item
            for item in records
            if not str(item.get("id") or "").strip()
            or str(item.get("id") or "").strip() not in emitted_ids
        ]
        summary_text = fishing_rounds_summary(
            pending,
            daily_limit_reached=daily_limit_reached,
        )
        if not summary_text:
            return False
        self.log.info("IN [Mini App | %s]:\n%s", identity, summary_text)
        try:
            self._mark_global_summary_emitted(
                identity,
                [str(item.get("id") or "") for item in pending],
            )
        except Exception:
            self.log.warning("Mini App fishing summary journal write failed", exc_info=True)
        self._record(
            identity,
            miniapp_fishing_summary_date=_today_text(),
            miniapp_fishing_round_records=records,
            miniapp_fishing_round_count_today=len(records),
            miniapp_fishing_summary_emitted_count=len(records),
            miniapp_fishing_last_daily_summary=summary_text[:5000],
            miniapp_fishing_last_daily_summary_time=_now_text(),
        )
        return True

    def _record_shop(self, identity: str, shop: dict[str, Any]) -> None:
        self._record(
            identity,
            miniapp_fishing_pond_options=[
                {
                    "key": str(item.get("key") or ""),
                    "name": str(item.get("name") or ""),
                    "unlocked": bool(item.get("unlocked")),
                    "required_exp": _integer(item.get("requiredExp"), 0),
                    "current_exp": _integer(item.get("currentExp"), 0),
                }
                for item in _items(shop.get("ponds"))
            ],
            miniapp_fishing_bait_options=[
                {
                    "key": str(item.get("key") or ""),
                    "item_id": str(item.get("itemId") or ""),
                    "name": str(item.get("name") or ""),
                    "count": _integer(item.get("count"), 0),
                    "unlocked": bool(item.get("unlocked")),
                    "cost": [dict(cost) for cost in _items(item.get("cost"))],
                }
                for item in _items(shop.get("baits"))
            ],
            miniapp_fishing_chum_options=[
                {
                    "key": str(item.get("key") or ""),
                    "name": str(item.get("name") or ""),
                    "uses": _integer(item.get("uses"), 0),
                    "remaining_today": _integer(item.get("remainingToday"), 0),
                    "affordable": bool(item.get("affordable")),
                    "cost": [dict(cost) for cost in _items(item.get("cost"))],
                }
                for item in _items(shop.get("chums"))
            ],
            miniapp_fishing_active_chum=_mapping(shop.get("activeChum")),
            miniapp_fishing_last_shop_sync_time=_now_text(),
        )

    async def _ensure_bait(
        self,
        identity: str,
        token: str,
        shop: dict[str, Any],
        bait_key: str,
        minimum: int = 1,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        bait = fishing_option(shop.get("baits"), bait_key)
        if not bait:
            raise MiniAppBeastError("fishing_bait_invalid")
        if not bait.get("unlocked"):
            raise MiniAppBeastError("fishing_bait_level_low")
        missing = max(0, int(minimum) - _integer(bait.get("count"), 0))
        if missing <= 0:
            return shop, bait
        try:
            quantity = affordable_bait_quantity(bait, BAIT_PURCHASE_QUANTITY, minimum)
        except MiniAppCircuitOpenError:
            raise
        except MiniAppBeastError as exc:
            if exc.code == "fishing_bait_unaffordable":
                shortages = cost_shortages(bait.get("cost"), minimum)
                await self._notify_material_shortage(
                    identity,
                    kind="鱼饵",
                    name=str(bait.get("name") or bait_key),
                    shortages=shortages,
                )
            raise
        bought = await self.transport.fishing_buy_bait(
            identity,
            token,
            str(bait.get("key") or bait_key),
            quantity,
            bait.get("cost"),
            log_operation=False,
        )
        updated_shop = fishing_shop(bought) or shop
        self._record_shop(identity, updated_shop)
        updated_bait = fishing_option(updated_shop.get("baits"), bait_key)
        if _integer(updated_bait.get("count"), 0) < minimum:
            raise MiniAppBeastError("fishing_bait_missing")
        bait_name = str(updated_bait.get("name") or bait.get("name") or bait_key)
        pending_purchases = [
            dict(item)
            for item in _items(
                self._state(identity).get("miniapp_fishing_pending_purchases")
            )
        ]
        existing = next(
            (item for item in pending_purchases if str(item.get("name") or "") == bait_name),
            None,
        )
        if existing is None:
            pending_purchases.append({"name": bait_name, "quantity": quantity})
        else:
            existing["quantity"] = _integer(existing.get("quantity"), 0) + quantity
        self._record(
            identity,
            miniapp_fishing_last_purchase_time=_now_text(),
            miniapp_fishing_last_purchase=f"{bait_name} x{quantity}",
            miniapp_fishing_pending_purchases=pending_purchases,
        )
        return updated_shop, updated_bait

    async def _ensure_chum(
        self,
        identity: str,
        token: str,
        shop: dict[str, Any],
        chum_key: str,
    ) -> dict[str, Any]:
        if not chum_key or chum_key == "none":
            return shop
        active = _mapping(shop.get("activeChum"))
        if active:
            return shop
        chum = fishing_option(shop.get("chums"), chum_key)
        if not chum:
            raise MiniAppBeastError("fishing_chum_invalid")
        if _integer(chum.get("remainingToday"), 0) <= 0:
            return self._use_no_chum_after_daily_limit(identity, shop, chum)
        for cost in _items(chum.get("cost")):
            required = _integer(cost.get("qty"), 0)
            owned = _integer(cost.get("owned"), 0)
            if owned >= required:
                continue
            bait = fishing_bait_by_name(shop, cost.get("name"))
            if not bait:
                continue
            try:
                shop, _ = await self._ensure_bait(
                    identity,
                    token,
                    shop,
                    str(bait.get("key") or ""),
                    required,
                )
            except MiniAppCircuitOpenError:
                raise
            except MiniAppBeastError as exc:
                if exc.code == "fishing_bait_unaffordable":
                    return self._use_no_chum_after_unaffordable(identity, shop, chum)
                raise
        refreshed = fishing_shop(
            await self.transport.fishing_shop(identity, token)
        ) or shop
        self._record_shop(identity, refreshed)
        chum = fishing_option(refreshed.get("chums"), chum_key)
        if not chum.get("affordable"):
            await self._notify_material_shortage(
                identity,
                kind="鱼窝",
                name=str(chum.get("name") or chum_key),
                shortages=cost_shortages(chum.get("cost")),
            )
            return self._use_no_chum_after_unaffordable(identity, refreshed, chum)
        try:
            applied = await self.transport.fishing_apply_chum(
                identity,
                token,
                chum_key,
                log_operation=False,
            )
        except MiniAppCircuitOpenError:
            raise
        except MiniAppBeastError as exc:
            if exc.code == "fishing_chum_daily_limit":
                return self._use_no_chum_after_daily_limit(identity, refreshed, chum)
            if exc.code == "fishing_chum_unaffordable":
                return self._use_no_chum_after_unaffordable(identity, refreshed, chum)
            raise
        updated = fishing_shop(applied) or refreshed
        self._record_shop(identity, updated)
        self._record(
            identity,
            miniapp_fishing_last_chum_time=_now_text(),
            miniapp_fishing_last_chum=str(chum.get("name") or chum_key),
        )
        return updated

    def _use_no_chum_after_daily_limit(
        self,
        identity: str,
        shop: dict[str, Any],
        chum: dict[str, Any],
    ) -> dict[str, Any]:
        return self._use_no_chum_fallback(
            identity,
            shop,
            chum,
            reason="daily_limit",
            detail_suffix="今日打窝次数已尽",
        )

    def _use_no_chum_after_unaffordable(
        self,
        identity: str,
        shop: dict[str, Any],
        chum: dict[str, Any],
    ) -> dict[str, Any]:
        """Continue without optional chum when its materials are insufficient."""
        return self._use_no_chum_fallback(
            identity,
            shop,
            chum,
            reason="unaffordable",
            detail_suffix="所需材料不足",
        )

    def _use_no_chum_fallback(
        self,
        identity: str,
        shop: dict[str, Any],
        chum: dict[str, Any],
        *,
        reason: str,
        detail_suffix: str,
    ) -> dict[str, Any]:
        """Treat an unavailable configured chum as a normal fallback."""
        today = _today_text()
        chum_key = str(chum.get("key") or "")
        chum_name = str(chum.get("name") or chum_key or "所选鱼窝")
        state = self._state(identity)
        already_recorded = (
            str(state.get("miniapp_fishing_chum_fallback_date") or "") == today
            and str(state.get("miniapp_fishing_chum_fallback_key") or "") == chum_key
        )
        self._record(
            identity,
            miniapp_fishing_chum_fallback_date=today,
            miniapp_fishing_chum_fallback_key=chum_key,
            miniapp_fishing_chum_fallback_name=chum_name,
            miniapp_fishing_chum_fallback_reason=reason,
            miniapp_fishing_chum_fallback_detail=(
                f"{chum_name}{detail_suffix}；本日后续继续不打窝"
            ),
        )
        if not already_recorded:
            logger = getattr(self.log, "info", None)
            if callable(logger):
                logger(
                    "Mini App fishing chum fallback for %s (%s): %s; continuing without chum.",
                    identity,
                    chum_name,
                    reason,
                )
        return shop

    def _resolve_pond(
        self,
        identity: str,
        shop: dict[str, Any],
        configured_key: str,
    ) -> tuple[dict[str, Any], str]:
        """Use the configured pond when available, otherwise a safe unlocked pond."""
        pond = fishing_option(shop.get("ponds"), configured_key)
        if not pond:
            raise MiniAppBeastError("fishing_pond_invalid")
        if pond.get("unlocked"):
            return pond, configured_key

        fallback = next(
            (
                item
                for item in _items(shop.get("ponds"))
                if isinstance(item, dict) and item.get("unlocked") and item.get("key")
            ),
            None,
        )
        if fallback is None:
            raise MiniAppBeastError("fishing_pond_locked")

        fallback_key = str(fallback.get("key") or "")
        state = self._state(identity)
        today = _today_text()
        already_recorded = (
            str(state.get("miniapp_fishing_pond_fallback_date") or "") == today
            and str(state.get("miniapp_fishing_pond_fallback_key") or "") == configured_key
            and str(state.get("miniapp_fishing_pond_fallback_to") or "") == fallback_key
        )
        self._record(
            identity,
            miniapp_fishing_pond_fallback_date=today,
            miniapp_fishing_pond_fallback_key=configured_key,
            miniapp_fishing_pond_fallback_to=fallback_key,
            miniapp_fishing_pond_fallback_name=str(fallback.get("name") or fallback_key),
            miniapp_fishing_pond_fallback_reason="locked",
        )
        if not already_recorded:
            self.log.warning(
                "Mini App fishing pond %s is locked for %s; falling back to %s.",
                configured_key,
                identity,
                fallback_key,
            )
        return fallback, fallback_key

    @staticmethod
    def _wait_for_bite(session: dict[str, Any]) -> int:
        bite_at = _integer(session.get("biteAt"), 0)
        server_now = _integer(session.get("serverNow"), 0)
        if not bite_at:
            return 5
        if not server_now:
            server_now = int(datetime.now().timestamp() * 1000)
        return max(1, math.ceil(max(0, bite_at - server_now) / 1000) + 1)

    async def _start_cast(
        self,
        identity: str,
        token: str,
        shop: dict[str, Any],
        settings: dict[str, Any],
    ) -> int:
        pond_key = str(settings.get("pond") or "qingxi")
        bait_key = str(settings.get("bait") or "demon_blood")
        chum_key = str(settings.get("chum") or "none")
        pond, pond_key = self._resolve_pond(identity, shop, pond_key)
        self._record(identity, miniapp_fishing_pending_purchases=[])
        shop = await self._ensure_chum(identity, token, shop, chum_key)
        shop, bait = await self._ensure_bait(identity, token, shop, bait_key)
        token, _ = await self.transport.fishing_next_cast(
            identity,
            token,
            pond_key,
            str(bait.get("itemId") or ""),
            log_operation=False,
        )
        token, start = await self.transport.fishing_start(identity, token)
        session = _mapping(start.get("session"))
        wait = self._wait_for_bite(session)
        active_chum = _mapping(shop.get("activeChum"))
        self._record(
            identity,
            miniapp_fishing_status="waiting",
            miniapp_fishing_last_error="",
            miniapp_fishing_pond=str(pond.get("name") or pond_key),
            miniapp_fishing_pond_key=pond_key,
            miniapp_fishing_bait=str(bait.get("name") or bait_key),
            miniapp_fishing_bait_key=bait_key,
            miniapp_fishing_chum=str(active_chum.get("name") or "不打窝"),
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(seconds=wait)
            ).strftime(TIME_FORMAT),
            miniapp_fishing_last_cast_time=_now_text(),
        )
        return wait

    async def _finish_challenge(
        self,
        identity: str,
        token: str,
        session: dict[str, Any],
        challenge: dict[str, Any],
    ) -> int:
        built = build_fishing_proof(challenge)
        proof = built["proof"]
        pond = str(_mapping(session.get("pond")).get("name") or "灵溪")
        bait = str(_mapping(session.get("bait")).get("name") or "鱼饵")
        self._record(
            identity,
            miniapp_fishing_status="reeling",
            miniapp_fishing_predicted_score=built["predicted_score"],
            miniapp_fishing_predicted_stability=round(built["stability"] * 100, 2),
            miniapp_fishing_reel_actions=built["actions"],
            miniapp_fishing_reel_duration_ms=proof["durationMs"],
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(milliseconds=proof["durationMs"] + 300)
            ).strftime(TIME_FORMAT),
        )
        await asyncio.sleep(proof["durationMs"] / 1000 + 0.2)
        finish = await self.transport.fishing_finish(
            identity,
            token,
            proof,
            log_operation=False,
        )
        catch_payload: dict[str, Any] = {}
        for attempt in range(DEFAULT_RESULT_ATTEMPTS):
            await asyncio.sleep(0.65 if attempt < 4 else 1.0)
            try:
                catch_payload = await self.transport.fishing_result(identity, token)
            except MiniAppCircuitOpenError:
                raise
            except MiniAppBeastError:
                if attempt >= 8:
                    break
                continue
            if _mapping(catch_payload.get("result")).get("ready"):
                break
        summary = fishing_result_summary(finish, catch_payload)
        score_result = _mapping(finish.get("result"))
        details = _mapping(score_result.get("details"))
        catch_result = _mapping(catch_payload.get("result"))
        fish = _mapping(catch_result.get("fish"))
        ready = bool(catch_result.get("ready"))
        caught = bool(catch_result.get("caught"))
        pending_purchases = _items(
            self._state(identity).get("miniapp_fishing_pending_purchases")
        )
        chum = str(self._state(identity).get("miniapp_fishing_chum") or "不打窝")
        if ready:
            self._append_round_summary(
                identity,
                record_id=str(challenge.get("challengeId") or ""),
                pond=pond,
                bait=bait,
                chum=chum,
                purchases=pending_purchases,
                summary=summary,
                caught=caught,
                weight=_number(fish.get("weight"), 0),
                exp_gain=_integer(catch_result.get("expGain"), 0),
                bonus_loot=_items(catch_result.get("bonusLoot")),
            )
        self._record(
            identity,
            miniapp_fishing_status=("caught" if caught else "empty" if ready else "settling"),
            miniapp_fishing_last_error="",
            miniapp_fishing_last_result=summary[:1000],
            miniapp_fishing_last_round_time=_now_text(),
            miniapp_fishing_last_score=_integer(score_result.get("score"), 0),
            miniapp_fishing_last_grade=str(score_result.get("grade") or ""),
            miniapp_fishing_last_stability=round(_number(details.get("stability"), 0) * 100, 2),
            miniapp_fishing_last_quality_bonus=round(
                _number(score_result.get("quality_bonus"), 0) * 100,
                2,
            ),
            miniapp_fishing_last_caught=caught,
            miniapp_fishing_last_fish=str(fish.get("name") or ""),
            miniapp_fishing_last_weight=_number(fish.get("weight"), 0),
            miniapp_fishing_last_exp_gain=_integer(catch_result.get("expGain"), 0),
            miniapp_fishing_last_bonus_loot=_items(catch_result.get("bonusLoot")),
            miniapp_fishing_pending_purchases=([] if ready else pending_purchases),
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(seconds=3 if ready else 30)
            ).strftime(TIME_FORMAT),
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder) and ready:
            try:
                recorder(
                    identity,
                    f".钓鱼 {bait}",
                    summary,
                    source="Mini App 灵溪垂钓",
                    final=True,
                )
            except Exception:
                self.log.warning("Mini App fishing reward recording failed", exc_info=True)
        self._last_round_completed = ready
        return 3 if ready else 30

    async def run_cycle(
        self,
        settings: dict[str, Any] | None = None,
        identity: str = "",
    ) -> int:
        settings = dict(settings or self.settings())
        identity = str(identity or self._current_identity or "主魂")
        self._current_identity = identity
        self._last_round_completed = False
        token, payload = await self.transport.fishing_entry(identity)
        session = _mapping(payload.get("session"))
        challenge = _mapping(payload.get("challenge"))
        shop_payload = await self.transport.fishing_shop(identity, token)
        shop = fishing_shop(shop_payload)
        self._record_shop(identity, shop)
        rod = _mapping(session.get("rod"))
        rod_name = str(rod.get("name") or "").strip()
        if rod:
            self._record(
                identity,
                miniapp_fishing_rod=rod_name,
                miniapp_fishing_rod_item_id=str(rod.get("itemId") or ""),
            )
        if challenge:
            return await self._finish_challenge(identity, token, session, challenge)
        phase = str(session.get("phase") or "").strip().lower()
        if phase == "lobby":
            if not fishing_rod_matches(settings, rod_name):
                raise MiniAppBeastError("fishing_rod_type_mismatch")
            return await self._start_cast(identity, token, shop, settings)
        if phase == "waiting":
            wait = self._wait_for_bite(session)
            self._record(
                identity,
                miniapp_fishing_status="waiting",
                miniapp_fishing_last_error="",
                miniapp_fishing_next_run_time=(
                    datetime.now() + timedelta(seconds=wait)
                ).strftime(TIME_FORMAT),
            )
            return wait
        if phase in {"expired", "settled", "missed"}:
            catch_payload = await self.transport.fishing_result(identity, token)
            summary = fishing_result_summary({}, catch_payload)
            ready = bool(_mapping(catch_payload.get("result")).get("ready"))
            catch_result = _mapping(catch_payload.get("result"))
            fish = _mapping(catch_result.get("fish"))
            pending_purchases = _items(
                self._state(identity).get("miniapp_fishing_pending_purchases")
            )
            if ready:
                self._append_round_summary(
                    identity,
                    record_id=str(
                        session.get("id")
                        or session.get("sessionId")
                        or session.get("castId")
                        or ""
                    ),
                    pond=str(self._state(identity).get("miniapp_fishing_pond") or "灵溪"),
                    bait=str(self._state(identity).get("miniapp_fishing_bait") or "鱼饵"),
                    chum=str(self._state(identity).get("miniapp_fishing_chum") or "不打窝"),
                    purchases=pending_purchases,
                    summary=summary,
                    caught=bool(catch_result.get("caught")),
                    weight=_number(fish.get("weight"), 0),
                    exp_gain=_integer(catch_result.get("expGain"), 0),
                    bonus_loot=_items(catch_result.get("bonusLoot")),
                )
            self._record(
                identity,
                miniapp_fishing_status="settled",
                miniapp_fishing_last_result=summary,
                miniapp_fishing_last_error="",
                miniapp_fishing_pending_purchases=([] if ready else pending_purchases),
                miniapp_fishing_next_run_time=(
                    datetime.now() + timedelta(seconds=3)
                ).strftime(TIME_FORMAT),
            )
            self._last_round_completed = ready
            return 3
        self._record(
            identity,
            miniapp_fishing_status="waiting",
            miniapp_fishing_last_error="",
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(seconds=15)
            ).strftime(TIME_FORMAT),
        )
        return 15

    @staticmethod
    def _seconds_since(value: Any) -> float:
        try:
            parsed = datetime.strptime(str(value or ""), TIME_FORMAT)
        except (TypeError, ValueError):
            return float("inf")
        return max(0.0, (datetime.now() - parsed).total_seconds())

    @staticmethod
    def _seconds_until(value: Any) -> int:
        try:
            parsed = datetime.strptime(str(value or ""), TIME_FORMAT)
        except (TypeError, ValueError):
            return 0
        return max(0, math.ceil((parsed - datetime.now()).total_seconds()))

    @classmethod
    def _scan_fresh(cls, scan: Any, seconds: int = FISHING_ROD_SCAN_SECONDS) -> bool:
        info = _mapping(scan)
        return bool(info.get("definitive")) and cls._seconds_since(info.get("updated_at")) <= seconds

    def _local_identity_keys(self, settings: dict[str, Any], runtime: dict[str, Any]) -> list[str]:
        available = list(ACCOUNT_IDENTITIES.get(self.account, ()))
        transport_ids = getattr(self.transport, "identity_player_ids", {}) or {}
        if isinstance(transport_ids, dict) and transport_ids:
            available = [
                identity
                for identity in available
                if identity in transport_ids or identity.casefold() in transport_ids
            ]
        allowed_keys = set(fishing_scan_keys(settings))
        transfer = _mapping(runtime.get("transfer"))
        priority_keys = [
            str(settings.get("rod_owner") or ""),
            str(runtime.get("rod_holder") or ""),
            str(runtime.get("current_key") or ""),
            str(transfer.get("from") or ""),
            str(transfer.get("to") or ""),
            *(str(item) for item in settings.get("participants") or []),
        ]
        result = []
        for key in priority_keys:
            account, identity = fishing_participant_parts(key)
            if (
                key in allowed_keys
                and account == self.account
                and identity in available
                and identity not in result
            ):
                result.append(identity)
        return result

    async def _scan_identity(
        self,
        identity: str,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        key = automation_participant_key(self.account, identity)
        info: dict[str, Any] = {
            "account": self.account,
            "identity": identity,
            "has_rod": False,
            "has_any_rod": False,
            "rod_name": "",
            "rod_matches": False,
            "phase": "",
            "active": False,
            "challenge": False,
            "definitive": False,
            "error": "",
            "updated_at": _now_text(),
        }
        try:
            _, payload = await self.transport.fishing_entry(identity)
            session = _mapping(payload.get("session"))
            rod = _mapping(session.get("rod"))
            phase = str(session.get("phase") or "").strip().lower()
            challenge = bool(_mapping(payload.get("challenge")))
            rod_name = str(rod.get("name") or "").strip()
            rod_matches = fishing_rod_matches(settings, rod_name)
            info.update(
                has_rod=rod_matches,
                has_any_rod=bool(rod),
                rod_name=rod_name,
                rod_matches=rod_matches,
                phase=phase,
                challenge=challenge,
                active=challenge or phase in {"waiting", "bite", "reeling"},
                definitive=True,
            )
        except MiniAppCircuitOpenError:
            raise
        except MiniAppBeastError as exc:
            info["error"] = exc.code
            info["definitive"] = exc.code == "fishing_rod_missing"
        except Exception as exc:
            info["error"] = type(exc).__name__.lower()

        def update(data: dict[str, Any]) -> None:
            if key not in set(fishing_scan_keys(settings)):
                data.setdefault("scans", {}).pop(key, None)
                return
            transfer = _mapping(data.get("transfer"))
            transfer_rod = str(transfer.get("rod_name") or "").strip()
            if transfer_rod and str(info.get("rod_name") or "") != transfer_rod:
                info["has_rod"] = False
                info["rod_matches"] = False
            scans = data.setdefault("scans", {})
            scans[key] = dict(info)
            if info.get("has_rod"):
                data["rod_holder"] = key
                data["rod_name"] = str(info.get("rod_name") or "")
                data["rod_holder_source"] = (
                    "manual" if str(settings.get("rod_owner") or "") == key else "auto"
                )
                data["rod_holder_verified_at"] = info["updated_at"]
                if (
                    transfer.get("status") == "purchase_failed"
                    and transfer.get("failure_code") == "missing_required_rod"
                    and transfer.get("listing_id")
                    and transfer.get("from") != key
                ):
                    transfer["from"] = key
                    transfer.setdefault("rod_name", str(info.get("rod_name") or ""))
                    transfer["status"] = "listed"
                    transfer["updated_at"] = info["updated_at"]
                    data["status"] = "transferring"
                    data["detail"] = (
                        f"已重新识别持竿者 {fishing_participant_label(key)}；"
                        f"继续购买原挂单 {transfer.get('listing_id')}"
                    )
                if transfer and transfer.get("to") == key:
                    completed_transfer = dict(transfer)
                    completed_transfer.update(
                        status="verified",
                        verified_at=info["updated_at"],
                    )
                    data["last_transfer"] = completed_transfer
                    data["transfer"] = {}
                    data["status"] = "ready"
                    data["detail"] = (
                        f"转竿后已由 Mini App 验证 {fishing_participant_label(key)} "
                        f"持有{info.get('rod_name')}"
                    )
            elif info.get("definitive") and str(data.get("rod_holder") or "") == key:
                data["rod_holder"] = ""
                data["rod_name"] = ""
                data["rod_holder_source"] = ""
                data["rod_holder_verified_at"] = ""

        _update_global_state(update, settings=settings)
        self._record(
            identity,
            miniapp_fishing_scan_has_rod=bool(info.get("has_rod")),
            miniapp_fishing_scan_rod_name=str(info.get("rod_name") or ""),
            miniapp_fishing_scan_phase=str(info.get("phase") or ""),
            miniapp_fishing_scan_error=str(info.get("error") or ""),
            miniapp_fishing_scan_time=info["updated_at"],
        )
        return info

    async def _scan_local(self, settings: dict[str, Any], force: bool = False) -> None:
        runtime = miniapp_fishing_global_snapshot(settings)
        scans = _mapping(runtime.get("scans"))
        for identity in self._local_identity_keys(settings, runtime):
            key = automation_participant_key(self.account, identity)
            if force or not self._scan_fresh(scans.get(key)):
                await self._scan_identity(identity, settings)
        self._scan_started = True

    def _set_global_status(
        self,
        settings: dict[str, Any],
        status: str,
        detail: str,
    ) -> dict[str, Any]:
        def update(data: dict[str, Any]) -> None:
            data["status"] = str(status or "waiting")
            data["detail"] = str(detail or "")[:500]

        return _update_global_state(update, settings=settings)

    def _schedule_transfer_retry(
        self,
        data: dict[str, Any],
        current: dict[str, Any],
        *,
        failed_status: str,
        detail: str,
        failure_code: str = "",
    ) -> None:
        next_retry_at = (
            datetime.now() + timedelta(seconds=FISHING_TRANSFER_FAILURE_RETRY_SECONDS)
        ).strftime(TIME_FORMAT)
        current["status"] = failed_status
        current["updated_at"] = _now_text()
        current["last_failure_at"] = _now_text()
        current["retry_count"] = max(0, _integer(current.get("retry_count"), 0)) + 1
        current["next_retry_at"] = next_retry_at
        if failure_code:
            current["failure_code"] = failure_code
        elif "failure_code" in current:
            current.pop("failure_code", None)
        data["status"] = "transfer_retry_wait"
        data["detail"] = (
            f"{detail}；已安排于 {next_retry_at} 自动补跑，之后每小时重试一次"
        )

    def _finalize_verified_transfer(
        self,
        settings: dict[str, Any],
        request_id: str,
        holder_key: str,
    ) -> bool:
        finalized = False

        def update(data: dict[str, Any]) -> None:
            nonlocal finalized
            current = _mapping(data.get("transfer"))
            if not current or str(current.get("id") or "") != request_id:
                return
            target_key = str(holder_key or current.get("to") or "").strip()
            if not target_key:
                return
            scan = _mapping(_mapping(data.get("scans")).get(target_key))
            transfer_rod = str(current.get("rod_name") or "").strip()
            if not scan.get("has_rod") or (
                transfer_rod and str(scan.get("rod_name") or "") != transfer_rod
            ):
                return
            completed_transfer = dict(current)
            completed_transfer.update(
                status="verified",
                verified_at=_now_text(),
            )
            data["last_transfer"] = completed_transfer
            data["transfer"] = {}
            data["status"] = "ready"
            data["rod_name"] = str(scan.get("rod_name") or transfer_rod)
            data["detail"] = (
                f"转竿后已由 Mini App 验证 {fishing_participant_label(target_key)} "
                f"持有{data['rod_name']}"
            )
            finalized = True

        _update_global_state(update, settings=settings)
        return finalized

    def _complete_round(
        self,
        settings: dict[str, Any],
        participant_key: str,
    ) -> dict[str, Any]:
        def update(data: dict[str, Any]) -> None:
            force = _mapping(data.get("force_retry"))
            if force.get("pending") and participant_key in force.get("pending", []):
                self._finish_force_retry_in_state(data, participant_key)
                return
            participants = [str(item) for item in data.get("participants") or []]
            data["last_round"] = {
                "participant": participant_key,
                "completed_at": _now_text(),
            }
            if participant_key in participants:
                # Keep the rod on this identity until the Mini App confirms its daily
                # cast limit. Rotating after every single catch creates dozens of
                # unnecessary market transfers and prevents one complete daily summary.
                data["current_key"] = participant_key
            data["status"] = "fishing"
            data["detail"] = (
                f"{fishing_participant_label(participant_key)} 本竿完成；"
                "继续垂钓至今日竿数耗尽"
            )

        return _update_global_state(update, settings=settings)

    @staticmethod
    def _finish_force_retry_in_state(
        data: dict[str, Any],
        participant_key: str,
        *,
        daily_done: bool = False,
    ) -> None:
        force = _mapping(data.get("force_retry"))
        pending = [str(item) for item in force.get("pending") or [] if str(item) != participant_key]
        attempted = [str(item) for item in force.get("attempted") or []]
        if participant_key not in attempted:
            attempted.append(participant_key)
        force["pending"] = pending
        force["attempted"] = attempted
        confirmed = _mapping(force.get("confirmed_daily_done"))
        if daily_done:
            confirmed[participant_key] = _now_text()
        force["confirmed_daily_done"] = confirmed
        if pending:
            data["force_retry"] = force
            data["current_key"] = pending[0]
            data["status"] = "force_retry"
            data["detail"] = (
                f"强制重试已完成 {fishing_participant_label(participant_key)}；"
                f"下一位 {fishing_participant_label(pending[0])}"
            )
            return
        restored = dict(_mapping(force.get("completed_before")))
        restored.update(confirmed)
        data["completed_today"] = restored
        finished = dict(force)
        finished["completed_at"] = _now_text()
        data["last_force_retry"] = finished
        data["force_retry"] = {}
        data["current_key"] = _next_participant(
            [str(item) for item in data.get("participants") or []],
            "",
            restored,
        )
        data["status"] = "ready" if data["current_key"] else "daily_done"
        data["detail"] = "强制重试已完成，所选身份均已尝试"

    def _mark_daily_done(
        self,
        settings: dict[str, Any],
        participant_key: str,
    ) -> dict[str, Any]:
        def update(data: dict[str, Any]) -> None:
            force = _mapping(data.get("force_retry"))
            if force.get("pending") and participant_key in force.get("pending", []):
                self._finish_force_retry_in_state(
                    data,
                    participant_key,
                    daily_done=True,
                )
                return
            completed = data.setdefault("completed_today", {})
            completed[participant_key] = _now_text()
            participants = [str(item) for item in data.get("participants") or []]
            data["current_key"] = _next_participant(
                participants,
                participant_key,
                completed,
            )
            if data["current_key"]:
                data["status"] = "ready"
                data["detail"] = (
                    f"{fishing_participant_label(participant_key)} 今日竿数已尽；"
                    f"下一位 {fishing_participant_label(data['current_key'])}"
                )
            else:
                data["status"] = "daily_done"
                data["detail"] = "所选身份今日垂钓均已完成"

        return _update_global_state(update, settings=settings)

    def _finish_force_retry_after_error(
        self,
        settings: dict[str, Any],
        participant_key: str,
    ) -> None:
        def update(data: dict[str, Any]) -> None:
            force = _mapping(data.get("force_retry"))
            if force.get("pending") and participant_key in force.get("pending", []):
                self._finish_force_retry_in_state(data, participant_key)

        _update_global_state(update, settings=settings)

    def _response_text(self, response: Any) -> str:
        resolver = getattr(self.actor, "fishing_response_text", None)
        if callable(resolver):
            return str(resolver(response) or "")
        if isinstance(response, str):
            return response
        return str(getattr(response, "text", "") or getattr(response, "raw_text", "") or "")

    async def _send_trade_command(self, identity: str, command: str) -> str:
        sender = getattr(self.actor, "send_fishing_command", None)
        if callable(sender):
            return self._response_text(await sender(identity, command, timeout=90))
        if identity == "主魂":
            sender = getattr(self.actor, "send_and_wait_feedback")
            response = await sender(command, timeout=90, max_retries=0, return_response_msg=True)
        else:
            sender = getattr(self.actor, "send_and_wait_feedback_identity")
            response = await sender(
                identity,
                command,
                timeout=90,
                max_retries=0,
                return_response_msg=True,
            )
        return self._response_text(response)

    async def _create_listing(
        self,
        settings: dict[str, Any],
        holder_key: str,
        target_key: str,
        *,
        reuse_request_id: str = "",
    ) -> bool:
        target_account, target_identity = fishing_participant_parts(target_key)
        if target_account != self.account:
            return False
        request_id = str(reuse_request_id or f"{int(time.time() * 1000)}-{os.getpid()}")
        started = False
        rod_name = ""

        def begin(data: dict[str, Any]) -> None:
            nonlocal rod_name, started
            current = _mapping(data.get("transfer"))
            configured_rod = configured_fishing_rod(settings)
            holder_scan = _mapping(_mapping(data.get("scans")).get(holder_key))
            rod_name = str(current.get("rod_name") or "").strip()
            if not rod_name and configured_rod != "auto":
                rod_name = configured_rod
            if not rod_name:
                rod_name = str(holder_scan.get("rod_name") or "").strip()
            if rod_name not in FISHING_ROD_ITEMS:
                data["status"] = "scanning"
                data["detail"] = (
                    f"尚未识别 {fishing_participant_label(holder_key)} 的鱼竿类型，"
                    "暂不创建换竿挂单"
                )
                return
            if current:
                if not reuse_request_id or str(current.get("id") or "") != request_id:
                    return
                current.update({
                    "status": "listing_sending",
                    "from": holder_key,
                    "to": target_key,
                    "rod_name": rod_name,
                    "listing_id": "",
                    "updated_at": _now_text(),
                })
                current.setdefault("started_at", _now_text())
                for key in (
                    "response",
                    "purchase_response",
                    "failure_code",
                    "next_retry_at",
                    "last_failure_at",
                ):
                    current.pop(key, None)
            else:
                data["transfer"] = {
                    "id": request_id,
                    "status": "listing_sending",
                    "from": holder_key,
                    "to": target_key,
                    "rod_name": rod_name,
                    "listing_id": "",
                    "started_at": _now_text(),
                    "updated_at": _now_text(),
                }
            data["status"] = "transferring"
            data["detail"] = (
                f"{fishing_participant_label(target_key)} 正在上架凝血草换取{rod_name}"
            )
            started = True

        _update_global_state(begin, settings=settings)
        if not started:
            return False
        response_text = await self._send_trade_command(
            target_identity,
            fishing_rod_listing_command(rod_name),
        )
        parsed = parse_trade_listing_response(response_text)
        if parsed.get("status") == "insufficient_resource":
            resolver = getattr(self.actor, "fishing_resolve_missing_resources", None)
            if callable(resolver) and await resolver(
                target_identity,
                parsed.get("missing_resources") or [],
            ):
                response_text = await self._send_trade_command(
                    target_identity,
                    fishing_rod_listing_command(rod_name),
                )
                parsed = parse_trade_listing_response(response_text)
        listing_id = str(parsed.get("listing_id") or "").strip()

        def finish(data: dict[str, Any]) -> None:
            transfer = _mapping(data.get("transfer"))
            if transfer.get("id") != request_id:
                return
            transfer["updated_at"] = _now_text()
            transfer["response"] = response_text[:800]
            if parsed.get("status") == "success" and listing_id:
                transfer["status"] = "listed"
                transfer["listing_id"] = listing_id
                transfer.pop("next_retry_at", None)
                data["status"] = "transferring"
                data["detail"] = (
                    f"挂单 {listing_id} 已生成，等待 "
                    f"{fishing_participant_label(holder_key)} 购买"
                )
            else:
                self._schedule_transfer_retry(
                    data,
                    transfer,
                    failed_status=("listing_failed" if response_text else "listing_unknown"),
                    detail=(
                        f"{fishing_participant_label(target_key)} 上架失败或未识别挂单ID"
                    ),
                )

        _update_global_state(finish, settings=settings)
        return bool(parsed.get("status") == "success" and listing_id)

    async def _purchase_listing(
        self,
        settings: dict[str, Any],
        transfer: dict[str, Any],
    ) -> bool:
        holder_key = str(transfer.get("from") or "")
        holder_account, holder_identity = fishing_participant_parts(holder_key)
        listing_id = str(transfer.get("listing_id") or "").strip()
        request_id = str(transfer.get("id") or "")
        if holder_account != self.account or not listing_id or not request_id:
            return False
        claimed = False

        def begin(data: dict[str, Any]) -> None:
            nonlocal claimed
            current = _mapping(data.get("transfer"))
            if current.get("id") != request_id or current.get("status") != "listed":
                return
            current["status"] = "purchase_sending"
            current["updated_at"] = _now_text()
            data["status"] = "transferring"
            data["detail"] = (
                f"{fishing_participant_label(holder_key)} 正在购买挂单 {listing_id}"
            )
            claimed = True

        _update_global_state(begin, settings=settings)
        if not claimed:
            return False
        response_text = await self._send_trade_command(
            holder_identity,
            f".购买 {listing_id}",
        )
        parsed = parse_trade_purchase_response(response_text)

        def finish(data: dict[str, Any]) -> None:
            current = _mapping(data.get("transfer"))
            if current.get("id") != request_id:
                return
            current["updated_at"] = _now_text()
            current["purchase_response"] = response_text[:800]
            if parsed.get("status") == "success":
                current["status"] = "purchased"
                current["purchased_at"] = _now_text()
                current.pop("next_retry_at", None)
                data["rod_holder"] = ""
                data["rod_holder_source"] = "transfer"
                data["rod_holder_verified_at"] = ""
                data["status"] = "verifying_transfer"
                data["detail"] = (
                    f"挂单 {listing_id} 已成交，等待 Mini App 验证 "
                    f"{fishing_participant_label(current.get('to'))} 持竿"
                )
            else:
                self._schedule_transfer_retry(
                    data,
                    current,
                    failed_status=("purchase_failed" if response_text else "purchase_unknown"),
                    detail=(
                        f"{fishing_participant_label(holder_key)} 购买挂单 {listing_id} 失败"
                    ),
                    failure_code=str(parsed.get("status") or "unrecognized"),
                )
                if parsed.get("status") == "missing_required_rod":
                    data["rod_holder"] = ""
                    data["rod_holder_verified_at"] = ""

        _update_global_state(finish, settings=settings)
        return parsed.get("status") == "success"

    async def _resume_failed_transfer(
        self,
        settings: dict[str, Any],
        transfer: dict[str, Any],
    ) -> int:
        request_id = str(transfer.get("id") or "").strip()
        if not request_id:
            return FISHING_TRANSFER_RETRY_SECONDS
        from_key = str(transfer.get("from") or "").strip()
        to_key = str(transfer.get("to") or "").strip()
        listing_id = str(transfer.get("listing_id") or "").strip()
        from_account, from_identity = fishing_participant_parts(from_key)
        to_account, to_identity = fishing_participant_parts(to_key)
        if from_account == self.account:
            await self._scan_identity(from_identity, settings)
        if to_account == self.account:
            await self._scan_identity(to_identity, settings)

        runtime = miniapp_fishing_global_snapshot(settings)
        current = _mapping(runtime.get("transfer"))
        if str(current.get("id") or "") != request_id:
            return 2
        holder_key = str(runtime.get("rod_holder") or current.get("from") or "").strip()
        target_key = str(current.get("to") or to_key).strip()
        if holder_key == target_key and target_key:
            self._finalize_verified_transfer(settings, request_id, target_key)
            return 2
        if holder_key and holder_key != from_key:
            def rewrite_holder(data: dict[str, Any]) -> None:
                update_transfer = _mapping(data.get("transfer"))
                if str(update_transfer.get("id") or "") != request_id:
                    return
                update_transfer["from"] = holder_key
                update_transfer["updated_at"] = _now_text()

            _update_global_state(rewrite_holder, settings=settings)
            current = _mapping(miniapp_fishing_global_snapshot(settings).get("transfer"))
            from_key = str(current.get("from") or holder_key).strip()
            from_account, from_identity = fishing_participant_parts(from_key)
            listing_id = str(current.get("listing_id") or listing_id).strip()
        if holder_key == from_key and listing_id and from_account == self.account:
            def rearm_purchase(data: dict[str, Any]) -> None:
                update_transfer = _mapping(data.get("transfer"))
                if str(update_transfer.get("id") or "") != request_id:
                    return
                update_transfer["from"] = from_key
                update_transfer["status"] = "listed"
                update_transfer["updated_at"] = _now_text()
                update_transfer.pop("next_retry_at", None)
                data["status"] = "transferring"
                data["detail"] = f"补跑中：继续购买挂单 {listing_id}"

            _update_global_state(rearm_purchase, settings=settings)
            await self._purchase_listing(
                settings,
                {
                    **current,
                    "status": "listed",
                    "from": from_key,
                },
            )
            return 5
        if holder_key == from_key and to_account == self.account:
            await self._create_listing(
                settings,
                from_key,
                target_key,
                reuse_request_id=request_id,
            )
            return 5

        def reschedule(data: dict[str, Any]) -> None:
            update_transfer = _mapping(data.get("transfer"))
            if str(update_transfer.get("id") or "") != request_id:
                return
            self._schedule_transfer_retry(
                data,
                update_transfer,
                failed_status=str(update_transfer.get("status") or "transfer_failed"),
                detail="转竿恢复时仍未确认鱼竿位置",
                failure_code=str(update_transfer.get("failure_code") or ""),
            )

        _update_global_state(reschedule, settings=settings)
        return min(FISHING_TRANSFER_FAILURE_RETRY_SECONDS, 300)

    async def _handle_transfer(
        self,
        settings: dict[str, Any],
        transfer: dict[str, Any],
    ) -> int:
        status = str(transfer.get("status") or "")
        from_account, _ = fishing_participant_parts(transfer.get("from"))
        to_account, to_identity = fishing_participant_parts(transfer.get("to"))
        if status == "listed" and from_account == self.account:
            await self._purchase_listing(settings, transfer)
            return 5
        if status == "purchased" and to_account == self.account:
            info = await self._scan_identity(to_identity, settings)
            if info.get("has_rod"):
                return 2
            self._set_global_status(
                settings,
                "verifying_transfer",
                f"等待 {fishing_participant_label(transfer.get('to'))} 的鱼竿到账",
            )
            return 5
        if status in FISHING_TRANSFER_FAILURE_STATUSES:
            retry_at = str(transfer.get("next_retry_at") or "").strip()
            remaining = self._seconds_until(retry_at)
            if remaining > 0:
                self._set_global_status(
                    settings,
                    "transfer_retry_wait",
                    f"转竿异常，已安排于 {retry_at} 自动补跑",
                )
                return min(300, max(5, remaining))
            return await self._resume_failed_transfer(settings, transfer)
        if status in {"listing_sending", "purchase_sending"} and self._seconds_since(
            transfer.get("updated_at")
        ) > 180:
            def mark_unknown(data: dict[str, Any]) -> None:
                current = _mapping(data.get("transfer"))
                if current.get("id") != transfer.get("id"):
                    return
                self._schedule_transfer_retry(
                    data,
                    current,
                    failed_status=(
                        "listing_unknown" if status == "listing_sending" else "purchase_unknown"
                    ),
                    detail="转竿操作结果未知",
                    failure_code=str(current.get("failure_code") or ""),
                )

            _update_global_state(mark_unknown, settings=settings)
        return 5

    async def _run_holder_cycle(
        self,
        settings: dict[str, Any],
        participant_key: str,
    ) -> int:
        account, identity = fishing_participant_parts(participant_key)
        if account != self.account:
            return 5
        pause_seconds = 0
        resolver = getattr(self.actor, "identity_pause_seconds", None)
        if callable(resolver):
            pause_seconds = max(0, _integer(resolver(identity), 0))
        if pause_seconds > 0:
            self._set_global_status(
                settings,
                "identity_paused",
                f"{fishing_participant_label(participant_key)} 暂停中",
            )
            return max(15, pause_seconds)
        wait = await self.run_cycle(settings, identity=identity)
        if self._last_round_completed:
            self._complete_round(settings, participant_key)
        return wait

    async def _drive_once(self, settings: dict[str, Any]) -> int:
        await self._scan_local(settings, force=not self._scan_started)
        runtime = miniapp_fishing_global_snapshot(settings)
        transfer = _mapping(runtime.get("transfer"))
        scans = _mapping(runtime.get("scans"))

        active_key = str(runtime.get("rod_holder") or transfer.get("from") or "")
        active_scan = _mapping(scans.get(active_key))
        if active_key and self._scan_fresh(active_scan, 90) and active_scan.get("active"):
            active_account, _ = fishing_participant_parts(active_key)
            self._set_global_status(
                settings,
                "active_round",
                f"{fishing_participant_label(active_key)} 尚有鱼讯，先完成收线再转竿",
            )
            if active_account == self.account:
                return await self._run_holder_cycle(settings, active_key)
            return 5

        if transfer:
            return await self._handle_transfer(settings, transfer)

        holder_key = str(runtime.get("rod_holder") or "")
        if not holder_key:
            expected = fishing_scan_keys(settings)
            all_scanned = all(self._scan_fresh(scans.get(key)) for key in expected)
            configured_rod = configured_fishing_rod(settings)
            detected_rods = sorted(
                {
                    str(_mapping(scan).get("rod_name") or "").strip()
                    for scan in scans.values()
                    if str(_mapping(scan).get("rod_name") or "").strip()
                }
            )
            if configured_rod == "auto":
                missing_detail = "未在全部账号身份中找到支持的鱼竿"
            else:
                missing_detail = f"未找到所选鱼竿{configured_rod}"
                if detected_rods:
                    missing_detail += f"；已识别：{'、'.join(detected_rods)}"
            self._set_global_status(
                settings,
                "no_rod" if all_scanned else "scanning",
                missing_detail if all_scanned else "正在扫描鱼竿所在身份及类型",
            )
            force = _mapping(runtime.get("force_retry"))
            target_key = str(runtime.get("current_key") or "")
            if all_scanned and target_key in (force.get("pending") or []):
                self._finish_force_retry_after_error(settings, target_key)
                return 1
            return 300 if all_scanned else 5

        holder_scan = _mapping(scans.get(holder_key))
        if not self._scan_fresh(holder_scan, 90):
            holder_account, holder_identity = fishing_participant_parts(holder_key)
            if holder_account == self.account:
                await self._scan_identity(holder_identity, settings)
            self._set_global_status(
                settings,
                "scanning",
                f"正在复核 {fishing_participant_label(holder_key)} 的持竿状态",
            )
            return 5

        target_key = str(runtime.get("current_key") or "")
        if not target_key:
            return 300
        if holder_key != target_key:
            target_account, _ = fishing_participant_parts(target_key)
            if target_account == self.account:
                await self._create_listing(settings, holder_key, target_key)
                return 5
            self._set_global_status(
                settings,
                "waiting_transfer",
                f"等待 {fishing_participant_label(target_key)} 创建换竿挂单",
            )
            return 5

        holder_account, _ = fishing_participant_parts(holder_key)
        if holder_account == self.account:
            self._set_global_status(
                settings,
                "fishing",
                f"当前由 {fishing_participant_label(holder_key)} 自动垂钓",
            )
            return await self._run_holder_cycle(settings, holder_key)
        return 5

    async def run_loop(self) -> None:
        if not self.supported:
            return
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        while getattr(self.actor, "is_running", True):
            wait = self.retry_seconds
            settings = self.settings()
            try:
                self._clear_irrelevant_local_statuses(settings)
                force_retry = self._apply_force_retry_request(settings)
                if not settings.get("enabled"):
                    self._record(
                        self._status_identity(settings),
                        miniapp_fishing_status="paused",
                        miniapp_fishing_last_error="",
                        miniapp_fishing_next_run_time="",
                    )
                    self._set_global_status(settings, "paused", "灵溪自动垂钓已暂停")
                    wait = DEFAULT_DISABLED_SECONDS
                elif not self._local_relevant_identities(settings):
                    miniapp_fishing_global_snapshot(settings)
                    self._scan_started = False
                    wait = DEFAULT_DISABLED_SECONDS
                else:
                    pause = getattr(self.actor, "pause_event", None)
                    if pause is not None:
                        await pause.wait()
                    start_wait, start_at = fishing_start_wait(settings.get("start_time"))
                    if force_retry.get("pending"):
                        start_wait, start_at = 0, ""
                    if start_wait > 0:
                        identity = self._status_identity(settings)
                        self._record(
                            identity,
                            miniapp_fishing_status="waiting_start",
                            miniapp_fishing_last_error="",
                            miniapp_fishing_next_run_time=start_at,
                        )
                        self._set_global_status(
                            settings,
                            "waiting_start",
                            f"等待 {str(settings.get('start_time') or '')} 开始自动垂钓",
                        )
                        wait = min(start_wait, 300)
                    else:
                        circuit_error = miniapp_circuit_preflight(
                            getattr(self.transport, "origin", "")
                        )
                        if circuit_error is not None:
                            raise circuit_error
                        wait = await self._drive_once(settings)
                self._global_state_busy_failures = 0
            except asyncio.CancelledError:
                raise
            except MiniAppCircuitOpenError as exc:
                identity = self._status_identity(settings)
                wait = miniapp_circuit_wait_seconds(exc, self.retry_seconds)
                self._record(
                    identity,
                    miniapp_fishing_status="paused_upstream",
                    miniapp_fishing_last_error=exc.code,
                    miniapp_fishing_last_error_time=_now_text(),
                    miniapp_fishing_next_run_time=(
                        datetime.now() + timedelta(seconds=wait)
                    ).strftime(TIME_FORMAT),
                )
                try:
                    self._set_global_status(
                        settings,
                        "paused_upstream",
                        f"Mini App 上游熔断，等待至 {exc.retry_at or '下一次探测'}",
                    )
                except MiniAppFishingGlobalStateBusy:
                    # The per-account pause is already persisted. Do not let an
                    # unrelated ledger writer turn an upstream pause into a
                    # traceback or an immediate retry.
                    pass
                wait = max(wait, 60)
                await self._sleep_until_next_cycle(wait, settings, uncapped=True)
                continue
            except MiniAppFishingGlobalStateBusy:
                identity = self._status_identity(settings)
                self._global_state_busy_failures += 1
                level = min(
                    self._global_state_busy_failures,
                    len(FISHING_GLOBAL_LOCK_RETRY_SECONDS),
                )
                wait = FISHING_GLOBAL_LOCK_RETRY_SECONDS[level - 1]
                self._record(
                    identity,
                    miniapp_fishing_status="paused_state",
                    miniapp_fishing_last_error="miniapp_fishing_global_lock_timeout",
                    miniapp_fishing_last_error_time=_now_text(),
                    miniapp_fishing_next_run_time=(
                        datetime.now() + timedelta(seconds=wait)
                    ).strftime(TIME_FORMAT),
                )
                if self._global_state_busy_failures <= len(FISHING_GLOBAL_LOCK_RETRY_SECONDS):
                    self.log.warning(
                        "Mini App fishing shared state stayed busy for %s; "
                        "pausing this worker for %ss (level %s).",
                        identity,
                        wait,
                        level,
                    )
                await self._sleep_until_next_cycle(wait, settings, uncapped=True)
                continue
            except Exception as exc:
                identity = self._status_identity(settings)
                code = (
                    exc.code
                    if isinstance(exc, MiniAppBeastError)
                    else type(exc).__name__.lower()
                )
                participant_key = automation_participant_key(self.account, identity)
                try:
                    force_state = _mapping(
                        miniapp_fishing_global_snapshot(settings).get("force_retry")
                    )
                except MiniAppFishingGlobalStateBusy:
                    force_state = _mapping(_load_global_state().get("force_retry"))
                force_retry_active = participant_key in (force_state.get("pending") or [])
                previous_status = str(
                    self._state(identity).get("miniapp_fishing_status") or ""
                )
                summary_emitted = False
                if code == "fishing_daily_limit_reached":
                    status = "daily_done"
                    wait = 5
                    summary_emitted = self._emit_daily_summary(
                        identity,
                        daily_limit_reached=True,
                    )
                    self._mark_daily_done(settings, participant_key)
                elif code in {"fishing_rod_missing", "fishing_rod_type_mismatch"}:
                    status = "no_rod"
                    wait = 5

                    def clear_holder(data: dict[str, Any]) -> None:
                        if str(data.get("rod_holder") or "") == participant_key:
                            data["rod_holder"] = ""
                            data["rod_name"] = ""
                            data["rod_holder_source"] = ""
                            data["rod_holder_verified_at"] = ""
                        scan = data.setdefault("scans", {}).setdefault(participant_key, {})
                        scan.update(
                            has_rod=False,
                            definitive=True,
                            error=code,
                            updated_at=_now_text(),
                        )

                    _update_global_state(clear_holder, settings=settings)
                    self._scan_started = False
                elif code in {"fishing_auth_refreshed", "hash_mismatch", "auth_date_expired"}:
                    status = "auth_refresh"
                    wait = 5
                    self._set_global_status(settings, status, "Mini App 授权刷新中")
                elif code == "fishing_shop_cost_missing":
                    # The fishing API can temporarily publish a shop item without
                    # its cost metadata.  Treat it as a recoverable service-side
                    # condition and retry hourly instead of flooding the log.
                    status = "shop_unavailable"
                    wait = FISHING_SHOP_RETRY_SECONDS
                    self._set_global_status(
                        settings,
                        status,
                        "鱼饵商店价格数据暂不可用，1小时后自动重试",
                    )
                elif code == "fishing_bait_unaffordable":
                    status = "waiting_resources"
                    wait = FISHING_SHOP_RETRY_SECONDS
                    self._set_global_status(
                        settings,
                        status,
                        "鱼饵材料不足，1小时后自动重试",
                    )
                elif code == "fishing_pond_locked":
                    status = "waiting_pond"
                    wait = FISHING_SHOP_RETRY_SECONDS
                    self._set_global_status(
                        settings,
                        status,
                        "配置鱼塘暂未解锁，1小时后自动重试",
                    )
                else:
                    status = "error"
                    wait = self.retry_seconds
                    self._set_global_status(settings, status, f"{self.account}：{code}")
                self._record(
                    identity,
                    miniapp_fishing_status=status,
                    miniapp_fishing_last_error=code,
                    miniapp_fishing_last_error_time=_now_text(),
                    miniapp_fishing_next_run_time=(
                        datetime.now() + timedelta(seconds=wait)
                    ).strftime(TIME_FORMAT),
                )
                if status == "daily_done" and previous_status != status and not summary_emitted:
                    self.log.info(
                        "Mini App fishing daily limit reached for %s; advancing participant.",
                        identity,
                    )
                elif status == "no_rod" and previous_status != status:
                    self.log.warning(
                        "Mini App fishing rod cache invalidated for %s; rescanning.",
                        identity,
                    )
                elif status == "auth_refresh" and previous_status != status:
                    self.log.info("Mini App fishing authorization refreshed; retrying shortly.")
                elif status == "error":
                    self.log.error("Mini App fishing loop failed: %s", code, exc_info=True)
                elif status == "shop_unavailable" and previous_status != status:
                    self.log.warning(
                        "Mini App fishing shop cost metadata unavailable for %s; retrying hourly.",
                        identity,
                    )
                elif status == "waiting_resources" and previous_status != status:
                    self.log.warning(
                        "Mini App fishing bait materials unavailable for %s; retrying hourly.",
                        identity,
                    )
                elif status == "waiting_pond" and previous_status != status:
                    self.log.warning(
                        "Mini App fishing configured pond is locked for %s; retrying hourly.",
                        identity,
                    )
                if force_retry_active and status not in {"daily_done"}:
                    self._finish_force_retry_after_error(settings, participant_key)
                    wait = 1
            await self._sleep_until_next_cycle(wait, settings)
