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

from automation_settings import SUB_YINLUO_IDENTITY
from common_command_features import add_seconds_str, dt_to_str, is_future, now_str, seconds_until
from yinluo_features import YINLUO_CONVERT_COMMAND, YINLUO_IDENTITY


SOUL_CURSE_VISIT_COMMAND = ".探望南宫婉"
SOUL_CURSE_WANYING_GREETING_COMMAND = ".婉影问安"
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
        "shared": True,
    },
    "sub": {
        "owner_account": "sub",
        "target_username": "@Gamling33",
        "assistant_account": "",
        "assistant_identity": "",
        "visit_minute": 3,
        "shared": True,
    },
    "xiaohao": {
        "owner_account": "xiaohao",
        "target_username": "@TitanCreeper",
        "assistant_account": "",
        "assistant_identity": "",
        "visit_minute": 6,
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
    ),
    "sub": (
        {"owner_account": "main", "target_username": "@Weeguu", "assistant_identity": SUB_YINLUO_IDENTITY, "shared": True},
        {"owner_account": "xiaohao", "target_username": "@TitanCreeper", "assistant_identity": SUB_YINLUO_IDENTITY, "shared": True},
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
            return bool(entry.get(identity))
        if isinstance(entry, list):
            return identity in {str(item) for item in entry}
        return False

    def soul_curse_publisher_profile(self):
        return SOUL_CURSE_PUBLISHERS.get(self.soul_curse_account_key())

    def soul_curse_assistant_profile(self):
        return SOUL_CURSE_ASSISTANTS.get(self.soul_curse_account_key())

    def soul_curse_shared_assistant_profiles(self):
        """共享池竞争者监听所有账号的委托（含本账号）。"""
        profiles = list(SOUL_CURSE_SHARED_ASSISTANTS.get(self.soul_curse_account_key(), ()))
        publisher = SOUL_CURSE_PUBLISHERS.get(self.soul_curse_account_key())
        if publisher and publisher.get("shared"):
            own = {
                "owner_account": publisher.get("owner_account"),
                "target_username": publisher.get("target_username"),
                "assistant_identity": (
                    YINLUO_IDENTITY
                    if self.soul_curse_account_key() == "main"
                    else SUB_YINLUO_IDENTITY
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

    def get_soul_curse_state(self):
        state = self.state.setdefault("soul_curse", {})
        defaults = soul_curse_publisher_default_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        return state

    def get_soul_curse_assist_state(self, identity=YINLUO_IDENTITY, owner_account=""):
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

    def soul_curse_set_publisher_status(self, status, detail="", next_seconds=None, response=""):
        state = self.get_soul_curse_state()
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

    def record_soul_curse_visit_response(self, text, profile=None):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_visit(text)
        state = self.get_soul_curse_state()
        minute = int(profile.get("visit_minute") or 0)
        if parsed.get("status") in {"success", "done"}:
            state["last_visit_date"] = _today()
            state["next_visit_time"] = _next_visit_time(done_today=True, minute=minute)
            self.soul_curse_set_publisher_status(parsed.get("status"), "南宫婉探望已记录", None, text)
            return True
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_visit_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status("visit_cooldown", f"探望冷却 {wait}秒", wait, text)
            return True
        if not text:
            state["next_visit_time"] = add_seconds_str(now_str(), SOUL_CURSE_RETRY_SECONDS)
            self.soul_curse_set_publisher_status("visit_no_response", "探望无回执，稍后重试", SOUL_CURSE_RETRY_SECONDS)
            return False
        state["next_visit_time"] = add_seconds_str(now_str(), SOUL_CURSE_UNKNOWN_RETRY_SECONDS)
        self.soul_curse_set_publisher_status("visit_unknown", "探望回执未识别", SOUL_CURSE_UNKNOWN_RETRY_SECONDS, text)
        return False

    def record_soul_curse_wanying_greeting_response(self, text, profile=None):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_wanying_greeting(text)
        state = self.get_soul_curse_state()
        if parsed.get("status") in {"success", "done"}:
            now = now_str()
            state["last_wanying_greeting_date"] = _today()
            state["last_wanying_greeting_time"] = now
            state["next_wanying_greeting_time"] = _next_daily_time(
                done_today=True,
                minute=int(profile.get("wanying_minute") or 5),
            )
            self.soul_curse_set_publisher_status(parsed.get("status"), "婉影问安已记录", None, text)
            return True
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_wanying_greeting_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status("wanying_greeting_cooldown", f"婉影问安冷却 {wait}秒", wait, text)
            return True
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_wanying_greeting_time"] = add_seconds_str(now_str(), wait)
            self.soul_curse_set_publisher_status("wanying_greeting_blocked", "婉影问安暂不可用", wait, text)
            return True
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["next_wanying_greeting_time"] = add_seconds_str(now_str(), wait)
        self.soul_curse_set_publisher_status(
            "wanying_greeting_unknown",
            "婉影问安回执未识别" if text else "婉影问安无回执",
            wait,
            text,
        )
        return False

    def record_soul_curse_infer_response(self, text):
        parsed = parse_soul_curse_infer(text)
        state = self.get_soul_curse_state()
        now = now_str()
        if parsed.get("status") == "success":
            state["last_infer_time"] = now
            state["next_infer_time"] = add_seconds_str(now, SOUL_CURSE_CHAIN_SECONDS)
            state["chain_stage"] = "protect"
            state["next_action_at"] = ""
            self.soul_curse_set_publisher_status("infer_success", "封魂咒推演完成，准备护持神魂", 3, text)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_infer_time"] = add_seconds_str(now, wait)
            state["chain_stage"] = "infer"
            self.soul_curse_set_publisher_status("infer_cooldown", f"推演冷却 {wait}秒", wait, text)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "infer"
            self.soul_curse_set_publisher_status("infer_blocked", "推演暂不可用", wait, text)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["chain_stage"] = "infer"
        self.soul_curse_set_publisher_status("infer_unknown", "推演回执未识别" if text else "推演无回执", wait, text)
        return "unknown"

    def record_soul_curse_protect_response(self, text):
        parsed = parse_soul_curse_protect(text)
        state = self.get_soul_curse_state()
        now = now_str()
        if parsed.get("status") == "success":
            state["last_protect_time"] = now
            state["next_protect_time"] = add_seconds_str(now, SOUL_CURSE_CHAIN_SECONDS)
            state["chain_stage"] = "publish"
            state["next_action_at"] = ""
            self.soul_curse_set_publisher_status("protect_success", "护持神魂完成，准备发布委托", 3, text)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_protect_time"] = add_seconds_str(now, wait)
            state["chain_stage"] = "protect"
            self.soul_curse_set_publisher_status("protect_cooldown", f"护持冷却 {wait}秒", wait, text)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "protect"
            self.soul_curse_set_publisher_status("protect_blocked", "护持暂不可用", wait, text)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["chain_stage"] = "protect"
        self.soul_curse_set_publisher_status("protect_unknown", "护持回执未识别" if text else "护持无回执", wait, text)
        return "unknown"

    def record_soul_curse_co_study_response(self, text):
        parsed = parse_soul_curse_co_study(text)
        state = self.get_soul_curse_state()
        now = now_str()
        if parsed.get("status") == "success":
            state["last_co_study_time"] = now
            state["next_co_study_time"] = add_seconds_str(now, int(parsed.get("cooldown_seconds") or SOUL_CURSE_CHAIN_SECONDS))
            self.soul_curse_set_publisher_status("co_study_success", "同参封魂已记录", 3, text)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_co_study_time"] = add_seconds_str(now, wait)
            self.soul_curse_set_publisher_status("co_study_cooldown", f"同参封魂冷却 {wait}秒", wait, text)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["next_co_study_time"] = add_seconds_str(now, wait)
            self.soul_curse_set_publisher_status("co_study_blocked", "同参封魂暂不可用", wait, text)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["next_co_study_time"] = add_seconds_str(now, wait)
        self.soul_curse_set_publisher_status("co_study_unknown", "同参封魂回执未识别" if text else "同参封魂无回执", wait, text)
        return "unknown"

    def record_soul_curse_publish_response(self, text, profile=None):
        profile = profile or self.soul_curse_publisher_profile() or {}
        parsed = parse_soul_curse_publish(text)
        state = self.get_soul_curse_state()
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
                upsert_soul_curse_shared_commission(profile.get("owner_account"), {
                    "commission_id": commission_id,
                    "target_username": target,
                    # 认领留空：由就绪的阴罗身份在 assist tick 中按冷却竞争接取
                    "assistant_account": "",
                    "assistant_identity": "",
                    "status": "pending_accept",
                    "published_at": now,
                    "last_detail": detail,
                })
            self.soul_curse_set_publisher_status(f"publish_{parsed.get('status')}", detail, None, text)
            return "success"
        if parsed.get("status") == "cooldown":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "publish"
            self.soul_curse_set_publisher_status("publish_cooldown", f"发布委托冷却 {wait}秒", wait, text)
            return "cooldown"
        if parsed.get("status") == "blocked":
            wait = max(60, int(parsed.get("cooldown_seconds") or SOUL_CURSE_UNKNOWN_RETRY_SECONDS))
            state["chain_stage"] = "publish"
            self.soul_curse_set_publisher_status("publish_blocked", "发布委托暂不可用", wait, text)
            return "blocked"
        wait = SOUL_CURSE_RETRY_SECONDS if not text else SOUL_CURSE_UNKNOWN_RETRY_SECONDS
        state["chain_stage"] = "publish"
        self.soul_curse_set_publisher_status("publish_unknown", "发布委托回执未识别" if text else "发布委托无回执", wait, text)
        return "unknown"

    def record_soul_curse_accept_response(self, identity, commission_id, text, profile=None):
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

    async def soul_curse_main_extra_tick(self, profile):
        return await self.soul_curse_maybe_wanying_greeting(profile)

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
        identity = commission.get("assistant_identity") or YINLUO_IDENTITY
        if identity not in getattr(self, "avatars", []):
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
        identity = profile.get("assistant_identity") or YINLUO_IDENTITY
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
        owner = profile.get("owner_account")
        item = read_soul_curse_shared_state().get(owner, {})
        if not isinstance(item, dict) or not item.get("commission_id"):
            return 600
        status = str(item.get("status") or "")
        if status in {"completed", "gone", "blocked", "no_contract"}:
            return 600
        identity = profile.get("assistant_identity") or YINLUO_IDENTITY
        if item.get("assistant_account") and item.get("assistant_account") != self.soul_curse_account_key():
            # 已被其他阴罗身份认领——它冷却中就等它，不抢。
            other_next = str(item.get("next_action_at") or "")
            if other_next and is_future(other_next):
                return max(30, min(int(seconds_until(other_next)), 3600))
            return 600
        # 未认领（assistant_account 为空）：本身份链路冷却就绪才能认领。
        # 冷却未到的时间戳以本身份 assist 状态为准（认领前共享池无归属）。
        if not item.get("assistant_account"):
            state = self.get_soul_curse_assist_state(identity, owner)
            if is_future(state.get("next_action_at", "")):
                return max(30, min(int(seconds_until(state.get("next_action_at", ""))), 3600))
            if self.identity_pause_seconds(identity) > 0:
                return 300
            # 认领：原子写入自己账号，抢到即锁定
            upsert_soul_curse_shared_commission(owner, {
                "assistant_account": self.soul_curse_account_key(),
                "assistant_identity": identity,
                "claimed_at": now_str(),
            })
            item = read_soul_curse_shared_state().get(owner, {})
            if not isinstance(item, dict) or (
                item.get("assistant_account") and item.get("assistant_account") != self.soul_curse_account_key()
            ):
                # 并发竞争中没抢到
                return 600
        item.setdefault("owner_account", owner)
        item.setdefault("target_username", profile.get("target_username"))
        item.setdefault("assistant_identity", identity)
        return await self.soul_curse_process_assist_commission(item, source="shared")

    async def soul_curse_tick(self):
        waits = [600]
        publisher = self.soul_curse_publisher_profile()
        shared_assistants = self.soul_curse_shared_assistant_profiles()

        if publisher and not self.soul_curse_identity_enabled(identity="主魂"):
            log = self.soul_curse_logger()
            log.info("Soul curse publisher disabled for [%s] by settings.", self.soul_curse_account_key())
            publisher = None

        if publisher:
            self.soul_curse_sync_shared_publisher_status(publisher)
            visit_wait = await self.soul_curse_maybe_visit(publisher)
            if visit_wait <= 10:
                return max(5, visit_wait)
            waits.append(visit_wait)

            extra_wait = await self.soul_curse_main_extra_tick(publisher)
            if extra_wait <= 10:
                return max(5, extra_wait)
            waits.append(extra_wait)

            state = self.get_soul_curse_state()
            local_commission_pending = False
            if not publisher.get("shared") and state.get("commission_id") and state.get("commission_status") in {"success", "existing"}:
                assist_state = self.get_soul_curse_assist_state(
                    publisher.get("assistant_identity") or YINLUO_IDENTITY,
                    publisher.get("owner_account"),
                )
                if assist_state.get("strip_commission_id") != state.get("commission_id"):
                    wait = await self.soul_curse_process_assist_commission({
                        "owner_account": publisher.get("owner_account"),
                        "commission_id": state.get("commission_id"),
                        "target_username": publisher.get("target_username"),
                        "assistant_identity": publisher.get("assistant_identity") or YINLUO_IDENTITY,
                    }, source="local")
                    if wait <= 10:
                        return max(5, wait)
                    waits.append(wait)
                    local_commission_pending = True
                else:
                    self.soul_curse_mark_publisher_commission_completed(
                        publisher.get("owner_account"),
                        state.get("commission_id"),
                        assist_state.get("last_completed_time") or assist_state.get("last_strip_time") or now_str(),
                    )

            state = self.get_soul_curse_state()
            if publisher.get("shared") and state.get("commission_id") and state.get("commission_status") not in {
                "completed", "gone", "blocked", "no_contract",
            }:
                wait = seconds_until(state.get("next_action_at", "")) if is_future(state.get("next_action_at", "")) else 600
                waits.append(wait)
                return max(30, min(int(wait), 3600))

            if not local_commission_pending:
                chain_wait = await self.soul_curse_run_publisher_chain(publisher)
                if chain_wait <= 10:
                    return max(5, chain_wait)
                waits.append(chain_wait)

        for assistant in shared_assistants:
            if not self.soul_curse_identity_enabled(
                account=assistant.get("owner_account"),
                identity=assistant.get("assistant_identity") or YINLUO_IDENTITY,
            ):
                continue
            wait = await self.soul_curse_shared_assist_tick(assistant)
            if wait <= 10:
                return max(5, wait)
            waits.append(wait)

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
        identity = str(identity or "主魂").strip() or "主魂"
        publisher = self.soul_curse_publisher_profile()
        if cmd == SOUL_CURSE_VISIT_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_visit_response(text, publisher)
        if cmd == SOUL_CURSE_WANYING_GREETING_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_wanying_greeting_response(text, publisher)
        if cmd == SOUL_CURSE_INFER_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_infer_response(text) in {"success", "cooldown", "blocked"}
        if cmd == SOUL_CURSE_PROTECT_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_protect_response(text) in {"success", "cooldown", "blocked"}
        if cmd == SOUL_CURSE_CO_STUDY_COMMAND and identity == "主魂" and publisher:
            return self.record_soul_curse_co_study_response(text) in {"success", "cooldown", "blocked"}
        if cmd.startswith(".发布解咒委托") and identity == "主魂" and publisher:
            return self.record_soul_curse_publish_response(text, publisher) in {"success", "cooldown", "blocked"}

        assistant = self.soul_curse_assistant_profile_for_command(identity, cmd)
        if identity == YINLUO_IDENTITY and assistant:
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
