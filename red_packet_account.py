#!/usr/bin/env python3
"""Run red-packet and Mini App automation for a write-restricted account."""

from __future__ import annotations

import argparse
import asyncio
import logging

from telethon import events

from auto_reply_features import maybe_restricted_exchange_place
from log_utils import resolve_target_chat_id
from red_packet_features import install_red_packet_monitor
from restricted_miniapp_worker import RestrictedMiniAppWorker
from world_boss_features import install_world_boss_monitor


ACCOUNT_SESSIONS = {
    "xiaohao": "xiaohao_session",
    "waaiging": "waaiging_session",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restricted-account Mini App worker")
    parser.add_argument("--account", required=True, choices=sorted(ACCOUNT_SESSIONS))
    return parser.parse_args()


def build_actor(account: str):
    session_name = ACCOUNT_SESSIONS[account]
    if account == "xiaohao":
        from cultivator_xiaohao import CultivatorXiaoHao

        return CultivatorXiaoHao(session_name=session_name)
    if account == "waaiging":
        from cultivator_waaiging import WaaigingCultivator

        return WaaigingCultivator(session_name=session_name)
    raise ValueError(f"Unsupported restricted account: {account}")


def install_restricted_exchange_monitor(actor, logger=None):
    """Listen for South Long Marquis events while the account is in standby mode."""
    log = logger or logging.getLogger(f"red_packet.{actor.account_key}")

    async def handle_event(event):
        try:
            await maybe_restricted_exchange_place(actor, event)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error(
                "[%s] Restricted South Long Marquis monitor failed",
                actor.account_key,
                exc_info=True,
            )

    builders = (
        events.NewMessage(chats=actor.target_chat_id),
        events.MessageEdited(chats=actor.target_chat_id),
    )
    registrations = []
    for builder in builders:
        actor.client.add_event_handler(handle_event, builder)
        registrations.append((handle_event, builder))
    log.warning(
        "[%s] Restricted South Long Marquis Mini App monitor active for chat %s",
        actor.account_key,
        actor.target_chat_id,
    )
    return registrations


async def run(account: str) -> None:
    logger = logging.getLogger(f"red_packet.{account}")
    actor = build_actor(account)
    client = actor.client
    monitor = None
    miniapp_worker = None
    world_boss_monitor = None
    exchange_handlers = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(f"Telegram session for {account} is not authorized")
        actor.my_info = await client.get_me()
        actor.target_chat_id = await resolve_target_chat_id(
            client,
            actor.target_chat_id,
            logger,
        )
        monitor = await install_red_packet_monitor(client, account, logger=logger)
        if not monitor.topic_id:
            raise RuntimeError(f"red-packet monitor for {account} was not installed")
        miniapp_worker = RestrictedMiniAppWorker(actor, account, logger=logger)
        actor._restricted_miniapp_worker = miniapp_worker
        miniapp_started = False
        try:
            await miniapp_worker.start()
            miniapp_started = True
        except Exception as exc:
            actor.state["restricted_miniapp_active"] = False
            actor.state["restricted_miniapp_last_error"] = (
                getattr(exc, "code", "") or type(exc).__name__.lower()
            )
            actor.save_state()
            code = getattr(exc, "code", "") or type(exc).__name__.lower()
            if code == "dwelling_token_expired":
                logger.error(
                    "[%s] Mini App scheduler disabled: fixed entry token expired; "
                    "red-packet listener remains active",
                    account,
                )
            else:
                logger.error(
                    "[%s] Mini App scheduler failed to start; red-packet listener remains active",
                    account,
                    exc_info=True,
                )
        if miniapp_started:
            exchange_handlers = install_restricted_exchange_monitor(actor, logger=logger)
        world_boss_monitor = await install_world_boss_monitor(
            actor,
            account,
            logger=logger,
            transport=miniapp_worker.transport,
        )
        logger.warning("[%s] Restricted account entered Mini App standby mode", account)
        await client.run_until_disconnected()
    finally:
        for callback, builder in exchange_handlers:
            client.remove_event_handler(callback, builder)
        if world_boss_monitor is not None:
            await world_boss_monitor.stop()
        if miniapp_worker is not None:
            await miniapp_worker.stop()
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
