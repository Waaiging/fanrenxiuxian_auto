#!/usr/bin/env python3
"""Telegram Mini App transport for dwelling-backed game commands."""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from miniapp_beast import (
    MiniAppBeastError,
    _post_json,
    extract_spirit_token,
    miniapp_entry_start_param,
    miniapp_origin,
    normalize_spirit_beast_roster,
    request_webview_init_data,
)
from reward_parsing import compact_reward_summary, daily_reward_items_for_command


AUTH_ERROR_CODES = {
    "auth_date_expired",
    "challenge_token_expired",
    "challenge_token_missing",
    "challenge_token_scope",
    "challenge_token_used",
    "hash_mismatch",
    "init_data_missing",
    "invalid_init_data",
    "invalid_token",
}

DESTINY_CHOICES = {"紫微", "天府", "太阴", "贪狼"}
DESTINY_ACTIONS = {"闭关", "炼制", "探索", "斗法"}
TRANSIENT_PROFILE_TEXTS = {
    "读取中",
    "加载中",
    "同步中",
    "查询中",
    "未知",
    "未知宗门",
    "暂无数据",
    "-",
    "--",
}
EXACT_COMMANDS = {
    ".查看闭关",
    ".闭关修炼",
    ".深度闭关",
    ".元婴出窍",
    ".我的侍妾",
    ".天机代卜",
    ".入梦寻图",
    ".拼图",
    ".登天阶",
    ".引九天罡风",
    ".问心台",
    # The Mini App uses this read-only status command to initialize the old
    # cloud-stairs scheduler even though it is not exposed as a primary action.
    ".天阶状态",
    ".观命",
    ".问道",
    ".小世界",
    ".显灵",
    ".安抚信徒",
    ".神迹 布道",
    ".我的阴罗幡",
    ".每日献祭",
    ".血洗山林",
    ".召唤魔影",
    ".召回魔影",
    ".一键收取精华",
}
PREFIX_COMMANDS = {
    ".化功为煞",
    ".囚禁魂魄",
    ".安抚幡灵",
}
SMALL_WORLD_COMMAND_ACTIONS = {
    ".显灵": "manifest",
    ".安抚信徒": "soothe",
    ".神迹 布道": "miracle_sermon",
}
SMALL_WORLD_ACTION_NAMES = {
    "collect": "小世界收割香火",
    "manifest": "小世界显灵",
    "soothe": "小世界安抚信徒",
    "miracle_sermon": "小世界神迹布道",
}


@dataclass(slots=True)
class MiniAppCommandResponse:
    """Small Telethon-message-compatible response used by existing parsers."""

    text: str
    payload: dict[str, Any]
    id: int = 0

    @property
    def raw_text(self) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text


def normalize_miniapp_command(command: str) -> str:
    return re.sub(r"\s+", " ", str(command or "").strip())


def miniapp_command_allowed(command: str) -> bool:
    command = normalize_miniapp_command(command)
    if command in EXACT_COMMANDS:
        return True
    parts = command.split(" ", 1)
    if len(parts) != 2:
        return False
    name, argument = parts
    if name in PREFIX_COMMANDS:
        return bool(argument.strip())
    if name == ".定命":
        return argument in DESTINY_CHOICES
    if name in {".推命", ".改命"}:
        return argument in DESTINY_ACTIONS
    return False


def extract_external_start_token(value: str, expected_prefix: str = "") -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if not token or (expected_prefix and not token.lower().startswith(expected_prefix.lower())):
        raise MiniAppBeastError("external_token_missing")
    return token


def command_result_text(payload: dict[str, Any]) -> str:
    result = payload.get("actionResult") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return ""
    return str(result.get("rawMessage") or result.get("message") or "").strip()


def command_result_ok(payload: dict[str, Any]) -> bool:
    result = payload.get("actionResult") if isinstance(payload, dict) else None
    return not isinstance(result, dict) or result.get("ok") is not False


def sect_farm_action_result_ok(payload: dict[str, Any], action: str) -> bool:
    """Accept harmless star-farm no-ops while preserving real failures."""
    if command_result_ok(payload):
        return True
    text = (command_result_text(payload) or miniapp_operation_result_text(payload)).strip().casefold()
    benign = {
        "soothe": {"nothing_to_soothe"},
        "collect": {"nothing_ready"},
    }
    return text in benign.get(str(action or "").strip().casefold(), set())


def small_world_data(payload: Any) -> dict[str, Any]:
    """Return the Mini App small-world block from a dwelling response."""
    if not isinstance(payload, dict):
        return {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    world = account.get("smallWorld") if isinstance(account.get("smallWorld"), dict) else None
    if world is None:
        world = payload.get("smallWorld") if isinstance(payload.get("smallWorld"), dict) else {}
    return world


def small_world_status_text(payload: Any) -> str:
    world = small_world_data(payload)
    if not world:
        return "小世界状态不可用"
    if world.get("hasWorld") is False:
        opened = world.get("open") if isinstance(world.get("open"), dict) else {}
        return str(opened.get("reasonText") or "尚未开辟小世界").strip()
    actions = world.get("actions") if isinstance(world.get("actions"), dict) else {}
    prayer = world.get("prayer") if isinstance(world.get("prayer"), dict) else None
    if prayer:
        return f"凡人祈愿待处理：{prayer.get('title') or '未命名祈愿'}"
    remaining = max(0, int(actions.get("prayerRemainingSeconds") or 0))
    return f"暂无凡人祈愿，下次约 {remaining} 秒" if remaining else "暂无凡人祈愿"


def miniapp_operation_result_text(payload: Any) -> str:
    """Return one concise, non-sensitive summary for Mini App operation logs."""
    if not isinstance(payload, dict):
        return "完成"
    result = payload.get("actionResult")
    if isinstance(result, dict):
        text = str(result.get("rawMessage") or result.get("message") or "").strip()
        if text:
            return re.sub(r"\s+", " ", text)[:500]
        if result.get("ok") is False:
            return str(result.get("error") or "操作失败")[:200]
    text = str(payload.get("rawMessage") or payload.get("message") or "").strip()
    if text:
        return re.sub(r"\s+", " ", text)[:500]
    if payload.get("ok") is False:
        return str(payload.get("error") or "操作失败")[:200]
    return "完成"


def _spirit_beast_reward_text(value: Any) -> str:
    """Normalize the loose reward shapes returned by spirit-beast actions."""
    parts: list[str] = []
    if isinstance(value, dict):
        iterable = value.items()
        for raw_name, raw_quantity in iterable:
            name = str(raw_name or "").strip()
            if not name:
                continue
            if isinstance(raw_quantity, dict):
                quantity = raw_quantity.get("quantity") or raw_quantity.get("count") or 1
            else:
                quantity = raw_quantity
            try:
                quantity = int(quantity or 0)
            except (TypeError, ValueError):
                quantity = 1
            parts.append(f"{name} x{max(1, quantity)}")
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("itemName") or item.get("label") or "").strip()
            if not name:
                continue
            try:
                quantity = int(item.get("quantity") or item.get("count") or 1)
            except (TypeError, ValueError):
                quantity = 1
            parts.append(f"{name} x{max(1, quantity)}")
    return "、".join(parts)


def spirit_beast_abyss_result_text(payload: Any) -> str:
    """Return one concise result for the Wan Beast Valley abyss action."""
    if not isinstance(payload, dict):
        return "探渊完成"
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    message = str(
        payload.get("message")
        or result.get("message")
        or result.get("rawMessage")
        or ""
    ).strip()
    if message:
        return re.sub(r"\s+", " ", message)[:500]

    won = result.get("won")
    title = str(result.get("title") or "").strip()
    parts = [title or ("探渊胜利" if won is True else "探渊完成")]
    beast = result.get("beast") if isinstance(result.get("beast"), dict) else {}
    wild_beast = result.get("wildBeast") if isinstance(result.get("wildBeast"), dict) else {}
    beast_name = str(beast.get("name") or "").strip()
    enemy_name = str(wild_beast.get("name") or "").strip()
    if beast_name and enemy_name:
        parts.append(f"{beast_name} 对阵 {enemy_name}")
    growth = result.get("growth")
    if isinstance(growth, dict):
        growth_text = "、".join(
            f"{key} +{value}"
            for key, value in growth.items()
            if value not in (None, "", 0, "0")
        )
        if growth_text:
            parts.append(growth_text)
    elif growth not in (None, "", 0, "0"):
        parts.append(f"成长 +{growth}")
    rewards = _spirit_beast_reward_text(result.get("rewards") or payload.get("rewards"))
    if rewards:
        parts.append(f"获得 {rewards}")
    return "，".join(parts)[:500]


def pagoda_challenge_result_text(payload: Any) -> str:
    """Return floor progress plus the actual pagoda reward/penalty summary."""
    replay = payload.get("replay") if isinstance(payload, dict) else {}
    replay = replay if isinstance(replay, dict) else {}
    cleared = int(replay.get("clearedCount") or 0)
    end_floor = int(replay.get("endFloor") or 0)
    failed_floor = int(replay.get("failedFloor") or 0)
    summary = f"通过 {cleared} 层，抵达第 {end_floor} 层"
    if failed_floor > 0:
        summary += f"，止步第 {failed_floor} 层"
    report = str(replay.get("report") or "").strip()
    reward_summary = compact_reward_summary(
        daily_reward_items_for_command(".闯塔", report)
    )
    if reward_summary:
        summary += f"；奖励：{reward_summary}"
    return summary[:500]


def sect_farm_snapshot_status(payload: Any) -> dict[str, Any]:
    """Normalize one sect-farm payload and derive its next useful wake-up."""
    domain = payload.get("domain") if isinstance(payload, dict) else None
    if not isinstance(domain, dict) or domain.get("mode") != "stars":
        raise MiniAppBeastError("star_farm_identity_mismatch")
    plots = [item for item in (domain.get("plots") or []) if isinstance(item, dict)]
    ready = 0
    troubled = 0
    empty: list[str] = []
    remaining_groups: dict[str, list[int]] = {}
    for item in plots:
        status = str(item.get("status") or item.get("statusLabel") or "").strip()
        if status == "可收集":
            ready += 1
        if status in {"星光黯淡", "元磁紊乱"}:
            troubled += 1
        if item.get("empty"):
            key = str(item.get("key") or item.get("plotKey") or "").strip()
            if key:
                empty.append(key)
            continue
        try:
            remaining = max(0, int(item.get("remainingSeconds") or 0))
        except (TypeError, ValueError):
            remaining = 0
        if remaining > 0:
            # Slots pulled to the same star mature only seconds apart.  Wake
            # after the last slot in that batch so one soothe/collect handles
            # the whole batch; mixed star types keep their own earlier batch.
            group = str(item.get("name") or item.get("starName") or "stars").strip() or "stars"
            remaining_groups.setdefault(group, []).append(remaining)
    next_wait_seconds = min(
        (max(values) for values in remaining_groups.values()),
        default=0,
    )
    return {
        "plots": plots,
        "ready_count": ready,
        "troubled_count": troubled,
        "empty_keys": empty,
        "next_wait_seconds": next_wait_seconds,
    }


def sect_farm_pull_batch_operation(plot_keys: Any) -> str:
    keys = [str(key).strip() for key in (plot_keys or []) if str(key).strip()]
    return f"宗门灵圃牵引星辰（{len(keys)}个星位）"


def sect_farm_pull_batch_result_text(
    plot_keys: Any,
    star_name: str,
    result_texts: Any = None,
) -> str:
    keys = [str(key).strip() for key in (plot_keys or []) if str(key).strip()]
    target = str(star_name or "星辰").strip() or "星辰"
    summary = f"星位 {'、'.join(keys)} 已牵引{target}，共 {len(keys)} 个引星盘"
    cultivation_cost = 0
    for text in result_texts or []:
        for amount in re.findall(r"消耗修为\s*([\d,]+)", str(text or "").replace("**", "")):
            try:
                cultivation_cost += int(amount.replace(",", ""))
            except ValueError:
                continue
    if cultivation_cost:
        summary += f"，消耗修为 {cultivation_cost:,}"
    return summary


class MiniAppDwellingTransport:
    """Authenticated, identity-aware access to the fixed dwelling entry."""

    def __init__(
        self,
        client: Any,
        entry_url: str,
        bot_username: str = "fanrenxiuxian_bot",
        timeout: int = 20,
        logger: Any = None,
        post_json: Any = None,
    ) -> None:
        self.client = client
        self.entry_url = str(entry_url or "").strip()
        self.bot_username = str(bot_username or "fanrenxiuxian_bot").strip()
        self.timeout = max(5, min(60, int(timeout or 20)))
        self.logger = logger
        self.post_json = post_json
        self.entry_token = miniapp_entry_start_param(self.entry_url)
        self.origin = miniapp_origin(self.entry_url)
        self.init_data = ""
        self.start_payload: dict[str, Any] = {}
        self.identity_choices: list[dict[str, Any]] = []
        self.identity_player_ids: dict[str, int] = {}
        self._external_tokens: dict[tuple[int, str], str] = {}
        self._lock = asyncio.Lock()

    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self.logger, level, None)
        if callable(method):
            method(message, *args)

    async def _logged_operation(
        self,
        identity: str,
        operation: str,
        callback: Any,
        summarize: Any = None,
    ) -> dict[str, Any]:
        """Execute one semantic Mini App operation and always leave an audit log."""
        self._log("info", "OUT [Mini App | %s]:\n%s", identity, operation)
        try:
            payload = await callback()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower()
            self._log("error", "Mini App [%s] %s失败：%s", identity, operation, code)
            raise
        try:
            summary = summarize(payload) if callable(summarize) else miniapp_operation_result_text(payload)
        except Exception:
            # Logging must never turn a successful Mini App operation into a failure.
            summary = miniapp_operation_result_text(payload)
        summary = re.sub(r"\s+", " ", str(summary or "完成").strip())[:500]
        self._log(
            "info",
            "IN [Mini App | %s]:\n%s -> %s",
            identity,
            operation,
            summary or "完成",
        )
        return payload

    async def initialize(self, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            if self.init_data and self.start_payload and not force:
                return self.start_payload
            return await self._initialize_unlocked()

    async def _initialize_unlocked(self) -> dict[str, Any]:
        self.init_data = await request_webview_init_data(
            self.client,
            self.bot_username,
            self.entry_token,
        )
        payload = await _post_json(
            self.origin,
            "/api/miniapp/xianxia-dwelling/start",
            {"token": self.entry_token, "initData": self.init_data},
            self.timeout,
            post_json=self.post_json,
        )
        identity = payload.get("identity") or {}
        choices = identity.get("choices") or []
        self.identity_choices = [item for item in choices if isinstance(item, dict)]
        self.identity_player_ids = {}
        personal_id = None
        for item in self.identity_choices:
            try:
                player_id = int(item.get("playerId"))
            except (TypeError, ValueError):
                continue
            if item.get("source") == "personal" and personal_id is None:
                personal_id = player_id
            for value in (
                item.get("daoName"),
                item.get("displayName"),
                item.get("username"),
            ):
                key = str(value or "").strip()
                if key:
                    self.identity_player_ids[key] = player_id
                    self.identity_player_ids[key.casefold()] = player_id
        selected = identity.get("selectedPlayerId")
        try:
            personal_id = personal_id or int(selected)
        except (TypeError, ValueError):
            pass
        if personal_id is not None:
            self.identity_player_ids["主魂"] = int(personal_id)
        if not self.identity_player_ids.get("主魂"):
            raise MiniAppBeastError("miniapp_identity_missing")
        self.start_payload = payload
        self._external_tokens.clear()
        self._log(
            "info",
            "Mini App dwelling authenticated with %s available identities",
            len(self.identity_choices),
        )
        return payload

    def player_id(self, identity: str = "主魂") -> int:
        key = str(identity or "主魂").strip() or "主魂"
        value = self.identity_player_ids.get(key)
        if value is None:
            value = self.identity_player_ids.get(key.casefold())
        if value is None:
            raise MiniAppBeastError("miniapp_identity_unknown")
        return int(value)

    async def _request_unlocked(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        identity: str = "主魂",
        retry_auth: bool = True,
        include_player_id: bool = True,
    ) -> dict[str, Any]:
        if not self.init_data or not self.start_payload:
            await self._initialize_unlocked()
        body = {
            "token": self.entry_token,
            "initData": self.init_data,
        }
        if include_player_id:
            body["playerId"] = self.player_id(identity)
        body.update(payload or {})
        try:
            return await _post_json(
                self.origin,
                path,
                body,
                self.timeout,
                post_json=self.post_json,
            )
        except MiniAppBeastError as exc:
            if retry_auth and exc.code in AUTH_ERROR_CODES:
                self._log("warning", "Mini App authorization expired; refreshing fixed entry")
                await self._initialize_unlocked()
                return await self._request_unlocked(
                    path,
                    payload=payload,
                    identity=identity,
                    retry_auth=False,
                    include_player_id=include_player_id,
                )
            raise

    async def request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        identity: str = "主魂",
        include_player_id: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            return await self._request_unlocked(
                path,
                payload=payload,
                identity=identity,
                include_player_id=include_player_id,
            )

    async def details(self, identity: str = "主魂") -> dict[str, Any]:
        return await self._logged_operation(
            identity,
            "同步洞府详情",
            lambda: self.request(
                "/api/miniapp/xianxia-dwelling/details",
                identity=identity,
            ),
        )

    async def overview(self, identity: str = "主魂") -> dict[str, Any]:
        """Fetch a background profile snapshot without routine success logs."""
        return await self.request(
            "/api/miniapp/xianxia-dwelling/overview",
            identity=identity,
        )

    async def small_world_snapshot(self, identity: str = "主魂") -> dict[str, Any]:
        """Read small-world state silently for cooldown-aware scheduling."""
        async with self._lock:
            return await self._request_unlocked(
                "/api/miniapp/xianxia-dwelling/details",
                identity=identity,
            )

    async def small_world_action(self, identity: str, action: str) -> dict[str, Any]:
        action = str(action or "").strip()
        operation = SMALL_WORLD_ACTION_NAMES.get(action)
        if not operation:
            raise MiniAppBeastError("small_world_action_not_allowed")
        async with self._lock:
            return await self._logged_operation(
                identity,
                operation,
                lambda: self._request_unlocked(
                    "/api/miniapp/xianxia-dwelling/small-world",
                    {"action": action},
                    identity=identity,
                ),
            )

    async def journey_snapshot(self, identity: str = "主魂") -> dict[str, Any]:
        """Read the dwelling journey state without routine success logs."""
        async with self._lock:
            return await self._request_unlocked(
                "/api/miniapp/xianxia-dwelling/details",
                identity=identity,
            )

    async def journey_action(
        self,
        identity: str,
        mode: str = "deep",
    ) -> dict[str, Any]:
        """Run one Mini App wild-experience action from the journey tab."""
        mode = str(mode or "").strip().casefold()
        if mode != "deep":
            raise MiniAppBeastError("wild_experience_mode_invalid")
        async with self._lock:
            return await self._logged_operation(
                identity,
                "游历·野外历练（深入）",
                lambda: self._request_unlocked(
                    "/api/miniapp/xianxia-dwelling/journey",
                    {"action": "wild_experience", "mode": mode},
                    identity=identity,
                ),
            )

    async def journey_with_destiny_prefix(
        self,
        identity: str,
        prefix_command: str = ".改命 探索",
        mode: str = "deep",
    ) -> tuple[MiniAppCommandResponse, dict[str, Any] | None]:
        """Execute the destiny prefix and deep click as one transport transaction."""
        prefix_command = normalize_miniapp_command(prefix_command)
        mode = str(mode or "").strip().casefold()
        if prefix_command != ".改命 探索":
            raise MiniAppBeastError("journey_prefix_invalid")
        if mode != "deep":
            raise MiniAppBeastError("wild_experience_mode_invalid")
        async with self._lock:
            prefix_payload = await self._logged_operation(
                identity,
                f"指令 {prefix_command}",
                lambda: self._request_unlocked(
                    "/api/miniapp/xianxia-dwelling/command-center",
                    {"command": prefix_command},
                    identity=identity,
                ),
            )
            prefix = MiniAppCommandResponse(
                command_result_text(prefix_payload),
                prefix_payload,
            )
            if not command_result_ok(prefix_payload):
                return prefix, None
            journey_payload = await self._logged_operation(
                identity,
                "游历·野外历练（深入）",
                lambda: self._request_unlocked(
                    "/api/miniapp/xianxia-dwelling/journey",
                    {"action": "wild_experience", "mode": mode},
                    identity=identity,
                ),
            )
            return prefix, journey_payload

    async def command(
        self,
        command: str,
        identity: str = "主魂",
        meditation_prefix: bool = False,
    ) -> MiniAppCommandResponse:
        command = normalize_miniapp_command(command)
        if not miniapp_command_allowed(command):
            raise MiniAppBeastError("miniapp_command_not_allowed")
        async with self._lock:
            if meditation_prefix and command == ".闭关修炼":
                prefix = await self._logged_operation(
                    identity,
                    "指令 .推命 闭关（闭关前置）",
                    lambda: self._request_unlocked(
                        "/api/miniapp/xianxia-dwelling/command-center",
                        {"command": ".推命 闭关"},
                        identity=identity,
                    ),
                )
                if not command_result_ok(prefix):
                    return MiniAppCommandResponse(command_result_text(prefix), prefix)

            if command == ".闭关修炼":
                path = "/api/miniapp/xianxia-dwelling/cultivation"
                payload: dict[str, Any] = {}
            elif command == ".深度闭关":
                path = "/api/miniapp/xianxia-dwelling/deep-seclusion"
                payload = {"action": "start"}
            elif command == ".查看闭关":
                path = "/api/miniapp/xianxia-dwelling/deep-seclusion"
                payload = {"action": "status"}
            elif command == ".小世界":
                path = "/api/miniapp/xianxia-dwelling/details"
                payload = {}
            elif command in SMALL_WORLD_COMMAND_ACTIONS:
                path = "/api/miniapp/xianxia-dwelling/small-world"
                payload = {"action": SMALL_WORLD_COMMAND_ACTIONS[command]}
            else:
                path = "/api/miniapp/xianxia-dwelling/command-center"
                payload = {"command": command}
            result = await self._logged_operation(
                identity,
                f"指令 {command}",
                lambda: self._request_unlocked(path, payload, identity=identity),
            )
            if command == ".查看闭关":
                deep = (
                    ((result.get("dwelling") or {}).get("meditation") or {}).get("deepSeclusion")
                    or {}
                )
                if deep.get("completed") or deep.get("canSettle"):
                    result = await self._logged_operation(
                        identity,
                        "深度闭关自动结算",
                        lambda: self._request_unlocked(
                            "/api/miniapp/xianxia-dwelling/deep-seclusion",
                            {"action": "settle"},
                            identity=identity,
                        ),
                    )
            text = small_world_status_text(result) if command == ".小世界" else command_result_text(result)
            return MiniAppCommandResponse(text, result)

    async def _external_token_unlocked(
        self,
        identity: str,
        action: str,
        expected_prefix: str,
        force: bool = False,
    ) -> str:
        if not self.init_data or not self.start_payload:
            await self._initialize_unlocked()
        player_id = self.player_id(identity)
        cache_key = (player_id, action)
        if not force and self._external_tokens.get(cache_key):
            return self._external_tokens[cache_key]
        payload = await self._request_unlocked(
            "/api/miniapp/xianxia-dwelling/external",
            {"action": action},
            identity=identity,
        )
        token = (
            extract_spirit_token(payload.get("url"))
            if expected_prefix.lower() == "spiritbeast_"
            else extract_external_start_token(payload.get("url"), expected_prefix)
        )
        self._external_tokens[cache_key] = token
        return token

    async def _external_request_unlocked(
        self,
        identity: str,
        action: str,
        expected_prefix: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        token = await self._external_token_unlocked(identity, action, expected_prefix)
        body = {"token": token, "initData": self.init_data}
        body.update(payload or {})
        try:
            return await _post_json(
                self.origin,
                path,
                body,
                int(timeout or self.timeout),
                post_json=self.post_json,
            )
        except MiniAppBeastError as exc:
            if exc.code not in AUTH_ERROR_CODES | {"entry_token_missing", "invalid_token"}:
                raise
            self._log(
                "warning",
                "Mini App external authorization expired for %s; refreshing fixed entry",
                action,
            )
            # External entry tokens and Telegram WebApp initData are signed as
            # one authorization context.  Refreshing only the external token
            # can keep pairing it with stale initData and repeatedly produce
            # hash_mismatch (most visible in long-running restricted workers).
            await self._initialize_unlocked()
            token = await self._external_token_unlocked(
                identity,
                action,
                expected_prefix,
                force=True,
            )
            body = {"token": token, "initData": self.init_data}
            body.update(payload or {})
            return await _post_json(
                self.origin,
                path,
                body,
                int(timeout or self.timeout),
                post_json=self.post_json,
            )

    async def spirit_beast_snapshot(
        self,
        identity: str = "主魂",
        log_operation: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            request = lambda: self._external_request_unlocked(
                identity,
                "spirit_beast",
                "spiritbeast_",
                "/api/miniapp/xianxia-spirit-beast/start",
            )
            if log_operation:
                payload = await self._logged_operation(
                    identity,
                    "读取万兽谷灵兽列表",
                    request,
                    summarize=lambda result: f"{len(normalize_spirit_beast_roster(result))} 只灵兽",
                )
            else:
                payload = await request()
            return {
                "beasts": normalize_spirit_beast_roster(payload),
                "player": payload.get("player") or {},
                "raw": payload,
            }

    async def spirit_beast_abyss_enter(
        self,
        identity: str,
        beast_id: int,
        beast_name: str = "",
    ) -> dict[str, Any]:
        try:
            beast_id = int(beast_id)
        except (TypeError, ValueError) as exc:
            raise MiniAppBeastError("spirit_beast_id_invalid") from exc
        if beast_id <= 0:
            raise MiniAppBeastError("spirit_beast_id_invalid")
        detail = str(beast_name or beast_id).strip()
        operation = f"万兽谷探渊（{detail}）"
        async with self._lock:
            return await self._logged_operation(
                identity,
                operation,
                lambda: self._external_request_unlocked(
                    identity,
                    "spirit_beast",
                    "spiritbeast_",
                    "/api/miniapp/xianxia-spirit-beast/abyss/enter",
                    payload={"beastId": beast_id},
                ),
                summarize=spirit_beast_abyss_result_text,
            )

    async def spirit_beast_interaction(
        self,
        identity: str,
        beast_id: int,
        interaction: str = "安抚",
    ) -> dict[str, Any]:
        if interaction != "安抚":
            raise MiniAppBeastError("spirit_beast_interaction_not_allowed")
        try:
            beast_id = int(beast_id)
        except (TypeError, ValueError) as exc:
            raise MiniAppBeastError("spirit_beast_id_invalid") from exc
        if beast_id <= 0:
            raise MiniAppBeastError("spirit_beast_id_invalid")
        async with self._lock:
            # Contract cycles can soothe many beasts at once.  The worker
            # emits one combined audit entry after the batch instead of two
            # transport entries for every individual beast.
            return await self._external_request_unlocked(
                identity,
                "spirit_beast",
                "spiritbeast_",
                "/api/miniapp/xianxia-spirit-beast/action",
                payload={
                    "action": "interact",
                    "beastId": beast_id,
                    "interaction": interaction,
                },
            )

    async def sect_farm_snapshot(self, identity: str) -> dict[str, Any]:
        """Read the farm state silently; callers log actions and failures."""
        async with self._lock:
            return await self._external_request_unlocked(
                identity,
                "sect_farm",
                "farm_",
                "/api/miniapp/xianxia-sect-farm/start",
            )

    async def sect_farm_action(
        self,
        identity: str,
        action: str,
        plot_key: str = "",
        star_name: str = "",
        log_operation: bool = True,
    ) -> dict[str, Any]:
        if action not in {"collect", "soothe", "pull"}:
            raise MiniAppBeastError("sect_farm_action_not_allowed")
        body: dict[str, Any] = {"action": action, "plotKey": str(plot_key or "")}
        if action == "pull":
            if not star_name:
                raise MiniAppBeastError("star_name_missing")
            body["starName"] = str(star_name)
        action_name = {"collect": "收集精华", "soothe": "安抚星辰", "pull": "牵引星辰"}[action]
        detail = str(plot_key or star_name or "").strip()
        operation = f"宗门灵圃{action_name}{f'（{detail}）' if detail else ''}"
        async with self._lock:
            request = lambda: self._external_request_unlocked(
                identity,
                "sect_farm",
                "farm_",
                "/api/miniapp/xianxia-sect-farm/action",
                payload=body,
            )
            if log_operation:
                return await self._logged_operation(
                    identity,
                    operation,
                    request,
                )
            return await request()

    async def _fishing_request_unlocked(
        self,
        identity: str,
        token: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        if not self.init_data or not self.start_payload:
            await self._initialize_unlocked()
        body = {"token": str(token or "").strip(), "initData": self.init_data}
        if not body["token"]:
            raise MiniAppBeastError("fishing_token_missing")
        body.update(payload or {})
        try:
            return await _post_json(
                self.origin,
                path,
                body,
                int(timeout or self.timeout),
                post_json=self.post_json,
            )
        except MiniAppBeastError as exc:
            if exc.code not in AUTH_ERROR_CODES:
                raise
            # A fishing token is signed together with the current initData.
            # Refresh both on the next cycle instead of pairing an old cast
            # token with newly signed Telegram data.
            await self._initialize_unlocked()
            self._external_tokens.pop((self.player_id(identity), "fishing"), None)
            raise MiniAppBeastError("fishing_auth_refreshed") from exc

    async def fishing_entry(self, identity: str = "主魂") -> tuple[str, dict[str, Any]]:
        """Create or resume the identity's fishing lobby/cast."""
        async with self._lock:
            token = await self._external_token_unlocked(
                identity,
                "fishing",
                "fish_",
                force=True,
            )
            payload = await self._fishing_request_unlocked(
                identity,
                token,
                "/api/miniapp/xianxia-fishing/start",
            )
            return str(payload.get("token") or token), payload

    async def fishing_start(
        self,
        identity: str,
        token: str,
    ) -> tuple[str, dict[str, Any]]:
        async with self._lock:
            payload = await self._fishing_request_unlocked(
                identity,
                token,
                "/api/miniapp/xianxia-fishing/start",
            )
            return str(payload.get("token") or token), payload

    async def fishing_shop(self, identity: str, token: str) -> dict[str, Any]:
        async with self._lock:
            return await self._fishing_request_unlocked(
                identity,
                token,
                "/api/miniapp/xianxia-fishing/shop",
            )

    async def fishing_buy_bait(
        self,
        identity: str,
        token: str,
        bait_key: str,
        quantity: int,
    ) -> dict[str, Any]:
        bait_key = str(bait_key or "").strip()
        quantity = int(quantity or 0)
        if not bait_key or quantity < 1 or quantity > 99:
            raise MiniAppBeastError("fishing_quantity_invalid")
        async with self._lock:
            return await self._logged_operation(
                identity,
                f"灵溪垂钓购买鱼饵（{bait_key} x{quantity}）",
                lambda: self._fishing_request_unlocked(
                    identity,
                    token,
                    "/api/miniapp/xianxia-fishing/buy-bait",
                    {"baitKey": bait_key, "quantity": quantity},
                ),
            )

    async def fishing_apply_chum(
        self,
        identity: str,
        token: str,
        chum_key: str,
    ) -> dict[str, Any]:
        chum_key = str(chum_key or "").strip()
        if not chum_key:
            raise MiniAppBeastError("fishing_chum_invalid")
        async with self._lock:
            return await self._logged_operation(
                identity,
                f"灵溪垂钓打窝（{chum_key}）",
                lambda: self._fishing_request_unlocked(
                    identity,
                    token,
                    "/api/miniapp/xianxia-fishing/chum",
                    {"chumKey": chum_key},
                ),
            )

    async def fishing_next_cast(
        self,
        identity: str,
        token: str,
        pond_key: str,
        bait_item_id: str,
        log_operation: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        pond_key = str(pond_key or "").strip()
        bait_item_id = str(bait_item_id or "").strip()
        if not pond_key:
            raise MiniAppBeastError("fishing_pond_invalid")
        if not bait_item_id:
            raise MiniAppBeastError("fishing_bait_invalid")
        async with self._lock:
            request = lambda: self._fishing_request_unlocked(
                identity,
                token,
                "/api/miniapp/xianxia-fishing/next",
                {"pondKey": pond_key, "baitItemId": bait_item_id},
            )
            payload = (
                await self._logged_operation(
                    identity,
                    f"灵溪垂钓开竿（{pond_key} · {bait_item_id}）",
                    request,
                )
                if log_operation
                else await request()
            )
            return str(payload.get("token") or token), payload

    async def fishing_finish(
        self,
        identity: str,
        token: str,
        proof: dict[str, Any],
        log_operation: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(proof, dict) or not proof.get("challengeId"):
            raise MiniAppBeastError("fishing_proof_invalid")
        async with self._lock:
            request = lambda: self._fishing_request_unlocked(
                identity,
                token,
                "/api/miniapp/xianxia-fishing/finish",
                {"fishingProof": proof},
            )
            if log_operation:
                return await self._logged_operation(
                    identity,
                    "灵溪垂钓自动收线",
                    request,
                )
            return await request()

    async def fishing_result(self, identity: str, token: str) -> dict[str, Any]:
        async with self._lock:
            return await self._fishing_request_unlocked(
                identity,
                token,
                "/api/miniapp/xianxia-fishing/result",
            )

    async def pagoda_snapshot(self, identity: str) -> dict[str, Any]:
        """Read the independent pagoda state without routine success logs."""
        async with self._lock:
            return await self._external_request_unlocked(
                identity,
                "pagoda",
                "pagoda_",
                "/api/miniapp/xianxia-pagoda/start",
            )

    async def pagoda_challenge(self, identity: str) -> dict[str, Any]:
        """Run the identity's single scheduled daily pagoda challenge."""

        async with self._lock:
            return await self._logged_operation(
                identity,
                "琉璃问心塔一念登塔",
                lambda: self._external_request_unlocked(
                    identity,
                    "pagoda",
                    "pagoda_",
                    "/api/miniapp/xianxia-pagoda/challenge",
                    timeout=max(60, self.timeout),
                ),
                summarize=pagoda_challenge_result_text,
            )

    async def hunt_snapshot(self, identity: str) -> dict[str, Any]:
        """Read the current daily hunt counter/session without routine logs."""
        async with self._lock:
            return await self._request_unlocked(
                "/api/miniapp/xianxia-dwelling/details",
                identity=identity,
            )

    async def hunt_start(self, identity: str) -> dict[str, Any]:
        async with self._lock:
            return await self._request_unlocked(
                "/api/miniapp/xianxia-dwelling/hunt",
                identity=identity,
            )

    async def hunt_reveal(
        self,
        identity: str,
        session_id: str,
        index: int,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise MiniAppBeastError("hunt_session_missing")
        try:
            index = int(index)
        except (TypeError, ValueError) as exc:
            raise MiniAppBeastError("hunt_cell_invalid") from exc
        if index < 0 or index >= 25:
            raise MiniAppBeastError("hunt_cell_invalid")

        async with self._lock:
            return await self._request_unlocked(
                "/api/miniapp/xianxia-dwelling/hunt/reveal",
                {"sessionId": session_id, "index": index},
                identity=identity,
            )

    async def hunt_settle(self, identity: str, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise MiniAppBeastError("hunt_session_missing")

        async with self._lock:
            return await self._request_unlocked(
                "/api/miniapp/xianxia-dwelling/hunt/settle",
                {"sessionId": session_id},
                identity=identity,
            )


def miniapp_timestamp(milliseconds: Any) -> str:
    try:
        value = float(milliseconds) / 1000.0
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def identity_state(actor: Any, identity: str) -> dict[str, Any]:
    if identity != "主魂" and hasattr(actor, "get_avatar_state"):
        return actor.get_avatar_state(identity)
    return actor.state


def _snapshot_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _snapshot_text(*values: Any) -> str:
    for value in values:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value).strip()
        if text and text.casefold() not in {"none", "null", "undefined"}:
            return text
    return ""


def _snapshot_authoritative_text(*values: Any) -> str:
    """Return profile text only when it is an authoritative value, not a UI placeholder."""
    text = _snapshot_text(*values)
    normalized = text.rstrip(".。…").strip()
    if normalized in TRANSIENT_PROFILE_TEXTS:
        return ""
    return text


def _snapshot_int(value: Any, *, minimum: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            normalized = value.strip().replace(",", "")
            if not re.fullmatch(r"[+-]?\d+(?:\.0+)?", normalized):
                return None
            number = int(float(normalized))
        else:
            number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if minimum is not None and number < minimum:
        return None
    return number


def _sync_identity_sect(actor: Any, identity: str, sect_name: str) -> None:
    state = getattr(actor, "state", None)
    if not isinstance(state, dict):
        return
    mapping = getattr(actor, "identity_sect_names", None)
    if not isinstance(mapping, dict):
        mapping = state.get("identity_sect_names")
    mapping = dict(mapping) if isinstance(mapping, dict) else {}
    mapping[identity] = sect_name
    state["identity_sect_names"] = dict(mapping)
    try:
        actor.identity_sect_names = mapping
    except (AttributeError, TypeError):
        pass
    if identity == "主魂":
        state["sect_name"] = sect_name
        try:
            actor.sect_name = sect_name
        except (AttributeError, TypeError):
            pass


def apply_dwelling_snapshot(actor: Any, identity: str, payload: dict[str, Any]) -> bool:
    """Copy authoritative Mini App meditation/identity state into legacy state."""
    container = identity_state(actor, identity)
    payload = _snapshot_mapping(payload)
    account = _snapshot_mapping(payload.get("account"))
    profile = _snapshot_mapping(account.get("profile"))
    cultivation = _snapshot_mapping(profile.get("cultivation"))
    dwelling = _snapshot_mapping(payload.get("dwelling"))
    meditation = _snapshot_mapping(dwelling.get("meditation"))
    deep = _snapshot_mapping(meditation.get("deepSeclusion"))
    standard = _snapshot_mapping(meditation.get("standardCultivation"))
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    container["miniapp_last_sync_time"] = now_text
    container["miniapp_last_error"] = ""

    player_id = _snapshot_int(account.get("playerId"))
    if player_id:
        container["miniapp_player_id"] = player_id
    dao_name = _snapshot_text(account.get("daoName"))
    if dao_name:
        container["miniapp_dao_name"] = dao_name

    # The overview endpoint can briefly expose UI placeholders such as
    # "读取中" while an identity profile is still loading. Never let those
    # transient values overwrite a previously confirmed sect mapping.
    sect_name = _snapshot_authoritative_text(profile.get("sectName"), account.get("sectName"))
    spirit_root_value = profile.get("spiritRoot")
    spirit_root = _snapshot_text(
        _snapshot_mapping(spirit_root_value).get("name"),
        spirit_root_value,
        profile.get("spiritRootName"),
        account.get("spiritRootName"),
    )
    cultivation_level = _snapshot_text(
        account.get("cultivationLevel"),
        profile.get("cultivationLevel"),
        cultivation.get("level"),
        standard.get("currentLevel"),
        meditation.get("currentLevel"),
    )
    current_exp = _snapshot_int(
        cultivation.get("current")
        if cultivation.get("current") is not None
        else meditation.get("currentCultivation"),
        minimum=0,
    )
    total_exp = _snapshot_int(
        cultivation.get("next")
        if cultivation.get("next") is not None
        else meditation.get("nextThreshold"),
        minimum=1,
    )
    if current_exp is None or total_exp is None:
        cultivation_text = _snapshot_text(cultivation.get("text"))
        match = re.search(r"([\d,]+)\s*/\s*([\d,]+)", cultivation_text)
        if match:
            if current_exp is None:
                current_exp = _snapshot_int(match.group(1), minimum=0)
            if total_exp is None:
                total_exp = _snapshot_int(match.group(2), minimum=1)

    has_profile_snapshot = False
    if sect_name:
        container["miniapp_sect_name"] = sect_name
        container["sect_name"] = sect_name
        _sync_identity_sect(actor, identity, sect_name)
        has_profile_snapshot = True
    if spirit_root:
        container["miniapp_spirit_root"] = spirit_root
        container["spirit_root"] = spirit_root
        has_profile_snapshot = True
    if cultivation_level:
        container["miniapp_cultivation_level"] = cultivation_level
        container["cultivation_level"] = cultivation_level
        container["level"] = cultivation_level
        has_profile_snapshot = True
    if current_exp is not None and total_exp is not None:
        container["miniapp_current_exp"] = current_exp
        container["miniapp_total_exp"] = total_exp
        container["current_exp"] = current_exp
        container["total_exp"] = total_exp
        has_profile_snapshot = True
    if has_profile_snapshot:
        snapshot = _snapshot_mapping(payload.get("snapshot"))
        snapshot_level = _snapshot_text(snapshot.get("level")) or "snapshot"
        container["miniapp_profile_updated_at"] = now_text
        container["miniapp_profile_source"] = f"dwelling_{snapshot_level}"

    # Command-center responses often contain placeholder dwelling/account
    # objects. Only authoritative details and meditation endpoints may change
    # the legacy meditation schedule.
    if not deep and not standard:
        return True

    active = bool(deep.get("active"))
    completed = bool(deep.get("completed") or deep.get("canSettle"))
    end_time = miniapp_timestamp(deep.get("endMs"))
    if active:
        container["in_deep_meditation"] = True
        if end_time:
            container["deep_meditation_end_time"] = end_time
        container["next_meditation_retry_time"] = ""
        container["meditation_restart_pending"] = False
    elif completed:
        container["in_deep_meditation"] = False
        container["deep_meditation_end_time"] = ""
        container["meditation_restart_pending"] = True
    else:
        container["in_deep_meditation"] = False
        container["deep_meditation_end_time"] = ""
        if deep.get("canStart"):
            container["meditation_restart_pending"] = True

    cooldown_until = miniapp_timestamp(standard.get("cooldownUntilMs"))
    if standard.get("cooldownRemainingSeconds") and cooldown_until:
        container["next_meditation_time"] = cooldown_until
    elif standard.get("canCultivate"):
        container["next_meditation_time"] = ""
    return True
