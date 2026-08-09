"""Shared Telegram red-packet monitoring and dashboard state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import unicodedata
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telethon import events


CONFIG_DIR = Path(__file__).resolve().parent
RED_PACKET_SETTINGS_FILE = CONFIG_DIR / "red_packet_settings.json"
RED_PACKET_STATUS_TEMPLATE = "red_packet_status_{account}.json"
RED_PACKET_COORDINATION_FILE = "red_packet_claim_coordination.json"
RED_PACKET_COORDINATION_LOCK_FILE = "red_packet_claim_coordination.lock"
RED_PACKET_CHAT = "ja_netfilter_group"
RED_PACKET_ANCHOR_MESSAGE_ID = 458347
RED_PACKET_LINK = f"https://t.me/{RED_PACKET_CHAT}/{RED_PACKET_ANCHOR_MESSAGE_ID}"
RED_PACKET_BUTTON_TEXT = "抢红包"
RED_PACKET_BUTTON_TEXTS = (RED_PACKET_BUTTON_TEXT, "抢")
RED_PACKET_NOTIFY_TARGET = "Waaiging"
RED_PACKET_RECEIPT_BOT_IDS = {7900199668, 8547797815, 8757550896}
# 回执里的名字是 LinuxDo 论坛账户名（"已自动分发到论坛账户"），不是 Telegram
# 昵称/用户名。所有账号都绑定到同一个论坛账户，因此统一接受这些名字。
RED_PACKET_FORUM_CLAIM_NAMES = ("Waaiging",)
RED_PACKET_ACCOUNT_NAMES = {
    "main": "主号",
    "sub": "副号",
    "xiaohao": "小号",
    "waaiging": "Waaiging",
}
RESTRICTED_MINIAPP_STATE_FILES = {
    "xiaohao": "state_xiaohao.json",
    "waaiging": "state_waaiging.json",
}
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_HANDLED_MESSAGE_IDS = 200
MAX_NOTIFIED_RECEIPT_IDS = 200
MAX_DIAGNOSTIC_TEXT_LENGTH = 2000
PENDING_CLAIM_TTL_SECONDS = 120
CLAIM_CONFIRMATION_TIMEOUT_SECONDS = 60
# Cover all three 12-second click attempts so another process can take over if
# the first owner exhausts its retries and releases the reservation.
CLAIM_RESERVATION_WAIT_SECONDS = 40
CLAIM_RESERVATION_POLL_SECONDS = 0.1
CLICK_RETRY_DELAYS = (0, 0.2, 0.5)
CLAIM_CLICK_MIN_COUNT = 3
CLAIM_CLICK_MAX_COUNT = 5
CLAIM_CLICK_INTERVAL_SECONDS = 1
NOTIFICATION_RETRY_DELAYS = (0, 2, 5)
NOTIFICATION_BACKGROUND_RETRY_SECONDS = 60

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
_REJECTED_CLICK_MARKERS = (
    "已抢完",
    "已经抢",
    "抢过",
    "失败",
    "过期",
    "无效",
    "不存在",
    "已结束",
)
_CONFIRMED_CLICK_MARKERS = ("领取成功", "成功领取")
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


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _coordination_path() -> Path:
    return CONFIG_DIR / RED_PACKET_COORDINATION_FILE


def _coordination_lock_path() -> Path:
    return CONFIG_DIR / RED_PACKET_COORDINATION_LOCK_FILE


def _load_coordination_unlocked() -> dict[str, Any]:
    data = _read_json(_coordination_path())
    claims = data.get("claims") if isinstance(data.get("claims"), dict) else {}
    return {"claims": claims}


def _purge_shared_claims(data: dict[str, Any], now: float) -> None:
    cutoff = now - PENDING_CLAIM_TTL_SECONDS
    data["claims"] = {
        str(message_id): claim
        for message_id, claim in data.get("claims", {}).items()
        if isinstance(claim, dict) and float(claim.get("updated_at") or 0) >= cutoff
    }


def _reserve_shared_claim(account: str, message_id: int, amount: Decimal) -> dict[str, Any]:
    now = time.time()
    with _exclusive_file_lock(_coordination_lock_path()):
        data = _load_coordination_unlocked()
        _purge_shared_claims(data, now)
        key = str(message_id)
        claim = data["claims"].get(key)
        if not isinstance(claim, dict):
            claim = {
                "account": account,
                "message_id": message_id,
                "packet_amount": format(amount, "f"),
                "state": "reserved",
                "created_at": now,
                "updated_at": now,
            }
            data["claims"][key] = claim
            _atomic_write_json(_coordination_path(), data)
        return dict(claim)


def _set_shared_claim_state(message_id: int, account: str, state: str) -> None:
    now = time.time()
    with _exclusive_file_lock(_coordination_lock_path()):
        data = _load_coordination_unlocked()
        _purge_shared_claims(data, now)
        claim = data["claims"].get(str(message_id))
        if not isinstance(claim, dict) or claim.get("account") != account:
            return
        claim["state"] = state
        claim["updated_at"] = now
        _atomic_write_json(_coordination_path(), data)


def _release_shared_claim(message_id: int, account: str) -> None:
    with _exclusive_file_lock(_coordination_lock_path()):
        data = _load_coordination_unlocked()
        claim = data["claims"].get(str(message_id))
        if not isinstance(claim, dict) or claim.get("account") != account:
            return
        data["claims"].pop(str(message_id), None)
        _atomic_write_json(_coordination_path(), data)


def _notification_bot_config() -> tuple[str, Any]:
    config = _read_json(CONFIG_DIR / "config.json")
    token = str(config.get("notify_bot_token") or "").strip()
    if not token or "在这里填入" in token:
        return "", ""
    target = config.get("notify_target") or RED_PACKET_NOTIFY_TARGET
    if isinstance(target, str) and target.lstrip("-").isdigit():
        target = int(target)
    return token, target


def _send_notification_bot_sync(bot_token: str, target: Any, text: str, timeout: int = 8) -> None:
    payload = json.dumps({"chat_id": target, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except Exception as exc:
        code = getattr(exc, "code", "")
        suffix = f"_{code}" if code else ""
        raise RuntimeError(f"bot_api_{type(exc).__name__}{suffix}") from None
    if result.get("ok") is not True:
        description = str(result.get("description") or "rejected").strip()
        raise RuntimeError(f"bot_api_rejected: {description[:200]}")


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
        "claim_names": [],
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
    claim_names = source.get("claim_names") if isinstance(source.get("claim_names"), list) else []
    result.update(
        {
            "enabled": bool(source.get("enabled")),
            "accounts": normalized_accounts,
            "claim_names": [str(item).strip() for item in claim_names if str(item).strip()],
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
    claim_names: Any = None,
    updated_by: str = "dashboard",
) -> dict[str, Any]:
    if claim_names is None:
        claim_names = load_red_packet_settings().get("claim_names") or []
    data = normalize_red_packet_settings(
        {
            "enabled": enabled,
            "accounts": accounts,
            "claim_names": claim_names,
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


def load_restricted_miniapp_status(account: str) -> dict[str, Any]:
    filename = RESTRICTED_MINIAPP_STATE_FILES.get(account)
    if not filename:
        return {}
    state = _read_json(CONFIG_DIR / filename)
    return {
        "active": bool(state.get("restricted_miniapp_active")),
        "started_at": str(state.get("restricted_miniapp_started_at") or ""),
        "last_sync_time": str(state.get("restricted_miniapp_last_sync_time") or ""),
        "last_command": str(state.get("restricted_miniapp_last_command") or ""),
        "last_command_at": str(state.get("restricted_miniapp_last_command_at") or ""),
        "last_error": str(state.get("restricted_miniapp_last_error") or ""),
        "identity_count": int(state.get("restricted_miniapp_identity_count") or 0),
        "beast_sync_time": str(state.get("beast_miniapp_last_sync_time") or ""),
    }


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
                "miniapp": load_restricted_miniapp_status(key),
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
        self._pending_notifications: list[dict[str, Any]] = []
        self._notification_retry_task: asyncio.Task[Any] | None = None
        self._claim_confirmation_tasks: dict[int, asyncio.Task[Any]] = {}
        self._notification_lock = asyncio.Lock()
        self._inflight: set[int] = set()
        self._logged_edited_versions: dict[int, tuple[str, str]] = {}
        self._load_handled()

    def _load_handled(self) -> None:
        status = load_red_packet_status(self.account)
        values = status.get("handled_message_ids") if isinstance(status, dict) else []
        if isinstance(values, list):
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

        pending_values = status.get("pending_notifications") if isinstance(status, dict) else []
        if not isinstance(pending_values, list):
            pending_values = []
        if not pending_values and status.get("last_notification_error"):
            pending_values = [{
                "receipt_id": status.get("last_claim_receipt_id"),
                "claim_message_id": status.get("last_claim_message_id"),
                "amount": status.get("last_claimed_amount"),
                "currency": status.get("last_claimed_currency"),
                "created_at": status.get("updated_at") or _now_text(),
            }]
        seen_pending = set()
        for raw in pending_values:
            if not isinstance(raw, dict):
                continue
            try:
                receipt_id = int(raw.get("receipt_id") or 0)
                claim_message_id = int(raw.get("claim_message_id") or 0)
            except (TypeError, ValueError):
                continue
            amount = str(raw.get("amount") or "").strip()
            currency = str(raw.get("currency") or "").strip().upper()
            if receipt_id <= 0 or not amount or not currency or receipt_id in seen_pending:
                continue
            seen_pending.add(receipt_id)
            if receipt_id in self._notified_receipt_set:
                self._notified_receipt_set.discard(receipt_id)
                self._notified_receipts = [
                    value for value in self._notified_receipts if value != receipt_id
                ]
            self._pending_notifications.append({
                "receipt_id": receipt_id,
                "claim_message_id": claim_message_id,
                "amount": amount,
                "currency": currency,
                "created_at": str(raw.get("created_at") or _now_text()),
            })

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

    @staticmethod
    def _notification_text(record: dict[str, Any], account: str) -> str:
        return (
            "🧧 自动抢红包成功\n"
            f"账号：{RED_PACKET_ACCOUNT_NAMES[account]}\n"
            f"金额：{record['amount']} {record['currency']}"
        )

    def _pending_notification_ids(self) -> set[int]:
        return {
            int(item.get("receipt_id") or 0)
            for item in self._pending_notifications
            if isinstance(item, dict)
        }

    def _enqueue_notification(self, record: dict[str, Any]) -> None:
        receipt_id = int(record.get("receipt_id") or 0)
        if receipt_id <= 0 or receipt_id in self._pending_notification_ids():
            return
        self._pending_notifications.append(record)

    def _remove_pending_notification(self, receipt_id: int) -> None:
        self._pending_notifications = [
            item
            for item in self._pending_notifications
            if int(item.get("receipt_id") or 0) != int(receipt_id or 0)
        ]

    def _ensure_notification_retry_task(self, initial_delay: int) -> None:
        if not self._pending_notifications:
            return
        task = self._notification_retry_task
        if task is not None and not task.done():
            return
        try:
            self._notification_retry_task = asyncio.create_task(
                self._notification_retry_loop(initial_delay),
                name=f"red_packet_notify_{self.account}",
            )
        except RuntimeError:
            self._notification_retry_task = None

    async def _send_notification_once(self, notification: str) -> str:
        bot_token, bot_target = _notification_bot_config()
        prefer_bot = self.account in RESTRICTED_MINIAPP_STATE_FILES and bool(bot_token)
        transports = ("bot", "client") if prefer_bot else ("client", "bot")
        errors = []
        for transport in transports:
            if transport == "bot":
                if not bot_token:
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            _send_notification_bot_sync,
                            bot_token,
                            bot_target,
                            notification,
                        ),
                        timeout=12,
                    )
                    return "bot"
                except Exception as exc:
                    errors.append(f"bot={exc}")
                    continue

            if self.client is None:
                continue
            target = self.notify_entity or RED_PACKET_NOTIFY_TARGET
            try:
                await asyncio.wait_for(
                    self.client.send_message(target, notification),
                    timeout=12,
                )
                return "client"
            except Exception as exc:
                errors.append(f"client={exc}")

        raise RuntimeError("; ".join(errors) or "no notification transport configured")

    async def _deliver_notification(
        self,
        record: dict[str, Any],
        retry_delays: tuple[int, ...] = NOTIFICATION_RETRY_DELAYS,
    ) -> bool:
        receipt_id = int(record.get("receipt_id") or 0)
        if receipt_id <= 0 or receipt_id in self._notified_receipt_set:
            self._remove_pending_notification(receipt_id)
            return True
        notification = self._notification_text(record, self.account)
        last_error = ""
        async with self._notification_lock:
            if receipt_id in self._notified_receipt_set:
                self._remove_pending_notification(receipt_id)
                return True
            for attempt, retry_delay in enumerate(retry_delays, start=1):
                if retry_delay:
                    await asyncio.sleep(retry_delay)
                try:
                    transport = await self._send_notification_once(notification)
                    self._remember_receipt(receipt_id)
                    self._remove_pending_notification(receipt_id)
                    self._write_status(
                        last_claimed_amount=record["amount"],
                        last_claimed_currency=record["currency"],
                        last_claim_receipt_id=receipt_id,
                        last_claim_message_id=int(record.get("claim_message_id") or 0),
                        last_notification_at=_now_text(),
                        last_notification_error="",
                    )
                    self.log.warning(
                        "[%s] Red-packet claim notification sent via %s: amount=%s %s receipt=%s",
                        self.account,
                        transport,
                        record["amount"],
                        record["currency"],
                        receipt_id,
                    )
                    return True
                except Exception as exc:
                    last_error = str(exc)
                    self.log.warning(
                        "[%s] Red-packet notification attempt %s failed: %s",
                        self.account,
                        attempt,
                        exc,
                    )
            self._write_status(
                last_claimed_amount=record["amount"],
                last_claimed_currency=record["currency"],
                last_claim_receipt_id=receipt_id,
                last_claim_message_id=int(record.get("claim_message_id") or 0),
                last_notification_error=last_error,
            )
            return False

    async def _notification_retry_loop(self, initial_delay: int = 0) -> None:
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        while self._pending_notifications:
            record = dict(self._pending_notifications[0])
            if await self._deliver_notification(record, retry_delays=(0,)):
                continue
            await asyncio.sleep(NOTIFICATION_BACKGROUND_RETRY_SECONDS)

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

    def _cancel_claim_confirmation(self, message_id: int) -> None:
        task = self._claim_confirmation_tasks.pop(message_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _expire_unconfirmed_claim(self, message_id: int) -> None:
        try:
            await asyncio.sleep(CLAIM_CONFIRMATION_TIMEOUT_SECONDS)
            pending = next(
                (
                    claim
                    for claim in self._pending_claims
                    if int(claim.get("message_id") or 0) == message_id
                ),
                None,
            )
            if pending is None:
                return
            _set_shared_claim_state(message_id, self.account, "unconfirmed")
            status = load_red_packet_status(self.account)
            updates: dict[str, Any] = {
                "last_unconfirmed_message_id": message_id,
                "last_unconfirmed_at": _now_text(),
            }
            if int(status.get("last_message_id") or 0) == message_id:
                updates.update(
                    last_action="claim_unconfirmed",
                    last_error=(
                        f"领取请求已受理，但 {CLAIM_CONFIRMATION_TIMEOUT_SECONDS} 秒内未收到到账回执"
                    ),
                )
            self._write_status(**updates)
            self.log.warning(
                "[%s] Red packet %s was not confirmed within %ss",
                self.account,
                message_id,
                CLAIM_CONFIRMATION_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            return
        finally:
            current = asyncio.current_task()
            if self._claim_confirmation_tasks.get(message_id) is current:
                self._claim_confirmation_tasks.pop(message_id, None)

    def _schedule_claim_confirmation(self, message_id: int) -> None:
        if self.client is None:
            return
        self._cancel_claim_confirmation(message_id)
        try:
            self._claim_confirmation_tasks[message_id] = asyncio.create_task(
                self._expire_unconfirmed_claim(message_id),
                name=f"red_packet_confirmation_{self.account}_{message_id}",
            )
        except RuntimeError:
            self._claim_confirmation_tasks.pop(message_id, None)

    async def _acquire_shared_claim(self, message_id: int, amount: Decimal) -> dict[str, Any]:
        deadline = time.monotonic() + CLAIM_RESERVATION_WAIT_SECONDS
        while True:
            claim = _reserve_shared_claim(self.account, message_id, amount)
            if claim.get("account") == self.account:
                return claim
            if claim.get("state") != "reserved" or time.monotonic() >= deadline:
                return claim
            await asyncio.sleep(CLAIM_RESERVATION_POLL_SECONDS)

    async def _click_with_retry(self, message_id: int, button: Any) -> tuple[Any, str, Any]:
        current_button = button
        last_error: Exception | None = None
        for attempt, retry_delay in enumerate(CLICK_RETRY_DELAYS, start=1):
            if retry_delay:
                await asyncio.sleep(retry_delay)
            try:
                result = await asyncio.wait_for(current_button.click(), timeout=12)
                return (
                    result,
                    type(getattr(current_button, "button", current_button)).__name__,
                    current_button,
                )
            except Exception as exc:
                last_error = exc
                if attempt >= len(CLICK_RETRY_DELAYS) or self.client is None:
                    break
                try:
                    refreshed = await self.client.get_messages(self.entity, ids=message_id)
                    refreshed_button = red_packet_button(refreshed) if refreshed else None
                except Exception as refresh_exc:
                    self.log.warning(
                        "[%s] Red packet %s refresh before retry %s failed: %s",
                        self.account,
                        message_id,
                        attempt + 1,
                        refresh_exc,
                    )
                    continue
                if refreshed_button is None:
                    break
                current_button = refreshed_button
                self.log.warning(
                    "[%s] Red packet %s click failed, retrying with refreshed message: %s",
                    self.account,
                    message_id,
                    exc,
                )
        if last_error is not None:
            raise last_error
        raise RuntimeError("red-packet callback button is unavailable")

    async def _click_burst(
        self,
        message_id: int,
        button: Any,
    ) -> tuple[str, int, int, list[str]]:
        target_count = random.randint(CLAIM_CLICK_MIN_COUNT, CLAIM_CLICK_MAX_COUNT)
        current_button = button
        button_type = type(getattr(button, "button", button)).__name__
        results: list[str] = []

        for click_index in range(target_count):
            if click_index and CLAIM_CLICK_INTERVAL_SECONDS:
                await asyncio.sleep(CLAIM_CLICK_INTERVAL_SECONDS)
            try:
                result, button_type, current_button = await self._click_with_retry(
                    message_id,
                    current_button,
                )
            except Exception as exc:
                if not results:
                    raise
                self.log.warning(
                    "[%s] Red packet %s follow-up click %s/%s failed after submission: %s",
                    self.account,
                    message_id,
                    click_index + 1,
                    target_count,
                    exc,
                )
                break

            result_message = str(getattr(result, "message", "") or "")
            results.append(result_message)
            if any(
                marker in result_message
                for marker in (*_CONFIRMED_CLICK_MARKERS, *_REJECTED_CLICK_MARKERS)
            ):
                break

        return button_type, len(results), target_count, results

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
                "pending_notifications": self._pending_notifications,
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
            self.self_names.update(
                _identity_key(value) for value in RED_PACKET_FORUM_CLAIM_NAMES if value
            )
            extra_names = load_red_packet_settings().get("claim_names") or []
            self.self_names.update(_identity_key(value) for value in extra_names if value)
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
            await self.process_edited_message(event)

        self._write_status(listening=True, last_action="listening", last_error="")
        self._ensure_notification_retry_task(initial_delay=0)
        self.log.info(
            "[%s] Red-packet monitor ready: chat=@%s topic=%s anchor=%s buttons=%s",
            self.account,
            RED_PACKET_CHAT,
            self.topic_id,
            RED_PACKET_ANCHOR_MESSAGE_ID,
            self.anchor_buttons,
        )
        return True

    def _log_edited_bot_message(self, message: Any, sender: Any = None) -> bool:
        if not self._is_target_topic(message):
            return False
        sender_id = int(getattr(message, "sender_id", 0) or 0)
        sender_username = str(getattr(sender, "username", "") or "").strip()
        is_bot = bool(
            getattr(sender, "bot", False)
            or sender_username.lower().endswith("_bot")
            or sender_id in RED_PACKET_RECEIPT_BOT_IDS
            or red_packet_button(message) is not None
        )
        if not is_bot:
            return False

        message_id = int(getattr(message, "id", 0) or 0)
        message_text = str(
            getattr(message, "raw_text", "") or getattr(message, "text", "") or ""
        )
        buttons = red_packet_button_metadata(message)
        buttons_json = json.dumps(buttons, ensure_ascii=False, sort_keys=True)
        version = (message_text, buttons_json)
        if message_id and self._logged_edited_versions.get(message_id) == version:
            return False
        if message_id:
            self._logged_edited_versions[message_id] = version
            if len(self._logged_edited_versions) > 300:
                self._logged_edited_versions = dict(
                    list(self._logged_edited_versions.items())[-150:]
                )

        sender_label = f"@{sender_username}" if sender_username else f"sender:{sender_id}"
        button_suffix = f"\nbuttons={buttons_json}" if buttons else ""
        self.log.info(
            "[%s] Red-packet bot edited message %s from %s:\n%s%s",
            self.account,
            message_id or "unknown",
            sender_label,
            message_text,
            button_suffix,
        )
        return True

    async def process_edited_message(self, event: Any) -> bool:
        message = event.message
        if not self._is_target_topic(message):
            return False
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        logged = self._log_edited_bot_message(message, sender)
        await self.process_receipt(message)
        await self.process_message(message, source="edited")
        return logged

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
        if receipt is None:
            return
        name_matches = _identity_key(receipt["name"]) in self.self_names
        self._purge_pending_claims()
        if not name_matches and not self._pending_claims:
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
        if (
            not message_id
            or message_id in self._notified_receipt_set
            or message_id in self._pending_notification_ids()
        ):
            return
        if not name_matches:
            # 有待确认的点击、回执可信但名字对不上——大概率是论坛账户名变了。
            self.log.warning(
                "[%s] Red-packet receipt name %r is not recognized while a claim is pending; "
                "add it to red_packet_settings.json claim_names if it belongs to this account.",
                self.account,
                receipt["name"],
            )
            return
        self._purge_pending_claims()
        if not self._pending_claims:
            return
        reply = getattr(message, "reply_to", None)
        reply_message_id = int(getattr(reply, "reply_to_msg_id", 0) or 0)
        pending = next(
            (
                claim
                for claim in self._pending_claims
                if reply_message_id != self.topic_id
                and int(claim.get("message_id") or 0) == reply_message_id
            ),
            self._pending_claims[0],
        )
        self._pending_claims.remove(pending)
        claim_message_id = int(pending.get("message_id") or 0)
        self._cancel_claim_confirmation(claim_message_id)
        _set_shared_claim_state(claim_message_id, self.account, "confirmed")
        amount_text = format(receipt["amount"], "f")
        currency = receipt["currency"]
        record = {
            "receipt_id": message_id,
            "claim_message_id": claim_message_id,
            "amount": amount_text,
            "currency": currency,
            "created_at": _now_text(),
        }
        self._enqueue_notification(record)
        self._write_status(
            last_action="claimed",
            last_claimed_amount=amount_text,
            last_claimed_currency=currency,
            last_claim_receipt_id=message_id,
            last_claim_message_id=record["claim_message_id"],
            last_notification_error="",
        )
        if not await self._deliver_notification(record):
            self._ensure_notification_retry_task(
                initial_delay=NOTIFICATION_BACKGROUND_RETRY_SECONDS,
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
                settings = current_settings

            shared_claim_reserved = len(settings["accounts"]) > 1
            if shared_claim_reserved:
                shared_claim = await self._acquire_shared_claim(message_id, amount)
                if shared_claim.get("account") != self.account:
                    owner = str(shared_claim.get("account") or "")
                    self._remember(message_id)
                    self._write_status(
                        last_seen_at=_now_text(),
                        last_message_id=message_id,
                        last_amount=format(amount, "f"),
                        last_minimum_amount=format(minimum, "f"),
                        last_delay_seconds=format(delay_seconds, "f"),
                        last_source=source,
                        last_action="claimed_by_other_account",
                        last_claim_owner=owner,
                        last_error="",
                    )
                    self.log.info(
                        "[%s] Red packet %s delegated to account %s",
                        self.account,
                        message_id,
                        owner,
                    )
                    return

            pending_claim = self._register_pending_claim(message_id, amount)
            try:
                button_type, click_count, click_target, click_results = await self._click_burst(
                    message_id,
                    button,
                )
            except Exception:
                self._remove_pending_claim(pending_claim)
                if shared_claim_reserved:
                    _release_shared_claim(message_id, self.account)
                raise
            confirmed_results = [
                message
                for message in click_results
                if any(marker in message for marker in _CONFIRMED_CLICK_MARKERS)
            ]
            accepted_results = [
                message
                for message in click_results
                if not any(marker in message for marker in _REJECTED_CLICK_MARKERS)
            ]
            confirmed = bool(confirmed_results)
            rejected = not accepted_results and not confirmed
            result_message = (
                confirmed_results[-1]
                if confirmed_results
                else accepted_results[0]
                if accepted_results
                else click_results[-1]
            )
            if rejected:
                self._remove_pending_claim(pending_claim)
                if shared_claim_reserved:
                    _set_shared_claim_state(message_id, self.account, "closed")
            elif confirmed:
                self._remove_pending_claim(pending_claim)
                if shared_claim_reserved:
                    _set_shared_claim_state(message_id, self.account, "confirmed")
            else:
                if shared_claim_reserved:
                    _set_shared_claim_state(message_id, self.account, "awaiting_receipt")
            self._remember(message_id)
            self._write_status(
                last_seen_at=_now_text(),
                last_click_at=_now_text(),
                last_message_id=message_id,
                last_amount=format(amount, "f"),
                last_minimum_amount=format(minimum, "f"),
                last_delay_seconds=format(delay_seconds, "f"),
                last_source=source,
                last_action=(
                    "claim_rejected" if rejected else "claimed" if confirmed else "claim_pending"
                ),
                last_button_type=button_type,
                last_click_count=click_count,
                last_click_target=click_target,
                last_click_interval_seconds=CLAIM_CLICK_INTERVAL_SECONDS,
                last_click_results=click_results,
                last_result=result_message,
                last_error="",
            )
            if not rejected and not confirmed:
                self._schedule_claim_confirmation(message_id)
            self.log.warning(
                "[%s] Red packet %s claim submitted: amount=%s minimum=%s delay=%ss "
                "clicks=%s/%s interval=%ss result=%s",
                self.account,
                message_id,
                amount,
                minimum,
                delay_seconds,
                click_count,
                click_target,
                CLAIM_CLICK_INTERVAL_SECONDS,
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
        if self._notification_retry_task is not None:
            self._notification_retry_task.cancel()
        for task in self._claim_confirmation_tasks.values():
            task.cancel()
        self._claim_confirmation_tasks.clear()
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
