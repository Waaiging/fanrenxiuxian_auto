#!/usr/bin/env python3
"""Read Wanling spirit-beast state from the Telegram Mini App."""

import asyncio
import hashlib
import inspect
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from telethon import types, utils
from telethon.tl.functions.messages import RequestMainWebViewRequest


DEFAULT_BOT_USERNAME = "fanrenxiuxian_bot"
DEFAULT_REFRESH_SECONDS = 30 * 60
DEFAULT_RETRY_SECONDS = 5 * 60
REFRESH_REQUEST_FILE = "miniapp_beast_refresh_request.json"
SESSION_CACHE_FILE = "miniapp_beast_session.json"


class MiniAppBeastError(RuntimeError):
    """A sanitized Mini App transport or API failure."""

    def __init__(self, code, status=0):
        self.code = str(code or "miniapp_request_failed")
        self.status = int(status or 0)
        super().__init__(self.code)


def miniapp_entry_start_param(entry_url):
    """Extract the fixed ``startapp`` token without logging the token itself."""
    parsed = urllib.parse.urlsplit(str(entry_url or "").strip())
    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if not parsed.scheme or not parsed.netloc or not token:
        raise MiniAppBeastError("invalid_entry_url")
    return token


def miniapp_origin(entry_url):
    parsed = urllib.parse.urlsplit(str(entry_url or "").strip())
    if parsed.netloc.lower() == "t.me":
        return "https://asc.aiopenai.app"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MiniAppBeastError("invalid_entry_url")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def extract_webview_init_data(webview_url):
    fragment = urllib.parse.parse_qs(urllib.parse.urlsplit(str(webview_url or "")).fragment)
    init_data = str((fragment.get("tgWebAppData") or [""])[0]).strip()
    if not init_data:
        raise MiniAppBeastError("init_data_missing")
    return init_data


def extract_spirit_token(value):
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if not token.lower().startswith("spiritbeast_"):
        raise MiniAppBeastError("spirit_beast_token_missing")
    return token


def normalize_spirit_beast_roster(payload):
    """Map Mini App ``beasts`` records to the existing automation cache schema."""
    rows = payload.get("beasts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise MiniAppBeastError("beast_roster_missing")
    beasts = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        beast_type = str(item.get("beastType") or "").strip()
        try:
            tier = max(0, int(item.get("tier") or 0))
            stamina = max(0, int(item.get("stamina") or 0))
            power = max(0, int(item.get("combatPower") or 0))
            experience = max(0, int(item.get("experience") or 0))
            beast_id = int(item.get("id") or 0)
            level = max(0, int(item.get("level") or 0))
        except (TypeError, ValueError):
            continue
        if not name or not beast_type or tier <= 0:
            continue
        beasts.append({
            "id": beast_id,
            "full_name": name,
            "status": str(item.get("status") or "未知").strip() or "未知",
            "species": f"{tier}阶{beast_type}",
            "beast_type": beast_type,
            "tier": tier,
            "level": level,
            "power": power,
            "stamina": stamina,
            "exp": experience,
            "is_active": bool(item.get("isActive")),
            "can_expedition": bool(item.get("canExpedition")),
        })
    if not beasts:
        raise MiniAppBeastError("beast_roster_empty")
    beasts.sort(
        key=lambda item: (item["power"], item["stamina"], item["full_name"]),
        reverse=True,
    )
    return beasts


def _post_json_sync(origin, path, payload, timeout):
    request = urllib.request.Request(
        urllib.parse.urljoin(origin.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin.rstrip("/") + "/miniapp/xianxia-dwelling",
            "User-Agent": "Mozilla/5.0 Telegram-Android/11.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(5, int(timeout or 20))) as response:
            status = int(getattr(response, "status", 200) or 200)
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        body = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise MiniAppBeastError(type(exc).__name__.lower()) from exc
    try:
        data = json.loads(body)
    except Exception as exc:
        raise MiniAppBeastError("invalid_json", status) from exc
    if not isinstance(data, dict):
        raise MiniAppBeastError("invalid_response", status)
    if status >= 400 or data.get("ok") is False:
        raise MiniAppBeastError(data.get("error") or f"http_{status}", status)
    return data


async def _post_json(origin, path, payload, timeout, post_json=None):
    if post_json is None:
        return await asyncio.to_thread(_post_json_sync, origin, path, payload, timeout)
    result = post_json(origin, path, payload, timeout)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise MiniAppBeastError("invalid_response")
    if result.get("ok") is False:
        raise MiniAppBeastError(result.get("error") or "miniapp_request_failed")
    return result


async def request_webview_init_data(client, bot_username, start_param):
    bot_entity = await client.get_entity(bot_username)
    result = await client(RequestMainWebViewRequest(
        peer=await client.get_input_entity(bot_entity),
        bot=utils.get_input_user(bot_entity),
        platform="android",
        start_param=start_param,
        theme_params=types.DataJSON(data="{}"),
    ))
    return extract_webview_init_data(getattr(result, "url", ""))


async def fetch_miniapp_beast_snapshot(
    client,
    entry_url,
    cached_spirit_token="",
    bot_username=DEFAULT_BOT_USERNAME,
    timeout=20,
    post_json=None,
):
    """Fetch one authoritative roster without sending any Telegram chat command."""
    entry_token = miniapp_entry_start_param(entry_url)
    origin = miniapp_origin(entry_url)
    init_data = await request_webview_init_data(client, bot_username, entry_token)

    spirit_token = str(cached_spirit_token or "").strip()
    if spirit_token:
        try:
            roster = await _post_json(
                origin,
                "/api/miniapp/xianxia-spirit-beast/start",
                {"token": spirit_token, "initData": init_data},
                timeout,
                post_json=post_json,
            )
            return {
                "beasts": normalize_spirit_beast_roster(roster),
                "player": roster.get("player") or {},
                "spirit_token": spirit_token,
            }
        except MiniAppBeastError:
            spirit_token = ""

    await _post_json(
        origin,
        "/api/miniapp/xianxia-dwelling/start",
        {"token": entry_token, "initData": init_data},
        timeout,
        post_json=post_json,
    )
    external = await _post_json(
        origin,
        "/api/miniapp/xianxia-dwelling/external",
        {"token": entry_token, "initData": init_data, "action": "spirit_beast"},
        timeout,
        post_json=post_json,
    )
    spirit_token = extract_spirit_token(external.get("url"))
    roster = await _post_json(
        origin,
        "/api/miniapp/xianxia-spirit-beast/start",
        {"token": spirit_token, "initData": init_data},
        timeout,
        post_json=post_json,
    )
    return {
        "beasts": normalize_spirit_beast_roster(roster),
        "player": roster.get("player") or {},
        "spirit_token": spirit_token,
    }


def refresh_request_path(config_dir):
    return os.path.join(os.path.abspath(config_dir), REFRESH_REQUEST_FILE)


def session_cache_path(config_dir):
    return os.path.join(os.path.abspath(config_dir), SESSION_CACHE_FILE)


def entry_fingerprint(entry_url):
    return hashlib.sha256(str(entry_url or "").strip().encode("utf-8")).hexdigest()


def read_cached_spirit_token(config_dir, entry_url):
    try:
        with open(session_cache_path(config_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("entry_fingerprint") != entry_fingerprint(entry_url):
            return ""
        token = str(data.get("spirit_token") or "").strip()
        return token if token.lower().startswith("spiritbeast_") else ""
    except Exception:
        return ""


def write_cached_spirit_token(config_dir, entry_url, spirit_token):
    token = str(spirit_token or "").strip()
    if not token.lower().startswith("spiritbeast_"):
        raise MiniAppBeastError("spirit_beast_token_missing")
    path = session_cache_path(config_dir)
    data = {
        "entry_fingerprint": entry_fingerprint(entry_url),
        "spirit_token": token,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_refresh_request(config_dir):
    try:
        with open(refresh_request_path(config_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_refresh_request(config_dir, requested_by="dashboard"):
    path = refresh_request_path(config_dir)
    data = {
        "request_id": uuid.uuid4().hex,
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "requested_by": str(requested_by or "dashboard")[:80],
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data
