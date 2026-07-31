#!/usr/bin/env python3
"""Run red-packet and Mini App automation for a write-restricted account."""

from __future__ import annotations

import argparse
import asyncio
import logging

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


async def run(account: str) -> None:
    logger = logging.getLogger(f"red_packet.{account}")
    actor = build_actor(account)
    client = actor.client
    monitor = None
    miniapp_worker = None
    world_boss_monitor = None
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(f"Telegram session for {account} is not authorized")
        actor.my_info = await client.get_me()
        monitor = await install_red_packet_monitor(client, account, logger=logger)
        if not monitor.topic_id:
            raise RuntimeError(f"red-packet monitor for {account} was not installed")
        miniapp_worker = RestrictedMiniAppWorker(actor, account, logger=logger)
        actor._restricted_miniapp_worker = miniapp_worker
        try:
            await miniapp_worker.start()
        except Exception as exc:
            actor.state["restricted_miniapp_active"] = False
            actor.state["restricted_miniapp_last_error"] = (
                getattr(exc, "code", "") or type(exc).__name__.lower()
            )
            actor.save_state()
            logger.error(
                "[%s] Mini App scheduler failed to start; red-packet listener remains active",
                account,
                exc_info=True,
            )
        world_boss_monitor = await install_world_boss_monitor(
            actor,
            account,
            logger=logger,
            transport=miniapp_worker.transport,
        )
        logger.warning("[%s] Restricted account entered Mini App standby mode", account)
        await client.run_until_disconnected()
    finally:
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
