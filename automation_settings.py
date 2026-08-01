#!/usr/bin/env python3
"""Shared Dashboard-controlled automation settings."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(__file__).resolve().parent
AUTOMATION_SETTINGS_FILE = CONFIG_DIR / "automation_settings.json"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ACCOUNT_NAMES = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}
ACCOUNT_IDENTITIES = {
    "main": ("主魂", "无咎子", "缘生子", "素缘子"),
    "sub": ("主魂", "厚土", "缘生子", "寻真子"),
    "xiaohao": ("主魂", "问心子", "素心子", "缘生子"),
    "waaiging": ("主魂",),
}
MULAN_SUPPORT_MODES = ("斥候", "破灯", "奇袭", "护阵")
DEFAULT_MULAN_SUPPORT_MODE = "护阵"
MINIAPP_FISHING_PONDS = (
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
DEFAULT_MINIAPP_FISHING_ENABLED = True
DEFAULT_MINIAPP_FISHING_POND = "qingxi"
DEFAULT_MINIAPP_FISHING_BAIT = "demon_blood"
DEFAULT_MINIAPP_FISHING_CHUM = "none"
DEFAULT_WORLD_BOSS_PARTICIPANTS = tuple(
    (account, "主魂") for account in ACCOUNT_IDENTITIES
)


def automation_participant_key(account: Any, identity: Any) -> str:
    return f"{str(account or '').strip()}|{str(identity or '').strip()}"


def _valid_participant(account: str, identity: str) -> bool:
    return account in ACCOUNT_IDENTITIES and identity in ACCOUNT_IDENTITIES[account]


def _normalize_participant(value: Any) -> tuple[str, str] | None:
    if isinstance(value, dict):
        account = str(value.get("account") or "").strip()
        identity = str(value.get("identity") or "").strip()
    else:
        text = str(value or "").strip()
        if "|" not in text:
            return None
        account, identity = (part.strip() for part in text.split("|", 1))
    if not _valid_participant(account, identity):
        return None
    return account, identity


def default_automation_settings() -> dict[str, Any]:
    return {
        "version": 2,
        "world_boss": {
            "participants": [
                automation_participant_key(account, identity)
                for account, identity in DEFAULT_WORLD_BOSS_PARTICIPANTS
            ],
        },
        "mulan_support": {"mode": DEFAULT_MULAN_SUPPORT_MODE},
        "miniapp_fishing": {
            "enabled": DEFAULT_MINIAPP_FISHING_ENABLED,
            "account": "main",
            "identity": "主魂",
            "pond": DEFAULT_MINIAPP_FISHING_POND,
            "bait": DEFAULT_MINIAPP_FISHING_BAIT,
            "chum": DEFAULT_MINIAPP_FISHING_CHUM,
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
            for account, identities in ACCOUNT_IDENTITIES.items()
            for identity in identities
            if selected_by_account.get(account) == identity
        ]

    mulan = source.get("mulan_support")
    if isinstance(mulan, dict):
        mode = str(mulan.get("mode") or "").strip()
        if mode in MULAN_SUPPORT_MODES:
            result["mulan_support"]["mode"] = mode

    fishing = source.get("miniapp_fishing")
    if isinstance(fishing, dict):
        result["miniapp_fishing"]["enabled"] = bool(
            fishing.get("enabled", DEFAULT_MINIAPP_FISHING_ENABLED)
        )
        pond = str(fishing.get("pond") or "").strip()
        bait = str(fishing.get("bait") or "").strip()
        chum = str(fishing.get("chum") or "").strip()
        if pond in {item[0] for item in MINIAPP_FISHING_PONDS}:
            result["miniapp_fishing"]["pond"] = pond
        if bait in {item[0] for item in MINIAPP_FISHING_BAITS}:
            result["miniapp_fishing"]["bait"] = bait
        if chum in {item[0] for item in MINIAPP_FISHING_CHUMS}:
            result["miniapp_fishing"]["chum"] = chum

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

    current_fishing = (load_automation_settings().get("miniapp_fishing") or {})
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
    if fishing_pond not in {item[0] for item in MINIAPP_FISHING_PONDS}:
        raise ValueError("invalid Mini App fishing pond")
    if fishing_bait not in {item[0] for item in MINIAPP_FISHING_BAITS}:
        raise ValueError("invalid Mini App fishing bait")
    if fishing_chum not in {item[0] for item in MINIAPP_FISHING_CHUMS}:
        raise ValueError("invalid Mini App fishing chum")

    settings = normalize_automation_settings(
        {
            "world_boss": {"participants": world_boss_participants},
            "mulan_support": {"mode": mode},
            "miniapp_fishing": {
                "enabled": fishing_enabled,
                "pond": fishing_pond,
                "bait": fishing_bait,
                "chum": fishing_chum,
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
    if account not in ACCOUNT_IDENTITIES:
        return []
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    selected = set((source.get("world_boss") or {}).get("participants") or [])
    return [
        identity
        for identity in ACCOUNT_IDENTITIES[account]
        if automation_participant_key(account, identity) in selected
    ]


def mulan_support_mode(settings: dict[str, Any] | None = None) -> str:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return str((source.get("mulan_support") or {}).get("mode") or DEFAULT_MULAN_SUPPORT_MODE)


def mulan_support_command(settings: dict[str, Any] | None = None) -> str:
    return f".支援慕兰 {mulan_support_mode(settings)}"


def miniapp_fishing_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    source = normalize_automation_settings(settings) if settings is not None else load_automation_settings()
    return dict(source.get("miniapp_fishing") or default_automation_settings()["miniapp_fishing"])


def automation_dashboard_payload() -> dict[str, Any]:
    settings = load_automation_settings()
    selected = set((settings.get("world_boss") or {}).get("participants") or [])
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
                for account, identities in ACCOUNT_IDENTITIES.items()
            ],
        },
        "mulan_support": {
            "mode": mulan_support_mode(settings),
            "command": mulan_support_command(settings),
            "modes": list(MULAN_SUPPORT_MODES),
        },
        "miniapp_fishing": {
            **miniapp_fishing_settings(settings),
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
        },
        "server_time": datetime.now().strftime(TIME_FORMAT),
    }
