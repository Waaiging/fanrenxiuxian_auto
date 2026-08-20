#!/usr/bin/env python3
"""Shared Dashboard-controlled automation settings."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from state_io import load_json_state


CONFIG_DIR = Path(__file__).resolve().parent
AUTOMATION_SETTINGS_FILE = CONFIG_DIR / "automation_settings.json"
SUB_STATE_FILE = CONFIG_DIR / "state_sub.json"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_SUB_IDENTITY_STATE_CACHE: dict[str, Any] = {
    "signature": None,
    "state": {},
}
_SUB_IDENTITY_STATE_LOCK = threading.Lock()

ACCOUNT_NAMES = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}
DEFAULT_SUB_YINLUO_IDENTITY = "竹和生"
SUB_YINLUO_PLAYER_ID = "-1003885521329"


def _load_sub_identity_state() -> dict[str, Any]:
    with _SUB_IDENTITY_STATE_LOCK:
        try:
            stat = SUB_STATE_FILE.stat()
            signature = (
                str(SUB_STATE_FILE),
                int(getattr(stat, "st_mtime_ns", 0) or 0),
                int(stat.st_size),
            )
        except OSError:
            signature = (str(SUB_STATE_FILE), None, None)

        if _SUB_IDENTITY_STATE_CACHE.get("signature") == signature:
            cached = _SUB_IDENTITY_STATE_CACHE.get("state")
            return cached if isinstance(cached, dict) else {}

        state = load_json_state(
            str(SUB_STATE_FILE),
            expected_type=dict,
            default={},
        ) or {}
        _SUB_IDENTITY_STATE_CACHE["signature"] = signature
        _SUB_IDENTITY_STATE_CACHE["state"] = state
        return state


def current_sub_yinluo_identity(state: Any = None) -> str:
    """Return the current Dao name for the stable sub-account Yinluo avatar."""
    source = state if isinstance(state, dict) else _load_sub_identity_state()
    player_names = source.get("avatar_dao_names_by_player_id")
    if not isinstance(player_names, dict):
        player_names = source.get("avatar_dao_names_by_tgid")
    if isinstance(player_names, dict):
        current = str(player_names.get(SUB_YINLUO_PLAYER_ID) or "").strip()
        if current and current != "一缕残魂":
            return current

    aliases = source.get("avatar_dao_name_aliases")
    current = DEFAULT_SUB_YINLUO_IDENTITY
    seen = set()
    while isinstance(aliases, dict) and current not in seen:
        seen.add(current)
        mapped = str(aliases.get(current) or "").strip()
        if not mapped or mapped == current or mapped == "一缕残魂":
            break
        current = mapped
    if current != DEFAULT_SUB_YINLUO_IDENTITY:
        return current

    return DEFAULT_SUB_YINLUO_IDENTITY


SUB_YINLUO_IDENTITY = current_sub_yinluo_identity()
LEGACY_SUB_IDENTITY_ALIASES = {
    "缘生子": SUB_YINLUO_IDENTITY,
    DEFAULT_SUB_YINLUO_IDENTITY: SUB_YINLUO_IDENTITY,
}
ACCOUNT_IDENTITIES = {
    "main": ("主魂", "无咎子", "缘生子", "素缘子"),
    "sub": ("主魂", "厚土", SUB_YINLUO_IDENTITY, "寻真子"),
    "xiaohao": ("主魂", "问心子", "素心子", "缘生子"),
    "waaiging": ("主魂",),
}
MULAN_SUPPORT_MODES = ("斥候", "破灯", "奇袭", "护阵")
DEFAULT_MULAN_SUPPORT_MODE = "护阵"
MINIAPP_FISHING_PONDS = (
    ("auto", "默认最高级"),
    ("qingxi", "青溪浅滩"),
    ("hantan", "灵眼寒潭"),
    ("luanxing", "乱星海礁"),
)
MINIAPP_FISHING_BAITS = (
    ("plain", "凡饵"),
    ("spirit_rice", "灵米饵"),
    ("spirit_worm", "灵虫饵"),
    ("demon_blood", "妖血饵"),
    ("moon", "月华饵"),
)
MINIAPP_FISHING_CHUMS = (
    ("none", "不打窝"),
    ("rice", "米糠小窝"),
    ("grass", "灵草窝"),
    ("demon", "妖腥窝"),
)
MINIAPP_FISHING_RODS = (
    ("auto", "自动识别"),
    ("青竹钓竿", "青竹钓竿"),
    ("银竹钓竿", "银竹钓竿"),
    ("金竹钓竿", "金竹钓竿"),
    ("金雷竹钓竿", "金雷竹钓竿"),
)
TIANXING_MEDITATION_MODES = (
    ("deep", "深度闭关"),
    ("fate", "推命闭关"),
)
DEFAULT_TIANXING_MEDITATION_MODE = "deep"
DEFAULT_TIANXING_USE_HEQI_PILL = False
DEFAULT_TIANXING_TIANJI_GRIND_ENABLED = False
DEFAULT_TIANXING_TIANJI_GRIND_TARGET = 0
TIANXING_TIANJI_SUPPORTED_IDENTITIES = {
    "main": ("主魂", "无咎子"),
    "waaiging": ("主魂",),
}
DEFAULT_TIANXING_TIANJI_GRIND_PARTICIPANTS = ("main|主魂",)
DEFAULT_MINIAPP_FISHING_ENABLED = True
DEFAULT_MINIAPP_FISHING_POND = "qingxi"
DEFAULT_MINIAPP_FISHING_BAIT = "demon_blood"
DEFAULT_MINIAPP_FISHING_CHUM = "none"
DEFAULT_MINIAPP_FISHING_ROD = "auto"
MINIAPP_FISHING_SUPPORTED_ACCOUNTS = ("main", "sub", "xiaohao", "waaiging")
DEFAULT_MINIAPP_FISHING_PARTICIPANTS = ("main|主魂",)
DEFAULT_MINIAPP_FISHING_ROD_OWNER = "auto"
DEFAULT_MINIAPP_FISHING_START_TIME = ""
DEFAULT_MINIAPP_JOURNEY_ENABLED = True
MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS = tuple(ACCOUNT_IDENTITIES)
DEFAULT_MINIAPP_JOURNEY_PARTICIPANTS = (
    "main|主魂",
    "main|无咎子",
    "waaiging|主魂",
)
DEFAULT_MINIAPP_TIANJI_TRIAL_ENABLED = True
DEFAULT_MINIAPP_TIANJI_TRIAL_PARTICIPANTS = tuple(
    f"{account}|{identity}"
    for account, identities in ACCOUNT_IDENTITIES.items()
    for identity in identities
)
DEFAULT_MINIAPP_FATE_CARDS_ENABLED = True
DEFAULT_MINIAPP_FATE_CARDS_PARTICIPANTS = tuple(
    f"{account}|{identity}"
    for account, identities in ACCOUNT_IDENTITIES.items()
    for identity in identities
)
DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MIN = 0
DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MAX = 0
DEFAULT_WORLD_BOSS_PARTICIPANTS = tuple(
    (account, "主魂") for account in ACCOUNT_IDENTITIES
)


def canonical_automation_identity(account: Any, identity: Any) -> str:
    account_key = str(account or "").strip()
    identity_name = str(identity or "").strip()
    if account_key == "sub":
        current = current_sub_yinluo_identity()
        if identity_name in {"缘生子", DEFAULT_SUB_YINLUO_IDENTITY, SUB_YINLUO_IDENTITY}:
            return current
        aliases = _load_sub_identity_state().get("avatar_dao_name_aliases")
        seen = set()
        while isinstance(aliases, dict) and identity_name not in seen:
            seen.add(identity_name)
            mapped = str(aliases.get(identity_name) or "").strip()
            if not mapped or mapped == identity_name:
                break
            identity_name = mapped
        if identity_name == "一缕残魂":
            return current
    return identity_name


def automation_account_identities() -> dict[str, tuple[str, ...]]:
    identities = dict(ACCOUNT_IDENTITIES)
    current = current_sub_yinluo_identity()
    identities["sub"] = tuple(
        current if identity == SUB_YINLUO_IDENTITY else identity
        for identity in ACCOUNT_IDENTITIES["sub"]
    )
    return identities


def automation_participant_key(account: Any, identity: Any) -> str:
    account_key = str(account or "").strip()
    return f"{account_key}|{canonical_automation_identity(account_key, identity)}"


def _valid_participant(account: str, identity: str) -> bool:
    identities = automation_account_identities()
    return account in identities and identity in identities[account]


def _normalize_participant(value: Any) -> tuple[str, str] | None:
    if isinstance(value, dict):
        account = str(value.get("account") or "").strip()
        identity = str(value.get("identity") or "").strip()
    else:
        text = str(value or "").strip()
        if "|" not in text:
            return None
        account, identity = (part.strip() for part in text.split("|", 1))
    identity = canonical_automation_identity(account, identity)
    if not _valid_participant(account, identity):
        return None
    return account, identity


def _normalize_clock_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        return ""
    return text if parsed.strftime("%H:%M") == text else ""


def _normalize_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _parse_optional_nonnegative_int(value: Any, error_message: str) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError(error_message)
    if number < 0:
        raise ValueError(error_message)
    return number


def default_automation_settings() -> dict[str, Any]:
    return {
        "version": 12,
        "world_boss": {
            "participants": [
                automation_participant_key(account, identity)
                for account, identity in DEFAULT_WORLD_BOSS_PARTICIPANTS
            ],
        },
        "mulan_support": {"mode": DEFAULT_MULAN_SUPPORT_MODE},
        "miniapp_beast_abyss": {
            "power_min": DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MIN,
            "power_max": DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MAX,
        },
        "miniapp_fishing": {
            "enabled": DEFAULT_MINIAPP_FISHING_ENABLED,
            "participants": list(DEFAULT_MINIAPP_FISHING_PARTICIPANTS),
            "rod": DEFAULT_MINIAPP_FISHING_ROD,
            "rod_owner": DEFAULT_MINIAPP_FISHING_ROD_OWNER,
            "pond": DEFAULT_MINIAPP_FISHING_POND,
            "bait": DEFAULT_MINIAPP_FISHING_BAIT,
            "chum": DEFAULT_MINIAPP_FISHING_CHUM,
            "start_time": DEFAULT_MINIAPP_FISHING_START_TIME,
        },
        "miniapp_journey": {
            "enabled": DEFAULT_MINIAPP_JOURNEY_ENABLED,
            "participants": list(DEFAULT_MINIAPP_JOURNEY_PARTICIPANTS),
        },
        "miniapp_tianji_trial": {
            "enabled": DEFAULT_MINIAPP_TIANJI_TRIAL_ENABLED,
            "participants": [
                automation_participant_key(account, identity)
                for account, identities in automation_account_identities().items()
                for identity in identities
            ],
        },
        "miniapp_fate_cards": {
            "enabled": DEFAULT_MINIAPP_FATE_CARDS_ENABLED,
            "participants": [
                automation_participant_key(account, identity)
                for account, identities in automation_account_identities().items()
                for identity in identities
            ],
        },
        "tianxing": {
            "meditation_mode": DEFAULT_TIANXING_MEDITATION_MODE,
            "meditation_switch_id": "",
            "use_heqi_pill": DEFAULT_TIANXING_USE_HEQI_PILL,
            "tianji_grind_enabled": DEFAULT_TIANXING_TIANJI_GRIND_ENABLED,
            "tianji_grind_target": DEFAULT_TIANXING_TIANJI_GRIND_TARGET,
            "tianji_grind_participants": list(DEFAULT_TIANXING_TIANJI_GRIND_PARTICIPANTS),
            "tianji_round_id": "",
        },
        "updated_at": "",
        "updated_by": "",
    }


def normalize_automation_settings(data: Any) -> dict[str, Any]:
    source = data if isinstance(data, dict) else {}
    result = default_automation_settings()

    world_boss = source.get("world_boss")
    if isinstance(world_boss, dict) and isinstance(world_boss.get("participants"), list):
        selected_by_account: dict[str, str] = {}
        for item in world_boss["participants"]:
            normalized = _normalize_participant(item)
            if normalized is None:
                continue
            account, identity = normalized
            selected_by_account.setdefault(account, identity)
        result["world_boss"]["participants"] = [
            automation_participant_key(account, identity)
            for account, identities in automation_account_identities().items()
            for identity in identities
            if selected_by_account.get(account) == identity
        ]

    mulan = source.get("mulan_support")
    if isinstance(mulan, dict):
        mode = str(mulan.get("mode") or "").strip()
        if mode in MULAN_SUPPORT_MODES:
            result["mulan_support"]["mode"] = mode

    abyss = source.get("miniapp_beast_abyss")
    if isinstance(abyss, dict):
        power_min = _normalize_nonnegative_int(
            abyss.get("power_min"),
            DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MIN,
        )
        power_max = _normalize_nonnegative_int(
            abyss.get("power_max"),
            DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MAX,
        )
        if power_max > 0 and power_min > 0 and power_max < power_min:
            power_min = DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MIN
            power_max = DEFAULT_MINIAPP_BEAST_ABYSS_POWER_MAX
        result["miniapp_beast_abyss"]["power_min"] = power_min
        result["miniapp_beast_abyss"]["power_max"] = power_max

    fishing = source.get("miniapp_fishing")
    if isinstance(fishing, dict):
        result["miniapp_fishing"]["enabled"] = bool(
            fishing.get("enabled", DEFAULT_MINIAPP_FISHING_ENABLED)
        )
        if isinstance(fishing.get("participants"), list):
            participants = []
            for item in fishing["participants"]:
                normalized = _normalize_participant(item)
                if normalized is None or normalized[0] not in MINIAPP_FISHING_SUPPORTED_ACCOUNTS:
                    continue
                key = automation_participant_key(*normalized)
                if key not in participants:
                    participants.append(key)
            result["miniapp_fishing"]["participants"] = participants
        else:
            legacy = _normalize_participant(
                {
                    "account": fishing.get("account", "main"),
                    "identity": fishing.get("identity", "主魂"),
                }
            )
            if legacy is not None and legacy[0] in MINIAPP_FISHING_SUPPORTED_ACCOUNTS:
                result["miniapp_fishing"]["participants"] = [
                    automation_participant_key(*legacy)
                ]
        rod_owner = str(fishing.get("rod_owner") or DEFAULT_MINIAPP_FISHING_ROD_OWNER).strip()
        normalized_owner = _normalize_participant(rod_owner)
        if rod_owner == "auto":
            result["miniapp_fishing"]["rod_owner"] = "auto"
        elif normalized_owner is not None and normalized_owner[0] in MINIAPP_FISHING_SUPPORTED_ACCOUNTS:
            result["miniapp_fishing"]["rod_owner"] = automation_participant_key(*normalized_owner)
        rod = str(fishing.get("rod") or DEFAULT_MINIAPP_FISHING_ROD).strip()
        if rod in {item[0] for item in MINIAPP_FISHING_RODS}:
            result["miniapp_fishing"]["rod"] = rod
        pond = str(fishing.get("pond") or "").strip()
        bait = str(fishing.get("bait") or "").strip()
        chum = str(fishing.get("chum") or "").strip()
        start_time = _normalize_clock_time(fishing.get("start_time"))
        if pond in {item[0] for item in MINIAPP_FISHING_PONDS}:
            result["miniapp_fishing"]["pond"] = pond
        if bait in {item[0] for item in MINIAPP_FISHING_BAITS}:
            result["miniapp_fishing"]["bait"] = bait
        if chum in {item[0] for item in MINIAPP_FISHING_CHUMS}:
            result["miniapp_fishing"]["chum"] = chum
        result["miniapp_fishing"]["start_time"] = start_time

    journey = source.get("miniapp_journey")
    if isinstance(journey, dict):
        result["miniapp_journey"]["enabled"] = bool(
            journey.get("enabled", DEFAULT_MINIAPP_JOURNEY_ENABLED)
        )
        if isinstance(journey.get("participants"), list):
            participants = []
            for item in journey["participants"]:
                normalized = _normalize_participant(item)
                if (
                    normalized is None
                    or normalized[0] not in MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS
                ):
                    continue
                key = automation_participant_key(*normalized)
                if key not in participants:
                    participants.append(key)
            result["miniapp_journey"]["participants"] = participants

    trial = source.get("miniapp_tianji_trial")
    if isinstance(trial, dict):
        result["miniapp_tianji_trial"]["enabled"] = bool(
            trial.get("enabled", DEFAULT_MINIAPP_TIANJI_TRIAL_ENABLED)
        )
        if isinstance(trial.get("participants"), list):
            participants = []
            for item in trial["participants"]:
                normalized = _normalize_participant(item)
                if normalized is None:
                    continue
                key = automation_participant_key(*normalized)
                if key not in participants:
                    participants.append(key)
            result["miniapp_tianji_trial"]["participants"] = participants

    fate_cards = source.get("miniapp_fate_cards")
    if isinstance(fate_cards, dict):
        result["miniapp_fate_cards"]["enabled"] = bool(
            fate_cards.get("enabled", DEFAULT_MINIAPP_FATE_CARDS_ENABLED)
        )
        if isinstance(fate_cards.get("participants"), list):
            participants = []
            for item in fate_cards["participants"]:
                normalized = _normalize_participant(item)
                if normalized is None:
                    continue
                key = automation_participant_key(*normalized)
                if key not in participants:
                    participants.append(key)
            result["miniapp_fate_cards"]["participants"] = participants

    tianxing = source.get("tianxing")
    if isinstance(tianxing, dict):
        meditation_mode = str(tianxing.get("meditation_mode") or "").strip()
        if meditation_mode in {item[0] for item in TIANXING_MEDITATION_MODES}:
            result["tianxing"]["meditation_mode"] = meditation_mode
        result["tianxing"]["meditation_switch_id"] = str(
            tianxing.get("meditation_switch_id") or ""
        )[:40]
        # Read the old key once so upgrading keeps an existing checkbox setting.
        result["tianxing"]["use_heqi_pill"] = bool(
            tianxing.get(
                "use_heqi_pill",
                tianxing.get("use_" + "zengyuan_pill", DEFAULT_TIANXING_USE_HEQI_PILL),
            )
        )
        result["tianxing"]["tianji_grind_enabled"] = bool(
            tianxing.get("tianji_grind_enabled", DEFAULT_TIANXING_TIANJI_GRIND_ENABLED)
        )
        result["tianxing"]["tianji_grind_target"] = _normalize_nonnegative_int(
            tianxing.get("tianji_grind_target"),
            DEFAULT_TIANXING_TIANJI_GRIND_TARGET,
        )
        raw_participants = tianxing.get("tianji_grind_participants")
        if isinstance(raw_participants, list):
            participants = []
            for item in raw_participants:
                normalized = _normalize_participant(item)
                if normalized is None:
                    continue
                account, identity = normalized
                if identity not in TIANXING_TIANJI_SUPPORTED_IDENTITIES.get(account, ()):
                    continue
                key = automation_participant_key(account, identity)
                if key not in participants:
                    participants.append(key)
            result["tianxing"]["tianji_grind_participants"] = participants
        result["tianxing"]["tianji_round_id"] = str(
            tianxing.get("tianji_round_id") or ""
        )[:40]

    result["updated_at"] = str(source.get("updated_at") or "")
    result["updated_by"] = str(source.get("updated_by") or "")
    return result


def load_automation_settings() -> dict[str, Any]:
    try:
        with AUTOMATION_SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            return normalize_automation_settings(json.load(handle))
    except (OSError, ValueError, TypeError):
        return default_automation_settings()


def save_automation_settings(
    *,
    world_boss_participants: Any,
    mulan_support_mode: Any,
    miniapp_fishing_enabled: Any = None,
    miniapp_fishing_pond: Any = None,
    miniapp_fishing_bait: Any = None,
    miniapp_fishing_chum: Any = None,
    miniapp_fishing_participants: Any = None,
    miniapp_fishing_rod: Any = None,
    miniapp_fishing_rod_owner: Any = None,
    miniapp_fishing_start_time: Any = None,
    miniapp_journey_enabled: Any = None,
    miniapp_journey_participants: Any = None,
    miniapp_tianji_trial_enabled: Any = None,
    miniapp_tianji_trial_participants: Any = None,
    miniapp_fate_cards_enabled: Any = None,
    miniapp_fate_cards_participants: Any = None,
    miniapp_beast_abyss_power_min: Any = None,
    miniapp_beast_abyss_power_max: Any = None,
    tianxing_meditation_mode: Any = None,
    tianxing_use_heqi_pill: Any = None,
    tianxing_tianji_grind_enabled: Any = None,
    tianxing_tianji_grind_target: Any = None,
    tianxing_tianji_grind_participants: Any = None,
    updated_by: str = "dashboard",
) -> dict[str, Any]:
    if not isinstance(world_boss_participants, list):
        raise ValueError("world boss participants must be a list")
    mode = str(mulan_support_mode or "").strip()
    if mode not in MULAN_SUPPORT_MODES:
        raise ValueError("invalid Mulan support mode")
    invalid = [
        item for item in world_boss_participants if _normalize_participant(item) is None
    ]
    if invalid:
        raise ValueError("invalid world boss participant")
    normalized_participants = [
        _normalize_participant(item) for item in world_boss_participants
    ]
    selected_accounts = [item[0] for item in normalized_participants if item is not None]
    if len(selected_accounts) != len(set(selected_accounts)):
        raise ValueError("multiple world boss identities per account")

    current_settings = load_automation_settings()
    current_fishing = current_settings.get("miniapp_fishing") or {}
    fishing_enabled = (
        bool(current_fishing.get("enabled", DEFAULT_MINIAPP_FISHING_ENABLED))
        if miniapp_fishing_enabled is None
        else bool(miniapp_fishing_enabled)
    )
    fishing_pond = str(
        current_fishing.get("pond")
        if miniapp_fishing_pond is None
        else miniapp_fishing_pond
    ).strip()
    fishing_bait = str(
        current_fishing.get("bait")
        if miniapp_fishing_bait is None
        else miniapp_fishing_bait
    ).strip()
    fishing_chum = str(
        current_fishing.get("chum")
        if miniapp_fishing_chum is None
        else miniapp_fishing_chum
    ).strip()
    raw_fishing_start_time = (
        current_fishing.get("start_time", DEFAULT_MINIAPP_FISHING_START_TIME)
        if miniapp_fishing_start_time is None
        else miniapp_fishing_start_time
    )
    fishing_start_time = _normalize_clock_time(raw_fishing_start_time)
    if str(raw_fishing_start_time or "").strip() and not fishing_start_time:
        raise ValueError("invalid Mini App fishing start time")
    raw_fishing_participants = (
        current_fishing.get("participants")
        if miniapp_fishing_participants is None
        else miniapp_fishing_participants
    )
    if not isinstance(raw_fishing_participants, list):
        raise ValueError("Mini App fishing participants must be a list")
    fishing_participants = []
    for item in raw_fishing_participants:
        normalized = _normalize_participant(item)
        if normalized is None or normalized[0] not in MINIAPP_FISHING_SUPPORTED_ACCOUNTS:
            raise ValueError("invalid Mini App fishing participant")
        key = automation_participant_key(*normalized)
        if key not in fishing_participants:
            fishing_participants.append(key)
    if fishing_enabled and not fishing_participants:
        raise ValueError("Mini App fishing participants required")
    fishing_rod = str(
        current_fishing.get("rod", DEFAULT_MINIAPP_FISHING_ROD)
        if miniapp_fishing_rod is None
        else miniapp_fishing_rod
    ).strip()
    if fishing_rod not in {item[0] for item in MINIAPP_FISHING_RODS}:
        raise ValueError("invalid Mini App fishing rod")
    fishing_rod_owner = str(
        current_fishing.get("rod_owner", DEFAULT_MINIAPP_FISHING_ROD_OWNER)
        if miniapp_fishing_rod_owner is None
        else miniapp_fishing_rod_owner
    ).strip()
    normalized_owner = _normalize_participant(fishing_rod_owner)
    if fishing_rod_owner != "auto" and (
        normalized_owner is None
        or normalized_owner[0] not in MINIAPP_FISHING_SUPPORTED_ACCOUNTS
    ):
        raise ValueError("invalid Mini App fishing rod owner")
    if fishing_pond not in {item[0] for item in MINIAPP_FISHING_PONDS}:
        raise ValueError("invalid Mini App fishing pond")
    if fishing_bait not in {item[0] for item in MINIAPP_FISHING_BAITS}:
        raise ValueError("invalid Mini App fishing bait")
    if fishing_chum not in {item[0] for item in MINIAPP_FISHING_CHUMS}:
        raise ValueError("invalid Mini App fishing chum")

    current_journey = current_settings.get("miniapp_journey") or {}
    journey_enabled = (
        bool(current_journey.get("enabled", DEFAULT_MINIAPP_JOURNEY_ENABLED))
        if miniapp_journey_enabled is None
        else bool(miniapp_journey_enabled)
    )
    raw_journey_participants = (
        current_journey.get("participants")
        if miniapp_journey_participants is None
        else miniapp_journey_participants
    )
    if not isinstance(raw_journey_participants, list):
        raise ValueError("Mini App journey participants must be a list")
    journey_participants = []
    for item in raw_journey_participants:
        normalized = _normalize_participant(item)
        if (
            normalized is None
            or normalized[0] not in MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS
        ):
            raise ValueError("invalid Mini App journey participant")
        key = automation_participant_key(*normalized)
        if key not in journey_participants:
            journey_participants.append(key)
    if journey_enabled and not journey_participants:
        raise ValueError("Mini App journey participants required")

    current_trial = current_settings.get("miniapp_tianji_trial") or {}
    trial_enabled = (
        bool(current_trial.get("enabled", DEFAULT_MINIAPP_TIANJI_TRIAL_ENABLED))
        if miniapp_tianji_trial_enabled is None
        else bool(miniapp_tianji_trial_enabled)
    )
    raw_trial_participants = (
        current_trial.get("participants")
        if miniapp_tianji_trial_participants is None
        else miniapp_tianji_trial_participants
    )
    if not isinstance(raw_trial_participants, list):
        raise ValueError("Mini App Tianji trial participants must be a list")
    trial_participants = []
    for item in raw_trial_participants:
        normalized = _normalize_participant(item)
        if normalized is None:
            raise ValueError("invalid Mini App Tianji trial participant")
        key = automation_participant_key(*normalized)
        if key not in trial_participants:
            trial_participants.append(key)
    if trial_enabled and not trial_participants:
        raise ValueError("Mini App Tianji trial participants required")

    current_fate_cards = current_settings.get("miniapp_fate_cards") or {}
    fate_cards_enabled = (
        bool(current_fate_cards.get("enabled", DEFAULT_MINIAPP_FATE_CARDS_ENABLED))
        if miniapp_fate_cards_enabled is None
        else bool(miniapp_fate_cards_enabled)
    )
    raw_fate_cards_participants = (
        current_fate_cards.get("participants")
        if miniapp_fate_cards_participants is None
        else miniapp_fate_cards_participants
    )
    if not isinstance(raw_fate_cards_participants, list):
        raise ValueError("Mini App Fate Cards participants must be a list")
    fate_cards_participants = []
    for item in raw_fate_cards_participants:
        normalized = _normalize_participant(item)
        if normalized is None:
            raise ValueError("invalid Mini App Fate Cards participant")
        key = automation_participant_key(*normalized)
        if key not in fate_cards_participants:
            fate_cards_participants.append(key)
    if fate_cards_enabled and not fate_cards_participants:
        raise ValueError("Mini App Fate Cards participants required")
    current_abyss = current_settings.get("miniapp_beast_abyss") or {}
    power_min = _parse_optional_nonnegative_int(
        current_abyss.get("power_min")
        if miniapp_beast_abyss_power_min is None
        else miniapp_beast_abyss_power_min,
        "invalid Mini App beast abyss power range",
    )
    power_max = _parse_optional_nonnegative_int(
        current_abyss.get("power_max")
        if miniapp_beast_abyss_power_max is None
        else miniapp_beast_abyss_power_max,
        "invalid Mini App beast abyss power range",
    )
    if power_max > 0 and power_min > 0 and power_max < power_min:
        raise ValueError("invalid Mini App beast abyss power range")

    current_tianxing = current_settings.get("tianxing") or {}
    meditation_mode = str(
        current_tianxing.get("meditation_mode", DEFAULT_TIANXING_MEDITATION_MODE)
        if tianxing_meditation_mode is None
        else tianxing_meditation_mode
    ).strip()
    if meditation_mode not in {item[0] for item in TIANXING_MEDITATION_MODES}:
        raise ValueError("invalid Tianxing meditation mode")
    meditation_switch_id = str(current_tianxing.get("meditation_switch_id") or "")
    if (
        not meditation_switch_id
        or meditation_mode
        != str(current_tianxing.get("meditation_mode") or DEFAULT_TIANXING_MEDITATION_MODE)
    ):
        meditation_switch_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    use_heqi_pill = (
        bool(
            current_tianxing.get(
                "use_heqi_pill",
                current_tianxing.get(
                    "use_" + "zengyuan_pill", DEFAULT_TIANXING_USE_HEQI_PILL
                ),
            )
        )
        if tianxing_use_heqi_pill is None
        else bool(tianxing_use_heqi_pill)
    )
    tianji_grind_enabled = (
        bool(current_tianxing.get("tianji_grind_enabled", DEFAULT_TIANXING_TIANJI_GRIND_ENABLED))
        if tianxing_tianji_grind_enabled is None
        else bool(tianxing_tianji_grind_enabled)
    )
    tianji_grind_target = _parse_optional_nonnegative_int(
        current_tianxing.get("tianji_grind_target", DEFAULT_TIANXING_TIANJI_GRIND_TARGET)
        if tianxing_tianji_grind_target is None
        else tianxing_tianji_grind_target,
        "invalid Tianxing Tianji grind target",
    )
    if tianji_grind_target > 10000:
        raise ValueError("invalid Tianxing Tianji grind target")
    raw_tianji_participants = (
        current_tianxing.get("tianji_grind_participants")
        if tianxing_tianji_grind_participants is None
        else tianxing_tianji_grind_participants
    )
    if not isinstance(raw_tianji_participants, list):
        raise ValueError("Tianxing Tianji grind participants must be a list")
    tianji_grind_participants = []
    for item in raw_tianji_participants:
        normalized = _normalize_participant(item)
        if normalized is None:
            raise ValueError("invalid Tianxing Tianji grind participant")
        account, identity = normalized
        if identity not in TIANXING_TIANJI_SUPPORTED_IDENTITIES.get(account, ()):
            raise ValueError("invalid Tianxing Tianji grind participant")
        key = automation_participant_key(account, identity)
        if key not in tianji_grind_participants:
            tianji_grind_participants.append(key)
    if tianji_grind_enabled and tianji_grind_target <= 0:
        raise ValueError("Tianxing Tianji grind target required")
    if tianji_grind_enabled and not tianji_grind_participants:
        raise ValueError("Tianxing Tianji grind participants required")
    tianji_round_id = str(current_tianxing.get("tianji_round_id") or "")
    if tianji_grind_enabled and (
        not bool(current_tianxing.get("tianji_grind_enabled"))
        or tianji_grind_target
        != _normalize_nonnegative_int(current_tianxing.get("tianji_grind_target"))
    ):
        tianji_round_id = datetime.now().strftime("%Y%m%d%H%M%S%f")

    settings = normalize_automation_settings(
        {
            "world_boss": {"participants": world_boss_participants},
            "mulan_support": {"mode": mode},
            "miniapp_beast_abyss": {
                "power_min": power_min,
                "power_max": power_max,
            },
            "miniapp_fishing": {
                "enabled": fishing_enabled,
                "participants": fishing_participants,
                "rod": fishing_rod,
                "rod_owner": fishing_rod_owner,
                "pond": fishing_pond,
                "bait": fishing_bait,
                "chum": fishing_chum,
                "start_time": fishing_start_time,
            },
            "miniapp_journey": {
                "enabled": journey_enabled,
                "participants": journey_participants,
            },
            "miniapp_tianji_trial": {
                "enabled": trial_enabled,
                "participants": trial_participants,
            },
            "miniapp_fate_cards": {
                "enabled": fate_cards_enabled,
                "participants": fate_cards_participants,
            },
            "tianxing": {
                "meditation_mode": meditation_mode,
                "meditation_switch_id": meditation_switch_id,
                "use_heqi_pill": use_heqi_pill,
                "tianji_grind_enabled": tianji_grind_enabled,
                "tianji_grind_target": tianji_grind_target,
                "tianji_grind_participants": tianji_grind_participants,
                "tianji_round_id": tianji_round_id,
            },
            "updated_at": datetime.now().strftime(TIME_FORMAT),
            "updated_by": str(updated_by or "dashboard")[:80],
        }
    )
    AUTOMATION_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = AUTOMATION_SETTINGS_FILE.with_name(
        f"{AUTOMATION_SETTINGS_FILE.name}.{os.getpid()}.tmp"
    )
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, AUTOMATION_SETTINGS_FILE)
    return settings


def world_boss_identities_for_account(
    account: str,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    account = str(account or "").strip()
    identities = automation_account_identities()
    if account not in identities:
        return []
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    selected = set((source.get("world_boss") or {}).get("participants") or [])
    return [
        identity
        for identity in identities[account]
        if automation_participant_key(account, identity) in selected
    ]


def mulan_support_mode(settings: dict[str, Any] | None = None) -> str:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return str((source.get("mulan_support") or {}).get("mode") or DEFAULT_MULAN_SUPPORT_MODE)


def mulan_support_command(settings: dict[str, Any] | None = None) -> str:
    return f".支援慕兰 {mulan_support_mode(settings)}"


def miniapp_beast_abyss_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(source.get("miniapp_beast_abyss") or default_automation_settings()["miniapp_beast_abyss"])


def miniapp_beast_abyss_power_in_range(
    power: Any,
    settings: dict[str, Any] | None = None,
    *,
    match_all_when_empty: bool = True,
) -> bool:
    config = miniapp_beast_abyss_settings(settings)
    power_min = _normalize_nonnegative_int(config.get("power_min"))
    power_max = _normalize_nonnegative_int(config.get("power_max"))
    filter_active = power_min > 0 or power_max > 0
    if not filter_active:
        return bool(match_all_when_empty)
    try:
        current = int(power)
    except (TypeError, ValueError):
        return False
    if current < 0:
        return False
    if power_min > 0 and current < power_min:
        return False
    if power_max > 0 and current > power_max:
        return False
    return True


def miniapp_fishing_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(source.get("miniapp_fishing") or default_automation_settings()["miniapp_fishing"])


def miniapp_journey_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(
        source.get("miniapp_journey")
        or default_automation_settings()["miniapp_journey"]
    )


def miniapp_journey_identities_for_account(
    account: str,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    account = str(account or "").strip()
    supported = automation_account_identities().get(account, ())
    if account not in MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS or not supported:
        return []
    selected = set(miniapp_journey_settings(settings).get("participants") or [])
    return [
        identity
        for identity in supported
        if automation_participant_key(account, identity) in selected
    ]


def miniapp_tianji_trial_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(
        source.get("miniapp_tianji_trial")
        or default_automation_settings()["miniapp_tianji_trial"]
    )


def miniapp_tianji_trial_identities_for_account(
    account: str,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    account = str(account or "").strip()
    supported = automation_account_identities().get(account, ())
    if not supported:
        return []
    selected = set(miniapp_tianji_trial_settings(settings).get("participants") or [])
    return [
        identity
        for identity in supported
        if automation_participant_key(account, identity) in selected
    ]


def miniapp_fate_cards_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(
        source.get("miniapp_fate_cards")
        or default_automation_settings()["miniapp_fate_cards"]
    )


def miniapp_fate_cards_identities_for_account(
    account: str,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    account = str(account or "").strip()
    supported = automation_account_identities().get(account, ())
    if not supported:
        return []
    selected = set(miniapp_fate_cards_settings(settings).get("participants") or [])
    return [
        identity
        for identity in supported
        if automation_participant_key(account, identity) in selected
    ]


def tianxing_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(source.get("tianxing") or default_automation_settings()["tianxing"])


def tianxing_tianji_identities_for_account(
    account: str,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    account = str(account or "").strip()
    supported = TIANXING_TIANJI_SUPPORTED_IDENTITIES.get(account, ())
    if not supported:
        return []
    config = tianxing_settings(settings)
    selected = set(config.get("tianji_grind_participants") or [])
    return [
        identity
        for identity in supported
        if automation_participant_key(account, identity) in selected
    ]


def set_tianxing_heqi_pill_enabled(
    enabled: bool,
    *,
    updated_by: str = "runtime",
) -> dict[str, Any]:
    """Update the runtime pill switch without changing unrelated settings."""
    current = load_automation_settings()
    return save_automation_settings(
        world_boss_participants=list(
            (current.get("world_boss") or {}).get("participants") or []
        ),
        mulan_support_mode=(current.get("mulan_support") or {}).get("mode"),
        tianxing_use_heqi_pill=bool(enabled),
        updated_by=updated_by,
    )


def automation_dashboard_payload() -> dict[str, Any]:
    settings = load_automation_settings()
    selected = set((settings.get("world_boss") or {}).get("participants") or [])
    account_identities = automation_account_identities()
    return {
        "settings": settings,
        "world_boss": {
            "selected_count": len(selected),
            "accounts": [
                {
                    "key": account,
                    "name": ACCOUNT_NAMES[account],
                    "identities": [
                        {
                            "key": automation_participant_key(account, identity),
                            "name": identity,
                            "selected": automation_participant_key(account, identity) in selected,
                        }
                        for identity in identities
                    ],
                }
                for account, identities in account_identities.items()
            ],
        },
        "mulan_support": {
            "mode": mulan_support_mode(settings),
            "command": mulan_support_command(settings),
            "modes": list(MULAN_SUPPORT_MODES),
        },
        "miniapp_beast_abyss": {
            **miniapp_beast_abyss_settings(settings),
        },
        "miniapp_fishing": {
            **miniapp_fishing_settings(settings),
            "accounts": [
                {
                    "key": account,
                    "name": ACCOUNT_NAMES[account],
                    "identities": [
                        {
                            "key": automation_participant_key(account, identity),
                            "name": identity,
                        }
                        for identity in account_identities[account]
                    ],
                }
                for account in MINIAPP_FISHING_SUPPORTED_ACCOUNTS
            ],
            "ponds": [
                {"key": key, "name": name}
                for key, name in MINIAPP_FISHING_PONDS
            ],
            "baits": [
                {"key": key, "name": name}
                for key, name in MINIAPP_FISHING_BAITS
            ],
            "chums": [
                {"key": key, "name": name}
                for key, name in MINIAPP_FISHING_CHUMS
            ],
            "rods": [
                {"key": key, "name": name}
                for key, name in MINIAPP_FISHING_RODS
            ],
        },
        "miniapp_journey": {
            **miniapp_journey_settings(settings),
            "accounts": [
                {
                    "key": account,
                    "name": ACCOUNT_NAMES[account],
                    "identities": [
                        {
                            "key": automation_participant_key(account, identity),
                            "name": identity,
                        }
                        for identity in account_identities[account]
                    ],
                }
                for account in MINIAPP_JOURNEY_SUPPORTED_ACCOUNTS
            ],
        },
        "miniapp_tianji_trial": {
            **miniapp_tianji_trial_settings(settings),
            "accounts": [
                {
                    "key": account,
                    "name": ACCOUNT_NAMES[account],
                    "identities": [
                        {
                            "key": automation_participant_key(account, identity),
                            "name": identity,
                        }
                        for identity in identities
                    ],
                }
                for account, identities in account_identities.items()
            ],
        },
        "miniapp_fate_cards": {
            **miniapp_fate_cards_settings(settings),
            "accounts": [
                {
                    "key": account,
                    "name": ACCOUNT_NAMES[account],
                    "identities": [
                        {
                            "key": automation_participant_key(account, identity),
                            "name": identity,
                        }
                        for identity in identities
                    ],
                }
                for account, identities in account_identities.items()
            ],
        },
        "tianxing": {
            **tianxing_settings(settings),
            "meditation_modes": [
                {"key": key, "name": name}
                for key, name in TIANXING_MEDITATION_MODES
            ],
            "tianji_accounts": [
                {
                    "key": account,
                    "name": ACCOUNT_NAMES[account],
                    "identities": [
                        {
                            "key": automation_participant_key(account, identity),
                            "name": identity,
                        }
                        for identity in identities
                    ],
                }
                for account, identities in TIANXING_TIANJI_SUPPORTED_IDENTITIES.items()
            ],
        },
        "server_time": datetime.now().strftime(TIME_FORMAT),
    }
