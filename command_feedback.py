"""
【命令反馈处理模块 —— 所有账号脚本共享】

提供 send_and_wait_feedback_common 函数，是所有脚本发送指令并等待反馈的核心函数。
核心流程：
  1. 发送指令到游戏群组
  2. 注册反馈事件（事件循环等待游戏机器人回复）
  3. 超时未回复时自动重试
  4. 返回回复内容或消息对象

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 等脚本共享使用。

【阅读导览】
- send_and_wait_feedback_common 是唯一核心入口：发送、登记 pending、等待、重试、清理。
- _record_repeated_response_guard：同一错误回复反复出现时熔断，防止刷屏。
- _record_timed_response_guard：回复里带“还需/冷却”时间时，按时间设置命令守卫。
- _handle_telegram_send_protection：Telegram 自身限流/禁言等发送异常的保护。
"""
import asyncio
import re
import time
from datetime import datetime

from log_utils import (
    cap_command_retries,          # 限制最大重试次数
    command_response_family,      # 指令回复类型
    command_send_allowed,         # 指令守卫：检测发送频率
    is_passive_settlement_response, # 被动结算会截断任意指令回复
    log_incoming_message,         # 记录收到的消息到日志
    record_bot_no_response,       # 记录机器人无响应事件
    record_bot_response,          # 记录机器人有响应事件
    record_cultivation_delta_from_text,    # 同步修为增减到 state
    record_cultivation_profile_from_text,  # 同步境界/修为资料
    record_command_sent,       # 指令台账：记录已发送命令
    remember_script_send_intent,  # 记录脚本有发送意图
    remember_script_sent_message, # 记录脚本已发送消息
    record_recent_profile_command, # 记录可能产生无 reply 档案回复的指令
    schedule_command_auto_delete, # 安排消息自动删除
    wait_for_bot_activity_before_send,  # 等待机器人活跃后再发消息
    send_text_alert,              # 异常报警发送
    force_command_guard_block,     # 响应异常时强制暂停当前指令
)

REPEATED_RESPONSE_GUARD_WINDOW_SECONDS = 3 * 60
REPEATED_RESPONSE_GUARD_LIMIT = 3
REPEATED_RESPONSE_GUARD_BLOCK_SECONDS = 60 * 60
REPEATED_RESPONSE_GUARD_EXCLUDED_PREFIXES = (
    ".查看闭关",
    ".我的状态",
    ".状态",
    ".鱼篓",
    ".钓鱼状态",
    ".我的洞府",
)
TIMED_RESPONSE_GUARD_KEYWORDS = (
    "冷却",
    "剩余",
    "还需",
    "尚需",
    "预计还需",
    "后再",
    "后可",
    "请在",
    "调息",
    "休整",
)
TIMED_RESPONSE_GUARD_EXCLUDED_PREFIXES = (
    ".查看闭关",
    ".我的状态",
    ".状态",
    ".鱼篓",
    ".钓鱼状态",
    ".我的洞府",
    ".我的阴罗幡",
    ".我的侍妾",
    ".观星台",
)

TELEGRAM_WRITE_RESTRICTED_PATTERNS = (
    "CHAT_WRITE_FORBIDDEN",
    "USER_BANNED_IN_CHANNEL",
    "CHAT_SEND_PLAIN_FORBIDDEN",
    "CHAT_ADMIN_REQUIRED",
    "You can't write in this chat",
    "You're banned from sending messages",
    "banned from sending messages",
    "not enough rights to send",
)
TELEGRAM_FLOOD_WAIT_PATTERNS = (
    "FLOOD_WAIT",
    "A wait of",
    "seconds is required",
)


def _normalize_repeated_response_text(text):
    clean = str(text or "").replace("**", "").replace("`", "")
    clean = re.sub(r"^\s*\[Avatar:\s*[^\]]+\]\s*", "", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:500]


def _record_repeated_response_guard(actor, message, response_text, logger=None, identity=None):
    """同一指令短时间得到相同异常回复时，自动暂停该指令一段时间。"""
    command = str(message or "").strip()
    if not command or any(command.startswith(prefix) for prefix in REPEATED_RESPONSE_GUARD_EXCLUDED_PREFIXES):
        return False
    signature = _normalize_repeated_response_text(response_text)
    if len(signature) < 8:
        return False

    now = time.monotonic()
    current_id = str(identity or getattr(actor, "current_identity", "主魂") or "主魂")
    key = f"{current_id}\u001f{command}"
    guard = getattr(actor, "_repeated_response_guard", None)
    if guard is None:
        guard = {}
        setattr(actor, "_repeated_response_guard", guard)

    entry = guard.get(key) or {}
    if entry.get("signature") == signature and now - float(entry.get("first_seen", now)) <= REPEATED_RESPONSE_GUARD_WINDOW_SECONDS:
        entry["count"] = int(entry.get("count") or 1) + 1
        entry["last_seen"] = now
    else:
        entry = {"signature": signature, "count": 1, "first_seen": now, "last_seen": now, "alerted": False}
    guard[key] = entry

    if entry["count"] < REPEATED_RESPONSE_GUARD_LIMIT or entry.get("alerted"):
        return False

    entry["alerted"] = True
    preview = signature[:120]
    reason_text = (
        f"{REPEATED_RESPONSE_GUARD_WINDOW_SECONDS // 60}分钟内同一回复重复 "
        f"{entry['count']} 次，疑似指令状态未推进，已暂停该命令 "
        f"{REPEATED_RESPONSE_GUARD_BLOCK_SECONDS // 60} 分钟。回复摘录：{preview}"
    )
    force_command_guard_block(
        actor,
        command,
        REPEATED_RESPONSE_GUARD_BLOCK_SECONDS,
        logger=logger,
        identity=current_id,
        reason="repeated_response",
        alert=True,
        reason_text=reason_text,
    )
    return True


def _record_timed_response_guard(actor, message, response_text, logger=None, identity=None):
    """回复中带明确冷却时间时，按解析出的时间退避，避免盲目重试。"""
    command = str(message or "").strip()
    if not command or any(command.startswith(prefix) for prefix in TIMED_RESPONSE_GUARD_EXCLUDED_PREFIXES):
        return False
    clean = str(response_text or "").replace("**", "").replace("`", "")
    if not any(keyword in clean for keyword in TIMED_RESPONSE_GUARD_KEYWORDS):
        return False
    parser = getattr(actor, "parse_wait_time", None)
    if not callable(parser):
        return False
    try:
        wait = int(parser(clean) or 0)
    except Exception:
        wait = 0
    if wait <= 0:
        return False

    wait = min(wait + 60, 7 * 24 * 3600)
    current_id = str(identity or getattr(actor, "current_identity", "主魂") or "主魂")
    force_command_guard_block(
        actor,
        command,
        wait,
        logger=logger,
        identity=current_id,
        reason="timed_response",
        alert=False,
    )
    if logger:
        logger.info(
            f"Timed response parsed for [{command}] ({current_id}); "
            f"backing off {wait}s instead of retrying blindly."
        )
    return True


def _is_telegram_send_protection_error(exc):
    name = exc.__class__.__name__
    text = f"{name}: {exc}"
    if any(pattern in text for pattern in TELEGRAM_WRITE_RESTRICTED_PATTERNS):
        return True, "write_restricted"
    if any(pattern in text for pattern in TELEGRAM_FLOOD_WAIT_PATTERNS):
        return True, "flood_wait"
    return False, ""


async def _handle_telegram_send_protection(actor, message, exc, logger=None, identity=None):
    matched, reason = _is_telegram_send_protection_error(exc)
    if not matched:
        return False

    current_id = str(identity or getattr(actor, "current_identity", "主魂") or "主魂")
    try:
        setattr(actor, "is_running", False)
        setattr(actor, "_telegram_send_protection_stop", {
            "identity": current_id,
            "command": str(message or ""),
            "reason": reason,
            "error": str(exc),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        state = getattr(actor, "state", None)
        if isinstance(state, dict):
            state["telegram_send_protection_stop"] = getattr(actor, "_telegram_send_protection_stop")
            save_state = getattr(actor, "save_state", None)
            if callable(save_state):
                save_state()
    except Exception:
        pass

    if logger:
        logger.critical(
            f"Telegram send protection triggered; stopping script. "
            f"identity={current_id}, command={message!r}, reason={reason}, error={exc}"
        )

    try:
        await send_text_alert(
            actor,
            "Telegram发送保护",
            (
                f"账号身份：{current_id}\n"
                f"指令：{message}\n"
                f"原因：{reason}\n"
                f"错误：{exc}\n"
                "已自动停止当前脚本，避免继续触发 Telegram 限制。"
            ),
            logger=logger,
        )
    except Exception:
        pass
    return True


async def send_and_wait_feedback_common(
    actor,
    logger,
    message,
    timeout=45,
    max_retries=2,
    reply_to=None,
    return_msg=False,
    return_sent=False,
    delete_after=True,
    return_response_msg=False,
    return_msg_role="sent",
    suppress_no_response_alert=False,
    skip_bot_activity_wait=False,
):
    """
    发送指令并等待机器人回复的核心函数。

    参数：
        actor: 脚本实例（Cultivator/SubCultivator/Xiaohao）
        logger: 日志记录器
        message: 要发送的指令文本
        timeout: 等待机器人回复的超时时间（秒）
        max_retries: 超时后最大重试次数
        reply_to: 回复的目标消息 ID（用于引用回复）
        return_msg: 是否返回消息对象而非文本
        return_sent: 是否返回已发送的消息对象
        return_response_msg: 是否返回响应消息对象
        return_msg_role: "sent"=返回发送的消息 / "response"=返回机器人的回复
        suppress_no_response_alert: 超时时是否抑制"无响应"警报

    返回值：
        取决于 return_* 参数：
        - return_response_msg=True: 返回响应消息对象
        - return_sent=True: 返回已发送的消息对象（仅当有回复时）
        - return_msg=True: 根据 return_msg_role 返回发送或响应的消息对象
        - 默认: 返回响应文本字符串

    核心流程：
        1. 获取 cmd_lock（同一时间只能处理一条指令）
        2. 等待机器人活跃
        3. 检测指令守卫（发送频率限制）
        4. 发送指令
        5. 注册 feedback_events 事件
        6. 等待 asyncio.Event 触发（由 handle_game_response 在收到回复时设置）
        7. 超时则重试，直到 max_retries
    """
    pause_event = getattr(actor, "pause_event", None)
    if pause_event is not None:
        await pause_event.wait()

    async with actor.cmd_lock:
        _func_start = time.monotonic()
        logger.info(f"[DEBUG-FEEDBACK] ENTER send_and_wait_feedback, cmd={message!r}, identity={getattr(actor, 'current_identity', '?')}")
        max_retries = cap_command_retries(max_retries)
        retries = 0
        resp_text = ""
        final_sent_msg = None
        final_resp_msg = None
        matched_feedback = False

        while retries <= max_retries and getattr(actor, "is_running", True):
            target_reply = reply_to if reply_to else actor.topic_id
            try:
                if pause_event is not None:
                    await pause_event.wait()
                # 等待游戏机器人活跃后再发送；身份发送管线会在拿 avatar_send_lock 前预检，
                # 锁内调用用 skip_bot_activity_wait 避免维护期间长时间持锁。
                if not skip_bot_activity_wait:
                    logger.info(f"[DEBUG-FEEDBACK] [{message}] waiting for bot activity...")
                    if not await wait_for_bot_activity_before_send(actor, message, logger):
                        logger.warning(f"[DEBUG-FEEDBACK] [{message}] bot activity check FAILED (bot dead?)")
                        break
                else:
                    logger.info(f"[DEBUG-FEEDBACK] [{message}] bot activity prechecked, skipping lock-held wait")
                logger.info(f"[DEBUG-FEEDBACK] [{message}] bot active, checking guard...")
                # 检查指令守卫（过快的发送会被阻止）
                if not command_send_allowed(actor, message, logger):
                    block = getattr(actor, "_last_command_guard_block", {}) or {}
                    if block.get("reason") == "dashboard_disabled":
                        cache = getattr(actor, "_dashboard_disabled_feedback_log_cache", None)
                        if cache is None:
                            cache = {}
                            setattr(actor, "_dashboard_disabled_feedback_log_cache", cache)
                        cache_key = f"{getattr(actor, 'current_identity', '主魂')}\u001f{message}"
                        now_for_log = time.monotonic()
                        if now_for_log - cache.get(cache_key, 0) > 300:
                            logger.info(f"[DEBUG-FEEDBACK] [{message}] skipped: paused by dashboard")
                            cache[cache_key] = now_for_log
                    else:
                        logger.info(f"[DEBUG-FEEDBACK] [{message}] blocked by command guard")
                    break
                remember_script_send_intent(actor, message)
                # 发送指令到游戏群组
                logger.info(f"[DEBUG-FEEDBACK] [{message}] sending message to chat...")
                sent_msg = await actor.client.send_message(actor.target_chat_id, message, reply_to=target_reply)
                if not sent_msg:
                    break
                sent_wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                remember_script_sent_message(actor, sent_msg)
                schedule_command_auto_delete(actor, sent_msg, text=message, logger=logger)
                msg_id = sent_msg.id
                final_sent_msg = sent_msg
                _identity = getattr(actor, "current_identity", None)
                sent_at_by_key = getattr(actor, "_last_command_sent_at_by_identity_command", None)
                if sent_at_by_key is None:
                    sent_at_by_key = {}
                    setattr(actor, "_last_command_sent_at_by_identity_command", sent_at_by_key)
                sent_at_by_key[(str(_identity or "主魂"), str(message or "").strip())] = sent_wall
                sent_at_by_id = getattr(actor, "_last_command_sent_at_by_id", None)
                if sent_at_by_id is None:
                    sent_at_by_id = {}
                    setattr(actor, "_last_command_sent_at_by_id", sent_at_by_id)
                sent_at_by_id[msg_id] = sent_wall
                _tag = f" [{_identity}]" if _identity else ""
                logger.info(f"🟢 OUT{_tag}:\n{message}")
                if hasattr(actor, "command_avatar_map"):
                    actor.command_avatar_map[msg_id] = _identity or "主魂"
                record_command_sent(
                    actor,
                    sent_msg,
                    message,
                    identity=_identity or "主魂",
                    source="auto",
                    reply_to=target_reply,
                    logger=logger,
                )
                record_recent_profile_command(actor, msg_id, message, _identity or "主魂", source="auto")
            except Exception as exc:
                logger.error(f"Send Error [{message}] reply_to={target_reply}: {exc}")
                await _handle_telegram_send_protection(actor, message, exc, logger=logger, identity=getattr(actor, "current_identity", "主魂"))
                break

            actor.last_sent_id = msg_id
            # 记录指令发送者的 account ID，供宽松匹配排除命令回声并保留审计线索
            actor.feedback_senders = getattr(actor, "feedback_senders", {})
            actor.feedback_senders[msg_id] = sent_msg.sender_id
            # 注册反馈事件：handle_game_response 收到回复时会设置此事件
            evt = asyncio.Event()
            actor.feedback_events[msg_id] = evt
            actor.feedback_commands[msg_id] = message
            actor.feedback_sent_ts[msg_id] = time.monotonic()
            actor.feedback_identities = getattr(actor, "feedback_identities", {})
            actor.feedback_identities[msg_id] = _identity or "主魂"
            try:
                # 等待机器人回复，超时可能重试
                logger.info(f"[DEBUG-FEEDBACK] [{message}] waiting for response (timeout={timeout}s)...")
                await asyncio.wait_for(evt.wait(), timeout=timeout)
                logger.info(f"[DEBUG-FEEDBACK] [{message}] response received!")
                resp_text = actor.last_feedback_text.pop(msg_id, "").strip()
                final_resp_msg = actor.last_feedback_msg.pop(msg_id, None)
                matched_feedback = True
                record_bot_response(actor, command=message, msg=final_resp_msg, logger=logger)
                await log_incoming_message(
                    actor, message, resp_text, msg=final_resp_msg, logger=logger, identity=_identity or "主魂"
                )
                record_cultivation_profile_from_text(
                    actor, resp_text, identity=_identity or "主魂", logger=logger, source=message
                )
                record_cultivation_delta_from_text(
                    actor, resp_text, identity=_identity or "主魂", logger=logger, source=message, msg=final_resp_msg
                )
                _record_timed_response_guard(
                    actor, message, resp_text, logger=logger, identity=_identity or "主魂"
                )
                _record_repeated_response_guard(
                    actor, message, resp_text, logger=logger, identity=_identity or "主魂"
                )
                if hasattr(actor, "record_identity_yuanying_recovery_from_text"):
                    actor.record_identity_yuanying_recovery_from_text(
                        _identity or "主魂",
                        resp_text,
                        source=message,
                        command=message,
                    )
                if is_passive_settlement_response(resp_text):
                    family = command_response_family(message)
                    if family not in {"meditation", ".元婴出窍"}:
                        logger.info(
                            f"[DEBUG-FEEDBACK] [{message}] passive settlement consumed; "
                            "returning empty response so command logic will not record success."
                        )
                        resp_text = ""
                        final_resp_msg = None
                        final_sent_msg = None
                        matched_feedback = False
                break
            except asyncio.TimeoutError:
                retries += 1
                timeout_log = (
                    f"[DEBUG-FEEDBACK] [{message}] TIMEOUT after {timeout}s "
                    f"(retry {retries}/{max_retries})"
                )
                if suppress_no_response_alert:
                    logger.info(f"{timeout_log} (suppressed)")
                else:
                    logger.warning(timeout_log)
                if retries <= max_retries:
                    if suppress_no_response_alert:
                        logger.info(f"Timeout [{message}] ({retries}/{max_retries}), retrying...")
                    else:
                        logger.warning(f"Timeout [{message}] ({retries}/{max_retries}), retrying...")
                    await asyncio.sleep(5)
                else:
                    if suppress_no_response_alert:
                        logger.info(f"Timeout [{message}] - no direct response; continuing without retry.")
                    else:
                        logger.warning(f"Timeout [{message}] - No response from robot.")
                        record_bot_no_response(actor, message, logger)
                        alert_msg = f"账号: {getattr(actor, 'current_identity', '主魂')}\n指令: {message}\n状态: 重试{max_retries}次后仍无响应！"
                        await send_text_alert(actor, "异常提醒", alert_msg, logger=logger)
            finally:
                # 清理反馈事件，避免内存泄漏
                actor.feedback_events.pop(msg_id, None)
                actor.last_feedback_text.pop(msg_id, None)
                actor.last_feedback_msg.pop(msg_id, None)
                actor.feedback_commands.pop(msg_id, None)
                actor.feedback_sent_ts.pop(msg_id, None)
                actor.feedback_senders.pop(msg_id, None)
                sent_at_by_id = getattr(actor, "_last_command_sent_at_by_id", None)
                if isinstance(sent_at_by_id, dict) and len(sent_at_by_id) > 300:
                    for old_id in list(sent_at_by_id.keys())[:-150]:
                        sent_at_by_id.pop(old_id, None)
                if hasattr(actor, "feedback_identities"):
                    actor.feedback_identities.pop(msg_id, None)

        await asyncio.sleep(3)
        _total = time.monotonic() - _func_start
        _ret_preview = resp_text[:60] if resp_text else "(empty)"
        logger.info(f"[DEBUG-FEEDBACK] EXIT send_and_wait_feedback, cmd={message!r}, total={_total:.1f}s, matched={matched_feedback}, resp={_ret_preview!r}")
        # 根据调用者要求的返回格式返回结果
        if return_response_msg:
            return final_resp_msg
        if return_sent:
            return final_sent_msg if matched_feedback else None
        if return_msg:
            return final_resp_msg if return_msg_role == "response" else final_sent_msg
        return resp_text
