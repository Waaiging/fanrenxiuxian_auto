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
import re
import time

from log_utils import COMMAND_CONTROL_FILE, command_send_allowed, format_in_log, is_game_bot_sender, remember_script_send_intent, remember_script_sent_message, schedule_command_auto_delete


# =====================================================================
# 常量定义
# =====================================================================
EXCHANGE_MAIN_COMMAND = ".交换 功法"
EXCHANGE_AVATAR_COMMAND = ".交换 法宝"
CONCUBINE_PLACE_COMMAND = ".安置侍妾"
CONCUBINE_RECALL_COMMAND = ".召回侍妾"
EXCHANGE_CONCUBINE_DELAY_SECONDS = 5
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
    """三主魂换功法，所有化身换法宝。"""
    return EXCHANGE_MAIN_COMMAND if (identity or "主魂") == "主魂" else EXCHANGE_AVATAR_COMMAND


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


def _response_id(response):
    try:
        return int(getattr(response, "id", 0) or 0)
    except Exception:
        return 0


async def _send_direct_auto_reply_command(actor, command, reply_to=None):
    """发送自动回复辅助指令；用于需要绕过通用自动禁用策略的短流程。"""
    remember_script_send_intent(actor, command)
    sent = await actor.client.send_message(
        actor.target_chat_id,
        command,
        reply_to=reply_to,
    )
    remember_script_sent_message(actor, sent)
    schedule_command_auto_delete(actor, sent, text=command, logger=_logger(actor))
    return sent


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


async def _run_exchange_reply_sequence(actor, identity, exchange_command, reply_to):
    """南陇侯交换：安置侍妾 -> 交换 -> 召回侍妾，期间避免其它身份命令插队。"""
    current_task = asyncio.current_task()
    claimed_atomic = False
    if hasattr(actor, "active_atomic_task"):
        while actor.active_atomic_task is not None and actor.active_atomic_task != current_task:
            await asyncio.sleep(0.5)
        if actor.active_atomic_task is None:
            actor.active_atomic_task = current_task
            claimed_atomic = True

    try:
        await _send_auto_reply_identity_command(
            actor,
            identity,
            CONCUBINE_PLACE_COMMAND,
            timeout=5,
            max_retries=0,
            suppress_no_response_alert=True,
        )
        await asyncio.sleep(EXCHANGE_CONCUBINE_DELAY_SECONDS)

        await _send_auto_reply_identity_command(
            actor,
            identity,
            exchange_command,
            reply_to=reply_to,
            timeout=45,
        )
        await asyncio.sleep(EXCHANGE_CONCUBINE_DELAY_SECONDS)

        # .召回侍妾在通用自动指令策略里被禁用；这里是南陇侯交换的显式收尾。
        await _send_direct_auto_reply_command(actor, CONCUBINE_RECALL_COMMAND)
    finally:
        if claimed_atomic and getattr(actor, "active_atomic_task", None) == current_task:
            actor.active_atomic_task = None


# =====================================================================
# 对外接口函数
# =====================================================================

def is_auto_reply_followup(actor, msg, sender=None):
    """
    判断当前消息是否是脚本自动回复后的后续消息。
    通过检查消息的回复目标是否在自动回复已发送列表中。
    如果匹配，则从列表中移除该消息 ID，表示"已消费"。
    """
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
    交换功法/法宝的自动回复处理。

    触发条件：游戏机器人消息中包含".交换"关键词且提到了本账号。
    行为：按执行身份发送相应指令。
      - 三个主魂：回复 ".交换 功法"
      - 九个分身：回复 ".交换 法宝"

    通过 seen_ids 去重，避免对同一条消息重复回复。
    seen_ids 最多保留 300 条，超出时裁剪到最近 150 条。
    """
    msg = event.message
    text = text if text is not None else (msg.text or "")
    if await maybe_auto_reply_merchant(actor, event, text=text, sender=sender):
        return True
    if ".交换" not in _normalized_text(text):
        return False
        
    identity = _mentions_self(actor, msg, text)
    if not identity:
        return False

    if sender is None:
        sender = await event.get_sender()
    if not is_game_bot_sender(actor, sender):
        return False

    seen_ids = getattr(actor, "exchange_auto_reply_seen_ids", None)
    if seen_ids is None:
        seen_ids = set()
        actor.exchange_auto_reply_seen_ids = seen_ids
    if msg.id in seen_ids:
        return True  # 已处理过，跳过
    seen_ids.add(msg.id)
    if len(seen_ids) > 300:
        actor.exchange_auto_reply_seen_ids = set(list(seen_ids)[-150:])

    identity = "主魂" if identity == "main" else identity
    cmd = exchange_command_for_identity(identity)

    try:
        # 主人指令防封安全防线：增加 1 分钟左右的随机延迟，模拟真人行为防封
        import random
        delay = random.randint(60, 75)
        _logger(actor).info(
            f"Auto exchange reply triggered for {identity}: {cmd} "
            f"(msg {msg.id}). Waiting {delay}s for safety..."
        )
        await asyncio.sleep(delay)

        await _run_exchange_reply_sequence(actor, identity, cmd, reply_to=msg.id)

        _identity = getattr(actor, "current_identity", None)
        _tag = f" [{_identity}]" if _identity else ""
        _logger(actor).info(
            f"OUT{_tag}:\n"
            f"{CONCUBINE_PLACE_COMMAND} -> {cmd} -> {CONCUBINE_RECALL_COMMAND} "
            f"(exchange reply_to={msg.id})"
        )
        return True
    except Exception as e:
        _logger(actor).error(f"Auto exchange reply failed for message {msg.id}: {e}")
        return True
