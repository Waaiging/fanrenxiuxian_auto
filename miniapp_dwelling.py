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
EXACT_COMMANDS = {
    ".查看闭关",
    ".闭关修炼",
    ".深度闭关",
    ".元婴出窍",
    ".我的侍妾",
    ".天机代卜",
    ".入梦寻图",
    ".登天阶",
    ".引九天罡风",
    ".问心台",
    # The Mini App uses this read-only status command to initialize the old
    # cloud-stairs scheduler even though it is not exposed as a primary action.
    ".天阶状态",
    ".观命",
    ".问道",
    ".我的阴罗幡",
    ".每日献祭",
    ".血洗山林",
    ".召唤魔影",
    ".召回魔影",
    ".一键收取精华",
    ".辨认咒纹",
    ".借幡镇魂",
    ".剥离咒源",
}
PREFIX_COMMANDS = {
    ".化功为煞",
    ".囚禁魂魄",
    ".安抚幡灵",
    ".接取解咒委托",
    ".辨认咒纹",
    ".借幡镇魂",
    ".剥离咒源",
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
            return MiniAppCommandResponse(command_result_text(result), result)

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
    ) -> dict[str, Any]:
        token = await self._external_token_unlocked(identity, action, expected_prefix)
        body = {"token": token, "initData": self.init_data}
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
            if exc.code not in AUTH_ERROR_CODES | {"entry_token_missing", "invalid_token"}:
                raise
            token = await self._external_token_unlocked(
                identity,
                action,
                expected_prefix,
                force=True,
            )
            body["token"] = token
            return await _post_json(
                self.origin,
                path,
                body,
                self.timeout,
                post_json=self.post_json,
            )

    async def spirit_beast_snapshot(self, identity: str = "主魂") -> dict[str, Any]:
        async with self._lock:
            payload = await self._logged_operation(
                identity,
                "读取万兽谷灵兽列表",
                lambda: self._external_request_unlocked(
                    identity,
                    "spirit_beast",
                    "spiritbeast_",
                    "/api/miniapp/xianxia-spirit-beast/start",
                ),
                summarize=lambda result: f"{len(normalize_spirit_beast_roster(result))} 只灵兽",
            )
            return {
                "beasts": normalize_spirit_beast_roster(payload),
                "player": payload.get("player") or {},
                "raw": payload,
            }

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
            return await self._logged_operation(
                identity,
                f"万兽谷灵兽{interaction}（ID {beast_id}）",
                lambda: self._external_request_unlocked(
                    identity,
                    "spirit_beast",
                    "spiritbeast_",
                    "/api/miniapp/xianxia-spirit-beast/action",
                    payload={
                        "action": "interact",
                        "beastId": beast_id,
                        "interaction": interaction,
                    },
                ),
            )

    async def sect_farm_snapshot(self, identity: str) -> dict[str, Any]:
        async with self._lock:
            return await self._logged_operation(
                identity,
                "读取宗门灵圃",
                lambda: self._external_request_unlocked(
                    identity,
                    "sect_farm",
                    "farm_",
                    "/api/miniapp/xianxia-sect-farm/start",
                ),
                summarize=lambda result: f"{len(((result.get('domain') or {}).get('plots') or []))} 个星位",
            )

    async def sect_farm_action(
        self,
        identity: str,
        action: str,
        plot_key: str = "",
        star_name: str = "",
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
            return await self._logged_operation(
                identity,
                operation,
                lambda: self._external_request_unlocked(
                    identity,
                    "sect_farm",
                    "farm_",
                    "/api/miniapp/xianxia-sect-farm/action",
                    payload=body,
                ),
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

    sect_name = _snapshot_text(profile.get("sectName"), account.get("sectName"))
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
