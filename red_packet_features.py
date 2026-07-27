"""Shared Telegram red-packet monitoring and dashboard state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta
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
RED_PACKET_BUTTON_TEXTS = (RED_PACKET_BUTTON_TEXT, "抢")
RED_PACKET_NOTIFY_TARGET = "Waaiging"
RED_PACKET_RECEIPT_BOT_IDS = {7900199668, 8547797815, 8757550896}
RED_PACKET_ACCOUNT_NAMES = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_HANDLED_MESSAGE_IDS = 200
MAX_NOTIFIED_RECEIPT_IDS = 200
MAX_DIAGNOSTIC_TEXT_LENGTH = 2000
PENDING_CLAIM_TTL_SECONDS = 120

_AMOUNT_TOKEN = r"(?<![0-9])([0-9]+(?:[,.\s][0-9]{3})*(?:\.[0-9]+)?)(?![0-9])"
_CURRENCY_CODE = r"(?:USDT|USDC|LDC|CNY|RMB|USD|TRX|TON|BNB|ETH|BTC|SOL|DOGE|EUR|GBP|HKD|TWD|JPY|U|元|块|币)(?![A-Za-z])"
_CURRENCY_SYMBOL = r"(?:[¥￥$€£₽₿])"
_CURRENCY = rf"(?:{_CURRENCY_CODE}|{_CURRENCY_SYMBOL})"
_AMOUNT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        rf"(?:红包总金额|红包金额|总金额|总额|金额|单包金额|单个金额|每份|每个)\s*(?:[/|]\s*[^\n\r：:=]{{0,16}})?\s*[：:=]?\s*(?:{_CURRENCY_SYMBOL}\s*)?{_AMOUNT_TOKEN}\s*(?:{_CURRENCY_CODE})?",
        rf"(?:💰|💵|💴|💶|💷|💸)\s*(?:金额\s*)?[：:=]?\s*(?:{_CURRENCY_SYMBOL}\s*)?{_AMOUNT_TOKEN}\s*(?:{_CURRENCY_CODE})?",
        rf"(?:红包|🧧)[^\n\r]{{0,40}}?{_CURRENCY_SYMBOL}\s*{_AMOUNT_TOKEN}",
        rf"(?:红包|🧧)[^\n\r]{{0,40}}?(?:{_CURRENCY_SYMBOL}\s*)?{_AMOUNT_TOKEN}\s*{_CURRENCY_CODE}",
        rf"(?:{_CURRENCY_SYMBOL}\s*)?{_AMOUNT_TOKEN}\s*{_CURRENCY_CODE}[^\n\r]{{0,40}}?(?:红包|🧧)",
        rf"{_CURRENCY_SYMBOL}\s*{_AMOUNT_TOKEN}",
        rf"{_AMOUNT_TOKEN}\s*{_CURRENCY_CODE}",
    )
)
_CLAIM_RECEIPT_PATTERN = re.compile(
    r"🧧\s*恭喜\s+(?P<name>.+?)\s+抢到\s+"
    r"(?P<amount>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<currency>[A-Za-z0-9]+)\s*[！!]",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_REJECTED_CLICK_MARKERS = ("已抢完", "已经抢", "抢过", "失败", "过期", "无效")
_RECEIPT_BOT_USERNAME_PATTERN = re.compile(r"^hantianz+_bot$", re.IGNORECASE)


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


def _delay_text(value: Any) -> str:
    try:
        delay = Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid delay seconds") from None
    if not delay.is_finite() or delay < 0 or delay > Decimal("300"):
        raise ValueError("invalid delay seconds")
    text = format(delay.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _time_text(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if not _TIME_PATTERN.fullmatch(text):
        raise ValueError("invalid schedule time")
    return text


def _identity_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().lstrip("@").casefold()


def extract_claim_receipt(text: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    match = _CLAIM_RECEIPT_PATTERN.search(normalized)
    if not match:
        return None
    try:
        amount = Decimal(match.group("amount"))
    except (InvalidOperation, ValueError):
        return None
    return {
        "name": match.group("name").strip(),
        "amount": amount,
        "currency": match.group("currency").upper(),
    }


def is_within_red_packet_schedule(
    settings: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if not bool(settings.get("schedule_enabled")):
        return True
    start_text = _time_text(settings.get("schedule_start", "00:00"))
    end_text = _time_text(settings.get("schedule_end", "00:00"))
    current = now or datetime.now()
    current_minutes = current.hour * 60 + current.minute
    start_hour, start_minute = (int(part) for part in start_text.split(":"))
    end_hour, end_minute = (int(part) for part in end_text.split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return True
    if start < end:
        return start <= current_minutes < end
    return current_minutes >= start or current_minutes < end


def default_red_packet_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "accounts": [],
        "minimum_amount": "0",
        "delay_seconds": "0",
        "schedule_enabled": False,
        "schedule_start": "00:00",
        "schedule_end": "00:00",
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
            "delay_seconds": _delay_text(source.get("delay_seconds", "0")),
            "schedule_enabled": bool(source.get("schedule_enabled")),
            "schedule_start": _time_text(source.get("schedule_start", "00:00")),
            "schedule_end": _time_text(source.get("schedule_end", "00:00")),
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
    delay_seconds: Any = "0",
    schedule_enabled: bool = False,
    schedule_start: Any = "00:00",
    schedule_end: Any = "00:00",
    updated_by: str = "dashboard",
) -> dict[str, Any]:
    data = normalize_red_packet_settings(
        {
            "enabled": enabled,
            "accounts": accounts,
            "minimum_amount": minimum_amount,
            "delay_seconds": delay_seconds,
            "schedule_enabled": schedule_enabled,
            "schedule_start": schedule_start,
            "schedule_end": schedule_end,
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
    combined = unicodedata.normalize("NFKC", combined)
    for pattern in _AMOUNT_PATTERNS:
        match = pattern.search(combined)
        if not match:
            continue
        try:
            amount_text = re.sub(r"[,\s]", "", match.group(1))
            return Decimal(amount_text)
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
    short = None
    partial = None
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            text = str(getattr(button, "text", "") or "").strip()
            normalized = re.sub(r"[\s\ufe0f🧧🎁🎉]", "", text).strip(
                "[]【】()（）<>《》:：!！.-_"
            )
            if normalized == RED_PACKET_BUTTON_TEXT:
                return button
            if normalized == "抢" and short is None:
                short = button
            if RED_PACKET_BUTTON_TEXT in normalized and partial is None:
                partial = button
    return short or partial


def red_packet_button_metadata(message: Any) -> list[dict[str, Any]]:
    rows = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            raw = getattr(button, "button", None)
            data = getattr(raw, "data", None)
            data_bytes = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
            try:
                data_text = data_bytes.decode("utf-8")[:256] if data_bytes else ""
            except UnicodeDecodeError:
                data_text = ""
            rows.append(
                {
                    "text": str(getattr(button, "text", "") or ""),
                    "type": type(raw).__name__ if raw is not None else type(button).__name__,
                    "url": str(getattr(button, "url", "") or ""),
                    "has_data": bool(data),
                    "data_hex": data_bytes[:128].hex(),
                    "data_text": data_text,
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
            "button_texts": list(RED_PACKET_BUTTON_TEXTS),
            "notify_target": f"@{RED_PACKET_NOTIFY_TARGET}",
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
        self.notify_entity = None
        self.self_names: set[str] = set()
        self._handled: list[int] = []
        self._handled_set: set[int] = set()
        self._notified_receipts: list[int] = []
        self._notified_receipt_set: set[int] = set()
        self._pending_claims: list[dict[str, Any]] = []
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
        receipt_values = status.get("notified_receipt_ids") if isinstance(status, dict) else []
        if isinstance(receipt_values, list):
            for value in receipt_values[-MAX_NOTIFIED_RECEIPT_IDS:]:
                try:
                    receipt_id = int(value)
                except (TypeError, ValueError):
                    continue
                if receipt_id not in self._notified_receipt_set:
                    self._notified_receipts.append(receipt_id)
                    self._notified_receipt_set.add(receipt_id)

    def _remember(self, message_id: int) -> None:
        if message_id in self._handled_set:
            return
        self._handled.append(message_id)
        self._handled_set.add(message_id)
        while len(self._handled) > MAX_HANDLED_MESSAGE_IDS:
            old = self._handled.pop(0)
            self._handled_set.discard(old)

    def _remember_receipt(self, message_id: int) -> None:
        if message_id in self._notified_receipt_set:
            return
        self._notified_receipts.append(message_id)
        self._notified_receipt_set.add(message_id)
        while len(self._notified_receipts) > MAX_NOTIFIED_RECEIPT_IDS:
            old = self._notified_receipts.pop(0)
            self._notified_receipt_set.discard(old)

    def _purge_pending_claims(self) -> None:
        cutoff = datetime.now() - timedelta(seconds=PENDING_CLAIM_TTL_SECONDS)
        self._pending_claims = [
            claim for claim in self._pending_claims if claim.get("created_at") >= cutoff
        ]

    def _register_pending_claim(self, message_id: int, amount: Decimal) -> dict[str, Any]:
        self._purge_pending_claims()
        pending = {
            "message_id": message_id,
            "packet_amount": format(amount, "f"),
            "created_at": datetime.now(),
        }
        self._pending_claims.append(pending)
        return pending

    def _remove_pending_claim(self, pending: dict[str, Any]) -> None:
        if pending in self._pending_claims:
            self._pending_claims.remove(pending)

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
                "notified_receipt_ids": self._notified_receipts,
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
            me = await self.client.get_me()
            identity_values = [
                getattr(me, "username", ""),
                getattr(me, "first_name", ""),
                " ".join(
                    part
                    for part in [getattr(me, "first_name", ""), getattr(me, "last_name", "")]
                    if part
                ),
            ]
            self.self_names = {_identity_key(value) for value in identity_values if value}
            try:
                self.notify_entity = await self.client.get_entity(RED_PACKET_NOTIFY_TARGET)
            except Exception as exc:
                self.log.warning(
                    "[%s] Red-packet notification target @%s could not be resolved: %s",
                    self.account,
                    RED_PACKET_NOTIFY_TARGET,
                    exc,
                )
        except Exception as exc:
            self._write_status(listening=False, last_action="install_error", last_error=str(exc))
            self.log.error("[%s] Red-packet monitor setup failed: %s", self.account, exc)
            return False

        @self.client.on(events.NewMessage(chats=self.entity))
        async def new_message_handler(event: Any) -> None:
            await self.process_receipt(event.message)
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

    async def process_receipt(self, message: Any) -> None:
        if not self._is_target_topic(message):
            return
        receipt = extract_claim_receipt(
            getattr(message, "raw_text", "") or getattr(message, "text", "") or ""
        )
        if receipt is None or _identity_key(receipt["name"]) not in self.self_names:
            return
        sender_id = int(getattr(message, "sender_id", 0) or 0)
        trusted_sender = sender_id in RED_PACKET_RECEIPT_BOT_IDS
        if not trusted_sender and hasattr(message, "get_sender"):
            try:
                sender = await message.get_sender()
                sender_username = str(getattr(sender, "username", "") or "")
                trusted_sender = bool(_RECEIPT_BOT_USERNAME_PATTERN.fullmatch(sender_username))
            except Exception:
                trusted_sender = False
        if not trusted_sender:
            return
        message_id = int(getattr(message, "id", 0) or 0)
        if not message_id or message_id in self._notified_receipt_set:
            return
        self._purge_pending_claims()
        if not self._pending_claims:
            return
        pending = self._pending_claims.pop(0)
        self._remember_receipt(message_id)
        amount_text = format(receipt["amount"], "f")
        currency = receipt["currency"]
        notification = (
            "🧧 自动抢红包成功\n"
            f"账号：{RED_PACKET_ACCOUNT_NAMES[self.account]}\n"
            f"金额：{amount_text} {currency}"
        )
        target = self.notify_entity or RED_PACKET_NOTIFY_TARGET
        last_error = ""
        for attempt, retry_delay in enumerate((0, 2, 5), start=1):
            if retry_delay:
                await asyncio.sleep(retry_delay)
            try:
                await asyncio.wait_for(self.client.send_message(target, notification), timeout=12)
                self._write_status(
                    last_claimed_amount=amount_text,
                    last_claimed_currency=currency,
                    last_claim_receipt_id=message_id,
                    last_claim_message_id=pending["message_id"],
                    last_notification_at=_now_text(),
                    last_notification_error="",
                )
                self.log.warning(
                    "[%s] Red-packet claim notification sent: amount=%s %s receipt=%s",
                    self.account,
                    amount_text,
                    currency,
                    message_id,
                )
                return
            except Exception as exc:
                last_error = str(exc)
                self.log.warning(
                    "[%s] Red-packet notification attempt %s failed: %s",
                    self.account,
                    attempt,
                    exc,
                )
        self._write_status(
            last_claimed_amount=amount_text,
            last_claimed_currency=currency,
            last_claim_receipt_id=message_id,
            last_claim_message_id=pending["message_id"],
            last_notification_error=last_error,
        )

    async def process_message(self, message: Any, *, source: str) -> None:
        if not self._is_target_topic(message):
            return
        button_metadata = red_packet_button_metadata(message)
        button = red_packet_button(message)
        if button is None:
            claim_like = [item for item in button_metadata if "抢" in item["text"]]
            if claim_like:
                settings = load_red_packet_settings()
                if settings["enabled"] and self.account in settings["accounts"]:
                    self.log.warning(
                        "[%s] Claim-like buttons were not recognized: message=%s buttons=%s",
                        self.account,
                        getattr(message, "id", None),
                        claim_like,
                    )
            return

        message_id = int(getattr(message, "id", 0) or 0)
        if not message_id or message_id in self._handled_set or message_id in self._inflight:
            return
        settings = load_red_packet_settings()
        if not settings["enabled"] or self.account not in settings["accounts"]:
            return
        if not is_within_red_packet_schedule(settings):
            self._remember(message_id)
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=None,
                last_source=source,
                last_action="outside_schedule",
                last_schedule_start=settings["schedule_start"],
                last_schedule_end=settings["schedule_end"],
                last_error="",
            )
            self.log.info(
                "[%s] Red packet %s skipped outside schedule %s-%s",
                self.account,
                message_id,
                settings["schedule_start"],
                settings["schedule_end"],
            )
            return

        message_text = str(getattr(message, "raw_text", "") or getattr(message, "text", "") or "")
        button_texts = [item["text"] for item in button_metadata]
        amount = extract_red_packet_amount(message_text, button_texts)
        minimum = Decimal(settings["minimum_amount"])
        delay_seconds = Decimal(settings.get("delay_seconds", "0"))
        if amount is None:
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=None,
                last_source=source,
                last_action="amount_unknown",
                last_message_text=message_text[:MAX_DIAGNOSTIC_TEXT_LENGTH],
                last_buttons=button_metadata,
                last_error="无法从红包消息中解析金额",
            )
            self.log.warning(
                "[%s] Red packet %s skipped: amount unknown text=%r buttons=%s",
                self.account,
                message_id,
                message_text[:MAX_DIAGNOSTIC_TEXT_LENGTH],
                button_metadata,
            )
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
            if delay_seconds > 0:
                scheduled_at = (datetime.now() + timedelta(seconds=float(delay_seconds))).strftime(
                    TIME_FORMAT
                )
                self._write_status(
                    last_seen_at=_now_text(),
                    last_message_id=message_id,
                    last_amount=format(amount, "f"),
                    last_minimum_amount=format(minimum, "f"),
                    last_delay_seconds=format(delay_seconds, "f"),
                    last_scheduled_click_at=scheduled_at,
                    last_source=source,
                    last_action="waiting_delay",
                    last_error="",
                )
                self.log.info(
                    "[%s] Red packet %s waiting %ss before click: amount=%s minimum=%s",
                    self.account,
                    message_id,
                    delay_seconds,
                    amount,
                    minimum,
                )
                await asyncio.sleep(float(delay_seconds))

                current_settings = load_red_packet_settings()
                current_minimum = Decimal(current_settings["minimum_amount"])
                cancel_reason = ""
                if not current_settings["enabled"]:
                    cancel_reason = "自动抢红包已关闭"
                elif self.account not in current_settings["accounts"]:
                    cancel_reason = "账号已取消参与"
                elif amount < current_minimum:
                    cancel_reason = "金额低于最新最低金额"
                elif not is_within_red_packet_schedule(current_settings):
                    cancel_reason = "当前时间已超出生效时段"
                if cancel_reason:
                    self._remember(message_id)
                    self._write_status(
                        last_seen_at=_now_text(),
                        last_message_id=message_id,
                        last_amount=format(amount, "f"),
                        last_minimum_amount=format(current_minimum, "f"),
                        last_delay_seconds=format(delay_seconds, "f"),
                        last_source=source,
                        last_action="delay_cancelled",
                        last_error=cancel_reason,
                    )
                    self.log.info(
                        "[%s] Red packet %s cancelled after delay: %s",
                        self.account,
                        message_id,
                        cancel_reason,
                    )
                    return
                minimum = current_minimum

            pending_claim = self._register_pending_claim(message_id, amount)
            try:
                result = await asyncio.wait_for(button.click(), timeout=12)
            except Exception:
                self._remove_pending_claim(pending_claim)
                raise
            result_message = str(getattr(result, "message", "") or "")
            rejected = any(marker in result_message for marker in _REJECTED_CLICK_MARKERS)
            if rejected:
                self._remove_pending_claim(pending_claim)
            self._remember(message_id)
            self._write_status(
                last_seen_at=_now_text(),
                last_click_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_delay_seconds=format(delay_seconds, "f"),
                last_source=source,
                last_action="claim_rejected" if rejected else "clicked",
                last_button_type=button_type,
                last_result=result_message,
                last_error="",
            )
            self.log.warning(
                "[%s] Red packet %s clicked: amount=%s minimum=%s delay=%ss result=%s",
                self.account,
                message_id,
                amount,
                minimum,
                delay_seconds,
                result_message or "callback sent",
            )
        except Exception as exc:
            self._write_status(
                last_seen_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_delay_seconds=format(delay_seconds, "f"),
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
