#!/usr/bin/env python3
"""
【通用固定冷却指令模块 —— 所有账号脚本共享】

提供 CommonCommandMixin 混入类，封装所有账号通用的固定冷却指令：
  1. 野外历练 —— 定时外出历练，策略可配置（谨慎/均衡/深入）
  2. 宗门战况/参战 —— 自动检测宗门战役，参战获取军勋
  3. 固定冷却指令的记录与重试逻辑

被 intelligent_cultivator.py、sub_cultivator.py、cultivator_xiaohao.py 继承使用。
"""
import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta

from log_utils import is_game_bot_sender, notify_unrecognized_response, text_targets_current_account


# =====================================================================
# 常量定义
# =====================================================================
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
FIELD_TRAINING_COMMAND = ".野外历练 谨慎"      # 野外历练指令（各账号可覆盖）
FIELD_TRAINING_CD_SECONDS = 2 * 3600           # 野外历练冷却 2 小时
SECT_WAR_STATUS_COMMAND = ".宗门战况"           # 查询宗门战况
SECT_WAR_JOIN_COMMAND = ".参战"                 # 参战指令
SECT_WAR_JOIN_CD_SECONDS = 2 * 3600            # 参战冷却 2 小时
SECT_WAR_RETRY_SECONDS = 10 * 60               # 宗门战重试间隔 10 分钟

# 已知宗门列表（用于解析宗门战双方）
KNOWN_SECTS = (
    "凌霄宫", "星宫", "万灵宗", "天星宗", "黄枫谷",
    "掩月宗", "落云宗", "古剑门", "百巧院", "鬼灵门",
    "合欢宗", "御灵宗", "天道盟", "九国盟",
)


# =====================================================================
# 时间工具函数
# =====================================================================

def dt_to_str(dt):
    return dt.strftime(TIME_FORMAT)

def now_str():
    return dt_to_str(datetime.now())

def str_to_dt(value):
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except Exception:
        return datetime.now()

def add_seconds_str(value, seconds):
    return dt_to_str(str_to_dt(value) + timedelta(seconds=seconds))

def is_future(value):
    try:
        return str_to_dt(value) > datetime.now()
    except Exception:
        return False

def seconds_until(value):
    try:
        return max(0, (str_to_dt(value) - datetime.now()).total_seconds())
    except Exception:
        return 0


# =====================================================================
# 默认状态数据
# =====================================================================

def common_command_default_state():
    """返回通用命令的默认状态字典"""
    return {
        "last_field_training_time": "",
        "next_field_training_time": "",
        "sect_name": "",
        "sect_war_left": "",
        "sect_war_right": "",
        "sect_war_active_until": "",
        "last_sect_war_status_time": "",
        "next_sect_war_status_time": "",
        "last_sect_war_join_time": "",
        "next_sect_war_join_time": "",
        "last_sect_war_response": "",
    }


# =====================================================================
# CommonCommandMixin 混入类
# =====================================================================

class CommonCommandMixin:
    """通用固定冷却指令混入类。"""

    # ---- 状态管理 ----

    def ensure_common_command_state(self):
        """确保状态字典包含所有默认键，并在 sect_name 变更时更新"""
        changed = False
        for key, value in common_command_default_state().items():
            if key not in self.state:
                self.state[key] = value
                changed = True
        sect = (getattr(self, "sect_name", "") or "").strip()
        if sect and self.state.get("sect_name") != sect:
            self.state["sect_name"] = sect
            changed = True
        if changed:
            self.save_state()

    def common_command_logger(self):
        """获取子类的日志记录器"""
        return logging.getLogger(self.__class__.__name__)

    # ---- 野外历练 ----

    def is_field_training_response(self, text):
        """判断游戏回复是否为野外历练相关的消息"""
        clean = (text or "").replace("**", "")
        return "野外历练" in clean or "山中灵机未复" in clean

    def is_field_training_command_result(self, text):
        """判断已归属到野外历练指令的回复是否代表本轮有结果。"""
        clean = (text or "").replace("**", "")
        return self.is_field_training_response(text) or ("卦象" in clean and "修为增加" in clean)

    def is_field_training_cooldown_response(self, text):
        """判断回复是否为野外历练冷却中"""
        clean = (text or "").replace("**", "")
        return any(k in clean for k in ["山中灵机未复", "冷却", "后再", "尚未", "请在"])

    def record_field_training_response(self, text, context="野外历练"):
        """
        记录野外历练的回复结果。
        解析冷却时间或成功状态，更新 next_field_training_time。
        """
        self.ensure_common_command_state()
        log = self.common_command_logger()
        if not text:
            self.state["next_field_training_time"] = add_seconds_str(now_str(), 600)
            self.save_state()
            log.warning(f"Field training: missing response; retry at {self.state['next_field_training_time']}.")
            return False

        cd = self.parse_wait_time(text)
        now = now_str()
        if self.is_field_training_cooldown_response(text) and cd > 0:
            self.state["next_field_training_time"] = add_seconds_str(now, cd)
            self.save_state()
            log.info(f"Field training cooldown from response: {cd}s, next at {self.state['next_field_training_time']}.")
            return True

        if self.is_field_training_command_result(text):
            self.state["last_field_training_time"] = now
            self.state["next_field_training_time"] = add_seconds_str(now, FIELD_TRAINING_CD_SECONDS)
            self.save_state()
            log.info(f"Field training recorded. Next at {self.state['next_field_training_time']}.")
            return True

        # 无法识别的回复：重试 10 分钟后
        notify_unrecognized_response(self, FIELD_TRAINING_COMMAND, text, log, context)
        self.state["next_field_training_time"] = add_seconds_str(now, 600)
        self.save_state()
        log.warning(f"Field training unrecognized response; skipped until {self.state['next_field_training_time']}.")
        return False

    def maybe_record_field_training_passive(self, msg, text):
        """
        被动同步野外历练状态。
        当群聊中出现本账号的野外历练结果时（由其他来源触发），
        直接记录冷却，避免重复发送。
        """
        if not self.is_field_training_response(text):
            return False
        if not text_targets_current_account(self, msg, text):
            return False
        return self.record_field_training_response(text, "野外历练被动同步")

    # ---- 宗门战 — 辅助方法 ----

    def account_sect_name(self):
        """获取本账号的宗门名称"""
        return (getattr(self, "sect_name", "") or self.state.get("sect_name", "") or "").strip()

    def clean_common_text(self, text):
        """去除 markdown 加粗标记和反引号"""
        return (text or "").replace("**", "").replace("`", "")

    def parse_sect_war_sides(self, text):
        """
        从宗门战消息中解析对战双方。
        先尝试用已知宗门列表匹配，再用正则匹配。
        """
        clean = self.clean_common_text(text)
        found = []
        for sect in KNOWN_SECTS:
            if sect in clean and sect not in found:
                found.append(sect)
        if len(found) >= 2:
            return found[0], found[1]

        # 正则匹配：如 "【凌霄宫】 vs 【星宫】"
        patterns = [
            r"【([^】]{2,12})】\s*(?:vs|VS|Vs|对阵|对战|迎战|挑战|伐山|攻打|攻|对)\s*【([^】]{2,12})】",
            r"([一-龥]{2,12})\s*(?:vs|VS|Vs|对阵|对战|迎战|挑战|伐山|攻打)\s*([一-龥]{2,12})",
        ]
        for pattern in patterns:
            match = re.search(pattern, clean)
            if match:
                left = match.group(1).strip()
                right = match.group(2).strip()
                if left and right and left != right:
                    return left, right
        return "", ""

    def sect_war_remaining_seconds(self, text):
        """从宗门战消息中提取剩余时间（秒）"""
        clean = self.clean_common_text(text)
        relevant_lines = []
        for line in clean.splitlines():
            if any(k in line for k in ["剩余", "持续", "结束", "战役", "战斗", "对战"]):
                relevant_lines.append(line)
        if relevant_lines:
            cd = self.parse_wait_time("\n".join(relevant_lines))
            if cd > 0:
                return cd
        return self.parse_wait_time(clean)

    def is_no_sect_war_response(self, text):
        """判断是否"暂无宗门战"的回复"""
        clean = self.clean_common_text(text)
        return bool(clean and any(k in clean for k in ["暂无", "没有", "未开启", "尚未开启"])
                    and any(k in clean for k in ["宗门战", "宗门对战", "战役", "战况"]))

    def is_sect_war_status_response(self, text):
        """判断消息是否为合法的宗门战况回复"""
        clean = self.clean_common_text(text)
        if not clean:
            return False
        left, right = self.parse_sect_war_sides(clean)
        return bool(left and right) or self.is_no_sect_war_response(clean)

    def sect_war_account_involved(self):
        """判断本账号的宗门是否在当前宗门战中"""
        sect = self.account_sect_name()
        if not sect:
            return False
        return sect in {self.state.get("sect_war_left", ""), self.state.get("sect_war_right", "")}

    def sect_war_is_active(self):
        """判断宗门战是否仍在有效期内"""
        active_until = self.state.get("sect_war_active_until", "")
        return bool(active_until and is_future(active_until))

    def sect_war_message_should_trigger_status(self, text):
        """判断是否应该因为某条消息触发宗门战况查询"""
        clean = self.clean_common_text(text)
        return "战役" in clean

    # ---- 宗门战 — 状态记录 ----

    def record_sect_war_status_response(self, text):
        """记录宗门战况回复：解析对战双方、剩余时间"""
        self.ensure_common_command_state()
        log = self.common_command_logger()
        now = now_str()
        if not text:
            self.state["next_sect_war_status_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
            self.save_state()
            return False

        left, right = self.parse_sect_war_sides(text)
        remaining = self.sect_war_remaining_seconds(text)
        self.state["last_sect_war_status_time"] = now
        self.state["last_sect_war_response"] = self.clean_common_text(text)[:500]

        if left and right:
            self.state["sect_war_left"] = left
            self.state["sect_war_right"] = right
            if remaining > 0:
                self.state["sect_war_active_until"] = add_seconds_str(now, remaining)
            self.state["next_sect_war_status_time"] = ""
            self.save_state()
            return True

        if self.is_no_sect_war_response(text):
            self.state["sect_war_left"] = ""
            self.state["sect_war_right"] = ""
            self.state["sect_war_active_until"] = ""
            self.state["next_sect_war_status_time"] = ""
            self.save_state()
            return True

        notify_unrecognized_response(self, SECT_WAR_STATUS_COMMAND, text, log, "宗门战况")
        self.state["next_sect_war_status_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
        self.save_state()
        return False

    # ---- 宗门战 — 参战 ----

    def is_sect_war_join_cooldown_response(self, text):
        return any(k in self.clean_common_text(text) for k in ["冷却", "后再", "尚需", "还需", "休整", "调息"])

    def is_sect_war_join_success_response(self, text):
        return any(k in self.clean_common_text(text) for k in [
            "参战成功", "加入战场", "奔赴战场", "投入战斗", "已参战",
            "参与了宗门", "前线请战", "个人军勋", "你本次获得",
        ])

    def is_sect_war_join_unavailable_response(self, text):
        return any(k in self.clean_common_text(text) for k in ["暂无", "没有", "未开启", "不在对战", "不属于交战", "无法参战"])

    def record_sect_war_join_response(self, text):
        """记录参战结果"""
        self.ensure_common_command_state()
        log = self.common_command_logger()
        now = now_str()
        if not text:
            # 保留旧有的冷却时间，避免频繁重试
            current_next = self.state.get("next_sect_war_join_time", "")
            if current_next and is_future(current_next):
                return False
            self.state["next_sect_war_join_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
            self.save_state()
            return False

        cd = self.parse_wait_time(text)
        if self.is_sect_war_join_cooldown_response(text) and cd > 0:
            self.state["next_sect_war_join_time"] = add_seconds_str(now, cd)
            self.save_state()
            return True

        if self.is_sect_war_join_success_response(text):
            self.state["last_sect_war_join_time"] = now
            self.state["next_sect_war_join_time"] = add_seconds_str(now, SECT_WAR_JOIN_CD_SECONDS)
            self.save_state()
            return True

        if self.is_sect_war_join_unavailable_response(text):
            # 无法参战：清空缓存，等待下一次触发
            self.state["sect_war_left"] = ""
            self.state["sect_war_right"] = ""
            self.state["sect_war_active_until"] = ""
            self.state["next_sect_war_status_time"] = ""
            self.state["next_sect_war_join_time"] = ""
            self.save_state()
            return True

        notify_unrecognized_response(self, SECT_WAR_JOIN_COMMAND, text, log, "宗门参战")
        self.state["next_sect_war_join_time"] = add_seconds_str(now, SECT_WAR_RETRY_SECONDS)
        self.save_state()
        return False

    # ---- 宗门战 — 自动参战流程 ----

    async def maybe_join_sect_war_now(self):
        """
        如果宗门战正在进行且本账号宗门参战，自动发送.参战。
        使用独立锁防止并发多次参战。
        """
        lock = getattr(self, "_sect_war_join_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_sect_war_join_lock", lock)

        async with lock:
            self.ensure_common_command_state()
            log = self.common_command_logger()
            active_until = self.state.get("sect_war_active_until", "")
            if not active_until or not is_future(active_until):
                return False
            if not self.sect_war_account_involved():
                return False
            next_join = self.state.get("next_sect_war_join_time", "")
            if next_join and is_future(next_join):
                return False
            log.info(f"Sect war active for {self.account_sect_name()}; sending {SECT_WAR_JOIN_COMMAND}.")
            join_resp = await self.send_and_wait_feedback(SECT_WAR_JOIN_COMMAND, timeout=90)
            return self.record_sect_war_join_response(join_resp)

    async def fetch_sect_war_status_from_trigger(self):
        """由消息触发：机器人发送战役消息时，自动查询宗门战况"""
        try:
            log = self.common_command_logger()
            log.info(f"Sect war keyword detected from bot; sending {SECT_WAR_STATUS_COMMAND}.")
            status_resp = await self.send_and_wait_feedback(SECT_WAR_STATUS_COMMAND, timeout=90)
            self.record_sect_war_status_response(status_resp)
            await self.maybe_join_sect_war_now()
        finally:
            setattr(self, "_sect_war_status_task", None)

    def maybe_handle_sect_war_message(self, msg, text, sender):
        """
        被动检测群聊中的战役消息，自动触发宗门战况查询和参战。
        有多层保护避免误触发（只检测非回复消息、排除已有冷却的账号等）。
        """
        if not sender or not is_game_bot_sender(self, sender):
            return False
        if not self.sect_war_message_should_trigger_status(text):
            return False
        # 回复消息已经由 feedback 机制处理，不需要再触发
        if getattr(msg, 'reply_to', None):
            return False
        if self.is_sect_war_join_success_response(text) or self.is_sect_war_join_cooldown_response(text):
            return False
        if self.is_no_sect_war_response(text):
            return False

        if self.is_sect_war_status_response(text):
            self.record_sect_war_status_response(text)
            task = asyncio.create_task(self.maybe_join_sect_war_now())
            setattr(self, "_sect_war_join_task", task)
            return True

        if self.sect_war_is_active():
            return True

        next_status = self.state.get("next_sect_war_status_time", "")
        if next_status and is_future(next_status):
            return True

        task = getattr(self, "_sect_war_status_task", None)
        if task and not task.done():
            return True

        self.state["next_sect_war_status_time"] = add_seconds_str(now_str(), SECT_WAR_RETRY_SECONDS)
        self.save_state()
        task = asyncio.create_task(self.fetch_sect_war_status_from_trigger())
        setattr(self, "_sect_war_status_task", task)
        return True

    # ---- 主循环 ----

    async def run_field_training_loop(self):
        """野外历练主循环：定时发送野外历练指令"""
        self.ensure_common_command_state()
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(20, 80))

        while self.is_running:
            # 只检查本地冷却时不切身份；真正发送主魂命令时由 send_and_wait_feedback 对齐。
            self.ensure_common_command_state()
            next_time = self.state.get("next_field_training_time", "")
            if next_time and is_future(next_time):
                wait_sec = seconds_until(next_time)
                await asyncio.sleep(min(wait_sec, 600))
                continue

            log = self.common_command_logger()
            cmd = getattr(self, 'field_training_command', FIELD_TRAINING_COMMAND)
            log.info(f"Field training due: sending {cmd}.")
            resp = await self.send_and_wait_feedback(cmd, timeout=90)
            self.record_field_training_response(resp)
            await asyncio.sleep(5)

    async def run_sect_war_loop(self):
        """宗门战主循环：检测宗门战有效期并在可参战时自动参战"""
        self.ensure_common_command_state()
        await self.startup_done.wait()
        await asyncio.sleep(random.randint(30, 90))

        while self.is_running:
            # 只检查本地冷却时不切身份；真正发送主魂命令时由 send_and_wait_feedback 对齐。
            self.ensure_common_command_state()
            next_join = self.state.get("next_sect_war_join_time", "")
            active_until = self.state.get("sect_war_active_until", "")
            # 宗门战过期：清空缓存
            if active_until and not is_future(active_until):
                self.state["sect_war_left"] = ""
                self.state["sect_war_right"] = ""
                self.state["sect_war_active_until"] = ""
                self.state["next_sect_war_join_time"] = ""
                self.save_state()
                await asyncio.sleep(60)
                continue

            if active_until and self.sect_war_account_involved():
                if next_join and is_future(next_join):
                    wait_sec = min(seconds_until(next_join), seconds_until(active_until), 600)
                    await asyncio.sleep(max(5, wait_sec))
                    continue
                await self.maybe_join_sect_war_now()
                await asyncio.sleep(5)
                continue

            await asyncio.sleep(600)
