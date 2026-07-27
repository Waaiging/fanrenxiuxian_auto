"""Shared Telegram red-packet monitoring and dashboard state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telethon import events


CONFIG_DIR = Path(__file__).resolve().parent
RED_PACKET_SETTINGS_FILE = CONFIG_DIR / "red_packet_settings.json"
RED_PACKET_STATUS_TEMPLATE = "red_packet_status_{account}.json"
RED_PACKET_CHAT = "ja_netfilter_group"
RED_PACKET_ANCHOR_MESSAGE_ID = 458347
RED_PACKET_LINK = f"https://t.me/{RED_PACKET_CHAT}/{RED_PACKET_ANCHOR_MESSAGE_ID}"
RED_PACKET_BUTTON_TEXT = "抢红包"
RED_PACKET_ACCOUNT_NAMES = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_HANDLED_MESSAGE_IDS = 200

_AMOUNT_TOKEN = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
_CURRENCY = r"(?:USDT|LDC|CNY|RMB|USD|TRX|TON|U|元)"
_AMOUNT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"(?:红包总金额|红包金额|总金额|总额|金额|单包金额|单个金额|每份)\s*[：:=]?\s*(?:[¥￥$]\s*)?{_AMOUNT_TOKEN}\s*{_CURRENCY}?",
        rf"(?:红包|🧧)[^\n\r]{{0,28}}?(?:[¥￥$]\s*)?{_AMOUNT_TOKEN}\s*{_CURRENCY}",
        rf"(?:[¥￥$]\s*)?{_AMOUNT_TOKEN}\s*{_CURRENCY}[^\n\r]{{0,28}}?(?:红包|🧧)",
    )
)


def _now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _decimal_text(value: Any) -> str:
    try:
        amount = Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid minimum amount") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000"):
        raise ValueError("invalid minimum amount")
    text = format(amount.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def default_red_packet_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "accounts": [],
        "minimum_amount": "0",
        "updated_at": "",
        "updated_by": "",
    }


def normalize_red_packet_settings(data: Any) -> dict[str, Any]:
    source = data if isinstance(data, dict) else {}
    accounts = source.get("accounts") if isinstance(source.get("accounts"), list) else []
    normalized_accounts = [
        account for account in RED_PACKET_ACCOUNT_NAMES if account in set(str(item) for item in accounts)
    ]
    result = default_red_packet_settings()
    result.update(
        {
            "enabled": bool(source.get("enabled")),
            "accounts": normalized_accounts,
            "minimum_amount": _decimal_text(source.get("minimum_amount", "0")),
            "updated_at": str(source.get("updated_at") or ""),
            "updated_by": str(source.get("updated_by") or ""),
        }
    )
    return result


def load_red_packet_settings() -> dict[str, Any]:
    try:
        return normalize_red_packet_settings(_read_json(RED_PACKET_SETTINGS_FILE))
    except ValueError:
        return default_red_packet_settings()


def save_red_packet_settings(
    *,
    enabled: bool,
    accounts: list[str],
    minimum_amount: Any,
    updated_by: str = "dashboard",
) -> dict[str, Any]:
    data = normalize_red_packet_settings(
        {
            "enabled": enabled,
            "accounts": accounts,
            "minimum_amount": minimum_amount,
            "updated_at": _now_text(),
            "updated_by": updated_by,
        }
    )
    if data["enabled"] and not data["accounts"]:
        raise ValueError("at least one account is required")
    _atomic_write_json(RED_PACKET_SETTINGS_FILE, data)
    return data


def extract_red_packet_amount(text: str, button_texts: list[str] | None = None) -> Decimal | None:
    combined = "\n".join(
        part for part in [str(text or ""), *(str(item or "") for item in (button_texts or []))] if part
    )
    for pattern in _AMOUNT_PATTERNS:
        match = pattern.search(combined)
        if not match:
            continue
        try:
            return Decimal(match.group(1).replace(",", ""))
        except (InvalidOperation, ValueError):
            continue
    return None


def message_topic_id(message: Any, *, root_fallback: bool = False) -> int | None:
    reply = getattr(message, "reply_to", None)
    top_id = getattr(reply, "reply_to_top_id", None)
    if top_id:
        return int(top_id)
    if reply is not None and getattr(reply, "forum_topic", False):
        reply_id = getattr(reply, "reply_to_msg_id", None)
        if reply_id:
            return int(reply_id)
    if root_fallback:
        message_id = getattr(message, "id", None)
        return int(message_id) if message_id else None
    return None


def red_packet_button(message: Any) -> Any | None:
    partial = None
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            text = str(getattr(button, "text", "") or "").strip()
            if text == RED_PACKET_BUTTON_TEXT:
                return button
            if RED_PACKET_BUTTON_TEXT in text and partial is None:
                partial = button
    return partial


def red_packet_button_metadata(message: Any) -> list[dict[str, Any]]:
    rows = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            raw = getattr(button, "button", None)
            rows.append(
                {
                    "text": str(getattr(button, "text", "") or ""),
                    "type": type(raw).__name__ if raw is not None else type(button).__name__,
                    "url": str(getattr(button, "url", "") or ""),
                    "has_data": bool(getattr(raw, "data", None)),
                }
            )
    return rows


def _status_path(account: str) -> Path:
    return CONFIG_DIR / RED_PACKET_STATUS_TEMPLATE.format(account=account)


def load_red_packet_status(account: str) -> dict[str, Any]:
    if account not in RED_PACKET_ACCOUNT_NAMES:
        return {}
    return _read_json(_status_path(account))


def red_packet_dashboard_payload() -> dict[str, Any]:
    settings = load_red_packet_settings()
    return {
        "settings": settings,
        "target": {
            "chat": f"@{RED_PACKET_CHAT}",
            "anchor_message_id": RED_PACKET_ANCHOR_MESSAGE_ID,
            "link": RED_PACKET_LINK,
            "button_text": RED_PACKET_BUTTON_TEXT,
        },
        "accounts": [
            {
                "key": key,
                "name": name,
                "selected": key in settings["accounts"],
                "status": load_red_packet_status(key),
            }
            for key, name in RED_PACKET_ACCOUNT_NAMES.items()
        ],
        "server_time": _now_text(),
    }


class RedPacketMonitor:
    def __init__(self, client: Any, account: str, logger: logging.Logger | None = None):
        if account not in RED_PACKET_ACCOUNT_NAMES:
            raise ValueError(f"unknown red-packet account: {account}")
        self.client = client
        self.account = account
        self.log = logger or logging.getLogger(__name__)
        self.entity = None
        self.chat_id: int | None = None
        self.topic_id: int | None = None
        self.anchor_buttons: list[dict[str, Any]] = []
        self.anchor_missing = False
        self._handled: list[int] = []
        self._handled_set: set[int] = set()
        self._inflight: set[int] = set()
        self._load_handled()

    def _load_handled(self) -> None:
        status = load_red_packet_status(self.account)
        values = status.get("handled_message_ids") if isinstance(status, dict) else []
        if not isinstance(values, list):
            return
        for value in values[-MAX_HANDLED_MESSAGE_IDS:]:
            try:
                message_id = int(value)
            except (TypeError, ValueError):
                continue
            if message_id not in self._handled_set:
                self._handled.append(message_id)
                self._handled_set.add(message_id)

    def _remember(self, message_id: int) -> None:
        if message_id in self._handled_set:
            return
        self._handled.append(message_id)
        self._handled_set.add(message_id)
        while len(self._handled) > MAX_HANDLED_MESSAGE_IDS:
            old = self._handled.pop(0)
            self._handled_set.discard(old)

    def _write_status(self, **updates: Any) -> None:
        status = load_red_packet_status(self.account)
        status.update(
            {
                "account": self.account,
                "account_name": RED_PACKET_ACCOUNT_NAMES[self.account],
                "chat": f"@{RED_PACKET_CHAT}",
                "chat_id": self.chat_id,
                "topic_id": self.topic_id,
                "anchor_message_id": RED_PACKET_ANCHOR_MESSAGE_ID,
                "anchor_buttons": self.anchor_buttons,
                "anchor_missing": self.anchor_missing,
                "handled_message_ids": self._handled,
                "updated_at": _now_text(),
            }
        )
        status.update(updates)
        try:
            _atomic_write_json(_status_path(self.account), status)
        except OSError as exc:
            self.log.warning("[%s] Red-packet status write failed: %s", self.account, exc)

    async def install(self) -> bool:
        try:
            self.entity = await self.client.get_entity(RED_PACKET_CHAT)
            self.chat_id = int(getattr(self.entity, "id", 0) or 0)
            if bool(getattr(self.entity, "left", False)):
                raise RuntimeError(f"account is not a member of @{RED_PACKET_CHAT}")
            anchor = await self.client.get_messages(self.entity, ids=RED_PACKET_ANCHOR_MESSAGE_ID)
            if not anchor:
                recent_messages = await self.client.get_messages(self.entity, limit=100)
                topic_confirmed = any(
                    message_topic_id(message) == RED_PACKET_ANCHOR_MESSAGE_ID
                    for message in recent_messages
                    if message
                )
                if not topic_confirmed:
                    raise RuntimeError(
                        f"deleted topic root {RED_PACKET_ANCHOR_MESSAGE_ID} could not be confirmed"
                    )
                self.topic_id = RED_PACKET_ANCHOR_MESSAGE_ID
                self.anchor_missing = True
            else:
                self.topic_id = message_topic_id(anchor, root_fallback=True)
                self.anchor_buttons = red_packet_button_metadata(anchor)
            if not self.topic_id:
                raise RuntimeError("target topic id could not be resolved from the anchor message")
        except Exception as exc:
            self._write_status(listening=False, last_action="install_error", last_error=str(exc))
            self.log.error("[%s] Red-packet monitor setup failed: %s", self.account, exc)
            return False

        @self.client.on(events.NewMessage(chats=self.entity))
        async def new_message_handler(event: Any) -> None:
            await self.process_message(event.message, source="new")

        @self.client.on(events.MessageEdited(chats=self.entity))
        async def edited_message_handler(event: Any) -> None:
            await self.process_message(event.message, source="edited")

        self._write_status(listening=True, last_action="listening", last_error="")
        self.log.info(
            "[%s] Red-packet monitor ready: chat=@%s topic=%s anchor=%s buttons=%s",
            self.account,
            RED_PACKET_CHAT,
            self.topic_id,
            RED_PACKET_ANCHOR_MESSAGE_ID,
            self.anchor_buttons,
        )
        return True

    def _is_target_topic(self, message: Any) -> bool:
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id == self.topic_id:
            return True
        return message_topic_id(message) == self.topic_id

    async def process_message(self, message: Any, *, source: str) -> None:
        if not self._is_target_topic(message):
            return
        button = red_packet_button(message)
        if button is None:
            return

        message_id = int(getattr(message, "id", 0) or 0)
        if not message_id or message_id in self._handled_set or message_id in self._inflight:
            return
        settings = load_red_packet_settings()
        if not settings["enabled"] or self.account not in settings["accounts"]:
            return

        button_texts = [item["text"] for item in red_packet_button_metadata(message)]
        amount = extract_red_packet_amount(getattr(message, "raw_text", "") or getattr(message, "text", ""), button_texts)
        minimum = Decimal(settings["minimum_amount"])
        if amount is None:
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=None,
                last_source=source,
                last_action="amount_unknown",
                last_error="无法从红包消息中解析金额",
            )
            self.log.warning("[%s] Red packet %s skipped: amount unknown", self.account, message_id)
            return
        if amount < minimum:
            self._remember(message_id)
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_source=source,
                last_action="below_minimum",
                last_error="",
            )
            self.log.info(
                "[%s] Red packet %s skipped: amount=%s minimum=%s",
                self.account,
                message_id,
                amount,
                minimum,
            )
            return

        raw_button = getattr(button, "button", None)
        button_type = type(raw_button).__name__ if raw_button is not None else type(button).__name__
        if "Callback" not in button_type:
            self._remember(message_id)
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_source=source,
                last_action="unsupported_button",
                last_button_type=button_type,
                last_error=f"暂不支持 {button_type} 按钮",
            )
            self.log.warning(
                "[%s] Red packet %s has unsupported button type %s",
                self.account,
                message_id,
                button_type,
            )
            return

        self._inflight.add(message_id)
        try:
            result = await asyncio.wait_for(button.click(), timeout=12)
            result_message = str(getattr(result, "message", "") or "")
            self._remember(message_id)
            self._write_status(
                last_seen_at=_now_text(),
                last_click_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_source=source,
                last_action="clicked",
                last_button_type=button_type,
                last_result=result_message,
                last_error="",
            )
            self.log.warning(
                "[%s] Red packet %s clicked: amount=%s minimum=%s result=%s",
                self.account,
                message_id,
                amount,
                minimum,
                result_message or "callback sent",
            )
        except Exception as exc:
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_source=source,
                last_action="click_error",
                last_button_type=button_type,
                last_error=str(exc),
            )
            self.log.error("[%s] Red packet %s click failed: %s", self.account, message_id, exc)
        finally:
            self._inflight.discard(message_id)

    def mark_stopped(self, action: str = "stopped") -> None:
        self._write_status(listening=False, last_action=action)


async def install_red_packet_monitor(
    client: Any,
    account: str,
    logger: logging.Logger | None = None,
) -> RedPacketMonitor:
    monitor = RedPacketMonitor(client, account, logger=logger)
    await monitor.install()
    setattr(client, "_red_packet_monitor", monitor)
    return monitor
