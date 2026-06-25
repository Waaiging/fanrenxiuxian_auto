import asyncio
import re
import time
from datetime import datetime, timedelta

from common_command_features import add_seconds_str, is_future, now_str, seconds_until, str_to_dt
from log_utils import (
    dashboard_command_disabled,
    feedback_response_matches_command,
    is_game_bot_sender,
    load_command_controls,
    log_incoming_message,
    meaningful_reply_to_msg_id,
    record_bot_response,
)


FISHING_MASTER_COMMAND = ".钓鱼 灵虫饵"
FISHING_BAIT = "灵虫饵"
FISHING_DAILY_LIMIT = 20
FISHING_ROUND_BUFFER_SECONDS = 5
FISHING_IMPENDING_GUARD_SECONDS = 120
FISHING_CROSS_IDENTITY_YIELD_SECONDS = 45
FISHING_CROSS_IDENTITY_YIELD_COOLDOWN_SECONDS = 120
FISHING_ACTIVE_SWITCH_BUFFER_SECONDS = 10
FISHING_RETRY_SECONDS = 10 * 60
FISHING_DISABLED_SLEEP_SECONDS = 60

FISHING_NEST_PLAN = (
    ("妖腥窝", 1),
    ("灵草窝", 2),
    ("米糠小窝", 1),
)

FISHING_NEST_BAIT_REQUIREMENTS = {
    "妖腥窝": ("妖血饵", 2),
    "灵草窝": ("灵米饵", 3),
    "米糠小窝": ("凡饵", 2),
}

FISHING_BAIT_NAMES = {"凡饵", "灵虫饵", "灵米饵", "妖血饵"}


def fishing_default_state():
    return {
        "last_sync_date": "",
        "last_status": "paused",
        "last_detail": "",
        "last_response": "",
        "next_action_at": "",
        "rod_owned": None,
        "skill": "",
        "skill_exp": 0,
        "today_count": 0,
        "daily_limit": FISHING_DAILY_LIMIT,
        "baits": {},
        "catches": {},
        "current_nest": "",
        "current_nest_remaining": 0,
        "nest_plan_date": "",
        "nest_counts": {},
        "nest_blocked": {},
        "bait_purchase_date": "",
        "bait_purchase_done": False,
        "daily_done_basket_sync_date": "",
        "active": False,
        "active_bait": "",
        "active_started_at": "",
        "active_due_at": "",
        "last_catch": "",
        "last_round_at": "",
        "consecutive_empty": 0,
        "last_cross_identity_yield_at": "",
    }


def _strip_markdown(text):
    return str(text or "").replace("**", "").replace("`", "")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _next_day_action_time():
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=5, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def parse_fishing_basket(text):
    clean = _strip_markdown(text)
    result = {
        "matched": "【鱼篓】" in clean,
        "rod_owned": None,
        "skill": "",
        "skill_exp": 0,
        "today_count": None,
        "daily_limit": None,
        "current_nest": "",
        "current_nest_remaining": 0,
        "baits": {},
        "catches": {},
    }
    if not result["matched"]:
        return result

    if "青竹钓竿：" in clean:
        result["rod_owned"] = "已持有" in clean

    skill_match = re.search(r"钓术：\s*(Lv\.\d+\s*[^\n（(]+)\s*[（(](\d+)熟练度", clean)
    if skill_match:
        result["skill"] = skill_match.group(1).strip()
        result["skill_exp"] = int(skill_match.group(2))

    count_match = re.search(r"今日竿数：\s*(\d+)\s*/\s*(\d+)", clean)
    if count_match:
        result["today_count"] = int(count_match.group(1))
        result["daily_limit"] = int(count_match.group(2))

    nest_match = re.search(r"当前窝料：\s*(?:【)?([^（\n]+?)(?:】)?(?:（剩余\s*(\d+)\s*竿）)?\s*(?:\n|$)", clean)
    if nest_match:
        nest = nest_match.group(1).strip()
        if nest and nest != "无":
            result["current_nest"] = nest
            result["current_nest_remaining"] = int(nest_match.group(2) or 0)

    section = ""
    for raw_line in clean.splitlines():
        line = raw_line.strip()
        if line == "鱼饵":
            section = "baits"
            continue
        if line == "鱼获":
            section = "catches"
            continue
        if not line.startswith("- "):
            continue
        item_match = re.match(r"-\s*(.+?)\s*x\s*(\d+)\s*$", line)
        if not item_match:
            continue
        name = item_match.group(1).strip()
        count = int(item_match.group(2))
        if section == "baits":
            result["baits"][name] = count
        elif section == "catches":
            result["catches"][name] = count

    return result


def parse_fishing_start(text):
    clean = _strip_markdown(text)
    result = {
        "matched": False,
        "status": "",
        "bait": "",
        "wait_seconds": 0,
        "missing_bait": "",
        "today_count": None,
        "daily_limit": None,
    }
    if "你挂上" in clean and "抛竿入水" in clean:
        result["matched"] = True
        result["status"] = "started"
        bait_match = re.search(r"你挂上\s*【([^】]+)】", clean)
        if bait_match:
            result["bait"] = bait_match.group(1).strip()
        wait_match = re.search(r"预计\s*(\d+)\s*秒\s*内会有鱼讯", clean)
        countdown_match = re.search(r"鱼讯倒计时：\s*(\d+)\s*秒", clean)
        result["wait_seconds"] = int((wait_match or countdown_match).group(1)) if (wait_match or countdown_match) else 60
        return result

    missing_match = re.search(r"鱼篓中没有【([^】]+)】", clean)
    if missing_match:
        result["matched"] = True
        result["status"] = "missing_bait"
        result["missing_bait"] = missing_match.group(1).strip()
        return result

    limit_match = re.search(r"今日已垂钓\s*(\d+)\s*/\s*(\d+)", clean)
    if limit_match:
        result["matched"] = True
        result["status"] = "daily_limit"
        result["today_count"] = int(limit_match.group(1))
        result["daily_limit"] = int(limit_match.group(2))
        return result

    if "尚无【青竹钓竿】" in clean:
        result["matched"] = True
        result["status"] = "no_rod"
        return result

    if "已有一竿尚未收起" in clean:
        result["matched"] = True
        result["status"] = "already_active"
        return result

    return result


def parse_buy_bait(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": "", "bait": "", "count": 0}
    buy_match = re.search(r"购得\s*【([^】]+)】x(\d+)", clean)
    if buy_match:
        result.update({
            "matched": True,
            "status": "success",
            "bait": buy_match.group(1).strip(),
            "count": int(buy_match.group(2)),
        })
        return result
    if "渔具铺中并无此等鱼饵" in clean:
        result.update({"matched": True, "status": "invalid_bait"})
    elif "资源不足" in clean or "灵石不足" in clean or "材料不足" in clean:
        result.update({"matched": True, "status": "insufficient_resource"})
    return result


def parse_nest_response(text):
    clean = _strip_markdown(text)
    result = {
        "matched": False,
        "status": "",
        "nest": "",
        "remaining": 0,
        "missing_name": "",
        "missing_count": 0,
    }
    success_match = re.search(r"撒下\s*【([^】]+)】.*接下来\s*(\d+)\s*竿", clean)
    if "【打窝已成】" in clean or success_match:
        result["matched"] = True
        result["status"] = "success"
        if success_match:
            result["nest"] = success_match.group(1).strip()
            result["remaining"] = int(success_match.group(2))
        return result

    missing_match = re.search(r"资源不足：\s*([^x。\n]+)x(\d+)", clean)
    if missing_match:
        result.update({
            "matched": True,
            "status": "missing_resource",
            "missing_name": missing_match.group(1).strip(),
            "missing_count": int(missing_match.group(2)),
        })
        return result

    if "今日此类窝料已经用尽" in clean:
        result.update({"matched": True, "status": "daily_used_up"})
    elif "打窝失败" in clean:
        result.update({"matched": True, "status": "failed"})
    return result


def parse_rod_response(text):
    clean = _strip_markdown(text)
    result = {"matched": False, "status": "", "catch": ""}
    if "【提竿成功】" in clean:
        result["matched"] = True
        result["status"] = "success"
        fish_match = re.search(r"一尾\s*【([^】]+)】", clean)
        if fish_match:
            result["catch"] = fish_match.group(1).strip()
        return result
    if "【空竿】" in clean or "提竿太急" in clean or "时机差了一线" in clean or "收竿起身" in clean:
        result["matched"] = True
        result["status"] = "empty"
    return result


class FishingMixin:
    def get_fishing_state(self, identity="主魂"):
        target = self.state if identity == "主魂" else self.get_avatar_state(identity)
        state = target.get("fishing")
        if not isinstance(state, dict):
            state = fishing_default_state()
            target["fishing"] = state
        else:
            defaults = fishing_default_state()
            for key, value in defaults.items():
                state.setdefault(key, value)
        today = _today()
        if state.get("nest_plan_date") != today:
            state["nest_plan_date"] = today
            state["nest_counts"] = {}
            state["nest_blocked"] = {}
        if state.get("bait_purchase_date") != today:
            state["bait_purchase_date"] = today
            state["bait_purchase_done"] = False
        return state

    def fishing_logger(self):
        return getattr(self, "log", None)

    def fishing_command_is_enabled(self, identity):
        controls = load_command_controls().get(getattr(self, "account_key", ""), {})
        if not isinstance(controls, dict):
            return False
        keys = (FISHING_MASTER_COMMAND,)
        for ident in (identity, "*"):
            ident_controls = controls.get(ident, {})
            if not isinstance(ident_controls, dict):
                continue
            for key in keys:
                entry = ident_controls.get(key)
                if isinstance(entry, dict):
                    return not bool(entry.get("disabled"))
                if entry:
                    return False
        return False

    async def send_fishing_command(self, identity, command, timeout=60):
        previous_last_sent_id = getattr(self, "last_sent_id", None)
        if identity == "主魂":
            response = await self.send_and_wait_feedback(
                command,
                timeout=timeout,
                max_retries=0,
                suppress_no_response_alert=True,
            )
        else:
            response = await self.send_and_wait_feedback_identity(
                identity,
                command,
                timeout=timeout,
                max_retries=0,
                suppress_no_response_alert=True,
            )
        if self.fishing_response_text(response):
            return response
        sent_id = getattr(self, "last_sent_id", None)
        if sent_id and sent_id != previous_last_sent_id:
            polled = await self.fishing_poll_reply_to_sent_command(identity, command, sent_id)
            if polled:
                return polled
        return response

    async def fishing_poll_reply_to_sent_command(self, identity, command, sent_id, timeout=8):
        client = getattr(self, "client", None)
        if client is None or not sent_id:
            return ""
        log = self.fishing_logger()
        deadline = time.monotonic() + max(1, float(timeout or 1))
        while time.monotonic() < deadline:
            try:
                messages = await client.get_messages(getattr(self, "target_chat_id"), limit=40)
            except Exception as exc:
                if log:
                    log.info(f"Fishing poll [{identity}] failed for {command}: {exc}")
                return ""
            for msg in reversed(messages or []):
                try:
                    if (getattr(msg, "id", 0) or 0) <= int(sent_id):
                        continue
                    if meaningful_reply_to_msg_id(self, msg) != sent_id:
                        continue
                    text = self.fishing_response_text(msg)
                    if not text or not feedback_response_matches_command(command, text):
                        continue
                    sender = await msg.get_sender()
                    if sender is not None and not is_game_bot_sender(self, sender):
                        continue
                    record_bot_response(self)
                    await log_incoming_message(
                        self,
                        command,
                        text,
                        msg=msg,
                        logger=log,
                        identity=identity,
                    )
                    if log:
                        log.info(
                            f"Fishing poll [{identity}] matched {command} reply: "
                            f"command_msg={sent_id}, response_msg={getattr(msg, 'id', None)}."
                        )
                    return text
                except Exception:
                    continue
            await asyncio.sleep(1)
        return ""

    def fishing_response_text(self, response):
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        return str(getattr(response, "text", "") or getattr(response, "raw_text", "") or "")

    def fishing_set_status(self, identity, status, detail="", next_seconds=None, response=""):
        state = self.get_fishing_state(identity)
        state["last_status"] = status
        state["last_detail"] = str(detail or "")[:300]
        if response:
            state["last_response"] = str(response or "")[:500]
        if next_seconds is not None:
            state["next_action_at"] = add_seconds_str(now_str(), next_seconds)
        self.save_state()

    def fishing_wait_from_state(self, identity, default_seconds=FISHING_RETRY_SECONDS):
        state = self.get_fishing_state(identity)
        active_due = state.get("active_due_at", "")
        if state.get("active") and active_due and is_future(active_due):
            return max(1, min(seconds_until(active_due), 300))
        next_action = state.get("next_action_at", "")
        if next_action and is_future(next_action):
            return max(1, min(seconds_until(next_action), 300))
        return default_seconds

    def fishing_impending_wait(self, identity):
        return self.fishing_impending_wait_for_identity(identity)

    def fishing_impending_wait_for_identity(self, identity):
        try:
            if hasattr(self, "_state_impending_command_wait"):
                target = self.state if identity == "主魂" else self.get_avatar_state(identity)
                state_without_meditation = dict(target or {})
                state_without_meditation["in_deep_meditation"] = True
                state_without_meditation["deep_meditation_end_time"] = ""
                state_without_meditation["next_meditation_retry_time"] = ""
                wait = self._state_impending_command_wait(state_without_meditation, identity=identity)
                if hasattr(self, "custom_command_impending_wait"):
                    custom_wait = self.custom_command_impending_wait(identity)
                    if hasattr(self, "merge_impending_wait"):
                        wait = self.merge_impending_wait(wait, custom_wait)
                    else:
                        wait = min(wait, custom_wait)
            else:
                wait = self.get_identity_impending_command_wait(identity)
        except Exception:
            return -1
        if wait is None:
            return -1
        try:
            return float(wait)
        except Exception:
            return -1

    def fishing_other_identity_impending_wait(self, identity):
        identities = ["主魂"]
        identities.extend(list(getattr(self, "avatars", []) or []))
        best_identity = ""
        best_wait = None
        for other in identities:
            if other == identity:
                continue
            try:
                if self.identity_pause_seconds(other) > 0:
                    continue
            except Exception:
                pass
            wait = self.fishing_impending_wait_for_identity(other)
            if wait is None or wait < 0:
                continue
            if best_wait is None or wait < best_wait:
                best_wait = wait
                best_identity = other
        if best_wait is None:
            return "", -1
        return best_identity, float(best_wait)

    def fishing_recently_yielded_to_other_identity(self, identity):
        state = self.get_fishing_state(identity)
        last_at = state.get("last_cross_identity_yield_at", "")
        if not last_at:
            return False
        try:
            return seconds_until(add_seconds_str(last_at, FISHING_CROSS_IDENTITY_YIELD_COOLDOWN_SECONDS)) > 0
        except Exception:
            return False

    def fishing_active_switch_wait(self, identity, target_identity="", command=""):
        """Return seconds to delay switching away from an unfinished fishing round."""
        identity = str(identity or "主魂").strip() or "主魂"
        target_identity = str(target_identity or "").strip()
        if target_identity and target_identity == identity:
            return 0
        if str(command or "").strip() == ".提竿":
            return 0
        try:
            state = self.get_fishing_state(identity)
        except Exception:
            return 0
        if not state.get("active"):
            return 0
        due_at = state.get("active_due_at", "")
        if due_at and is_future(due_at):
            return max(1, min(
                seconds_until(due_at) + FISHING_ACTIVE_SWITCH_BUFFER_SECONDS,
                300,
            ))
        return FISHING_ACTIVE_SWITCH_BUFFER_SECONDS

    def fishing_active_due_for_switch(self, identity, target_identity="", command=""):
        """Return True when a switch is waiting on a fishing round that is already due."""
        identity = str(identity or "主魂").strip() or "主魂"
        target_identity = str(target_identity or "").strip()
        if target_identity and target_identity == identity:
            return False
        if str(command or "").strip() == ".提竿":
            return False
        try:
            state = self.get_fishing_state(identity)
        except Exception:
            return False
        if not state.get("active"):
            return False
        due_at = state.get("active_due_at", "")
        return bool(due_at and not is_future(due_at))

    async def fishing_switch_wait_or_raise_due(self, identity, target_identity="", command=""):
        """
        Return switch wait seconds, raising an overdue rod first when the current
        identity is already allowed to finish the active fishing round.

        Call this only while the caller holds avatar_send_lock and current_identity
        is still the fishing identity; the raw send path intentionally avoids
        reacquiring the same lock.
        """
        wait = self.fishing_active_switch_wait(identity, target_identity=target_identity, command=command)
        if wait <= 0:
            return wait
        if not self.fishing_active_due_for_switch(identity, target_identity=target_identity, command=command):
            return wait
        log = self.fishing_logger()
        if log:
            log.info(
                f"Fishing [{identity}] is overdue before switching to {target_identity or 'unknown'}; "
                "raising rod first."
            )
        await self.fishing_raise_rod_current_identity(identity)
        return self.fishing_active_switch_wait(identity, target_identity=target_identity, command=command)

    async def fishing_sync_basket(self, identity):
        resp = await self.send_fishing_command(identity, ".鱼篓", timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_fishing_basket(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if not parsed.get("matched"):
            self.fishing_set_status(identity, "sync_failed", "鱼篓回复未识别", FISHING_RETRY_SECONDS, text)
            return False
        for key in ("rod_owned", "skill", "skill_exp", "current_nest", "current_nest_remaining"):
            if parsed.get(key) is not None:
                state[key] = parsed.get(key)
        if parsed.get("today_count") is not None:
            state["today_count"] = parsed["today_count"]
        if parsed.get("daily_limit") is not None:
            state["daily_limit"] = parsed["daily_limit"]
        state["baits"] = parsed.get("baits", {})
        state["catches"] = parsed.get("catches", {})
        state["last_sync_date"] = _today()
        state["last_status"] = "synced"
        state["last_detail"] = f"今日竿数 {state.get('today_count', 0)}/{state.get('daily_limit', FISHING_DAILY_LIMIT)}"
        self.save_state()
        return True

    async def fishing_buy_bait(self, identity, bait, count):
        count = max(1, int(count or 1))
        command = f".买鱼饵 {bait} {count}"
        resp = await self.send_fishing_command(identity, command, timeout=90)
        text = self.fishing_response_text(resp)
        parsed = parse_buy_bait(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if parsed.get("status") == "success":
            baits = state.setdefault("baits", {})
            baits[bait] = int(baits.get(bait, 0)) + int(parsed.get("count") or count)
            state["last_status"] = "bait_bought"
            state["last_detail"] = f"购入 {bait} x{parsed.get('count') or count}"
            self.save_state()
            return True
        self.fishing_set_status(identity, "bait_buy_failed", f"{bait} x{count} 购买失败", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_ensure_daily_bait(self, identity):
        state = self.get_fishing_state(identity)
        today_count = int(state.get("today_count") or 0)
        daily_limit = int(state.get("daily_limit") or FISHING_DAILY_LIMIT)
        needed = max(0, daily_limit - today_count)
        if needed <= 0:
            return True
        if state.get("bait_purchase_done") and int(state.get("baits", {}).get(FISHING_BAIT, 0)) > 0:
            return True
        current = int(state.get("baits", {}).get(FISHING_BAIT, 0))
        buy_count = max(0, needed - current)
        if buy_count <= 0:
            state["bait_purchase_done"] = True
            self.save_state()
            return True
        if not await self.fishing_buy_bait(identity, FISHING_BAIT, buy_count):
            return False
        state = self.get_fishing_state(identity)
        state["bait_purchase_date"] = _today()
        state["bait_purchase_done"] = True
        self.save_state()
        return True

    def fishing_next_nest(self, identity):
        state = self.get_fishing_state(identity)
        if state.get("current_nest") and int(state.get("current_nest_remaining") or 0) > 0:
            return ""
        counts = state.setdefault("nest_counts", {})
        blocked = state.setdefault("nest_blocked", {})
        today = _today()
        for nest, limit in FISHING_NEST_PLAN:
            if blocked.get(nest) == today:
                continue
            if int(counts.get(nest, 0)) < limit:
                return nest
        return ""

    async def fishing_try_nest(self, identity):
        nest = self.fishing_next_nest(identity)
        if not nest:
            return True
        resp = await self.send_fishing_command(identity, f".打窝 {nest}", timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_nest_response(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        if parsed.get("status") == "success":
            nest_name = parsed.get("nest") or nest
            state["current_nest"] = nest_name
            state["current_nest_remaining"] = int(parsed.get("remaining") or 0)
            counts = state.setdefault("nest_counts", {})
            counts[nest_name] = int(counts.get(nest_name, 0)) + 1
            state["last_status"] = "nested"
            state["last_detail"] = f"{nest_name} 剩余 {state['current_nest_remaining']} 竿"
            self.save_state()
            return True
        if parsed.get("status") == "missing_resource":
            missing_name = parsed.get("missing_name", "")
            missing_count = int(parsed.get("missing_count") or 0)
            if missing_name in FISHING_BAIT_NAMES and missing_count > 0:
                if await self.fishing_buy_bait(identity, missing_name, missing_count):
                    return await self.fishing_try_nest(identity)
                return False
            state.setdefault("nest_blocked", {})[nest] = _today()
            self.fishing_set_status(identity, "nest_blocked", f"{nest} 缺少 {missing_name}x{missing_count}", 60, text)
            return False
        if parsed.get("status") == "daily_used_up":
            limit = dict(FISHING_NEST_PLAN).get(nest, 1)
            state.setdefault("nest_counts", {})[nest] = limit
            state["last_status"] = "nest_used_up"
            state["last_detail"] = f"{nest} 今日次数已尽"
            self.save_state()
            return True
        if parsed.get("matched"):
            state.setdefault("nest_blocked", {})[nest] = _today()
            self.fishing_set_status(identity, "nest_failed", f"{nest} 打窝失败", 60, text)
            return False
        self.fishing_set_status(identity, "nest_unrecognized", f"{nest} 回复未识别", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_start_round(self, identity):
        resp = await self.send_fishing_command(identity, FISHING_MASTER_COMMAND, timeout=60)
        text = self.fishing_response_text(resp)
        parsed = parse_fishing_start(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        status = parsed.get("status")
        if status == "started":
            wait_seconds = max(5, int(parsed.get("wait_seconds") or 60)) + FISHING_ROUND_BUFFER_SECONDS
            state["active"] = True
            state["active_bait"] = parsed.get("bait") or FISHING_BAIT
            state["active_started_at"] = now_str()
            state["active_due_at"] = add_seconds_str(now_str(), wait_seconds)
            baits = state.setdefault("baits", {})
            baits[FISHING_BAIT] = max(0, int(baits.get(FISHING_BAIT, 0)) - 1)
            state["last_status"] = "fishing"
            state["last_detail"] = f"{state['active_bait']} 等鱼讯 {wait_seconds}秒"
            self.save_state()
            return True
        if status == "missing_bait":
            missing = parsed.get("missing_bait") or FISHING_BAIT
            remaining = max(1, int(state.get("daily_limit") or FISHING_DAILY_LIMIT) - int(state.get("today_count") or 0))
            if await self.fishing_buy_bait(identity, missing, remaining):
                return await self.fishing_start_round(identity)
            return False
        if status == "daily_limit":
            state["today_count"] = parsed.get("today_count") or state.get("today_count", FISHING_DAILY_LIMIT)
            state["daily_limit"] = parsed.get("daily_limit") or state.get("daily_limit", FISHING_DAILY_LIMIT)
            state["active"] = False
            state["next_action_at"] = _next_day_action_time()
            state["last_status"] = "daily_done"
            state["last_detail"] = f"今日已垂钓 {state['today_count']}/{state['daily_limit']}"
            self.save_state()
            await self.fishing_sync_daily_done_basket(identity)
            return True
        if status == "no_rod":
            state["rod_owned"] = False
            state["active"] = False
            self.fishing_set_status(identity, "no_rod", "尚无青竹钓竿", 3600, text)
            return False
        if status == "already_active":
            state["active"] = True
            state["active_due_at"] = add_seconds_str(now_str(), 60)
            state["last_status"] = "recover_active"
            state["last_detail"] = "已有一竿，60秒后尝试提竿"
            self.save_state()
            return True
        self.fishing_set_status(identity, "start_unrecognized", "钓鱼回复未识别", FISHING_RETRY_SECONDS, text)
        return False

    async def fishing_sync_daily_done_basket(self, identity):
        state = self.get_fishing_state(identity)
        today = _today()
        if state.get("daily_done_basket_sync_date") == today:
            return True
        ok = await self.fishing_sync_basket(identity)
        state = self.get_fishing_state(identity)
        if ok:
            state["daily_done_basket_sync_date"] = today
        state["last_status"] = "daily_done"
        state["last_detail"] = f"今日已垂钓 {state.get('today_count')}/{state.get('daily_limit')}"
        state["next_action_at"] = _next_day_action_time()
        self.save_state()
        return ok

    async def fishing_raise_rod_current_identity(self, identity):
        previous_last_sent_id = getattr(self, "last_sent_id", None)
        resp = await self._send_and_wait_feedback_raw(
            ".提竿",
            timeout=60,
            max_retries=0,
            suppress_no_response_alert=True,
        )
        if not self.fishing_response_text(resp):
            sent_id = getattr(self, "last_sent_id", None)
            if sent_id and sent_id != previous_last_sent_id:
                polled = await self.fishing_poll_reply_to_sent_command(identity, ".提竿", sent_id)
                if polled:
                    resp = polled
        return await self.fishing_record_rod_response(identity, resp)

    async def fishing_raise_rod(self, identity):
        resp = await self.send_fishing_command(identity, ".提竿", timeout=60)
        return await self.fishing_record_rod_response(identity, resp)

    async def fishing_record_rod_response(self, identity, resp):
        text = self.fishing_response_text(resp)
        parsed = parse_rod_response(text)
        state = self.get_fishing_state(identity)
        state["last_response"] = text[:500]
        state["active"] = False
        state["active_due_at"] = ""
        state["active_started_at"] = ""
        state["active_bait"] = ""
        state["last_round_at"] = now_str()
        state["today_count"] = min(int(state.get("daily_limit") or FISHING_DAILY_LIMIT), int(state.get("today_count") or 0) + 1)
        if state.get("current_nest") and int(state.get("current_nest_remaining") or 0) > 0:
            state["current_nest_remaining"] = max(0, int(state.get("current_nest_remaining") or 0) - 1)
            if state["current_nest_remaining"] <= 0:
                state["current_nest"] = ""
        if parsed.get("status") == "success":
            state["last_status"] = "caught"
            state["last_catch"] = parsed.get("catch", "")
            state["last_detail"] = f"提竿成功{('：' + state['last_catch']) if state['last_catch'] else ''}"
            state["consecutive_empty"] = 0
        elif parsed.get("status") == "empty":
            state["last_status"] = "empty"
            state["last_detail"] = "空竿"
            state["consecutive_empty"] = int(state.get("consecutive_empty") or 0) + 1
        else:
            state["last_status"] = "raise_unrecognized"
            state["last_detail"] = "提竿回复未识别"
            state["consecutive_empty"] = int(state.get("consecutive_empty") or 0) + 1
        if int(state.get("today_count") or 0) >= int(state.get("daily_limit") or FISHING_DAILY_LIMIT):
            state["next_action_at"] = _next_day_action_time()
        else:
            state["next_action_at"] = add_seconds_str(now_str(), 5)
        self.save_state()
        if int(state.get("today_count") or 0) >= int(state.get("daily_limit") or FISHING_DAILY_LIMIT):
            await self.fishing_sync_daily_done_basket(identity)
        return parsed.get("matched", False)

    async def fishing_tick(self, identity):
        if not self.fishing_command_is_enabled(identity):
            state = self.get_fishing_state(identity)
            if state.get("last_status") != "paused" or state.get("last_detail") != "dashboard 默认暂停/未启用":
                self.fishing_set_status(identity, "paused", "dashboard 默认暂停/未启用", None)
            return FISHING_DISABLED_SLEEP_SECONDS
        if self.identity_pause_seconds(identity) > 0:
            return 60

        state = self.get_fishing_state(identity)
        if state.get("last_status") == "meditation_blocked":
            state["last_status"] = "enabled"
            state["last_detail"] = "深度闭关不阻塞钓鱼"
            state["next_action_at"] = ""
            self.save_state()

        today = _today()
        if state.get("last_sync_date") != today or state.get("rod_owned") is None:
            await self.fishing_sync_basket(identity)
            return 5

        if state.get("rod_owned") is False:
            self.fishing_set_status(identity, "no_rod", "尚无青竹钓竿", 3600)
            return 3600

        if int(state.get("today_count") or 0) >= int(state.get("daily_limit") or FISHING_DAILY_LIMIT):
            await self.fishing_sync_daily_done_basket(identity)
            return self.fishing_wait_from_state(identity, 3600)

        if state.get("active"):
            due_at = state.get("active_due_at", "")
            if due_at and is_future(due_at):
                return max(1, min(seconds_until(due_at), 300))
            await self.fishing_raise_rod(identity)
            return 5

        next_action = state.get("next_action_at", "")
        if next_action and is_future(next_action):
            return max(1, min(seconds_until(next_action), 300))

        other_identity, other_wait = self.fishing_other_identity_impending_wait(identity)
        if (
            other_identity
            and 0 <= other_wait <= FISHING_IMPENDING_GUARD_SECONDS
            and not self.fishing_recently_yielded_to_other_identity(identity)
        ):
            state["last_cross_identity_yield_at"] = now_str()
            self.fishing_set_status(
                identity,
                "yielding_identity",
                f"本轮提竿后让路给 {other_identity} 的到期指令",
                FISHING_CROSS_IDENTITY_YIELD_SECONDS,
            )
            return FISHING_CROSS_IDENTITY_YIELD_SECONDS

        impending = self.fishing_impending_wait(identity)
        if 0 <= impending <= FISHING_IMPENDING_GUARD_SECONDS:
            self.fishing_set_status(
                identity,
                "yielding",
                f"让路给 {impending:.0f}秒内到期的其他指令",
                max(10, min(int(impending) + 10, 120)),
            )
            return max(10, min(int(impending) + 10, 120))

        if not await self.fishing_ensure_daily_bait(identity):
            return FISHING_RETRY_SECONDS
        if not await self.fishing_try_nest(identity):
            return 60
        await self.fishing_start_round(identity)
        return self.fishing_wait_from_state(identity, 60)

    async def run_fishing_loop(self, identity="主魂", initial_delay=0):
        await self.startup_done.wait()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        log = self.fishing_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                wait_seconds = await self.fishing_tick(identity)
                if log:
                    log.info(f"Fishing loop [{identity}] sleeping {int(wait_seconds)}s.")
                if (
                    not self.fishing_command_is_enabled(identity)
                    and hasattr(self, "wait_for_dashboard_command_control_change")
                ):
                    await self.wait_for_dashboard_command_control_change(
                        max(10, min(int(wait_seconds), FISHING_DISABLED_SLEEP_SECONDS))
                    )
                else:
                    await asyncio.sleep(max(1, min(int(wait_seconds), 300)))
            except Exception as exc:
                if log:
                    log.error(f"Fishing loop [{identity}] error: {exc}", exc_info=True)
                await asyncio.sleep(FISHING_RETRY_SECONDS)
