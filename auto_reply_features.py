#!/usr/bin/env python3
"""
【自动回复功能模块 —— 所有账号脚本共享】

在游戏机器人发出特定消息时，脚本自动进行被动回复。
目前支持的自动回复场景：
  1. 交换功法（.交换 功法）—— 当游戏机器人提到本账号且有"交换功法"关键词时，自动回复该指令
"""
import asyncio
import logging
import re

from log_utils import command_send_allowed, format_in_log, is_game_bot_sender, remember_script_send_intent, remember_script_sent_message, schedule_command_auto_delete


# =====================================================================
# 常量定义
# =====================================================================
EXCHANGE_MAGIC_TREASURE_COMMAND = ".交换 功法"  # 交换功法指令


# =====================================================================
# 内部工具函数
# =====================================================================

def _logger(actor):
    """获取当前账号实例的日志记录器"""
    return logging.getLogger(actor.__class__.__name__)


def _normalized_text(text):
    """去除多余空白，标准化消息文本"""
    return re.sub(r"\s+", " ", text or "").strip()


def _mentions_self(actor, msg, text):
    """
    判断消息是否提到了本账号或下属的化身。
    如果提到主号（通过 username 或 ID），返回 "main"。
    如果提到化身，返回化身名称。
    否则返回 False。
    """
    me = getattr(actor, "my_info", None)
    lower_text = (text or "").lower()
    
    # 1. 检查是否提到了化身的真实名字
    avatars = getattr(actor, "avatars", [])
    for avatar in avatars:
        if avatar in (text or ""):
            return avatar

    # 2. 检查是否提到了化身的 username（如 @hajiimiii）
    avatar_usernames = getattr(actor, "avatar_usernames", {})
    for uname, avatar in avatar_usernames.items():
        if f"@{uname.lower()}" in lower_text:
            return avatar

    if not me:
        return False

    lower_text = (text or "").lower()
    username = (getattr(me, "username", "") or "").lower().lstrip("@")
    if username and f"@{username}" in lower_text:
        return "main"

    my_id = getattr(me, "id", None)
    if my_id:
        for entity in getattr(msg, "entities", None) or []:
            if getattr(entity, "user_id", None) == my_id:
                return "main"

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
    replied_id = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
    if not replied_id:
        return False

    sent_ids = getattr(actor, "auto_reply_sent_ids", set())
    if replied_id not in sent_ids:
        return False

    sent_ids.discard(replied_id)
    _logger(actor).info(format_in_log("auto-reply", msg.text or "", sender=sender, msg=msg))
    return True


async def maybe_auto_reply_exchange(actor, event, text=None, sender=None):
    """
    交换功法/法宝的自动回复处理。

    触发条件：游戏机器人消息中包含".交换"关键词且提到了本账号。
    行为：自动发送相应的指令进行交换。
      - 星宫/万灵宗：回复 ".交换 法宝"
      - 凌霄宫：回复 ".交换 功法"

    通过 seen_ids 去重，避免对同一条消息重复回复。
    seen_ids 最多保留 300 条，超出时裁剪到最近 150 条。
    """
    msg = event.message
    text = text if text is not None else (msg.text or "")
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

    # 动态决定自动回复的指令
    sect = getattr(actor, "sect_name", "")
    cmd = ".交换 法宝" if sect in ["星宫", "万灵宗"] else ".交换 功法"

    try:
        # 主人指令防封安全防线：增加 1 分钟左右的随机延迟，模拟真人行为防封
        import random
        delay = random.randint(60, 75)
        _logger(actor).info(f"Auto exchange reply triggered for {identity} (msg {msg.id}). Waiting {delay}s for safety...")
        await asyncio.sleep(delay)

        # 指令守卫：检测是否可以发送此指令（频率限制）
        if not command_send_allowed(actor, cmd, _logger(actor)):
            return True
        remember_script_send_intent(actor, cmd)
        
        # 发送指令，并引用原始消息作为回复
        if identity != "main" and hasattr(actor, "send_and_wait_feedback_identity"):
            # 如果是化身且支持带身份发送，则通过化身管线发送
            sent = await actor.send_and_wait_feedback_identity(
                identity, cmd, timeout=30, reply_to=msg.id, return_msg=True
            )
            # send_and_wait_feedback_identity 不一定会返回 sent message 对象，所以这里需要安全处理
            # 但我们在上一部加了 return_msg 支持（或者通过_send_and_wait_feedback_raw）
            # 因为这里需要记录 sent.id，如果返回的是字符串，则无法记录
            # 退化处理：如果是字符串，则无法直接 auto_delete 和 follow_up
            if sent and hasattr(sent, "id"):
                remember_script_sent_message(actor, sent)
                schedule_command_auto_delete(actor, sent, text=cmd, logger=_logger(actor))
                sent_ids = getattr(actor, "auto_reply_sent_ids", None)
                if sent_ids is None:
                    sent_ids = set()
                    actor.auto_reply_sent_ids = sent_ids
                sent_ids.add(sent.id)
        else:
            # 原本的直接发送逻辑
            sent = await actor.client.send_message(
                actor.target_chat_id,
                cmd,
                reply_to=msg.id,
            )
            remember_script_sent_message(actor, sent)
            schedule_command_auto_delete(actor, sent, text=cmd, logger=_logger(actor))
            # 记录已发送消息 ID，供后续 is_auto_reply_followup 使用
            sent_ids = getattr(actor, "auto_reply_sent_ids", None)
            if sent_ids is None:
                sent_ids = set()
                actor.auto_reply_sent_ids = sent_ids
            sent_ids.add(sent.id)

        _identity = getattr(actor, "current_identity", None)
        _tag = f" [{_identity}]" if _identity else ""
        _logger(actor).info(f"🟢 OUT{_tag}:\n{cmd} (reply_to={msg.id})")
        return True
    except Exception as e:
        _logger(actor).error(f"Auto exchange reply failed for message {msg.id}: {e}")
        return True
