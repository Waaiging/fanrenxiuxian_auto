"""Announcement-driven Nangongque / 月殿血誓 participation for every worker.

Only the official /start, /state, /ws-ticket, /input and /claim protocol is
used. Authentication stays on the Telegram loop; room control runs on its own
loop. Private checkpoints preserve the original room and monotonically
increasing input sequence across restarts and full/restricted worker switches.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit
import uuid

from telethon import events

from automation_command_controls import CommandControlPaused, NANGONGQUE_BOSS, require_enabled
from automation_settings import nangongque_boss_identities_for_account
from log_utils import is_game_bot_sender, resolve_actor_target_chats
from miniapp_beast import MiniAppBeastError, MiniAppCircuitOpenError, _post_json, _run_blocking, request_webview_init_data
from nangongque_strategy import NangongqueStrategy, RoomInput, TERMINAL_ROOMS, number, self_player
from world_boss_features import WorldBossEntry, _ProcessLease, _PersistentWorldBossJsonClient, _diagnostic_value, extract_world_event_entry, select_identity_choice
from world_boss_recovery import WorldBossRecoveryStore
from world_boss_runtime import run_isolated_battle
from world_boss_turnstile import default_world_boss_turnstile_broker


API = "/api/miniapp/xianxia-nangongque-boss/"
JOIN_SECONDS = 60
RECOVERY_SECONDS = 30 * 60
INPUT_INTERVAL = 0.24  # Official movement heartbeat is 220 ms.
SCAN_LIMIT = 30
HISTORY_LIMIT = 20
TERMINAL_ERRORS = frozenset({
    "nangongque_not_active", "nangongque_join_closed", "nangongque_identity_invalid",
    "nangongque_public_entry_required", "nangongque_room_missing", "invalid_token",
    "invalid_init_data", "missing_init_data", "cultivator_missing", "demo_reward_unavailable",
    "nangongque_identity_missing", "nangongque_protocol_error",
})
SETTLEMENT_FIELDS = (
    "success", "grade", "score", "rank", "cultivation", "stones", "merit", "materials",
    "rareDrops", "rareRolls", "claimed", "failureReason", "automationReview", "automationRiskScore",
)


def safe_error(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or type(exc).__name__.lower())
    return code if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code) else "request_failed"


def extract_nangongque_entry(message: Any, *, sender_username: str = "") -> WorldBossEntry | None:
    return extract_world_event_entry(
        message, sender_username=sender_username,
        title_markers=("世界通告", "月殿血誓开启", "南宫阙"),
        button_text="进入月殿战场", token_prefix="nqb_",
    )


async def drain_operation(awaitable):
    """Finish an in-flight request/checkpoint before giving up the account lease."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class NangongqueHTTP:
    def __init__(self, origin: str, account: str, *, post_json=None):
        self.origin, self.post_json = origin, post_json
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"nangongque-{account}")
        self.client = _PersistentWorldBossJsonClient(origin, referer_path="/miniapp/xianxia-nangongque-boss")

    async def request(self, operation: str, body: dict, *, timeout: int = 5) -> dict:
        if operation not in {"start", "state", "ws-ticket", "input", "claim"}:
            raise MiniAppBeastError("nangongque_protocol_error")
        result = await _post_json(
            self.origin, API + operation, body, timeout, post_json=self.post_json,
            executor=self.executor, request_sync=self.client.post_sync,
            time_critical=operation in {"start", "claim"},
        )
        if result.get("ok") is not True:
            raise MiniAppBeastError("nangongque_protocol_error")
        return result

    async def close(self):
        await drain_operation(asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True))
        self.client.close()


class NangongqueRoom:
    def __init__(self, checkpoint: dict, store: WorldBossRecoveryStore, http: NangongqueHTTP,
                 *, publish=None, sleep=asyncio.sleep, clock=time.monotonic,
                 epoch=time.time, socket_factory=None, enable_socket=True):
        self.checkpoint, self.store, self.http = checkpoint, store, http
        self.publish, self.sleep, self.clock, self.epoch = publish, sleep, clock, epoch
        self.socket_factory, self.enable_socket = socket_factory, enable_socket
        self.snapshot: dict = {}
        self.full_received = -1e9
        self.last_poll = -1e9
        self.socket = None
        self.socket_task = None
        self.closed = False
        self.sequence = int(checkpoint.get("last_seq") or 0)
        self.strategy = NangongqueStrategy()
        self.diag = checkpoint.setdefault("diagnostics", {
            "version": 1, "inputs_sent": 0, "input_responses": 0, "input_errors": 0,
            "requested_actions": {}, "phases": [], "ws_connections": 0, "ws_errors": 0,
        })
        self.last_publish = -1e9
        self.last_phase = 0

    def body(self) -> dict:
        return {key: self.checkpoint[key] for key in ("sessionToken", "roomId", "playerId")}

    async def save(self):
        self.checkpoint["diagnostics"] = self.diag
        snapshot = json.loads(json.dumps(self.checkpoint))
        await drain_operation(_run_blocking(self.store.save, snapshot))

    def accept(self, data: dict) -> bool:
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise MiniAppBeastError("nangongque_protocol_error")
        for field in ("roomId", "playerId"):
            if data.get(field) is not None and str(data[field]) != str(self.checkpoint[field]):
                raise MiniAppBeastError("nangongque_protocol_error")
        seq, old_seq = int(number(data.get("stateSeq"))), int(number(self.snapshot.get("stateSeq")))
        server_time = number(data.get("serverTimeMs"))
        old_time = number(self.snapshot.get("serverTimeMs"))
        if seq and (seq < old_seq or (seq == old_seq and server_time <= old_time)):
            return False
        self.sequence = max(self.sequence, int(number(data.get("inputAckSeq"))))
        if isinstance(data.get("sessionToken"), str) and data["sessionToken"]:
            self.checkpoint["sessionToken"] = data["sessionToken"]
        if data.get("compact"):
            own = self_player(self.snapshot, self.checkpoint["playerId"])
            if own is not None and isinstance(data.get("self"), dict):
                if data["self"].get("id") is not None and str(data["self"]["id"]) != str(own.get("id")):
                    raise MiniAppBeastError("nangongque_protocol_error")
                # Compact receipts contain only our player; keep the last full
                # hazard snapshot and its age instead of pretending it is new.
                own.update(data["self"])
            for field in ("stateSeq", "serverTimeMs", "inputAckSeq"):
                if field in data:
                    self.snapshot[field] = data[field]
        else:
            if not isinstance(data.get("room"), dict) or not isinstance(data.get("players"), list):
                raise MiniAppBeastError("nangongque_protocol_error")
            self.snapshot = data
            self.full_received = self.clock()
        if isinstance(data.get("settlement"), dict):
            self.checkpoint["settlement"] = {key: _diagnostic_value(data["settlement"][key])
                                              for key in SETTLEMENT_FIELDS if key in data["settlement"]}
        return True

    async def poll(self):
        self.last_poll = self.clock()
        self.accept(await self.http.request("state", self.body(), timeout=3))

    async def _socket_loop(self):
        failures = 0
        while not self.closed:
            connection = None
            try:
                ticket = await self.http.request("ws-ticket", self.body())
                value = str(ticket.get("ticket") or "")
                if not value or len(value) > 4096:
                    raise MiniAppBeastError("nangongque_protocol_error")
                parsed = urlsplit(self.http.origin)
                url = ("wss" if parsed.scheme == "https" else "ws") + "://" + parsed.netloc
                url += "/ws/miniapp/xianxia-nangongque-boss/state?ticket=" + quote(value, safe="")
                factory = self.socket_factory
                if factory is None:
                    from websocket import create_connection
                    factory = create_connection
                async def connect():
                    nonlocal connection
                    connection = await _run_blocking(lambda: factory(url, timeout=3, origin=self.http.origin))
                await drain_operation(connect())
                connection.settimeout(1)
                self.socket = connection
                self.diag["ws_connections"] += 1
                failures = 0
                received = self.clock()
                while not self.closed:
                    try:
                        raw = await _run_blocking(connection.recv)
                    except Exception as exc:
                        if type(exc).__name__ in {"WebSocketTimeoutException", "TimeoutError"} and self.clock() - received < 5:
                            continue
                        raise
                    if not raw:
                        break
                    if len(raw) > 1_000_000:
                        raise MiniAppBeastError("nangongque_protocol_error")
                    self.accept(json.loads(raw))
                    received = self.clock()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.diag["ws_errors"] += 1
            finally:
                self.socket = None
                if connection is not None:
                    await drain_operation(_run_blocking(connection.close))
            failures += 1
            await self.sleep(min(8.0, 0.9 * 1.8 ** min(4, failures)))

    async def send_input(self, intent: RoomInput):
        self.sequence += 1
        # Save BEFORE sending, including when the previous response was lost.
        # Resuming reads /state and uses a fresh sequence; it never replays input.
        self.checkpoint["last_seq"] = self.sequence
        await self.save()
        now = self.clock()
        if intent.reason != "stale_state_stop":
            intent = self.strategy.decide(self.snapshot, self.checkpoint["playerId"], now,
                                          snapshot_age=now - self.full_received)
            if not intent.send:
                if intent.reason == "stale_state":
                    intent = RoomInput(reason="stale_state_stop")
                else:
                    return
        self.strategy.sent(intent, now)
        self.diag["inputs_sent"] += 1
        if intent.action:
            actions = self.diag["requested_actions"]
            actions[intent.action] = actions.get(intent.action, 0) + 1
        body = self.body()
        body["input"] = {
            "seq": self.sequence, "time": int(self.epoch() * 1000),
            "moveX": round(intent.move_x, 5), "moveY": round(intent.move_y, 5),
            "action": intent.action,
            "compact": self.socket is not None and self.clock() - self.full_received < 1,
        }
        try:
            data = await self.http.request("input", body)
            self.diag["input_responses"] += 1
            self.accept(data)
        except MiniAppBeastError:
            self.diag["input_errors"] += 1
            raise

    async def _publish(self, *, force=False):
        phase = int(number((self.snapshot.get("boss") or {}).get("phase")))
        if phase != self.last_phase and phase:
            self.diag["phases"].append({"phase": phase, "at": int(self.epoch())})
            self.diag["phases"] = self.diag["phases"][-12:]
            self.last_phase, force = phase, True
        if not force and self.clock() - self.last_publish < 10:
            return
        own = self_player(self.snapshot, self.checkpoint["playerId"]) or {}
        self.diag["last_room"] = {key: _diagnostic_value((self.snapshot.get("room") or {}).get(key))
                                  for key in ("status", "humanCount", "roomLimit")}
        self.diag["last_player"] = {key: number(own.get(key)) for key in ("hp", "maxHp", "damage", "mechanic", "guard")}
        self.diag["last_phase"] = phase
        self.diag["input_ack_seq"] = int(number(self.snapshot.get("inputAckSeq")))
        await self.save()
        if self.publish:
            await self.publish(_diagnostic_value(self.diag))
        self.last_publish = self.clock()

    async def claim(self) -> dict:
        self.checkpoint["stage"] = "finish_pending"
        await self.save()
        settlement = self.checkpoint.get("settlement") or {}
        if not settlement.get("claimed"):
            data = await self.http.request("claim", {"sessionToken": self.checkpoint["sessionToken"]}, timeout=8)
            returned = data.get("settlement")
            if isinstance(returned, dict):
                settlement.update({key: _diagnostic_value(returned[key]) for key in SETTLEMENT_FIELDS if key in returned})
            # Like the official claim button, an ok:true claim response is the
            # acknowledgement, even when it omits the already-shown settlement.
            settlement["claimed"] = True
        self.checkpoint["settlement"] = settlement
        result = {"status": "completed", "identity": self.checkpoint["identity"],
                  "room_id": self.checkpoint["roomId"], "settlement": settlement,
                  "diagnostics": _diagnostic_value(self.diag)}
        self.checkpoint.update(stage="terminal", result=result, sessionToken="", init_data="")
        await self.save()
        return result

    async def run(self) -> dict:
        errors = 0
        stopped_for_stale = False
        try:
            # Always reconcile the original session first, including after a
            # claim timeout or a worker switch. No second /start during a room.
            try:
                await self.poll()
            except MiniAppBeastError as exc:
                if safe_error(exc) == "nangongque_not_active" and self.checkpoint.get("settlement"):
                    return await drain_operation(self.claim())
                raise
            if self.enable_socket and (self.snapshot.get("room") or {}).get("status") not in TERMINAL_ROOMS:
                self.socket_task = asyncio.create_task(self._socket_loop())
            while self.epoch() < self.checkpoint["expires_epoch"]:
                status = str((self.snapshot.get("room") or {}).get("status") or "")
                settlement = self.checkpoint.get("settlement")
                if status in TERMINAL_ROOMS or self.checkpoint["stage"] == "finish_pending":
                    if settlement:
                        await self._publish(force=True)
                        return await drain_operation(self.claim())
                    await self.sleep(1)
                    await self.poll()
                    continue
                started = self.clock()
                try:
                    if self.clock() - self.full_received > 0.8 and self.clock() - self.last_poll >= 1:
                        await self.poll()
                    intent = self.strategy.decide(self.snapshot, self.checkpoint["playerId"], self.clock(),
                                                  snapshot_age=self.clock() - self.full_received)
                    if intent.send:
                        await drain_operation(self.send_input(intent))
                        stopped_for_stale = False
                    elif intent.reason == "stale_state" and not stopped_for_stale:
                        await drain_operation(self.send_input(RoomInput(reason="stale_state_stop")))
                        stopped_for_stale = True
                    elif self.clock() - self.last_poll >= 1:
                        await self.poll()
                    errors = 0
                    await self._publish()
                except MiniAppBeastError as exc:
                    if safe_error(exc) in TERMINAL_ERRORS:
                        raise
                    errors += 1
                    self.diag["last_error"] = safe_error(exc)
                    if errors >= 8:
                        raise
                    # An uncertain input is not retried. The next operation is
                    # a read of the original room and then a new decision.
                    await self.sleep(min(8, 0.5 * 2 ** min(4, errors)))
                    await self.poll()
                interval = 0.6 if status == "joining" else INPUT_INTERVAL
                await self.sleep(max(0.01, interval - (self.clock() - started)))
            raise MiniAppBeastError("nangongque_recovery_expired")
        finally:
            self.closed = True
            if self.socket_task is not None:
                self.socket_task.cancel()
                await asyncio.gather(self.socket_task, return_exceptions=True)
            await self.save()


class NangongqueBossMonitor:
    def __init__(self, actor: Any, account: str, *, logger=None, transport=None, post_json=None,
                 broker=None, store=None, sleep=asyncio.sleep, epoch=time.time, room_factory=NangongqueRoom):
        self.actor, self.client, self.account = actor, actor.client, account
        self.log = logger or logging.getLogger(f"nangongque.{account}")
        self.transport, self.post_json = transport, post_json
        self.broker = broker or default_world_boss_turnstile_broker()
        self.sleep, self.epoch, self.room_factory = sleep, epoch, room_factory
        self.base = Path(getattr(actor, "state_file", "") or __file__).resolve().parent
        self.store = store or WorldBossRecoveryStore(self.base / ".nangongque_boss_recovery", account,
            clock=epoch, stages={"joining", "fighting", "finish_pending", "terminal"})
        self.enabled = bool(((getattr(actor, "config", {}) or {}).get("nangongque_boss") or {}).get("enabled", True))
        self.tasks: set[asyncio.Task] = set()
        self.inflight: set[str] = set()
        self.handlers: list = []
        self.fight_lock = asyncio.Lock()
        self.cleanup_task = None
        self.target_chats = []
        self.verification_requests: set[str] = set()

    def _save(self):
        saver = getattr(self.actor, "save_state", None)
        if callable(saver):
            saver()

    def _record(self, entry: WorldBossEntry, status: str, **updates):
        state = self.actor.state
        history = state.setdefault("nangongque_boss_events", [])
        record = next((row for row in history if row.get("fingerprint") == entry.fingerprint), None)
        if record is None:
            record = {"fingerprint": entry.fingerprint, "message_id": entry.message_id,
                      "chat_id": entry.chat_id, "notice_epoch": entry.notice_epoch}
            history.append(record)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record.update(status=status, updated_at=stamp, **updates)
        del history[:-HISTORY_LIMIT]
        state.update(nangongque_boss_last_status=status, nangongque_boss_last_updated_at=stamp,
                     nangongque_boss_last_error=str(updates.get("error") or ""))
        self._save()

    def _require_join(self, entry: WorldBossEntry, identity: str):
        if not self.enabled or identity not in nangongque_boss_identities_for_account(self.account):
            raise CommandControlPaused(NANGONGQUE_BOSS, identity)
        require_enabled(self.actor, identity, NANGONGQUE_BOSS)
        if self.epoch() >= entry.notice_epoch + JOIN_SECONDS:
            raise MiniAppBeastError("nangongque_join_closed")

    def _cached_player_id(self, identity):
        owners = [self.transport] + [getattr(getattr(self.actor, name, None), "transport", None)
                                    for name in ("_miniapp_command_router", "_miniapp_beast_contract", "_restricted_miniapp_worker")]
        for transport in owners:
            if transport is not None:
                try:
                    return int(transport.player_id(identity))
                except Exception:
                    pass
        return None

    async def _verification(self, entry, identity):
        self._require_join(entry, identity)
        remaining = max(1, int(entry.notice_epoch + JOIN_SECONDS - self.epoch()))
        request = self.broker.create_request(
            event_fingerprint=entry.fingerprint, message_id=entry.message_id,
            account=self.account, identity=identity, challenge_id="", origin=entry.origin,
            activity="nangongque", ttl_seconds=remaining,
        )
        request_id = request["request_id"]
        self.verification_requests.add(request_id)
        self.log.info("[%s/%s] 南宫阙浏览器验证排队，剩余集结时间 %s 秒", self.account, identity, remaining)
        try:
            while self.epoch() < entry.notice_epoch + JOIN_SECONDS:
                self._require_join(entry, identity)
                token = self.broker.take_token(request_id)
                if token:
                    return token, request_id
                await self.sleep(min(0.25, max(0.01, entry.notice_epoch + JOIN_SECONDS - self.epoch())))
            raise MiniAppBeastError("nangongque_turnstile_timeout")
        except BaseException:
            self.broker.cancel(request_id, reason="nangongque_join_stopped")
            raise

    async def _join(self, entry, identity, checkpoint, http):
        try:
            return await self._join_flow(entry, identity, checkpoint, http)
        finally:
            # Covers pauses/cancellation after take_token but before /start.
            # Accepted/rejected receipts are preserved by broker.cancel().
            for request_id in self.verification_requests:
                self.broker.cancel(request_id, reason="nangongque_join_stopped")
            self.verification_requests.clear()

    async def _join_flow(self, entry, identity, checkpoint, http):
        self._require_join(entry, identity)
        init_data = await request_webview_init_data(self.client, entry.bot_username, entry.token)
        self._require_join(entry, identity)
        player_id = checkpoint.get("selected_player_id") or self._cached_player_id(identity)
        handoffs = 0
        token, request_id = "", ""
        for attempt in range(6):
            self._require_join(entry, identity)
            payload = {"token": entry.token, "initData": init_data,
                       "playerId": str(player_id) if player_id is not None else ""}
            if token:
                payload.update(turnstileToken=token, turnstileIdempotencyKey=str(uuid.uuid4()))

            async def start_and_checkpoint():
                self._require_join(entry, identity)
                data = await http.request("start", payload, timeout=12)
                if not data.get("needsIdentitySelection"):
                    if not all(data.get(key) for key in ("sessionToken", "roomId", "playerId")) or player_id is None:
                        raise MiniAppBeastError("nangongque_protocol_error")
                    checkpoint.update(stage="fighting", selected_player_id=player_id,
                                      **{key: data[key] for key in ("sessionToken", "roomId", "playerId")})
                    await drain_operation(_run_blocking(self.store.save, checkpoint))
                    if request_id:
                        self.broker.record_result(request_id, accepted=True, http_status=200)
                return data

            try:
                data = await drain_operation(start_and_checkpoint())
            except MiniAppBeastError as exc:
                code = safe_error(exc)
                if request_id:
                    self.broker.record_result(request_id, accepted=False, error=code, http_status=exc.status)
                token, request_id = "", ""
                if code in {"turnstile_required", "turnstile_failed"} and handoffs < 2:
                    handoffs += 1
                    token, request_id = await self._verification(entry, identity)
                    continue
                if code in TERMINAL_ERRORS or code.startswith("turnstile_"):
                    raise
                # /start is the official reconnect operation. Browser tokens
                # are one-shot; retry without a consumed token and re-verify.
                await self.sleep(min(2.5, 0.5 * (attempt + 1)))
                continue
            if request_id:
                self.broker.record_result(request_id, accepted=not data.get("needsIdentitySelection"),
                                          error="identity_selection_required" if data.get("needsIdentitySelection") else "",
                                          http_status=200)
            token, request_id = "", ""
            if data.get("needsIdentitySelection"):
                selected = select_identity_choice(self.actor, data.get("identityChoices"), identity)
                if selected is None:
                    raise MiniAppBeastError("nangongque_identity_missing")
                player_id = selected
                checkpoint["selected_player_id"] = selected
                self._require_join(entry, identity)
                if (data.get("turnstile") or {}).get("enabled"):
                    handoffs += 1
                    if handoffs > 2:
                        raise MiniAppBeastError("nangongque_turnstile_timeout")
                    token, request_id = await self._verification(entry, identity)
                continue
            return
        raise MiniAppBeastError("nangongque_start_retry_exhausted")

    def _queue(self, entry: WorldBossEntry, *, checkpoint=None) -> bool:
        if entry.fingerprint in self.inflight:
            return False
        selected = nangongque_boss_identities_for_account(self.account)
        if checkpoint and checkpoint.get("stage") == "terminal":
            return False
        if not checkpoint and (not selected or self.epoch() >= entry.notice_epoch + JOIN_SECONDS):
            return False
        if not checkpoint and any(row.get("fingerprint") == entry.fingerprint and row.get("terminal")
                                  for row in self.actor.state.get("nangongque_boss_events", [])):
            return False
        self.inflight.add(entry.fingerprint)
        identity = checkpoint["identity"] if checkpoint else selected[0]
        self._record(entry, "queued", identity=identity)
        task = asyncio.create_task(self._run_entry(entry, identity), name=f"nangongque_{self.account}_{entry.message_id}")
        self.tasks.add(task)

        def done(finished):
            self.tasks.discard(finished)
            self.inflight.discard(entry.fingerprint)
            if not finished.cancelled() and finished.exception():
                self.log.error("南宫阙任务异常结束：%s", safe_error(finished.exception()))
        task.add_done_callback(done)
        return True

    async def process_message(self, message: Any, *, source="new") -> bool:
        if not self.enabled:
            return False
        entry = extract_nangongque_entry(message)
        if entry is None:
            return False
        # Edits retain the original notice time; old buttons cannot open a new
        # registration window. Recovery uses private sessions independently.
        if not -5 <= self.epoch() - entry.notice_epoch < JOIN_SECONDS:
            return False
        try:
            sender = await message.get_sender()
        except Exception:
            return False
        if not sender or not is_game_bot_sender(self.actor, sender):
            return False
        entry = extract_nangongque_entry(message, sender_username=getattr(sender, "username", ""))
        if entry is None:
            return False
        if self.target_chats:
            def chat_key(value):
                value = str(value)
                return value[4:] if value.startswith("-100") else value.lstrip("-")
            if chat_key(entry.chat_id) not in {chat_key(chat) for chat in self.target_chats}:
                return False
        return self._queue(entry)

    async def _run_entry(self, entry, identity):
        async with self.fight_lock:
            lease = _ProcessLease(self.base / f".nangongque_boss_{self.account}.lock")
            acquired = False
            while self.epoch() < entry.notice_epoch + RECOVERY_SECONDS:
                acquired = lease.acquire()
                if acquired:
                    break
                await self.sleep(2)
            if not acquired:
                return
            http = NangongqueHTTP(entry.origin, self.account, post_json=self.post_json)
            checkpoint = None
            try:
                checkpoint = await _run_blocking(self.store.load, entry.fingerprint)
                if checkpoint and checkpoint["stage"] == "terminal":
                    result = checkpoint.get("result") or {}
                    self._record(entry, result.get("status", "completed"), terminal=True, identity_results=[result])
                    return
                if checkpoint is None:
                    checkpoint = {"version": 1, "account": self.account, "entry": asdict(entry),
                                  "identity": identity, "expires_epoch": entry.notice_epoch + RECOVERY_SECONDS,
                                  "stage": "joining", "last_seq": 0}
                    await drain_operation(_run_blocking(self.store.save, checkpoint))
                identity = checkpoint["identity"]
                self.log.info("OUT [Mini App | %s]:\n南宫阙·月殿血誓 自动参战", identity)
                loop = asyncio.get_running_loop()

                async def report(diag):
                    async def update():
                        self._record(entry, "running", identity=identity, diagnostics=diag)
                    await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(update(), loop))

                failures = 0
                while self.epoch() < checkpoint["expires_epoch"]:
                    try:
                        if checkpoint["stage"] == "joining":
                            self._require_join(entry, identity)
                            self.broker.request_warmup(event_fingerprint=entry.fingerprint, origin=entry.origin,
                                                       notice_epoch=entry.notice_epoch, activity="nangongque")
                            await self._join(entry, identity, checkpoint, http)
                        self._record(entry, "running", identity=identity, room_id=checkpoint.get("roomId"))
                        room = self.room_factory(checkpoint, self.store, http, publish=report)
                        result = await run_isolated_battle(room.run, account=f"nangongque-{self.account}")
                        self._record(entry, result["status"], terminal=True, identity_results=[result])
                        settled = result.get("settlement") or {}
                        self.log.info("IN [Mini App | %s]:\n南宫阙·月殿血誓 -> %s；%s等，贡献 %s，奖励%s%s",
                                      identity, "伏杀成功" if settled.get("success") else "房间未获胜",
                                      settled.get("grade", "--"), settled.get("score", 0),
                                      "已领取" if settled.get("claimed") else "待领取",
                                      "；服务端标记行为复核" if settled.get("automationReview") else "")
                        return
                    except CommandControlPaused:
                        self._record(entry, "paused", identity=identity)
                        if self.epoch() >= entry.notice_epoch + JOIN_SECONDS:
                            return
                        await self.sleep(1)
                    except MiniAppBeastError as exc:
                        code = safe_error(exc)
                        if code in TERMINAL_ERRORS:
                            raise
                        failures += 1
                        self._record(entry, "recovery_pending", identity=identity, error=code)
                        self.log.warning("[%s/%s] 南宫阙等待原房间恢复：%s", self.account, identity, code)
                        delay = max(min(30, 2 ** min(5, failures)), float(getattr(exc, "retry_after", 0) or 0))
                        await self.sleep(min(delay, max(0, checkpoint["expires_epoch"] - self.epoch())))
                raise MiniAppBeastError("nangongque_recovery_expired")
            except asyncio.CancelledError:
                if checkpoint and checkpoint.get("stage") == "terminal" and checkpoint.get("result"):
                    result = checkpoint["result"]
                    self._record(entry, result["status"], terminal=True, identity_results=[result])
                else:
                    self._record(entry, "interrupted", identity=identity)
                raise
            except CommandControlPaused:
                self._record(entry, "paused", identity=identity)
            except Exception as exc:
                code = safe_error(exc)
                terminal = code in TERMINAL_ERRORS or code == "nangongque_recovery_expired"
                result = {"status": "event_closed" if code in {"nangongque_not_active", "nangongque_join_closed"} else "failed",
                          "identity": identity, "error": code}
                if checkpoint and terminal:
                    checkpoint.update(stage="terminal", result=result, sessionToken="", init_data="")
                    await drain_operation(_run_blocking(self.store.save, checkpoint))
                self._record(entry, result["status"], terminal=terminal, identity_results=[result], error=code)
                self.log.warning("IN [Mini App | %s]:\n南宫阙·月殿血誓 -> %s", identity, code)
            finally:
                try:
                    await http.close()
                finally:
                    lease.release()

    async def _cleanup(self):
        while True:
            await self.sleep(30)
            await _run_blocking(self.store.list_pending)  # Also removes expired private files.

    async def install(self) -> bool:
        if not self.enabled:
            self.actor.state["nangongque_boss_monitor_active"] = False
            self._save()
            return False
        try:
            self.target_chats = await resolve_actor_target_chats(self.actor, self.log)
            for kind, builder in (("new", events.NewMessage), ("edited", events.MessageEdited)):
                async def handler(event, source=kind):
                    await self.process_message(event.message, source=source)
                self.client.add_event_handler(handler, builder(chats=self.target_chats))
                self.handlers.append(handler)
            self.actor.state.update(nangongque_boss_monitor_active=True,
                                    nangongque_boss_monitor_started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self._save()
            self.log.info("[%s] Nangongque 月殿血誓 monitor ready for chats %s", self.account, self.target_chats)
            for checkpoint in await _run_blocking(self.store.list_pending):
                if checkpoint["stage"] != "terminal":
                    self._queue(WorldBossEntry(**checkpoint["entry"]), checkpoint=checkpoint)
            for chat in self.target_chats:
                try:
                    for message in await self.client.get_messages(chat, limit=SCAN_LIMIT):
                        await self.process_message(message, source="startup")
                except Exception as exc:
                    self.log.warning("南宫阙启动补查失败：%s", safe_error(exc))
            self.cleanup_task = asyncio.create_task(self._cleanup())
            return True
        except Exception as exc:
            await self.stop()
            self.actor.state["nangongque_boss_last_error"] = safe_error(exc)
            self._save()
            self.log.error("南宫阙监听初始化失败：%s", safe_error(exc))
            return False

    async def stop(self):
        for handler in self.handlers:
            self.client.remove_event_handler(handler)
        self.handlers.clear()
        tasks = list(self.tasks) + ([self.cleanup_task] if self.cleanup_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.actor.state["nangongque_boss_monitor_active"] = False
        self._save()


async def install_nangongque_boss_monitor(actor, account, *, logger=None, transport=None):
    monitor = NangongqueBossMonitor(actor, account, logger=logger, transport=transport)
    await monitor.install()
    actor._nangongque_boss_monitor = monitor
    return monitor
