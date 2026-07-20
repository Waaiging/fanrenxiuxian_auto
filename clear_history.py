#!/usr/bin/env python3
"""
【清理历史指令工具】

用于扫描 Telegram 会话中本账号发出的点号指令，并删除超过指定时间的旧消息。
主脚本里的“清屏”控制最终也会复用这里的思路。

注意：
- 默认会复制 session 后再扫描，降低和正在运行脚本抢 SQLite session 的风险。
- dry_run=True 时只打印将删除的消息，不真正删除。
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient


CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

ACCOUNTS = {
    "main": {
        "session": "telegram_cli_session",
        "config": "config.json",
        "label": "main",
    },
    "sub": {
        "session": "sub_account_session",
        "config": "config_sub.json",
        "label": "sub",
    },
    "xiaohao": {
        "session": "xiaohao_session",
        "config": "config.json",
        "label": "xiaohao",
    },
    "waaiging": {
        "session": "waaiging_session",
        "config": "config.json",
        "label": "waaiging",
    },
}


def session_base_to_file(session_base):
    return session_base if session_base.endswith(".session") else f"{session_base}.session"


def cleanup_temp_session(session_base):
    for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
        path = f"{session_base}{suffix}"
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def make_session_copy(session_base):
    source = session_base_to_file(session_base)
    if not os.path.exists(source):
        raise FileNotFoundError(f"Session file not found: {source}")
    temp_base = os.path.join(
        CONFIG_DIR,
        f".clear_{os.path.basename(session_base)}_{os.getpid()}_{uuid.uuid4().hex}",
    )
    shutil.copy2(source, session_base_to_file(temp_base))
    return temp_base


def load_account_config(account):
    if account not in ACCOUNTS:
        raise ValueError(f"Unknown account: {account}")

    meta = ACCOUNTS[account]
    config_path = os.path.join(CONFIG_DIR, meta["config"])
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    monitor = config.get("monitor", {})
    return {
        "api_id": config["api_id"],
        "api_hash": config["api_hash"],
        "session": os.path.join(CONFIG_DIR, meta["session"]),
        "chat_id": monitor.get("chat_id", "fanrenxxz"),
        "topic_id": monitor.get("topic_id"),
        "label": meta["label"],
    }


def normalize_group_target(target):
    """Convert a configured numeric channel ID to Telethon's marked peer ID."""
    if isinstance(target, bool) or target is None:
        return target
    if isinstance(target, int):
        numeric = target
    elif isinstance(target, str):
        stripped = target.strip()
        if not stripped.lstrip("-").isdigit():
            return stripped
        numeric = int(stripped)
    else:
        return target

    # Config stores the raw channel_id (for example 2083016447), while
    # Telethon needs the marked supergroup ID (-1002083016447).
    if numeric > 0:
        return int(f"-100{numeric}")
    return numeric


async def resolve_group_entity(client, configured_target):
    target = normalize_group_target(configured_target)
    try:
        return await client.get_input_entity(target)
    except ValueError as exc:
        raise RuntimeError(
            f"无法定位目标群 {configured_target!r}，请检查 monitor.chat_id，并确认该账号仍在群内。"
        ) from exc


def is_in_topic(msg, topic_id):
    if not topic_id:
        return True

    reply_to = getattr(msg, "reply_to", None)
    candidates = [
        getattr(msg, "reply_to_msg_id", None),
        getattr(msg, "reply_to_top_id", None),
    ]

    if not reply_to:
        return topic_id in candidates

    candidates.extend([
        getattr(reply_to, "reply_to_top_id", None),
        getattr(reply_to, "reply_to_msg_id", None),
    ])
    return topic_id in candidates


async def delete_sent_messages(account, scan_limit, topic_only=False, dry_run=False, older_than_minutes=35, use_session_copy=True):
    cfg = load_account_config(account)
    session_to_use = cfg["session"]
    temp_session = ""
    if use_session_copy:
        temp_session = make_session_copy(cfg["session"])
        session_to_use = temp_session
    client = TelegramClient(session_to_use, cfg["api_id"], cfg["api_hash"])
    try:
        await client.start()
        chat = await resolve_group_entity(client, cfg["chat_id"])
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(0, older_than_minutes))
        deleted = 0
        scanned = 0
        outgoing = 0
        in_topic = 0
        game_commands = 0
        old_enough = 0
        selected = 0
        batch = []
        me = await client.get_me()
        async for msg in client.iter_messages(chat, limit=scan_limit, from_user=me):
            scanned += 1
            if not getattr(msg, "out", False):
                continue
            outgoing += 1

            text = (getattr(msg, "raw_text", None) or getattr(msg, "text", None) or "").strip()
            if not text.startswith("."):
                continue
            game_commands += 1

            msg_date = getattr(msg, "date", None)
            if msg_date is None:
                continue
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date > cutoff:
                continue
            old_enough += 1

            msg_in_topic = is_in_topic(msg, cfg["topic_id"])
            if msg_in_topic:
                in_topic += 1
            if topic_only and not msg_in_topic:
                continue

            selected += 1
            if dry_run:
                continue

            batch.append(msg.id)
            if len(batch) >= 100:
                await client.delete_messages(chat, batch, revoke=True)
                deleted += len(batch)
                batch.clear()

        if batch:
            await client.delete_messages(chat, batch, revoke=True)
            deleted += len(batch)

        return scanned, outgoing, game_commands, old_enough, in_topic, selected, deleted
    finally:
        await client.disconnect()
        if temp_session:
            cleanup_temp_session(temp_session)


def main():
    parser = argparse.ArgumentParser(description="删除指定账号在目标群里 35 分钟以前的点号游戏指令。")
    parser.add_argument("account", choices=sorted(ACCOUNTS.keys()))
    parser.add_argument("--scan-limit", type=int, default=None)
    parser.add_argument("--older-than-minutes", type=int, default=35)
    parser.add_argument("--topic-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-session-copy", action="store_true", help="直接使用原 session；默认使用临时副本以避免脚本运行时数据库锁。")
    args = parser.parse_args()

    try:
        scanned, outgoing, game_commands, old_enough, in_topic, selected, deleted = asyncio.run(
            delete_sent_messages(
                args.account,
                args.scan_limit,
                topic_only=args.topic_only,
                dry_run=args.dry_run,
                older_than_minutes=args.older_than_minutes,
                use_session_copy=not args.no_session_copy,
            )
        )
    except Exception as exc:
        print(f"{args.account} 清屏失败：{exc}", file=sys.stderr)
        return 1
    mode = "预览" if args.dry_run else "删除"
    scope = "当前话题" if args.topic_only else "目标群"
    print(
        f"{args.account} 清屏完成：模式={mode}，范围={scope}，"
        f"仅处理 {args.older_than_minutes} 分钟以前的 . 开头游戏指令，"
        f"扫描自己消息={scanned} 条，自己发言={outgoing} 条，"
        f"游戏指令={game_commands} 条，超过阈值={old_enough} 条，"
        f"当前话题匹配={in_topic} 条，选中={selected} 条，已删除={deleted} 条"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
