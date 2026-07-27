#!/usr/bin/env python3
"""Run only the red-packet listener for a restricted Telegram account."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from telethon import TelegramClient

from red_packet_features import install_red_packet_monitor


CONFIG_DIR = Path(__file__).resolve().parent
ACCOUNT_SESSIONS = {
    "xiaohao": ("config.json", "xiaohao_session"),
    "waaiging": ("config.json", "waaiging_session"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restricted-account red-packet listener")
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNT_SESSIONS))
    return parser.parse_args()


async def run(account: str) -> None:
    config_name, session_name = ACCOUNT_SESSIONS[account]
    with (CONFIG_DIR / config_name).open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    logger = logging.getLogger(f"red_packet.{account}")
    client = TelegramClient(
        str(CONFIG_DIR / session_name),
        config["api_id"],
        config["api_hash"],
    )
    monitor = None
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(f"Telegram session for {account} is not authorized")
        monitor = await install_red_packet_monitor(client, account, logger=logger)
        if not monitor.topic_id:
            raise RuntimeError(f"red-packet monitor for {account} was not installed")
        logger.warning("[%s] Restricted account entered red-packet standby mode", account)
        await client.run_until_disconnected()
    finally:
        if monitor is not None:
            monitor.mark_stopped("standby_stopped")
        await client.disconnect()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    asyncio.run(run(args.account))


if __name__ == "__main__":
    main()
