#!/usr/bin/env python3
"""Automated Lingxi fishing through the authenticated Telegram Mini App."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from typing import Any

from automation_settings import miniapp_fishing_settings
from miniapp_beast import MiniAppBeastError
from miniapp_dwelling import identity_state


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_RETRY_SECONDS = 60
DEFAULT_DISABLED_SECONDS = 30
DEFAULT_RESULT_ATTEMPTS = 18
IDENTITY = "主魂"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _now_text() -> str:
    return datetime.now().strftime(TIME_FORMAT)


def fishing_shop(payload: Any) -> dict[str, Any]:
    return _mapping(_mapping(payload).get("shop"))


def fishing_option(items: Any, key: Any) -> dict[str, Any]:
    wanted = str(key or "").strip()
    for item in _items(items):
        if str(item.get("key") or "").strip() == wanted:
            return item
    return {}


def fishing_bait_by_name(shop: Any, name: Any) -> dict[str, Any]:
    wanted = str(name or "").strip()
    for item in _items(_mapping(shop).get("baits")):
        if str(item.get("name") or "").strip() == wanted:
            return item
    return {}


def _javascript_char_code_sum(value: Any) -> int:
    encoded = str(value or "seed").encode("utf-16-le", errors="surrogatepass")
    return sum(int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2))


def _js_round(value: float) -> int:
    return math.floor(float(value) + 0.5)


def build_fishing_proof(challenge: Any) -> dict[str, Any]:
    """Replay the page's 20 ms control model and keep tension inside its safe band."""

    data = _mapping(challenge)
    challenge_id = str(data.get("challengeId") or "").strip()
    if not challenge_id:
        raise MiniAppBeastError("fishing_challenge_missing")
    target_low = _number(data.get("targetLow"), 41)
    target_high = _number(data.get("targetHigh"), 68)
    if target_high <= target_low + 2:
        raise MiniAppBeastError("fishing_challenge_band_invalid")
    fish_power = max(0.1, _number(data.get("fishPower"), 1.7))
    min_duration_ms = max(0, _integer(data.get("minDurationMs"), 5200))
    max_duration_ms = max(min_duration_ms + 1000, _integer(data.get("maxDurationMs"), 70000))
    seed_offset = _javascript_char_code_sum(data.get("fishSeed") or "seed") / 19

    tension = (target_low + target_high) / 2 - 8
    progress = 0.0
    elapsed_ms = 0
    holding = False
    danger_ms = 0.0
    slack_ms = 0.0
    samples = 0
    stable_samples = 0
    actions = 0
    events: list[dict[str, Any]] = []
    width = target_high - target_low
    hold_below = target_low + width * 0.54
    release_above = target_low + width * 0.72

    while elapsed_ms < max_duration_ms and (progress < 100 or elapsed_ms < min_duration_ms):
        if tension < target_low:
            desired_holding = True
        elif tension > target_high:
            desired_holding = False
        elif holding:
            desired_holding = tension < release_above
        else:
            desired_holding = tension <= hold_below
        if desired_holding != holding:
            holding = desired_holding
            actions += 1
            events.append({"t": elapsed_ms + 20, "holding": holding})

        dt = 0.02
        elapsed_ms += 20
        pulse = math.sin(elapsed_ms * 0.0027 * fish_power + seed_offset)
        surge = max(0.0, math.sin(elapsed_ms * 0.0041 + seed_offset * 1.7))
        fish_pull = fish_power * (0.72 + pulse * 0.24 + surge * 0.42)
        if holding:
            tension += (24 + fish_pull * 3.1) * dt
        else:
            tension += (fish_pull * 4.8 - 24) * dt
        tension += math.sin(elapsed_ms * 0.012 + seed_offset) * 0.24
        tension = max(0.0, min(100.0, tension))

        in_band = target_low <= tension <= target_high
        if in_band:
            stable_samples += 1
            progress += (8.2 + fish_power * 0.7 + (2.2 if holding else 0.5)) * dt
        elif tension > target_high:
            danger_ms += dt * 1000
            progress -= (1.5 + fish_power * 0.25) * dt
        else:
            slack_ms += dt * 1000
            progress -= 0.9 * dt
        if holding and tension < target_low:
            progress += 1.1 * dt
        progress = max(0.0, min(100.0, progress))
        samples += 1

    if progress < 100:
        raise MiniAppBeastError("fishing_proof_not_landed")
    stability = stable_samples / samples if samples else 0.0
    penalty = danger_ms / 430 + slack_ms / 520 + max(0.0, 100 - progress) * 0.04
    score = max(55, min(100, _js_round(72 + stability * 28 - penalty)))
    return {
        "proof": {
            "mode": "xianxiaFishingV2",
            "challengeId": challenge_id,
            "durationMs": elapsed_ms,
            "events": events,
        },
        "predicted_score": score,
        "stability": stability,
        "danger_ms": danger_ms,
        "slack_ms": slack_ms,
        "actions": actions,
        "progress": progress,
    }


def fishing_result_summary(finish_payload: Any, catch_payload: Any) -> str:
    score_result = _mapping(_mapping(finish_payload).get("result"))
    details = _mapping(score_result.get("details"))
    catch_result = _mapping(_mapping(catch_payload).get("result"))
    grade = str(score_result.get("grade") or "-")
    score = _integer(score_result.get("score"), 0)
    stability = round(_number(details.get("stability"), 0) * 100)
    quality = round(_number(score_result.get("quality_bonus"), 0) * 100)
    parts = [f"评分 {grade} {score}分", f"稳定 {stability}%", f"品质 +{quality}%"]
    if not catch_result.get("ready"):
        parts.append("鱼获结算中")
        return "，".join(parts)
    if catch_result.get("caught") and isinstance(catch_result.get("fish"), dict):
        fish = catch_result["fish"]
        parts.append(
            f"提竿成功：【{str(fish.get('name') or '灵鱼')}】"
            f" {str(catch_result.get('rarityLabel') or catch_result.get('rarity') or '')}"
            f" {float(fish.get('weight') or 0):.2f}斤"
        )
    else:
        parts.append(f"空竿：{str(catch_result.get('message') or '水下灵影擦钩而过')}" )
    exp_gain = _integer(catch_result.get("expGain"), 0)
    if exp_gain:
        parts.append(f"钓术经验 +{exp_gain}")
    loot = []
    for item in _items(catch_result.get("bonusLoot")):
        name = str(item.get("name") or "物品")
        loot.append(f"{name}x{max(1, _integer(item.get('qty'), 1))}")
    if loot:
        parts.append("伴生机缘 " + "、".join(loot))
    return "，".join(part for part in parts if part)


class MiniAppFishingAutomation:
    """Run server-verified fishing for the main account's main soul."""

    def __init__(self, actor: Any, transport: Any, account: str, logger: Any) -> None:
        self.actor = actor
        self.transport = transport
        self.account = str(account or "").strip()
        self.log = logger
        config = getattr(actor, "config", {}) or {}
        settings = config.get("miniapp_beast") or {}
        self.retry_seconds = max(
            15,
            _integer(settings.get("fishing_retry_seconds"), DEFAULT_RETRY_SECONDS),
        )
        self.purchase_quantity = max(
            1,
            min(99, _integer(settings.get("fishing_bait_purchase_quantity"), 1)),
        )

    @property
    def supported(self) -> bool:
        return self.account == "main"

    def settings(self) -> dict[str, Any]:
        return miniapp_fishing_settings()

    def _state(self) -> dict[str, Any]:
        return identity_state(self.actor, IDENTITY)

    def _save(self) -> None:
        try:
            self.actor.save_state()
        except Exception:
            self.log.warning("Mini App fishing state save failed", exc_info=True)

    def _record(self, **updates: Any) -> None:
        state = self._state()
        state.update(updates)
        state["miniapp_fishing_updated_at"] = _now_text()
        self._save()

    def _record_shop(self, shop: dict[str, Any]) -> None:
        self._record(
            miniapp_fishing_pond_options=[
                {
                    "key": str(item.get("key") or ""),
                    "name": str(item.get("name") or ""),
                    "unlocked": bool(item.get("unlocked")),
                    "required_exp": _integer(item.get("requiredExp"), 0),
                    "current_exp": _integer(item.get("currentExp"), 0),
                }
                for item in _items(shop.get("ponds"))
            ],
            miniapp_fishing_bait_options=[
                {
                    "key": str(item.get("key") or ""),
                    "item_id": str(item.get("itemId") or ""),
                    "name": str(item.get("name") or ""),
                    "count": _integer(item.get("count"), 0),
                    "unlocked": bool(item.get("unlocked")),
                }
                for item in _items(shop.get("baits"))
            ],
            miniapp_fishing_chum_options=[
                {
                    "key": str(item.get("key") or ""),
                    "name": str(item.get("name") or ""),
                    "uses": _integer(item.get("uses"), 0),
                    "remaining_today": _integer(item.get("remainingToday"), 0),
                    "affordable": bool(item.get("affordable")),
                }
                for item in _items(shop.get("chums"))
            ],
            miniapp_fishing_active_chum=_mapping(shop.get("activeChum")),
            miniapp_fishing_last_shop_sync_time=_now_text(),
        )

    async def _ensure_bait(
        self,
        token: str,
        shop: dict[str, Any],
        bait_key: str,
        minimum: int = 1,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        bait = fishing_option(shop.get("baits"), bait_key)
        if not bait:
            raise MiniAppBeastError("fishing_bait_invalid")
        if not bait.get("unlocked"):
            raise MiniAppBeastError("fishing_bait_level_low")
        missing = max(0, int(minimum) - _integer(bait.get("count"), 0))
        if missing <= 0:
            return shop, bait
        quantity = max(missing, self.purchase_quantity)
        bought = await self.transport.fishing_buy_bait(
            IDENTITY,
            token,
            str(bait.get("key") or bait_key),
            quantity,
            log_operation=False,
        )
        updated_shop = fishing_shop(bought) or shop
        self._record_shop(updated_shop)
        updated_bait = fishing_option(updated_shop.get("baits"), bait_key)
        if _integer(updated_bait.get("count"), 0) < minimum:
            raise MiniAppBeastError("fishing_bait_missing")
        bait_name = str(updated_bait.get("name") or bait.get("name") or bait_key)
        pending_purchases = [
            dict(item)
            for item in _items(self._state().get("miniapp_fishing_pending_purchases"))
        ]
        existing = next(
            (item for item in pending_purchases if str(item.get("name") or "") == bait_name),
            None,
        )
        if existing is None:
            pending_purchases.append({"name": bait_name, "quantity": quantity})
        else:
            existing["quantity"] = _integer(existing.get("quantity"), 0) + quantity
        self._record(
            miniapp_fishing_last_purchase_time=_now_text(),
            miniapp_fishing_last_purchase=f"{bait_name} x{quantity}",
            miniapp_fishing_pending_purchases=pending_purchases,
        )
        return updated_shop, updated_bait

    async def _ensure_chum(
        self,
        token: str,
        shop: dict[str, Any],
        chum_key: str,
    ) -> dict[str, Any]:
        if not chum_key or chum_key == "none":
            return shop
        active = _mapping(shop.get("activeChum"))
        if active:
            return shop
        chum = fishing_option(shop.get("chums"), chum_key)
        if not chum:
            raise MiniAppBeastError("fishing_chum_invalid")
        if _integer(chum.get("remainingToday"), 0) <= 0:
            raise MiniAppBeastError("fishing_chum_daily_limit")
        for cost in _items(chum.get("cost")):
            required = _integer(cost.get("qty"), 0)
            owned = _integer(cost.get("owned"), 0)
            if owned >= required:
                continue
            bait = fishing_bait_by_name(shop, cost.get("name"))
            if not bait:
                continue
            shop, _ = await self._ensure_bait(
                token,
                shop,
                str(bait.get("key") or ""),
                required,
            )
        refreshed = fishing_shop(await self.transport.fishing_shop(IDENTITY, token)) or shop
        self._record_shop(refreshed)
        chum = fishing_option(refreshed.get("chums"), chum_key)
        if not chum.get("affordable"):
            raise MiniAppBeastError("fishing_chum_unaffordable")
        applied = await self.transport.fishing_apply_chum(
            IDENTITY,
            token,
            chum_key,
            log_operation=False,
        )
        updated = fishing_shop(applied) or refreshed
        self._record_shop(updated)
        self._record(
            miniapp_fishing_last_chum_time=_now_text(),
            miniapp_fishing_last_chum=str(chum.get("name") or chum_key),
        )
        return updated

    @staticmethod
    def _wait_for_bite(session: dict[str, Any]) -> int:
        bite_at = _integer(session.get("biteAt"), 0)
        server_now = _integer(session.get("serverNow"), 0)
        if not bite_at:
            return 5
        if not server_now:
            server_now = int(datetime.now().timestamp() * 1000)
        return max(1, math.ceil(max(0, bite_at - server_now) / 1000) + 1)

    async def _start_cast(
        self,
        token: str,
        shop: dict[str, Any],
        settings: dict[str, Any],
    ) -> int:
        pond_key = str(settings.get("pond") or "qingxi")
        bait_key = str(settings.get("bait") or "demon_blood")
        chum_key = str(settings.get("chum") or "none")
        pond = fishing_option(shop.get("ponds"), pond_key)
        if not pond:
            raise MiniAppBeastError("fishing_pond_invalid")
        if not pond.get("unlocked"):
            raise MiniAppBeastError("fishing_pond_locked")
        self._record(miniapp_fishing_pending_purchases=[])
        shop = await self._ensure_chum(token, shop, chum_key)
        shop, bait = await self._ensure_bait(token, shop, bait_key)
        token, _ = await self.transport.fishing_next_cast(
            IDENTITY,
            token,
            pond_key,
            str(bait.get("itemId") or ""),
            log_operation=False,
        )
        token, start = await self.transport.fishing_start(IDENTITY, token)
        session = _mapping(start.get("session"))
        wait = self._wait_for_bite(session)
        active_chum = _mapping(shop.get("activeChum"))
        self._record(
            miniapp_fishing_status="waiting",
            miniapp_fishing_last_error="",
            miniapp_fishing_pond=str(pond.get("name") or pond_key),
            miniapp_fishing_pond_key=pond_key,
            miniapp_fishing_bait=str(bait.get("name") or bait_key),
            miniapp_fishing_bait_key=bait_key,
            miniapp_fishing_chum=str(active_chum.get("name") or "不打窝"),
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(seconds=wait)
            ).strftime(TIME_FORMAT),
            miniapp_fishing_last_cast_time=_now_text(),
        )
        return wait

    async def _finish_challenge(
        self,
        token: str,
        session: dict[str, Any],
        challenge: dict[str, Any],
    ) -> int:
        built = build_fishing_proof(challenge)
        proof = built["proof"]
        pond = str(_mapping(session.get("pond")).get("name") or "灵溪")
        bait = str(_mapping(session.get("bait")).get("name") or "鱼饵")
        self._record(
            miniapp_fishing_status="reeling",
            miniapp_fishing_predicted_score=built["predicted_score"],
            miniapp_fishing_predicted_stability=round(built["stability"] * 100, 2),
            miniapp_fishing_reel_actions=built["actions"],
            miniapp_fishing_reel_duration_ms=proof["durationMs"],
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(milliseconds=proof["durationMs"] + 300)
            ).strftime(TIME_FORMAT),
        )
        await asyncio.sleep(proof["durationMs"] / 1000 + 0.2)
        finish = await self.transport.fishing_finish(
            IDENTITY,
            token,
            proof,
            log_operation=False,
        )
        catch_payload: dict[str, Any] = {}
        for attempt in range(DEFAULT_RESULT_ATTEMPTS):
            await asyncio.sleep(0.65 if attempt < 4 else 1.0)
            try:
                catch_payload = await self.transport.fishing_result(IDENTITY, token)
            except MiniAppBeastError:
                if attempt >= 8:
                    break
                continue
            if _mapping(catch_payload.get("result")).get("ready"):
                break
        summary = fishing_result_summary(finish, catch_payload)
        score_result = _mapping(finish.get("result"))
        details = _mapping(score_result.get("details"))
        catch_result = _mapping(catch_payload.get("result"))
        fish = _mapping(catch_result.get("fish"))
        ready = bool(catch_result.get("ready"))
        caught = bool(catch_result.get("caught"))
        pending_purchases = _items(
            self._state().get("miniapp_fishing_pending_purchases")
        )
        purchase_text = "、".join(
            f"{str(item.get('name') or '鱼饵')}x{max(1, _integer(item.get('quantity'), 1))}"
            for item in pending_purchases
        )
        chum = str(self._state().get("miniapp_fishing_chum") or "不打窝")
        if ready:
            round_title = f"灵溪垂钓汇总（{pond} · {bait} · {chum}）"
            if purchase_text:
                round_title += f"｜自动购饵 {purchase_text}"
            self.log.info(
                "IN [Mini App | %s]:\n%s -> %s",
                IDENTITY,
                round_title,
                summary,
            )
        self._record(
            miniapp_fishing_status=("caught" if caught else "empty" if ready else "settling"),
            miniapp_fishing_last_error="",
            miniapp_fishing_last_result=summary[:1000],
            miniapp_fishing_last_round_time=_now_text(),
            miniapp_fishing_last_score=_integer(score_result.get("score"), 0),
            miniapp_fishing_last_grade=str(score_result.get("grade") or ""),
            miniapp_fishing_last_stability=round(_number(details.get("stability"), 0) * 100, 2),
            miniapp_fishing_last_quality_bonus=round(
                _number(score_result.get("quality_bonus"), 0) * 100,
                2,
            ),
            miniapp_fishing_last_caught=caught,
            miniapp_fishing_last_fish=str(fish.get("name") or ""),
            miniapp_fishing_last_weight=_number(fish.get("weight"), 0),
            miniapp_fishing_last_exp_gain=_integer(catch_result.get("expGain"), 0),
            miniapp_fishing_last_bonus_loot=_items(catch_result.get("bonusLoot")),
            miniapp_fishing_pending_purchases=([] if ready else pending_purchases),
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(seconds=3 if ready else 30)
            ).strftime(TIME_FORMAT),
        )
        recorder = getattr(self.actor, "record_daily_reward_event", None)
        if callable(recorder) and ready:
            try:
                recorder(
                    IDENTITY,
                    f".钓鱼 {bait}",
                    summary,
                    source="Mini App 灵溪垂钓",
                    final=True,
                )
            except Exception:
                self.log.warning("Mini App fishing reward recording failed", exc_info=True)
        return 3 if ready else 30

    async def run_cycle(self, settings: dict[str, Any] | None = None) -> int:
        settings = dict(settings or self.settings())
        token, payload = await self.transport.fishing_entry(IDENTITY)
        session = _mapping(payload.get("session"))
        challenge = _mapping(payload.get("challenge"))
        shop_payload = await self.transport.fishing_shop(IDENTITY, token)
        shop = fishing_shop(shop_payload)
        self._record_shop(shop)
        rod = _mapping(session.get("rod"))
        if rod:
            self._record(
                miniapp_fishing_rod=str(rod.get("name") or ""),
                miniapp_fishing_rod_item_id=str(rod.get("itemId") or ""),
            )
        if challenge:
            return await self._finish_challenge(token, session, challenge)
        phase = str(session.get("phase") or "").strip().lower()
        if phase == "lobby":
            return await self._start_cast(token, shop, settings)
        if phase == "waiting":
            wait = self._wait_for_bite(session)
            self._record(
                miniapp_fishing_status="waiting",
                miniapp_fishing_last_error="",
                miniapp_fishing_next_run_time=(
                    datetime.now() + timedelta(seconds=wait)
                ).strftime(TIME_FORMAT),
            )
            return wait
        if phase in {"expired", "settled", "missed"}:
            catch_payload = await self.transport.fishing_result(IDENTITY, token)
            summary = fishing_result_summary({}, catch_payload)
            self._record(
                miniapp_fishing_status="settled",
                miniapp_fishing_last_result=summary,
                miniapp_fishing_last_error="",
                miniapp_fishing_next_run_time=(
                    datetime.now() + timedelta(seconds=3)
                ).strftime(TIME_FORMAT),
            )
            return 3
        self._record(
            miniapp_fishing_status="waiting",
            miniapp_fishing_last_error="",
            miniapp_fishing_next_run_time=(
                datetime.now() + timedelta(seconds=15)
            ).strftime(TIME_FORMAT),
        )
        return 15

    async def run_loop(self) -> None:
        if not self.supported:
            return
        startup = getattr(self.actor, "startup_done", None)
        if startup is not None:
            await startup.wait()
        while getattr(self.actor, "is_running", True):
            wait = self.retry_seconds
            try:
                settings = self.settings()
                if not settings.get("enabled"):
                    self._record(
                        miniapp_fishing_status="paused",
                        miniapp_fishing_last_error="",
                        miniapp_fishing_next_run_time="",
                    )
                    wait = DEFAULT_DISABLED_SECONDS
                else:
                    pause = getattr(self.actor, "pause_event", None)
                    if pause is not None:
                        await pause.wait()
                    pause_seconds = 0
                    resolver = getattr(self.actor, "identity_pause_seconds", None)
                    if callable(resolver):
                        pause_seconds = max(0, _integer(resolver(IDENTITY), 0))
                    if pause_seconds > 0:
                        wait = max(15, pause_seconds)
                    else:
                        wait = await self.run_cycle(settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                previous_status = str(
                    self._state().get("miniapp_fishing_status") or ""
                )
                code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
                if code == "fishing_daily_limit_reached":
                    status = "daily_done"
                    wait = 300
                elif code == "fishing_rod_missing":
                    status = "no_rod"
                    wait = 300
                elif code in {"fishing_auth_refreshed", "hash_mismatch", "auth_date_expired"}:
                    status = "auth_refresh"
                    wait = 5
                else:
                    status = "error"
                    wait = self.retry_seconds
                self._record(
                    miniapp_fishing_status=status,
                    miniapp_fishing_last_error=code,
                    miniapp_fishing_last_error_time=_now_text(),
                    miniapp_fishing_next_run_time=(
                        datetime.now() + timedelta(seconds=wait)
                    ).strftime(TIME_FORMAT),
                )
                if status == "daily_done" and previous_status != status:
                    self.log.info("Mini App fishing daily limit reached; waiting for reset.")
                elif status == "no_rod" and previous_status != status:
                    self.log.warning("Mini App fishing paused: no fishing rod is available.")
                elif status == "auth_refresh" and previous_status != status:
                    self.log.info("Mini App fishing authorization refreshed; retrying shortly.")
                elif status == "error":
                    self.log.error("Mini App fishing loop failed: %s", code, exc_info=True)
            await asyncio.sleep(max(1, min(int(wait), 300)))
