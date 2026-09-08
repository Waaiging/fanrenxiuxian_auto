#!/usr/bin/env python3
"""
【自动回复功能模块 —— 所有账号脚本共享】

在游戏机器人发出特定消息时，脚本自动进行被动回复。
目前支持的自动回复场景：
  1. 南陇侯交换 —— 当游戏机器人提到本账号/化身且给出".交换"选项时，按身份自动回复
  2. 神秘商人 —— 看到目标商品时自动查看货品并购买优先物品

【阅读导览】
- is_auto_reply_followup：判断某条消息是否属于自动回复链，防止被普通匹配误处理。
- maybe_auto_reply_merchant：神秘商人购买逻辑，带文件锁和事件去重。
- maybe_auto_reply_exchange：南陇侯交换逻辑，必要时先安置/召回侍妾。
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

from log_utils import (
    COMMAND_CONTROL_FILE,
    actor_message_target,
    command_send_allowed,
    format_in_log,
    is_game_bot_sender,
    meaningful_reply_to_msg_id,
    normalize_telegram_chat_id,
    record_command_response_for_command_id,
    record_command_sent,
    remember_script_send_intent,
    remember_script_sent_message,
    schedule_command_auto_delete,
    send_text_alert,
)


# =====================================================================
# 常量定义
# =====================================================================
EXCHANGE_MAIN_COMMAND = ".交换 法宝"
EXCHANGE_AVATAR_COMMAND = ".交换 法宝"
CONCUBINE_PLACE_COMMAND = ".安置侍妾"
CONCUBINE_RECALL_COMMAND = ".召回侍妾"
EXCHANGE_CONCUBINE_DELAY_SECONDS = 5
EXCHANGE_SETTLEMENT_RETRY_LIMIT = 3
EXCHANGE_SETTLEMENT_FIRST_WAIT_SECONDS = 60
EXCHANGE_EVENT_TTL_SECONDS = 10 * 60
EXCHANGE_EVENT_SAFETY_MARGIN_SECONDS = 20
EXCHANGE_DELAY_RANGE_SECONDS = (60, 75)
EXCHANGE_STATE_KEY = "exchange_auto_events"
RESTRICTED_EXCHANGE_PLACE_STATE_KEY = "restricted_exchange_place_events"
RESTRICTED_EXCHANGE_ACCOUNTS = {"xiaohao", "waaiging"}
RESTRICTED_EXCHANGE_PLACE_SUCCESS_STATUSES = {"placed", "not_applicable", "skipped_recent"}
MERCHANT_LOOK_COMMAND = ".查看货品"
MERCHANT_BUY_COMMAND_PREFIX = ".购买商品"
MERCHANT_PRIORITY_ITEMS = ("掌天瓶的仿制品", "九天息壤", "尘封的储物袋")
MERCHANT_STATE_FILE = os.path.join(os.path.dirname(COMMAND_CONTROL_FILE), "merchant_auto_reply_state.json")


# =====================================================================
# 内部工具函数
# =====================================================================

def _logger(actor):
    """获取当前账号实例的日志记录器"""
    return logging.getLogger(actor.__class__.__name__)


def _normalized_text(text):
    """去除多余空白，标准化消息文本"""
    return re.sub(r"\s+", " ", text or "").strip()


def _token_mentioned(text, token):
    """判断文本是否明确提到某个用户名/别名。"""
    token = str(token or "").lower().lstrip("@").strip()
    if not token:
        return False
    lower_text = (text or "").lower()
    return (
        f"@{token}" in lower_text
        or f"【{token}】" in lower_text
        or re.search(rf"(?<![a-z0-9_]){re.escape(token)}(?![a-z0-9_])", lower_text)
    )


def _mentions_self(actor, msg, text):
    """
    判断消息是否提到了本账号或下属的化身。
    如果提到主号（通过 username 或 ID），返回 "主魂"。
    如果提到化身，返回化身名称。
    否则返回 False。
    """
    me = getattr(actor, "my_info", None)
    text = text or ""
    lower_text = text.lower()
    
    # 1. 检查是否提到了化身的真实名字
    avatars = getattr(actor, "avatars", [])
    for avatar in avatars:
        if avatar and (avatar in text or f"【{str(avatar).lower()}】" in lower_text):
            return avatar

    # 2. 检查是否提到了化身的 username（如 @hajiimiii / 【hajiimiii】）
    avatar_usernames = getattr(actor, "avatar_usernames", {})
    for uname, avatar in avatar_usernames.items():
        if _token_mentioned(text, uname):
            return avatar

    # 3. 检查显式身份用户名映射（主魂和后续可能补充的化身别名）
    identity_usernames = getattr(actor, "identity_usernames", {}) or {}
    if isinstance(identity_usernames, dict):
        for identity, values in identity_usernames.items():
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                if _token_mentioned(text, value):
                    return str(identity or "").strip() or "主魂"

    if not me:
        return False

    username = (getattr(me, "username", "") or "").lower().lstrip("@")
    if _token_mentioned(text, username):
        return "主魂"

    my_id = getattr(me, "id", None)
    if my_id:
        for entity in getattr(msg, "entities", None) or []:
            if getattr(entity, "user_id", None) == my_id:
                return "主魂"

    return False


def exchange_command_for_identity(identity):
    """南陇侯交换统一回复 .交换 法宝（2026-09-07 用户指定，主魂化身一致）。"""
    return EXCHANGE_MAIN_COMMAND if (identity or "主魂") == "主魂" else EXCHANGE_AVATAR_COMMAND


def is_exchange_teaser_text(text):
    clean = str(text or "").replace("**", "")
    return "南陇侯" in clean and "强横神念" in clean


def is_exchange_offer_text(text):
    clean = _normalized_text(str(text or "").replace("**", ""))
    return "南陇侯" in clean and ".交换" in clean


def is_exchange_settlement_text(text):
    clean = str(text or "").replace("**", "")
    return (
        "南陇侯的交易" in clean
        and "选择将侍妾" in clean
        and "作为回报" in clean
    )


def _exchange_state(actor):
    state = getattr(actor, "state", None)
    if not isinstance(state, dict):
        state = {}
        setattr(actor, "state", state)
    events = state.setdefault(EXCHANGE_STATE_KEY, {})
    return events if isinstance(events, dict) else {}


def _save_exchange_state(actor):
    events = _exchange_state(actor)
    if len(events) > 80:
        for old_key in list(events.keys())[:-50]:
            events.pop(old_key, None)
    save_state = getattr(actor, "save_state", None)
    if callable(save_state):
        save_state()


def _update_exchange_state(actor, event_key, **values):
    events = _exchange_state(actor)
    entry = events.get(str(event_key)) if isinstance(events.get(str(event_key)), dict) else {}
    entry.update(values)
    entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events[str(event_key)] = entry
    _save_exchange_state(actor)
    return entry


def _restricted_exchange_place_state(actor):
    state = getattr(actor, "state", None)
    if not isinstance(state, dict):
        state = {}
        setattr(actor, "state", state)
    events = state.get(RESTRICTED_EXCHANGE_PLACE_STATE_KEY)
    if not isinstance(events, dict):
        events = {}
        state[RESTRICTED_EXCHANGE_PLACE_STATE_KEY] = events
    return events


def _update_restricted_exchange_place_state(actor, event_key, **values):
    events = _restricted_exchange_place_state(actor)
    key = str(event_key or "")
    entry = events.get(key) if isinstance(events.get(key), dict) else {}
    entry.update(values)
    entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events[key] = entry
    if len(events) > 80:
        for old_key in list(events.keys())[:-50]:
            events.pop(old_key, None)
    save_state = getattr(actor, "save_state", None)
    if callable(save_state):
        save_state()
    return entry


def _restricted_exchange_place_lock(actor):
    lock = getattr(actor, "_restricted_exchange_place_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(actor, "_restricted_exchange_place_lock", lock)
    return lock


def _recent_restricted_exchange_place_event(actor, identity, current_key):
    now = datetime.now()
    for event_key, entry in reversed(list(_restricted_exchange_place_state(actor).items())):
        if str(event_key) == str(current_key) or not isinstance(entry, dict):
            continue
        if (
            entry.get("status") not in {"placed", "not_applicable"}
            or entry.get("identity") != identity
        ):
            continue
        try:
            placed_at = datetime.strptime(
                str(
                    entry.get("placed_at")
                    or entry.get("completed_at")
                    or entry.get("updated_at")
                    or ""
                ),
                "%Y-%m-%d %H:%M:%S",
            )
        except (TypeError, ValueError):
            continue
        if 0 <= (now - placed_at).total_seconds() <= EXCHANGE_EVENT_TTL_SECONDS:
            return str(event_key)
    return ""


def _exchange_deadline(entry):
    try:
        return datetime.strptime(str(entry.get("deadline_at") or ""), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now()


def _exchange_seconds_left(entry):
    return max(0, int((_exchange_deadline(entry) - datetime.now()).total_seconds()))


def _exchange_waiters(actor):
    waiters = getattr(actor, "_exchange_settlement_waiters", None)
    if waiters is None:
        waiters = {}
        setattr(actor, "_exchange_settlement_waiters", waiters)
    return waiters


def _exchange_step_waiters(actor):
    waiters = getattr(actor, "_exchange_step_waiters", None)
    if waiters is None:
        waiters = {}
        setattr(actor, "_exchange_step_waiters", waiters)
    return waiters


def is_merchant_event_text(text):
    clean = str(text or "").replace("**", "")
    return "【天机异动" in clean and "异界商人" in clean and ".查看货品" in clean


def parse_merchant_goods(text):
    clean = str(text or "").replace("**", "").replace("`", "")
    goods = []
    for line in clean.splitlines():
        match = re.match(r"\s*(\d+)\s*[.、]\s*([^(\n（]+)", line)
        if not match:
            continue
        goods.append({
            "number": int(match.group(1)),
            "name": match.group(2).strip(),
        })
    return goods


def merchant_purchase_plan(text):
    goods = parse_merchant_goods(text)
    by_name = {item["name"]: item["number"] for item in goods if item.get("name")}
    plan = []
    for name in MERCHANT_PRIORITY_ITEMS:
        number = by_name.get(name)
        if number:
            plan.append((name, number))
    return plan


def _merchant_lock_path():
    return f"{MERCHANT_STATE_FILE}.lock"


def _acquire_merchant_lock(timeout=5):
    deadline = time.time() + max(0.1, float(timeout or 0.1))
    lock_path = _merchant_lock_path()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
            return fd, lock_path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return None, lock_path
            time.sleep(0.05)


def _release_merchant_lock(lock):
    if not lock:
        return
    fd, lock_path = lock
    try:
        if fd is not None:
            os.close(fd)
    except OSError:
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass


def _load_merchant_state():
    try:
        with open(MERCHANT_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_merchant_state(data):
    os.makedirs(os.path.dirname(MERCHANT_STATE_FILE), exist_ok=True)
    tmp = f"{MERCHANT_STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MERCHANT_STATE_FILE)


def claim_merchant_event(actor, msg_id):
    """领取一次商人事件处理权。

    多个脚本同时看到同一条商人消息时，只允许一个脚本继续购买，避免重复下单。
    """
    account = str(getattr(actor, "account_key", "") or actor.__class__.__name__)
    key = str(msg_id or "")
    if not key:
        return False
    lock = _acquire_merchant_lock()
    try:
        data = _load_merchant_state()
        events = data.setdefault("events", {})
        entry = events.get(key)
        if isinstance(entry, dict) and entry.get("status") in {"claimed", "looked", "purchased", "done"}:
            return False
        events[key] = {
            "status": "claimed",
            "account": account,
            "claimed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_merchant_state(data)
        return True
    finally:
        _release_merchant_lock(lock)


def update_merchant_event(msg_id, **values):
    key = str(msg_id or "")
    if not key:
        return
    lock = _acquire_merchant_lock()
    try:
        data = _load_merchant_state()
        events = data.setdefault("events", {})
        entry = events.get(key) if isinstance(events.get(key), dict) else {}
        entry.update(values)
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        events[key] = entry
        if len(events) > 200:
            for old_key in list(events.keys())[:-120]:
                events.pop(old_key, None)
        _save_merchant_state(data)
    finally:
        _release_merchant_lock(lock)


def _response_text(response):
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    return str(getattr(response, "text", "") or getattr(response, "raw_text", "") or "")


def _event_message_key(message):
    """Use chat plus message ID because Telegram IDs are chat-local."""
    message_id = getattr(message, "id", 0) or 0
    chat_id = normalize_telegram_chat_id(getattr(message, "chat_id", None))
    return f"{chat_id}:{message_id}" if chat_id is not None else str(message_id)


async def maybe_restricted_exchange_place(actor, event, text=None, sender=None, source="new"):
    """受限小号的南陇侯兜底：只经 Mini App 立即安置侍妾。"""
    account = str(getattr(actor, "account_key", "") or "").strip().lower()
    if account not in RESTRICTED_EXCHANGE_ACCOUNTS:
        return False

    msg = event.message
    text = text if text is not None else (getattr(msg, "text", "") or "")
    if not (is_exchange_teaser_text(text) or is_exchange_offer_text(text)):
        return False

    if sender is None:
        sender = await event.get_sender()
    if not is_game_bot_sender(actor, sender):
        return False

    identity = _mentions_self(actor, msg, text)
    if not identity:
        return False
    identity = "主魂" if identity == "main" else identity
    event_key = _event_message_key(msg)
    if not event_key:
        return False

    async with _restricted_exchange_place_lock(actor):
        events = _restricted_exchange_place_state(actor)
        existing = events.get(event_key) if isinstance(events.get(event_key), dict) else {}
        if existing.get("status") in RESTRICTED_EXCHANGE_PLACE_SUCCESS_STATUSES:
            return True
        if existing.get("status") == "sending":
            return True

        recent_key = _recent_restricted_exchange_place_event(actor, identity, event_key)
        if recent_key:
            _update_restricted_exchange_place_state(
                actor,
                event_key,
                status="skipped_recent",
                identity=identity,
                source=source,
                message_text=str(text or "")[:500],
                covered_by_event=recent_key,
            )
            _logger(actor).info(
                "Restricted exchange placement already completed for %s by event %s; "
                "skipping message %s.",
                identity,
                recent_key,
                event_key,
            )
            return True

        _update_restricted_exchange_place_state(
            actor,
            event_key,
            status="sending",
            identity=identity,
            source=source,
            message_text=str(text or "")[:500],
            detected_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        sender_fn = getattr(actor, "send_and_wait_feedback_identity", None)
        if not callable(sender_fn):
            _update_restricted_exchange_place_state(
                actor,
                event_key,
                status="failed",
                error="miniapp_identity_sender_unavailable",
            )
            _logger(actor).error(
                "Restricted exchange placement unavailable for %s (message %s): "
                "Mini App identity sender is missing.",
                identity,
                event_key,
            )
            return True

        try:
            response = await sender_fn(
                identity,
                CONCUBINE_PLACE_COMMAND,
                timeout=45,
                max_retries=1,
                suppress_no_response_alert=True,
                return_response_msg=True,
            )
        except Exception as exc:
            _update_restricted_exchange_place_state(
                actor,
                event_key,
                status="failed",
                error=str(exc)[:300],
            )
            _logger(actor).error(
                "Restricted exchange placement failed for %s (message %s): %s",
                identity,
                event_key,
                exc,
                exc_info=True,
            )
            await send_text_alert(
                actor,
                "南陇侯安置失败",
                f"账号：{account}\n身份：{identity}\nMini App 执行 {CONCUBINE_PLACE_COMMAND} 异常：{exc}",
                logger=_logger(actor),
            )
            return True

        response_text = _response_text(response).replace("**", "")
        payload = getattr(response, "payload", None)
        action_result = payload.get("actionResult") if isinstance(payload, dict) else None
        payload_ok = (
            bool(action_result.get("ok"))
            if isinstance(action_result, dict)
            else bool(payload.get("ok")) if isinstance(payload, dict) else False
        )
        place_ok = bool(response is not None) and (
            payload_ok
            or any(
                marker in response_text
                for marker in (
                    "已将侍妾安置",
                    "安置在藏娇阁",
                    "藏娇阁中已有人居住",
                    "已有人居住",
                    "居于藏娇阁",
                )
            )
        )
        no_concubine = any(
            marker in response_text
            for marker in ("没有可安置的侍妾", "暂无可安置的侍妾", "无可安置的侍妾")
        )
        terminal_status = "placed" if place_ok else "not_applicable" if no_concubine else "failed"
        _update_restricted_exchange_place_state(
            actor,
            event_key,
            status=terminal_status,
            response=response_text[:500],
            placed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S") if place_ok else "",
            completed_at=(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if place_ok or no_concubine
                else ""
            ),
            error="" if place_ok or no_concubine else "miniapp_place_unconfirmed",
        )
        if place_ok:
            _logger(actor).info(
                "Restricted exchange event %s for %s: %s completed through Mini App.",
                event_key,
                identity,
                CONCUBINE_PLACE_COMMAND,
            )
        elif no_concubine:
            _logger(actor).info(
                "Restricted exchange event %s for %s needs no placement: %s",
                event_key,
                identity,
                response_text[:300],
            )
        else:
            _logger(actor).error(
                "Restricted exchange event %s for %s: Mini App placement was not confirmed: %s",
                event_key,
                identity,
                response_text[:300] or "<empty>",
            )
            await send_text_alert(
                actor,
                "南陇侯安置未确认",
                f"账号：{account}\n身份：{identity}\nMini App 回复：{response_text[:300] or '空回复'}",
                logger=_logger(actor),
            )
        return True


def _response_id(response):
    try:
        return int(getattr(response, "id", 0) or 0)
    except Exception:
        return 0


async def _send_direct_auto_reply_command(actor, command, reply_to=None, identity=None):
    """发送自动回复辅助指令；用于需要绕过通用自动禁用策略的短流程。"""
    target_chat, target_reply = actor_message_target(actor, reply_to=reply_to)
    remember_script_send_intent(actor, command)
    sent = await actor.client.send_message(
        target_chat,
        command,
        reply_to=target_reply,
    )
    remember_script_sent_message(actor, sent)
    schedule_command_auto_delete(actor, sent, text=command, logger=_logger(actor))
    record_command_sent(
        actor,
        sent,
        command,
        identity=identity or getattr(actor, "current_identity", "主魂"),
        source="auto",
        reply_to=target_reply,
        logger=_logger(actor),
    )
    return sent


async def _live_reply_target(actor, reply_to):
    if not reply_to:
        return None
    try:
        target_chat, _ = actor_message_target(actor, reply_to=reply_to)
        msg = await actor.client.get_messages(target_chat, ids=reply_to)
        return reply_to if msg else None
    except Exception:
        return None


async def _send_direct_with_reply_fallback(actor, command, reply_to=None, identity=None):
    live_reply = await _live_reply_target(actor, reply_to)
    try:
        return await _send_direct_auto_reply_command(
            actor, command, reply_to=live_reply, identity=identity
        )
    except Exception as exc:
        if live_reply is None:
            raise
        _logger(actor).warning(
            f"Auto exchange reply target {live_reply} became unavailable; retrying {command} without reply: {exc}"
        )
        return await _send_direct_auto_reply_command(
            actor, command, reply_to=None, identity=identity
        )


async def _send_direct_and_wait_reply(actor, command, identity, deadline, reply_to=None):
    sent = await _send_direct_with_reply_fallback(
        actor, command, reply_to=reply_to, identity=identity
    )
    waiter = {
        "event": asyncio.Event(),
        "text": "",
        "msg": None,
        "chat_id": getattr(sent, "chat_id", None),
    }
    waiter_key = _event_message_key(sent)
    _exchange_step_waiters(actor)[waiter_key] = waiter
    seconds_left = max(0, int((deadline - datetime.now()).total_seconds()))
    try:
        await asyncio.wait_for(waiter["event"].wait(), timeout=max(1, min(60, seconds_left)))
    except asyncio.TimeoutError:
        return sent, "", None
    finally:
        _exchange_step_waiters(actor).pop(waiter_key, None)
    return sent, waiter.get("text", ""), waiter.get("msg")


async def _send_auto_reply_identity_command(
    actor,
    identity,
    command,
    *,
    reply_to=None,
    timeout=45,
    max_retries=2,
    suppress_no_response_alert=False,
):
    """按身份发送自动回复指令；旧脚本则退回到直接发送。"""
    if hasattr(actor, "send_and_wait_feedback_identity"):
        return await actor.send_and_wait_feedback_identity(
            identity,
            command,
            timeout=timeout,
            max_retries=max_retries,
            reply_to=reply_to,
            suppress_no_response_alert=suppress_no_response_alert,
        )

    if not command_send_allowed(actor, command, _logger(actor)):
        return None
    return await _send_direct_auto_reply_command(actor, command, reply_to=reply_to)


async def _run_exchange_reply_sequence(actor, identity, exchange_command, reply_to, event_key):
    """南陇侯交换：安置侍妾 -> 交换 -> 召回侍妾，期间避免其它身份命令插队。"""
    current_task = asyncio.current_task()
    claimed_atomic = False
    entry = _exchange_state(actor).get(str(event_key), {})
    deadline = _exchange_deadline(entry)
    if hasattr(actor, "active_atomic_task"):
        while actor.active_atomic_task is not None and actor.active_atomic_task != current_task:
            if datetime.now() >= deadline - timedelta(seconds=EXCHANGE_EVENT_SAFETY_MARGIN_SECONDS):
                _update_exchange_state(actor, event_key, status="expired_waiting_atomic")
                return False
            await asyncio.sleep(0.5)
        if actor.active_atomic_task is None:
            actor.active_atomic_task = current_task
            claimed_atomic = True

    try:
        if datetime.now() >= deadline:
            _update_exchange_state(actor, event_key, status="expired_before_place")
            return False

        place_resp = await _send_auto_reply_identity_command(
            actor,
            identity,
            CONCUBINE_PLACE_COMMAND,
            timeout=5,
            max_retries=0,
            suppress_no_response_alert=True,
        )
        place_text = _response_text(place_resp).replace("**", "")
        place_ok = bool(place_text) and any(
            marker in place_text
            for marker in ("安置", "藏娇阁中已有人居住", "已有人居住")
        )
        _update_exchange_state(
            actor, event_key, place_status="ok" if place_ok else "failed", place_response=place_text[:300]
        )
        if not place_ok:
            await send_text_alert(
                actor,
                "南陇侯交换中止",
                f"身份：{identity}\n步骤：安置侍妾\n回复：{place_text or '无回复'}",
                logger=_logger(actor),
            )
            return False
        await asyncio.sleep(EXCHANGE_CONCUBINE_DELAY_SECONDS)

        if datetime.now() >= deadline - timedelta(seconds=EXCHANGE_EVENT_SAFETY_MARGIN_SECONDS):
            _update_exchange_state(actor, event_key, status="expired_before_exchange")
            return False

        settlement_waiter = {"event": asyncio.Event(), "text": "", "msg": None, "command_msg_id": 0}
        _exchange_waiters(actor)[str(event_key)] = settlement_waiter
        sent_exchange = await _send_direct_with_reply_fallback(
            actor, exchange_command, reply_to=reply_to, identity=identity
        )
        settlement_waiter["command_msg_id"] = getattr(sent_exchange, "id", 0)
        _update_exchange_state(
            actor,
            event_key,
            status="exchange_sent",
            command_msg_id=getattr(sent_exchange, "id", 0),
            used_reply_to=meaningful_reply_to_msg_id(actor, sent_exchange) or 0,
        )

        # 无回复时重发：1分钟内没收到结算回复就再发一次，最多发送3遍。
        settlement_confirmed = False
        try:
            for attempt in range(1, EXCHANGE_SETTLEMENT_RETRY_LIMIT + 1):
                if attempt > 1:
                    margin_left = int(
                        (deadline - timedelta(seconds=EXCHANGE_EVENT_SAFETY_MARGIN_SECONDS) - datetime.now()).total_seconds()
                    )
                    if margin_left <= 0:
                        break
                    sent_exchange = await _send_direct_with_reply_fallback(
                        actor, exchange_command, reply_to=reply_to, identity=identity
                    )
                    settlement_waiter["command_msg_id"] = getattr(sent_exchange, "id", 0)
                    _update_exchange_state(
                        actor,
                        event_key,
                        status="exchange_resent",
                        command_msg_id=getattr(sent_exchange, "id", 0),
                        resend_attempt=attempt,
                    )
                remaining = max(0, int((deadline - datetime.now()).total_seconds()))
                wait_seconds = min(EXCHANGE_SETTLEMENT_FIRST_WAIT_SECONDS, max(1, remaining))
                try:
                    await asyncio.wait_for(settlement_waiter["event"].wait(), timeout=wait_seconds)
                    settlement_confirmed = True
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            _exchange_waiters(actor).pop(str(event_key), None)
        if not settlement_confirmed:
            _update_exchange_state(actor, event_key, status="exchange_unconfirmed")
            await send_text_alert(
                actor,
                "南陇侯交换未确认",
                f"身份：{identity}\n指令：{exchange_command}\n已重试{EXCHANGE_SETTLEMENT_RETRY_LIMIT}次仍未收到南陇侯结算。",
                logger=_logger(actor),
            )
            return False

        settlement_text = settlement_waiter.get("text", "")
        _update_exchange_state(
            actor, event_key, status="exchange_confirmed", settlement=settlement_text[:500]
        )
        await asyncio.sleep(EXCHANGE_CONCUBINE_DELAY_SECONDS)

        if datetime.now() >= deadline:
            _update_exchange_state(actor, event_key, recall_status="skipped_deadline")
            return True

        _, recall_text, _ = await _send_direct_and_wait_reply(
            actor, CONCUBINE_RECALL_COMMAND, identity, deadline
        )
        recall_clean = recall_text.replace("**", "")
        recall_ok = bool(recall_clean) and any(
            marker in recall_clean for marker in ("召回", "随你一同历练", "状态: 随行中")
        )
        _update_exchange_state(
            actor,
            event_key,
            recall_status="ok" if recall_ok else "unconfirmed",
            recall_response=recall_clean[:300],
            status="done" if recall_ok else "done_recall_unconfirmed",
        )
        if not recall_ok:
            await send_text_alert(
                actor,
                "南陇侯交换召回未确认",
                f"身份：{identity}\n交换已成功，但召回侍妾未确认：{recall_clean or '无回复'}",
                logger=_logger(actor),
            )
        return True
    finally:
        if claimed_atomic and getattr(actor, "active_atomic_task", None) == current_task:
            actor.active_atomic_task = None


def _consume_exchange_step_reply(actor, msg, text, sender):
    if not is_game_bot_sender(actor, sender):
        return False
    replied_id = meaningful_reply_to_msg_id(actor, msg)
    chat_id = normalize_telegram_chat_id(getattr(msg, "chat_id", None))
    waiter = _exchange_step_waiters(actor).get(
        f"{chat_id}:{replied_id}" if chat_id is not None else replied_id
    )
    if not waiter:
        return False
    if normalize_telegram_chat_id(getattr(msg, "chat_id", None)) != normalize_telegram_chat_id(
        waiter.get("chat_id")
    ):
        return False
    waiter["text"] = str(text or "")
    waiter["msg"] = msg
    waiter["event"].set()
    return True


def _consume_exchange_settlement(actor, msg, text, sender):
    if not is_exchange_settlement_text(text) or not is_game_bot_sender(actor, sender):
        return False
    identity = _mentions_self(actor, msg, text)
    if not identity:
        return False
    for event_key, waiter in list(_exchange_waiters(actor).items()):
        entry = _exchange_state(actor).get(str(event_key), {})
        if entry.get("identity") != identity or waiter["event"].is_set():
            continue
        waiter["text"] = str(text or "")
        waiter["msg"] = msg
        command_msg_id = waiter.get("command_msg_id") or entry.get("command_msg_id")
        if command_msg_id:
            record_command_response_for_command_id(
                actor,
                command_msg_id,
                msg,
                text=text,
                status="matched",
                logger=_logger(actor),
                sender=sender,
            )
        waiter["event"].set()
        return True
    return False


# =====================================================================
# 对外接口函数
# =====================================================================

def is_auto_reply_followup(actor, msg, sender=None):
    """
    判断当前消息是否是脚本自动回复后的后续消息。
    通过检查消息的回复目标是否在自动回复已发送列表中。
    如果匹配，则从列表中移除该消息 ID，表示"已消费"。
    """
    if sender is not None and not is_game_bot_sender(actor, sender):
        return False
    if _consume_exchange_step_reply(actor, msg, getattr(msg, "text", "") or "", sender):
        _logger(actor).info(format_in_log("exchange-step", msg.text or "", sender=sender, msg=msg))
        return True

    replied_id = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
    if not replied_id:
        return False

    sent_ids = getattr(actor, "auto_reply_sent_ids", set())
    if replied_id not in sent_ids:
        return False

    sent_ids.discard(replied_id)
    _logger(actor).info(format_in_log("auto-reply", msg.text or "", sender=sender, msg=msg))
    return True


async def maybe_auto_reply_merchant(actor, event, text=None, sender=None):
    """异界商人：被点名后查看货品，并按优先级购买指定商品。"""
    msg = event.message
    text = text if text is not None else (msg.text or "")
    if not is_merchant_event_text(text):
        return False

    identity = _mentions_self(actor, msg, text)
    if not identity:
        return False

    if sender is None:
        sender = await event.get_sender()
    if not is_game_bot_sender(actor, sender):
        return False

    seen_ids = getattr(actor, "merchant_auto_reply_seen_ids", None)
    if seen_ids is None:
        seen_ids = set()
        actor.merchant_auto_reply_seen_ids = seen_ids
    if msg.id in seen_ids:
        return True
    seen_ids.add(msg.id)
    if len(seen_ids) > 300:
        actor.merchant_auto_reply_seen_ids = set(list(seen_ids)[-150:])
    if not claim_merchant_event(actor, msg.id):
        _logger(actor).info(f"Auto merchant message {msg.id} already claimed by another script.")
        return True

    identity = "主魂" if identity == "main" else identity
    try:
        _logger(actor).info(
            f"Auto merchant triggered for {identity}: {MERCHANT_LOOK_COMMAND} (msg {msg.id})."
        )
        goods_resp = await _send_auto_reply_identity_command(
            actor,
            identity,
            MERCHANT_LOOK_COMMAND,
            timeout=45,
            max_retries=0,
            suppress_no_response_alert=True,
        )
        goods_text = _response_text(goods_resp)
        plan = merchant_purchase_plan(goods_text)
        if not plan:
            update_merchant_event(msg.id, status="done", goods=parse_merchant_goods(goods_text), purchased=[])
            _logger(actor).info(f"Auto merchant [{identity}]: no priority goods found.")
            return True

        bought = []
        for item_name, number in plan:
            command = f"{MERCHANT_BUY_COMMAND_PREFIX} {number}"
            _logger(actor).info(f"Auto merchant [{identity}]: buying {item_name} with {command}.")
            buy_resp = await _send_auto_reply_identity_command(
                actor,
                identity,
                command,
                timeout=45,
                max_retries=0,
                suppress_no_response_alert=True,
            )
            bought.append(f"{item_name}#{number}")
            buy_text = _response_text(buy_resp)
            if buy_text and any(k in buy_text for k in ("灵石不足", "不足", "无法购买", "购买失败")):
                _logger(actor).info(
                    f"Auto merchant [{identity}]: stop after failed buy response for {item_name}: "
                    f"{buy_text[:120]!r}."
                )
                break
            await asyncio.sleep(2)
        update_merchant_event(
            msg.id,
            status="purchased",
            goods=parse_merchant_goods(goods_text),
            purchased=bought,
        )
        _logger(actor).info(f"Auto merchant [{identity}] handled purchases: {', '.join(bought)}.")
        return True
    except Exception as e:
        update_merchant_event(msg.id, status="failed", error=str(e)[:200])
        _logger(actor).error(f"Auto merchant reply failed for message {msg.id}: {e}")
        return True


async def maybe_auto_reply_exchange(actor, event, text=None, sender=None):
    """
    交换法宝的自动回复处理。

    触发条件：游戏机器人消息中包含".交换"关键词且提到了本账号。
    行为：按执行身份发送相应指令。
      - 所有身份（主魂+化身）统一回复 ".交换 法宝"（2026-09-07 用户指定）

    通过 seen_ids 去重，避免对同一条消息重复回复。
    seen_ids 最多保留 300 条，超出时裁剪到最近 150 条。
    """
    msg = event.message
    text = text if text is not None else (msg.text or "")
    if await maybe_auto_reply_merchant(actor, event, text=text, sender=sender):
        return True

    if sender is None:
        sender = await event.get_sender()
    if not is_game_bot_sender(actor, sender):
        return False

    if _consume_exchange_step_reply(actor, msg, text, sender):
        return True
    if _consume_exchange_settlement(actor, msg, text, sender):
        return True

    identity = _mentions_self(actor, msg, text)
    if is_exchange_teaser_text(text) and identity:
        teaser_key = f"teaser:{_event_message_key(msg)}"
        _update_exchange_state(
            actor,
            teaser_key,
            status="teaser_seen",
            identity=identity,
            teaser_msg_id=getattr(msg, "id", 0),
            teaser_text=str(text or "")[:500],
            seen_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        _logger(actor).info(
            f"Auto exchange teaser recorded for {identity} (msg {getattr(msg, 'id', None)})."
        )
        return True

    if not is_exchange_offer_text(text):
        return False
    if not identity:
        return False

    seen_ids = getattr(actor, "exchange_auto_reply_seen_ids", None)
    if seen_ids is None:
        seen_ids = set()
        actor.exchange_auto_reply_seen_ids = seen_ids
    event_key = _event_message_key(msg)
    if event_key in seen_ids:
        return True  # 已处理过，跳过
    seen_ids.add(event_key)
    if len(seen_ids) > 300:
        actor.exchange_auto_reply_seen_ids = set(list(seen_ids)[-150:])

    identity = "主魂" if identity == "main" else identity
    cmd = exchange_command_for_identity(identity)

    try:
        # Persist/claim immediately because the bot may delete the offer before
        # the human-like delay finishes.
        now = datetime.now()
        delay = random.randint(*EXCHANGE_DELAY_RANGE_SECONDS)
        _update_exchange_state(
            actor,
            event_key,
            status="claimed",
            identity=identity,
            command=cmd,
            offer_msg_id=getattr(msg, "id", 0),
            offer_text=str(text or "")[:1000],
            claimed_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            deadline_at=(now + timedelta(seconds=EXCHANGE_EVENT_TTL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S"),
            delay_seconds=delay,
        )
        _logger(actor).info(
            f"Auto exchange reply triggered for {identity}: {cmd} "
            f"(msg {msg.id}). Waiting {delay}s for safety..."
        )
        async def delayed_sequence():
            try:
                await asyncio.sleep(delay)
                completed = await _run_exchange_reply_sequence(
                    actor, identity, cmd, reply_to=msg.id, event_key=event_key
                )
                _identity = getattr(actor, "current_identity", None)
                _tag = f" [{_identity}]" if _identity else ""
                _logger(actor).info(
                    f"OUT{_tag}:\n"
                    f"{CONCUBINE_PLACE_COMMAND} -> {cmd} -> {CONCUBINE_RECALL_COMMAND} "
                    f"(exchange event={event_key}, completed={completed})"
                )
            except Exception as exc:
                _update_exchange_state(actor, event_key, status="failed", error=str(exc)[:300])
                _logger(actor).error(f"Auto exchange sequence failed for message {msg.id}: {exc}", exc_info=True)
                await send_text_alert(
                    actor,
                    "南陇侯交换异常",
                    f"身份：{identity}\n指令：{cmd}\n异常：{exc}",
                    logger=_logger(actor),
                )

        task = asyncio.create_task(delayed_sequence())
        tasks = getattr(actor, "_exchange_auto_tasks", None)
        if tasks is None:
            tasks = set()
            setattr(actor, "_exchange_auto_tasks", tasks)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return True
    except Exception as e:
        _logger(actor).error(f"Auto exchange reply failed for message {msg.id}: {e}")
        return True


async def resume_pending_exchange_events(actor):
    """Resume a claimed, not-yet-sent exchange after a short process restart."""
    startup_done = getattr(actor, "startup_done", None)
    if startup_done is not None:
        await startup_done.wait()
    for event_key, entry in list(_exchange_state(actor).items()):
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if status == "exchange_sent":
            # Never resend a possibly successful irreversible exchange.
            _update_exchange_state(actor, event_key, status="interrupted_after_exchange_send")
            await send_text_alert(
                actor,
                "南陇侯交换需人工确认",
                f"事件 {event_key} 在交换指令发出后重启，未自动重发以避免重复交换。",
                logger=_logger(actor),
            )
            continue
        if status != "claimed" or _exchange_seconds_left(entry) <= EXCHANGE_EVENT_SAFETY_MARGIN_SECONDS:
            continue
        identity = str(entry.get("identity") or "主魂")
        command = str(entry.get("command") or exchange_command_for_identity(identity))
        reply_to = int(entry.get("offer_msg_id") or 0) or None
        _logger(actor).warning(
            f"Resuming pending auto exchange event {event_key} for {identity}; "
            f"{_exchange_seconds_left(entry)}s remain."
        )
        asyncio.create_task(
            _run_exchange_reply_sequence(
                actor, identity, command, reply_to=reply_to, event_key=event_key
            )
        )
