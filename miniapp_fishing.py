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
    MINIAPP_FISHING_SUPPORTED_ACCOUNTS,
    automation_participant_key,
    miniapp_fishing_settings,
)
from fishing_features import parse_trade_listing_response, parse_trade_purchase_response
from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import identity_state


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_RETRY_SECONDS = 60
DEFAULT_DISABLED_SECONDS = 30
DEFAULT_RESULT_ATTEMPTS = 18
BAIT_PURCHASE_QUANTITY = 10
FISHING_ROD_ITEM = "银竹钓竿"
FISHING_ROD_LISTING_MATERIAL = "凝血草"
FISHING_ROD_LISTING_COMMAND = f".上架 {FISHING_ROD_LISTING_MATERIAL} 换 {FISHING_ROD_ITEM}1"
FISHING_ROD_SCAN_SECONDS = 300
FISHING_TRANSFER_RETRY_SECONDS = 60
MINIAPP_FISHING_GLOBAL_FILE = Path(__file__).resolve().parent / "miniapp_fishing_global.json"


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
    if identity not in ACCOUNT_IDENTITIES.get(account, ()):
        return "", ""
    return account, identity


def fishing_participant_label(value: Any) -> str:
    account, identity = fishing_participant_parts(value)
    if not account:
        return ""
    return f"{ACCOUNT_NAMES.get(account, account)}｜{identity}"


def _global_default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "date": _today_text(),
        "participants": [],
        "current_key": "",
        "rod_holder": "",
        "rod_holder_source": "",
        "rod_holder_verified_at": "",
        "scans": {},
        "completed_today": {},
        "transfer": {},
        "last_transfer": {},
        "last_round": {},
        "status": "scanning",
        "detail": "等待扫描鱼竿",
        "updated_at": _now_text(),
    }


def _load_global_state() -> dict[str, Any]:
    try:
        with MINIAPP_FISHING_GLOBAL_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError, TypeError):
        data = {}
    defaults = _global_default_state()
    for key, value in defaults.items():
        data.setdefault(key, value)
    if str(data.get("date") or "") != _today_text():
        data["date"] = _today_text()
        data["completed_today"] = {}
        data["last_round"] = {}
        if not _mapping(data.get("transfer")):
            data["current_key"] = ""
    for key in ("scans", "completed_today", "transfer", "last_transfer", "last_round"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    if not isinstance(data.get("participants"), list):
        data["participants"] = []
    return data


def _save_global_state(data: dict[str, Any]) -> None:
    path = MINIAPP_FISHING_GLOBAL_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    data["date"] = _today_text()
    data["updated_at"] = _now_text()
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _acquire_global_lock(timeout: float = 5.0) -> tuple[int | None, Path]:
    lock_path = MINIAPP_FISHING_GLOBAL_FILE.with_name(
        f"{MINIAPP_FISHING_GLOBAL_FILE.name}.lock"
    )
    deadline = time.monotonic() + max(0.5, float(timeout or 0.5))
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, str(os.getpid()).encode("ascii", errors="ignore"))
            return descriptor, lock_path
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return None, lock_path
            time.sleep(0.05)


def _release_global_lock(lock: tuple[int | None, Path]) -> None:
    descriptor, lock_path = lock
    try:
        if descriptor is not None:
            os.close(descriptor)
    except OSError:
        pass
    try:
        if descriptor is not None:
            lock_path.unlink()
    except OSError:
        pass


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
    participants = [
        str(item).strip()
        for item in settings.get("participants") or []
        if fishing_participant_parts(item) != ("", "")
    ]
    participants = list(dict.fromkeys(participants))
    old_participants = [str(item) for item in data.get("participants") or []]
    data["participants"] = participants
    completed = _mapping(data.get("completed_today"))
    data["completed_today"] = {
        key: value for key, value in completed.items() if key in participants
    }
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
    lock = _acquire_global_lock()
    if lock[0] is None:
        raise RuntimeError("miniapp_fishing_global_lock_timeout")
    try:
        data = _load_global_state()
        if settings is not None:
            _reconcile_global_state(data, settings)
        if callable(updater):
            updater(data)
        _save_global_state(data)
        return data
    finally:
        _release_global_lock(lock)


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
    transfer = _mapping(data.get("transfer"))
    result["transfer_from_label"] = fishing_participant_label(transfer.get("from"))
    result["transfer_to_label"] = fishing_participant_label(transfer.get("to"))
    result["participant_labels"] = [
        fishing_participant_label(item) for item in data.get("participants") or []
    ]
    return result


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


def fishing_rounds_summary(records: Any) -> str:
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
        title = f"灵溪垂钓汇总（{pond} · {bait} · {chum}，共 {len(rounds)} 竿）"
    else:
        title = f"灵溪垂钓汇总（共 {len(rounds)} 竿）"

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

    def _record(self, identity: str | None = None, **updates: Any) -> None:
        state = self._state(identity)
        state.update(updates)
        state["miniapp_fishing_account"] = self.account
        state["miniapp_fishing_identity"] = str(identity or self._current_identity or "主魂")
        state["miniapp_fishing_updated_at"] = _now_text()
        self._save()

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
        if normalized_id and any(str(item.get("id") or "") == normalized_id for item in records):
            return
        records.append(
            {
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
        )
        records = records[-50:]
        self._record(
            identity,
            miniapp_fishing_summary_date=today,
            miniapp_fishing_round_records=records,
            miniapp_fishing_round_count_today=len(records),
            miniapp_fishing_summary_emitted_count=min(emitted_count, len(records)),
        )

    def _emit_daily_summary(self, identity: str) -> bool:
        state = self._state(identity)
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
        pending = records[emitted_count:]
        summary_text = fishing_rounds_summary(pending)
        if not summary_text:
            return False
        self.log.info("IN [Mini App | %s]:\n%s", identity, summary_text)
        self._record(
            identity,
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
        quantity = BAIT_PURCHASE_QUANTITY
        bought = await self.transport.fishing_buy_bait(
            identity,
            token,
            str(bait.get("key") or bait_key),
            quantity,
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
            shop, _ = await self._ensure_bait(
                identity,
                token,
                shop,
                str(bait.get("key") or ""),
                required,
            )
        refreshed = fishing_shop(
            await self.transport.fishing_shop(identity, token)
        ) or shop
        self._record_shop(identity, refreshed)
        chum = fishing_option(refreshed.get("chums"), chum_key)
        if not chum.get("affordable"):
            raise MiniAppBeastError("fishing_chum_unaffordable")
        try:
            applied = await self.transport.fishing_apply_chum(
                identity,
                token,
                chum_key,
                log_operation=False,
            )
        except MiniAppBeastError as exc:
            if exc.code == "fishing_chum_daily_limit":
                return self._use_no_chum_after_daily_limit(identity, refreshed, chum)
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
        """Treat an exhausted configured chum as a normal same-day fallback."""
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
            miniapp_fishing_chum_fallback_reason="daily_limit",
            miniapp_fishing_chum_fallback_detail=(
                f"{chum_name}今日打窝次数已尽；本日后续继续不打窝"
            ),
        )
        if not already_recorded:
            logger = getattr(self.log, "info", None)
            if callable(logger):
                logger(
                    "Mini App fishing chum daily limit reached for %s (%s); "
                    "continuing without chum.",
                    identity,
                    chum_name,
                )
        return shop

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
        pond = fishing_option(shop.get("ponds"), pond_key)
        if not pond:
            raise MiniAppBeastError("fishing_pond_invalid")
        if not pond.get("unlocked"):
            raise MiniAppBeastError("fishing_pond_locked")
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
        if rod:
            self._record(
                identity,
                miniapp_fishing_rod=str(rod.get("name") or ""),
                miniapp_fishing_rod_item_id=str(rod.get("itemId") or ""),
            )
        if challenge:
            return await self._finish_challenge(identity, token, session, challenge)
        phase = str(session.get("phase") or "").strip().lower()
        if phase == "lobby":
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
        priority_keys = [
            str(settings.get("rod_owner") or ""),
            str(runtime.get("rod_holder") or ""),
            str(runtime.get("current_key") or ""),
            *(str(item) for item in settings.get("participants") or []),
        ]
        result = []
        for key in priority_keys:
            account, identity = fishing_participant_parts(key)
            if account == self.account and identity in available and identity not in result:
                result.append(identity)
        result.extend(identity for identity in available if identity not in result)
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
            "rod_name": "",
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
            info.update(
                has_rod=bool(rod),
                rod_name=str(rod.get("name") or ""),
                phase=phase,
                challenge=challenge,
                active=challenge or phase in {"waiting", "bite", "reeling"},
                definitive=True,
            )
        except MiniAppBeastError as exc:
            info["error"] = exc.code
            info["definitive"] = exc.code == "fishing_rod_missing"
        except Exception as exc:
            info["error"] = type(exc).__name__.lower()

        def update(data: dict[str, Any]) -> None:
            scans = data.setdefault("scans", {})
            scans[key] = dict(info)
            transfer = _mapping(data.get("transfer"))
            if info.get("has_rod"):
                data["rod_holder"] = key
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
                    transfer["status"] = "listed"
                    transfer["updated_at"] = info["updated_at"]
                    data["status"] = "transferring"
                    data["detail"] = (
                        f"已重新识别持竿者 {fishing_participant_label(key)}；"
                        f"继续购买原挂单 {transfer.get('listing_id')}"
                    )
                if transfer.get("status") == "purchased" and transfer.get("to") == key:
                    completed_transfer = dict(transfer)
                    completed_transfer.update(
                        status="verified",
                        verified_at=info["updated_at"],
                    )
                    data["last_transfer"] = completed_transfer
                    data["transfer"] = {}
                    data["status"] = "ready"
                    data["detail"] = f"转竿后已由 Mini App 验证 {fishing_participant_label(key)} 持竿"
            elif info.get("definitive") and str(data.get("rod_holder") or "") == key:
                data["rod_holder"] = ""
                data["rod_holder_source"] = ""
                data["rod_holder_verified_at"] = ""

        _update_global_state(update, settings=settings)
        self._record(
            identity,
            miniapp_fishing_scan_has_rod=bool(info.get("has_rod")),
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

    def _complete_round(
        self,
        settings: dict[str, Any],
        participant_key: str,
    ) -> dict[str, Any]:
        def update(data: dict[str, Any]) -> None:
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

    def _mark_daily_done(
        self,
        settings: dict[str, Any],
        participant_key: str,
    ) -> dict[str, Any]:
        def update(data: dict[str, Any]) -> None:
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
    ) -> bool:
        target_account, target_identity = fishing_participant_parts(target_key)
        if target_account != self.account:
            return False
        request_id = f"{int(time.time() * 1000)}-{os.getpid()}"
        started = False

        def begin(data: dict[str, Any]) -> None:
            nonlocal started
            if _mapping(data.get("transfer")):
                return
            data["transfer"] = {
                "id": request_id,
                "status": "listing_sending",
                "from": holder_key,
                "to": target_key,
                "listing_id": "",
                "started_at": _now_text(),
                "updated_at": _now_text(),
            }
            data["status"] = "transferring"
            data["detail"] = (
                f"{fishing_participant_label(target_key)} 正在上架凝血草换取银竹钓竿"
            )
            started = True

        _update_global_state(begin, settings=settings)
        if not started:
            return False
        response_text = await self._send_trade_command(
            target_identity,
            FISHING_ROD_LISTING_COMMAND,
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
                    FISHING_ROD_LISTING_COMMAND,
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
                data["status"] = "transferring"
                data["detail"] = (
                    f"挂单 {listing_id} 已生成，等待 "
                    f"{fishing_participant_label(holder_key)} 购买"
                )
            else:
                transfer["status"] = "listing_failed" if response_text else "listing_unknown"
                data["status"] = "transfer_failed"
                data["detail"] = (
                    f"{fishing_participant_label(target_key)} 上架失败或未识别挂单ID；"
                    "为避免重复挂单已停止自动重试"
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
                data["rod_holder"] = ""
                data["rod_holder_source"] = "transfer"
                data["rod_holder_verified_at"] = ""
                data["status"] = "verifying_transfer"
                data["detail"] = (
                    f"挂单 {listing_id} 已成交，等待 Mini App 验证 "
                    f"{fishing_participant_label(current.get('to'))} 持竿"
                )
            else:
                current["status"] = "purchase_failed" if response_text else "purchase_unknown"
                current["failure_code"] = str(parsed.get("status") or "unrecognized")
                data["status"] = "transfer_failed"
                data["detail"] = (
                    f"{fishing_participant_label(holder_key)} 购买挂单 {listing_id} 失败；"
                    "为避免错误转移已停止自动重试"
                )
                if parsed.get("status") == "missing_required_rod":
                    data["rod_holder"] = ""
                    data["rod_holder_verified_at"] = ""

        _update_global_state(finish, settings=settings)
        return parsed.get("status") == "success"

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
        if status in {
            "listing_failed",
            "listing_unknown",
            "purchase_failed",
            "purchase_unknown",
        }:
            return FISHING_TRANSFER_RETRY_SECONDS
        if status in {"listing_sending", "purchase_sending"} and self._seconds_since(
            transfer.get("updated_at")
        ) > 180:
            def mark_unknown(data: dict[str, Any]) -> None:
                current = _mapping(data.get("transfer"))
                if current.get("id") != transfer.get("id"):
                    return
                current["status"] = (
                    "listing_unknown" if status == "listing_sending" else "purchase_unknown"
                )
                current["updated_at"] = _now_text()
                data["status"] = "transfer_failed"
                data["detail"] = "转竿操作结果未知；为避免重复交易已停止自动重试"

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
            expected = [
                automation_participant_key(account, identity)
                for account in MINIAPP_FISHING_SUPPORTED_ACCOUNTS
                for identity in ACCOUNT_IDENTITIES.get(account, ())
            ]
            all_scanned = all(self._scan_fresh(scans.get(key)) for key in expected)
            self._set_global_status(
                settings,
                "no_rod" if all_scanned else "scanning",
                "未在主号/副号身份中找到鱼竿" if all_scanned else "正在扫描鱼竿所在身份",
            )
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
                if not settings.get("enabled"):
                    self._record(
                        "主魂",
                        miniapp_fishing_status="paused",
                        miniapp_fishing_last_error="",
                        miniapp_fishing_next_run_time="",
                    )
                    self._set_global_status(settings, "paused", "灵溪自动垂钓已暂停")
                    wait = DEFAULT_DISABLED_SECONDS
                else:
                    pause = getattr(self.actor, "pause_event", None)
                    if pause is not None:
                        await pause.wait()
                    start_wait, start_at = fishing_start_wait(settings.get("start_time"))
                    if start_wait > 0:
                        identity = str(self._current_identity or "主魂")
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
                        wait = await self._drive_once(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                identity = str(self._current_identity or "主魂")
                participant_key = automation_participant_key(self.account, identity)
                previous_status = str(
                    self._state(identity).get("miniapp_fishing_status") or ""
                )
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                summary_emitted = False
                if code == "fishing_daily_limit_reached":
                    status = "daily_done"
                    wait = 5
                    summary_emitted = self._emit_daily_summary(identity)
                    self._mark_daily_done(settings, participant_key)
                elif code == "fishing_rod_missing":
                    status = "no_rod"
                    wait = 5

                    def clear_holder(data: dict[str, Any]) -> None:
                        if str(data.get("rod_holder") or "") == participant_key:
                            data["rod_holder"] = ""
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
            await asyncio.sleep(max(1, min(int(wait), 300)))
