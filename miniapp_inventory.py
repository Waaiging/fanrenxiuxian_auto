#!/usr/bin/env python3
"""On-demand Mini App inventory snapshots shared with the Dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any

from automation_settings import canonical_automation_identity


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
INVENTORY_TYPES = ("法宝", "物品", "材料")
INVENTORY_TYPE_ORDER = {name: index for index, name in enumerate(INVENTORY_TYPES)}
INVENTORY_ACCOUNT_IDENTITIES = {
    "main": ("主魂", "无咎子", "缘生子", "素缘子"),
    "sub": ("主魂", "厚土", "竹和生", "寻真子"),
    "xiaohao": ("主魂", "问心子", "素心子", "缘生子"),
    "waaiging": ("主魂",),
}
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_REQUEST_WRITE_LOCK = threading.Lock()


def _now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def _account_key(account: Any) -> str:
    key = str(account or "").strip()
    if key not in INVENTORY_ACCOUNT_IDENTITIES:
        raise ValueError("unknown_inventory_account")
    return key


def inventory_request_path(account: Any, base_dir: str | None = None) -> str:
    key = _account_key(account)
    return os.path.join(base_dir or _BASE_DIR, f"miniapp_inventory_request_{key}.json")


def inventory_cache_path(account: Any, base_dir: str | None = None) -> str:
    key = _account_key(account)
    return os.path.join(base_dir or _BASE_DIR, f"miniapp_inventory_cache_{key}.json")


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: str, value: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def read_inventory_request(account: Any, base_dir: str | None = None) -> dict[str, Any]:
    return _read_json(inventory_request_path(account, base_dir))


def write_inventory_request(
    account: Any,
    identity: Any,
    *,
    requested_by: str = "dashboard",
    request_id: str = "",
    base_dir: str | None = None,
) -> dict[str, Any]:
    key = _account_key(account)
    target = str(identity or "").strip()
    if target != "*":
        target = canonical_automation_identity(key, target)
    if target != "*" and target not in INVENTORY_ACCOUNT_IDENTITIES[key]:
        raise ValueError("unknown_inventory_identity")
    request = {
        "request_id": str(request_id or uuid.uuid4().hex),
        "account": key,
        "identity": target,
        "requested_at": _now_text(),
        "requested_by": str(requested_by or "dashboard"),
    }
    with _REQUEST_WRITE_LOCK:
        _write_json(inventory_request_path(key, base_dir), request)
    return request


def empty_inventory_cache(account: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "account": _account_key(account),
        "updated_at": "",
        "last_completed_request_id": "",
        "last_request": {},
        "request_progress": {},
        "snapshots": {},
    }


def read_inventory_cache(account: Any, base_dir: str | None = None) -> dict[str, Any]:
    key = _account_key(account)
    cache = _read_json(inventory_cache_path(key, base_dir))
    if not cache:
        return empty_inventory_cache(key)
    cache.setdefault("version", 1)
    cache["account"] = key
    cache.setdefault("updated_at", "")
    cache.setdefault("last_completed_request_id", "")
    cache.setdefault("last_request", {})
    cache.setdefault("request_progress", {})
    cache.setdefault("snapshots", {})
    if not isinstance(cache["snapshots"], dict):
        cache["snapshots"] = {}
    if key == "sub" and "缘生子" in cache["snapshots"]:
        cache["snapshots"].setdefault("竹和生", cache["snapshots"].pop("缘生子"))
    for field in ("last_request", "request_progress"):
        value = cache.get(field)
        if isinstance(value, dict) and value.get("identity") not in {None, "*"}:
            value["identity"] = canonical_automation_identity(key, value.get("identity"))
    return cache


def write_inventory_cache(
    account: Any,
    cache: dict[str, Any],
    base_dir: str | None = None,
) -> None:
    key = _account_key(account)
    value = dict(cache)
    value["account"] = key
    value["version"] = 1
    value["updated_at"] = _now_text()
    _write_json(inventory_cache_path(key, base_dir), value)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _quantity(value: Any, default: int = 0) -> int | float:
    if value is None or value == "":
        return default
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def _row_name(row: dict[str, Any]) -> str:
    for key in ("name", "itemName", "label", "itemId"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _item_detail(category: str, row: dict[str, Any]) -> str:
    details: list[str] = []
    if category == "法宝":
        if row.get("active"):
            details.append("已祭出")
        if row.get("suppressed"):
            details.append("被压制")
        if row.get("natal"):
            details.append("本命")
        if row.get("refined"):
            details.append("已炼化")
        durability = row.get("durability")
        maximum = row.get("maxDurability")
        if durability is not None and maximum is not None:
            details.append(f"耐久 {durability}/{maximum}")
        elif row.get("durabilityPct") is not None:
            details.append(f"耐久 {row.get('durabilityPct')}%")
    elif category == "物品":
        type_label = str(row.get("typeLabel") or "").strip()
        if type_label:
            details.append(type_label)
    else:
        description = str(row.get("description") or row.get("effectText") or "").strip()
        if description:
            details.append(description)
    return " · ".join(details)


def normalize_inventory_sections(inventory_payload: Any) -> list[dict[str, Any]]:
    """Normalize bag treasures, items, and materials into sorted rows."""
    inventory_account = _mapping(_mapping(inventory_payload).get("account"))
    bag = _mapping(inventory_account.get("bagTreasure"))
    sources = (
        ("法宝", _rows(bag.get("treasures")), 1),
        ("物品", _rows(bag.get("items")), 0),
        ("材料", _rows(bag.get("materials")), 0),
    )
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for category, rows, default_quantity in sources:
        for row in rows:
            name = _row_name(row)
            if not name:
                continue
            item_id = str(row.get("itemId") or "").strip()
            key = (category, name.casefold(), item_id.casefold())
            quantity = _quantity(row.get("quantity"), default_quantity)
            if quantity <= 0:
                continue
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    "type": category,
                    "name": name,
                    "quantity": quantity,
                    "item_id": item_id,
                    "detail": _item_detail(category, row),
                }
            else:
                current["quantity"] = _quantity(current.get("quantity")) + quantity
    return sorted(
        merged.values(),
        key=lambda row: (
            INVENTORY_TYPE_ORDER.get(str(row.get("type")), len(INVENTORY_TYPES)),
            str(row.get("name") or "").casefold(),
            str(row.get("item_id") or "").casefold(),
        ),
    )


def inventory_snapshot(
    account: str,
    identity: str,
    inventory_payload: Any,
) -> dict[str, Any]:
    items = normalize_inventory_sections(inventory_payload)
    counts = {category: 0 for category in INVENTORY_TYPES}
    for row in items:
        counts[str(row.get("type"))] += 1
    return {
        "account": account,
        "identity": identity,
        "updated_at": _now_text(),
        "counts": counts,
        "item_count": len(items),
        "items": items,
    }


def search_inventory_caches(
    caches: dict[str, dict[str, Any]],
    query: Any,
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    results: list[dict[str, Any]] = []
    for account, identities in INVENTORY_ACCOUNT_IDENTITIES.items():
        snapshots = _mapping(_mapping(caches.get(account)).get("snapshots"))
        for identity in identities:
            snapshot = _mapping(snapshots.get(identity))
            for row in _rows(snapshot.get("items")):
                haystack = " ".join(
                    str(row.get(key) or "") for key in ("name", "item_id", "detail")
                ).casefold()
                if needle not in haystack:
                    continue
                result = dict(row)
                result.update({
                    "account": account,
                    "identity": identity,
                    "updated_at": str(snapshot.get("updated_at") or ""),
                })
                results.append(result)
    account_order = {name: index for index, name in enumerate(INVENTORY_ACCOUNT_IDENTITIES)}
    identity_order = {
        (account, identity): index
        for account, identities in INVENTORY_ACCOUNT_IDENTITIES.items()
        for index, identity in enumerate(identities)
    }
    return sorted(
        results,
        key=lambda row: (
            INVENTORY_TYPE_ORDER.get(str(row.get("type")), len(INVENTORY_TYPES)),
            str(row.get("name") or "").casefold(),
            account_order.get(str(row.get("account")), 99),
            identity_order.get((str(row.get("account")), str(row.get("identity"))), 99),
        ),
    )


class MiniAppInventoryWorker:
    """Consume Dashboard refresh requests through an account-owned cache file."""

    def __init__(
        self,
        actor: Any,
        transport: Any,
        account: str,
        logger: logging.Logger | None = None,
        *,
        base_dir: str | None = None,
        poll_seconds: float = 1.0,
        inter_identity_delay: float = 0.15,
    ) -> None:
        self.actor = actor
        self.transport = transport
        self.account = _account_key(account)
        self.log = logger or logging.getLogger(f"miniapp_inventory.{self.account}")
        self.base_dir = base_dir
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.inter_identity_delay = max(0.0, float(inter_identity_delay))

    def identities(self) -> list[str]:
        configured = INVENTORY_ACCOUNT_IDENTITIES[self.account]
        actor_identities = ["主魂", *(getattr(self.actor, "avatars", []) or [])]
        mapped = getattr(self.transport, "identity_player_ids", {}) or {}
        return [
            identity
            for identity in configured
            if identity in actor_identities and (not mapped or identity in mapped)
        ]

    def _error_code(self, exc: Exception) -> str:
        return str(getattr(exc, "code", "") or type(exc).__name__.lower())

    async def process_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request.get("request_id") or "").strip()
        target = str(request.get("identity") or "").strip()
        cache = read_inventory_cache(self.account, self.base_dir)
        available = self.identities()
        targets = available if target == "*" else [target]
        errors: dict[str, str] = {}
        completed = 0
        progress = {
            "request_id": request_id,
            "identity": target,
            "status": "running",
            "completed": 0,
            "total": len(targets),
            "started_at": _now_text(),
        }
        cache["request_progress"] = progress
        write_inventory_cache(self.account, cache, self.base_dir)

        if not request_id:
            errors[target or "?"] = "missing_request_id"
        elif not targets or any(identity not in available for identity in targets):
            errors[target or "?"] = "unknown_inventory_identity"
        else:
            for index, identity in enumerate(targets):
                try:
                    sections = await self.transport.inventory_sections(identity)
                    cache["snapshots"][identity] = inventory_snapshot(
                        self.account,
                        identity,
                        _mapping(sections).get("inventory"),
                    )
                    completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    code = self._error_code(exc)
                    errors[identity] = code
                    self.log.warning(
                        "[%s | %s] Mini App inventory refresh failed (%s)",
                        self.account,
                        identity,
                        code,
                    )
                progress["completed"] = index + 1
                progress["current_identity"] = identity
                progress["errors"] = dict(errors)
                cache["request_progress"] = dict(progress)
                write_inventory_cache(self.account, cache, self.base_dir)
                if index + 1 < len(targets) and self.inter_identity_delay:
                    await asyncio.sleep(self.inter_identity_delay)

        status = "completed"
        if errors and completed:
            status = "partial"
        elif errors:
            status = "error"
        finished_at = _now_text()
        last_request = {
            "request_id": request_id,
            "identity": target,
            "status": status,
            "completed": completed,
            "total": len(targets),
            "errors": errors,
            "requested_at": str(request.get("requested_at") or ""),
            "requested_by": str(request.get("requested_by") or ""),
            "finished_at": finished_at,
        }
        cache["last_completed_request_id"] = request_id
        cache["last_request"] = last_request
        cache["request_progress"] = dict(last_request)
        write_inventory_cache(self.account, cache, self.base_dir)
        return last_request

    async def run_loop(self) -> None:
        cache = read_inventory_cache(self.account, self.base_dir)
        seen_request_id = str(cache.get("last_completed_request_id") or "")
        while getattr(self.actor, "is_running", True):
            request = read_inventory_request(self.account, self.base_dir)
            request_id = str(request.get("request_id") or "").strip()
            if request_id and request_id != seen_request_id:
                await self.process_request(request)
                seen_request_id = request_id
            await asyncio.sleep(self.poll_seconds)
