"""Scoped, persistent Xuangu quiz automation with human-confirmed answers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unicodedata
import uuid

from automation_settings import canonical_automation_identity, xuangu_quiz_enabled
from command_feedback import guarded_one_shot_send
from log_utils import (
    actor_message_target, actor_target_chat_ids, command_send_precheck,
    is_game_bot_sender, normalize_telegram_chat_id, record_command_response_for_command_id,
    record_cultivation_delta_from_text, telegram_event_message_context,
    telegram_message_topic_id,
)
from state_io import load_json_state, update_json_state

ROOT = Path(__file__).resolve().parent
BANK_FILE = ROOT / "xuangu_question_bank.json"
STATE_FILE = ROOT / "xuangu_quiz_state.json"
QUESTION_BOT_ID = 8388633812
JA_CHAT = -1001680975844
LINUXDO_CHAT = -1002083016447
JA_TOPIC = 7310786
DEADLINE_SECONDS = 300
SEND_MARGIN_SECONDS = 10
RESULT_GRACE_SECONDS = 30
LOG = logging.getLogger("xuangu_quiz")
INTRO_PATTERNS = (
    ("玄骨考校", re.compile(r"^神念直入脑海，一个苍老的声音向 @(.+?) 提问[：:]")),
    ("玄骨窥鼎", re.compile(r"^那道魔念绕着你识海中的虚天鼎缓缓打转，向 @(.+?) 阴恻恻地发问[：:]")),
    ("玄骨夺焰", re.compile(r"^一缕魔念直逼你识海中的乾蓝寒焰，玄骨上人的声音在 @(.+?) 脑海中炸响[：:]")),
)
RESULT_HEADING = re.compile(r"^【(玄骨(?:考校|窥鼎|夺焰))[·・](答对|答错|超时|题面失效)】")


def clean_text(text):
    return str(text or "").replace("**", "").replace("`", "").strip()


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def question_key(kind, question):
    return hashlib.sha256((kind + "\n" + normalized(question)).encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class Question:
    kind: str
    target: str
    text: str
    options: dict

    @property
    def key(self):
        return question_key(self.kind, self.text)


def parse_question(text):
    body = clean_text(text)
    if len(body) > 12000:
        return None
    match = next(((kind, pattern.match(body)) for kind, pattern in INTRO_PATTERNS if pattern.match(body)), None)
    if not match:
        return None
    kind, intro = match
    options = list(re.finditer(r"^([A-D])[.．、:：]\s*([^\n]+)$", body, re.M))
    if len(options) != 4 or {m[1] for m in options} != set("ABCD"):
        return None
    stem = body[intro.end():options[0].start()].strip().strip('“”"').strip()
    choices = {m[1]: m[2].strip() for m in options}
    if not stem or len(stem) > 1000 or any(not v or len(v) > 1000 for v in choices.values()):
        return None
    if not re.search(r"300\s*秒", body) or ".作答" not in body:
        return None
    return Question(kind, intro[1].strip(), stem, choices)


def parse_result(text):
    body = clean_text(text)
    heading = RESULT_HEADING.match(body)
    target = re.search(r"\n@(.+?) (?:的答案|面对|未能)", body)
    if not heading or not target:
        return None
    kind, outcome = heading.groups()
    correct = (re.search(r"正确答案\s*[:：]\s*([A-D])", body) if outcome == "答错" else
               re.search(r"的答案\s*([A-D])\s*完全正确", body) if outcome == "答对" else None)
    if outcome in {"答对", "答错"} and correct is None:
        return None
    return {"kind": kind, "target": target[1].strip(), "outcome": outcome,
            "correct_letter": correct[1] if correct else None}


def is_quiz_message(text):
    body = clean_text(text)
    return bool(RESULT_HEADING.match(body) or any(p.match(body) for _, p in INTRO_PATTERNS)
                or re.match(r"^【天机异象\s*·\s*玄骨(?:考校|窥鼎|夺焰)】", body))


def _state():
    return load_json_state(str(STATE_FILE), expected_type=dict, default={}) or {}


def _update(mutator):
    def apply(data):
        for key in ("events", "pending", "answers"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        mutator(data)
        # Old messages cannot become eligible again: message age is checked
        # independently of this bounded journal.
        if len(data["events"]) > 600:
            removable = sorted((e.get("created_at", 0), key) for key, e in data["events"].items()
                               if e.get("deadline", 0) + 86400 < time.time())
            for _, key in removable[:len(data["events"]) - 600]:
                data["events"].pop(key, None)
        return data
    return update_json_state(str(STATE_FILE), apply, default={})


def _bank():
    try:
        rows = json.loads(BANK_FILE.read_text(encoding="utf-8"))["questions"]
        result = {}
        for row in rows:
            key = question_key(row["event_type"], row["question"])
            answer = str(row["answer"]).strip()
            if not answer or (key in result and result[key] != answer):
                raise ValueError("conflicting question bank")
            result[key] = answer
        return result
    except (OSError, ValueError, KeyError, TypeError):
        LOG.error("[玄骨答题] 已验证题库读取失败，暂停使用内置答案")
        return {}


def answer_for(question, state=None):
    data = _state() if state is None else state
    if (data.get("pending", {}).get(question.key) or {}).get("status") == "conflict":
        return None
    confirmed = (data.get("answers", {}).get(question.key) or {}).get("answer")
    answer = confirmed or _bank().get(question.key)
    letters = [letter for letter, choice in question.options.items() if answer and normalized(choice) == normalized(answer)]
    return letters[0] if len(letters) == 1 else None


def quiz_callback_data(msg, letter):
    """Use only the original bot's complete, consistent A/B/C/D keyboard.

    A callback is sent as the logged-in Telegram user, not a channel avatar.
    The caller must therefore also verify that the question names its main soul.
    """
    choices, tokens = {}, set()
    for row in getattr(getattr(msg, "reply_markup", None), "rows", []) or []:
        for button in getattr(row, "buttons", []) or []:
            data = getattr(button, "data", None)
            match = re.fullmatch(rb"xgq:([A-Za-z0-9_-]{1,48}):([A-D])", data) if isinstance(data, bytes) else None
            if not match:
                return None
            option = match[2].decode("ascii")
            if clean_text(getattr(button, "text", "")) != option or option in choices:
                return None
            choices[option] = data
            tokens.add(match[1])
    return choices.get(letter) if set(choices) == set("ABCD") and len(tokens) == 1 else None


def _epoch(msg):
    date = getattr(msg, "date", None)
    if not isinstance(date, datetime):
        return 0
    return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()


def _quiz_chat_id(value):
    """Keep API-safe channel IDs while accepting either configured ID format."""
    normalized_id = normalize_telegram_chat_id(value)
    return next((chat for chat in (JA_CHAT, LINUXDO_CHAT)
                 if normalize_telegram_chat_id(chat) == normalized_id), None)


def _scope(actor, msg):
    chat = _quiz_chat_id(getattr(msg, "chat_id", None))
    allowed = {_quiz_chat_id(c) for c in actor_target_chat_ids(actor)}
    if chat not in {JA_CHAT, LINUXDO_CHAT} or chat not in allowed:
        return False
    if chat == JA_CHAT:
        topic = telegram_message_topic_id(msg)
        direct = getattr(msg, "reply_to_msg_id", None) or getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
        if topic != JA_TOPIC and not (topic is None and direct == JA_TOPIC):
            return False
    return True


def target_identity(actor, target, msg=None):
    """Match only the question's explicit target, never names in its options."""
    aliases = {}
    def add(identity, value):
        identity = canonical_automation_identity(getattr(actor, "account_key", ""), identity)
        aliases.setdefault(str(value or "").lstrip("@").casefold(), set()).add(identity)
    me = getattr(actor, "my_info", None)
    if getattr(me, "username", None):
        add("主魂", me.username)
    for identity, values in (getattr(actor, "identity_usernames", None) or {}).items():
        for value in [values] if isinstance(values, str) else values or []:
            add(identity, value)
    for value, identity in (getattr(actor, "avatar_usernames", None) or {}).items():
        if value and not str(value).casefold().endswith("bot"):
            add(identity, value)
    matches = set(aliases.get(str(target).lstrip("@").casefold(), ()))
    # A text-mention ID may identify a player without a username. Ignore entities
    # outside the introduction, so mentions embedded in a question cannot win.
    if msg is not None:
        prefix = str(getattr(msg, "text", "") or "").split("\n", 1)[0]
        prefix_length = len(prefix.encode("utf-16-le")) // 2
        for entity in getattr(msg, "entities", None) or []:
            player_id = getattr(entity, "user_id", None)
            if player_id is None or getattr(entity, "offset", prefix_length) >= prefix_length:
                continue
            if player_id == getattr(me, "id", None):
                matches.add("主魂")
            avatar = ({**(getattr(actor, "_avatar_chat_ids", None) or {}),
                       **(getattr(actor, "avatar_identities", None) or {})}).get(str(player_id))
            if avatar:
                matches.add(canonical_automation_identity(getattr(actor, "account_key", ""), avatar))
    managed = {"主魂", *(canonical_automation_identity(getattr(actor, "account_key", ""), a)
                           for a in getattr(actor, "avatars", []) or [])}
    return next(iter(matches)) if len(matches) == 1 and matches <= managed else None


def _event_key(msg):
    return f"{_quiz_chat_id(msg.chat_id)}:{msg.id}"


def _question_entry(msg, question):
    created = _epoch(msg)
    chat = _quiz_chat_id(msg.chat_id)
    return {"chat_id": chat, "message_id": msg.id, "topic_id": JA_TOPIC if chat == JA_CHAT else None,
            "created_at": created, "deadline": created + DEADLINE_SECONDS,
            "kind": question.kind, "target": question.target, "question": question.text,
            "options": question.options, "question_key": question.key,
            "status": "observed", "text": str(getattr(msg, "text", "") or ""),
            "link": ("https://t.me/ja_netfilter_group/" if chat == JA_CHAT else "https://t.me/c/2083016447/") + str(msg.id)}


def _remember_pending(data, entry, reason="unknown"):
    key = entry["question_key"]
    pending = data["pending"].setdefault(key, {
        "key": key, "event_type": entry["kind"], "question": entry["question"],
        "first_seen": time.time(), "sources": [], "option_versions": [],
    })
    pending.update(last_seen=time.time(), options=entry["options"], status=reason)
    if entry["options"] not in pending["option_versions"]:
        pending["option_versions"] = [*pending["option_versions"], entry["options"]][-20:]
    source = {"chat_id": entry["chat_id"], "message_id": entry["message_id"], "link": entry["link"]}
    if source not in pending["sources"]:
        pending["sources"] = [*pending["sources"], source][-10:]


def _writable(actor):
    return (not getattr(actor, "xuangu_quiz_callback_only", False)
            and not getattr(actor, "xuangu_quiz_read_only", False) and getattr(actor, "is_running", True))


def _can_callback(actor, identity):
    return (identity == "主魂" and not getattr(actor, "xuangu_quiz_read_only", False)
            and getattr(actor, "is_running", True))


def _set_event(key, *, expected_owner=None, expected_status=None, **values):
    def mutate(data):
        entry = data["events"].get(key)
        if entry and (expected_owner is None or entry.get("owner") == expected_owner) and (
                expected_status is None or entry.get("status") == expected_status):
            entry.update(values, updated_at=time.time())
    return _update(mutate).get("events", {}).get(key, {})


def _record_result(actor, entry):
    result = entry.get("result")
    if not result or not entry.get("sent_message_id"):
        return
    msg = SimpleNamespace(id=result["message_id"], chat_id=entry["chat_id"],
                          text=result["text"], sender_id=result["sender_id"], out=False)
    sender = SimpleNamespace(id=result["sender_id"], username=result["sender_username"], bot=True)
    record_command_response_for_command_id(actor, entry["sent_message_id"], msg,
                                          text=msg.text, sender=sender, logger=LOG)


async def _answer(actor, msg, question, key, owner, identity):
    """Prefer a main-soul callback; claim either transport before dispatch."""
    letter = answer_for(question)
    if not letter:
        _set_event(key, expected_owner=owner, expected_status="queued", status="skipped", reason="答案待确认")
        return
    command = ".作答 " + letter
    deadline = _epoch(msg) + DEADLINE_SECONDS - SEND_MARGIN_SECONDS
    signal = asyncio.Event()
    signals = getattr(actor, "_xuangu_quiz_signals", None)
    if signals is None:
        signals = actor._xuangu_quiz_signals = {}
    signals[key] = signal

    def dispatch_enabled(current_actor, *, callback=False):
        active = _can_callback(current_actor, identity) if callback else _writable(current_actor)
        if not active or time.time() >= deadline:
            return False
        if not xuangu_quiz_enabled(current_actor.account_key, identity):
            return False
        pause = getattr(current_actor, "pause_event", None)
        if pause is not None and not pause.is_set():
            return False
        if callback:
            return command_send_precheck(current_actor, command, LOG, identity=identity)
        target_chat, target_reply = actor_message_target(current_actor, reply_to=msg.id)
        if normalize_telegram_chat_id(target_chat) != normalize_telegram_chat_id(msg.chat_id) or target_reply != msg.id:
            return False
        return True

    async def allowed(current_actor, sending):
        if not dispatch_enabled(current_actor):
            return False
        if str(sending).strip() == command:
            try:
                live = await asyncio.wait_for(current_actor.client.get_messages(msg.chat_id, ids=msg.id),
                                              timeout=min(10, max(0.01, deadline - time.time())))
            except Exception:
                LOG.warning("[玄骨答题] 发送前无法重新确认题面 %s，跳过本次作答", key)
                return False
            if (not live or getattr(live, "sender_id", None) != QUESTION_BOT_ID
                    or _event_key(live) != key
                    or parse_question(getattr(live, "text", "")) != question or not _scope(current_actor, live)
                    or target_identity(current_actor, question.target, live) != identity):
                _set_event(key, expected_owner=owner, expected_status="queued", status="invalid", reason="发送前题面已删除、失效或发生变化")
                return False
            # Messages, switches and settings can change while the fetch awaits.
            if not dispatch_enabled(current_actor):
                return False
        accepted = False
        def claim(data):
            nonlocal accepted
            entry = data["events"].get(key, {})
            if entry.get("owner") != owner or entry.get("status") != "queued":
                return
            if answer_for(question, data) != command[-1]:
                return
            if str(sending).strip() == command:
                active = canonical_automation_identity(current_actor.account_key, getattr(current_actor, "current_identity", ""))
                if active != identity:
                    return
                entry.update(status="sending", attempt_at=time.time(), answer=command[-1], transport="command")
            accepted = True
        _update(claim)
        return accepted

    def sent(current_actor, sending, sent_msg):
        def update(data):
            entry = data["events"].get(key, {})
            if entry.get("owner") != owner:
                return
            entry.update(sent_message_id=sent_msg.id, sent_at=time.time())
            if entry.get("status") == "sending":
                entry["status"] = "awaiting_result"
        entry = _update(update)["events"].get(key, {})
        _record_result(current_actor, entry)
        LOG.info("[玄骨答题] [%s/%s] %s，回复题面 %s", current_actor.account_key, identity, command, key)

    async def click_answer(live, data):
        if not dispatch_enabled(actor, callback=True):
            return
        accepted = False
        def claim(state):
            nonlocal accepted
            entry = state["events"].get(key, {})
            if (entry.get("owner") == owner and entry.get("status") == "queued"
                    and answer_for(question, state) == letter):
                entry.update(status="sending", attempt_at=time.time(), answer=letter,
                             transport="callback", callback_data=data.decode("ascii"))
                accepted = True
        _update(claim)
        if not accepted:
            return
        # Once dispatched, an exception or empty callback response is ambiguous.
        # Never fall back to a group command or retry the button in that case.
        LOG.info("OUT [玄骨答题按钮 | %s/%s]:\n点击 %s（%s），题面 %s",
                 actor.account_key, identity, letter, question.options[letter], key)
        try:
            response = await asyncio.wait_for(live.click(data=data), timeout=min(15, max(0.01, deadline - time.time())))
        except Exception as exc:
            _set_event(key, expected_owner=owner, expected_status="sending", status="send_uncertain",
                       reason=f"按钮作答请求未确认（{type(exc).__name__}），不自动重发")
            LOG.warning("[玄骨答题] [%s/%s] 题面 %s 按钮请求未确认：%s", actor.account_key, identity, key, type(exc).__name__)
            return
        if response is None:
            _set_event(key, expected_owner=owner, expected_status="sending", status="send_uncertain",
                       reason="按钮作答请求未确认，不自动重发")
            return
        _set_event(key, expected_owner=owner,
                   callback_at=time.time(), callback_response=str(getattr(response, "message", "") or "")[:500])
        _set_event(key, expected_owner=owner, expected_status="sending", status="awaiting_result")

    try:
        with telegram_event_message_context(msg):
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            live = await asyncio.wait_for(actor.client.get_messages(msg.chat_id, ids=msg.id), timeout=min(15, remaining))
            live_question = parse_question(getattr(live, "text", "")) if live else None
            if (not live or getattr(live, "sender_id", None) != QUESTION_BOT_ID
                    or _event_key(live) != key
                    or live_question != question or not _scope(actor, live)
                    or target_identity(actor, question.target, live) != identity):
                _set_event(key, expected_owner=owner, expected_status="queued", status="invalid", reason="题面已删除、失效或发生变化")
                return
            original_data = quiz_callback_data(msg, letter) if identity == "主魂" else None
            callback_data = quiz_callback_data(live, letter) if identity == "主魂" else None
            if original_data and callback_data != original_data:
                _set_event(key, expected_owner=owner, expected_status="queued", status="invalid", reason="发送前答题按钮已移除或变化")
                return
            if callback_data:
                await click_answer(live, callback_data)
            elif identity == "主魂" and getattr(live, "reply_markup", None):
                _set_event(key, expected_owner=owner, expected_status="queued", status="skipped", reason="原题按钮无法核实，跳过作答")
            elif _writable(actor):
                with guarded_one_shot_send(command, allowed, sent):
                    await asyncio.wait_for(actor.send_and_wait_feedback_identity(
                        identity, command, reply_to=msg.id, max_retries=0, retry_on_timeout=False,
                        force_identity_check=True,
                        force_fresh_identity_confirm=bool(getattr(actor, "avatars", [])),
                        suppress_no_response_alert=True,
                    ), timeout=max(0.01, deadline - time.time()))
            else:
                _set_event(key, expected_owner=owner, expected_status="queued", status="skipped", reason="群发受限且没有可用的主魂答题按钮")
        entry = _state().get("events", {}).get(key, {})
        if entry.get("status") == "awaiting_result":
            try:
                await asyncio.wait_for(signal.wait(), timeout=max(0.01, _epoch(msg) + DEADLINE_SECONDS + RESULT_GRACE_SECONDS - time.time()))
            except asyncio.TimeoutError:
                if _state().get("events", {}).get(key, {}).get("status") == "awaiting_result":
                    _set_event(key, expected_owner=owner, expected_status="awaiting_result", status="unconfirmed", reason="未收到判题回执，保留发送记录且不重发")
    except asyncio.CancelledError:
        # A queued item may resume after restart; sending is never retried.
        raise
    except asyncio.TimeoutError:
        LOG.info("[玄骨答题] [%s/%s] 题目 %s 超过发送窗口", actor.account_key, identity, key)
    except Exception:
        LOG.exception("[玄骨答题] [%s/%s] 题目 %s 处理失败", actor.account_key, identity, key)
    finally:
        entry = _state().get("events", {}).get(key, {})
        if entry.get("owner") == owner:
            if entry.get("status") == "sending":
                _set_event(key, expected_owner=owner, expected_status="sending", status="send_uncertain", reason="发送结果未确认，不自动重发")
            elif entry.get("status") == "queued" and not asyncio.current_task().cancelling():
                _set_event(key, expected_owner=owner, expected_status="queued", status="expired" if time.time() >= deadline else "skipped",
                           reason="超过有效期" if time.time() >= deadline else "开关、身份或发送守卫未放行")
        signals.pop(key, None)


async def maybe_handle_xuangu_quiz(actor, event, text=None, sender=None, *, resume=False):
    msg = event.message
    body = clean_text(text if text is not None else getattr(msg, "text", ""))
    if not _scope(actor, msg):
        return False
    if re.fullmatch(r"[.。]作答\s+[A-Da-d]", body):
        sender_id = getattr(msg, "sender_id", None)
        own_ids = {str(getattr(getattr(actor, "my_info", None), "id", "")),
                   *(str(value) for value in (getattr(actor, "avatar_identities", None) or {})),
                   *(str(value) for value in (getattr(actor, "_avatar_chat_ids", None) or {}))}
        if str(sender_id) in own_ids:
            reply = getattr(msg, "reply_to_msg_id", None) or getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
            parent_key = f"{_quiz_chat_id(msg.chat_id)}:{reply}"
            def manual(data):
                entry = data["events"].get(parent_key, {})
                if entry and entry.get("status") in {"queued", "observed", "unknown"}:
                    entry.update(status="manual", manual_message_id=msg.id)
            _update(manual)
        return False
    if not is_quiz_message(body):
        return False
    sender = sender or await event.get_sender()
    if not is_game_bot_sender(actor, sender):
        return False
    result = parse_result(body)
    if result:
        changed = []
        now = _epoch(msg)
        def judge(data):
            matches = [(key, e) for key, e in data["events"].items()
                       if e["chat_id"] == _quiz_chat_id(msg.chat_id)
                       and e["kind"] == result["kind"] and e["target"].casefold() == result["target"].casefold()
                       and 0 <= now - e["created_at"] <= DEADLINE_SECONDS + 120
                       and e["message_id"] <= msg.id]
            exact = [(key, entry) for key, entry in matches if entry["message_id"] == msg.id]
            if exact:
                matches = exact
            if len(matches) != 1:
                return
            key, entry = matches[0]
            if entry.get("result"):
                if entry.get("account") == getattr(actor, "account_key", None) and not entry.get("result_processed"):
                    entry["result_processed"] = True
                    changed.append((key, dict(entry)))
                return
            if result["outcome"] == "题面失效":
                # An expired/closed question can be edited before the separate
                # judgment arrives. Stop sending, but let that judgment settle it.
                entry.update(status="invalid", invalidated_at=time.time())
                changed.append((key, dict(entry)))
                return
            outcome = {"答对": "correct", "答错": "incorrect", "超时": "timeout", "题面失效": "invalid"}[result["outcome"]]
            entry.update(status=outcome, result={"message_id": msg.id, "text": body,
                "sender_id": msg.sender_id, "sender_username": getattr(sender, "username", ""),
                "correct_letter": result["correct_letter"], "at": time.time()})
            letter = result["correct_letter"]
            if letter in entry["options"]:
                correct = entry["options"][letter]
                expected = (data["answers"].get(entry["question_key"], {}).get("answer") or _bank().get(entry["question_key"]))
                if expected and normalized(correct) != normalized(expected):
                    _remember_pending(data, entry, reason="conflict")
                pending = data["pending"].get(entry["question_key"])
                if pending:
                    pending.update(observed_answer=correct, judgment_link=("https://t.me/ja_netfilter_group/" if entry["chat_id"] == JA_CHAT else "https://t.me/c/2083016447/") + str(msg.id))
            if entry.get("account") == getattr(actor, "account_key", None):
                entry["result_processed"] = True
            changed.append((key, dict(entry)))
        _update(judge)
        for key, entry in changed:
            if entry.get("account") == getattr(actor, "account_key", None) and entry.get("result"):
                _record_result(actor, entry)
                recorded = entry["result"]
                result_msg = SimpleNamespace(id=recorded["message_id"], chat_id=entry["chat_id"], text=recorded["text"])
                record_cultivation_delta_from_text(actor, recorded["text"], identity=entry["identity"], logger=LOG, source="玄骨答题", msg=result_msg)
                LOG.info("IN [玄骨答题 | %s/%s]:\n%s：%s", actor.account_key, entry["identity"], entry["kind"], result["outcome"])
            signal = (getattr(actor, "_xuangu_quiz_signals", None) or {}).get(key)
            if signal:
                signal.set()
        return True
    question = parse_question(body)
    if not question or msg.sender_id != QUESTION_BOT_ID:
        return True
    created = _epoch(msg)
    if created <= 0 or created > time.time() + 30 or time.time() >= created + DEADLINE_SECONDS - SEND_MARGIN_SECONDS:
        return True
    identity = target_identity(actor, question.target, msg)
    key, owner = _event_key(msg), uuid.uuid4().hex
    letter = answer_for(question)
    eligible = bool(identity and letter and xuangu_quiz_enabled(actor.account_key, identity)
                    and (_writable(actor) or (_can_callback(actor, identity) and quiz_callback_data(msg, letter))))
    claimed = False
    def observe(data):
        nonlocal claimed
        entry = data["events"].setdefault(key, _question_entry(msg, question))
        if entry["question_key"] != question.key or entry["options"] != question.options:
            entry.update(status="invalid", reason="同一消息的题面发生变化")
            return
        if not letter:
            _remember_pending(data, entry, reason="conflict" if (data["pending"].get(question.key) or {}).get("status") == "conflict" else "unknown")
            if entry["status"] == "observed":
                entry["status"] = "unknown"
        if not eligible or entry["status"] not in {"observed", "queued"}:
            return
        if entry["status"] == "queued" and not (resume and entry.get("account") == actor.account_key):
            return
        entry.update(status="queued", owner=owner, account=actor.account_key, identity=identity)
        claimed = True
    _update(observe)
    if claimed:
        tasks = getattr(actor, "_xuangu_quiz_tasks", None)
        if tasks is None:
            tasks = actor._xuangu_quiz_tasks = {}
        task = asyncio.create_task(_answer(actor, msg, question, key, owner, identity), name="xuangu_quiz_" + key)
        tasks[key] = task
        def done(finished):
            if tasks.get(key) is finished:
                tasks.pop(key, None)
            if not finished.cancelled() and (error := finished.exception()):
                LOG.error("[玄骨答题] 事件任务未完成：%s", key,
                          exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(done)
    elif identity and not letter:
        LOG.info("[玄骨答题] [%s/%s] 未知题已保存，等待补充答案：%s", actor.account_key, identity, question.text)
    return True


async def resume_pending_xuangu_quiz_events(actor):
    for entry in list(_state().get("events", {}).values()):
        if entry.get("account") != getattr(actor, "account_key", None) or entry.get("status") != "queued":
            continue
        if time.time() >= entry["deadline"] - SEND_MARGIN_SECONDS:
            _set_event(f"{entry['chat_id']}:{entry['message_id']}", status="expired", reason="重启时题目已过期")
            continue
        try:
            msg = await asyncio.wait_for(actor.client.get_messages(entry["chat_id"], ids=entry["message_id"]), timeout=15)
            if msg:
                sender = await msg.get_sender()
                await maybe_handle_xuangu_quiz(actor, SimpleNamespace(message=msg), sender=sender, resume=True)
        except Exception:
            LOG.exception("[玄骨答题] 恢复未发送题目失败")


def quiz_dashboard_payload():
    data = _state()
    pending = [p for p in data.get("pending", {}).values() if p.get("status") != "resolved"]
    answers = {**_bank(), **{key: value.get("answer") for key, value in data.get("answers", {}).items()}}
    own_events = [e for e in data.get("events", {}).values() if e.get("account")]
    return {"known_count": len(answers), "pending_count": len(pending),
            "pending": sorted(pending, key=lambda p: p.get("last_seen", 0), reverse=True)[:50],
            "recent_events": sorted(own_events, key=lambda e: e.get("created_at", 0), reverse=True)[:12]}


def confirm_quiz_answer(key, answer, updated_by="dashboard"):
    """Confirm an answer's content; never use its historical option letter."""
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{24}", key):
        raise ValueError("invalid quiz question")
    if not isinstance(answer, str) or not answer or len(answer) > 1000:
        raise ValueError("invalid quiz answer")
    def confirm(data):
        pending = data["pending"].get(key)
        if not pending:
            raise ValueError("quiz question not found")
        choices = [value for options in pending.get("option_versions", []) for value in options.values()]
        if answer not in choices:
            raise ValueError("answer must be an observed option")
        data["answers"][key] = {"answer": answer, "confirmed_at": time.time(), "confirmed_by": str(updated_by)[:80]}
        pending["status"] = "resolved"
    _update(confirm)
    return {"question_key": key, "answer": answer}
