"""
【掌天瓶·凝液/养树自动化】

主号主魂专属：每日一轮「.掌天瓶 凝液」→「.掌天瓶 养树」。
凝液 24 小时冷却，冷却剩余时间从游戏响应中解析；
树胚存在时才养树（无树胚时跳过养树，只凝液保存绿液）。
身份守卫：仅在主魂身份执行；天道禁制（魂印冻结）时停用并通知。
"""
import asyncio
import random
import re

from common_command_features import add_seconds_str, is_future, now_str, seconds_until

SKY_BOTTLE_CONDENSE_COMMAND = ".掌天瓶 凝液"
SKY_BOTTLE_NURTURE_COMMAND = ".掌天瓶 养树"
SKY_BOTTLE_STATUS_COMMAND = ".掌天瓶"

SKY_BOTTLE_COOLDOWN_FALLBACK_SECONDS = 24 * 3600
SKY_BOTTLE_COOLDOWN_BUFFER_SECONDS = 300
SKY_BOTTLE_RETRY_SECONDS = 10 * 60
SKY_BOTTLE_UNKNOWN_RETRY_SECONDS = 30 * 60
SKY_BOTTLE_LOOP_MAX_SLEEP_SECONDS = 3600


def sky_bottle_default_state():
    return {
        "enabled": True,
        "next_condense_time": "",
        "last_condense_time": "",
        "last_condense_response": "",
        "liquid_count": 0,
        "has_tree_embryo": False,
        "last_nurture_time": "",
        "last_nurture_response": "",
        "embryo_source_text": "",
        "disabled_reason": "",
        "last_status": "",
        "last_detail": "",
        "last_action_at": "",
    }


class SkyBottleMixin:
    """掌天瓶凝液/养树循环。安装到主号主魂（intelligent_cultivator.Cultivator）。"""

    def get_sky_bottle_state(self):
        state = self.state.setdefault("sky_bottle", {})
        defaults = sky_bottle_default_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        return state

    def _sky_bottle_logger(self):
        return getattr(self, "log", None) or __import__("logging").getLogger(__name__)

    def sky_bottle_enabled(self):
        state = self.get_sky_bottle_state()
        if not state.get("enabled", True):
            return False
        if str(state.get("disabled_reason") or "").strip():
            return False
        return True

    def sky_bottle_set_embryo(self, text, source="auto"):
        """从游戏消息中被动捕获树胚获得事件（树胚机缘：@xxx 获得【灵眼树胚】x1）。"""
        text = str(text or "")
        if "灵眼树胚" not in text:
            return False
        state = self.get_sky_bottle_state()
        # 真实消息格式：树胚机缘：**@Weeguu** 获得 **【灵眼树胚】x1**。
        gained = re.search(
            r"树胚机缘[：:]\s*\**@?(\S+?)\**\s*获得\s*\**【灵眼树胚】x?(\d+)\**",
            text,
        )
        if gained:
            username = gained.group(1)
            count = int(gained.group(2) or 1)
            my_username = str(getattr(self, "expected_username", "") or "").lstrip("@").casefold()
            if my_username and username.lstrip("@").casefold() == my_username:
                state["has_tree_embryo"] = True
                state["embryo_depleted"] = False
                state["embryo_source_text"] = text[:200]
                state["embryo_count"] = int(state.get("embryo_count") or 0) + count
                state["last_action_at"] = now_str()
                self.save_state()
                return True
        return False

    def record_sky_bottle_manual_response(self, command, text):
        """手动指令响应同步：用户手动凝液/养树后，状态机自动对齐。"""
        cmd = str(command or "").strip()
        text = str(text or "")
        state = self.get_sky_bottle_state()
        updated = False
        if cmd == SKY_BOTTLE_CONDENSE_COMMAND:
            if "掌天瓶·凝液" in text and "当前绿液" in text:
                m = re.search(r"当前绿液[：:]\s*\*{0,2}(\d+)\s*/\s*(\d+)", text)
                if m:
                    state["liquid_count"] = int(m.group(1))
                    state["last_condense_time"] = now_str()
                    state["last_condense_response"] = text[:200]
                    state["next_condense_time"] = add_seconds_str(
                        now_str(), SKY_BOTTLE_COOLDOWN_FALLBACK_SECONDS
                    )
                    state["last_status"] = "condense_success"
                    state["last_detail"] = "手动凝液成功"
                    state["last_action_at"] = now_str()
                    updated = True
            elif "尚未再度圆满" in text:
                wait = self.parse_wait_time(text) if hasattr(self, "parse_wait_time") else -1
                if wait and wait > 0:
                    state["next_condense_time"] = add_seconds_str(now_str(), wait)
                    state["last_status"] = "condense_cooldown"
                    state["last_detail"] = f"手动凝液冷却，剩余 {wait}s"
                    state["last_action_at"] = now_str()
                    updated = True
            elif "天道禁制" in text:
                state["disabled_reason"] = "天道禁制（魂印冻结）"
                state["last_status"] = "disabled_heaven_ban"
                state["last_detail"] = text[:200]
                state["last_action_at"] = now_str()
                updated = True
        elif cmd == SKY_BOTTLE_NURTURE_COMMAND:
            if "掌天瓶·养树" in text and "炼成了" in text:
                m = re.search(r"当前绿液[：:]\s*\*{0,2}(\d+)\s*/\s*(\d+)", text)
                state["liquid_count"] = int(m.group(1)) if m else 0
                state["has_tree_embryo"] = False
                state["last_nurture_time"] = now_str()
                state["last_nurture_response"] = text[:200]
                state["last_status"] = "nurture_success"
                state["last_detail"] = "手动养树成功"
                state["last_action_at"] = now_str()
                updated = True
        if updated:
            self.save_state()
        return updated

    async def sky_bottle_condense(self):
        """发送 .掌天瓶 凝液 并解析响应。返回等待秒数。"""
        state = self.get_sky_bottle_state()
        log = self._sky_bottle_logger()
        resp = await self.send_and_wait_feedback(
            SKY_BOTTLE_CONDENSE_COMMAND,
            timeout=60,
            max_retries=0,
        )
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else str(resp or "")
        log.info("掌天瓶凝液响应：%s", resp_text[:150])
        if "掌天瓶·凝液" in resp_text and "当前绿液" in resp_text:
            m = re.search(r"当前绿液[：:]\s*\*{0,2}(\d+)\s*/\s*(\d+)", resp_text)
            if m:
                state["liquid_count"] = int(m.group(1))
            state["last_condense_time"] = now_str()
            state["last_condense_response"] = resp_text[:200]
            state["next_condense_time"] = add_seconds_str(
                now_str(), SKY_BOTTLE_COOLDOWN_FALLBACK_SECONDS
            )
            state["last_status"] = "condense_success"
            state["last_detail"] = "凝液成功"
            state["last_action_at"] = now_str()
            self.save_state()
            return 5
        if "尚未再度圆满" in resp_text:
            wait = self.parse_wait_time(resp_text) if hasattr(self, "parse_wait_time") else -1
            if wait is None or wait <= 0:
                wait = SKY_BOTTLE_RETRY_SECONDS
            wait = max(60, int(wait))
            state["next_condense_time"] = add_seconds_str(
                now_str(), wait + SKY_BOTTLE_COOLDOWN_BUFFER_SECONDS
            )
            state["last_status"] = "condense_cooldown"
            state["last_detail"] = f"凝液冷却中，剩余 {wait}s"
            state["last_action_at"] = now_str()
            self.save_state()
            return wait + SKY_BOTTLE_COOLDOWN_BUFFER_SECONDS
        if "天道禁制" in resp_text:
            state["disabled_reason"] = "天道禁制（魂印冻结）"
            state["last_status"] = "disabled_heaven_ban"
            state["last_detail"] = resp_text[:200]
            state["last_action_at"] = now_str()
            self.save_state()
            await self.sky_bottle_notify_user("掌天瓶已被天道禁制冻结（魂印缺失），自动循环已停用。")
            return SKY_BOTTLE_LOOP_MAX_SLEEP_SECONDS
        if "尚未重聚" in resp_text:
            # 身份不对（化身身份发出）——等化身循环结束后重试
            state["last_status"] = "wrong_identity"
            state["last_detail"] = resp_text[:200]
            state["last_action_at"] = now_str()
            self.save_state()
            return SKY_BOTTLE_RETRY_SECONDS
        # 未知响应
        state["last_status"] = "unknown_response"
        state["last_detail"] = resp_text[:200]
        state["last_action_at"] = now_str()
        self.save_state()
        return SKY_BOTTLE_UNKNOWN_RETRY_SECONDS

    async def sky_bottle_nurture(self):
        """发送 .掌天瓶 养树（储物袋默认有树胚存货；缺货时游戏会明确回复）。返回等待秒数。"""
        state = self.get_sky_bottle_state()
        log = self._sky_bottle_logger()
        if state.get("embryo_depleted"):
            return None
        if int(state.get("liquid_count") or 0) <= 0:
            # 无绿液：需要先凝液
            return await self.sky_bottle_condense()
        resp = await self.send_and_wait_feedback(
            SKY_BOTTLE_NURTURE_COMMAND,
            timeout=60,
            max_retries=0,
        )
        resp_text = getattr(resp, "text", "") if hasattr(resp, "text") else str(resp or "")
        log.info("掌天瓶养树响应：%s", resp_text[:150])
        if "掌天瓶·养树" in resp_text and "炼成了" in resp_text:
            m = re.search(r"当前绿液[：:]\s*\*{0,2}(\d+)\s*/\s*(\d+)", resp_text)
            if m:
                state["liquid_count"] = int(m.group(1))
            state["has_tree_embryo"] = True
            state["embryo_depleted"] = False
            state["last_nurture_time"] = now_str()
            state["last_nurture_response"] = resp_text[:200]
            state["last_status"] = "nurture_success"
            state["last_detail"] = "养树成功"
            state["last_action_at"] = now_str()
            self.save_state()
            return 5
        if any(marker in resp_text for marker in ("没有灵眼树胚", "尚无树胚", "树胚不足", "没有足够的树胚")):
            # 储物袋树胚用尽——标记缺货，只凝液存绿液，直到再次捕获获得事件
            state["embryo_depleted"] = True
            state["has_tree_embryo"] = False
            state["last_status"] = "embryo_depleted"
            state["last_detail"] = resp_text[:200]
            state["last_action_at"] = now_str()
            self.save_state()
            await self.sky_bottle_notify_user(
                "储物袋灵眼树胚已用尽，掌天瓶转入只凝液模式（绿液留存，等树胚补充）。"
            )
            return None
        if "尚无绿液" in resp_text:
            # 绿液意外为 0（消耗后状态未同步）——重置并先凝液
            state["liquid_count"] = 0
            self.save_state()
            return await self.sky_bottle_condense()
        if "尚未重聚" in resp_text:
            state["last_status"] = "wrong_identity"
            state["last_detail"] = resp_text[:200]
            state["last_action_at"] = now_str()
            self.save_state()
            remote = SKY_BOTTLE_RETRY_SECONDS
            return remote
        if "天道禁制" in resp_text:
            state["disabled_reason"] = "天道禁制（魂印冻结）"
            state["last_status"] = "disabled_heaven_ban"
            state["last_detail"] = resp_text[:200]
            state["last_action_at"] = now_str()
            self.save_state()
            await self.sky_bottle_notify_user("掌天瓶已被天道禁制冻结（魂印缺失），自动循环已停用。")
            return SKY_BOTTLE_LOOP_MAX_SLEEP_SECONDS
        state["last_status"] = "unknown_response"
        state["last_detail"] = resp_text[:200]
        state["last_action_at"] = now_str()
        self.save_state()
        return SKY_BOTTLE_UNKNOWN_RETRY_SECONDS

    async def sky_bottle_notify_user(self, message):
        """重要异常时私发通知给 Waaiging。"""
        client = getattr(self, "client", None)
        if client is None:
            return
        try:
            await client.send_message(8219248252, f"[掌天瓶] {message}")
        except Exception:
            pass

    async def run_sky_bottle_loop(self, initial_delay=0, sleep_func=None):
        """掌天瓶主循环：凝液冷却到点 → 凝液 → 有树胚则养树。"""
        await self.startup_done.wait()
        state = self.get_sky_bottle_state()
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)
        log = self._sky_bottle_logger()
        while getattr(self, "is_running", True):
            try:
                await self.pause_event.wait()
                if not self.sky_bottle_enabled():
                    await asyncio.sleep(600)
                    continue
                # 主魂身份守卫
                if hasattr(self, "_wait_for_main_identity"):
                    await self._wait_for_main_identity()
                wait = await self.sky_bottle_tick()
                if hasattr(self, "common_scheduler_sleep_seconds"):
                    sleep_seconds = self.common_scheduler_sleep_seconds(
                        wait + random.randint(5, 30),
                        minimum=30,
                        sleep_func=sleep_func,
                    )
                else:
                    sleep_seconds = max(30, min(int(wait), SKY_BOTTLE_LOOP_MAX_SLEEP_SECONDS))
                log.info("Sky bottle loop sleeping %ss.", sleep_seconds)
                await asyncio.sleep(sleep_seconds)
            except Exception as exc:
                log.error("Sky bottle loop error: %s", exc, exc_info=True)
                await asyncio.sleep(SKY_BOTTLE_RETRY_SECONDS)

    async def sky_bottle_tick(self):
        state = self.get_sky_bottle_state()
        # 绿液在手且未缺货：立即养树（不等凝液冷却——养树只耗树胚+绿液）
        if not state.get("embryo_depleted") and int(state.get("liquid_count") or 0) > 0:
            nurture_wait = await self.sky_bottle_nurture()
            if nurture_wait is not None:
                return nurture_wait
        next_time = str(state.get("next_condense_time") or "")
        if next_time and is_future(next_time):
            return seconds_until(next_time)
        # 到点：先养树（储物袋默认有树胚存货+有绿液），否则凝液。
        # has_tree_embryo 仅在游戏明确回复"无树胚"类错误时置 False（缺货标记）。
        out_of_embryo = state.get("embryo_depleted")
        if not out_of_embryo and int(state.get("liquid_count") or 0) > 0:
            wait = await self.sky_bottle_nurture()
            if wait is not None:
                return wait
        wait = await self.sky_bottle_condense()
        if wait <= 10 and not state.get("embryo_depleted") and int(state.get("liquid_count") or 0) > 0:
            # 凝液刚成功且储物袋可能还有树胚：立刻补一次养树，不等下一轮
            nurture_wait = await self.sky_bottle_nurture()
            if nurture_wait is not None:
                return nurture_wait
        return wait
