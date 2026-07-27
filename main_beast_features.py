#!/usr/bin/env python3
"""Focused Wanling beast automation for the main account."""

import asyncio
import hashlib
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta

from miniapp_beast import (
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_RETRY_SECONDS,
    MiniAppBeastError,
    fetch_miniapp_beast_snapshot,
    read_cached_spirit_token,
    read_refresh_request,
    write_cached_spirit_token,
)
from miniapp_beast_contract import MiniAppBeastContractWorker


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
HUNT_CD_SECONDS = 6 * 3600
HUNT_FULL_RETRY_SECONDS = 10 * 60
HUNT_FAIL_RETRY_SECONDS = 60 * 60
ABYSS_CD_SECONDS = 6 * 3600
PASTURE_CD_SECONDS = 4 * 3600 + 60
INTERACTION_CD_SECONDS = 90 * 60
BORDER_PATROL_CD_SECONDS = 75 * 60
ACTION_RETRY_SECONDS = 10 * 60
ABYSS_MIN_STAMINA = 30
BORDER_PATROL_MIN_STAMINA = 24
FOCUS_PROTECT_STAMINA = 50
ROSTER_DAILY_LIMIT = 2
MAX_LOOP_SLEEP_SECONDS = 300


def beast_now():
    return datetime.now()


def beast_time(value=None):
    return (value or beast_now()).strftime(TIME_FORMAT)


def parse_beast_time(value):
    try:
        return datetime.strptime(str(value or ""), TIME_FORMAT)
    except Exception:
        return None


def beast_add_seconds(seconds, value=None):
    return beast_time((value or beast_now()) + timedelta(seconds=max(0, int(seconds or 0))))


def beast_seconds_until(value):
    target = parse_beast_time(value)
    return max(0, int((target - beast_now()).total_seconds())) if target else 0


def beast_time_is_future(value):
    target = parse_beast_time(value)
    return bool(target and target > beast_now())


def main_beast_default_state():
    return {
        "last_hunt_time": "",
        "next_hunt_time": "",
        "last_release_beast_time": "",
        "beast_hunt_stopped": False,
        "beast_hunt_stopped_reason": "",
        "last_abyss_time": "",
        "next_abyss_time": "",
        "last_pasture_time": "",
        "next_pasture_time": "",
        "pasture_pending_count": 0,
        "pasture_pending_since": "",
        "last_pasture_return_time": "",
        "last_pasture_return_event_key": "",
        "last_beast_interaction_time": "",
        "next_beast_interaction_time": "",
        "last_beast_border_patrol_time": "",
        "next_beast_border_patrol_time": "",
        "beast_border_patrol_name": "",
        "beast_border_patrol_mode": "袭营",
        "best_beast_name": "",
        "best_beast_power": 0,
        "best_beast_status": "",
        "best_beast_stamina": -1,
        "beasts_cache": [],
        "beast_roster_updated_at": "",
        "beast_roster_last_source": "",
        "beast_roster_auto_query_date": "",
        "beast_roster_auto_query_count": 0,
        "next_beast_status_check_time": "",
        "last_beast_roster_query_result": "",
        "last_beast_roster_response_excerpt": "",
        "beast_miniapp_last_attempt_time": "",
        "beast_miniapp_last_sync_time": "",
        "beast_miniapp_last_error": "",
        "beast_miniapp_sync_count": 0,
        "beast_miniapp_refresh_request_id": "",
    }


def main_beast_feedback_candidate(command, text):
    command = str(command or "").strip()
    clean = str(text or "").replace("**", "")
    if command == ".我的灵兽":
        return "灵兽伙伴们" in clean or "灵兽袋" in clean
    if command == ".一键放养":
        return any(marker in clean for marker in ("万兽奔腾", "放养", "没有可放养", "暂无可放养"))
    if command == ".巡边归来":
        return "巡边归来" in clean or ("巡边" in clean and "归来" in clean)
    if command.startswith(".灵兽巡边") or command == ".巡边状态":
        return any(marker in clean for marker in ("灵兽巡边", "边境巡行", "巡边", "暂无灵兽"))
    if command.startswith(".灵兽互动"):
        return "灵兽互动" in clean or "灵契" in clean
    if command == ".寻觅灵兽":
        return any(marker in clean for marker in ("驯化", "寻觅", "灵兽袋", "灵兽都被吓跑"))
    if command.startswith(".探渊 "):
        return any(marker in clean for marker in ("万兽渊", "渊中", "正在与", "险胜", "体力不足"))
    return False


class MainBeastMixin:
    """Main-soul beast loops using the same priorities as the xiaohao main soul."""

    def initialize_main_beast_runtime(self):
        self.beast_lock = asyncio.Lock()
        self.beast_wakeup = asyncio.Event()
        settings = self.main_beast_miniapp_settings()
        self._miniapp_beast_token = read_cached_spirit_token(
            self.main_beast_config_dir(),
            settings["entry_url"],
        ) if settings["enabled"] else ""
        self._miniapp_beast_contract = MiniAppBeastContractWorker.from_actor(
            self,
            logger=self.main_beast_logger(),
        )

    def main_beast_logger(self):
        return logging.getLogger("MainBeast")

    def ensure_main_beast_state(self):
        changed = False
        for key, value in main_beast_default_state().items():
            if key not in self.state:
                self.state[key] = list(value) if isinstance(value, list) else value
                changed = True
        return changed

    def main_beast_save(self):
        self.save_state()

    def main_beast_response_text(self, response):
        if response is None:
            return ""
        if isinstance(response, str):
            return response
        return str(getattr(response, "text", "") or "")

    def main_beast_parse_wait(self, text):
        try:
            value = self.parse_wait_time(str(text or ""))
            if value is not None and int(value) >= 0:
                return int(value)
        except Exception:
            pass
        clean = str(text or "").replace("**", "").replace(" ", "")
        total = 0
        found = False
        for amount, unit in re.findall(r"(\d+)(小时|分钟|分|秒)", clean):
            total += int(amount) * (3600 if unit == "小时" else 60 if unit in {"分钟", "分"} else 1)
            found = True
        return total if found else -1

    def main_beast_name_matches(self, full_name, target_name):
        left = re.sub(r"\s*\([^)]*\)\s*$", "", str(full_name or "")).strip()
        right = re.sub(r"\s*\([^)]*\)\s*$", "", str(target_name or "")).strip()
        return bool(left and right and left == right)

    def main_beast_tier(self, value):
        text = str(value.get("species") if isinstance(value, dict) else value or "")
        match = re.search(r"([一二三四五六七八九十\d]+)\s*阶", text)
        if not match:
            return 0
        raw = match.group(1)
        if raw.isdigit():
            return int(raw)
        chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        return chinese.get(raw, 0)

    def parse_main_beasts_info(self, text):
        """Parse the current repeated-block `.我的灵兽` response."""
        clean = str(text or "").replace("（", "(").replace("）", ")").replace("**", "")
        if "灵兽伙伴们" not in clean and not re.search(r"\n-\s*[^\n]+\n\s*-\s*种类[:：]", clean):
            return []
        beasts = []
        for block in re.split(r"\n-\s*", clean):
            if not block.strip() or "灵兽伙伴们" in block:
                continue
            header = block.strip().splitlines()[0].strip()
            name = re.sub(r"\s*\([^)]*\)\s*$", "", header).strip(" -")
            status_match = re.search(r"\((出战中|休息中|放养中|受伤|重伤|治疗中|探险中|偷菜中|巡游中|巡边中)\)\s*$", header)
            species_match = re.search(r"种类[:：]\s*([^\n]+)", block)
            power_match = re.search(r"战力[:：]\s*(\d+)", block)
            stamina_match = re.search(r"体力[:：]\s*(\d+)", block)
            exp_match = re.search(r"经验[:：]\s*(\d+)", block)
            if not name or not species_match or not power_match or not stamina_match:
                continue
            species = species_match.group(1).strip()
            beasts.append({
                "full_name": name,
                "status": status_match.group(1) if status_match else "未知",
                "species": species,
                "tier": self.main_beast_tier(species),
                "power": int(power_match.group(1)),
                "stamina": int(stamina_match.group(1)),
                "exp": int(exp_match.group(1)) if exp_match else 0,
            })
        beasts.sort(key=lambda item: (item["power"], item["stamina"], item["full_name"]), reverse=True)
        return beasts

    def main_beast_by_name(self, name):
        for beast in self.state.get("beasts_cache") or []:
            if self.main_beast_name_matches(beast.get("full_name"), name):
                return beast
        return None

    def update_main_best_beast(self):
        cache = list(self.state.get("beasts_cache") or [])
        if not cache:
            return None
        best = max(cache, key=lambda item: (int(item.get("power") or 0), int(item.get("stamina", -1)), item.get("full_name", "")))
        self.state["best_beast_name"] = best.get("full_name", "")
        self.state["best_beast_power"] = int(best.get("power") or 0)
        self.state["best_beast_status"] = best.get("status", "未知")
        self.state["best_beast_stamina"] = int(best.get("stamina", -1))
        return best

    def set_main_beast_status(self, name, status, stamina=None):
        if not name or not status:
            return
        for beast in self.state.get("beasts_cache") or []:
            if self.main_beast_name_matches(beast.get("full_name"), name):
                beast["status"] = status
                if stamina is not None:
                    beast["stamina"] = max(0, int(stamina))
                break
        if self.main_beast_name_matches(self.state.get("best_beast_name"), name):
            self.state["best_beast_status"] = status
            if stamina is not None:
                self.state["best_beast_stamina"] = max(0, int(stamina))

    def record_main_beast_roster(self, text, source=""):
        beasts = self.parse_main_beasts_info(text)
        excerpt = re.sub(r"\s+", " ", str(text or "")).strip()
        self.state["last_beast_roster_response_excerpt"] = excerpt[:240]
        if not beasts:
            self.state["last_beast_roster_query_result"] = "not_roster"
            return False
        self.state["beasts_cache"] = beasts
        self.state["beast_roster_updated_at"] = beast_time()
        self.state["beast_roster_last_source"] = source or "reply"
        self.state["last_beast_roster_query_result"] = "parsed"
        self.update_main_best_beast()
        active_patrol = next((item for item in beasts if "巡边" in str(item.get("status") or "")), None)
        if active_patrol:
            self.state["beast_border_patrol_name"] = active_patrol.get("full_name", "")
        self.main_beast_save()
        self.main_beast_logger().info("Main beast roster synced from %s: %s beasts", source or "reply", len(beasts))
        return True

    def main_beast_miniapp_settings(self):
        config = getattr(self, "config", {}) or {}
        settings = config.get("miniapp_beast") or {}
        if not isinstance(settings, dict):
            settings = {}
        entry_url = str(settings.get("entry_url") or "").strip()
        enabled = bool(settings.get("enabled", bool(entry_url))) and bool(entry_url)
        try:
            refresh_seconds = max(60, int(settings.get("refresh_seconds") or DEFAULT_REFRESH_SECONDS))
        except (TypeError, ValueError):
            refresh_seconds = DEFAULT_REFRESH_SECONDS
        try:
            retry_seconds = max(60, int(settings.get("retry_seconds") or DEFAULT_RETRY_SECONDS))
        except (TypeError, ValueError):
            retry_seconds = DEFAULT_RETRY_SECONDS
        try:
            timeout = max(5, min(60, int(settings.get("timeout_seconds") or 20)))
        except (TypeError, ValueError):
            timeout = 20
        return {
            "enabled": enabled,
            "entry_url": entry_url,
            "bot_username": str(settings.get("bot_username") or "fanrenxiuxian_bot").strip(),
            "refresh_seconds": refresh_seconds,
            "retry_seconds": retry_seconds,
            "timeout": timeout,
        }

    def main_beast_config_dir(self):
        state_file = str(getattr(self, "state_file", "") or "").strip()
        if state_file:
            return os.path.dirname(os.path.abspath(state_file))
        return os.path.dirname(os.path.abspath(__file__))

    def record_main_beast_miniapp_snapshot(self, snapshot):
        beasts = list((snapshot or {}).get("beasts") or [])
        if not beasts:
            raise MiniAppBeastError("beast_roster_empty")
        self.state["beasts_cache"] = beasts
        self.state["beast_roster_updated_at"] = beast_time()
        self.state["beast_roster_last_source"] = "miniapp"
        self.state["last_beast_roster_query_result"] = "parsed"
        self.state["last_beast_roster_response_excerpt"] = ""
        self.state["beast_miniapp_last_sync_time"] = beast_time()
        self.state["beast_miniapp_last_error"] = ""
        self.state["beast_miniapp_sync_count"] = int(self.state.get("beast_miniapp_sync_count") or 0) + 1
        self.update_main_best_beast()
        active_patrol = next((item for item in beasts if "巡边" in str(item.get("status") or "")), None)
        if active_patrol:
            self.state["beast_border_patrol_name"] = active_patrol.get("full_name", "")
        self.main_beast_save()
        self.main_beast_logger().info("Main beast roster synced from Mini App: %s beasts", len(beasts))
        return True

    def normalize_main_beast_roster_quota(self):
        today = beast_now().strftime("%Y-%m-%d")
        if self.state.get("beast_roster_auto_query_date") == today:
            return False
        self.state["beast_roster_auto_query_date"] = today
        self.state["beast_roster_auto_query_count"] = 0
        return True

    def main_beast_roster_quota_reset_time(self):
        tomorrow = (beast_now() + timedelta(days=1)).replace(
            hour=0,
            minute=5,
            second=0,
            microsecond=0,
        )
        return beast_time(tomorrow)

    async def update_main_beast_cache(self, force=False):
        settings = self.main_beast_miniapp_settings()
        cache = list(self.state.get("beasts_cache") or [])
        trusted_cache = bool(
            cache
            and self.state.get("beast_roster_last_source") == "miniapp"
            and not self.state.get("beast_miniapp_last_error")
        )
        next_check = self.state.get("next_beast_status_check_time", "")
        if not settings["enabled"]:
            self.state["last_beast_roster_query_result"] = "miniapp_not_configured"
            self.state["beast_miniapp_last_error"] = "miniapp_not_configured"
            self.state["next_beast_status_check_time"] = beast_add_seconds(settings["retry_seconds"])
            self.main_beast_save()
            self.main_beast_logger().warning(
                "Main beast Mini App sync is not configured; deprecated .我的灵兽 will not be sent."
            )
            return False
        quota_changed = self.normalize_main_beast_roster_quota()
        if not force and next_check and beast_time_is_future(next_check):
            if quota_changed:
                self.main_beast_save()
            return trusted_cache

        automatic_count = int(self.state.get("beast_roster_auto_query_count") or 0)
        if not force and automatic_count >= ROSTER_DAILY_LIMIT:
            self.state["next_beast_status_check_time"] = self.main_beast_roster_quota_reset_time()
            self.main_beast_save()
            return trusted_cache
        if not force:
            automatic_count += 1
            self.state["beast_roster_auto_query_count"] = automatic_count

        self.state["beast_miniapp_last_attempt_time"] = beast_time()
        self.state["last_beast_roster_query_result"] = "miniapp_fetching"
        self.main_beast_save()
        try:
            snapshot = await fetch_miniapp_beast_snapshot(
                self.client,
                settings["entry_url"],
                cached_spirit_token=getattr(self, "_miniapp_beast_token", ""),
                bot_username=settings["bot_username"],
                timeout=settings["timeout"],
            )
            self._miniapp_beast_token = str(snapshot.get("spirit_token") or "")
            try:
                write_cached_spirit_token(
                    self.main_beast_config_dir(),
                    settings["entry_url"],
                    self._miniapp_beast_token,
                )
            except Exception:
                self.main_beast_logger().warning(
                    "Main beast Mini App token cache could not be persisted; continuing with memory cache.",
                    exc_info=True,
                )
            self.record_main_beast_miniapp_snapshot(snapshot)
            self.state["next_beast_status_check_time"] = (
                self.main_beast_roster_quota_reset_time()
                if not force and automatic_count >= ROSTER_DAILY_LIMIT
                else beast_add_seconds(settings["refresh_seconds"])
            )
            self.main_beast_save()
            return True
        except MiniAppBeastError as exc:
            self._miniapp_beast_token = ""
            error = exc.code
        except Exception as exc:
            self._miniapp_beast_token = ""
            error = type(exc).__name__.lower()
            self.main_beast_logger().exception("Main beast Mini App sync failed")
        self.state["last_beast_roster_query_result"] = "miniapp_error"
        self.state["beast_miniapp_last_error"] = error
        self.state["next_beast_status_check_time"] = (
            self.main_beast_roster_quota_reset_time()
            if not force and automatic_count >= ROSTER_DAILY_LIMIT
            else beast_add_seconds(settings["retry_seconds"])
        )
        self.main_beast_save()
        self.main_beast_logger().warning(
            "Main beast Mini App sync failed (%s); stale roster will not be used for a new beast action.",
            error,
        )
        return False

    async def run_main_beast_miniapp_timer(self):
        await self.startup_done.wait()
        self.main_beast_logger().info("Main beast Mini App roster scheduler started")
        while self.is_running:
            try:
                request = read_refresh_request(self.main_beast_config_dir())
                request_id = str(request.get("request_id") or "")
                last_request_id = str(self.state.get("beast_miniapp_refresh_request_id") or "")
                manual_refresh = bool(request_id and request_id != last_request_id)
                next_check = self.state.get("next_beast_status_check_time", "")
                due = not self.state.get("beasts_cache") or not next_check or not beast_time_is_future(next_check)
                if (manual_refresh or due) and not self.avatar_send_lock.locked():
                    async with self.beast_lock:
                        if manual_refresh:
                            self.state["beast_miniapp_refresh_request_id"] = request_id
                            self.main_beast_save()
                        await self.update_main_beast_cache(force=manual_refresh)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.main_beast_logger().exception("Main beast Mini App roster iteration failed")
            await asyncio.sleep(30)

    def main_beast_candidates(self, action):
        cache = list(self.state.get("beasts_cache") or [])
        focus_name = self.state.get("best_beast_name", "")
        candidates = []
        for beast in cache:
            name = beast.get("full_name", "")
            status = str(beast.get("status") or "未知")
            stamina = int(beast.get("stamina", -1))
            if any(marker in status for marker in ("受伤", "重伤", "治疗", "探险", "偷菜", "巡游", "巡边")):
                continue
            if action == "abyss" and stamina < ABYSS_MIN_STAMINA:
                continue
            if action == "patrol":
                if self.main_beast_name_matches(name, focus_name) or stamina < BORDER_PATROL_MIN_STAMINA:
                    continue
            candidates.append(beast)
        candidates.sort(key=lambda item: (int(item.get("stamina", -1)), int(item.get("power", 0))), reverse=True)
        if action == "abyss":
            focus = next((item for item in candidates if self.main_beast_name_matches(item.get("full_name"), focus_name)), None)
            ordered = []
            if focus and int(focus.get("stamina", -1)) >= FOCUS_PROTECT_STAMINA:
                ordered.append(focus)
            tier_one = [item for item in candidates if item is not focus and self.main_beast_tier(item) == 1]
            fallback = [item for item in candidates if item is not focus and item not in tier_one]
            ordered.extend(tier_one or fallback)
            return ordered
        return candidates

    def main_beast_is_wind_sparrow(self, beast):
        species = str((beast or {}).get("species") or "")
        species = re.sub(r"^[一二三四五六七八九十\d]+阶", "", species).strip()
        return species == "风雀"

    def select_main_beast_for_release(self, cache):
        indexed = list(enumerate(cache or []))
        species_groups = {}
        for index, beast in indexed:
            species_groups.setdefault(str(beast.get("species") or beast.get("full_name") or ""), []).append((index, beast))
        duplicate_candidates = [
            min(items, key=lambda pair: (int(pair[1].get("power") or 0), pair[0]))
            for items in species_groups.values()
            if len(items) > 1
        ]
        candidates = duplicate_candidates or indexed
        return min(candidates, key=lambda pair: (int(pair[1].get("power") or 0), pair[0]))[1] if candidates else None

    def main_beast_due(self, next_key, last_key, cooldown):
        next_time = self.state.get(next_key, "")
        if next_time:
            return not beast_time_is_future(next_time)
        last_time = parse_beast_time(self.state.get(last_key, ""))
        return not last_time or last_time + timedelta(seconds=cooldown) <= beast_now()

    def schedule_main_beast_retry(self, key, seconds=ACTION_RETRY_SECONDS):
        self.state[key] = beast_add_seconds(seconds)
        self.main_beast_save()

    def main_beast_action_paused(self, command):
        checker = getattr(self, "dashboard_command_paused", None)
        return bool(checker and checker(command, "主魂"))

    def record_main_hunt_response(self, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            self.schedule_main_beast_retry("next_hunt_time", HUNT_FAIL_RETRY_SECONDS)
            return False
        wait = self.main_beast_parse_wait(clean)
        if wait > 0 and any(marker in clean for marker in ("后再", "冷却", "吓跑", "再来")):
            self.state["next_hunt_time"] = beast_add_seconds(wait)
            self.main_beast_save()
            return True
        if any(marker in clean for marker in ("驯化成功", "建立了契约", "寻觅成功", "成功捕获")):
            now = beast_now()
            self.state["last_hunt_time"] = beast_time(now)
            self.state["next_hunt_time"] = beast_add_seconds(HUNT_CD_SECONDS, now)
            if "风雀" in clean:
                self.state["beast_hunt_stopped"] = True
                self.state["beast_hunt_stopped_reason"] = "寻觅到了风雀"
            self.main_beast_save()
            return True
        if any(marker in clean for marker in ("灵兽袋已满", "最多只能容纳", "腾出空间")):
            self.schedule_main_beast_retry("next_hunt_time", HUNT_FULL_RETRY_SECONDS)
            return True
        self.schedule_main_beast_retry("next_hunt_time", HUNT_FAIL_RETRY_SECONDS)
        return False

    async def run_main_beast_hunt_timer(self):
        await self.startup_done.wait()
        self.main_beast_logger().info("Main Wanling beast hunt scheduler started")
        while self.is_running:
            try:
                if self.state.get("beast_hunt_stopped") or self.main_beast_action_paused(".寻觅灵兽"):
                    await asyncio.sleep(MAX_LOOP_SLEEP_SECONDS)
                    continue
                if not self.main_beast_due("next_hunt_time", "last_hunt_time", HUNT_CD_SECONDS):
                    await asyncio.sleep(min(MAX_LOOP_SLEEP_SECONDS, max(30, beast_seconds_until(self.state.get("next_hunt_time")))))
                    continue
                if self.avatar_send_lock.locked():
                    await asyncio.sleep(30)
                    continue
                async with self.beast_lock:
                    if not self.main_beast_due("next_hunt_time", "last_hunt_time", HUNT_CD_SECONDS):
                        continue
                    cache = list(self.state.get("beasts_cache") or [])
                    if len(cache) >= 10 and self.main_beast_is_wind_sparrow(cache[9]):
                        self.state["beast_hunt_stopped"] = True
                        self.state["beast_hunt_stopped_reason"] = f"第十只灵兽种类是风雀：{cache[9].get('full_name', '风雀')}"
                        self.state["next_hunt_time"] = ""
                        self.main_beast_save()
                        continue
                    if len(cache) >= 10:
                        last_release = parse_beast_time(self.state.get("last_release_beast_time"))
                        if last_release and (beast_now() - last_release).total_seconds() < 600:
                            self.schedule_main_beast_retry("next_hunt_time", HUNT_FULL_RETRY_SECONDS)
                            continue
                        lowest = self.select_main_beast_for_release(cache)
                        if not lowest:
                            self.schedule_main_beast_retry("next_hunt_time", HUNT_FAIL_RETRY_SECONDS)
                            continue
                        release = await self.send_and_wait_feedback(f".放生 {lowest['full_name']}", timeout=60, max_retries=0)
                        release_text = self.main_beast_response_text(release)
                        if any(marker in release_text for marker in ("解除", "放生", "回归", "没有名为", "未找到")):
                            self.state["beasts_cache"] = [item for item in cache if item is not lowest]
                            self.state["last_release_beast_time"] = beast_time()
                            self.main_beast_save()
                        else:
                            self.schedule_main_beast_retry("next_hunt_time", HUNT_FAIL_RETRY_SECONDS)
                            continue
                    response = await self.send_and_wait_feedback(".寻觅灵兽", timeout=60, max_retries=0)
                    self.record_main_hunt_response(self.main_beast_response_text(response))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.main_beast_logger().exception("Main beast hunt iteration failed")
            await asyncio.sleep(60)

    async def normalize_main_beast_for_action(self, beast, action):
        name = beast.get("full_name", "")
        status = str(beast.get("status") or "未知")
        if "休息" in status or status in {"", "未知"}:
            return True
        if any(marker in status for marker in ("受伤", "重伤", "治疗", "巡边", "探险")):
            return False
        response = await self.send_and_wait_feedback(f".灵兽休息 {name}", timeout=60, max_retries=0)
        text = self.main_beast_response_text(response).replace("**", "")
        if any(marker in text for marker in ("召回", "休养", "休息中", "无需召回", "提前召回")):
            self.set_main_beast_status(name, "休息中")
            self.main_beast_save()
            return True
        wait = self.main_beast_parse_wait(text)
        if "受伤" in text or "重伤" in text or "休养" in text:
            self.set_main_beast_status(name, "受伤")
            if action == "abyss":
                self.state["next_abyss_time"] = beast_add_seconds(max(ACTION_RETRY_SECONDS, wait if wait > 0 else 0))
            self.main_beast_save()
        return False

    async def wait_main_abyss_result(self, response_msg, timeout=35):
        current = response_msg
        text = self.main_beast_response_text(current)
        message_id = getattr(current, "id", None)
        if not message_id or any(marker in text for marker in ("体力不足", "正忙", "后再", "险胜", "胜利", "失败", "击败")):
            return text
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.is_running:
            await asyncio.sleep(2)
            try:
                fresh = await self.client.get_messages(self.target_chat_id, ids=message_id)
                if isinstance(fresh, (list, tuple)):
                    fresh = fresh[0] if fresh else None
                if fresh:
                    current = fresh
                    text = self.main_beast_response_text(fresh) or text
            except Exception:
                continue
            if any(marker in text for marker in ("险胜", "胜利", "落败", "失败", "击败", "战利品", "体力不足")):
                break
        return text

    def record_main_abyss_response(self, name, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            return "retry"
        if "体力不足" in clean:
            current = re.search(r"当前\s*(\d+)", clean)
            required = re.search(r"(?:至少需要|需要)\s*(\d+)\s*点?体力", clean)
            stamina = int(current.group(1)) if current else max(0, int(required.group(1)) - 1) if required else 0
            self.set_main_beast_status(name, "休息中", stamina=stamina)
            self.main_beast_save()
            return "fallback"
        if "正忙" in clean or "无法进入万兽渊" in clean:
            return "normalize"
        if any(marker in clean for marker in ("险胜", "胜利", "成功击败", "战利品", "获得")):
            now = beast_now()
            injury_wait = self.main_beast_parse_wait(clean) if any(marker in clean for marker in ("受伤", "休养", "重伤")) else -1
            self.state["last_abyss_time"] = beast_time(now)
            self.state["next_abyss_time"] = beast_add_seconds(max(ABYSS_CD_SECONDS, injury_wait if injury_wait > 0 else 0), now)
            self.set_main_beast_status(name, "受伤" if injury_wait > 0 else "休息中")
            self.main_beast_save()
            return "success"
        if any(marker in clean for marker in ("落败", "战败", "未能击败")):
            now = beast_now()
            wait = self.main_beast_parse_wait(clean)
            self.state["last_abyss_time"] = beast_time(now)
            self.state["next_abyss_time"] = beast_add_seconds(max(ABYSS_CD_SECONDS, wait if wait > 0 else 0), now)
            self.set_main_beast_status(name, "受伤" if "伤" in clean else "休息中")
            self.main_beast_save()
            return "success"
        wait = self.main_beast_parse_wait(clean)
        if wait > 0 and any(marker in clean for marker in ("后再", "冷却", "尚需")):
            self.state["next_abyss_time"] = beast_add_seconds(wait)
            self.main_beast_save()
            return "cooldown"
        return "retry"

    async def execute_main_beast_abyss(self):
        if not await self.update_main_beast_cache():
            self.schedule_main_beast_retry("next_abyss_time", 30 * 60)
            return False
        for beast in self.main_beast_candidates("abyss"):
            name = beast.get("full_name", "")
            if not await self.normalize_main_beast_for_action(beast, "abyss"):
                continue
            response = await self.send_and_wait_feedback(
                f".探渊 {name}", timeout=60, max_retries=0, return_response_msg=True, delete_after=False
            )
            text = await self.wait_main_abyss_result(response)
            result = self.record_main_abyss_response(name, text)
            if result == "success" or result == "cooldown":
                return result == "success"
            if result == "normalize":
                self.set_main_beast_status(name, "出战中")
                if await self.normalize_main_beast_for_action(self.main_beast_by_name(name) or beast, "abyss"):
                    retry = await self.send_and_wait_feedback(
                        f".探渊 {name}", timeout=60, max_retries=0, return_response_msg=True, delete_after=False
                    )
                    retry_result = self.record_main_abyss_response(name, await self.wait_main_abyss_result(retry))
                    if retry_result in {"success", "cooldown"}:
                        return retry_result == "success"
            await asyncio.sleep(2)
        self.schedule_main_beast_retry("next_abyss_time", 30 * 60)
        return False

    def record_main_pasture_response(self, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            self.schedule_main_beast_retry("next_pasture_time")
            return False
        wait = self.main_beast_parse_wait(clean)
        if wait > 0 and any(marker in clean for marker in ("后再", "冷却", "归来")) and "万兽奔腾" not in clean:
            self.state["next_pasture_time"] = beast_add_seconds(wait)
            self.main_beast_save()
            return True
        if "万兽奔腾" in clean or ("放养" in clean and any(marker in clean for marker in ("灵兽谷", "万兽谷", "冲入", "出发"))):
            now = beast_now()
            count_match = re.search(r"等\s*(\d+)\s*只灵兽", clean)
            names_match = re.search(r"\n([^\n]+?)\s*欢快地冲入", clean)
            names = []
            if names_match:
                names = [part.strip(" *、，") for part in re.split(r"[、，,]", names_match.group(1)) if part.strip(" *、，")]
            for beast in self.state.get("beasts_cache") or []:
                if "休息" in str(beast.get("status") or ""):
                    beast["status"] = "放养中"
            self.update_main_best_beast()
            self.state["last_pasture_time"] = beast_time(now)
            self.state["next_pasture_time"] = beast_add_seconds(PASTURE_CD_SECONDS, now)
            self.state["pasture_pending_since"] = beast_time(now)
            self.state["pasture_pending_count"] = int(count_match.group(1)) if count_match else len(names)
            self.main_beast_save()
            return True
        if any(marker in clean for marker in ("没有可放养", "暂无可放养", "没有处于休息")):
            self.schedule_main_beast_retry("next_pasture_time", 30 * 60)
            return True
        self.schedule_main_beast_retry("next_pasture_time")
        return False

    def main_beast_interaction_target(self):
        return self.update_main_best_beast()

    def record_main_interaction_response(self, name, text):
        clean = str(text or "").replace("**", "")
        if not clean:
            self.schedule_main_beast_retry("next_beast_interaction_time")
            return False
        wait = self.main_beast_parse_wait(clean)
        if wait > 0 and any(marker in clean for marker in ("后再", "冷却", "尚需")):
            self.state["next_beast_interaction_time"] = beast_add_seconds(wait)
            self.main_beast_save()
            return True
        if "放养" in clean:
            target = self.state.get("next_pasture_time")
            self.state["next_beast_interaction_time"] = target if beast_time_is_future(target) else beast_add_seconds(30 * 60)
            self.set_main_beast_status(name, "放养中")
            self.main_beast_save()
            return True
        if "灵兽互动" in clean or any(marker in clean for marker in ("心情 +", "羁绊 +", "忠诚 +", "体力 +", "灵契")):
            now = beast_now()
            self.state["last_beast_interaction_time"] = beast_time(now)
            self.state["next_beast_interaction_time"] = beast_add_seconds(INTERACTION_CD_SECONDS, now)
            if "安抚" in clean or "平静" in clean:
                self.set_main_beast_status(name, "休息中")
            self.main_beast_save()
            return True
        self.schedule_main_beast_retry("next_beast_interaction_time")
        return False

    def record_main_patrol_response(self, name, text, returning=False):
        clean = str(text or "").replace("**", "")
        if not clean:
            self.schedule_main_beast_retry("next_beast_border_patrol_time")
            return False
        if returning and "巡边归来" in clean:
            old_name = self.state.get("beast_border_patrol_name", "")
            if old_name:
                self.set_main_beast_status(old_name, "休息中")
            self.state["beast_border_patrol_name"] = ""
            self.state["next_beast_border_patrol_time"] = ""
            self.main_beast_save()
            return True
        wait = self.main_beast_parse_wait(clean)
        if wait > 0 and any(marker in clean for marker in ("巡边", "巡行", "预计", "剩余", "还需")):
            now = beast_now()
            if name:
                self.state["beast_border_patrol_name"] = name
                self.set_main_beast_status(name, "巡边中")
            if not self.state.get("last_beast_border_patrol_time") or not returning:
                self.state["last_beast_border_patrol_time"] = beast_time(now)
            self.state["next_beast_border_patrol_time"] = beast_add_seconds(wait, now)
            self.main_beast_save()
            return True
        if "体力不足" in clean:
            current = re.search(r"当前\s*(\d+)", clean)
            if current:
                self.set_main_beast_status(name, "休息中", stamina=int(current.group(1)))
            self.main_beast_save()
            return False
        if any(marker in clean for marker in ("灵兽巡边", "边境巡行", "夜嗅敌营", "领命")):
            now = beast_now()
            self.state["last_beast_border_patrol_time"] = beast_time(now)
            self.state["next_beast_border_patrol_time"] = beast_add_seconds(BORDER_PATROL_CD_SECONDS, now)
            self.state["beast_border_patrol_name"] = name
            self.set_main_beast_status(name, "巡边中")
            self.main_beast_save()
            return True
        if any(marker in clean for marker in ("暂无灵兽巡边", "没有灵兽巡边")):
            self.state["beast_border_patrol_name"] = ""
            self.state["next_beast_border_patrol_time"] = ""
            self.main_beast_save()
            return True
        self.schedule_main_beast_retry("next_beast_border_patrol_time")
        return False

    async def execute_main_beast_patrol(self):
        active_name = str(self.state.get("beast_border_patrol_name") or "")
        if active_name:
            response = await self.send_and_wait_feedback(".巡边归来", timeout=60, max_retries=0)
            if not self.record_main_patrol_response(active_name, self.main_beast_response_text(response), returning=True):
                return False
        if not await self.update_main_beast_cache():
            self.schedule_main_beast_retry("next_beast_border_patrol_time", 30 * 60)
            return False
        attempted = set()
        while True:
            candidate = next((item for item in self.main_beast_candidates("patrol") if item.get("full_name") not in attempted), None)
            if not candidate:
                self.schedule_main_beast_retry("next_beast_border_patrol_time", 30 * 60)
                return False
            name = candidate.get("full_name", "")
            attempted.add(name)
            if not await self.normalize_main_beast_for_action(candidate, "patrol"):
                continue
            response = await self.send_and_wait_feedback(f".灵兽巡边 {name} 袭营", timeout=60, max_retries=0)
            if self.record_main_patrol_response(name, self.main_beast_response_text(response)):
                return True

    def record_main_pasture_return(self, text):
        clean = str(text or "").replace("**", "")
        if "灵兽归来" not in clean and not ("放养的灵兽" in clean and "归来" in clean):
            return False
        event_key = hashlib.sha1(clean.encode("utf-8", errors="ignore")).hexdigest()
        if self.state.get("last_pasture_return_event_key") == event_key:
            return True
        for beast in self.state.get("beasts_cache") or []:
            if "放养" in str(beast.get("status") or ""):
                beast["status"] = "休息中"
        for name, recovery in re.findall(r"【([^】]+)】[^\n]*体力恢复\s*(\d+)", clean):
            beast = self.main_beast_by_name(name)
            if beast:
                beast["stamina"] = min(100, int(beast.get("stamina", 0)) + int(recovery))
        self.state["pasture_pending_count"] = 0
        self.state["pasture_pending_since"] = ""
        self.state["last_pasture_return_time"] = beast_time()
        self.state["last_pasture_return_event_key"] = event_key
        self.state["next_pasture_time"] = beast_add_seconds(60)
        self.update_main_best_beast()
        self.main_beast_save()
        wakeup = getattr(self, "beast_wakeup", None)
        if wakeup is not None:
            wakeup.set()
        return True

    def record_manual_beast_command_response(self, command, text):
        command = str(command or "").strip()
        if command == ".我的灵兽":
            return self.record_main_beast_roster(text, source="manual .我的灵兽")
        if command == ".寻觅灵兽":
            return self.record_main_hunt_response(text)
        if command == ".一键放养":
            return self.record_main_pasture_response(text)
        if command.startswith(".灵兽互动 "):
            parts = command.split()
            return self.record_main_interaction_response(parts[1] if len(parts) > 1 else self.state.get("best_beast_name", ""), text)
        if command.startswith(".灵兽巡边 "):
            parts = command.split()
            return self.record_main_patrol_response(parts[1] if len(parts) > 1 else "", text)
        if command == ".巡边归来":
            return self.record_main_patrol_response(self.state.get("beast_border_patrol_name", ""), text, returning=True)
        if command == ".巡边状态":
            return self.record_main_patrol_response(self.state.get("beast_border_patrol_name", ""), text)
        if command.startswith(".探渊 ") or command.startswith(".灵兽探渊 "):
            parts = command.split()
            return self.record_main_abyss_response(parts[1] if len(parts) > 1 else "", text) != "retry"
        if command.startswith(".灵兽休息 "):
            parts = command.split()
            name = parts[1] if len(parts) > 1 else ""
            if any(marker in str(text or "") for marker in ("召回", "休息中", "休养", "无需召回", "提前召回")):
                self.set_main_beast_status(name, "休息中")
                self.main_beast_save()
                return True
        if command.startswith(".灵兽出战 "):
            parts = command.split()
            name = parts[1] if len(parts) > 1 else ""
            if any(marker in str(text or "") for marker in ("出战", "迎战", "已派")):
                self.set_main_beast_status(name, "出战中")
                self.main_beast_save()
                return True
        return False

    async def run_main_beast_action_timer(self):
        await self.startup_done.wait()
        self.main_beast_logger().info("Main Wanling beast action scheduler started")
        while self.is_running:
            sleep_for = MAX_LOOP_SLEEP_SECONDS
            try:
                if self.avatar_send_lock.locked():
                    await asyncio.sleep(30)
                    continue
                async with self.beast_lock:
                    due_abyss = self.main_beast_due("next_abyss_time", "last_abyss_time", ABYSS_CD_SECONDS)
                    due_patrol = self.main_beast_due("next_beast_border_patrol_time", "last_beast_border_patrol_time", BORDER_PATROL_CD_SECONDS)
                    due_pasture = self.main_beast_due("next_pasture_time", "last_pasture_time", PASTURE_CD_SECONDS)
                    if due_abyss and not self.main_beast_action_paused(".探渊 <灵兽>"):
                        await self.execute_main_beast_abyss()
                        await asyncio.sleep(2)
                    if due_patrol and not self.main_beast_action_paused(".灵兽巡边 <灵兽> 袭营"):
                        await self.execute_main_beast_patrol()
                        await asyncio.sleep(2)
                    if due_pasture and not self.main_beast_action_paused(".一键放养"):
                        response = await self.send_and_wait_feedback(".一键放养", timeout=60, max_retries=0)
                        self.record_main_pasture_response(self.main_beast_response_text(response))
                        await asyncio.sleep(2)
                future_times = [
                    beast_seconds_until(self.state.get(key))
                    for key in (
                        "next_abyss_time", "next_beast_border_patrol_time",
                        "next_pasture_time",
                    )
                    if beast_time_is_future(self.state.get(key))
                ]
                if future_times:
                    sleep_for = max(30, min(MAX_LOOP_SLEEP_SECONDS, min(future_times) + random.randint(5, 20)))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.main_beast_logger().exception("Main beast action iteration failed")
                sleep_for = 60
            try:
                self.beast_wakeup.clear()
                await asyncio.wait_for(self.beast_wakeup.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
