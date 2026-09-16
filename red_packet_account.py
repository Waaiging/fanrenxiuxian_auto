#!/usr/bin/env python3
"""Run red-packet and Mini App automation for a write-restricted account."""

from __future__ import annotations

import argparse
import asyncio
import logging

from telethon import events

from auto_reply_features import maybe_restricted_exchange_place
from log_utils import actor_target_chat_ids, resolve_actor_target_chats, routed_telegram_event_handler
from miniapp_beast import MiniAppCircuitOpenError, miniapp_circuit_wait_seconds
from red_packet_features import install_red_packet_monitor
from restricted_miniapp_worker import RestrictedMiniAppWorker
from world_boss_features import install_world_boss_monitor
from nangongque_boss import install_nangongque_boss_monitor
from xuangu_quiz_features import maybe_handle_xuangu_quiz, resume_pending_xuangu_quiz_events
from telegram_message_logging import install_addressed_message_monitor


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

    @routed_telegram_event_handler
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
        events.NewMessage(chats=actor_target_chat_ids(actor)),
        events.MessageEdited(chats=actor_target_chat_ids(actor)),
    )
    registrations = []
    for builder in builders:
        actor.client.add_event_handler(handle_event, builder)
        registrations.append((handle_event, builder))
    log.warning(
        "[%s] Restricted South Long Marquis Mini App monitor active for chats %s",
        actor.account_key,
        actor_target_chat_ids(actor),
    )
    return registrations


def install_restricted_quiz_monitor(actor):
    """Prefer bot answer buttons for enabled identities while group writes stay off."""
    actor.xuangu_quiz_callback_only = True
    @routed_telegram_event_handler
    async def handle_event(event):
        try:
            await maybe_handle_xuangu_quiz(actor, event)
        except Exception:
            logging.getLogger("xuangu_quiz").exception("[玄骨答题] 受限账号题目监听失败")
    registrations = []
    for builder in (events.NewMessage(chats=actor_target_chat_ids(actor)),
                    events.MessageEdited(chats=actor_target_chat_ids(actor))):
        actor.client.add_event_handler(handle_event, builder)
        registrations.append((handle_event, builder))
    return registrations


async def resume_restricted_miniapp_after_circuit(
    worker,
    actor,
    account: str,
    first_error: MiniAppCircuitOpenError,
    logger=None,
) -> bool:
    """Resume only at breaker-approved times; never poll a failed upstream."""
    log = logger or logging.getLogger(f"red_packet.{account}")
    circuit_error = first_error
    last_logged_retry_at = ""
    while getattr(actor, "is_running", True):
        wait = miniapp_circuit_wait_seconds(circuit_error, 60)
        retry_at = circuit_error.retry_at or f"in {wait}s"
        if retry_at != last_logged_retry_at:
            log.info(
                "[%s] Mini App scheduler will make one recovery attempt at %s; "
                "no requests will be sent before then",
                account,
                retry_at,
            )
            last_logged_retry_at = retry_at
        await asyncio.sleep(wait)
        if not getattr(actor, "is_running", True):
            return False
        try:
            await worker.start()
        except asyncio.CancelledError:
            raise
        except MiniAppCircuitOpenError as exc:
            circuit_error = exc
            continue
        except Exception as exc:
            code = getattr(exc, "code", "") or type(exc).__name__.lower()
            actor.state["restricted_miniapp_active"] = False
            actor.state["restricted_miniapp_last_error"] = code
            actor.save_state()
            log.error(
                "[%s] Mini App scheduler recovery stopped after a non-upstream error: %s",
                account,
                code,
                exc_info=True,
            )
            return False
        log.warning(
            "[%s] Mini App upstream recovered; restricted scheduler resumed",
            account,
        )
        return True
    return False


async def run(account: str) -> None:
    logger = logging.getLogger(f"red_packet.{account}")
    actor = build_actor(account)
    client = actor.client
    monitor = None
    miniapp_worker = None
    miniapp_recovery_task = None
    world_boss_monitor = None
    nangongque_boss_monitor = None
    exchange_handlers = []
    quiz_handlers = []
    quiz_resume_task = None
    addressed_handlers = []
    surprise_raid_task = None
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(f"Telegram session for {account} is not authorized")
        actor.my_info = await client.get_me()
        await resolve_actor_target_chats(actor, logger)
        addressed_handlers = install_addressed_message_monitor(actor)
        quiz_handlers = install_restricted_quiz_monitor(actor)
        quiz_resume_task = asyncio.create_task(resume_pending_xuangu_quiz_events(actor))
        monitor = await install_red_packet_monitor(client, account, logger=logger)
        if not monitor.topic_id:
            raise RuntimeError(f"red-packet monitor for {account} was not installed")
        miniapp_worker = RestrictedMiniAppWorker(actor, account, logger=logger)
        actor._restricted_miniapp_worker = miniapp_worker
        # The restricted worker owns the normal startup reconciliation, but a
        # fixed-entry-token failure must not leave the group-command-only
        # surprise raid scheduler waiting forever in standby mode.
        actor.startup_done.set()
        if hasattr(actor, "run_surprise_raid_scheduler"):
            surprise_raid_task = asyncio.create_task(
                actor.run_surprise_raid_scheduler(initial_delay=30),
                name=f"surprise_raid_{account}",
            )
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
            if isinstance(exc, MiniAppCircuitOpenError):
                actor.state["restricted_miniapp_retry_at"] = exc.retry_at
                actor.save_state()
                logger.info(
                    "[%s] Mini App scheduler paused by upstream circuit until %s; "
                    "red-packet listener remains active",
                    account,
                    exc.retry_at or f"in {exc.retry_after}s",
                )

                async def recover_scheduler(circuit_error=exc) -> None:
                    recovered = await resume_restricted_miniapp_after_circuit(
                        miniapp_worker,
                        actor,
                        account,
                        circuit_error,
                        logger=logger,
                    )
                    if recovered:
                        exchange_handlers.extend(
                            install_restricted_exchange_monitor(actor, logger=logger)
                        )

                miniapp_recovery_task = asyncio.create_task(
                    recover_scheduler(),
                    name=f"miniapp_{account}_startup_recovery",
                )
            elif code == "dwelling_token_expired":
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
        nangongque_boss_monitor = await install_nangongque_boss_monitor(
            actor, account, logger=logger, transport=miniapp_worker.transport,
        )
        logger.warning("[%s] Restricted account entered Mini App standby mode", account)
        await client.run_until_disconnected()
    finally:
        if miniapp_recovery_task is not None:
            miniapp_recovery_task.cancel()
            await asyncio.gather(miniapp_recovery_task, return_exceptions=True)
        for callback, builder in exchange_handlers:
            client.remove_event_handler(callback, builder)
        for callback, builder in quiz_handlers:
            client.remove_event_handler(callback, builder)
        quiz_tasks = [task for task in [quiz_resume_task, *(getattr(actor, "_xuangu_quiz_tasks", {}) or {}).values()]
                      if task is not None]
        for task in quiz_tasks:
            task.cancel()
        await asyncio.gather(*quiz_tasks, return_exceptions=True)
        for callback, builder in addressed_handlers:
            client.remove_event_handler(callback, builder)
        if world_boss_monitor is not None:
            await world_boss_monitor.stop()
        if nangongque_boss_monitor is not None:
            await nangongque_boss_monitor.stop()
        if miniapp_worker is not None:
            await miniapp_worker.stop()
        if surprise_raid_task is not None:
            surprise_raid_task.cancel()
            await asyncio.gather(surprise_raid_task, return_exceptions=True)
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
