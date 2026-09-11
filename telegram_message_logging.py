"""Read-only capture of mentions and replies in an account's monitored chats."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from telethon import events, utils

import log_utils as lu


def explicit_mention_identities(actor, msg, text):
    # Telegram's `mentioned` flag also covers replies: do not mistake it for @.
    mentions = set(lu.text_username_mentions(text))
    matched = [identity for identity, names in lu._identity_profile_usernames(actor).items()
               if mentions.intersection(names)]
    ids = {}
    my_id = lu._safe_message_int(getattr(getattr(actor, "my_info", None), "id", None))
    if my_id is not None:
        ids[my_id] = "主魂"
    for sender_id, identity in (getattr(actor, "_avatar_chat_ids", {}) or {}).items():
        numeric_id = lu._safe_message_int(sender_id)
        if numeric_id is not None:
            ids[numeric_id] = identity
    for entity in getattr(msg, "entities", None) or []:
        entity_id = lu._safe_message_int(getattr(entity, "user_id", None))
        url = str(getattr(entity, "url", "") or "")
        if url.startswith("tg://user"):
            entity_id = lu._safe_message_int(lu.parse_qs(lu.urlparse(url).query).get("id", [None])[0])
        identity = ids.get(entity_id)
        if identity and identity not in matched:
            matched.append(identity)
    return matched


def message_log_text(msg, text=None):
    text = str(text if text is not None else getattr(msg, "text", None) or "")
    if text:
        return text
    for attr, label in (("photo", "照片"), ("voice", "语音"), ("video", "视频"),
                        ("sticker", "贴纸"), ("document", "文件"), ("poll", "投票"),
                        ("media", "媒体消息")):
        if getattr(msg, attr, None):
            return f"[{label}]"
    return "[无文字消息]"


def _own_identity(actor, msg):
    if not lu._is_own_outgoing_sender(actor, msg):
        return ""
    sender_id = str(getattr(msg, "sender_id", ""))
    for avatar_id, identity in (getattr(actor, "_avatar_chat_ids", {}) or {}).items():
        if sender_id == str(avatar_id):
            return str(identity)
    return "主魂"


def _message_scope(actor, msg):
    return (lu.actor_account_key(actor) or actor.__class__.__name__,
            lu._safe_message_int(getattr(msg, "chat_id", None)),
            lu._safe_message_int(getattr(msg, "id", None)))


def _previous_attention(actor, msg):
    key = _message_scope(actor, msg)
    cached = (getattr(actor, "_telegram_attention_cache", None) or {}).get(key)
    if cached:
        return cached
    if key[1] is None or key[2] is None:
        return {}
    try:
        with lu._message_db_connect() as conn:
            row = conn.execute(
                "SELECT attention FROM message_events WHERE account=? AND chat_id=? AND msg_id=? "
                "AND attention!='' ORDER BY id DESC LIMIT 1", key,
            ).fetchone()
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


async def reply_identity(actor, msg):
    """Resolve the actual parent; topic IDs and cross-chat ID collisions are not replies."""
    reply = getattr(msg, "reply_to", None)
    replied_id = lu._safe_message_int(getattr(reply, "reply_to_msg_id", None)
                                    or getattr(msg, "reply_to_msg_id", None))
    chat_id = lu._safe_message_int(getattr(msg, "chat_id", None))
    peer = getattr(reply, "reply_to_peer_id", None)
    if peer is not None:
        chat_id = utils.get_peer_id(peer)
    if not replied_id or chat_id is None:
        return ""
    # A forum thread marker points at the topic's creation service message.
    if getattr(reply, "forum_topic", False) and (
        not getattr(reply, "reply_to_top_id", None)
        or replied_id == getattr(reply, "reply_to_top_id", None)
    ):
        return ""
    if peer is None and replied_id == lu._safe_message_int(getattr(actor, "topic_id", None)):
        return ""
    account = _message_scope(actor, msg)[0]
    key = (account, chat_id, replied_id)
    cache = getattr(actor, "_telegram_reply_owner_cache", None)
    if cache is None:
        cache = actor._telegram_reply_owner_cache = {}
    if key in cache:
        return cache[key]
    row = command = None
    try:
        with lu._message_db_connect() as conn:
            row = conn.execute(
                "SELECT sender_id, is_out FROM message_events WHERE account=? AND chat_id=? AND msg_id=? "
                "ORDER BY id DESC LIMIT 1", key,
            ).fetchone()
            if row is None or row[1]:
                command = conn.execute(
                    "SELECT identity FROM command_ledger WHERE account=? AND chat_id=? AND command_msg_id=? LIMIT 1", key,
                ).fetchone()
    except Exception:
        pass
    identity = ""
    if command:
        identity = command[0] or "主魂"
    elif row:
        identity = _own_identity(actor, SimpleNamespace(sender_id=row[0], out=bool(row[1])))
    else:
        parent = None
        get_parent = getattr(msg, "get_reply_message", None)
        if callable(get_parent):
            try:
                parent = await asyncio.wait_for(get_parent(), timeout=2)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(actor.__class__.__name__).debug(
                    "Reply parent unavailable: chat=%s message=%s", chat_id, replied_id,
                )
        if parent is None:
            # A missing/deleted parent is unknown, not a negative ownership fact.
            return ""
        if (lu._safe_message_int(getattr(parent, "id", None)) != replied_id
                or lu._safe_message_int(getattr(parent, "chat_id", None)) != chat_id):
            return ""
        identity = _own_identity(actor, parent)
        lu.record_message_event(actor, parent, event_kind="history", direction="raw")
    cache[key] = identity
    if len(cache) > 1000:
        actor._telegram_reply_owner_cache = dict(list(cache.items())[-500:])
    return identity


async def log_addressed_message_if_needed(actor, msg, text=None, sender=None, *, edited=False):
    """Capture once per observed version without claiming a gameplay response."""
    if msg is None or lu._is_own_outgoing_sender(actor, msg):
        return False
    logger = logging.getLogger(actor.__class__.__name__)
    try:
        text = message_log_text(msg, text)
        mentioned = explicit_mention_identities(actor, msg, text)
        if not edited and not mentioned and not (lu._reply_to_msg_id(msg) or getattr(msg, "reply_to_msg_id", None)):
            return False
        previous = _previous_attention(actor, msg)
        replied_identity = await reply_identity(actor, msg)
        if not mentioned and not replied_identity and not (edited and previous):
            return False
        if sender is None:
            sender = getattr(msg, "sender", None)
            getter = getattr(msg, "get_sender", None)
            if sender is None and callable(getter):
                try:
                    sender = await asyncio.wait_for(getter(), timeout=2)
                except Exception:
                    pass
        fingerprint = lu._message_text_hash(json.dumps(
            [text, mentioned, replied_identity, str(getattr(msg, "media", None))], ensure_ascii=False,
        ))
        cache = getattr(actor, "_telegram_attention_cache", None)
        if cache is None:
            cache = actor._telegram_attention_cache = {}
        key = _message_scope(actor, msg)
        texts = getattr(actor, "_logged_attention_message_texts", None)
        if texts is None:
            texts = actor._logged_attention_message_texts = {}
        if previous.get("fingerprint") == fingerprint:
            cache[key] = previous
            texts[lu.telegram_message_key(msg)] = text
            return True
        relations = (["mention"] if mentioned else []) + (["reply"] if replied_identity else [])
        attention = {
            "relations": relations or previous.get("relations", []),
            "mentions": mentioned or (previous.get("mentions", []) if edited else []),
            "reply_identity": replied_identity,
            "reply_to_msg_id": lu._safe_message_int(getattr(msg, "reply_to_msg_id", None)
                                                   or lu._reply_to_msg_id(msg)),
            "fingerprint": fingerprint,
        }
        logged = lu.log_mention_if_needed(actor, msg, text=text, sender=sender,
                                         label="edited" if edited else "mention",
                                         mentions_only=True, attention=attention)
        if logged:
            cache[key] = attention
            texts[lu.telegram_message_key(msg)] = text
            if len(cache) > 500:
                actor._telegram_attention_cache = dict(list(cache.items())[-250:])
                actor._logged_attention_message_texts = dict(list(texts.items())[-250:])
        return logged
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Mention/reply logging failed")
        return False


def install_addressed_message_monitor(actor):
    """Keep capture running even when Telegram writes/Mini App startup are restricted."""
    async def handle(event, *, edited=False):
        msg = event.message
        sender = getattr(msg, "sender", None)
        if sender is None:
            try:
                sender = await event.get_sender()
            except Exception:
                pass
        lu.record_message_event(actor, msg, sender=sender,
                                event_kind="edited" if edited else "new", direction="raw")
        await log_addressed_message_if_needed(actor, msg, sender=sender, edited=edited)

    async def edited_handler(event):
        await handle(event, edited=True)

    registrations = [(handle, events.NewMessage(chats=lu.actor_target_chat_ids(actor))),
                     (edited_handler, events.MessageEdited(chats=lu.actor_target_chat_ids(actor)))]
    for callback, builder in registrations:
        actor.client.add_event_handler(callback, builder)
    logging.getLogger(actor.__class__.__name__).info("Mention/reply log monitor active for chats %s", lu.actor_target_chat_ids(actor))
    return registrations
