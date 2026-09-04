"""
【封魂咒 / 解咒委托自动化】

主号、副号和小号主魂负责南宫婉、封魂咒推演、护持神魂和发布解咒委托；
各自配置的阴罗宗身份负责接取委托并执行辨认、借幡、剥离三步。
小号发布的委托仍由副号玄续玄接取，因此玄续玄会为副号与小号分别保存状态，
并通过共享 JSON 接收小号委托 ID。
"""
import asyncio
import contextlib
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta

from automation_settings import (
    DEFAULT_SUB_YINLUO_IDENTITY,
    SUB_YINLUO_IDENTITY,
    canonical_automation_identity,
)
from common_command_features import add_seconds_str, dt_to_str, is_future, now_str, seconds_until
from yinluo_features import YINLUO_CONVERT_COMMAND, YINLUO_IDENTITY


SOUL_CURSE_VISIT_COMMAND = ".探望南宫婉"
SOUL_CURSE_WANYING_GREETING_COMMAND = ".婉影问安"
SOUL_CURSE_MOON_MEDITATION_COMMAND = ".月下合参"
# 月下合参：婉影共鸣·封魂同参阶段的合参指令，24 小时冷却。
SOUL_CURSE_MOON_MEDITATION_COOLDOWN_SECONDS = 24 * 3600
SOUL_CURSE_INFER_COMMAND = ".推演封魂咒"
SOUL_CURSE_PROTECT_COMMAND = ".护持神魂"
SOUL_CURSE_CO_STUDY_COMMAND = ".同参封魂"
SOUL_CURSE_PUBLISH_COMMAND = ".发布解咒委托 1"
SOUL_CURSE_ACCEPT_COMMAND = ".接取解咒委托"
SOUL_CURSE_IDENTIFY_COMMAND = ".辨认咒纹"
SOUL_CURSE_SUPPRESS_COMMAND = ".借幡镇魂"
SOUL_CURSE_STRIP_COMMAND = ".剥离咒源"

SOUL_CURSE_CHAIN_SECONDS = 8 * 3600
SOUL_CURSE_VISIT_HOUR = 9
SOUL_CURSE_RETRY_SECONDS = 10 * 60
SOUL_CURSE_UNKNOWN_RETRY_SECONDS = 30 * 60
SOUL_CURSE_SHARED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soul_curse_commissions.json")
SOUL_CURSE_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soul_curse_settings.json")

SOUL_CURSE_PUBLISHERS = {
    "main": {
        "owner_account": "main",
        "target_username": "@Weeguu",
        "assistant_account": "",
        "assistant_identity": "",
        "visit_minute": 0,
        "wanying_greeting_enabled": True,
        "moon_meditation_enabled": True,
        "shared": True,
    },
    "sub": {
        "owner_account": "sub",
        "target_username": "@Gamling33",
        "assistant_account": "",
        "assistant_identity": "",
        "visit_minute": 3,
        "moon_meditation_enabled": False,
        "shared": True,
    },
    "xiaohao": {
        "owner_account": "xiaohao",
        "target_username": "@TitanCreeper",
        "assistant_account": "",
        "assistant_identity": "",
        "visit_minute": 6,
        "moon_meditation_enabled": False,
        "shared": True,
    },
    "waaiging": {
        "owner_account": "waaiging",
        "target_username": "@Waaiging",
        "assistant_account": "",
        "assistant_identity": "",
        "visit_minute": 9,
        "moon_meditation_enabled": False,
        "shared": True,
    },
}

SOUL_CURSE_ASSISTANTS = {
    "main": {
        "owner_account": "main",
        "target_username": "@Weeguu",
        "assistant_identity": YINLUO_IDENTITY,
    },
    "sub": {
        "owner_account": "sub",
        "target_username": "@Gamling33",
        "assistant_identity": SUB_YINLUO_IDENTITY,
        "shared": False,
    },
}

SOUL_CURSE_SHARED_ASSISTANTS = {
    # 三个账号的委托全部进共享池；主号缘生子和副号玄续玄都按各自链路冷却竞争认领。
    "main": (
        {"owner_account": "sub", "target_username": "@Gamling33", "assistant_identity": YINLUO_IDENTITY, "shared": True},
        {"owner_account": "xiaohao", "target_username": "@TitanCreeper", "assistant_identity": YINLUO_IDENTITY, "shared": True},
        {"owner_account": "waaiging", "target_username": "@Waaiging", "assistant_identity": YINLUO_IDENTITY, "shared": True},
    ),
    "sub": (
        {"owner_account": "main", "target_username": "@Weeguu", "assistant_identity": SUB_YINLUO_IDENTITY, "shared": True},
        {"owner_account": "xiaohao", "target_username": "@TitanCreeper", "assistant_identity": SUB_YINLUO_IDENTITY, "shared": True},
        {"owner_account": "waaiging", "target_username": "@Waaiging", "assistant_identity": SUB_YINLUO_IDENTITY, "shared": True},
    ),
}


SOUL_CURSE_AVATAR_PUBLISHERS = {
    # 每账号化身 publisher 候选名单；实际启用由 soul_curse_settings.json 按身份开关。
    # visit_minute 逐身份错开，避免同账号多个身份同分钟探望刷屏。
    # 注意：阴罗宗身份（main 缘生子 / sub 玄续玄）不在此名单——它们的开关
    # 只控制 assist 链（接取→辨认→借幡→剥离）。给它们挂 publisher 链会以
    # 阴罗身份发探望/推演/护持/发布等无资格发送的前置指令。
    "main": (
        {"identity": "无咎子", "target_username": "@wuxinglinggen"},
        {"identity": "素缘子", "target_username": "@oldeinstein"},
    ),
    "sub": (
        {"identity": "厚土", "target_username": "@crayonxxin"},
        {"identity": "寻真子", "target_username": "@ding303"},
        {"identity": "寒续尘", "target_username": "@ding303"},
    ),
    "xiaohao": (
        {"identity": "问心子", "target_username": "@lianqi10000"},
        {"identity": "素心子", "target_username": "@hajiimiii"},
        {"identity": "灵脉玄", "target_username": "@adai925"},
    ),
}


def _strip_markdown(text):
    return str(text or "").replace("**", "").replace("`", "")


def _parse_duration_seconds(text):
    total = 0
    matched = False
    for pattern, factor in (
        (r"(\d+)\s*天", 86400),
        (r"(\d+)\s*(?:小时|时)", 3600),
        (r"(\d+)\s*(?:分钟|分)", 60),
        (r"(\d+)\s*秒", 1),
    ):
        for match in re.finditer(pattern, str(text or "")):
            total += int(match.group(1)) * factor
            matched = True
    return total if matched else 0


def _parse_remaining_seconds(text):
    clean = str(text or "")
    match = re.search(r"请在\s*([^后]+?)\s*后", clean)
    if match:
        remaining = _parse_duration_seconds(match.group(1))
        if remaining > 0:
            return remaining
    return _parse_duration_seconds(clean)


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _target_username(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith("@") else f"@{text}"


def _next_visit_time(done_today=False, minute=0, now=None):
    now = now or datetime.now()
    target = now.replace(hour=SOUL_CURSE_VISIT_HOUR, minute=int(minute or 0), second=0, microsecond=0)
    if done_today or now >= target:
        target = target + timedelta(days=1)
    return dt_to_str(target)


def _next_daily_time(done_today=False, hour=0, minute=5, now=None):
    now = now or datetime.now()
    target = now.replace(hour=int(hour or 0), minute=int(minute or 0), second=0, microsecond=0)
    if done_today or now >= target:
        target = target + timedelta(days=1)
    return dt_to_str(target)


def soul_curse_publisher_default_state():
    return {
        "last_visit_date": "",
        "next_visit_time": "",
        "last_wanying_greeting_date": "",
        "next_wanying_greeting_time": "",
        "last_wanying_greeting_time": "",
        "last_moon_meditation_date": "",
        "next_moon_meditation_time": "",
        "last_moon_meditation_time": "",
        "last_co_study_time": "",
        "next_co_study_time": "",
        "last_infer_time": "",
        "next_infer_time": "",
        "last_protect_time": "",
        "next_protect_time": "",
        "last_chain_time": "",
        "next_chain_time": "",
        "chain_stage": "",
        "commission_id": "",
        "commission_status": "",
        "commission_target": "",
        "commission_updated_at": "",
        "last_status": "init",
        "last_detail": "",
        "last_response": "",
        "next_action_at": "",
    }


def soul_curse_assist_default_state():
    return {
        "owner_account": "",
        "target_username": "",
        "commission_id": "",
        "accepted_commission_id": "",
        "identify_commission_id": "",
        "suppress_commission_id": "",
        "strip_commission_id": "",
        "completed_commission_id": "",
        "last_accept_time": "",
        "last_identify_time": "",
        "next_identify_time": "",
        "last_suppress_time": "",
        "next_suppress_time": "",
        "last_strip_time": "",
        "next_strip_time": "",
        "last_completed_time": "",
        "next_chain_time": "",
        "status": "init",
        "last_detail": "",
        "last_response": "",
        "next_action_at": "",
        "updated_at": "",
    }


def parse_soul_curse_visit(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if "今日已探望过南宫婉" in clean or ("南宫婉" in clean and "频繁惊扰" in clean):
        return {"matched": True, "status": "done", "cooldown_seconds": cd}
    if "探望南宫婉" in clean:
        if cd > 0 and any(k in clean for k in ("请在", "后再", "尚需", "冷却")):
            return {"matched": True, "status": "cooldown", "cooldown_seconds": cd}
        return {"matched": True, "status": "success", "cooldown_seconds": 24 * 3600}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_wanying_greeting(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if ("婉影问安" in clean or ("婉影" in clean and "问安" in clean)) and cd > 0 and any(
        k in clean for k in ("请在", "后再", "尚需", "冷却", "今日已")
    ):
        status = "done" if "今日已" in clean else "cooldown"
        return {"matched": True, "status": status, "cooldown_seconds": cd}
    if "今日已" in clean and ("婉影" in clean or "问安" in clean):
        return {"matched": True, "status": "done", "cooldown_seconds": 0}
    if "婉影问安" in clean or ("婉影" in clean and "问安" in clean):
        return {"matched": True, "status": "success", "cooldown_seconds": 24 * 3600}
    if any(k in clean for k in ("无法问安", "条件不足", "修为不足")):
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_moon_meditation(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    # 2026-09-01 实测回复：冷却态为「月影护持尚未恢复，请在 X 后再试」，
    # 文本不含“月下合参”字样——必须同时匹配“月影护持”。
    moon_markers = ("月下合参" in clean, ("月下" in clean and "合参" in clean), "月影护持" in clean)
    if any(moon_markers) and cd > 0 and any(
        k in clean for k in ("请在", "后再", "尚需", "冷却", "尚未恢复", "今日已")
    ):
        status = "done" if "今日已" in clean else "cooldown"
        return {"matched": True, "status": status, "cooldown_seconds": cd}
    if "今日已" in clean and any(moon_markers):
        return {"matched": True, "status": "done", "cooldown_seconds": 0}
    if "月下合参" in clean or ("月下" in clean and "合参" in clean):
        return {"matched": True, "status": "success", "cooldown_seconds": SOUL_CURSE_MOON_MEDITATION_COOLDOWN_SECONDS}
    if any(k in clean for k in ("无法合参", "条件不足", "修为不足", "远航")):
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_infer(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if "封魂咒" in clean and cd > 0 and any(k in clean for k in ("请在", "后再", "冷却", "变化极慢")):
        return {"matched": True, "status": "cooldown", "cooldown_seconds": cd}
    if "推演封魂咒" in clean or ("封魂咒" in clean and any(k in clean for k in ("咒源", "魂封", "推演"))):
        return {"matched": True, "status": "success", "cooldown_seconds": SOUL_CURSE_CHAIN_SECONDS}
    if any(k in clean for k in ("无法推演", "条件不足", "修为不足")):
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_protect(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if "神魂护持" in clean and cd > 0 and any(k in clean for k in ("请在", "后再", "冷却", "不可过密")):
        return {"matched": True, "status": "cooldown", "cooldown_seconds": cd}
    if "护持神魂" in clean or ("神魂" in clean and any(k in clean for k in ("魂封", "月魄", "护持"))):
        return {"matched": True, "status": "success", "cooldown_seconds": SOUL_CURSE_CHAIN_SECONDS}
    if any(k in clean for k in ("无法护持", "条件不足", "修为不足")):
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_co_study(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if ("同参封魂" in clean or ("同参" in clean and "封魂" in clean)) and cd > 0 and any(
        k in clean for k in ("请在", "后再", "冷却", "尚需", "不可频繁")
    ):
        return {"matched": True, "status": "cooldown", "cooldown_seconds": cd}
    if "同参封魂" in clean or ("同参" in clean and "封魂" in clean):
        return {"matched": True, "status": "success", "cooldown_seconds": SOUL_CURSE_CHAIN_SECONDS}
    if any(k in clean for k in ("无法同参", "条件不足", "修为不足")):
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_publish(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "commission_id": "", "cooldown_seconds": 0}
    commission_id = ""
    for pattern in (
        r"委托\s*ID[:：]\s*(\d+)",
        r"进行中的解咒委托\s*[（(]\s*ID[:：]\s*(\d+)",
        r"\bID[:：]\s*(\d+)",
    ):
        match = re.search(pattern, clean, flags=re.I)
        if match:
            commission_id = match.group(1)
            break
    if "解咒委托已发布" in clean and commission_id:
        return {"matched": True, "status": "success", "commission_id": commission_id, "cooldown_seconds": 0}
    if "已有进行中的解咒委托" in clean and commission_id:
        return {"matched": True, "status": "existing", "commission_id": commission_id, "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if cd > 0 and any(k in clean for k in ("冷却", "请在", "后再")):
        return {"matched": True, "status": "cooldown", "commission_id": "", "cooldown_seconds": cd}
    if any(k in clean for k in ("灵石不足", "不可发布", "无法发布", "条件不足")):
        return {"matched": True, "status": "blocked", "commission_id": "", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "commission_id": commission_id, "cooldown_seconds": 0}


def parse_soul_curse_accept(text):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    if "咒契协定已成" in clean or ("已接取" in clean and "解咒委托" in clean):
        return {"matched": True, "status": "success", "cooldown_seconds": 0}
    if "该委托不存在" in clean or "已被他人接取" in clean:
        return {"matched": True, "status": "gone", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    if "只有阴罗宗弟子" in clean or "不可接取" in clean or "无法接取" in clean:
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    cd = _parse_remaining_seconds(clean)
    if cd > 0 and any(k in clean for k in ("冷却", "请在", "后再")):
        return {"matched": True, "status": "cooldown", "cooldown_seconds": cd}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def parse_soul_curse_action(text, action):
    clean = _strip_markdown(text)
    if not clean:
        return {"matched": False, "status": "", "cooldown_seconds": 0}
    if "煞气不足" in clean:
        return {"matched": True, "status": "sha_not_enough", "cooldown_seconds": 0}
    cd = _parse_remaining_seconds(clean)
    if cd > 0 and any(k in clean for k in ("冷却", "请在", "后再试", "后再")):
        return {"matched": True, "status": "cooldown", "cooldown_seconds": cd}
    if "没有有效的咒契协定" in clean or "需先由对方发布委托" in clean:
        return {"matched": True, "status": "no_contract", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    if "咒源尚未辨明" in clean or "推进到 50" in clean:
        return {"matched": True, "status": "source_not_ready", "cooldown_seconds": 4 * 3600}
    if action == "identify" and ("阴罗辨咒" in clean or "辨认咒纹" in clean):
        return {"matched": True, "status": "success", "cooldown_seconds": 4 * 3600}
    if action == "suppress" and "借幡镇魂" in clean:
        return {"matched": True, "status": "success", "cooldown_seconds": 6 * 3600}
    if action == "strip" and ("剥离咒源成功" in clean or ("剥离咒源" in clean and "获得" in clean)):
        return {"matched": True, "status": "success", "cooldown_seconds": 8 * 3600}
    if any(k in clean for k in ("条件不足", "修为不足", "无法")):
        return {"matched": True, "status": "blocked", "cooldown_seconds": SOUL_CURSE_UNKNOWN_RETRY_SECONDS}
    return {"matched": False, "status": "", "cooldown_seconds": 0}


def read_soul_curse_shared_state():
    try:
        with open(SOUL_CURSE_SHARED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def write_soul_curse_shared_state(data):
    os.makedirs(os.path.dirname(SOUL_CURSE_SHARED_FILE), exist_ok=True)
    temp = f"{SOUL_CURSE_SHARED_FILE}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data if isinstance(data, dict) else {}, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp, SOUL_CURSE_SHARED_FILE)


def upsert_soul_curse_shared_commission(owner_account, updates):
    data = read_soul_curse_shared_state()
    key = str(owner_account or "").strip()
    if not key:
        return {}
    item = data.get(key, {}) if isinstance(data.get(key), dict) else {}
    item.update(updates or {})
    item["owner_account"] = key
    item["updated_at"] = now_str()
    data[key] = item
    write_soul_curse_shared_state(data)
    return item


@contextlib.asynccontextmanager
async def _null_async_context():
    yield


class SoulCurseMixin:
    def soul_curse_logger(self):
        if hasattr(self, "common_command_logger"):
            try:
                return self.common_command_logger()
            except Exception:
                pass
        return getattr(self, "log", None) or logging.getLogger(self.__class__.__name__)

    def soul_curse_response_text(self, response):
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        return str(getattr(response, "text", "") or getattr(response, "raw_text", "") or "")

    def soul_curse_account_key(self):
        return str(getattr(self, "account_key", "") or "").strip()

    def soul_curse_resolve_identity(self, identity, account=None):
        """Resolve a possibly stale Dao name to the live avatar name.

        A rebirth changes an avatar's Dao name while long-lived scheduler
        tasks and the soul-curse settings file can still contain the old one.
        Prefer the actor's in-memory resolver, then use the stable player-id
        resolver from ``automation_settings`` when its result is present in
        the actor's live avatar roster.  The roster check keeps lightweight
        test actors (and legacy state without a live roster) backwards
        compatible.
        """
        original = str(identity or "").strip() or "主魂"
        if original == "主魂":
            return original
        account = str(account or self.soul_curse_account_key()).strip()
        known = {
            str(name).strip()
            for name in (getattr(self, "avatars", []) or [])
            if str(name).strip()
        }
        state = getattr(self, "state", {}) or {}
        avatar_states = state.get("avatars") if isinstance(state, dict) else None
        if isinstance(avatar_states, dict):
            known.update(str(name).strip() for name in avatar_states if str(name).strip())
        resolved = original
        resolver = getattr(self, "resolve_avatar_identity", None)
        if callable(resolver):
            try:
                candidate = str(resolver(original) or "").strip()
            except Exception:
                candidate = ""
            if candidate and (not known or candidate in known):
                resolved = candidate
        try:
            canonical = str(canonical_automation_identity(account, resolved) or "").strip()
        except Exception:
            canonical = ""
        if canonical and (not known or canonical in known):
            resolved = canonical
        return resolved

    def soul_curse_yinluo_identity(self):
        """Return this account's current Yinluo identity, including renames."""
        account = self.soul_curse_account_key()
        if account not in {"main", "sub"}:
            # xiaohao/waaiging have no Yinluo assistant identity.  Returning
            # the main account's historical name here would make a Taiyi
            # avatar named 缘生子 look like an assistant after a rename.
            return ""
        configured = (
            getattr(self, "yinluo_identity", "")
            if account == "sub"
            else ""
        )
        defaults = (
            [configured, SUB_YINLUO_IDENTITY, DEFAULT_SUB_YINLUO_IDENTITY]
            if account == "sub"
            else [configured, YINLUO_IDENTITY]
        )
        known = {
            str(name).strip()
            for name in (getattr(self, "avatars", []) or [])
            if str(name).strip()
        }
        state = getattr(self, "state", {}) or {}
        avatar_states = state.get("avatars") if isinstance(state, dict) else None
        if isinstance(avatar_states, dict):
            known.update(str(name).strip() for name in avatar_states if str(name).strip())
        for candidate in defaults:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            resolved = self.soul_curse_resolve_identity(candidate, account)
            if not known or resolved in known:
                return resolved

        # If the static Yinluo name is stale and no alias was persisted, the
        # sect assignment is still an authoritative signal.
        sects = getattr(self, "identity_sect_names", {}) or {}
        if isinstance(sects, dict):
            for name in known:
                if str(sects.get(name) or "").strip() == "阴罗宗":
                    return name
        state_sects = getattr(self, "state", {}) or {}
        if isinstance(state_sects, dict) and isinstance(state_sects.get("identity_sect_names"), dict):
            for name in known:
                if str(state_sects["identity_sect_names"].get(name) or "").strip() == "阴罗宗":
                    return name
        fallback = next((str(x).strip() for x in defaults if str(x or "").strip()), "")
        return self.soul_curse_resolve_identity(fallback or YINLUO_IDENTITY, account)

    def soul_curse_identity_setting_candidates(self, account, identity):
        """Build current/legacy names accepted by the per-identity switch."""
        account = str(account or "").strip()
        original = str(identity or "主魂").strip() or "主魂"
        candidates = []

        def add(value):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)

        resolved_identity = self.soul_curse_resolve_identity(original, account)
        add(resolved_identity)
        add(original)
        if original != "主魂":
            current_yinluo = ""
            if account == self.soul_curse_account_key():
                current_yinluo = self.soul_curse_yinluo_identity()
            yinluo_names = {current_yinluo}
            if account == "main":
                yinluo_names.add(YINLUO_IDENTITY)
            elif account == "sub":
                yinluo_names.update({SUB_YINLUO_IDENTITY, DEFAULT_SUB_YINLUO_IDENTITY})
            is_yinluo = original in yinluo_names or resolved_identity in yinluo_names
            if is_yinluo:
                add(current_yinluo)
                if account == "main":
                    add(YINLUO_IDENTITY)
                elif account == "sub":
                    add(SUB_YINLUO_IDENTITY)
                    add(DEFAULT_SUB_YINLUO_IDENTITY)

        state = getattr(self, "state", {}) or {}
        aliases = state.get("avatar_dao_name_aliases") if isinstance(state, dict) else None
        if isinstance(aliases, dict):
            # Follow both directions so old settings survive a rename and a
            # stale command identity can still find the current switch.
            changed = True
            while changed:
                changed = False
                for old_name, new_name in aliases.items():
                    old_name = str(old_name or "").strip()
                    new_name = str(new_name or "").strip()
                    if not old_name or not new_name:
                        continue
                    if new_name in candidates and old_name not in candidates:
                        candidates.append(old_name)
                        changed = True
                    elif old_name in candidates and new_name not in candidates:
                        candidates.append(new_name)
                        changed = True
        return candidates

    def soul_curse_identity_enabled(self, account=None, identity="主魂"):
        """Per-identity kill switch backed by soul_curse_settings.json.

        Defaults to disabled: the whole soul-curse chain (visit / infer /
        protect / publish) only runs for identities explicitly enabled in the
        settings file, so the owner can opt each identity in manually.
        """
        account = str(account or self.soul_curse_account_key()).strip()
        identity = str(identity or "主魂").strip() or "主魂"
        try:
            with open(SOUL_CURSE_SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        if data.get("enabled") is False:
            return False
        identities = data.get("identities")
        if not isinstance(identities, dict):
            return False
        entry = identities.get(account)
        if isinstance(entry, dict):
            for candidate in self.soul_curse_identity_setting_candidates(account, identity):
                if candidate in entry:
                    return bool(entry.get(candidate))
            return False
        if isinstance(entry, list):
            enabled = {str(item).strip() for item in entry}
            return any(candidate in enabled for candidate in self.soul_curse_identity_setting_candidates(account, identity))
        return False

    def soul_curse_publisher_profile(self):
        return SOUL_CURSE_PUBLISHERS.get(self.soul_curse_account_key())

    def soul_curse_assistant_profile(self):
        profile = SOUL_CURSE_ASSISTANTS.get(self.soul_curse_account_key())
        if not profile:
            return None
        profile = dict(profile)
        profile["assistant_identity"] = self.soul_curse_yinluo_identity()
        return profile

    def soul_curse_shared_assistant_profiles(self):
        """共享池竞争者监听所有账号的委托（含本账号）。"""
        profiles = []
        yinluo_identity = self.soul_curse_yinluo_identity()
        for profile in SOUL_CURSE_SHARED_ASSISTANTS.get(self.soul_curse_account_key(), ()):
            profile = dict(profile)
            profile["assistant_identity"] = yinluo_identity
            profiles.append(profile)
        publisher = SOUL_CURSE_PUBLISHERS.get(self.soul_curse_account_key())
        # xiaohao/waaiging 只有发布身份，没有阴罗接取身份；不要把它们
        # 自己的 publisher 条目伪装成 assistant 候选，否则会先占用共享
        # 委托却无法执行接取动作，反而阻塞主号/副号阴罗身份。
        if publisher and publisher.get("shared") and yinluo_identity:
            own = {
                "owner_account": publisher.get("owner_account"),
                "target_username": publisher.get("target_username"),
                "assistant_identity": (
                    yinluo_identity
                ),
                "shared": True,
            }
            if not any(p.get("owner_account") == own["owner_account"] for p in profiles):
                profiles.append(own)
        return profiles

    def soul_curse_assistant_profiles(self):
        profiles = []
        primary = self.soul_curse_assistant_profile()
        if primary:
            profiles.append(primary)
        profiles.extend(self.soul_curse_shared_assistant_profiles())
        return profiles

    def get_soul_curse_state(self, identity="主魂"):
        identity = self.soul_curse_resolve_identity(identity)
        if identity == "主魂" or not hasattr(self, "get_avatar_state"):
            state = self.state.setdefault("soul_curse", {})
        else:
            try:
                avatar_state = self.get_avatar_state(identity)
            except Exception:
                avatar_state = None
            if not isinstance(avatar_state, dict):
                return self.state.setdefault("soul_curse", {})
            state = avatar_state.setdefault("soul_curse", {})
        defaults = soul_curse_publisher_default_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        return state

    def get_soul_curse_assist_state(self, identity=YINLUO_IDENTITY, owner_account=""):
        identity = self.soul_curse_resolve_identity(identity)
        target = self.get_avatar_state(identity) if identity != "主魂" and hasattr(self, "get_avatar_state") else self.state
        primary = self.soul_curse_assistant_profile() or {}
        primary_owner = str(primary.get("owner_account") or "").strip()
        owner_account = str(owner_account or primary_owner).strip()
        legacy = target.get("soul_curse_assist")
        if not isinstance(legacy, dict):
            legacy = {}
            target["soul_curse_assist"] = legacy

        legacy_owner = str(legacy.get("owner_account") or "").strip()
        if primary_owner and legacy_owner and legacy_owner != primary_owner:
            states = target.get("soul_curse_assists")
            if not isinstance(states, dict):
                states = {}
                target["soul_curse_assists"] = states
            existing = states.get(legacy_owner) if isinstance(states.get(legacy_owner), dict) else {}
            migrated = dict(legacy)
            migrated.update(existing)
            states[legacy_owner] = migrated
            legacy = {}
            target["soul_curse_assist"] = legacy

        if owner_account and primary_owner and owner_account != primary_owner:
            states = target.get("soul_curse_assists")
            if not isinstance(states, dict):
                states = {}
                target["soul_curse_assists"] = states
            state = states.setdefault(owner_account, {})
        else:
            state = legacy
        defaults = soul_curse_assist_default_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        if owner_account:
            state["owner_account"] = owner_account
        return state

    def soul_curse_assistant_profile_for_command(self, identity, command):
        identity = self.soul_curse_resolve_identity(identity)
        command = str(command or "").strip()
        profiles = self.soul_curse_assistant_profiles()
        for profile in profiles:
            target = _target_username(profile.get("target_username"))
            if target and target in command:
                return profile
        match = re.search(r"\.接取解咒委托\s+(\d+)", command)
        if match:
            commission_id = match.group(1)
            for profile in profiles:
                state = self.get_soul_curse_assist_state(
                    identity,
                    profile.get("owner_account"),
                )
                if str(state.get("commission_id") or "") == commission_id:
                    return profile
        return self.soul_curse_assistant_profile()

    def soul_curse_atomic_task(self, label):
        if hasattr(self, "common_atomic_task"):
            return self.common_atomic_task(label)
        return _null_async_context()

    def soul_curse_set_publisher_status(self, status, detail="", next_seconds=None, response="", identity="主魂"):
        state = self.get_soul_curse_state(identity)
        state["last_status"] = status
        state["last_detail"] = str(detail or "")[:300]
        if response:
            state["last_response"] = str(response or "")[:700]
        if next_seconds is not None:
            state["next_action_at"] = add_seconds_str(now_str(), max(0, int(next_seconds)))
        state["commission_updated_at"] = now_str()
        self.save_state()

    def soul_curse_set_assist_status(
        self,
        identity,
        status,
        detail="",
        next_seconds=None,
        response="",
        owner_account="",
    ):
        state = self.get_soul_curse_assist_state(identity, owner_account)
        state["status"] = status
        state["last_detail"] = str(detail or "")[:300]
        state["updated_at"] = now_str()
        if response:
            state["last_response"] = str(response or "")[:700]
        if next_seconds is not None:
            state["next_action_at"] = add_seconds_str(now_str(), max(0, int(next_seconds)))
        self.save_state()

    def soul_curse_mark_publisher_commission_completed(self, owner_account, commission_id, completed_at=None):
        publisher = self.soul_curse_publisher_profile()
        if not publisher or publisher.get("owner_account") != owner_account:
            return False
        state = self.get_soul_curse_state()
        if str(state.get("commission_id") or "") != str(commission_id or ""):
            return False
        completed_at = completed_at or now_str()
        next_ready = add_seconds_str(completed_at, SOUL_CURSE_CHAIN_SECONDS)
        state["commission_status"] = "completed"
        state["commission_updated_at"] = completed_at
        state["last_chain_time"] = completed_at
        state["next_chain_time"] = next_ready
        state["next_action_at"] = next_ready
        state["chain_stage"] = ""
        self.save_state()
        return True

    def soul_curse_command_paused(self, command, identity="主魂"):
        if hasattr(self, "dashboard_command_paused"):
            return bool(self.dashboard_command_paused(command, identity))
        return False

    async def soul_curse_send_main(self, command, timeout=60):
        return await self.send_and_wait_feedback(
            command,
            timeout=timeout,
            max_retries=0,
            suppress_no_response_alert=True,
        )

    async def soul_curse_send_identity(self, identity, command, timeout=60):
        return await self.send_and_wait_feedback_identity(
            identity,
            command,
            timeout=timeout,
            max_retries=0,
            force_identity_check=True,
            suppress_no_response_alert=True,
        )

    def record_soul_curse_visit_response(self, text, profile=None, identity="主魂"):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_visit(text)
        state = self.get_soul_curse_state(identity)
        minute = int(profile.get("visit_minute") or 0)
        if parsed.get("status") in {"success", "done"}:
            state["last_visit_date"] = _today()
            state["next_visit_time"] = _next_visit_time(done_today=True, minute=minute)
            self.soul_curse_set_publisher_status(parsed.get("status"), "南宫婉探望已记录", None, text, identity=identity)
            return True
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_visit_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status("visit_cooldown", f"探望冷却 {wait}秒", wait, text, identity=identity)
            return True
        if not text:
            state["next_visit_time"] = add_seconds_str(now_str(), SOUL_CURSE_RETRY_SECONDS)
            self.soul_curse_set_publisher_status("visit_no_response", "探望无回执，稍后重试", SOUL_CURSE_RETRY_SECONDS, identity=identity)
            return False
        state["next_visit_time"] = add_seconds_str(now_str(), SOUL_CURSE_UNKNOWN_RETRY_SECONDS)
        self.soul_curse_set_publisher_status("visit_unknown", "探望回执未识别", SOUL_CURSE_UNKNOWN_RETRY_SECONDS, text, identity=identity)
        return False

    def record_soul_curse_wanying_greeting_response(self, text, profile=None, identity="主魂"):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_wanying_greeting(text)
        state = self.get_soul_curse_state(identity)
        if parsed.get("status") in {"success", "done"}:
            now = now_str()
            state["last_wanying_greeting_date"] = _today()
            state["last_wanying_greeting_time"] = now
            state["next_wanying_greeting_time"] = _next_daily_time(
                done_today=True,
                minute=int(profile.get("wanying_minute") or 5),
            )
            self.soul_curse_set_publisher_status(parsed.get("status"), "婉影问安已记录", None, text, identity=identity)
            return True
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_wanying_greeting_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status("wanying_greeting_cooldown", f"婉影问安冷却 {wait}秒", wait, text, identity=identity)
            return True
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_wanying_greeting_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status("wanying_greeting_blocked", "婉影问安暂不可用", wait, text, identity=identity)
            return True
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["next_wanying_greeting_time"] = add_seconds_str(now_str(), wait)
        self.soul_curse_set_publisher_status(
            "wanying_greeting_unknown",
            "婉影问安回执未识别" if text else "婉影问安无回执",
            wait,
            text,
            identity=identity)
        return False

    def record_soul_curse_moon_meditation_response(self, text, profile=None, identity="主魂"):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_moon_meditation(text)
        state = self.get_soul_curse_state(identity)
        if parsed.get("status") in {"success", "done"}:
            now = now_str()
            state["last_moon_meditation_date"] = _today()
            state["last_moon_meditation_time"] = now
            state["next_moon_meditation_time"] = add_seconds_str(
                now, int(parsed.get("cooldown_seconds") or SOUL_CURSE_MOON_MEDITATION_COOLDOWN_SECONDS)
            )
            self.soul_curse_set_publisher_status(parsed.get("status"), "月下合参已记录", None, text, identity=identity)
            return True
        if parsed.get("status") in {"cooldown", "blocked"}:
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_moon_meditation_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status(
                f"moon_meditation_{parsed.get('status')}",
                f"月下合参{'冷却' if parsed.get('status') == 'cooldown' else '暂不可用'} {wait}秒",
                wait,
                text,
                identity=identity)
            return True
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["next_moon_meditation_time"] = add_seconds_str(now_str(), wait)
        self.soul_curse_set_publisher_status(
            "moon_meditation_unknown",
            "月下合参回执未识别" if text else "月下合参无回执",
            wait,
            text,
            identity=identity)
        return False

    def record_soul_curse_infer_response(self, text, identity="主魂"):
        parsed = parse_soul_curse_infer(text)
        state = self.get_soul_curse_state(identity)
        now = now_str()
        if parsed.get("status") == "success":
            state["last_infer_time"] = now
            state["next_infer_time"] = add_seconds_str(now, SOUL_CURSE_CHAIN_SECONDS)
            state["chain_stage"] = "protect"
            state["next_action_at"] = ""
            self.soul_curse_set_publisher_status("infer_success", "封魂咒推演完成，准备护持神魂", 3, text, identity=identity)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_infer_time"] = add_seconds_str(now, wait)
            state["chain_stage"] = "infer"
            self.soul_curse_set_publisher_status("infer_cooldown", f"推演冷却 {wait}秒", wait, text, identity=identity)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "infer"
            self.soul_curse_set_publisher_status("infer_blocked", "推演暂不可用", wait, text, identity=identity)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["chain_stage"] = "infer"
        self.soul_curse_set_publisher_status("infer_unknown", "推演回执未识别" if text else "推演无回执", wait, text, identity=identity)
        return "unknown"

    def record_soul_curse_protect_response(self, text, identity="主魂"):
        parsed = parse_soul_curse_protect(text)
        state = self.get_soul_curse_state(identity)
        now = now_str()
        if parsed.get("status") == "success":
            state["last_protect_time"] = now
            state["next_protect_time"] = add_seconds_str(now, SOUL_CURSE_CHAIN_SECONDS)
            state["chain_stage"] = "publish"
            state["next_action_at"] = ""
            self.soul_curse_set_publisher_status("protect_success", "护持神魂完成，准备发布委托", 3, text, identity=identity)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_protect_time"] = add_seconds_str(now, wait)
            state["chain_stage"] = "protect"
            self.soul_curse_set_publisher_status("protect_cooldown", f"护持冷却 {wait}秒", wait, text, identity=identity)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "protect"
            self.soul_curse_set_publisher_status("protect_blocked", "护持暂不可用", wait, text, identity=identity)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["chain_stage"] = "protect"
        self.soul_curse_set_publisher_status("protect_unknown", "护持回执未识别" if text else "护持无回执", wait, text, identity=identity)
        return "unknown"

    def record_soul_curse_co_study_response(self, text, identity="主魂"):
        parsed = parse_soul_curse_co_study(text)
        state = self.get_soul_curse_state(identity)
        now = now_str()
        if parsed.get("status") == "success":
            state["last_co_study_time"] = now
            state["next_co_study_time"] = add_seconds_str(now, int(parsed.get("cooldown_seconds") or SOUL_CURSE_CHAIN_SECONDS))
            self.soul_curse_set_publisher_status("co_study_success", "同参封魂已记录", 3, text, identity=identity)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_co_study_time"] = add_seconds_str(now, wait)
            self.soul_curse_set_publisher_status("co_study_cooldown", f"同参封魂冷却 {wait}秒", wait, text, identity=identity)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_co_study_time"] = add_seconds_str(now, wait)
            self.soul_curse_set_publisher_status("co_study_blocked", "同参封魂暂不可用", wait, text, identity=identity)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["next_co_study_time"] = add_seconds_str(now, wait)
        self.soul_curse_set_publisher_status("co_study_unknown", "同参封魂回执未识别" if text else "同参封魂无回执", wait, text, identity=identity)
        return "unknown"

    def record_soul_curse_publish_response(self, text, profile=None, identity="主魂"):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_publish(text)
        state = self.get_soul_curse_state(identity)
        now = now_str()
        if parsed.get("status") in {"success", "existing"} and parsed.get("commission_id"):
            commission_id = str(parsed.get("commission_id"))
            target = _target_username(profile.get("target_username"))
            state["commission_id"] = commission_id
            state["commission_target"] = target
            state["commission_status"] = parsed.get("status")
            state["chain_stage"] = ""
            state["last_chain_time"] = now
            state["next_chain_time"] = add_seconds_str(now, SOUL_CURSE_CHAIN_SECONDS)
            state["next_action_at"] = state["next_chain_time"]
            detail = f"解咒委托 ID {commission_id}，目标 {target}"
            if profile.get("shared"):
                upsert_soul_curse_shared_commission(profile.get("owner_key") or profile.get("owner_account"), {
                    "commission_id": commission_id,
                    "target_username": target,
                    # 认领留空：由就绪的阴罗身份在 assist tick 中按冷却竞争接取
                    "assistant_account": "",
                    "assistant_identity": "",
                    "status": "pending_accept",
                    "published_at": now,
                    "last_detail": detail,
                })
            self.soul_curse_set_publisher_status(f"publish_{parsed.get('status')}", detail, None, text, identity=identity)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "publish"
            self.soul_curse_set_publisher_status("publish_cooldown", f"发布委托冷却 {wait}秒", wait, text, identity=identity)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "publish"
            self.soul_curse_set_publisher_status("publish_blocked", "发布委托暂不可用", wait, text, identity=identity)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["chain_stage"] = "publish"
        self.soul_curse_set_publisher_status("publish_unknown", "发布委托回执未识别" if text else "发布委托无回执", wait, text, identity=identity)
        return "unknown"

    def record_soul_curse_accept_response(self, identity, commission_id, text, profile=None):
        identity = self.soul_curse_resolve_identity(identity)
        profile = profile or self.soul_curse_assistant_profile() or {}
        parsed = parse_soul_curse_accept(text)
        owner_account = str(profile.get("owner_account") or "")
        state = self.get_soul_curse_assist_state(identity, owner_account)
        state["owner_account"] = profile.get("owner_account", state.get("owner_account", ""))
        state["target_username"] = _target_username(profile.get("target_username") or state.get("target_username"))
        state["commission_id"] = str(commission_id or state.get("commission_id") or "")
        if parsed.get("status") == "success":
            state["accepted_commission_id"] = state["commission_id"]
            state["last_accept_time"] = now_str()
            self.soul_curse_set_assist_status(
                identity, "accepted", f"已接取委托 {state['commission_id']}", 3, text,
                owner_account=owner_account,
            )
            return "success"
        if parsed.get("status") in {"gone", "blocked", "cooldown"}:
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            self.soul_curse_set_assist_status(
                identity, f"accept_{parsed.get('status')}", f"接取委托 {state['commission_id']} 失败", wait, text,
                owner_account=owner_account,
            )
            return parsed.get("status")
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        self.soul_curse_set_assist_status(
            identity, "accept_unknown", "接取委托回执未识别" if text else "接取委托无回执", wait, text,
            owner_account=owner_account,
        )
        return "unknown"

    def record_soul_curse_action_response(self, identity, action, text, commission_id="", profile=None):
        identity = self.soul_curse_resolve_identity(identity)
        profile = profile or self.soul_curse_assistant_profile() or {}
        parsed = parse_soul_curse_action(text, action)
        owner_account = str(profile.get("owner_account") or "")
        state = self.get_soul_curse_assist_state(identity, owner_account)
        commission_id = str(commission_id or state.get("commission_id") or "")
        state["commission_id"] = commission_id
        state["owner_account"] = profile.get("owner_account", state.get("owner_account", ""))
        state["target_username"] = _target_username(profile.get("target_username") or state.get("target_username"))
        key_map = {
            "identify": ("last_identify_time", "next_identify_time", "identify_commission_id", "辨认咒纹"),
            "suppress": ("last_suppress_time", "next_suppress_time", "suppress_commission_id", "借幡镇魂"),
            "strip": ("last_strip_time", "next_strip_time", "strip_commission_id", "剥离咒源"),
        }
        last_key, next_key, id_key, label = key_map[action]
        now = now_str()
        if parsed.get("status") == "success":
            state[last_key] = now
            state[id_key] = commission_id
            if action == "strip":
                next_ready = add_seconds_str(now, SOUL_CURSE_CHAIN_SECONDS)
                state["completed_commission_id"] = commission_id
                state["last_completed_time"] = now
                state["next_chain_time"] = next_ready
                state["next_identify_time"] = next_ready
                state["next_suppress_time"] = next_ready
                state["next_strip_time"] = next_ready
                self.soul_curse_mark_publisher_commission_completed(profile.get("owner_account"), commission_id, now)
                self.soul_curse_set_assist_status(
                    identity, "completed", f"{label}完成，委托 {commission_id}", SOUL_CURSE_CHAIN_SECONDS, text,
                    owner_account=owner_account,
                )
            else:
                state[next_key] = add_seconds_str(now, int(parsed.get("cooldown_seconds") or SOUL_CURSE_CHAIN_SECONDS))
                self.soul_curse_set_assist_status(
                    identity, f"{action}_success", f"{label}完成，委托 {commission_id}", 3, text,
                    owner_account=owner_account,
                )
            return "success"
        if parsed.get("status") == "sha_not_enough":
            state[next_key] = ""
            self.soul_curse_set_assist_status(
                identity, f"{action}_sha_not_enough", f"{label}煞气不足，准备化功为煞", 5, text,
                owner_account=owner_account,
            )
            return "sha_not_enough"
        if parsed.get("status") in {"cooldown", "source_not_ready", "no_contract", "blocked"}:
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state[next_key] = add_seconds_str(now, wait)
            self.soul_curse_set_assist_status(
                identity, f"{action}_{parsed.get('status')}", f"{label}暂不可用，{wait}秒后再试", wait, text,
                owner_account=owner_account,
            )
            return parsed.get("status")
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        self.soul_curse_set_assist_status(
            identity, f"{action}_unknown", f"{label}回执未识别" if text else f"{label}无回执", wait, text,
            owner_account=owner_account,
        )
        return "unknown"

    def soul_curse_yinluo_convert_wait_seconds(self, identity, default_seconds=SOUL_CURSE_UNKNOWN_RETRY_SECONDS):
        if not hasattr(self, "get_yinluo_state"):
            return int(default_seconds)
        try:
            state = self.get_yinluo_state(identity)
            due_at = state.get("next_action_at", "")
            if due_at and is_future(due_at):
                return max(5, int(seconds_until(due_at)))
        except Exception:
            pass
        return int(default_seconds)

    async def soul_curse_replenish_sha_for_action(self, identity, action, label, response_text, profile=None):
        profile = profile or self.soul_curse_assistant_profile() or {}
        owner_account = str(profile.get("owner_account") or "")
        if not hasattr(self, "yinluo_convert_sha"):
            self.soul_curse_set_assist_status(
                identity,
                f"{action}_sha_not_enough",
                f"{label}煞气不足，但当前脚本没有化功为煞接口",
                SOUL_CURSE_UNKNOWN_RETRY_SECONDS,
                response_text,
                owner_account=owner_account,
            )
            return False

        try:
            yinluo_state = self.get_yinluo_state(identity) if hasattr(self, "get_yinluo_state") else {}
        except Exception:
            yinluo_state = {}
        if str(yinluo_state.get("last_status") or "").startswith("convert_") and is_future(yinluo_state.get("next_action_at", "")):
            wait = self.soul_curse_yinluo_convert_wait_seconds(identity)
            self.soul_curse_set_assist_status(
                identity,
                f"{action}_sha_convert_wait",
                f"{label}煞气不足，化功为煞仍在冷却/结算中，{wait}秒后重试",
                wait,
                response_text,
                owner_account=owner_account,
            )
            return False

        if self.soul_curse_command_paused(YINLUO_CONVERT_COMMAND, identity):
            self.soul_curse_set_assist_status(
                identity, f"{action}_sha_convert_paused", "化功为煞已暂停", 300, response_text,
                owner_account=owner_account,
            )
            return False

        self.soul_curse_set_assist_status(
            identity, f"{action}_sha_converting", f"{label}煞气不足，正在化功为煞", None, response_text,
            owner_account=owner_account,
        )
        converted = await self.yinluo_convert_sha(identity)
        try:
            yinluo_state = self.get_yinluo_state(identity) if hasattr(self, "get_yinluo_state") else {}
        except Exception:
            yinluo_state = {}
        convert_status = str(yinluo_state.get("last_status") or "")
        if converted and convert_status == "converted":
            state = self.get_soul_curse_assist_state(identity, owner_account)
            state["next_action_at"] = ""
            state["last_detail"] = f"{label}煞气不足，已化功为煞，立即重试"
            self.save_state()
            return True

        wait = self.soul_curse_yinluo_convert_wait_seconds(
            identity,
            60 if convert_status == "convert_pending" else SOUL_CURSE_UNKNOWN_RETRY_SECONDS,
        )
        self.soul_curse_set_assist_status(
            identity,
            f"{action}_{convert_status or 'sha_convert_failed'}",
            f"{label}煞气不足，化功为煞未完成，{wait}秒后重试",
            wait,
            response_text,
            owner_account=owner_account,
        )
        return False

    def soul_curse_main_extra_enabled(self, profile, key):
        return bool(profile and profile.get(key))

    async def soul_curse_maybe_wanying_greeting(self, profile):
        if not self.soul_curse_main_extra_enabled(profile, "wanying_greeting_enabled"):
            state = self.get_soul_curse_state()
            changed = False
            for key in (
                "last_wanying_greeting_date",
                "next_wanying_greeting_time",
                "last_wanying_greeting_time",
            ):
                if state.get(key):
                    state[key] = ""
                    changed = True
            if str(state.get("last_status") or "").startswith("wanying_greeting_"):
                state["last_status"] = ""
                state["last_detail"] = ""
                state["next_action_at"] = ""
                changed = True
            if changed:
                self.save_state()
            return 600
        state = self.get_soul_curse_state()
        if state.get("last_wanying_greeting_date") == _today():
            state["next_wanying_greeting_time"] = _next_daily_time(
                done_today=True,
                minute=int(profile.get("wanying_minute") or 5),
            )
            self.save_state()
            return seconds_until(state["next_wanying_greeting_time"])
        if self.soul_curse_command_paused(SOUL_CURSE_WANYING_GREETING_COMMAND, "主魂"):
            return 300
        next_time = state.get("next_wanying_greeting_time", "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)
        if self.identity_pause_seconds("主魂") > 0:
            return 300
        async with self.soul_curse_atomic_task("SoulCurseWanyingGreeting"):
            resp = await self.soul_curse_send_main(SOUL_CURSE_WANYING_GREETING_COMMAND, timeout=60)
            self.record_soul_curse_wanying_greeting_response(self.soul_curse_response_text(resp), profile)
        return 5

    async def soul_curse_maybe_moon_meditation(self, profile):
        state = self.get_soul_curse_state()
        if not self.soul_curse_main_extra_enabled(profile, "moon_meditation_enabled"):
            changed = False
            for key in (
                "last_moon_meditation_date",
                "next_moon_meditation_time",
                "last_moon_meditation_time",
            ):
                if state.get(key):
                    state[key] = ""
                    changed = True
            if str(state.get("last_status") or "").startswith("moon_meditation_"):
                state["last_status"] = ""
                state["last_detail"] = ""
                state["next_action_at"] = ""
                changed = True
            if changed:
                self.save_state()
            return 600
        if self.soul_curse_command_paused(SOUL_CURSE_MOON_MEDITATION_COMMAND, "主魂"):
            return 300
        next_time = state.get("next_moon_meditation_time", "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)
        if self.identity_pause_seconds("主魂") > 0:
            return 300
        async with self.soul_curse_atomic_task("SoulCurseMoonMeditation"):
            resp = await self.soul_curse_send_main(SOUL_CURSE_MOON_MEDITATION_COMMAND, timeout=60)
            self.record_soul_curse_moon_meditation_response(self.soul_curse_response_text(resp), profile)
        return 5

    async def soul_curse_main_extra_tick(self, profile):
        wait = await self.soul_curse_maybe_wanying_greeting(profile)
        moon_wait = await self.soul_curse_maybe_moon_meditation(profile)
        return min(wait, moon_wait)

    async def soul_curse_maybe_visit(self, profile):
        state = self.get_soul_curse_state()
        if state.get("last_visit_date") == _today():
            state["next_visit_time"] = _next_visit_time(done_today=True, minute=profile.get("visit_minute", 0))
            self.save_state()
            return seconds_until(state["next_visit_time"])
        if self.soul_curse_command_paused(SOUL_CURSE_VISIT_COMMAND, "主魂"):
            return 300
        next_visit = state.get("next_visit_time", "")
        if next_visit and is_future(next_visit):
            return seconds_until(next_visit)
        now = datetime.now()
        target = now.replace(
            hour=SOUL_CURSE_VISIT_HOUR,
            minute=int(profile.get("visit_minute") or 0),
            second=0,
            microsecond=0,
        )
        if now < target:
            state["next_visit_time"] = dt_to_str(target)
            self.save_state()
            return seconds_until(state["next_visit_time"])
        async with self.soul_curse_atomic_task("SoulCurseVisit"):
            resp = await self.soul_curse_send_main(SOUL_CURSE_VISIT_COMMAND, timeout=60)
            self.record_soul_curse_visit_response(self.soul_curse_response_text(resp), profile)
        return 5

    async def soul_curse_run_publisher_chain(self, profile):
        state = self.get_soul_curse_state()
        if is_future(state.get("next_action_at", "")):
            return seconds_until(state.get("next_action_at", ""))
        if state.get("chain_stage", "") in {"", "done"} and is_future(state.get("next_chain_time", "")):
            return seconds_until(state.get("next_chain_time", ""))
        if self.identity_pause_seconds("主魂") > 0:
            return 300

        async with self.soul_curse_atomic_task("SoulCurseChain"):
            stage = state.get("chain_stage") or "infer"
            if stage == "infer":
                if is_future(state.get("next_infer_time", "")):
                    return seconds_until(state.get("next_infer_time", ""))
                if self.soul_curse_command_paused(SOUL_CURSE_INFER_COMMAND, "主魂"):
                    return 300
                resp = await self.soul_curse_send_main(SOUL_CURSE_INFER_COMMAND, timeout=70)
                if self.record_soul_curse_infer_response(self.soul_curse_response_text(resp)) != "success":
                    return 5
                await asyncio.sleep(3)
                state = self.get_soul_curse_state()

            if state.get("chain_stage") == "protect":
                if is_future(state.get("next_protect_time", "")):
                    return seconds_until(state.get("next_protect_time", ""))
                if self.soul_curse_command_paused(SOUL_CURSE_PROTECT_COMMAND, "主魂"):
                    return 300
                resp = await self.soul_curse_send_main(SOUL_CURSE_PROTECT_COMMAND, timeout=70)
                if self.record_soul_curse_protect_response(self.soul_curse_response_text(resp)) != "success":
                    return 5
                await asyncio.sleep(3)
                state = self.get_soul_curse_state()

            if state.get("chain_stage") == "publish":
                if self.soul_curse_command_paused(SOUL_CURSE_PUBLISH_COMMAND, "主魂"):
                    return 300
                resp = await self.soul_curse_send_main(SOUL_CURSE_PUBLISH_COMMAND, timeout=70)
                if self.record_soul_curse_publish_response(self.soul_curse_response_text(resp), profile) != "success":
                    return 5
                state = self.get_soul_curse_state()
                if not profile.get("shared") and state.get("commission_id"):
                    commission = {
                        "owner_account": profile.get("owner_account"),
                        "commission_id": state.get("commission_id"),
                        "target_username": profile.get("target_username"),
                        "assistant_identity": profile.get("assistant_identity") or YINLUO_IDENTITY,
                    }
                    await self.soul_curse_process_assist_commission(commission, source="local")
        return 5

    async def soul_curse_process_assist_commission(self, commission, source="shared"):
        if not isinstance(commission, dict):
            return 600
        requested_identity = str(
            commission.get("assistant_identity") or self.soul_curse_yinluo_identity() or ""
        ).strip()
        # Accounts without an Yinluo avatar (for example xiaohao/waaiging)
        # must never fall back to ``主魂`` and execute assistant actions.
        if not requested_identity or requested_identity == "主魂":
            return 600
        identity = self.soul_curse_resolve_identity(requested_identity)
        known_avatars = {
            str(name).strip()
            for name in (getattr(self, "avatars", []) or [])
            if str(name).strip()
        }
        actor_state = getattr(self, "state", {}) or {}
        if isinstance(actor_state, dict) and isinstance(actor_state.get("avatars"), dict):
            known_avatars.update(str(name).strip() for name in actor_state["avatars"] if str(name).strip())
        if identity not in known_avatars:
            return 3600
        commission_id = str(commission.get("commission_id") or "").strip()
        if not commission_id:
            return 600
        profile = {
            "owner_account": commission.get("owner_account") or "",
            "target_username": _target_username(commission.get("target_username") or ""),
            "assistant_identity": identity,
            "shared": source == "shared",
        }
        owner_account = str(profile.get("owner_account") or "")
        state = self.get_soul_curse_assist_state(identity, owner_account)
        if is_future(state.get("next_action_at", "")):
            return seconds_until(state.get("next_action_at", ""))
        if self.identity_pause_seconds(identity) > 0:
            return 300

        async with self.soul_curse_atomic_task(f"SoulCurseAssist-{identity}"):
            state = self.get_soul_curse_assist_state(identity, owner_account)
            state["owner_account"] = profile.get("owner_account")
            state["target_username"] = profile.get("target_username")
            state["commission_id"] = commission_id
            self.save_state()

            if state.get("accepted_commission_id") != commission_id:
                command = f"{SOUL_CURSE_ACCEPT_COMMAND} {commission_id}"
                if self.soul_curse_command_paused(command, identity):
                    return 300
                resp = await self.soul_curse_send_identity(identity, command, timeout=70)
                result = self.record_soul_curse_accept_response(identity, commission_id, self.soul_curse_response_text(resp), profile)
                if result != "success":
                    self.soul_curse_update_shared_from_assist(profile, result)
                    return 5
                self.soul_curse_update_shared_from_assist(profile, "accepted")
                await asyncio.sleep(3)

            target = _target_username(profile.get("target_username"))
            for action, base_command, id_key, next_key in (
                ("identify", SOUL_CURSE_IDENTIFY_COMMAND, "identify_commission_id", "next_identify_time"),
                ("suppress", SOUL_CURSE_SUPPRESS_COMMAND, "suppress_commission_id", "next_suppress_time"),
                ("strip", SOUL_CURSE_STRIP_COMMAND, "strip_commission_id", "next_strip_time"),
            ):
                state = self.get_soul_curse_assist_state(identity, owner_account)
                if state.get(id_key) == commission_id:
                    continue
                if is_future(state.get(next_key, "")):
                    return seconds_until(state.get(next_key, ""))
                command = f"{base_command} {target}"
                if self.soul_curse_command_paused(command, identity):
                    return 300
                resp = await self.soul_curse_send_identity(identity, command, timeout=80)
                resp_text = self.soul_curse_response_text(resp)
                parsed = parse_soul_curse_action(resp_text, action)
                if parsed.get("status") == "sha_not_enough":
                    label = {
                        "identify": "辨认咒纹",
                        "suppress": "借幡镇魂",
                        "strip": "剥离咒源",
                    }[action]
                    if await self.soul_curse_replenish_sha_for_action(
                        identity,
                        action,
                        label,
                        resp_text,
                        profile,
                    ):
                        await asyncio.sleep(3)
                        resp = await self.soul_curse_send_identity(identity, command, timeout=80)
                        result = self.record_soul_curse_action_response(
                            identity,
                            action,
                            self.soul_curse_response_text(resp),
                            commission_id,
                            profile,
                        )
                    else:
                        result = "sha_not_enough"
                else:
                    result = self.record_soul_curse_action_response(identity, action, resp_text, commission_id, profile)
                self.soul_curse_update_shared_from_assist(profile, "completed" if action == "strip" and result == "success" else result)
                if result != "success":
                    return 5
                await asyncio.sleep(3)
        return 5

    def soul_curse_update_shared_from_assist(self, profile, status):
        owner = profile.get("owner_account")
        if not profile.get("shared"):
            return
        identity = self.soul_curse_resolve_identity(
            profile.get("assistant_identity") or self.soul_curse_yinluo_identity()
        )
        state = self.get_soul_curse_assist_state(identity, owner)
        upsert_soul_curse_shared_commission(owner, {
            "commission_id": state.get("commission_id", ""),
            "target_username": state.get("target_username", ""),
            "assistant_account": self.soul_curse_account_key(),
            "assistant_identity": identity,
            "status": status,
            "last_detail": state.get("last_detail", ""),
            "next_action_at": state.get("next_action_at", ""),
        })

    def soul_curse_sync_shared_publisher_status(self, profile):
        if not profile.get("shared"):
            return
        owner = profile.get("owner_account")
        item = read_soul_curse_shared_state().get(owner, {})
        if not isinstance(item, dict):
            return
        state = self.get_soul_curse_state()
        if not item.get("commission_id") or str(item.get("commission_id")) != str(state.get("commission_id") or ""):
            return
        status = str(item.get("status") or "")
        if not status:
            return
        if status == state.get("commission_status") and not (
            status == "completed" and not state.get("last_chain_time")
        ):
            return
        state["commission_status"] = status
        state["commission_updated_at"] = item.get("updated_at") or now_str()
        if status == "completed":
            completed_at = item.get("updated_at") or now_str()
            next_ready = add_seconds_str(completed_at, SOUL_CURSE_CHAIN_SECONDS)
            state["last_chain_time"] = completed_at
            state["next_chain_time"] = next_ready
            state["next_action_at"] = next_ready
            state["chain_stage"] = ""
        if status in {"gone", "no_contract", "blocked"}:
            retry_at = add_seconds_str(now_str(), SOUL_CURSE_UNKNOWN_RETRY_SECONDS)
            state["next_chain_time"] = retry_at
            state["next_action_at"] = retry_at
            state["chain_stage"] = "publish"
        self.save_state()

    async def soul_curse_shared_assist_tick(self, profile):
        profile = dict(profile or {})
        owner = profile.get("owner_account")
        # 化身 publisher 发布的委托 key 为 "account:identity"，主魂为 "account"。
        # assist 池遍历该账号所有 key，逐个检查是否有待接委托。
        data = read_soul_curse_shared_state()
        owner_keys = [key for key in data if str(key) == str(owner) or str(key).startswith(f"{owner}:")]
        owner_keys.sort()  # 主魂 key（无冒号）优先
        item = None
        terminal_statuses = {"completed", "gone", "blocked", "no_contract"}
        for key in owner_keys:
            candidate = data.get(key)
            if (
                isinstance(candidate, dict)
                and candidate.get("commission_id")
                and str(candidate.get("status") or "") not in terminal_statuses
            ):
                item = candidate
                item["_owner_key"] = key
                break
        if not isinstance(item, dict) or not item.get("commission_id"):
            return 600
        status = str(item.get("status") or "")
        if status in {"completed", "gone", "blocked", "no_contract"}:
            return 600
        configured_identity = profile.get("assistant_identity")
        if not configured_identity:
            yinluo_identity = getattr(self, "soul_curse_yinluo_identity", None)
            configured_identity = yinluo_identity() if callable(yinluo_identity) else YINLUO_IDENTITY
        if not str(configured_identity or "").strip():
            return 600
        resolver = getattr(self, "soul_curse_resolve_identity", None)
        identity = resolver(configured_identity) if callable(resolver) else str(configured_identity).strip()
        profile["assistant_identity"] = identity
        if item.get("assistant_account") and item.get("assistant_account") != self.soul_curse_account_key():
            # 已被其他阴罗身份认领——它冷却中就等它，不抢。
            other_next = str(item.get("next_action_at") or "")
            if other_next and is_future(other_next):
                return max(30, min(int(seconds_until(other_next)), 3600))
            return 600
        # 未认领（assistant_account 为空）：本身份链路冷却就绪才能认领。
        # 冷却未到的时间戳以本身份 assist 状态为准（认领前共享池无归属）。
        if not item.get("assistant_account"):
            owner_key = str(item.get("_owner_key") or owner or "").strip()
            state = self.get_soul_curse_assist_state(identity, owner_key)
            if is_future(state.get("next_action_at", "")):
                return max(30, min(int(seconds_until(state.get("next_action_at", ""))), 3600))
            if self.identity_pause_seconds(identity) > 0:
                return 300
            # 认领：原子写入自己账号，抢到即锁定
            upsert_soul_curse_shared_commission(owner_key, {
                "assistant_account": self.soul_curse_account_key(),
                "assistant_identity": identity,
                "claimed_at": now_str(),
            })
            item = read_soul_curse_shared_state().get(owner_key, {})
            if not isinstance(item, dict) or (
                item.get("assistant_account") and item.get("assistant_account") != self.soul_curse_account_key()
            ):
                # 并发竞争中没抢到
                return 600
        item.setdefault("owner_account", item.get("_owner_key") or owner)
        item.setdefault("target_username", profile.get("target_username"))
        item.setdefault("assistant_identity", identity)
        return await self.soul_curse_process_assist_commission(item, source="shared")

    def soul_curse_avatar_publisher_profiles(self):
        """本账号化身 publisher 候选名单（settings 未启用的会被 tick 过滤）。"""
        return list(SOUL_CURSE_AVATAR_PUBLISHERS.get(self.soul_curse_account_key(), ()))

    def soul_curse_avatar_publisher_profile(self, identity):
        for profile in self.soul_curse_avatar_publisher_profiles():
            if str(profile.get("identity") or "") == str(identity or ""):
                return profile
        return None

    async def soul_curse_avatar_publishers_tick(self, waits):
        """所有化身 publisher 链的统一 tick。

        返回 None 表示正常继续外层（wait 已并入 waits）；
        返回数字表示需要立即以该间隔重试。
        """
        avatars = list(getattr(self, "avatars", []) or [])
        profiles = self.soul_curse_avatar_publisher_profiles()
        for index, profile in enumerate(profiles):
            identity = str(profile.get("identity") or "")
            if identity not in avatars:
                continue
            if not self.soul_curse_identity_enabled(identity=identity):
                continue
            if self.identity_pause_seconds(identity) > 0:
                waits.append(300)
                continue
            try:
                wait = await self.soul_curse_avatar_publisher_tick(profile, index)
            except Exception as exc:
                log = self.soul_curse_logger()
                log.error(f"Soul curse avatar [{identity}] tick error: {exc}", exc_info=True)
                waits.append(SOUL_CURSE_RETRY_SECONDS)
                continue
            if wait <= 10:
                return max(5, wait)
            waits.append(wait)
        return None

    async def soul_curse_avatar_publisher_tick(self, profile, index=0):
        """单个化身 publisher 链 tick：探望→推演→护持→发布（按 avatar state 独立状态机）。"""
        identity = str(profile.get("identity") or "")
        account = self.soul_curse_account_key()
        owner_key = f"{account}:{identity}"
        chain_profile = {
            "owner_account": account,
            "owner_key": owner_key,
            "target_username": _target_username(profile.get("target_username")),
            "shared": True,
            "visit_minute": 10 + int(index) * 3,  # 化身间错开探望分钟
        }
        state = self.get_soul_curse_state(identity)
        # 1) 探望（每日一次）
        visit_wait = await self.soul_curse_avatar_maybe_visit(chain_profile, identity)
        if visit_wait <= 10:
            return visit_wait
        # 2) 链冷却 / 完成同步
        shared_item = read_soul_curse_shared_state().get(owner_key, {})
        if isinstance(shared_item, dict) and shared_item.get("commission_id"):
            status = str(shared_item.get("status") or "")
            if status == "completed" and state.get("commission_status") != "completed":
                completed_at = shared_item.get("updated_at") or now_str()
                next_ready = add_seconds_str(completed_at, SOUL_CURSE_CHAIN_SECONDS)
                state["commission_status"] = "completed"
                state["last_chain_time"] = completed_at
                state["next_chain_time"] = next_ready
                state["next_action_at"] = next_ready
                state["chain_stage"] = ""
                self.save_state()
        # 3) 链执行（infer→protect→publish 状态机，发送走身份切换）
        chain_wait = await self.soul_curse_run_avatar_publisher_chain(chain_profile, identity)
        if chain_wait <= 10:
            return chain_wait
        wait_list = [visit_wait, chain_wait]
        return max(60, min(int(min(wait_list)), 3600))

    async def soul_curse_avatar_maybe_visit(self, chain_profile, identity):
        """化身探望：每日 9 点档（分钟按身份错开），冷却与成功解析复用主魂逻辑。"""
        state = self.get_soul_curse_state(identity)
        if state.get("last_visit_date") == _today():
            state["next_visit_time"] = _next_visit_time(done_today=True, minute=chain_profile.get("visit_minute", 0))
            self.save_state()
            return seconds_until(state["next_visit_time"])
        if self.soul_curse_command_paused(SOUL_CURSE_VISIT_COMMAND, identity):
            return 300
        next_visit = state.get("next_visit_time", "")
        if next_visit and is_future(next_visit):
            return seconds_until(next_visit)
        now = datetime.now()
        target = now.replace(
            hour=SOUL_CURSE_VISIT_HOUR,
            minute=int(chain_profile.get("visit_minute") or 0),
            second=0,
            microsecond=0,
        )
        if now < target:
            state["next_visit_time"] = dt_to_str(target)
            self.save_state()
            return seconds_until(state["next_visit_time"])
        async with self.soul_curse_atomic_task(f"SoulCurseVisit-{identity}"):
            resp = await self.soul_curse_send_identity(identity, SOUL_CURSE_VISIT_COMMAND, timeout=60)
            self.record_soul_curse_visit_response(self.soul_curse_response_text(resp), chain_profile, identity=identity)
        return 5

    async def soul_curse_run_avatar_publisher_chain(self, chain_profile, identity):
        """化身链状态机：infer→protect→publish（与主魂链同构，状态存 avatar state）。"""
        state = self.get_soul_curse_state(identity)
        if is_future(state.get("next_action_at", "")):
            return seconds_until(state.get("next_action_at", ""))
        if state.get("chain_stage", "") in {"", "done"} and is_future(state.get("next_chain_time", "")):
            return seconds_until(state.get("next_chain_time", ""))
        if self.identity_pause_seconds(identity) > 0:
            return 300

        async with self.soul_curse_atomic_task(f"SoulCurseChain-{identity}"):
            stage = state.get("chain_stage") or "infer"
            if stage == "infer":
                if is_future(state.get("next_infer_time", "")):
                    return seconds_until(state.get("next_infer_time", ""))
                if self.soul_curse_command_paused(SOUL_CURSE_INFER_COMMAND, identity):
                    return 300
                resp = await self.soul_curse_send_identity(identity, SOUL_CURSE_INFER_COMMAND, timeout=70)
                if self.record_soul_curse_infer_response(self.soul_curse_response_text(resp), identity=identity) != "success":
                    return 5
                await asyncio.sleep(3)
                state = self.get_soul_curse_state(identity)

            if state.get("chain_stage") == "protect":
                if is_future(state.get("next_protect_time", "")):
                    return seconds_until(state.get("next_protect_time", ""))
                if self.soul_curse_command_paused(SOUL_CURSE_PROTECT_COMMAND, identity):
                    return 300
                resp = await self.soul_curse_send_identity(identity, SOUL_CURSE_PROTECT_COMMAND, timeout=70)
                if self.record_soul_curse_protect_response(self.soul_curse_response_text(resp), identity=identity) != "success":
                    return 5
                await asyncio.sleep(3)
                state = self.get_soul_curse_state(identity)

            if state.get("chain_stage") == "publish":
                if self.soul_curse_command_paused(SOUL_CURSE_PUBLISH_COMMAND, identity):
                    return 300
                resp = await self.soul_curse_send_identity(identity, SOUL_CURSE_PUBLISH_COMMAND, timeout=70)
                if self.record_soul_curse_publish_response(self.soul_curse_response_text(resp), chain_profile, identity=identity) != "success":
                    return 5
        return 5

    async def soul_curse_tick(self):
        waits = [600]
        publisher = self.soul_curse_publisher_profile()
        shared_assistants = self.soul_curse_shared_assistant_profiles()
        # Prefer this account's own newly-published commission.  External
        # commissions remain available, but must not hide the local one behind
        # an unrelated short retry.
        account = self.soul_curse_account_key()
        shared_assistants.sort(
            key=lambda item: 0 if str(item.get("owner_account") or "") == account else 1
        )

        if publisher and not self.soul_curse_identity_enabled(identity="主魂"):
            log = self.soul_curse_logger()
            log.info("Soul curse publisher disabled for [%s] by settings.", self.soul_curse_account_key())
            publisher = None

        if publisher:
            publisher_short_wait = False
            self.soul_curse_sync_shared_publisher_status(publisher)
            visit_wait = await self.soul_curse_maybe_visit(publisher)
            if visit_wait <= 10:
                waits.append(max(5, int(visit_wait)))
                publisher_short_wait = True
            else:
                waits.append(visit_wait)

            if not publisher_short_wait:
                extra_wait = await self.soul_curse_main_extra_tick(publisher)
                if extra_wait <= 10:
                    waits.append(max(5, int(extra_wait)))
                    publisher_short_wait = True
                else:
                    waits.append(extra_wait)

            local_commission_pending = False
            if not publisher_short_wait:
                state = self.get_soul_curse_state()
                if not publisher.get("shared") and state.get("commission_id") and state.get("commission_status") in {"success", "existing"}:
                    assistant_identity = self.soul_curse_yinluo_identity()
                    assist_state = self.get_soul_curse_assist_state(
                        assistant_identity,
                        publisher.get("owner_account"),
                    )
                    if assist_state.get("strip_commission_id") != state.get("commission_id"):
                        wait = await self.soul_curse_process_assist_commission({
                            "owner_account": publisher.get("owner_account"),
                            "commission_id": state.get("commission_id"),
                            "target_username": publisher.get("target_username"),
                            "assistant_identity": assistant_identity,
                        }, source="local")
                        waits.append(max(5, int(wait)) if wait <= 10 else wait)
                        local_commission_pending = True
                        if wait <= 10:
                            publisher_short_wait = True
                    else:
                        self.soul_curse_mark_publisher_commission_completed(
                            publisher.get("owner_account"),
                            state.get("commission_id"),
                            assist_state.get("last_completed_time") or assist_state.get("last_strip_time") or now_str(),
                        )

            if not publisher_short_wait:
                state = self.get_soul_curse_state()
                if publisher.get("shared") and state.get("commission_id") and state.get("commission_status") not in {
                    "completed", "gone", "blocked", "no_contract",
                }:
                    wait = seconds_until(state.get("next_action_at", "")) if is_future(state.get("next_action_at", "")) else 600
                    waits.append(wait)
                    # The publisher is waiting on the commission lifecycle,
                    # but the assistant must still be allowed to claim it in
                    # this same scheduler tick.
                    publisher_short_wait = True

            if not publisher_short_wait and not local_commission_pending:
                chain_wait = await self.soul_curse_run_publisher_chain(publisher)
                waits.append(max(5, int(chain_wait)) if chain_wait <= 10 else chain_wait)

        for assistant in shared_assistants:
            # gate 按执行方（本账号的阴罗身份）判断，不是按委托来源账号——
            # settings 的开关语义是"main.缘生子 是否干活"，不是"waaiging.缘生子"。
            assistant = dict(assistant)
            assistant["assistant_identity"] = self.soul_curse_resolve_identity(
                assistant.get("assistant_identity") or self.soul_curse_yinluo_identity()
            )
            if not self.soul_curse_identity_enabled(
                identity=assistant.get("assistant_identity") or self.soul_curse_yinluo_identity(),
            ):
                continue
            wait = await self.soul_curse_shared_assist_tick(assistant)
            if wait <= 10:
                return max(5, wait)
            waits.append(wait)

        # 化身 publisher 链：每个启用的化身独立跑探望→推演→护持→发布。
        avatar_waits = await self.soul_curse_avatar_publishers_tick(waits)
        if avatar_waits is not None:
            return avatar_waits

        return max(30, min(int(min(waits or [600])), 3600))

    async def run_soul_curse_loop(self, initial_delay=0, sleep_func=None):
        await self.startup_done.wait()
        self.get_soul_curse_state()
        for profile in self.soul_curse_assistant_profiles():
            self.get_soul_curse_assist_state(
                profile.get("assistant_identity") or YINLUO_IDENTITY,
                profile.get("owner_account"),
            )
        self.save_state()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        log = self.soul_curse_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                wait_seconds = await self.soul_curse_tick()
                log.info(f"Soul curse loop sleeping {int(wait_seconds)}s.")
                if hasattr(self, "common_scheduler_sleep_seconds"):
                    sleep_seconds = self.common_scheduler_sleep_seconds(
                        wait_seconds + random.randint(5, 30),
                        minimum=30,
                        sleep_func=sleep_func,
                    )
                else:
                    sleep_seconds = max(30, min(int(wait_seconds), 600))
                await asyncio.sleep(sleep_seconds)
            except Exception as exc:
                log.error(f"Soul curse loop error: {exc}", exc_info=True)
                await asyncio.sleep(SOUL_CURSE_RETRY_SECONDS)

    def record_soul_curse_manual_response(self, command, text, identity="主魂"):
        cmd = str(command or "").strip()
        identity = self.soul_curse_resolve_identity(identity)
        publisher = self.soul_curse_publisher_profile()
        if cmd == SOUL_CURSE_VISIT_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_visit_response(text, publisher)
        if cmd == SOUL_CURSE_WANYING_GREETING_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_wanying_greeting_response(text, publisher)
        if cmd == SOUL_CURSE_MOON_MEDITATION_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_moon_meditation_response(text, publisher)
        if cmd == SOUL_CURSE_INFER_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_infer_response(text) in {"success", "cooldown", "blocked"}
        if cmd == SOUL_CURSE_PROTECT_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_protect_response(text) in {"success", "cooldown", "blocked"}
        if cmd == SOUL_CURSE_CO_STUDY_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_co_study_response(text) in {"success", "cooldown", "blocked"}
        if cmd.startswith(".发布解咒委托") and identity == "主魂" and publisher:
            return self.record_soul_curse_publish_response(text, publisher) in {"success", "cooldown", "blocked"}

        assistant = self.soul_curse_assistant_profile_for_command(identity, cmd)
        if identity == self.soul_curse_yinluo_identity() and assistant:
            commission_id = ""
            match = re.search(r"\.接取解咒委托\s+(\d+)", cmd)
            if match:
                commission_id = match.group(1)
                return self.record_soul_curse_accept_response(identity, commission_id, text, assistant) in {"success", "gone", "blocked", "cooldown"}
            state = self.get_soul_curse_assist_state(
                identity,
                assistant.get("owner_account"),
            )
            commission_id = state.get("commission_id", "")
            action = ""
            if cmd.startswith(SOUL_CURSE_IDENTIFY_COMMAND):
                action = "identify"
            elif cmd.startswith(SOUL_CURSE_SUPPRESS_COMMAND):
                action = "suppress"
            elif cmd.startswith(SOUL_CURSE_STRIP_COMMAND):
                action = "strip"
            if action:
                return self.record_soul_curse_action_response(identity, action, text, commission_id, assistant) in {
                    "success", "cooldown", "source_not_ready", "no_contract", "blocked", "sha_not_enough",
                }
        return False
