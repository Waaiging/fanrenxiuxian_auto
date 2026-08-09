#!/usr/bin/env python3
"""Single-soul Tianxing cultivator for the restricted @Waaiging account."""

import asyncio
import os
from datetime import datetime, timedelta

import intelligent_cultivator as core
from command_modules import DEFAULT_WAAIGING_FIELD_TRAINING_COMMAND
from group_visibility_control import run_telegram_write_permission_monitor


CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "cultivator_waaiging.log")
STATE_FILE = os.path.join(CONFIG_DIR, "state_waaiging.json")
SESSION_NAME = "waaiging_session"
SECT_JOIN_COMMAND = ".拜入宗门 天星宗"
SECT_JOIN_COOLDOWN_FALLBACK_SECONDS = 24 * 3600
SECT_JOIN_UNKNOWN_RETRY_SECONDS = 60 * 60
TIANXING_PREFIX_DELAY_SECONDS = 3
TIANXING_DESTINY_RETRY_SECONDS = 30 * 60
TIANXING_DESTINY_CHOICES = ("贪狼", "太阴", "紫微", "天府")


class WaaigingCultivator(core.Cultivator):
    """Run only @Waaiging's Tianxing main soul; this account has no avatars."""

    def __init__(self, session_name=SESSION_NAME):
        core.configure_runtime_files(CONFIG_FILE, LOG_FILE, STATE_FILE)
        super().__init__(session_name=session_name)

        self.account_key = "waaiging"
        self.expected_username = "Waaiging"
        self.sect_name = "天星宗"
        self.identity_sect_names = {"主魂": "天星宗"}
        self.identity_usernames = {"主魂": ["Waaiging"]}
        self.field_training_command = DEFAULT_WAAIGING_FIELD_TRAINING_COMMAND
        self.lingxiao_enabled = False

        # This account has no avatars. Clear every inherited avatar mapping so
        # replies from other accounts cannot be attributed to a local identity.
        self.avatars = []
        self.avatar_identities = {}
        self.avatar_nicknames = {}
        self.avatar_usernames = {}
        self.avatar_features = {}
        self._avatar_chat_ids = {}
        self.actual_cooldown_probe_commands = {("主魂", ".探寻裂缝")}
        self._current_identity = "主魂"
        self._main_confirmed = True
        self.state["current_identity"] = "主魂"
        self.state["sect_name"] = self.sect_name
        self.state["identity_sect_names"] = dict(self.identity_sect_names)
        avatars_removed = self.state.pop("avatars", None) is not None
        self.state.setdefault("sect_join_confirmed", False)
        self.state.setdefault("sect_join_status", "pending")
        self.state.setdefault("next_sect_join_time", "")
        self.state.setdefault("sect_join_not_before", "")
        self.state.setdefault("last_sect_join_response", "")
        self.state.setdefault("last_destiny_date", "")
        self.state.setdefault("last_destiny_time", "")
        self.state.setdefault("last_destiny_choice", "")
        self.state.setdefault("next_tianxing_destiny_retry_time", "")

        # The account is controlled by the main process when the game group is
        # private. It must not start another copy of that controller itself.
        self.manage_restricted_accounts = False
        self.xiaohao_visibility_control_enabled = False
        self.enable_avatar_tasks = False
        self.enable_spirit_tree = False
        self.enable_main_beasts = False
        self.enable_soul_curse = False

        # The account's treasure and spirit names are unknown. Do not send a
        # different account's hard-coded commands until they are configured.
        self.enable_treasure_touch = False
        self.enable_nurture_spirit = False
        self.enable_small_world = False
        disabled_state_changed = False
        for key in (
            "next_treasure_touch_time",
            "next_nurture_spirit_time",
            "next_small_world_time",
            "next_small_world_calamity_time",
            "next_miracle_preach_time",
        ):
            if self.state.get(key):
                self.state[key] = ""
                disabled_state_changed = True
        if self.state.get("small_world_calamity_pending"):
            self.state["small_world_calamity_pending"] = False
            disabled_state_changed = True
        if disabled_state_changed or avatars_removed:
            self.save_state()

        # Match the existing restricted xiaohao send protection exactly.
        self.enable_telegram_write_permission_monitor = True
        self.telegram_write_restriction_retry_enabled = True
        self.telegram_send_protection_retry_seconds = max(
            60,
            int(self.mc.get("telegram_send_protection_retry_seconds", 15 * 60) or 15 * 60),
        )
        self.telegram_write_permission_poll_seconds = max(
            30,
            int(self.mc.get("telegram_write_permission_poll_seconds", 60) or 60),
        )

    def get_identity_from_msg(self, msg):
        sender_id = getattr(msg, "sender_id", None)
        my_id = getattr(getattr(self, "my_info", None), "id", None)
        return "主魂" if sender_id and my_id and int(sender_id) == int(my_id) else None

    def account_sect_name(self):
        if not self.state.get("sect_join_confirmed"):
            return ""
        return str(
            getattr(self, "sect_name", "")
            or self.state.get("sect_name")
            or "天星宗"
        ).strip()

    def identity_sect_name(self, identity="主魂"):
        if str(identity or "主魂") == "主魂" and not self.state.get("sect_join_confirmed"):
            return ""
        return super().identity_sect_name(identity)

    def stale_scheduler_due_items(self, overdue_seconds=core.SCHEDULER_STALE_DUE_SECONDS):
        stale = super().stale_scheduler_due_items(overdue_seconds=overdue_seconds)
        disabled_keys = set()
        if not self.enable_treasure_touch:
            disabled_keys.add("next_treasure_touch_time")
        if not self.enable_nurture_spirit:
            disabled_keys.add("next_nurture_spirit_time")
        if not self.enable_small_world:
            disabled_keys.update((
                "next_small_world_time",
                "next_small_world_calamity_time",
                "next_miracle_preach_time",
            ))
        return [item for item in stale if item[0] not in disabled_keys]

    async def _send_tianxing_prefixes(self, commands, action):
        for command in commands:
            core.log.info("Tianxing %s prefix: sending %s.", action, command)
            response = await super().send_and_wait_feedback(
                command,
                timeout=60,
                max_retries=0,
            )
            if not self.response_text(response).strip():
                core.log.warning(
                    "Tianxing %s prefix %s had no confirmed feedback; blocking the target command.",
                    action,
                    command,
                )
                return False
            await asyncio.sleep(TIANXING_PREFIX_DELAY_SECONDS)
        return True

    async def send_and_wait_feedback(self, message, *args, **kwargs):
        command = str(message or "").strip()
        prefixes = ()
        action = ""
        if self.state.get("sect_join_confirmed"):
            if command.startswith(".闭关修炼"):
                prefixes = (".推命 闭关",)
                action = "meditation"

        if not prefixes:
            return await super().send_and_wait_feedback(message, *args, **kwargs)

        async with self.common_atomic_task(f"Tianxing-{action}"):
            if action == "meditation" and not await self.ensure_tianxing_destiny_for_action(
                "主魂", "cultivation"
            ):
                return None
            if not await self._send_tianxing_prefixes(prefixes, action):
                return None
            return await super().send_and_wait_feedback(message, *args, **kwargs)

    def _tianxing_destiny_window(self, now=None):
        now = now or datetime.now()
        start = now.replace(
            hour=core.DESTINY_WINDOW_START_HOUR,
            minute=core.DESTINY_WINDOW_START_MINUTE,
            second=0,
            microsecond=0,
        )
        end = now.replace(
            hour=core.DESTINY_WINDOW_END_HOUR,
            minute=core.DESTINY_WINDOW_END_MINUTE,
            second=59,
            microsecond=999999,
        )
        return start, end

    def tianxing_destiny_wait_seconds(self, now=None):
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        start, _ = self._tianxing_destiny_window(now)
        if self.state.get("last_destiny_observation_date") != today:
            if now < start:
                return max(0, int((start - now).total_seconds()))
            retry_at = str(self.state.get("next_tianxing_destiny_retry_time") or "")
            if retry_at and core.is_future(retry_at):
                return max(1, int(core.seconds_until(retry_at)))
            return 0

        tomorrow_start = (now + timedelta(days=1)).replace(
            hour=core.DESTINY_WINDOW_START_HOUR,
            minute=core.DESTINY_WINDOW_START_MINUTE,
            second=0,
            microsecond=0,
        )
        return max(60, int((tomorrow_start - now).total_seconds()))

    def _defer_tianxing_destiny(self, reason, seconds=TIANXING_DESTINY_RETRY_SECONDS):
        deep_end = str(self.state.get("deep_meditation_end_time") or "")
        if self.state.get("in_deep_meditation") and deep_end and core.is_future(deep_end):
            retry_at = core.add_seconds_str(deep_end, 60)
        else:
            retry_at = core.add_seconds_str(core.now_str(), max(60, int(seconds)))
        self.state["next_tianxing_destiny_retry_time"] = retry_at
        self.save_state()
        core.log.info("Tianxing destiny deferred until %s: %s.", retry_at, reason)

    async def _tianxing_destiny_check(self):
        if not self.state.get("sect_join_confirmed"):
            return False
        if self.dashboard_command_paused(".观命", "主魂"):
            return False

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.state.get("last_destiny_observation_date") == today:
            return True
        start, end = self._tianxing_destiny_window(now)
        if now < start:
            return False

        defer_cutoff = end - timedelta(minutes=core.DESTINY_DEFER_CUTOFF_MINUTES)
        if now < defer_cutoff and self.daily_one_shot_should_defer(
            "主魂", ".观命", logger=core.log
        ):
            return False

        async with self.common_atomic_task("Tianxing-destiny"):
            completed = await self.observe_tianxing_destiny("主魂", force=True)
            if not completed:
                self._defer_tianxing_destiny(".观命 was not confirmed")
                return False
            self.state["next_tianxing_destiny_retry_time"] = ""
            self.save_state()
            return True

    async def run_tianxing_destiny_loop(self):
        await self.startup_done.wait()
        while self.is_running:
            try:
                if not self.state.get("sect_join_confirmed"):
                    await asyncio.sleep(300)
                    continue
                wait_seconds = self.tianxing_destiny_wait_seconds()
                if wait_seconds > 0:
                    await asyncio.sleep(core.scheduler_sleep_seconds(wait_seconds))
                    continue
                completed = await self._tianxing_destiny_check()
                await asyncio.sleep(300 if completed else 60)
            except Exception as exc:
                core.log.error("Tianxing destiny loop error: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    def record_sect_join_response(self, response):
        text = str(response or "").replace("**", "")
        self.state["last_sect_join_response"] = text[:500]
        if "天星宗" in text and any(
            marker in text for marker in ("成功拜入", "已是", "已经是", "本门弟子")
        ):
            self.state["sect_join_confirmed"] = True
            self.state["sect_join_status"] = "joined"
            self.state["sect_joined_at"] = core.now_str()
            self.state["next_sect_join_time"] = ""
            self.state["sect_join_not_before"] = ""
            done = self.state.setdefault("done", [])
            if ".宗门点卯" in done:
                done.remove(".宗门点卯")
            self.save_state()
            return "joined"

        if "不会接纳" in text or "天道烙印" in text or "叛出宗门" in text:
            wait_seconds = int(self.parse_wait_time(text) or 0)
            if wait_seconds <= 0:
                wait_seconds = SECT_JOIN_COOLDOWN_FALLBACK_SECONDS
            self.state["sect_join_status"] = "cooldown"
            next_attempt = core.add_seconds_str(
                core.now_str(),
                wait_seconds + 60,
            )
            not_before = str(self.state.get("sect_join_not_before") or "")
            if (
                not_before
                and core.is_future(not_before)
                and core.seconds_until(not_before) > core.seconds_until(next_attempt)
            ):
                next_attempt = not_before
            self.state["next_sect_join_time"] = next_attempt
            self.save_state()
            return "cooldown"

        self.state["sect_join_status"] = "retry"
        self.state["next_sect_join_time"] = core.add_seconds_str(
            core.now_str(),
            SECT_JOIN_UNKNOWN_RETRY_SECONDS,
        )
        self.save_state()
        return "retry"

    async def run_sect_join_loop(self):
        await self.startup_done.wait()
        await asyncio.sleep(20)
        while self.is_running:
            if self.state.get("sect_join_confirmed"):
                await asyncio.sleep(300)
                continue
            next_attempt = str(self.state.get("next_sect_join_time") or "")
            if next_attempt and core.is_future(next_attempt):
                await asyncio.sleep(core.scheduler_sleep_seconds(core.seconds_until(next_attempt)))
                continue
            core.log.info("Tianxing sect join due: sending %s.", SECT_JOIN_COMMAND)
            response = await self.send_and_wait_feedback(
                SECT_JOIN_COMMAND,
                timeout=60,
                max_retries=0,
            )
            status = self.record_sect_join_response(response)
            core.log.info(
                "Tianxing sect join result: %s; next=%s.",
                status,
                self.state.get("next_sect_join_time") or "none",
            )
            await asyncio.sleep(5)

    async def run_daily_tasks(self):
        await self.startup_done.wait()
        while self.is_running and not self.state.get("sect_join_confirmed"):
            await asyncio.sleep(300)
        if self.is_running:
            return await super().run_daily_tasks()
        return None

    async def run_cultivation_loop(self):
        self.create_scheduler_task("sect_join", lambda: self.run_sect_join_loop())
        self.create_scheduler_task("tianxing_destiny", lambda: self.run_tianxing_destiny_loop())
        return await super().run_cultivation_loop()

    async def run_telegram_write_permission_monitor(self):
        await run_telegram_write_permission_monitor(self, core.log)


if __name__ == "__main__":
    cultivator = WaaigingCultivator()
    try:
        asyncio.run(cultivator.start())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        core.log.error("Waaiging cultivator fatal error: %s", exc, exc_info=True)
