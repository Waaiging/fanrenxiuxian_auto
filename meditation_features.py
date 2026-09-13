"""Per-identity meditation selection shared by normal and restricted workers."""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta

from automation_settings import meditation_identity_settings, set_meditation_heqi_pill_enabled
from log_utils import (
    actor_account_key,
    is_deep_meditation_ongoing_response,
    is_deep_meditation_settlement_response,
    is_not_deep_meditation_response,
    send_text_alert,
)
from miniapp_beast import MiniAppCircuitOpenError, miniapp_circuit_wait_seconds
from sect_rules import SectTaskStopped


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_COMMAND_SELECTION = ContextVar("meditation_command_selection", default=None)


def _after(seconds):
    return (datetime.now() + timedelta(seconds=max(0, seconds))).strftime(TIME_FORMAT)


def _remaining(value):
    try:
        return max(0, (datetime.strptime(value, TIME_FORMAT) - datetime.now()).total_seconds())
    except (ValueError, TypeError):
        return 0


def cultivation_succeeded(text):
    clean = str(text or "").replace("**", "").replace("闭关失败损失", "")
    return any(word in clean for word in ("闭关成功", "修炼成功", "获得修为", "闭关收益")) and not any(
        word in clean for word in ("失败", "无法", "不足", "冷却中")
    )


class _MeditationSelectionChanged(Exception):
    pass


class MeditationModeMixin:
    def meditation_config(self, identity="主魂"):
        return meditation_identity_settings(actor_account_key(self) or "main", identity)

    def identity_meditation_mode(self, identity="主魂"):
        config = self.meditation_config(identity)
        return config["mode"] if config["enabled"] else "disabled"

    def required_meditation_destiny(self, identity="主魂"):
        """Return the destiny required before this soul's daily cultivation."""
        identity = self.resolve_avatar_identity(identity)
        if self.identity_sect_name(identity) == "天星宗" and self.identity_meditation_mode(identity) == "daily":
            return "紫微"
        return ""

    def meditation_command_paused(self, command, identity="主魂"):
        scope = _COMMAND_SELECTION.get()
        if scope is not None and scope[0] is self and scope[1] == identity:
            current = self.meditation_config(identity)
            if not current["enabled"] or any(
                current[field] != scope[2][field] for field in ("mode", "switch_id")
            ):
                return True
            if command == ".服用 合气丹" and not current["use_heqi_pill"]:
                return True
        command = str(command or "").strip()
        if command not in {".深度闭关", ".闭关修炼"}:
            return False
        mode = self.identity_meditation_mode(identity)
        return mode == "disabled" or (command == ".深度闭关" and mode != "deep")

    def _meditation_identity_state(self, identity):
        return self.state if identity == "主魂" else self.get_avatar_state(identity)

    def _meditation_runtime(self, identity):
        state = self._meditation_identity_state(identity)
        if not isinstance(state.get("meditation_runtime"), dict):
            legacy_mode = state.get("tianxing_meditation_prepared_mode", "")
            state["meditation_runtime"] = {
                "prepared_mode": "daily" if legacy_mode == "fate" else legacy_mode,
                "prepared_switch_id": state.get("tianxing_meditation_prepared_switch_id", ""),
                "success_count": max(0, int(state.get("tianxing_fate_success_count") or 0)),
            }
        return state["meditation_runtime"]

    def _meditation_selection_current(self, identity, expected, command=""):
        current = self.meditation_config(identity)
        if not current["enabled"] or any(
            current[field] != expected[field] for field in ("mode", "switch_id")
        ):
            return False
        if command == ".服用 合气丹" and not current["use_heqi_pill"]:
            return False
        if self.state.get("is_paused"):
            return False
        pause_event = getattr(self, "pause_event", None)
        if pause_event is not None and not pause_event.is_set():
            return False
        if self.identity_pause_seconds(identity) > 0:
            return False
        return not command or not self.dashboard_command_paused(command, identity)

    async def _send_meditation_command(self, identity, command, config):
        if not self._meditation_selection_current(identity, config, command):
            raise _MeditationSelectionChanged()
        # The feedback layer otherwise retries even when max_retries is zero.
        # Let this scheduler retry so it can recheck mode, identity and prefixes.
        options = {"timeout": 90, "max_retries": 0, "retry_on_timeout": False}
        if command == ".查看闭关":
            # A user mode switch must inspect the server even during the local guard.
            options["force_meditation_check"] = True
        token = _COMMAND_SELECTION.set((self, identity, dict(config)))
        try:
            if identity == "主魂":
                response = await self.send_and_wait_feedback(command, **options)
            else:
                response = await self.send_and_wait_feedback_identity(identity, command, **options)
        finally:
            _COMMAND_SELECTION.reset(token)
        return self.response_text(response).replace("**", "")

    def _clear_meditation_deep_state(self, identity):
        self._meditation_identity_state(identity).update({
            "in_deep_meditation": False,
            "deep_meditation_end_time": "",
            "deep_meditation_guard_until": "",
            "next_meditation_retry_time": "",
            "next_meditation_time": "",
            "meditation_restart_pending": False,
            "meditation_restart_mode": "",
        })
        self.save_state()

    def _meditation_retry(self, identity, config, text, seconds=300):
        runtime = self._meditation_runtime(identity)
        runtime.update(
            next_retry_at=_after(max(60, seconds)),
            retry_switch_id=config["switch_id"],
            retry_reason="",
            last_result=str(text or "未收到确认回复")[:240],
        )
        self.save_state()

    def _defer_meditation_for_missing_destiny(self, identity, config):
        required = self.unavailable_meditation_destiny(identity)
        if not required:
            return False
        # Candidates are fixed for the day. Keep this wait local to meditation;
        # exploration and crafting still use their own available destiny choices.
        retry_at = (datetime.now() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).strftime(TIME_FORMAT)
        waiting = {
            "next_retry_at": retry_at,
            "retry_switch_id": config["switch_id"],
            "retry_reason": "destiny_unavailable",
            "last_result": f"今日候选没有{required}，日常闭关等待 {retry_at} 重新观命",
        }
        runtime = self._meditation_runtime(identity)
        if any(runtime.get(key) != value for key, value in waiting.items()):
            runtime.update(waiting)
            self.save_state()
            self.common_command_logger().info(
                "[%s] 日常闭关等待候选刷新：今日候选没有%s，下次检查 %s",
                identity, required, retry_at,
            )
        return True

    def _meditation_wait(self, text, default=300):
        seconds = self.parse_wait_time(text)
        return seconds if seconds > 0 else default

    async def _meditation_use_pill(self, identity, config):
        if getattr(self, "_restricted_miniapp_worker", None) is not None:
            self._meditation_runtime(identity)["pill_status"] = "waiting_group_permission"
            self.save_state()
            return False
        text = await self._send_meditation_command(identity, ".服用 合气丹", config)
        shortage = any(word in text for word in ("没有足够", "不足", "未拥有", "数量不够", "没有合气丹"))
        if shortage:
            set_meditation_heqi_pill_enabled(
                actor_account_key(self) or "main", identity, False,
                updated_by="runtime-pill-shortage",
            )
            await send_text_alert(
                self, "闭关合气丹已停用",
                f"{identity} 的合气丹不足，已取消该身份的“服用合气丹”，后续继续日常闭关。",
                self.common_command_logger(),
            )
        success = bool(text) and not shortage and not any(
            word in text for word in ("失败", "无法", "不能", "冷却")
        ) and any(word in text for word in (
            "成功服用", "服用成功", "已服用", "服用了", "服下", "修为增加", "执行成功",
        ))
        if success:
            self._meditation_runtime(identity)["pill_status"] = "used"
            self._meditation_runtime(identity)["next_daily_at"] = ""
            self._meditation_identity_state(identity)["next_meditation_time"] = ""
            self.save_state()
        if not success:
            self._meditation_runtime(identity)["last_result"] = f"合气丹回复未确认：{text[:160]}"
            self.save_state()
        return success

    async def _prepare_identity_meditation(self, identity, config):
        runtime = self._meditation_runtime(identity)
        state = self._meditation_identity_state(identity)
        if (
            runtime.get("prepared_mode") == config["mode"]
            and runtime.get("prepared_switch_id", "") == config["switch_id"]
            and (config["mode"] == "deep" or not state.get("in_deep_meditation"))
        ):
            return True
        # Existing deep schedules retain their cached state on the first upgrade.
        if config["mode"] == "deep" and not config["switch_id"] and not runtime.get("prepared_mode"):
            runtime.update(prepared_mode="deep", prepared_switch_id="")
            self.save_state()
            return True

        text = await self._send_meditation_command(identity, ".查看闭关", config)
        ongoing = is_deep_meditation_ongoing_response(text)
        idle = is_not_deep_meditation_response(text) or is_deep_meditation_settlement_response(text)
        if not ongoing and not idle:
            self._meditation_retry(identity, config, text, self._meditation_wait(text))
            return False
        if ongoing and config["mode"] == "daily":
            force = await self._send_meditation_command(identity, ".强行出关", config)
            confirmed = bool(force) and not any(
                word in force for word in ("失败", "无法", "不能", "冷却", "不足")
            ) and any(word in force for word in (
                "强行出关", "强行中断", "出关成功", "已出关", "闭关结束", "未处于深度闭关",
            ))
            if not confirmed:
                self._meditation_retry(identity, config, force)
                return False
            self._clear_meditation_deep_state(identity)
            if self.meditation_config(identity)["use_heqi_pill"]:
                # Persist before sending: an uncertain reply/restart must not consume twice.
                attempt = config["switch_id"] or "daily"
                if runtime.get("switch_pill_attempted") != attempt:
                    runtime["switch_pill_attempted"] = attempt
                    self.save_state()
                    await self._meditation_use_pill(identity, config)
        elif ongoing:
            seconds = self.parse_wait_time(text)
            if seconds > 0:
                state.update(self.meditation_active_state_values(_after(seconds)))
            else:
                state["in_deep_meditation"] = True
            self.save_state()
        elif idle:
            self._clear_meditation_deep_state(identity)
            if config["mode"] == "deep":
                deep = await self._send_meditation_command(identity, ".深度闭关", config)
                if not is_deep_meditation_ongoing_response(deep) and not (
                    "深度闭关" in deep and any(word in deep for word in ("开启成功", "闭关已开启", "开始了", "已开启"))
                ):
                    self._meditation_retry(identity, config, deep, self._meditation_wait(deep))
                    return False
                seconds = self.parse_wait_time(deep)
                state.update(self.meditation_active_state_values(_after(seconds) if seconds > 0 else ""))
                self.save_state()

        if not self._meditation_selection_current(identity, config):
            raise _MeditationSelectionChanged()
        runtime.update(prepared_mode=config["mode"], prepared_switch_id=config["switch_id"], next_retry_at="")
        self.save_state()
        return True

    async def _daily_meditation_once(self, identity, config):
        if self.identity_sect_name(identity) == "天星宗":
            if not await self.ensure_tianxing_destiny_for_action(identity, "cultivation"):
                if not self._defer_meditation_for_missing_destiny(identity, config):
                    self._meditation_retry(
                        identity, config, "紫微命星未确认",
                        max(300, self.tianxing_destiny_retry_wait_seconds(identity)),
                    )
                return None
            prefix = await self._send_meditation_command(identity, ".推命 闭关", config)
            if not self.tianxing_prefix_response_ok(".推命 闭关", prefix):
                self._meditation_retry(identity, config, prefix, self.tianxing_prefix_wait_seconds(prefix) or 300)
                return None
        return await self._send_meditation_command(identity, ".闭关修炼", config)

    def _record_daily_meditation(self, identity, text):
        runtime = self._meditation_runtime(identity)
        runtime["last_result"] = text[:240]
        if cultivation_succeeded(text):
            runtime["success_count"] = max(0, int(runtime.get("success_count") or 0)) + 1
            runtime["last_time"] = datetime.now().strftime(TIME_FORMAT)
        wait = self._meditation_wait(text)
        runtime["next_daily_at"] = _after(max(60, wait))
        self._meditation_identity_state(identity)["next_meditation_time"] = runtime["next_daily_at"]
        self.save_state()

    async def configured_meditation_tick(self, identity="主魂"):
        """Return True when selection/switch/daily handling replaces the deep scheduler."""
        identity = self.resolve_avatar_identity(identity)
        config = self.meditation_config(identity)
        if not config["enabled"]:
            return True
        runtime = self._meditation_runtime(identity)
        if runtime.get("retry_reason") == "destiny_unavailable" and (
            runtime.get("retry_switch_id", "") != config["switch_id"]
            or not self.unavailable_meditation_destiny(identity)
        ):
            # A refreshed observation, new day, sect change or mode switch may
            # make cultivation possible before the saved wait expires.
            runtime.update(next_retry_at="", retry_reason="")
            self.save_state()
        if (
            runtime.get("retry_switch_id", "") == config["switch_id"]
            and _remaining(runtime.get("next_retry_at")) > 0
        ):
            return True
        command = ".闭关修炼" if config["mode"] == "daily" else ".深度闭关"
        if not self._meditation_selection_current(identity, config, command):
            return True
        try:
            async with self.common_atomic_task(f"Meditation-mode-{identity}"):
                if not self._meditation_selection_current(identity, config, command):
                    return True
                if not await self._prepare_identity_meditation(identity, config):
                    return True
                if config["mode"] == "deep":
                    return False
                if _remaining(runtime.get("next_daily_at")) > 0:
                    return True
                text = await self._daily_meditation_once(identity, config)
                if text is None:
                    return True
                self._record_daily_meditation(identity, text)
                count = runtime.get("success_count", 0)
                current = self.meditation_config(identity)
                if (
                    cultivation_succeeded(text) and current["use_heqi_pill"]
                    and count > 0 and count % 2 == 0 and runtime.get("last_pill_count") != count
                ):
                    runtime["last_pill_count"] = count
                    self.save_state()
                    if await self._meditation_use_pill(identity, config):
                        # Every cultivation, including the extra post-pill one, gets its own prefix.
                        followup = await self._daily_meditation_once(identity, config)
                        if followup is not None:
                            self._record_daily_meditation(identity, followup)
                return True
        except (_MeditationSelectionChanged, SectTaskStopped):
            return True
        except asyncio.CancelledError:
            raise
        except MiniAppCircuitOpenError as exc:
            self._meditation_retry(identity, config, str(exc), miniapp_circuit_wait_seconds(exc, 60))
        except Exception as exc:
            self._meditation_retry(identity, config, str(exc))
            self.common_command_logger().exception("[%s] meditation mode tick failed", identity)
        return True
